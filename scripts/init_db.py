"""Initialize the SQLite database and seed game records.

Creates data/mercari.sqlite (if missing), creates all tables and indexes
with IF NOT EXISTS, then upserts the game targets from configs/games.yaml
into the `games` table.

Usage:
    python scripts/init_db.py
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "configs" / "games.yaml"
DB_PATH = ROOT / "data" / "mercari.sqlite"
RAW_PAGES_DIR = ROOT / "data" / "raw_pages"

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    game_id         TEXT PRIMARY KEY,
    canonical_title TEXT NOT NULL,
    platform        TEXT NOT NULL,
    release_date    TEXT,
    publisher       TEXT,
    edition_scope   TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS search_runs (
    search_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id       TEXT NOT NULL,
    query         TEXT NOT NULL,
    scraped_at    TEXT NOT NULL,
    notes         TEXT,
    FOREIGN KEY (game_id) REFERENCES games(game_id)
);

CREATE TABLE IF NOT EXISTS search_results_raw (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    search_run_id INTEGER NOT NULL,
    listing_id    TEXT NOT NULL,
    listing_url   TEXT,
    rank          INTEGER,
    title_raw     TEXT,
    price_raw     TEXT,
    sold_flag_raw TEXT,
    thumbnail_url TEXT,
    raw_page_path TEXT,
    scraped_at    TEXT NOT NULL,
    FOREIGN KEY (search_run_id) REFERENCES search_runs(search_run_id)
);

CREATE TABLE IF NOT EXISTS listing_details_raw (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id      TEXT NOT NULL,
    scraped_at      TEXT NOT NULL,
    title_raw       TEXT,
    description_raw TEXT,
    price_raw       TEXT,
    status_raw      TEXT,
    condition_raw   TEXT,
    shipping_raw    TEXT,
    seller_name_raw TEXT,
    raw_page_path   TEXT
);

-- Analysis-ready clean layer: one row per usable (game_id, listing_id),
-- merging search_results_candidates + listing_details_raw + games.
-- Built/refreshed by clean_market_data.py --build-clean.
CREATE TABLE IF NOT EXISTS market_listings_clean (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id          TEXT NOT NULL,
    game_id             TEXT NOT NULL,
    canonical_title     TEXT,
    platform_final      TEXT,
    title_raw           TEXT,
    description_raw     TEXT,
    price_jpy           REAL,
    status_final        TEXT,
    condition_raw       TEXT,
    shipping_raw        TEXT,
    seller_name_raw     TEXT,
    is_standard_edition INTEGER,
    is_bundle           INTEGER,
    is_excluded         INTEGER,
    exclude_reason      TEXT,
    release_date        TEXT,
    scraped_at          TEXT,
    days_since_release  REAL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    FOREIGN KEY (game_id) REFERENCES games(game_id)
);

-- Deduplicated + classified intermediate layer over search_results_raw.
-- One row per (game_id, listing_id); populated by clean_market_data.py.
CREATE TABLE IF NOT EXISTS search_results_candidates (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id          TEXT NOT NULL,
    game_id             TEXT,
    title_raw           TEXT,
    listing_url         TEXT,
    price_raw           TEXT,
    sold_flag_raw       TEXT,
    first_seen_at       TEXT,
    last_seen_at        TEXT,
    seen_count          INTEGER,
    best_rank           INTEGER,
    platform_guess      TEXT,
    is_standard_edition INTEGER,
    is_bundle           INTEGER,
    is_excluded         INTEGER,
    exclude_reason      TEXT,
    needs_detail_scrape INTEGER,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_games_game_id
    ON games(game_id);
CREATE INDEX IF NOT EXISTS idx_search_runs_game_id
    ON search_runs(game_id);
CREATE INDEX IF NOT EXISTS idx_search_results_raw_listing_id
    ON search_results_raw(listing_id);
CREATE INDEX IF NOT EXISTS idx_listing_details_raw_listing_id
    ON listing_details_raw(listing_id);
CREATE INDEX IF NOT EXISTS idx_clean_listing_id
    ON market_listings_clean(listing_id);
CREATE INDEX IF NOT EXISTS idx_clean_game_id
    ON market_listings_clean(game_id);
CREATE INDEX IF NOT EXISTS idx_clean_platform
    ON market_listings_clean(platform_final);
CREATE INDEX IF NOT EXISTS idx_clean_status
    ON market_listings_clean(status_final);
CREATE INDEX IF NOT EXISTS idx_clean_days_since_release
    ON market_listings_clean(days_since_release);
CREATE INDEX IF NOT EXISTS idx_candidates_listing_id
    ON search_results_candidates(listing_id);
CREATE INDEX IF NOT EXISTS idx_candidates_game_id
    ON search_results_candidates(game_id);
CREATE INDEX IF NOT EXISTS idx_candidates_needs_detail
    ON search_results_candidates(needs_detail_scrape);
CREATE INDEX IF NOT EXISTS idx_candidates_excluded
    ON search_results_candidates(is_excluded);
"""


def now() -> str:
    """Current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def load_games(config_path: Path) -> dict:
    with config_path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def upsert_games(conn: sqlite3.Connection, games: dict) -> int:
    """Insert or update each game record. Preserves created_at on update."""
    ts = now()
    rows = 0
    for entry in games.values():
        conn.execute(
            """
            INSERT INTO games (game_id, canonical_title, platform, release_date,
                               publisher, edition_scope, created_at, updated_at)
            VALUES (:game_id, :canonical_title, :platform, :release_date,
                    :publisher, :edition_scope, :ts, :ts)
            ON CONFLICT(game_id) DO UPDATE SET
                canonical_title = excluded.canonical_title,
                platform        = excluded.platform,
                release_date    = excluded.release_date,
                publisher       = excluded.publisher,
                edition_scope   = excluded.edition_scope,
                updated_at      = excluded.updated_at
            """,
            {
                "game_id": entry["game_id"],
                "canonical_title": entry["canonical_title"],
                "platform": entry["platform"],
                "release_date": entry.get("release_date"),
                "publisher": entry.get("publisher"),
                "edition_scope": entry.get("edition_scope"),
                "ts": ts,
            },
        )
        rows += 1
    return rows


def main() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    RAW_PAGES_DIR.mkdir(parents=True, exist_ok=True)

    games = load_games(CONFIG_PATH)

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA)
        count = upsert_games(conn, games)
        conn.commit()

    print(f"Initialized {DB_PATH}")
    print(f"Seeded/updated {count} game record(s): {', '.join(games)}")


if __name__ == "__main__":
    main()
