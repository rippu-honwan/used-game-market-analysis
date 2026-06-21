"""Collect Mercari Japan search results via the web app's JSON API.

Phase: search-results only. No listing-detail scraping, no browser automation.

Mercari's web search results are loaded client-side from the JSON endpoint
    POST https://api.mercari.jp/v2/entities:search
which requires two things the static HTML page does NOT expose:
  - header  X-Platform: web
  - header  DPoP: <ES256 JWT>  (proof-of-possession, signed with a key we
            generate ourselves; Mercari's web client does the same -- no login
            or registration is involved)

For each search keyword in configs/games.yaml this script:
  1. inserts one row into search_runs,
  2. POSTs the search query to the JSON API,
  3. saves the raw JSON response to data/raw_pages/ for reproducibility,
  4. maps each item into search_results_raw (best-effort, dedup per run).

Pagination is cursor-based: the response's meta.nextPageToken is fed back as
pageToken on the next request. --max-pages caps how many pages we pull.

Usage:
    python scripts/scrape_search.py [--game-id GAME_ID]
                                    [--max-pages N] [--delay-seconds S]
    python scripts/scrape_search.py --self-test   # offline parser check
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml
from ecdsa import NIST256p, SigningKey
from ecdsa.util import sigencode_string

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "configs" / "games.yaml"
DB_PATH = ROOT / "data" / "mercari.sqlite"
RAW_PAGES_DIR = ROOT / "data" / "raw_pages"

API_URL = "https://api.mercari.jp/v2/entities:search"
ORIGIN = "https://jp.mercari.com"
ITEM_BASE = "https://jp.mercari.com/item/"
SHOP_BASE = "https://jp.mercari.com/shops/product/"

# A normal browser User-Agent. We stay polite via low request volume and a
# delay between requests, not via spoofing tricks.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 25  # seconds
PAGE_SIZE = 120  # items per request; matches the web client, keeps request count low


def now() -> str:
    """Current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


# --- DPoP signing -------------------------------------------------------------
# One ES256 key per process, same as a browser session. make_dpop() mints a
# fresh token (new iat/jti) per request. Pure-Python via `ecdsa`; the JWT
# signature is raw R||S (64 bytes), which is what JWS ES256 requires.
_SIGNING_KEY = SigningKey.generate(curve=NIST256p)


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64u_json(obj: dict) -> str:
    return _b64u(json.dumps(obj, separators=(",", ":")).encode())


def make_dpop(method: str, url: str) -> str:
    vk = _SIGNING_KEY.get_verifying_key()
    point = vk.pubkey.point
    jwk = {
        "crv": "P-256",
        "kty": "EC",
        "x": _b64u(point.x().to_bytes(32, "big")),
        "y": _b64u(point.y().to_bytes(32, "big")),
    }
    header = {"typ": "dpop+jwt", "alg": "ES256", "jwk": jwk}
    payload = {
        "iat": int(time.time()),
        "jti": str(uuid.uuid4()),
        "htu": url,
        "htm": method,
        "uuid": str(uuid.uuid4()),
    }
    signing_input = f"{_b64u_json(header)}.{_b64u_json(payload)}".encode()
    sig = _SIGNING_KEY.sign_deterministic(
        signing_input, hashfunc=hashlib.sha256, sigencode=sigencode_string
    )
    return f"{signing_input.decode()}.{_b64u(sig)}"


# --- config / API -------------------------------------------------------------
def load_games(config_path: Path) -> dict:
    with config_path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_search_body(keyword: str, page_token: str, session_id: str) -> dict:
    """Request body for /v2/entities:search. Mirrors the web client; most lists
    stay empty (no facet filters in this phase)."""
    return {
        "userId": "",
        "pageSize": PAGE_SIZE,
        "pageToken": page_token,
        "searchSessionId": session_id,
        "indexRouting": "INDEX_ROUTING_UNSPECIFIED",
        "thumbnailTypes": [],
        "searchCondition": {
            "keyword": keyword,
            "excludeKeyword": "",
            "sort": "SORT_SCORE",
            "order": "ORDER_DESC",
            "status": [],
            "sizeId": [],
            "categoryId": [],
            "brandId": [],
            "sellerId": [],
            "priceMin": 0,
            "priceMax": 0,
            "itemConditionId": [],
            "shippingPayerId": [],
            "shippingFromArea": [],
            "shippingMethod": [],
            "colorId": [],
            "hasCoupon": False,
            "attributes": [],
            "itemTypes": [],
            "skuIds": [],
            "shopIds": [],
            "excludeShippingMethodIds": [],
        },
        "defaultDatabaseId": "",
        "serviceFrom": "suruga",
        "withItemBrand": False,
        "withItemSize": False,
        "withItemPromotions": False,
        "withItemSizes": False,
        "withShopname": False,
        "useDynamicAttribute": False,
        "withSuggestedItems": False,
        "withOfferPricePromotions": False,
        "withProductSuggest": False,
        "withParentProducts": False,
        "withProductArticles": False,
        "withSearchConditionId": False,
        "withAuctionItems": False,
    }


