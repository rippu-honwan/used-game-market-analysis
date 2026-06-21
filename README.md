# Mercari Game Market Analysis

A student research project analyzing the **second-hand physical video game**
market on **Mercari Japan**.

## Purpose

Track how prices and availability of used physical game copies behave on
Mercari over time — around launch, restocks, and other events. The pipeline
scrapes search/listing pages, stores structured data locally, and (later)
exports clean datasets for analysis.

## Current scope

- **Marketplace:** Mercari Japan
- **Game:** PRAGMATA (CAPCOM, release 2026-04-24)
- **Platforms:** PS5 and Nintendo Switch 2
- **Editions:** standard edition only (collector / limited / bundle editions
  are excluded via keyword filtering)

The repo is designed to support more games later — each target is one entry in
`configs/games.yaml`, keyed by `game_id`.

## Design: SQLite-first

- **Structured data lives in SQLite** (`data/mercari.sqlite`): game targets,
  search runs, raw search results, raw listing details, and a cleaned
  `market_listings_clean` table.
- **Raw HTML lives on disk** under `data/raw_pages/`. Every scraped page is
  saved so parsing can be re-run later without re-fetching. Database rows
  reference the saved file via a `raw_page_path` column.
- **Analysis exports go to CSV** (later) via `scripts/export_dataset.py`.

## Folder structure

The repository root is the project root:

```text
./
  README.md
  requirements.txt
  .gitignore
  configs/
    games.yaml              # game targets, keywords, exclude rules
  data/
    mercari.sqlite          # main local storage (created by init_db.py)
    raw_pages/              # saved raw HTML pages
  scripts/
    init_db.py              # create schema + seed game records
    scrape_search.py        # scrape search results via JSON API
    scrape_listing.py       # scrape listing detail pages
    clean_market_data.py    # candidates + clean analysis table
    run_pipeline.py         # run search->candidate->detail->clean for one game_id
    export_dataset.py       # export market_listings_clean to CSV
  notebooks/
    pragmata_explore.ipynb  # quick DB preview
  docs/
    notes.md                # manual observations
```

## Getting started

```bash
pip install -r requirements.txt
python scripts/init_db.py     # creates data/mercari.sqlite and seeds games
```

## Collecting search results

`scripts/scrape_search.py` collects listings for the keywords in
`configs/games.yaml` from Mercari's **web JSON API** (the same
`POST https://api.mercari.jp/v2/entities:search` endpoint the web app calls).
The earlier HTML approach found nothing because results are rendered
client-side. For each keyword it records one row in `search_runs`, saves the raw
JSON response to `data/raw_pages/`, and inserts listing rows into
`search_results_raw`. It is conservative: a delay between requests, a clear
User-Agent, and graceful handling of request/JSON failures.

```bash
# all games, page 1 per keyword, 2s between requests
python scripts/scrape_search.py

# one game, multiple pages, explicit delay
python scripts/scrape_search.py --game-id pragmata_ps5 --max-pages 1 --delay-seconds 2.0

# offline check of the parser (no network)
python scripts/scrape_search.py --self-test
```

How it works / limitations:

- The API requires a `DPoP` (ES256 JWT) header plus `X-Platform: web`. The token
  is signed with a key we generate locally each run (`ecdsa`); no login is
  involved — Mercari's web client does the same.
- Mapped fields: `id`→`listing_id`, `name`→`title_raw`, `price`→`price_raw`,
  `status`→`sold_flag_raw` (raw `ITEM_STATUS_*`), first `thumbnails[]`→
  `thumbnail_url`, list position→`rank`. `listing_url` is built from the id.
- Pagination is cursor-based (`meta.nextPageToken`); `--max-pages` caps it.
- Prices/status are stored **raw**; normalization happens later in the cleaning
  step. This is an undocumented internal API and may change without notice.

## Candidate layer (dedup + filter)

`scripts/clean_market_data.py` collapses the repeated `search_results_raw` rows
into one row per `(game_id, listing_id)` in the **`search_results_candidates`**
table — a deduplicated, keyword-classified intermediate layer that sits before
listing-detail scraping. For each listing it records first/last seen, seen
count, best rank, and best-effort flags (`platform_guess`, `is_standard_edition`,
`is_bundle`, `is_excluded`, `exclude_reason`, `needs_detail_scrape`). Rules are
keyword-based and driven by `configs/games.yaml`; prices are **not** normalized
here.

```bash
python scripts/clean_market_data.py                       # upsert all games
python scripts/clean_market_data.py --game-id pragmata_ps5 --replace-existing
python scripts/clean_market_data.py --self-test           # offline classifier check
```

It writes a human-readable `data/search_results_candidates_preview.csv` and
prints a summary (excluded counts, platform/sold breakdowns, top exclude
reasons). The intended next step scrapes details only for rows where
`needs_detail_scrape = 1`.

