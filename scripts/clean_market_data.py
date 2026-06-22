"""Two cleaning phases over the Mercari raw tables.

Phase 1 (default) -- candidate layer:
  Deduplicate + classify `search_results_raw` (joined to `search_runs` for the
  game_id) into `search_results_candidates`: one row per (game_id, listing_id)
  with keyword-based platform / edition / exclusion flags and a
  needs_detail_scrape flag. Prices/status carried through verbatim.

Phase 2 (--build-clean) -- analysis layer:
  Merge `search_results_candidates` + `listing_details_raw` + `games` into
  `market_listings_clean`, the analysis-ready table used by notebooks. One row
  per usable (game_id, listing_id): is_excluded=0 candidates that have a detail
  row with a price. Adds numeric price_jpy, normalized status_final, and
  days_since_release; raw JP labels (condition/shipping) are copied as-is.

Usage:
    # phase 1: build candidates
    python scripts/clean_market_data.py [--game-id GAME_ID] [--replace-existing]
    # phase 2: build the clean analysis table
    python scripts/clean_market_data.py --build-clean [--game-id GAME_ID] [--replace-existing]
    python scripts/clean_market_data.py --self-test   # offline checks

Default (no --replace-existing): upsert by (game_id, listing_id), preserving
created_at. With --replace-existing: delete rows for the processed game_id(s)
first, then insert fresh. No --game-id processes all games.
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "configs" / "games.yaml"
DB_PATH = ROOT / "data" / "mercari.sqlite"
PREVIEW_CSV = ROOT / "data" / "search_results_candidates_preview.csv"

# Platform detection (title is lowercased before matching).
PS5_MARKERS = ["ps5", "プレステ5", "プレイステーション5", "playstation 5", "playstation5"]
SWITCH2_MARKERS = ["switch 2", "switch2", "スイッチ2", "スウィッチ2", "ニンテンドースイッチ2", "ns2"]

# Non-standard / unwanted edition keywords, grouped so we can emit a readable
# exclude_reason. Derived from configs/games.yaml exclude_keywords plus common
# English equivalents. NOTE: pre-order/early-purchase bonus terms (予約特典 /
# 早期購入特典 / 特典) are deliberately NOT here -- in the real data they appear
# on plenty of ordinary standard copies, so excluding them loses good listings.
CATEGORY_KEYWORDS = {
    "limited_edition": [
        "限定", "コレクター", "コレクターズ", "デラックス", "特装", "豪華",
        "スペシャルエディション", "プレミアム", "アルティメット",
        "deluxe", "collector", "limited", "premium", "ultimate", "special edition",
    ],
    # Only unambiguously digital terms. We deliberately drop bare "コード" /
    # "プロダクトコード": physical copies often advertise an *unused bonus*
    # download code (e.g. "早期購入特典DLコード付き", "コード未使用"), and
    # excluding those would drop real standard discs. The detail scrape can
    # disambiguate the genuinely ambiguous ones.
    "digital": ["ダウンロード版", "dl版", "ダウンロードコード", "digital", "download"],
    "guidebook": [
        "攻略本", "ガイドブック", "設定資料", "アートブック", "ファンブック",
        "guide book", "artbook",
    ],
    "bundle": ["同梱", "本体同梱", "セット", "まとめ売り", "まとめ", "bundle"],
}
# Order in which a non-standard reason is reported when several match.
NONSTANDARD_PRIORITY = ["limited_edition", "digital", "guidebook", "bundle"]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_games(config_path: Path) -> dict:
    with config_path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


# --- classification -----------------------------------------------------------
def _match(text: str, keywords: list[str]) -> bool:
    return any(k in text for k in keywords)


def guess_platform(title_lower: str) -> str:
    has_ps5 = _match(title_lower, PS5_MARKERS)
    has_sw2 = _match(title_lower, SWITCH2_MARKERS)
    if has_ps5 and not has_sw2:
        return "PS5"
    if has_sw2 and not has_ps5:
        return "Switch 2"
    if has_ps5 and has_sw2:
        return "ambiguous"
    return "unknown"


def expected_platform(platform: str | None) -> str | None:
    p = (platform or "").lower()
    if "switch" in p and "2" in p:
        return "Switch 2"
    if "ps5" in p or "playstation 5" in p:
        return "PS5"
    return None


def relevance_tokens(game_cfg: dict) -> set[str]:
    """Lowercased tokens that indicate the title is about this game: the
    canonical title plus the first word of each configured search keyword."""
    toks = {(game_cfg.get("canonical_title") or "").lower()}
    for kw in game_cfg.get("search_keywords", []):
        if kw.split():
            toks.add(kw.split()[0].lower())
    return {t for t in toks if t}


def classify(title: str | None, game_cfg: dict) -> dict:
    """Best-effort keyword classification. Returns the candidate flag columns."""
    t = (title or "").lower()
    relevant = _match(t, list(game_cfg["relevance_tokens"]))
    platform_guess = guess_platform(t)
    expected = game_cfg["expected_platform"]

    # Per-game exclude_keywords from configs/games.yaml (case-insensitive
    # substring match). This is the config-driven lever for sibling/related-title
    # contamination -- e.g. FF7 REBIRTH / REUNION / CRISIS CORE for the
    # INTERGRADE targets -- which the hardcoded CATEGORY_KEYWORDS don't cover.
    game_excludes = [k.lower() for k in (game_cfg.get("exclude_keywords") or [])]
    matches_game_exclude = relevant and _match(t, game_excludes)

    is_bundle = 1 if _match(t, CATEGORY_KEYWORDS["bundle"]) else 0
    nonstandard = next((c for c in NONSTANDARD_PRIORITY if _match(t, CATEGORY_KEYWORDS[c])), None)
    is_standard_edition = 0 if nonstandard else 1

    # exclude_reason priority:
    #   unmatched_title > game_specific_exclude > wrong_platform > non-standard edition.
    if not relevant:
        exclude_reason = "unmatched_title"
    elif matches_game_exclude:
        exclude_reason = "game_specific_exclude"
    elif expected and platform_guess in ("PS5", "Switch 2") and platform_guess != expected:
        exclude_reason = "wrong_platform"
    elif nonstandard:
        exclude_reason = nonstandard
    else:
        exclude_reason = None

    is_excluded = 1 if exclude_reason else 0
    needs_detail = 1 if (not is_excluded and is_standard_edition and relevant) else 0
    return {
        "platform_guess": platform_guess,
        "is_standard_edition": is_standard_edition,
        "is_bundle": is_bundle,
        "is_excluded": is_excluded,
        "exclude_reason": exclude_reason,
        "needs_detail_scrape": needs_detail,
    }


# --- dedup --------------------------------------------------------------------
def dedupe(raw_rows: list[sqlite3.Row]) -> dict[tuple[str, str], dict]:
    """Collapse raw rows into one record per (game_id, listing_id).

    Representative row = lowest rank, tie-broken by earliest scraped_at.
    """
    groups: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for r in raw_rows:
        groups[(r["game_id"], r["listing_id"])].append(r)

    out: dict[tuple[str, str], dict] = {}
    for key, rows in groups.items():
        ranks = [r["rank"] for r in rows if r["rank"] is not None]
        scraped = [r["scraped_at"] for r in rows if r["scraped_at"] is not None]
        rep = min(rows, key=lambda r: (r["rank"] if r["rank"] is not None else 1 << 30,
                                       r["scraped_at"] or ""))
        out[key] = {
            "game_id": key[0],
            "listing_id": key[1],
            "title_raw": rep["title_raw"],
            "listing_url": rep["listing_url"],
            "price_raw": rep["price_raw"],
            "sold_flag_raw": rep["sold_flag_raw"],
            "first_seen_at": min(scraped) if scraped else None,
            "last_seen_at": max(scraped) if scraped else None,
            "seen_count": len(rows),
            "best_rank": min(ranks) if ranks else None,
        }
    return out


# --- db io --------------------------------------------------------------------
def fetch_raw(conn: sqlite3.Connection, game_id: str | None) -> list[sqlite3.Row]:
    sql = (
        "SELECT sr.game_id AS game_id, r.listing_id, r.rank, r.title_raw, "
        "r.listing_url, r.price_raw, r.sold_flag_raw, r.scraped_at "
        "FROM search_results_raw r "
        "JOIN search_runs sr ON r.search_run_id = sr.search_run_id"
    )
    params: tuple = ()
    if game_id:
        sql += " WHERE sr.game_id = ?"
        params = (game_id,)
    return conn.execute(sql, params).fetchall()


def write_candidate(conn: sqlite3.Connection, rec: dict, existing_id: int | None) -> str:
    ts = now()
    cols = (
        rec["listing_id"], rec["game_id"], rec["title_raw"], rec["listing_url"],
        rec["price_raw"], rec["sold_flag_raw"], rec["first_seen_at"], rec["last_seen_at"],
        rec["seen_count"], rec["best_rank"], rec["platform_guess"],
        rec["is_standard_edition"], rec["is_bundle"], rec["is_excluded"],
        rec["exclude_reason"], rec["needs_detail_scrape"],
    )
    if existing_id is None:
        conn.execute(
            """INSERT INTO search_results_candidates
               (listing_id, game_id, title_raw, listing_url, price_raw, sold_flag_raw,
                first_seen_at, last_seen_at, seen_count, best_rank, platform_guess,
                is_standard_edition, is_bundle, is_excluded, exclude_reason,
                needs_detail_scrape, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            cols + (ts, ts),
        )
        return "inserted"
    conn.execute(
        """UPDATE search_results_candidates SET
           title_raw=?, listing_url=?, price_raw=?, sold_flag_raw=?, first_seen_at=?,
           last_seen_at=?, seen_count=?, best_rank=?, platform_guess=?,
           is_standard_edition=?, is_bundle=?, is_excluded=?, exclude_reason=?,
           needs_detail_scrape=?, updated_at=?
           WHERE id=?""",
        (rec["title_raw"], rec["listing_url"], rec["price_raw"], rec["sold_flag_raw"],
         rec["first_seen_at"], rec["last_seen_at"], rec["seen_count"], rec["best_rank"],
         rec["platform_guess"], rec["is_standard_edition"], rec["is_bundle"],
         rec["is_excluded"], rec["exclude_reason"], rec["needs_detail_scrape"], ts,
         existing_id),
    )
    return "updated"


