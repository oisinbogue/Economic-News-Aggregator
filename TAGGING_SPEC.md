# Tagging Accuracy Spec

Plan for replacing the keyword-based topic/country tagger with LLM- and
embedding-derived tags. Written 2026-08-06. Four independent phases, each
sized for one working session; later phases assume earlier ones have landed.

## Why

Measured over the full published corpus (`exports/master.csv`, 4,298 articles
as of 2026-08-06):

| Measure | Result |
|---|---|
| Articles with **no topic at all** | 3,703 / 4,298 (**86%**) |
| Tagged articles resting on a **single keyword hit** | 412 / 595 (**69%**) |
| Articles with ≥4 country tags | 206 (5%), long tail out to 11 |
| Articles tagged only `International` | 733 |

Two failure modes, and the recall one is the larger:

- **Precision.** `pipeline/tag.py:52-68` (`score_and_tag`) matches taxonomy
  keywords anywhere in title+summary+body with no threshold and no position
  weighting, so one incidental word assigns the theme for the whole article.
  69% of all tagged articles rest on exactly one keyword. Worked example:
  *"Kelly Earley: I like spice bags as much as the next person, but we need to
  draw the line"* (thejournal.ie, 2026-08-04) is a food/health-regulation
  opinion piece tagged `DEMOGRAPHICS & MIGRATION` because it mentions spice
  bags being popular with the Irish diaspora, and `diaspora` is an active
  keyword under that theme in `config/taxonomy.yaml`. Sampling the 53
  articles carrying that theme, the ones inspected were all single-keyword
  matches, with titles like *"India suspends policeman for firing AK-47 at
  student protesters"* and *"How can a country as wet as Ireland need hosepipe
  bans?"*.

- **Recall.** ~160 hand-written keywords across 8 themes cannot cover
  economics journalism. 86% of the corpus is untagged, which is why the
  site's topic filters return almost nothing — the same spice bag article is
  the *only* result for `DEMOGRAPHICS & MIGRATION` ∩ `China/Greater China`,
  not because it is a lone false positive but because nothing else in the
  index competes with it.

Country tagging compounds this: `pipeline/geo.py:102-120` accumulates matches
into a `set`, so every country mentioned anywhere gets equal standing with the
one the story is actually about. That is how the spice bag piece carries
`Australia/New Zealand, Canada, China/Greater China, Ireland, Middle East,
United Kingdom, United States`.

### Out of scope

A meaningful share of intake is not economics at all — sampled untagged
titles include *"Word Search"*, a road-accident report, a rape conviction and
a missile test. A relevance gate belongs upstream of tagging and is a separate
decision, deliberately not covered here.

---

## Phase 1 — Rank countries instead of set-unioning them

**Files:** `pipeline/geo.py`, `pipeline/curate.py`, `pipeline/db.py`,
`tests/test_geo.py`

Rewrite `detect_countries` to return countries *ranked by evidence strength*
rather than an unordered set:

- Count mentions per country rather than recording a boolean hit.
- Weight by position — a country named in the title or opening paragraph
  outranks one named once in the last paragraph. This is the single change
  that fixes the spice bag case.
- Keep the top 3. The existing tail runs to 11 countries on one article; past
  ~3 the tags carry no information and actively pollute the site's filters.
- Fix the current asymmetry: NER reads only the first `_NER_CHAR_CAP` (4,000)
  characters (`pipeline/geo.py:40`) while the institution substring pass runs
  over the full text, so a central bank named in the last paragraph counts but
  a city named there does not. Make both passes agree on scope.

`articles.country` stays a comma-separated `TEXT` column but becomes **order-
significant**, most-relevant first. Document that in the `db.py` schema
comment, since "comma-separated" currently implies no ordering.

Then fix `dominant_country` (`pipeline/curate.py:146-154`), which picks the
**alphabetically first** country. For the spice bag article that yields
`Australia/New Zealand` — so curation's `max_per_country` diversity cap is
currently counting an incidental mention as the article's country. It should
read position 0 of the now-ranked list.

Add a `tag_source` column to `articles` (`'keyword' | 'llm' | 'embedding'`,
NULL for pre-existing rows) following the `ALTER TABLE` migration convention
at `pipeline/db.py:255-280`. Nothing writes anything but `'keyword'` yet;
Phases 2 and 3 depend on it to tell provenance apart and to re-run
selectively.

**Why first:** entirely self-contained, no LLM involvement, and both later
phases consume its ranked output.

