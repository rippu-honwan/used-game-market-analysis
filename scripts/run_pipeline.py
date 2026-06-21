"""Run the full Mercari pipeline for one game_id, in order.

Steps: search -> candidate -> detail -> clean. Each step shells out to the
existing script with conservative settings. Stops on the first failure.

Usage:
    python scripts/run_pipeline.py                      # pragmata_switch2 (default)
    python scripts/run_pipeline.py pragmata_ps5
    python scripts/run_pipeline.py --limit 50 --delay-seconds 2.0
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
PY = sys.executable  # ponytail: reuse the interpreter running us, avoids python/python3 mismatch


def step(label: str, args: list[str]) -> None:
    print(f"\n{'=' * 60}\n{label}\n{'=' * 60}", flush=True)
    cmd = [PY, str(SCRIPTS / args[0]), *args[1:]]
    print("$ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)  # check=True -> stop on obvious failure


def main() -> None:
    p = argparse.ArgumentParser(description="Run search->candidate->detail->clean for one game_id")
    p.add_argument("game_id", nargs="?", default="pragmata_switch2", help="Target game_id (default pragmata_switch2)")
    p.add_argument("--max-pages", type=int, default=1)
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--delay-seconds", type=float, default=2.0)
    args = p.parse_args()

    g, d = args.game_id, str(args.delay_seconds)
    step("[STEP 1] Search", ["scrape_search.py", "--game-id", g, "--max-pages", str(args.max_pages), "--delay-seconds", d])
    step("[STEP 2] Candidate", ["clean_market_data.py", "--game-id", g, "--replace-existing"])
    step("[STEP 3] Detail", ["scrape_listing.py", "--game-id", g, "--limit", str(args.limit), "--delay-seconds", d])
    step("[STEP 4] Clean", ["clean_market_data.py", "--game-id", g, "--replace-existing", "--build-clean"])

    print(f"\nPipeline complete for {g}.")


if __name__ == "__main__":
    main()
