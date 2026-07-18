# Economic News Aggregator v3

Aggregates, clusters, and summarises economic news from ~240 sources, with
country/topic tagging, an LLM-curated daily top 10, and a prediction-accuracy
tracker. See [`PROJECT_BRIEF.md`](PROJECT_BRIEF.md) for full architecture and
phase plan.

Status: **Phase 0-6 complete**: repo/schema/feed list/taxonomy, the fetcher,
Cerebras summarisation + translation, country/topic tagging, story
clustering (carousel-ready), an LLM-curated daily top 10, a static site
(`pipeline.build`: homepage with the top 10 and category carousels, a full
date-indexed archive, country/topic filters + search over a client-side
index), data exports (`pipeline.export`: rolling `exports/master.csv`,
one `exports/daily/{date}.csv` per archive day, `latest.json`, `feed.xml`),
a source health dashboard (`site/health.html`, streak-based with
auto-recovery of deactivated feeds -- `pipeline.fetch`), and a
prediction-accuracy tracker (`pipeline.predictions` extracts falsifiable
predictions during summarisation, `pipeline.resolve` proposes verdicts from
the archive once a horizon passes, `pipeline.review` is the local
one-keypress confirmation queue -- see its docstring for why that's local
rather than a web control given GitHub Pages is static, `site/predictions.html`
shows the read-only queue + accuracy leaderboards) -- all wired into a GitHub
Actions workflow that runs the full pipeline several times a day and deploys
`site/` to GitHub Pages via `actions/deploy-pages`.

**Before the scheduled workflow can deploy**, enable Pages in the repo:
Settings -> Pages -> Build and deployment -> Source: "GitHub Actions". This
is a one-time manual step (GitHub won't let a workflow turn Pages on for
itself).

Note: the brief specifies Llama 3.3 70B on Cerebras, but that model is no
longer offered on the free tier as of Phase 2 (confirmed via `/v1/models`
2026-07-18) -- `gpt-oss-120b` is configured instead (see `config.yaml`).
The free tier is also 5 req/min (not 30 as stated in the brief).

## Layout

- `pipeline/` — Python modules (`config`, `db`, `fetch`, `validate_feeds`, ...)
- `config.yaml` — paths and run parameters
- `config/feeds.yaml` — all 240 sources, grouped by region, with validation status
- `config/taxonomy.yaml` — keyword/theme taxonomy for tagging
- `data/` — `feeds.csv` (raw source list), `feeds_validated.csv` (validation
  results), `aggregator.db` (SQLite archive, gitignored)
- `templates/`, `static/` — Jinja2 templates and CSS/JS for the static site
- `site/` — generated site output (`pipeline.build` + `pipeline.export`), gitignored

## Setup

```
pip install -r requirements.txt
cp .env.example .env   # fill in CEREBRAS_API_KEY once you have one
python -m pipeline.db              # initialise the schema
python -m pipeline.validate_feeds  # re-validate all feeds, populate the feeds table
python -m pipeline.fetch           # fetch new articles from active feeds (also probes inactive feeds for auto-recovery)
python -m pipeline.summarize       # summarise + translate fetched articles via Cerebras
python -m pipeline.predictions     # extract falsifiable predictions from summarised articles
python -m pipeline.tag             # country + topic tagging via config/taxonomy.yaml
python -m pipeline.cluster         # group same-story articles (carousel-ready)
python -m pipeline.curate          # LLM-curated daily top 10 -> daily_top10 table
python -m pipeline.resolve         # propose verdicts for predictions past their horizon (never auto-published)
python -m pipeline.build           # render the static site into site/ (incl. health.html, predictions.html)
python -m pipeline.export          # write site/exports/*.csv, latest.json, feed.xml
python -m pipeline.review          # local, one-keypress confirmation of proposed prediction verdicts
```

Open `site/index.html` via a local server (not `file://` -- the search box
fetches `search-index.json`, which browsers block over `file://`), e.g.:
`cd site && python -m http.server 8000`.

Scheduling is GitHub Actions (not local/OS-level) — see
`.github/workflows/pipeline.yml`, which runs the full fetch → summarize →
predictions → tag → cluster → curate → resolve → build → export chain six
times a day, persists
`data/aggregator.db` between runs via `actions/cache` (the db is
gitignored, so it's never committed). Because `actions/cache` is
best-effort (7-day LRU eviction, 10GB/repo cap) and would otherwise be the
*only* copy of the article archive, every run also backs up a gzipped
snapshot to the `db-latest` GitHub Release, restoring from it automatically
if the cache ever comes back empty. The workflow also deploys the rendered
`site/` directory to GitHub Pages via `actions/upload-pages-artifact` +
`actions/deploy-pages` (a separate `deploy` job, not a `gh-pages` branch
commit). Add `CEREBRAS_API_KEY` as a repo secret at Settings → Secrets and
variables → Actions, and set Pages source to "GitHub Actions" (Settings →
Pages), before enabling the schedule.
