# INDICATORS_BRIEF.md

Implementation brief for a new **Indicators** section of the Economic News Aggregator.
Repo: `C:\Users\oisin\OneDrive\Desktop\Economic News Aggregator` → published at
`https://oisinbogue.github.io/Economic-News-Aggregator/`

---

## 1. Goal

Add a third top-level section, **Indicators**, sitting between Home and Archive in the
nav. It shows live macroeconomic indicators for 31 countries/blocs, browsable by country
and by topic, each series updated on its release schedule (or daily for market data).

The visual model is `economic-monitor.emergent.host`: a responsive grid of compact cards,
each with a headline value, a change badge, a sparkline, a source label, and the next
release date. Filter chips for country and topic sit above the grid, plus a search box.

**Critical constraint on scope:** the value-add of this section over any other dashboard
is that it lives beside a news corpus. Do not build the news cross-linking in v1, but do
not make it impossible either — see §5 on schema.

---

## 2. Hard constraints

- **Zero ongoing cost.** No paid data, no paid hosting, no servers.
- **Static output only.** GitHub Pages. Everything is precomputed at build time into JSON
  + HTML. No client-side calls to data providers at page load.
- **No API keys are currently held.** Design the v1 source layer so it requires none. If a
  key would meaningfully improve a source, put it behind an optional env var that degrades
  gracefully when absent, and list it in `.env.example`.
- **Must not destabilise the news pipeline.** Separate workflow, separate database,
  separate failure domain. A broken indicator fetch must never block a news run, and vice
  versa.
- **Must look native to the existing site.** Reuse `static/style.css` design tokens,
  `templates/base.html`, the existing fonts (Inter / JetBrains Mono / Source Serif 4), and
  the existing dark-mode mechanism. Do not introduce a CSS framework or a JS framework.
  Vanilla JS only, consistent with `static/app.js`.

---

## 3. Repo integration

Follow existing conventions exactly. Proposed layout:

```
config/
  indicators/
    countries.yaml        # the 31 countries/blocs, ISO codes, currency, calendar tz
    core.yaml             # the 16 harmonised core indicators (definitions)
    series/
      us.yaml             # per-country series → provider mappings + extras
      gb.yaml
      ie.yaml
      ...
indicators/               # new Python package, parallel to pipeline/
  __init__.py
  config.py               # loads + validates the YAML above (fail fast on bad config)
  db.py                   # schema + helpers for indicators.db
  providers/
    __init__.py           # Provider protocol: fetch(series_spec) -> list[Observation]
    dbnomics.py
    ecb.py                # ECB Data Portal SDMX
    eurostat.py           # Eurostat SDMX
    ons.py                # UK ONS
    boe.py                # BoE IADB
    cso.py                # Ireland CSO PxStat
    imf.py                # IMF SDMX (fallback for EM coverage)
    worldbank.py          # slow-moving structural series only
    stooq.py              # FX, equity indices, commodities (CSV, no key)
  fetch.py                # orchestrates providers, writes raw observations
  transform.py            # derived series + YoY/MoM/QoQ/annualised computation
  fx.py                   # currency conversion layer (see §7)
  calendar.py             # next-release scheduling
  build.py                # renders /indicators/ HTML + JSON
  export.py               # per-series JSON, CSV, and a manifest
templates/
  indicators_index.html
  indicators_country.html
  _indicator_card.html
static/
  indicators.js
  indicators.css          # only if it cannot live inside style.css cleanly
tests/
  test_indicators_config.py
  test_indicators_transform.py
  test_indicators_fx.py
  test_indicators_providers.py
.github/workflows/
  indicators.yml
```

`indicators.db` is a **separate SQLite file** from the news DB, snapshotted to GitHub
Releases using the same mechanism the news DB already uses (mirror the approach in
`.github/workflows/backup-restore-test.yml`).

Do not modify `pipeline/` except for the single nav change in `templates/base.html`.

---

## 4. Data sources

Priority order for every series:

1. **DBnomics** (`https://api.db.nomics.world/v22/`) — no key, no meaningful rate limit,
   and it mirrors FRED, Eurostat, OECD, IMF, BIS, World Bank and dozens of national
   statistics offices behind one consistent JSON API. **This is the primary backbone,
   including for US series** (FRED is available as a DBnomics provider, which is how we
   avoid needing a FRED key).