def export_preview(conn: sqlite3.Connection, game_ids: list[str]) -> int:
    placeholders = ",".join("?" for _ in game_ids)
    rows = conn.execute(
        f"""SELECT game_id, listing_id, platform_guess, is_standard_edition, is_bundle,
                   is_excluded, exclude_reason, needs_detail_scrape, seen_count,
                   best_rank, price_raw, sold_flag_raw, listing_url, title_raw
            FROM search_results_candidates
            WHERE game_id IN ({placeholders})
            ORDER BY needs_detail_scrape DESC, best_rank ASC""",
        game_ids,
    ).fetchall()
    PREVIEW_CSV.parent.mkdir(parents=True, exist_ok=True)
    with PREVIEW_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if rows:
            w.writerow(rows[0].keys())
            w.writerows(tuple(r) for r in rows)
    return len(rows)


# --- clean layer (market_listings_clean) --------------------------------------
# Ordered to match the INSERT/UPDATE column lists below.
CLEAN_COLS = (
    "listing_id", "game_id", "canonical_title", "platform_final", "title_raw",
    "description_raw", "price_jpy", "status_final", "condition_raw", "shipping_raw",
    "seller_name_raw", "is_standard_edition", "is_bundle", "is_excluded",
    "exclude_reason", "release_date", "scraped_at", "days_since_release",
)


