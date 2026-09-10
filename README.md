**Beyond Food Security — Food System Resilience Agent**

This project is being developed for the **Women in Data Datathon 2026: What’s Cooking? From Farm to Fork**.

The goal is to identify countries that may appear food-secure today but have hidden structural vulnerabilities caused by:

* low crop diversity
* high import dependency
* concentrated food suppliers
* unstable production
* food affordability and access gaps

The project introduces the concept of a **Latent Vulnerability Gap**, comparing:

**Current Food Security**
vs.
**Structural Food System Resilience**

The system will use FAOSTAT production, trade, food balance, food security, and healthy-diet affordability datasets to:

**Detect → Explain → Stress-test → Recommend**

Example scenario:

> *What happens if Australian wheat production decreases by 15%?*

The system analyses trade dependencies, supplier concentration, domestic production capacity, and existing food-security conditions to identify countries with the greatest exposure.

**Planned stack:** Python, Pandas, NumPy, GeoPandas, NetworkX, Plotly, FastAPI/Streamlit, FAOSTAT data, and Claude for evidence-grounded AI analysis.

**Research focus:**
*Which countries appear food-secure today but contain production and trade dependencies that could make them vulnerable to future food-system shocks?*

---

## Status

### Phase 1 — FAOSTAT API exploration & ingestion foundation ✅

Built and verified live against FAO's authenticated FAOSTAT API
(`https://faostatservices.fao.org/api/v1`):

* **`FAOSTATClient`** (`src/api/client.py`) — authentication, discovery
  (`get_groups_and_domains`, `get_domain_metadata`,
  `get_areas`/`get_items`/`get_elements`/`get_years`/`get_flags`,
  `get_dimension` for domain-specific dimensions), data retrieval
  (`get_data`, `get_data_chunked`), retries with exponential backoff +
  jitter (`tenacity`), `Retry-After` handling on 429, one automatic
  re-authentication + retry on 401/403, request chunking for large code
  lists (`src/api/pagination.py`), structured logging, and request
  provenance (`pipeline_run_id`, per-request IDs, per-attempt records in
  `client.request_log`).
* **Authentication** (`src/api/auth.py`) — the FAOSTAT gateway requires a
  bearer token on every request, including discovery endpoints, and there
  is no login endpoint on the gateway itself: tokens are issued directly
  by an AWS Cognito user pool (`USER_PASSWORD_AUTH`), confirmed live by
  decoding the `iss`/`client_id` claims of a token minted through the FAO
  developer portal. `FAOSTATAuthenticator` uses a provided
  `FAOSTAT_ACCESS_TOKEN` while it's still valid, otherwise logs in with
  `FAOSTAT_USERNAME`/`FAOSTAT_PASSWORD` and refreshes automatically before
  the ~60-minute expiry.
* **Response validation** (`src/validation/schemas.py`) — Pydantic models
  for groups/domains, dimensions, and data observations; invalid rows are
  reported (`ValidationResult.errors`), never silently dropped.
* **Credentials** load only from the environment (`FAOSTAT_USERNAME` /
  `FAOSTAT_PASSWORD`, or a manual `FAOSTAT_ACCESS_TOKEN`) via
  `pydantic-settings` — see `.env.example`.
* **`notebooks/01_dataset_exploration.ipynb`** (built + executed by
  `notebooks/build_notebook.py`) — run live end-to-end against all 9
  target domains (`QCL`, `TM`, `FBS`, `FS`, `CAHD`, `RFM`, `CP`, `GT`,
  `RL`): years vs. the requested 2014–2024 window, area/item/element
  counts, units, a bounded data sample per domain with its missingness and
  validation-error rate, and one example observation. Summary exported to
  `notebooks/reports/phase1_domain_coverage.csv`.
* **34 tests passing at the end of Phase 1** (`uv run pytest`), all HTTP
  mocked via `respx` — no test depends on live credentials or network.

**Confirmed live (not assumed) API quirks**, all handled explicitly rather
than guessed at:

* FAOSTAT dimension *codes* are not uniform across domains: most domains
  expose a plain `area` dimension, but `TM`/`RFM` split it into
  `reporterarea`/`partnerarea`, and `FS`'s year dimension is `year3`, not
  `year`. `FAOSTATClient.get_domain_metadata` discovers the real codes per
  domain (matching on the dimension's `code`, not its `id` — several
  dimensions share an `id` with a "group" variant, e.g. QCL's `area` and
  `areagroup` both have `id == "area"`) and fails fast naming the actual
  available codes rather than guessing.
* The *column names* for those dimensions' values differ too: `area`
  values come back keyed `"Country Code"`/`"Country"`, but `reporterarea`
  values come back keyed `"Reporter Country Code"`/`"Reporter Countries"`.
  The code column is reliably the first key in each object, which the
  client's callers use instead of assuming a column name.
* An `element` filter on `QCL` reproducibly returns **zero rows**, even
  when the same query without it returns matching data. `get_data`
  detects this, re-fetches without the filter, and applies it
  client-side, logging that it did so.
* `FS`'s year dimension mixes real years with 3-year-average pseudo-years
  encoded as an 8-digit start+end concatenation (e.g. `"20142016"` for
  2014–2016) in the same field as plain 4-digit years — both are
  all-digit, so they can't be told apart by `str.isdigit()` alone.
* `FS` also reports some values as censored strings (e.g. `"<0.1"`) —
  a real, meaningful value, but not a parseable number; `DataObservation`
  correctly rejects these as reported validation errors rather than
  silently coercing or dropping them.

Two genuine (non-error) coverage gaps found for the 2014–2024 window: `FBS`
and `GT` don't yet have 2024 data published; `CAHD` only goes back to 2017.