2. **Official national/supranational APIs** where DBnomics coverage is thin, stale, or
   lower-frequency than the primary release: ECB Data Portal, Eurostat SDMX, ONS API, BoE
   IADB, CSO PxStat, Destatis, e-Stat (Japan), KOSIS (Korea), INEGI (Mexico), IBGE/BCB
   (Brazil), INDEC/BCRA (Argentina), TÜİK/TCMB (Turkey), CBN (Nigeria), StatsSA, CAPMAS
   (Egypt), GUS (Poland), KSH/MNB (Hungary), SSB (Norway), SCB (Sweden), DST (Denmark),
   BFS/SNB (Switzerland), GSO (Vietnam), DOSM (Malaysia), BPS (Indonesia), GASTAT (Saudi),
   FCSC (UAE), MOSPI/RBI (India), Rosstat/CBR (Russia).
3. **Stooq** for daily market data (FX, equity indices, commodities) — plain CSV
   endpoints, no key.
4. **IMF / World Bank** as a last-resort fallback for countries where nothing better
   exists, accepting lower frequency and longer lag.

**Every series in config must declare its provider chain explicitly** (primary + optional
fallbacks). No silent provider guessing at runtime.

**Attribution is mandatory.** Each card and each series page shows the originating
statistical agency, not "DBnomics". Store both `provider` (how we fetched it) and
`source_agency` (who produced it). Respect ONS/ONS-family Open Government Licence
attribution requirements and equivalent terms elsewhere; put a licence/attribution note
in the Indicators footer.

---

## 5. Data model

`indicators.db`, SQLite, source of truth. Suggested schema — adjust names to match
`pipeline/db.py` conventions:

**`series`** (one row per country × indicator)
| column | notes |
|---|---|
| `series_id` | stable slug, e.g. `ie.cpi_headline`, `us.policy_rate` |
| `country` | ISO 3166-1 alpha-2, plus `EU` for the bloc |
| `indicator_key` | matches `config/indicators/core.yaml` key, or a country-extra key |
| `title` | display name |
| `topic` | one of: economy, prices, labour, rates, housing, confidence, government, trade, energy, markets |
| `tier` | `core` \| `extra` |
| `unit` | `percent`, `index`, `currency`, `ratio`, `count`, `percent_gdp`, `bps` |
| `unit_currency` | ISO 4217 where `unit = currency`, else null |
| `scale` | `units` \| `thousands` \| `millions` \| `billions` \| `trillions` |
| `frequency` | `D` \| `W` \| `M` \| `Q` \| `A` |
| `transform_default` | `yoy` \| `mom` \| `qoq` \| `level` \| `annualised` — what the card shows |
| `seasonal_adjustment` | `sa` \| `nsa` \| `unknown` |
| `direction_good` | `up` \| `down` \| `neutral` — drives colour semantics (see §9) |
| `provider` / `provider_series_id` / `fallback_providers` | JSON list for fallbacks |
| `source_agency` / `source_url` / `licence` | attribution |
| `release_schedule` | cron-ish or rule string; see §8 |
| `release_lag_days` | typical publication lag |
| `is_derived` | bool — computed rather than fetched |
| `derivation` | expression/spec when `is_derived` |
| `revisable` | bool — whether prints get revised |
| `notes` | free text, surfaced as a tooltip (e.g. Ireland GDP caveat) |
| `news_topic_tags` | JSON list mapping to the news taxonomy in `config/taxonomy.yaml` — **populate this now, use it later** |
| `active` | bool |

**`observations`**
`series_id`, `period_start`, `period_end`, `frequency`, `value`, `value_usd` (nullable),
`is_preliminary`, `fetched_at`. Primary key `(series_id, period_start)`.

**`observation_revisions`** — append-only log of `(series_id, period_start, old_value,
new_value, observed_at)`. Write to it whenever a refetch changes a historic value. Costs
almost nothing now; makes first-print-vs-current analysis possible later.

**`releases`** — `series_id`, `scheduled_at`, `actual_at`, `status`. Powers "Next: 18 Aug
· in 11 days".

**`fetch_log`** — per-run, per-series status, so an Indicators equivalent of the existing
Source Health page is trivial later.

### Reference period vs release date
Store and display both. The card must be able to show *"2.10% · Q1 2026"* alongside
*"Next: 1 Oct"*. Conflating these is the single most common way a dashboard misleads
people, and the reference site does exactly that.

---

## 6. The indicator set

### 6.1 Core 16 — every country must have these (or an explicit `unavailable` marker)