def to_price_jpy(price_raw) -> float | None:
    """Numeric JPY from a raw price string. Strips currency/commas; None if no
    digits. First-pass only -- no currency assumptions beyond 'digits = yen'."""
    if price_raw is None:
        return None
    digits = "".join(ch for ch in str(price_raw) if ch.isdigit())
    return float(digits) if digits else None


def normalize_status(status_raw) -> str:
    """Collapse raw status (item API 'on_sale'/'sold_out'/'trading' or the
    search 'ITEM_STATUS_*' form) to a small fixed set; 'unknown' otherwise."""
    s = (status_raw or "").lower()
    if "sold" in s:
        return "sold_out"
    if "trading" in s:
        return "trading"
    if "on_sale" in s or "onsale" in s or "on sale" in s:
        return "on_sale"
    return "unknown"


def compute_days_since_release(scraped_at, release_date) -> float | None:
    """(scraped_at - release_date) in days, ISO-8601. None if either is
    missing/unparseable. scraped_at may be tz-aware; release_date is a date."""
    if not scraped_at or not release_date:
        return None
    try:
        sdt = datetime.fromisoformat(scraped_at)
        if sdt.tzinfo is not None:
            sdt = sdt.astimezone(timezone.utc).replace(tzinfo=None)
        rdt = datetime.fromisoformat(release_date)  # date-only -> midnight
    except ValueError:
        return None
    return (sdt - rdt).total_seconds() / 86400.0


