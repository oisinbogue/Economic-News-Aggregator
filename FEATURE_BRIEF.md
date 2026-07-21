# Economic News Aggregator — Feature Brief

## What it is
A fully automated economic news aggregator. A scheduled pipeline fetches articles from ~240 RSS/Atom feeds, summarizes and tags them with an LLM, clusters duplicate coverage, curates a daily top-10, tracks falsifiable predictions made in articles over time, and publishes a static site to GitHub Pages. No server, no database server — everything runs in GitHub Actions and outputs static files.

## Tech stack
- Python 3.12, SQLite (local archive, gitignored)
- Jinja2 for static site generation (no frontend framework, no JS build step)
- `httpx` + `feedparser` for async feed fetching, `trafilatura` for full-text/thumbnail extraction
- `scikit-learn` (TF-IDF) for article clustering
- Cerebras API (`gpt-oss-120b`) for all LLM tasks (summarize, translate, extract predictions, curate, resolve predictions)
- GitHub Actions for scheduling/CI, GitHub Pages for hosting, GitHub Releases as backup blob storage

## Pipeline (sequential modules, run on a cron)
1. **Fetch** — pulls ~240 feeds, dedups by SHA-256 of URL, skips live-blog/rolling-coverage URLs (unreliable text-to-story mapping); a companion cleanup job purges any previously-fetched live-blog articles and their downstream data.
2. **Summarize** — LLM summary + translation for non-English sources.
3. **Predictions** — extracts falsifiable predictions (who/what/direction/horizon) from article text.
4. **Tag** — country + topic tags via a keyword taxonomy.
5. **Cluster** — groups "same story, multiple outlets" using shared title words / TF-IDF cosine similarity within a 3-day window, capped at 5 articles per cluster.
6. **Curate** — LLM picks a daily top-10 from a shortlisted pool (not a keyword score).
7. **Resolve** — once a prediction's time horizon passes, LLM proposes a verdict (correct/wrong/unresolvable); never auto-published — requires a human keypress confirmation via a local CLI, since the static site can't host that control.
8. **Build** — renders the site: homepage (top-10 + category carousels), full date-indexed archive, source health dashboard, predictions dashboard, client-side search index.
9. **Export** — writes a rolling master CSV, per-day CSVs, `latest.json`, and an RSS feed — a de facto public API.
10. **Image backfill** — separate idempotent script that fills in missing thumbnails via og:image lookups.

## Frontend features
- Carousel (prev/next + dot indicators) for multi-outlet story clusters
- Clickable topic/country filter chips; clicking a tag on an article activates the matching chip and scrolls to it; empty sections collapse when a filter yields nothing; "clear filters" appears only when active
- Debounced client-side search over title/summary/country/topics
- Dark mode toggle, persisted in localStorage
- Editorial-style layout with article thumbnails, hero/flank treatment for top stories
- Zero build tooling — plain HTML/CSS/JS

## Storage & reliability
- SQLite is the single source of truth (feeds, articles, clusters, predictions, daily_top10 tables)
- Per-feed failure-streak tracking with auto-recovery probing (deactivated feeds are periodically retried rather than killed permanently)
- Backups: `actions/cache` persists the DB between runs (best-effort/evictable) + a gzipped copy uploaded to a dedicated GitHub Release every run as a versioned snapshot (never overwritten in place, so a failed upload can never leave zero valid backups); newest 3 snapshots kept

## Deployment
Single GitHub Actions workflow, cron 6x/day + manual dispatch, concurrency-guarded against overlapping runs, deploys to GitHub Pages. Requires a `CEREBRAS_API_KEY` secret.

## Context for improvement ideas
This is v2 of the project — Python/SQLite/YAML/Jinja2/GitHub Actions were chosen specifically to avoid failure modes of a prior prototype (single-machine dependency, fragile config format, kill-feed-on-first-failure policy). Open areas that might be worth exploring with fresh eyes: search UX (currently basic debounced client-side matching), prediction-resolution workflow (currently manual keypress CLI), source health/monitoring visibility, personalization/filtering depth, and whether the static-site constraint is still the right tradeoff as features grow.
