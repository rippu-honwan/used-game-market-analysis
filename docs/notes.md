# Research notes

Manual observations and decisions. Keep this updated as scraping behavior is
learned — it captures things the code/schema can't.

## Mercari search behavior

- TODO: note how search ranking/sorting behaves (relevance vs. newest).
- TODO: note pagination limits and any rate-limiting observed.
- TODO: note how sold vs. on-sale items appear in search results.

## Keyword noise

- Searching プラグマタ / PRAGMATA also surfaces unrelated or off-target items.
- See `exclude_keywords` in `configs/games.yaml` for the current filter list.
- TODO: log false positives/negatives here to tune keywords over time.

## Standard edition filtering

- Scope is **standard edition only**. Limited / collector / deluxe / bundle /
  download-code listings are excluded.
- Filtering is keyword-based for now (`exclude_keywords`); imperfect.
- TODO: record edge cases where keyword filtering misclassifies an item.

## Future event annotation

- Plan: annotate price observations with events (launch, restock, sale,
  review/news spikes) for later analysis.
- Key date so far: PRAGMATA public release 2026-04-24.
- TODO: decide where event annotations live (config vs. a future DB table).