def refine_platform(platform_guess, detail_title, description) -> str | None:
    """Keep a confident candidate guess (PS5/Switch 2); only try to resolve an
    unknown/ambiguous guess using the detail title, then description."""
    if platform_guess in ("PS5", "Switch 2"):
        return platform_guess
    for text in (detail_title, description):
        g = guess_platform((text or "").lower())
        if g in ("PS5", "Switch 2"):
            return g
    return platform_guess


def build_clean_record(row) -> dict:
    """Project one merged source row into the clean-table column set."""
    detail_title = row["detail_title"]
    return {
        "listing_id": row["listing_id"],
        "game_id": row["game_id"],
        "canonical_title": row["canonical_title"],
        "platform_final": refine_platform(
            row["platform_guess"], detail_title, row["description_raw"]
        ),
        "title_raw": detail_title if detail_title else row["cand_title"],
        "description_raw": row["description_raw"],
        "price_jpy": to_price_jpy(row["price_raw"]),
        "status_final": normalize_status(row["status_raw"]),
        "condition_raw": row["condition_raw"],
        "shipping_raw": row["shipping_raw"],
        "seller_name_raw": row["seller_name_raw"],
        "is_standard_edition": row["is_standard_edition"],
        "is_bundle": row["is_bundle"],
        "is_excluded": row["is_excluded"],
        "exclude_reason": row["exclude_reason"],
        "release_date": row["release_date"],
        "scraped_at": row["scraped_at"],
        "days_since_release": compute_days_since_release(
            row["scraped_at"], row["release_date"]
        ),
    }


def fetch_clean_source(conn: sqlite3.Connection, game_id: str | None) -> list[sqlite3.Row]:
    """Usable rows: non-excluded candidates LEFT JOINed to their detail row and
    game, kept only when a detail price exists."""
    sql = """
        SELECT c.listing_id, c.game_id,
               c.title_raw AS cand_title, c.platform_guess,
               c.is_standard_edition, c.is_bundle, c.is_excluded, c.exclude_reason,
               g.canonical_title, g.release_date,
               d.title_raw AS detail_title, d.description_raw, d.price_raw,
               d.status_raw, d.condition_raw, d.shipping_raw, d.seller_name_raw,
               d.scraped_at
        FROM search_results_candidates c
        JOIN games g ON c.game_id = g.game_id
        LEFT JOIN listing_details_raw d ON d.listing_id = c.listing_id
        WHERE c.is_excluded = 0 AND d.price_raw IS NOT NULL
    """
    params: tuple = ()
    if game_id:
        sql += " AND c.game_id = ?"
        params = (game_id,)
    return conn.execute(sql, params).fetchall()


def write_clean(conn: sqlite3.Connection, rec: dict, existing_id: int | None) -> str:
    ts = now()
    vals = [rec[c] for c in CLEAN_COLS]
    if existing_id is None:
        conn.execute(
            f"""INSERT INTO market_listings_clean
                ({', '.join(CLEAN_COLS)}, created_at, updated_at)
                VALUES ({', '.join('?' * (len(CLEAN_COLS) + 2))})""",
            [*vals, ts, ts],
        )
        return "inserted"
    conn.execute(
        f"""UPDATE market_listings_clean
            SET {', '.join(f'{c} = ?' for c in CLEAN_COLS)}, updated_at = ?
            WHERE id = ?""",
        [*vals, ts, existing_id],
    )
    return "updated"