| # | key | default transform | topic | direction_good |
|---|---|---|---|---|
| 1 | `gdp_real` | yoy (qoq also stored) | economy | up |
| 2 | `cpi_headline` | yoy | prices | neutral |
| 3 | `cpi_core` | yoy | prices | neutral |
| 4 | `policy_rate` | level | rates | neutral |
| 5 | `unemployment_rate` | level | labour | down |
| 6 | `wages_nominal` | yoy | labour | up |
| 7 | `industrial_production` | yoy | economy | up |
| 8 | `retail_sales` | yoy | economy | up |
| 9 | `govt_bond_10y` | level | rates | neutral |
| 10 | `fx_vs_usd` | level | markets | neutral |
| 11 | `house_prices` | yoy | housing | neutral |
| 12 | `govt_debt_gdp` | level | government | down |
| 13 | `budget_balance_gdp` | level | government | up |
| 14 | `current_account_gdp` | level | government | neutral |
| 15 | `consumer_confidence` | level | confidence | up |
| 16 | `equity_index` | yoy (level also stored) | markets | up |

`direction_good = neutral` matters: inflation is not "good when falling" at −2%, and a
falling exchange rate is not straightforwardly bad. Where a series has a target band
(inflation, mostly), store `target_value` / `target_band` in config and colour relative to
the band rather than relative to zero.

### 6.2 Derived series — computed, never fetched

Implement these in `indicators/transform.py`. They are cheap and they are most of what
makes the section interesting.

- `real_wage_growth` = `wages_nominal` − `cpi_headline`
- `real_policy_rate` = `policy_rate` − `cpi_core` (fall back to headline)
- `misery_index` = `unemployment_rate` + `cpi_headline`
- `yield_spread_domestic` = 10y − 2y where a 2y exists
- `yield_spread_vs_benchmark` = 10y − Bund 10y for euro-area members; 10y − UST 10y elsewhere
- `sahm_rule` = 3m MA of unemployment − min(12m trailing 3m MA), where monthly unemployment exists
- `house_price_to_income` = house price index ÷ nominal wage index, rebased
- `fx_real_change` — nominal FX move adjusted for the inflation differential vs the US

Derived series live in the same `series` table with `is_derived = 1`, so the UI treats
them identically. Mark them clearly in the UI as computed, with the formula in the tooltip.

### 6.3 Country-specific extras

Cap at 8 per country. These are the ones that make each country page worth visiting.

- **Ireland** — **Modified Domestic Demand and GNI\*** (mandatory; GDP is distorted by
  multinational flows and must carry a prominent caveat note), monthly Exchequer returns,
  corporation tax receipts and top-10-payer concentration, housing completions, net
  migration, Live Register.
- **UK** — mortgage approvals, average new-mortgage rate, company insolvencies, S&P Global
  composite PMI, RPI vs CPIH divergence, gilt 30y.
- **US** — core PCE, initial jobless claims (weekly), JOLTS openings and quits rate, ISM
  manufacturing and services, 30-year fixed mortgage rate, 10y–2y spread, 5y5y breakeven,
  participation rate.
- **EU / euro area** — HICP flash, ECB deposit facility rate, bank lending survey, TTF gas,
  EU ETS carbon price, euro-area unemployment dispersion.
- **Germany** — Ifo business climate, ZEW expectations, factory orders, industrial gas and
  electricity price vs US, BTP/OAT-relevant Bund reference yield.
- **France / Italy / Spain** — OAT-Bund and BTP-Bund spreads (topical and underserved),
  INSEE/ISTAT/INE business confidence, tourism arrivals (Spain, Italy).
- **Japan** — Tankan large manufacturers DI, BoJ policy balance rate, JGB 10y and 30y, yen
  real effective exchange rate, wage negotiation (shuntō) outcome, food self-sufficiency.
- **Korea** — **20-day exports** (highest-value single series on the whole site: fastest
  read on global trade anywhere, published three times a month), semiconductor export
  value, BoK base rate, household debt to GDP.
- **India** — GST collections, e-way bill volumes, RBI repo rate, IIP, WPI vs CPI gap,
  monsoon/rainfall deviation, forex reserves.
- **China** *(not in your list but the EU/Korea/Germany pages will beg for it — flag as a
  candidate)*.
- **Brazil** — Selic, IPCA-15, BCB Focus survey median forecasts, primary fiscal balance,
  commodity export price index.
- **Argentina** — **official vs parallel (blue) dollar gap**, EMBI country risk, monthly
  CPI (log scale mandatory), BCRA net reserves, REM survey forecasts.
- **Turkey** — **ENAG vs TÜİK inflation gap** (independent vs official; flag the dispute
  explicitly in the note field), TCMB policy rate, net FX reserves excluding swaps, 5y CDS.
- **Russia** — **Urals–Brent discount**, oil and gas budget revenues, CBR key rate, RUB/USD,
  with an explicit data-reliability caveat on official statistics since 2022.
