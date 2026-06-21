"""Scrape Mercari listing details into listing_details_raw via the item JSON API.

Phase: listing-detail scraping, JSON-API edition. The item HTML page is a
server-rendered shell that only exposes title/price in meta tags, so we now use
the same web JSON API that scrape_search.py uses for search:

    GET https://api.mercari.jp/items/get?id=<listing_id>
    headers: X-Platform: web  +  DPoP: <ES256 JWT>

The DPoP signing, User-Agent and Origin are reused directly from
scrape_search.py (one self-signed ES256 key per process; no login). The
response is {"result":"OK","data":{...item...}} and the item object carries
every field we need: name, description, price, status, item_condition.name,
shipping_payer.name, seller.name.

For each candidate flagged needs_detail_scrape=1 this script fetches the item
JSON, saves the raw response to data/raw_pages/ as
{game_id}_listing_{listing_id}_{timestamp}.json, parses the fields above, and
upserts one row per listing_id into listing_details_raw.

Polite by default: a small --limit, a delay between requests, per-listing error
isolation. Run several small batches rather than one big scrape.

Usage:
    python scripts/scrape_listing.py [--game-id GAME_ID] [--limit N]
                                     [--delay-seconds S] [--force]
    python scripts/scrape_listing.py --self-test   # offline parser check
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import requests

# Reuse scrape_search.py's proven Mercari web-API access (DPoP signing, UA,
# Origin) and small helpers instead of duplicating them. The script's own
# directory is on sys.path when run as `python scripts/scrape_listing.py`; the
# insert makes the import robust regardless of CWD.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from scrape_search import ORIGIN, USER_AGENT, load_games, make_dpop, now  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "configs" / "games.yaml"
DB_PATH = ROOT / "data" / "mercari.sqlite"
RAW_PAGES_DIR = ROOT / "data" / "raw_pages"

API_ITEM_URL = "https://api.mercari.jp/items/get"
REQUEST_TIMEOUT = 25  # seconds

FIELDS = (
    "title_raw",
    "description_raw",
    "price_raw",
    "status_raw",
    "condition_raw",
    "shipping_raw",
    "seller_name_raw",
)


# --- parsing ------------------------------------------------------------------
def data_from_response(payload: dict) -> dict | None:
    """The item object from a /items/get response, or None if it isn't OK."""
    if (
        isinstance(payload, dict)
        and payload.get("result") == "OK"
        and isinstance(payload.get("data"), dict)
    ):
        return payload["data"]
    return None


def parse_item(data: dict) -> dict:
    """Map the item JSON to listing_details_raw fields. Raw values, no
    normalization; missing/empty fields become None. All keys always present."""
    condition = data.get("item_condition") or {}
    shipping = data.get("shipping_payer") or {}
    seller = data.get("seller") or {}
    price = data.get("price")
    return {
        "title_raw": data.get("name") or None,
        "description_raw": data.get("description") or None,
        "price_raw": None if price is None else str(price),
        "status_raw": data.get("status") or None,
        "condition_raw": condition.get("name") or None,
        "shipping_raw": shipping.get("name") or None,
        "seller_name_raw": seller.get("name") or None,
    }


# --- db -----------------------------------------------------------------------
def select_candidates(conn: sqlite3.Connection, game_id: str | None) -> list[dict]:
    """Candidates needing a detail scrape, deduped by listing_id (best rank
    first). A listing matched under two games is fetched once -- listing_details_raw
    is keyed by listing_id only."""
    sql = (
        "SELECT game_id, listing_id, listing_url "
        "FROM search_results_candidates WHERE needs_detail_scrape = 1"
    )
    params: list = []
    if game_id:
        sql += " AND game_id = ?"
        params.append(game_id)
    sql += " ORDER BY best_rank IS NULL, best_rank ASC, listing_id"

    seen: set = set()
    out: list[dict] = []
    for g, listing_id, listing_url in conn.execute(sql, params):
        if listing_id in seen:
            continue
        seen.add(listing_id)
        out.append({"game_id": g, "listing_id": listing_id, "listing_url": listing_url})
    return out