def run_build_clean(
    conn: sqlite3.Connection, games: dict, game_id: str | None, replace: bool
) -> None:
    cand_sql = "SELECT COUNT(*) FROM search_results_candidates WHERE is_excluded = 0"
    cparams: tuple = ()
    if game_id:
        cand_sql += " AND game_id = ?"
        cparams = (game_id,)
    considered = conn.execute(cand_sql, cparams).fetchone()[0]

    rows = fetch_clean_source(conn, game_id)
    game_ids = sorted({r["game_id"] for r in rows})
    if not game_ids and game_id:
        game_ids = [game_id]  # so --replace-existing still clears the target

    if replace and game_ids:
        ph = ",".join("?" for _ in game_ids)
        n = conn.execute(
            f"DELETE FROM market_listings_clean WHERE game_id IN ({ph})", game_ids
        ).rowcount
        print(f"--replace-existing: deleted {n} existing clean row(s)")

    existing: dict[tuple[str, str], int] = {}
    if not replace and game_ids:
        ph = ",".join("?" for _ in game_ids)
        for r in conn.execute(
            f"SELECT id, game_id, listing_id FROM market_listings_clean "
            f"WHERE game_id IN ({ph})", game_ids
        ):
            existing[(r["game_id"], r["listing_id"])] = r["id"]

    stats = {"inserted": 0, "updated": 0}
    status_counts: dict[str, int] = defaultdict(int)
    prices: list[float] = []
    days: list[float] = []
    null_price = null_days = 0
    samples: list[dict] = []

    for r in rows:
        rec = build_clean_record(r)
        op = write_clean(conn, rec, existing.get((rec["game_id"], rec["listing_id"])))
        stats[op] += 1
        status_counts[rec["status_final"]] += 1
        if rec["price_jpy"] is None:
            null_price += 1
        else:
            prices.append(rec["price_jpy"])
        if rec["days_since_release"] is None:
            null_days += 1
        else:
            days.append(rec["days_since_release"])
        if len(samples) < 5:
            samples.append(rec)
    conn.commit()

    def mmm(vals: list[float], fmt: str) -> str:
        if not vals:
            return "n/a"
        return (f"min={min(vals):{fmt}} median={statistics.median(vals):{fmt}} "
                f"max={max(vals):{fmt}}")

    print("\n=== build clean summary ===")
    print(f"game_id(s) processed: {', '.join(game_ids) if game_ids else '(none)'}")
    print(f"candidates considered (is_excluded=0): {considered}")
    print(f"clean rows: inserted {stats['inserted']} | updated {stats['updated']} "
          f"| usable source rows {len(rows)}")
    print("counts by status_final:")
    for k, v in sorted(status_counts.items(), key=lambda kv: -kv[1]):
        print(f"  {k}: {v}")
    print(f"price_jpy: {mmm(prices, '.0f')}  (NULL: {null_price})")
    print(f"days_since_release: {mmm(days, '.1f')}  (NULL: {null_days})")
    print("\nexample rows (listing_id | price_jpy | status_final | condition_raw | days_since_release):")
    for s in samples:
        dd = f"{s['days_since_release']:.1f}" if s["days_since_release"] is not None else "NULL"
        print(f"  {s['listing_id']} | {s['price_jpy']} | {s['status_final']} | "
              f"{s['condition_raw']} | {dd}")