- **Nigeria / Egypt** — parallel FX rate and the official gap, FX reserves, remittance
  inflows, food inflation (separately from headline — it dominates the basket), fuel
  subsidy/pump price.
- **South Africa** — SARB repo rate, load-shedding stages/hours (Eskom), unemployment
  expanded definition, current account deficit.
- **Saudi / UAE** — **fiscal breakeven oil price**, non-oil PMI, OPEC+ production quota vs
  actual, sovereign wealth fund AUM, Dubai residential price index.
- **Canada / Mexico** — BoC/Banxico policy rate, USMCA-relevant trade balance with the US,
  remittances (Mexico), housing affordability (Canada).
- **Poland / Hungary** — NBP/MNB policy rate, EU funds disbursement status, PLN/HUF vs EUR,
  core inflation, real wage growth.
- **Denmark** — **Novo Nordisk's share of GDP growth and exports** (a genuinely important
  and rarely-charted concentration risk), pegged-krone deviation.
- **Norway** — Government Pension Fund Global market value, mainland GDP (not total),
  petroleum revenue.
- **Sweden** — floating-rate mortgage share, house prices, Riksbank rate.
- **Switzerland** — SNB policy rate, SNB balance sheet as % of GDP, CHF REER.
- **Indonesia / Malaysia / Vietnam** — FDI disbursement, exports (electronics share),
  policy rate, palm oil / commodity export prices, tourism arrivals.

Where an extra genuinely cannot be sourced for free, mark it `active: false` in config with
a `blocked_reason` rather than deleting it, so it can be revisited.

### 6.4 Cross-country comparison series (later phase, schema now)

`big_mac_index`, `energy_price_relative`, `housing_completions_per_1000` — all naturally
cross-country. Model them as normal series with a `comparable: true` flag.

---

## 7. Currency handling

Requirement: **both local currency and USD**.

- Daily FX rates fetched into an `fx_rates` table (`date`, `base`, `quote`, `rate`),
  sourced from ECB reference rates (free, no key, EUR-based — cross via EUR) with Stooq as
  a fallback for non-ECB pairs.
- For any series with `unit = currency`, compute and store `value_usd` at fetch time using
  the FX rate **for the observation's reference period**, not today's rate. Converting a
  2019 Argentine figure at today's peso rate produces nonsense.
- Store both; let the UI toggle. A single site-wide toggle (LOCAL / USD) persisted in
  localStorage, matching how the existing dark-mode toggle is implemented.
- Non-currency series (percentages, indices) are unaffected by the toggle — grey the
  toggle out or leave it inert on those cards, do not show a converted percentage.
- Label conversions explicitly: *"$411k (converted at period average, 1 USD = X)"*.

---

## 8. Update cadence

Choose the specific schedule yourself, but satisfy these requirements:

- **Independent of the news workflow.** New file `.github/workflows/indicators.yml`.
- **Tiered by frequency.** Daily market data (FX, yields, equities, commodities) does not
  need the same treatment as a quarterly GDP print. A cheap high-frequency run plus a
  fuller daily or twice-daily run is the obvious shape.
- **Release-aware.** Use the `releases` table so the pipeline knows to poll more
  aggressively around a scheduled release and back off otherwise. A series whose next
  release is six weeks away should not be refetched hourly.
- **Idempotent and incremental.** Only fetch what has plausibly changed. Full backfill is a
  separate, manually-triggered `workflow_dispatch` job.
- **Rate-limit safe.** Concurrency caps and polite delays per provider; exponential backoff
  on 429/5xx; never let a single provider's failure abort the run.
- **Never fails the build.** If a series fetch fails, serve the last good value and mark the
  card stale. Log it to `fetch_log`. Only a config-validation error should hard-fail.

Populate the release calendar from provider metadata where available; otherwise encode
release rules in config (e.g. "second Wednesday of the month, 07:00 Europe/London") and
compute forward. Store release times in UTC with an IANA timezone attached; render in the
visitor's local time.

---

## 9. Site output

### Routes
- `/indicators/` — landing page. All countries, all topics, filter chips + search box.
  Defaults to a sensible subset (do not render 700 cards on first paint — paginate or
  lazy-render).
- `/indicators/<country>/` — one country, all its indicators grouped by topic.
- `/indicators/<country>/<indicator>/` — single-series page: full history chart, table,
  CSV download, methodology, source attribution, revision history.
- `/indicators/data/<series_id>.json` — per-series full history.
- `/indicators/data/manifest.json` — lightweight index: every series with its latest value,
  change, sparkline points (downsampled to ~40), and next release. **This is what the grid
  loads.** Full history JSON loads only on the series page.