def post_search(session: requests.Session, body: dict) -> str:
    headers = {
        "Content-Type": "application/json",
        "Accept": "*/*",
        "X-Platform": "web",
        "Origin": ORIGIN,
        "Referer": ORIGIN + "/",
        "DPoP": make_dpop("POST", API_URL),
    }
    resp = session.post(API_URL, headers=headers, data=json.dumps(body), timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.text


def parse_items(items: list, seen: set, start_rank: int) -> tuple[list[dict], int]:
    """Map API items -> search_results_raw rows. Dedupe by listing_id (first
    occurrence wins); rank is the global position across pages in the run.
    Missing fields are stored as None."""
    rows: list[dict] = []
    rank = start_rank
    for it in items:
        listing_id = it.get("id")
        if not listing_id or listing_id in seen:
            continue
        seen.add(listing_id)
        rank += 1

        # ITEM_TYPE_MERCARI (C2C) -> /item/<id>; Mercari Shops products live
        # under /shops/product/<id>. Best-effort; most game listings are C2C.
        if it.get("itemType") == "ITEM_TYPE_BEYOND":
            listing_url = f"{SHOP_BASE}{listing_id}"
        else:
            listing_url = f"{ITEM_BASE}{listing_id}"

        thumbs = it.get("thumbnails") or []
        price = it.get("price")
        rows.append(
            {
                "listing_id": listing_id,
                "listing_url": listing_url,
                "rank": rank,
                "title_raw": it.get("name"),
                "price_raw": None if price is None else str(price),
                "sold_flag_raw": it.get("status"),  # raw ITEM_STATUS_* string
                "thumbnail_url": thumbs[0] if thumbs else None,
            }
        )
    return rows, rank


# --- storage ------------------------------------------------------------------
def save_raw_json(game_id: str, text: str, page: int) -> Path:
    RAW_PAGES_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = RAW_PAGES_DIR / f"{game_id}_search_{ts}_page{page}.json"
    path.write_text(text, encoding="utf-8")
    return path


def insert_search_run(conn: sqlite3.Connection, game_id: str, query: str) -> int:
    cur = conn.execute(
        "INSERT INTO search_runs (game_id, query, scraped_at, notes) VALUES (?, ?, ?, ?)",
        (game_id, query, now(), None),
    )
    return cur.lastrowid


def insert_results(
    conn: sqlite3.Connection, search_run_id: int, rows: list[dict], raw_page_path: str
) -> int:
    ts = now()
    for r in rows:
        conn.execute(
            """
            INSERT INTO search_results_raw
                (search_run_id, listing_id, listing_url, rank, title_raw,
                 price_raw, sold_flag_raw, thumbnail_url, raw_page_path, scraped_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                search_run_id,
                r["listing_id"],
                r["listing_url"],
                r["rank"],
                r["title_raw"],
                r["price_raw"],
                r["sold_flag_raw"],
                r["thumbnail_url"],
                raw_page_path,
                ts,
            ),
        )
    return len(rows)


# --- driver -------------------------------------------------------------------
def run_keyword(
    conn: sqlite3.Connection,
    session: requests.Session,
    game_id: str,
    keyword: str,
    max_pages: int,
    delay: float,
) -> int:
    print(f"  query: {keyword!r}")
    search_run_id = insert_search_run(conn, game_id, keyword)
    conn.commit()

    session_id = str(uuid.uuid4())  # one search session reused across its pages
    seen: set = set()
    rank = 0
    total = 0
    notes: list[str] = []
    page_token = ""

    for page in range(1, max_pages + 1):
        print(f"    page {page}: POST {API_URL}  keyword={keyword!r} token={page_token or '(first)'}")
        body = build_search_body(keyword, page_token, session_id)
        try:
            text = post_search(session, body)
        except Exception as e:  # HTTP/network error: record, stop paging, move on
            print(f"    ! request failed: {e}")
            notes.append(f"page{page}_request_error: {e}")
            break

        raw_path = save_raw_json(game_id, text, page)
        rel = str(raw_path.relative_to(ROOT))
        print(f"    saved raw JSON -> {rel}")

        try:
            data = json.loads(text)
        except Exception as e:  # unexpected body: raw already saved, then stop
            print(f"    ! JSON decode failed (raw kept): {e}")
            notes.append(f"page{page}_json_error")
            break

        items = data.get("items") or []
        rows, rank = parse_items(items, seen, rank)
        n = insert_results(conn, search_run_id, rows, rel)
        conn.commit()
        total += n
        print(f"    items in response: {len(items)} | inserted (deduped): {n}")
        if not items:
            print("    ! 0 items returned (raw kept)")
            notes.append(f"page{page}_empty")

        page_token = (data.get("meta") or {}).get("nextPageToken") or ""
        if not page_token:
            notes.append(f"no_more_pages_after_{page}")
            break
        if page < max_pages:
            time.sleep(delay)

    conn.execute(
        "UPDATE search_runs SET notes = ? WHERE search_run_id = ?",
        ("; ".join(notes) if notes else f"ok inserted={total}", search_run_id),
    )
    conn.commit()
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-id", help="Limit to a single game_id from games.yaml")
    parser.add_argument("--max-pages", type=int, default=1, help="Pages per keyword (default 1)")
    parser.add_argument("--delay-seconds", type=float, default=2.0, help="Delay between requests")
    parser.add_argument("--self-test", action="store_true", help="Run offline parser check and exit")
    args = parser.parse_args()

    if args.self_test:
        _self_test()
        return

    if not DB_PATH.exists():
        raise SystemExit(f"Database not found: {DB_PATH}. Run scripts/init_db.py first.")

    games = load_games(CONFIG_PATH)
    if args.game_id:
        if args.game_id not in games:
            raise SystemExit(f"Unknown game_id: {args.game_id}. Known: {', '.join(games)}")
        games = {args.game_id: games[args.game_id]}

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "ja,en;q=0.8"})

    grand_total = 0
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        for game_id, entry in games.items():
            print(f"game_id: {game_id}")
            keywords = entry.get("search_keywords", [])
            for i, keyword in enumerate(keywords):
                grand_total += run_keyword(
                    conn, session, game_id, keyword, args.max_pages, args.delay_seconds
                )
                if i < len(keywords) - 1:
                    time.sleep(args.delay_seconds)

    print(f"\nDone. Inserted {grand_total} listing row(s) total into search_results_raw.")


# --- offline self-check -------------------------------------------------------
_SELF_TEST_DATA = {
    "meta": {"nextPageToken": "v1:1", "numFound": "3"},
    "items": [
        {
            "id": "m11111111111",
            "name": "PRAGMATA プラグマタ PS5",
            "price": "4990",
            "status": "ITEM_STATUS_ON_SALE",
            "itemType": "ITEM_TYPE_MERCARI",
            "thumbnails": ["https://static.mercdn.net/thumb/item/webp/m11111111111_1.jpg"],
        },
        {
            "id": "m22222222222",
            "name": "プラグマタ Switch2 限定版",
            "price": "8800",
            "status": "ITEM_STATUS_SOLD_OUT",
            "itemType": "ITEM_TYPE_BEYOND",  # Mercari Shops -> /shops/product/
            "thumbnails": [],
        },
        {"id": "m11111111111", "name": "duplicate -> ignored", "price": "1"},
    ],
}


def _self_test() -> None:
    rows, rank = parse_items(_SELF_TEST_DATA["items"], set(), 0)
    assert len(rows) == 2 and rank == 2, (rows, rank)
    assert rows[0]["listing_id"] == "m11111111111"
    assert rows[0]["listing_url"] == "https://jp.mercari.com/item/m11111111111"
    assert rows[0]["title_raw"] == "PRAGMATA プラグマタ PS5"
    assert rows[0]["price_raw"] == "4990"
    assert rows[0]["sold_flag_raw"] == "ITEM_STATUS_ON_SALE"
    assert rows[0]["thumbnail_url"].endswith("m11111111111_1.jpg")
    assert rows[1]["listing_url"] == "https://jp.mercari.com/shops/product/m22222222222"
    assert rows[1]["sold_flag_raw"] == "ITEM_STATUS_SOLD_OUT"
    assert rows[1]["thumbnail_url"] is None
    # DPoP token must be a well-formed 3-part JWS
    assert make_dpop("POST", API_URL).count(".") == 2
    print(f"self-test OK: parsed {len(rows)} rows (dedupe, shop URL, fields), DPoP mints")


if __name__ == "__main__":
    main()