def existing_detail_ids(conn: sqlite3.Connection) -> set:
    return {r[0] for r in conn.execute("SELECT DISTINCT listing_id FROM listing_details_raw")}


def upsert_detail(
    conn: sqlite3.Connection, listing_id: str, fields: dict, raw_page_path: str
) -> str:
    """INSERT a new row, or UPDATE the existing one (only reached for existing
    rows under --force; non-force existing rows are filtered out before fetch)."""
    row = conn.execute(
        "SELECT id FROM listing_details_raw WHERE listing_id = ? LIMIT 1", (listing_id,)
    ).fetchone()
    values = [fields[k] for k in FIELDS]
    if row:
        conn.execute(
            f"""UPDATE listing_details_raw
                SET scraped_at = ?, {', '.join(f'{k} = ?' for k in FIELDS)},
                    raw_page_path = ?
                WHERE id = ?""",
            [now(), *values, raw_page_path, row[0]],
        )
        return "updated"
    conn.execute(
        f"""INSERT INTO listing_details_raw
            (listing_id, scraped_at, {', '.join(FIELDS)}, raw_page_path)
            VALUES ({', '.join('?' * (len(FIELDS) + 3))})""",
        [listing_id, now(), *values, raw_page_path],
    )
    return "inserted"


# --- storage / fetch ----------------------------------------------------------
def save_json(game_id: str, listing_id: str, text: str) -> Path:
    RAW_PAGES_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = RAW_PAGES_DIR / f"{game_id}_listing_{listing_id}_{ts}.json"
    path.write_text(text, encoding="utf-8")
    return path


def fetch_item(session: requests.Session, listing_id: str) -> tuple[str, dict | None]:
    """Return (raw_text, item_data|None). item_data is None if the API didn't
    return a usable item (non-200, bad JSON, or result != OK)."""
    url = f"{API_ITEM_URL}?id={listing_id}"
    headers = {
        "Accept": "*/*",
        "X-Platform": "web",
        "Origin": ORIGIN,
        "Referer": ORIGIN + "/",
        "DPoP": make_dpop("GET", url),
    }
    resp = session.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    text = resp.text
    if resp.status_code != 200:
        return text, None
    try:
        payload = json.loads(text)
    except ValueError:
        return text, None
    return text, data_from_response(payload)