### Card contents
Country flag/code · title · topic · source agency · headline value · change badge ·
**reference period** · sparkline · next release. Everything a user needs to not
misinterpret the number.

### Colour semantics
Drive colour from `direction_good`, not from the sign of the change. A rising unemployment
rate is red; a rising GDP figure is green; a moving inflation rate is coloured relative to
the target band, and grey where no band exists. Never colour a `neutral` series green or
red on sign alone. Use the existing palette in `style.css`; ensure the red/green pair is
distinguishable for the most common colour-vision deficiencies, and never rely on colour
alone — the arrow glyph carries the same information.

### Behaviour
- Filter chips: country (grouped by region), topic, and a `core`/`all` toggle.
- Search filters cards client-side by title, country, and source.
- Staleness: if `now − last_observation > expected_interval × 1.5`, show a subtle stale
  marker on the card.
- Dark mode via the existing mechanism, no separate implementation.
- Mobile-first grid. The reference site's four-across desktop grid should collapse
  cleanly to one or two columns.
- Every card and series page has a stable permalink.

### Accessibility & performance
Semantic HTML, keyboard-navigable filters, `prefers-reduced-motion` respected, sparklines
as inline SVG with an `aria-label` describing the trend and a `<title>`. Manifest gzipped
and kept small; measure it and keep the initial payload under a few hundred KB.

---

## 10. Phasing

Ship each phase working end-to-end before starting the next.

**Phase 1 — foundation.** Config schema, DB schema, DBnomics provider, transform engine,
FX layer, tests. Five countries only: **US, UK, Ireland, EU, Germany**. Core 16 plus the
derived series. No extras yet. Prove the schema survives contact with five very different
data environments.

**Phase 2 — site.** Templates, landing page, country pages, series pages, JSON/CSV export,
nav integration, dark mode, mobile. Still five countries.

**Phase 3 — automation.** `indicators.yml` workflow, release calendar, staleness handling,
retry/backoff, DB snapshot to GitHub Releases, `fetch_log`.

**Phase 4 — extras.** Country-specific extras for the five Phase-1 countries, including the
Ireland MDD/GNI\* work and the US high-frequency series.

**Phase 5+ — expansion.** Remaining 26 countries in batches, grouped by data environment
rather than alphabetically: euro-area members together (shared Eurostat/ECB plumbing),
then other advanced economies, then emerging markets (which will need the most
country-specific provider work), then the FX-distorted set (Argentina, Turkey, Nigeria,
Egypt, Russia) last, since they need parallel-rate handling and data-quality caveats.

**Adding a country in Phase 5+ must require config changes only.** If it requires touching
`indicators/` Python, the abstraction in Phase 1 was wrong — fix it before continuing.

---

## 11. Testing

- Config validation: every series has a provider, unit, frequency, transform, and
  `direction_good`; every `core` indicator exists for every active country or is explicitly
  marked unavailable with a reason.
- Transform unit tests with hand-checked fixtures: YoY/MoM/QoQ/annualised across monthly,
  quarterly, and annual frequencies, including partial-year and leap-year edges.
- FX conversion tests: period-matched rates, cross-rates via EUR, missing-rate handling.
- Provider tests against recorded fixtures (VCR-style), never live network in CI.
- A smoke test that builds the whole site from a fixture DB and asserts the manifest is
  well-formed and every card has a reference period.

---

## 12. Explicit non-goals for v1

Not in scope, but do not architect them out:

- News cross-linking (schema hook: `news_topic_tags`)
- LLM-generated commentary (Cerebras budget is already the binding constraint on the news
  pipeline — keep it out entirely)
- Consensus forecasts and surprise indices
- Alerts, email, or watchlists
- Multi-country comparison charts
- Revision analytics UI (the revision log is being written from day one; the UI is later)

---

## 13. Deliverables for this phase

1. `config/indicators/` YAML files for the Phase-1 countries, fully populated.
2. The `indicators/` package with DBnomics provider, transforms, FX, and DB layer.
3. Tests per §11, passing.
4. A `workflow_dispatch`-only backfill job (the scheduled workflow comes in Phase 3).
5. A short `INDICATORS.md` documenting the config schema, so adding a country later is a
   config-editing task and not an archaeology task.

Before writing code: read `PROJECT_BRIEF.md`, `FEATURE_BRIEF.md`, `TAGGING_SPEC.md`,
`pipeline/db.py`, `pipeline/build.py`, `pipeline/config.py`, `templates/base.html`, and
`static/style.css`, and match their conventions. Then propose the config schema and the DB
schema for review **before** implementing the providers.
