# Economic News Aggregator v3 — Project Brief

This file is context for Claude Code. Read it fully before doing any work, and re-read it at the start of each phase. Do not ask the user to repeat anything already answered here.

## 0. Starting situation

There are potentially THREE generations of this project on disk:

- **v1**: `aggregator.py`, a 3,004-line single-file prototype. It worked (Ollama + email digest) until a scheduled task silently broke in May 2026. It has already been audited. Findings below in "Salvage manifest."
- **v2**: a folder worked on yesterday, which is where you (Claude Code) are starting this session. It may already contain a partial rebuild.
- **v3**: the target — described in this brief.

**Your first task, before writing any new code:** audit the v2 folder you're starting in. Compare what's there against this brief's architecture and feature list. Decide and tell the user:
- Continue building in this folder (if its structure/dependencies already roughly match this brief), OR
- Start a fresh folder/repo and port over anything in v2 worth keeping (if it's a false start, wrong architecture, or has the same kind of rot v1 had — check for hardcoded secrets before anything else).

Report your recommendation and reasoning, then wait for the user to confirm before proceeding to Phase 0.

## 1. Already resolved — do not redo

- The v1 hardcoded Gmail app password has been revoked. If you find any credential hardcoded anywhere (v1 or v2 folder), flag it immediately regardless of phase — don't wait to mention it.
- The zombie Windows scheduled task pointing at the old v1 path has been deleted. Scheduling in v3 is GitHub Actions, not Windows Task Scheduler — do not create any local OS-level scheduled task.
- GitHub repo: none exists yet (confirmed — no `.git` folder anywhere). Create a **public** repo for this project (user's decision).
- The v2 folder has been audited (Prompt 0). Verdict: **start a fresh folder**, do not continue in v2. It was built against the wrong architecture (local Ollama, Windows Task Scheduler, a vector-embedding DB column) — all three directly contradict §2's "final" decisions. However, three of its files are solid working ports and should be migrated into the new repo rather than rebuilt: `pipeline/db.py` (SQLite schema — strip the `embedding BLOB` column, it was for local-embedding clustering which isn't part of this design), `pipeline/fetch.py` (async/httpx fetch + dedup + trafilatura fallback — already a real port of v1's fetch logic), `pipeline/validate_feeds.py` (working feed validator). Also migrate `data/feeds_validated.csv`, but note it only covers 140 of the 240 feeds in `RSS Feeds.docx` — it validated the old "active" list only, not the 100 previously-"disabled" ones the brief §4 specifically says to re-check. That gap must be closed in the new Phase 0, not treated as done.
- Add `.env` to `.gitignore` immediately when the new repo is created — before any `.env` file exists — so a future Cerebras key can never be committed by accident.
- Cerebras: the user has NOT signed up yet. Before Phase 2 needs it, stop and tell the user to create a free account at cloud.cerebras.ai and generate an API key. It must be stored as a GitHub Actions secret (`CEREBRAS_API_KEY`) and/or local `.env` (gitignored) — never committed.

## 2. Architecture decisions (final — do not re-litigate)

- **Language**: Python
- **Storage**: SQLite, one persistent article archive (this is new — v1 had no database, articles only lived inside daily HTML files)
- **LLM**: Cerebras API, Llama 3.3 70B, free tier (1M tokens/day, 30 req/min, 8,192 token context). NOT local Ollama — the user's laptop (MSI Prestige 14 AI EVO, Core Ultra 7 155H, 16GB RAM, no discrete GPU) is not in the runtime path at all in v3.
- **Compute/scheduling**: GitHub Actions cron, not the user's machine. This is deliberate — v1 died silently because it depended on a specific machine being on. v3 must not have that failure mode.
- **Hosting**: GitHub Pages, static site.
- **Site generation**: Jinja2 templates. NOT an f-string HTML builder (v1's `build_html` was a 1,300-line f-string — keep the *rendered output* as a UX/design reference only, never the code).
- **Config**: YAML. NOT .docx (v1's config-as-Word-documents caused Unicode whitespace bugs).
- **Outputs**: HTML site, CSV (daily snapshot + rolling master), JSON (`latest.json`), RSS feed.

## 3. Confirmed v3 feature list

1. Story clustering with a carousel UI: each cluster shows up to 5 articles, left/right arrows to click through different outlets' takes on the same story.
2. Country + topic tagging, with filterable site views.
3. Daily top-10, curated by the LLM for significance — NOT sorted by keyword-count score (that was v1's method and is explicitly being replaced).
4. Own RSS feed and JSON output (`latest.json`) as a de facto API.
5. Source health monitoring: track consecutive failure streaks per feed over time, auto-recover feeds that start working again. Explicitly do NOT replicate v1's `mark_dead_feeds.py` policy of permanently disabling a feed on a single failure — that policy is what silently killed ~100 good feeds (IMF, OECD, World Bank, Reuters, most central banks, all think tanks) in v1.
6. Translation of non-English sources via the same Cerebras call pattern.
7. Searchable research corpus — the SQLite archive should be queryable (e.g. "everything tagged Irish housing in the last 90 days") from day one, not bolted on later.
8. Prediction-accuracy tracker (new subsystem, no existing code):
   - **Extraction**: during summarization, a second LLM pass asks whether the article contains an explicit, falsifiable prediction (who, what metric/event, direction, time horizon). Only log predictions with a real horizon — discard vague hedges.
   - **Resolution**: when a prediction's horizon date passes, search the archive for coverage of the actual outcome, have the LLM propose a verdict (correct / wrong / unresolvable) with supporting article links, and surface it on a review page for one-click human confirmation. Do not auto-publish unverified verdicts.
   - **Output**: accuracy leaderboard per source and per named predictor, hit rate by topic.

## 4. Salvage manifest — from v1 (`aggregator.py` and its companion docs), IF not already present in the v2 folder

Check the v2 folder for these first — if v2 already ported them, verify quality against this list rather than re-porting.

| Asset | Location in v1 | Action |
|---|---|---|
| Feed list | `RSS Feeds.docx` | Export all 240 entries (including the 100 previously "disabled" ones) to YAML, grouped by region as before. Re-validate every single one from scratch — do not trust the old enabled/disabled flags, since the old policy disabled feeds on transient errors (timeouts, one-day-empty). |
| Keyword/theme taxonomy | `Keywords.docx`, plus `REGION_SLUG_MAP`, `THEME_PRIORITY`, `SUBCATEGORY_RULES` (`aggregator.py:79-204`) | Export to YAML. This is the seed data for feature #2 (country/topic tagging). |
| Clustering | `cluster_articles`, `_sig_words` (`aggregator.py:568-676`) | Port directly — union-find over shared title words (≥3) OR TF-IDF cosine >0.65, including the cross-region "perspectives" flag. Adapt the output shape to feed the carousel UI (feature #1), capped at 5 articles per cluster with a defined ranking for which 5 are shown if more exist. |
| Feed fetching | `fetch_one_feed`, `fetch_all_feeds` (`aggregator.py:317-409`) | Port directly. The existing error taxonomy (403/404/timeout/DNS/SSL) is the foundation for feature #5 — extend it to track streaks instead of one-shot kill. |
| Scoring + dedup | `aggregator.py:489-555` | Port the scoring logic. Dedup by first-70-chars-of-normalized-title is a fine first pass before clustering; don't over-engineer it since clustering is the real dedup mechanism. |
| Summarization prompt | `generate_summary` (`aggregator.py:847-894`) | Keep the prompt content; swap the call target from local Ollama URL to the Cerebras API (endpoint, auth header, response shape differ — everything else about the prompt design can stay). |
| Quality check | `feed_report.py` refusal-phrase detection | Port — cheap nugget that catches LLM refusal/hedge phrases slipping into summaries. |

**Explicitly do NOT carry over**: `.docx` as live config, print-statement logging, any hardcoded credential, the `build_html` f-string approach, kill-on-first-failure feed policy, anything from the unrelated TikTok/Instagram analytics project that was previously contaminating the same folder.

## 5. Phase plan

Work through phases in order. At the end of each phase, summarize what was built, flag any deviation from this brief, and stop for user confirmation before starting the next phase — don't chain multiple phases in one uninterrupted run.

- **Phase 0**: Folder/repo decision (see §0). Repo structure, `config.yaml`, SQLite schema, feed list exported from docx + fully re-validated, taxonomy exported to YAML.
- **Phase 1**: Fetcher — ported `fetch_one_feed`/`fetch_all_feeds`, dedup, article text extraction fallback (e.g. `trafilatura`) for sources worth full-text.
- **Phase 2**: Cerebras summarization + translation layer (requires user's API key — stop and ask if not yet provided), GitHub Actions workflow for incremental runs through the day.
- **Phase 3**: Ported clustering adapted for the carousel, country/topic tagging applied via taxonomy, LLM-curated top 10.
- **Phase 4**: Static site — Jinja2 templates, carousel UI (5-article cap, arrow nav), filters by country/topic, archive view, search over the SQLite corpus.
- **Phase 5**: Outputs — daily + master CSV, `latest.json`, RSS feed, GitHub Pages deploy step.
- **Phase 6**: Source health dashboard (failure-streak based), prediction extractor, resolution review queue, accuracy leaderboard.

## 6. Standing rules for every phase

- No secrets in code, ever — env vars / GitHub Secrets only.
- No local OS-level scheduling — GitHub Actions only.
- Prefer editing/porting existing salvageable functions over rewriting from scratch where the manifest above says to port.
- If something in the v2 folder contradicts this brief, flag it and ask rather than silently overriding either the folder or the brief.