# --- driver -------------------------------------------------------------------
def run(conn: sqlite3.Connection, games: dict, game_id: str | None, replace: bool) -> None:
    raw = fetch_raw(conn, game_id)
    print(f"raw rows processed: {len(raw)}")

    deduped = dedupe(raw)
    print(f"unique listings (game_id, listing_id): {len(deduped)}")
    print(f"unique listing_ids: {len({k[1] for k in deduped})}")

    # Precompute per-game classification helpers from config.
    cfg_cache: dict[str, dict] = {}
    for gid, entry in games.items():
        cfg_cache[gid] = {
            **entry,
            "relevance_tokens": relevance_tokens(entry),
            "expected_platform": expected_platform(entry.get("platform")),
        }

    game_ids = sorted({k[0] for k in deduped})
    if replace and game_ids:
        ph = ",".join("?" for _ in game_ids)
        n = conn.execute(
            f"DELETE FROM search_results_candidates WHERE game_id IN ({ph})", game_ids
        ).rowcount
        print(f"--replace-existing: deleted {n} existing candidate row(s)")

    # Existing keys (for upsert when not replacing).
    existing: dict[tuple[str, str], int] = {}
    if not replace and game_ids:
        ph = ",".join("?" for _ in game_ids)
        for row in conn.execute(
            f"SELECT id, game_id, listing_id FROM search_results_candidates "
            f"WHERE game_id IN ({ph})", game_ids
        ):
            existing[(row["game_id"], row["listing_id"])] = row["id"]

    stats = {"inserted": 0, "updated": 0, "excluded": 0}
    sold = defaultdict(int)
    platform = defaultdict(int)
    reasons = defaultdict(int)
    sample_titles: list[str] = []

    for key, rec in deduped.items():
        gid = key[0]
        cfg = cfg_cache.get(gid)
        if cfg is None:  # listing from a game not in config -> mark unmatched
            cfg = {"relevance_tokens": set(), "expected_platform": None}
        rec.update(classify(rec["title_raw"], cfg))

        result = write_candidate(conn, rec, existing.get(key))
        stats[result] += 1
        if rec["is_excluded"]:
            stats["excluded"] += 1
            reasons[rec["exclude_reason"]] += 1
        sold[rec["sold_flag_raw"]] += 1
        platform[rec["platform_guess"]] += 1
        if rec["needs_detail_scrape"] and len(sample_titles) < 5:
            sample_titles.append(rec["title_raw"] or "")

    conn.commit()

    n_csv = export_preview(conn, game_ids) if game_ids else 0

    # --- summary ---
    print("\n=== summary ===")
    print(f"candidate rows inserted: {stats['inserted']} | updated: {stats['updated']}")
    print(f"excluded: {stats['excluded']} | needs_detail_scrape=1: "
          f"{sum(1 for r in deduped.values() if r['needs_detail_scrape'])}")
    print("\ncounts by sold_flag_raw:")
    for k, v in sorted(sold.items(), key=lambda kv: -kv[1]):
        print(f"  {k}: {v}")
    print("\ncounts by platform_guess:")
    for k, v in sorted(platform.items(), key=lambda kv: -kv[1]):
        print(f"  {k}: {v}")
    print("\ntop exclude reasons:")
    for k, v in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print(f"  {k}: {v}")
    print("\n5 sample candidate titles (needs_detail_scrape=1):")
    for t in sample_titles:
        print(f"  - {t}")
    print(f"\npreview CSV: {PREVIEW_CSV.relative_to(ROOT)} ({n_csv} rows)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-id", help="Limit to a single game_id")
    parser.add_argument("--replace-existing", action="store_true",
                        help="Delete rows for processed game_id(s) before inserting")
    parser.add_argument("--build-clean", action="store_true",
                        help="Build market_listings_clean (phase 2) instead of candidates")
    parser.add_argument("--self-test", action="store_true", help="Offline checks")
    args = parser.parse_args()

    if args.self_test:
        _self_test()
        return

    if not DB_PATH.exists():
        raise SystemExit(f"Database not found: {DB_PATH}. Run scripts/init_db.py first.")

    games = load_games(CONFIG_PATH)
    if args.game_id and args.game_id not in games:
        raise SystemExit(f"Unknown game_id: {args.game_id}. Known: {', '.join(games)}")

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        if args.build_clean:
            run_build_clean(conn, games, args.game_id, args.replace_existing)
        else:
            run(conn, games, args.game_id, args.replace_existing)