**Done when:** no article carries more than 3 countries; the spice bag article
no longer carries `China/Greater China`; `test_geo.py` covers ranking, the
position weighting and the 3-country cap.

---

## Phase 2 — LLM tags on the existing summarise call

**Files:** `pipeline/summarize.py`, `pipeline/tag.py`, `pipeline/db.py`,
`tests/` (new `test_tag.py`)

`generate_summary` (`pipeline/summarize.py:60-79`) already makes one Cerebras
call per article. Extend that same call to also return the article's topics
and primary country. **The Cerebras free tier throttles on requests, not
tokens** (`pipeline/cerebras.py`, one call per 13s process-wide), so this
costs a handful of extra output tokens and *zero* additional calls or
rate-limit budget. That is what makes this affordable at all.

Design points:

- **Hybrid country selection.** Pass Phase 1's ranked candidates into the
  prompt and have the LLM *choose among them* rather than answer free-form.
  NER supplies recall and guarantees the answer lands in a real
  `config/countries.yaml` bucket; the LLM supplies the "what is this actually
  about" judgement that keyword and mention-counting cannot.
- **Closed topic set.** Constrain output to the 8 themes in
  `config/taxonomy.yaml` plus an explicit "none of these", so the model can
  decline instead of guessing. Reject and retry anything outside the set.
- **Token headroom.** `pipeline/cerebras.py:71-81` documents that
  `gpt-oss-120b` draws hidden reasoning tokens from the same `max_tokens`
  budget, failing with `finish_reason='length'` and *no content at all*
  rather than a truncated answer. The current call allows 900; adding
  structured output needs headroom or summaries start coming back empty.
- **Write `tag_source='llm'`.**
- **Demote the keyword tagger.** `score_and_tag` becomes the fallback used
  only when LLM output will not parse, not the primary path. Do not delete it
  — Phase 4 depends on it still existing until `score` has a replacement.

**Why second:** it is the actual fix, and Phase 3 cannot be calibrated until
it has produced labels.

**Done when:** newly summarised articles carry `tag_source='llm'`; topic
assignment is visibly sane on a hand-checked sample of ~20; no regression in
summary quality or empty-content rate.

---

## Phase 3 — Embedding backfill for the existing corpus

**Files:** `pipeline/embed.py`, new backfill script under `scripts/`

Phase 2 only ever touches newly summarised rows, so without this the 4,298
articles already in the database stay untagged forever. Re-summarising them
through Phase 2 is not an option: at the 13s/call throttle that is ~15 hours
of Actions time.

Instead, zero-shot topic assignment using the summary embeddings
`pipeline/embed.py` already computes for clustering — write one prototype
sentence per theme, embed it, cosine-match the article summary, threshold.
No LLM, no rate limit, whole backlog in minutes.

**Calibrate the threshold against Phase 2's output.** By the time this runs,
some articles will carry `tag_source='llm'`. Choose the cosine cut-off that
best *reproduces those LLM labels* on the overlap, rather than guessing a
number. This empirical calibration is the entire reason this phase comes
after Phase 2 rather than before it.

Backfill only rows where `tag_source` is NULL or `'keyword'`; write
`tag_source='embedding'`. Never overwrite an `'llm'` row.

**Done when:** the untagged share drops from 86% to something the topic
filters can actually work with; the chosen threshold's agreement rate with
LLM labels is measured and recorded in the script's docstring.

---

## Phase 4 — Replace `score` for cluster lead selection

**Files:** `pipeline/cluster.py`, `pipeline/db.py`, `tests/test_cluster.py`

`score` is not only a tagging artifact. `pipeline/cluster.py:196-197` uses it
to pick which article *leads* each carousel, and the `ORDER BY` clauses at
lines 283 and 328 depend on it. With 86% of articles at score 0 that ranking
is already close to meaningless — ties fall through to `fetched ASC`, so in
practice the carousel lead is "whichever copy of the story we downloaded
first."

Retiring keyword scoring therefore forces a replacement, and it is an upgrade
rather than cleanup: rank cluster members by **distance to the cluster
centroid**, so the most representative article leads. Embeddings are already
present from Phase 3's work.

Keep the `score` column (`pipeline/export.py` publishes it in the master CSV,
a de facto public API) but stop treating it as a ranking signal.

**Why last:** only becomes necessary once keywords retire, and it reuses the
embedding work from Phase 3.

**Done when:** carousel leads are chosen by centroid distance; `test_cluster.py`
covers the new ordering; no remaining code path treats `score` as meaningful.
