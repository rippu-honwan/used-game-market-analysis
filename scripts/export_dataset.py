"""Export the cleaned market dataset to CSV.

Exports market_listings_clean to a CSV file if the table has rows. The
analysis pipeline is not built yet, so this only dumps the cleaned table.

Usage:
    python scripts/export_dataset.py [--output PATH]

Default output: data/market_listings_clean.csv
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "mercari.sqlite"
DEFAULT_OUTPUT = ROOT / "data" / "market_listings_clean.csv"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_OUTPUT, help="CSV output path"
    )
    args = parser.parse_args()

    if not DB_PATH.exists():
        raise SystemExit(f"Database not found: {DB_PATH}. Run scripts/init_db.py first.")

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM market_listings_clean").fetchall()

    if not rows:
        print("market_listings_clean is empty — nothing to export.")
        print("Run the scrape + clean steps first, then re-run this script.")
        return

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(rows[0].keys())
        writer.writerows(tuple(r) for r in rows)

    print(f"Exported {len(rows)} row(s) to {args.output}")

    # TODO: once the analysis pipeline exists, export curated views
    # (e.g. sold-only, per-platform price series) instead of the raw clean table.


if __name__ == "__main__":
    main()