# --- offline self-check -------------------------------------------------------
def _self_test() -> None:
    cfg = {
        "canonical_title": "PRAGMATA",
        "platform": "PS5",
        "search_keywords": ["プラグマタ", "PRAGMATA"],
    }
    cfg["relevance_tokens"] = relevance_tokens(cfg)
    cfg["expected_platform"] = expected_platform(cfg["platform"])

    std = classify("PRAGMATA プラグマタ PS5 通常版", cfg)
    assert std["platform_guess"] == "PS5" and std["is_excluded"] == 0
    assert std["is_standard_edition"] == 1 and std["needs_detail_scrape"] == 1

    lim = classify("プラグマタ PS5 限定版 コレクターズ", cfg)
    assert lim["exclude_reason"] == "limited_edition" and lim["needs_detail_scrape"] == 0

    wp = classify("プラグマタ Switch2 通常版", cfg)
    assert wp["platform_guess"] == "Switch 2" and wp["exclude_reason"] == "wrong_platform"

    dig = classify("プラグマタ PS5 ダウンロード版 コード", cfg)
    assert dig["exclude_reason"] == "digital" and dig["is_standard_edition"] == 0

    bun = classify("プラグマタ PS5 と 他ゲーム まとめ セット", cfg)
    assert bun["is_bundle"] == 1 and bun["exclude_reason"] == "bundle"

    off = classify("全く関係ない商品", cfg)
    assert off["exclude_reason"] == "unmatched_title" and off["needs_detail_scrape"] == 0

    # pre-order bonus must NOT exclude a standard copy
    bonus = classify("【早期購入特典付き】PRAGMATA プラグマタ PS5", cfg)
    assert bonus["is_excluded"] == 0 and bonus["needs_detail_scrape"] == 1

    # --- per-game exclude_keywords (config-driven, e.g. FF7 sibling titles) ---
    ff = {
        "canonical_title": "FINAL FANTASY VII REMAKE INTERGRADE",
        "platform": "PS5",
        "search_keywords": ["FF7 リメイク インターグレード",
                            "FINAL FANTASY VII REMAKE INTERGRADE", "インターグレード PS5"],
        "exclude_keywords": ["リバース", "REBIRTH", "リユニオン", "REUNION",
                             "クライシスコア", "CRISIS CORE", "攻略本", "コードのみ"],
    }
    ff["relevance_tokens"] = relevance_tokens(ff)
    ff["expected_platform"] = expected_platform(ff["platform"])

    keep = classify("FINAL FANTASY VII REMAKE INTERGRADE PS5", ff)
    assert keep["is_excluded"] == 0 and keep["needs_detail_scrape"] == 1
    reb = classify("FF7 リバース PS5", ff)  # sibling title (REBIRTH)
    assert reb["exclude_reason"] == "game_specific_exclude" and reb["needs_detail_scrape"] == 0
    cc = classify("CRISIS CORE FINAL FANTASY VII REUNION PS5", ff)
    assert cc["exclude_reason"] == "game_specific_exclude" and cc["is_excluded"] == 1
    gb = classify("FF7 インターグレード 攻略本", ff)
    assert gb["exclude_reason"] == "game_specific_exclude"
    co = classify("FF7 インターグレード コードのみ", ff)
    assert co["exclude_reason"] == "game_specific_exclude"

    # PRAGMATA carve-out preserved: bare コード / 予約特典 are intentionally NOT in
    # the YAML, so a standard copy advertising an unused bonus code is still kept.
    prag = {
        "canonical_title": "PRAGMATA", "platform": "PS5",
        "search_keywords": ["プラグマタ", "PRAGMATA"],
        "exclude_keywords": ["限定版", "限定", "コレクターズ", "デラックス",
                             "同梱", "セット", "まとめ", "ダウンロード版", "DL版"],
    }
    prag["relevance_tokens"] = relevance_tokens(prag)
    prag["expected_platform"] = expected_platform(prag["platform"])
    code_bonus = classify("PRAGMATA プラグマタ PS5 特典コード未使用", prag)
    assert code_bonus["is_excluded"] == 0 and code_bonus["needs_detail_scrape"] == 1
    prag_lim = classify("プラグマタ PS5 限定版", prag)  # still excluded, now config-driven
    assert prag_lim["exclude_reason"] == "game_specific_exclude" and prag_lim["is_excluded"] == 1

    # --- clean-phase helpers ---
    assert to_price_jpy("4990") == 4990.0
    assert to_price_jpy("￥4,990円") == 4990.0
    assert to_price_jpy(None) is None and to_price_jpy("--") is None
    assert normalize_status("on_sale") == "on_sale"
    assert normalize_status("ITEM_STATUS_SOLD_OUT") == "sold_out"
    assert normalize_status("trading") == "trading"
    assert normalize_status("weird") == "unknown"
    assert abs(compute_days_since_release("2026-06-21T00:00:00+00:00", "2026-04-24") - 58.0) < 1e-9
    assert compute_days_since_release(None, "2026-04-24") is None
    assert compute_days_since_release("not-a-date", "2026-04-24") is None
    assert refine_platform("unknown", "PS5 プラグマタ", None) == "PS5"
    assert refine_platform("PS5", "switch2 misc", None) == "PS5"  # confident guess kept

    rec = build_clean_record({
        "listing_id": "m1", "game_id": "pragmata_ps5", "cand_title": "プラグマタ",
        "platform_guess": "unknown", "is_standard_edition": 1, "is_bundle": 0,
        "is_excluded": 0, "exclude_reason": None, "canonical_title": "PRAGMATA",
        "release_date": "2026-04-24", "detail_title": "PS5 PRAGMATA プラグマタ",
        "description_raw": "説明", "price_raw": "5000", "status_raw": "sold_out",
        "condition_raw": "目立った傷や汚れなし", "shipping_raw": "送料込み(出品者負担)",
        "seller_name_raw": "taro", "scraped_at": "2026-06-21T00:00:00+00:00",
    })
    assert rec["price_jpy"] == 5000.0 and rec["status_final"] == "sold_out"
    assert rec["platform_final"] == "PS5" and rec["title_raw"] == "PS5 PRAGMATA プラグマタ"
    assert abs(rec["days_since_release"] - 58.0) < 1e-9
    print("self-test OK: classify + clean (price/status/days/platform/build)")


if __name__ == "__main__":
    main()