## Collecting listing details

`scripts/scrape_listing.py` fills `listing_details_raw` for each candidate
flagged `needs_detail_scrape = 1`. Detail pages are **not** scraped from HTML:
the item page is a server-rendered shell whose static HTML only exposes
title/price via `meta` tags. Instead it uses Mercari's **item JSON API**:

```text
GET https://api.mercari.jp/items/get?id=<listing_id>
headers: X-Platform: web  +  DPoP: <ES256 JWT>
```

This is the same authenticated web-API approach as `scrape_search.py` — the
`DPoP` signing, User-Agent and Origin are imported straight from it (one
self-signed ES256 key per process, no login). The response is
`{"result":"OK","data":{...}}` and the item object carries every field we need:
`name`, `description`, `price`, `status`, `item_condition.name`,
`shipping_payer.name`, `seller.name`. The raw JSON response is saved to
`data/raw_pages/` as `{game_id}_listing_{listing_id}_{timestamp}.json`.

```bash
# scrape up to 30 PRAGMATA PS5 listings, 2s between requests
python scripts/scrape_listing.py --game-id pragmata_ps5 --limit 30

# re-scrape rows that already have details
python scripts/scrape_listing.py --game-id pragmata_ps5 --limit 30 --force

# offline parser check (no network)
python scripts/scrape_listing.py --self-test
```

Polite by default: a small `--limit` (50), a `--delay-seconds` floor of 1.0s,
and per-listing error isolation. **Run several small batches rather than one
large scrape.** Without `--force`, listings already in `listing_details_raw`
are skipped, so re-running simply continues where the last batch stopped.
Fetch/parse failures are logged and skipped (not written to the table, so they
retry next run); the raw response is still saved for debugging. Mercari Shops
products (`/shops/product/`) aren't served by this endpoint and fall through as
skips. Values are stored **raw** — no normalization happens here.

## Analysis layer (`market_listings_clean`)

`market_listings_clean` is the **main table for notebooks and analysis**. It
merges three sources into one analysis-ready row per usable
`(game_id, listing_id)`:

- `search_results_candidates` — platform/edition/exclusion flags,
- `listing_details_raw` — title, description, price, status, condition,
  shipping, seller (from the item JSON API),
- `games` — canonical title and release date.

Only **usable** rows are included: candidates with `is_excluded = 0` that have a
detail row with a price. On top of the raw fields it adds a few first-pass
derived columns — numeric `price_jpy`, a normalized `status_final`
(`on_sale` / `sold_out` / `trading` / `unknown`), `platform_final` (the candidate
guess, refined from the detail title/description when it was unknown), and
`days_since_release` (scraped date − release date, in days). Raw JP labels
(`condition_raw`, `shipping_raw`) are copied verbatim — no normalization yet.

```bash
# phase 2: build/refresh the clean table for one game (delete + reinsert)
python scripts/clean_market_data.py --game-id pragmata_ps5 --replace-existing --build-clean

# all games; without --replace-existing it upserts by (game_id, listing_id)
python scripts/clean_market_data.py --build-clean
```

It prints a summary (rows inserted/updated, counts by `status_final`,
min/median/max of `price_jpy` and `days_since_release`, NULL counts) and a few
example rows. It depends on detail data, so run `scrape_listing.py` first; the
clean table only covers listings whose details have been collected.

## Running the full pipeline (e.g. PRAGMATA Switch 2)

The four steps above (search → candidate → detail → clean) are the same for
every `game_id`. `scripts/run_pipeline.py` runs them in order for one game,
with conservative defaults (1 search page, detail `--limit 50`, 2s delay) and
stops on the first failure. It prints `[STEP 1] Search` … `[STEP 4] Clean`
banners between steps.

```bash
# full pipeline for Switch 2 (the default game_id)
python scripts/run_pipeline.py

# any other game_id, e.g. re-run PS5
python scripts/run_pipeline.py pragmata_ps5

# tune volume/politeness
python scripts/run_pipeline.py pragmata_switch2 --max-pages 1 --limit 50 --delay-seconds 2.0
```

The same pipeline now backs **both** platforms: results land in
`market_listings_clean` with `game_id = 'pragmata_ps5'` and
`game_id = 'pragmata_switch2'`, ready to compare in `pragmata_explore.ipynb`.
The detail step is incremental (skips listings already scraped, up to
`--limit` per run), so re-run `run_pipeline.py` to extend coverage beyond the
first 50 candidates.

## Status

Implemented end to end: search collection (`scrape_search.py`), the candidate
layer and the clean analysis table (`clean_market_data.py`), and listing-detail
scraping (`scrape_listing.py`, item JSON API). Not done yet: price normalization
beyond a numeric cast, condition-label normalization, and analysis notebooks/plots.

## Notes

- **Commits are manual.** No script or tool in this repo commits to git — the
  human commits changes intentionally.