Run it yourself: `uv sync --extra dev`, add real credentials to `.env` (see
`.env.example`), then `uv run pytest` and
`uv run python notebooks/build_notebook.py`.

Not built in Phase 1 (by design): Bronze/Silver/Gold tables, feature
engineering, scoring, the dashboard, and the AI analyst.

### Phase 2 — Bronze → Silver ELT over all 9 FAOSTAT domains ✅

Run live end-to-end against the authenticated API. `PHASE2.md` is the
standing reference for *how* the data is treated; the *measured* results of
the current run live in `reports/phase2_data_quality.md` and
`data_quality/*.csv`.

* **Layered ELT** (`src/elt/`) — discovery-bounded extraction → `bronze_*`
  (raw API values for the selected project scope, with `retrieved_at` /
  `pipeline_run_id` / `source_domain` provenance) → `silver_*` (country
  harmonisation, aggregate-area exclusion, duplicate resolution, unit
  conversion, FAOSTAT-flag handling, 3-year averages keyed to their end
  year) → indexes and cross-domain views. SQLite via SQLAlchemy Core
  (`src/database/`).
* **Scope is configuration, not code** (`config/variables.yaml`,
  `config/harmonisation.yaml`) — 2014–2024 for every domain, all available
  countries, and only the variables `DATA.md` requires. Element *names* are
  resolved to FAOSTAT codes by live discovery, never hard-coded, and are
  disambiguated by unit where a name is ambiguous (`Production|t`, since
  QCL's "Production" is both crop tonnage and a livestock head count).
* **QCL is crops-only** — the 162 leaf items of FAOSTAT's own `QC` itemgroup
  (`config/qcl_crop_items.yaml`, regenerated live each run); live animals and
  livestock primary/processed are excluded by construction, and a **hard**
  validation check fails the run if a non-crop item ever reaches
  `silver_production`.
* **TM is the full bilateral matrix** — all reporters × all partners, bounded
  only by the food/crop commodity subset. **RFM is kept separate**: fertilizer
  items only, its own `silver_fertilizer_trade` table, wired into no core
  calculation.
* **Per-domain isolation** — every domain gets its own `etl_runs` row and its
  own status. A core-domain failure blocks Phase 2; a supporting-domain
  problem is logged and reported but does not.
* **Resumable** — a full run takes hours, so the domain loop is selectable:
  `--domains`, `--reuse-bronze` (re-run a Silver transform against Bronze
  already fetched, with no API data pull), `--resume` (record under the
  existing run id), `--domains none` (rebuild reports only). See `PHASE2.md`
  §11.

Measured on the current run (`etl_runs`):

| Domain | Priority | Status | Bronze rows | Silver rows | Items requested → retrieved |
|---|---|---|---|---|---|
| QCL | core | partial | 432,189 | 309,946 | 162 → 161 |
| TM | core | partial | 6,123,614 | 5,931,780 | 162 → 154 |
| FBS | core | success | 2,424,763 | 1,953,306 | all → 122 |
| FS | core | success | 45,203 | 22,924 | 13 → 13 |
| CAHD | core | success | 5,702 | 5,004 | 8 → 8 |
| RFM | supporting | success | 1,721,174 | 1,676,964 | 25 → 25 |
| CP | supporting | success | 53,228 | 52,856 | 2 → 2 |
| GT | supporting | partial | 582,911 | 487,913 | 45 → 44 |
| RL | supporting | success | 103,076 | 88,595 | 45 → 45 |

**≈11.5M Bronze rows → ≈10.5M Silver rows**, zero validation violations
(hard or soft), **110 tests passing**.

`partial` means something requested did not come back, and is deliberately
not rounded up to `success`; every case is itemised in
`data_quality/variable_scope.csv`. All ten are confirmed **source** gaps,
each checked unfiltered before being accepted: QCL item 839
(balata/gutta-percha gums) has no observation after 1990, GT item 6966
("Other sectors") returns nothing at all, and 8 of the 162 food commodities
are absent from TM's own item dimension.

**Confirmed live API quirk found in Phase 2** — FAOSTAT's `item=` filter is
wrong on some domains, and wrong *silently*. It compares the requested code
against a fixed-width internal key, so a code *longer* than that width
matches nothing while the same query with no item filter returns the data,
and a code *at* the width prefix-matches unrequested child items:

```text
CAHD item=70040   ->  0 rows          (unfiltered: 70040 is there)
CAHD item=7004    ->  70040, 70041
FS   item=210091  ->  0 rows          (unfiltered: 210091 is there)
FS   item=21009   ->  210091, 210091F, 210091M
CP   item=23013   ->  23013 + 230131, 230132   (variants nobody asked for)
```

This had quietly cost the project six FS indicators — including prevalence of
undernourishment and of food insecurity — and six CAHD items including the
**Cost of a Healthy Diet**, all of which the availability-vs-access analysis
depends on. Affected domains are marked `item_filter: client` in
`config/variables.yaml` and scoped client-side instead; the rest keep the
server-side filter, confirmed to work. Because the failure mode is silent,
the status rule is the backstop: retrieving fewer items than requested is
recorded `partial`, never `success`.

Reproduce it — `uv sync --extra dev`, credentials in `.env`, then:

```bash
uv run pytest
uv run python -m src.elt.pipeline          # API -> Bronze -> Silver -> reports
uv run python notebooks/build_notebook_02.py
```

Not built in Phase 2 (by design): Gold tables, feature engineering (crop
diversity, HHI, CV, import reliance, supplier concentration), CFSS / SRS /
LVG scoring, shock simulation, the dashboard, and the AI analyst.