# --- driver -------------------------------------------------------------------
def run(args: argparse.Namespace) -> None:
    if not DB_PATH.exists():
        raise SystemExit(f"Database not found: {DB_PATH}. Run scripts/init_db.py first.")

    games = load_games(CONFIG_PATH)
    if args.game_id and args.game_id not in games:
        raise SystemExit(f"Unknown game_id: {args.game_id}. Known: {', '.join(games)}")

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "ja,en;q=0.8"})

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        candidates = select_candidates(conn, args.game_id)
        existing = existing_detail_ids(conn)

        total_found = len(candidates)
        if args.force:
            todo = candidates
            already = 0
        else:
            todo = [c for c in candidates if c["listing_id"] not in existing]
            already = total_found - len(todo)
        todo = todo[: args.limit]

        scope = f" game_id={args.game_id}" if args.game_id else ""
        print(f"Candidates needing detail (needs_detail_scrape=1{scope}): {total_found}")
        print(f"Already have details (skipped, no --force): {already}")
        print(f"Will fetch this run (limit {args.limit}): {len(todo)}")
        if not todo:
            print("Nothing to fetch.")
            return

        inserted = updated = failed = 0
        field_counts: Counter = Counter()
        n = len(todo)
        for i, c in enumerate(todo, 1):
            listing_id = c["listing_id"]
            try:
                text, data = fetch_item(session, listing_id)
            except Exception as e:  # network/timeout: isolate, log, continue
                print(f"  [{i}/{n}] {listing_id}  json=ERROR ({e})  -> skipped")
                failed += 1
                if i < n:
                    time.sleep(args.delay_seconds)
                continue

            # Keep the raw response either way (debugging); only the table is
            # left clean. ponytail: failures aren't recorded as rows, so they
            # naturally retry next run -- raw JSON is the breadcrumb. Shops
            # items (/shops/product/) aren't served here and fall through here.
            raw_path = save_json(c["game_id"], listing_id, text)
            rel = str(raw_path.relative_to(ROOT))

            if data is None:
                print(f"  [{i}/{n}] {listing_id}  json=ok status!=OK  -> skipped (raw kept)")
                failed += 1
                if i < n:
                    time.sleep(args.delay_seconds)
                continue

            fields = parse_item(data)
            op = upsert_detail(conn, listing_id, fields, rel)
            conn.commit()
            if op == "inserted":
                inserted += 1
            else:
                updated += 1

            populated = [k for k in FIELDS if fields[k] is not None]
            field_counts.update(populated)
            print(
                f"  [{i}/{n}] {listing_id}  json=ok  {op}  "
                f"price={fields['price_raw']} status={fields['status_raw']} "
                f"cond={fields['condition_raw']!r}  fields={len(populated)}/{len(FIELDS)}"
            )
            if i < n:
                time.sleep(args.delay_seconds)

        print(f"\nDone. selected={total_found} skipped_present={already} "
              f"inserted={inserted} updated={updated} failed={failed} (of {n} fetched)")
        if field_counts:
            ok = inserted + updated
            print("Field population (of successful rows):")
            for k in FIELDS:
                print(f"  {k:16} {field_counts.get(k, 0)}/{ok}")
        print("Tip: re-run to continue the next batch; use --force to re-scrape existing rows.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-id", help="Limit to a single game_id from games.yaml")
    parser.add_argument("--limit", type=int, default=50, help="Max listings to fetch this run (default 50)")
    parser.add_argument("--delay-seconds", type=float, default=2.0, help="Delay between requests (default 2.0)")
    parser.add_argument("--force", action="store_true", help="Re-fetch listings that already have details")
    parser.add_argument("--self-test", action="store_true", help="Run offline parser check and exit")
    args = parser.parse_args()

    if args.self_test:
        _self_test()
        return
    if args.delay_seconds < 1.0:
        print(f"note: bumping delay {args.delay_seconds}s -> 1.0s minimum (be polite)")
        args.delay_seconds = 1.0
    run(args)


# --- offline self-check -------------------------------------------------------
_SAMPLE_RESPONSE = {
    "result": "OK",
    "data": {
        "id": "m70787015721",
        "name": "PRAGMATA プラグマタ PS5",
        "description": "新品未開封です。\n即購入OK。",
        "price": 4990,
        "status": "on_sale",
        "item_condition": {"id": 3, "name": "目立った傷や汚れなし"},
        "shipping_payer": {"id": 2, "name": "送料込み(出品者負担)"},
        "seller": {"id": 737378784, "name": "りょく"},
    },
}


def _self_test() -> None:
    data = data_from_response(_SAMPLE_RESPONSE)
    assert data is not None
    f = parse_item(data)
    assert f["title_raw"] == "PRAGMATA プラグマタ PS5", f["title_raw"]
    assert f["description_raw"].startswith("新品未開封"), f["description_raw"]
    assert f["price_raw"] == "4990", f["price_raw"]
    assert f["status_raw"] == "on_sale", f["status_raw"]
    assert f["condition_raw"] == "目立った傷や汚れなし", f["condition_raw"]
    assert f["shipping_raw"] == "送料込み(出品者負担)", f["shipping_raw"]
    assert f["seller_name_raw"] == "りょく", f["seller_name_raw"]

    # Error envelope -> no usable item.
    assert data_from_response({"result": "error", "data": None}) is None
    assert data_from_response({"result": "OK", "data": "nope"}) is None

    # Defensive: sparse/missing fields degrade to None, no crash.
    sparse = parse_item({"name": "x"})
    assert sparse["title_raw"] == "x" and sparse["price_raw"] is None
    assert all(sparse[k] is None for k in FIELDS if k != "title_raw"), sparse

    # DPoP token is a well-formed 3-part JWS for the GET URL.
    assert make_dpop("GET", f"{API_ITEM_URL}?id=m1").count(".") == 2
    print("self-test OK: item parse, OK/error envelope, sparse degrade, DPoP mints")


if __name__ == "__main__":
    main()
