# Economic News Aggregator v3

Aggregates, clusters, and summarises economic news from ~240 sources, with
country/topic tagging, an LLM-curated daily top 10, and a prediction-accuracy
tracker. See [`PROJECT_BRIEF.md`](PROJECT_BRIEF.md) for full architecture and
phase plan.

Status: **Phase 0 complete** (repo, schema, feed list, taxonomy). Phase 1
(fetcher) is ported and working; Phase 2+ (Cerebras summarisation, site
generation, outputs) not yet built.

## Layout

- `pipeline/` — Python modules (`config`, `db`, `fetch`, `validate_feeds`, ...)
- `config.yaml` — paths and run parameters
- `config/feeds.yaml` — all 240 sources, grouped by region, with validation status
- `config/taxonomy.yaml` — keyword/theme taxonomy for tagging
- `data/` — `feeds.csv` (raw source list), `feeds_validated.csv` (validation
  results), `aggregator.db` (SQLite archive, gitignored)

## Setup

```
pip install -r requirements.txt
cp .env.example .env   # fill in CEREBRAS_API_KEY once you have one
python -m pipeline.db              # initialise the schema
python -m pipeline.validate_feeds  # re-validate all feeds, populate the feeds table
python -m pipeline.fetch           # fetch new articles from active feeds
```

Scheduling is GitHub Actions (not local/OS-level) — see `.github/workflows/`
once Phase 2 adds the cron job.
