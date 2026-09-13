# Beyond Food Security — Food System Resilience Agent

**Women in Data Datathon 2026 — *What's Cooking? From Farm to Fork***

## What this project is

Most food-security measures tell you how a country is doing **today**. This
project asks a different question:

> Which countries look food-secure right now, but have structural weaknesses
> that could make them vulnerable to a future shock?

A country can have plenty of food available and still be fragile — because it
grows very few different crops, buys most of its food from one or two
suppliers, or has production that swings wildly from year to year. Those
weaknesses stay invisible until something goes wrong.

To measure this, the project compares two things:

| | What it measures |
|---|---|
| **CFSS** — Current Food Security Status | How food-secure a country looks today |
| **SRS** — Structural Resilience Score | How robust its production and trade structure actually is |

The gap between them is the headline metric:

```
Latent Vulnerability Gap (LVG) = CFSS − SRS
```

A **high** LVG means a country looks fine today but rests on fragile
foundations.

> **Important:** this measures *structural exposure*, not prediction. A high
> LVG does not mean a country will experience hunger or shortage.

### Questions it answers

- Which countries appear food-secure but are structurally vulnerable?
- Where does plenty of available food coexist with people unable to afford a
  healthy diet?
- Does growing a wider variety of crops actually make production more stable?
- Who depends on a small number of suppliers for their food imports?
- What happens to importers if a major exporter's harvest fails?

## Tech stack

**Python 3.11+**, managed with [`uv`](https://docs.astral.sh/uv/).

**Installed and in use:**

| Area | Tools |
|---|---|
| Data source | FAOSTAT REST API (authenticated) |
| API client | `httpx`, `tenacity` (retries), `pydantic` (validation) |
| Config & secrets | `pyyaml`, `pydantic-settings`, `python-dotenv` |
| Storage | SQLite via `SQLAlchemy` Core |
| Data handling | `pandas` |
| Notebooks | `jupyter`, `nbclient` |
| Testing | `pytest`, `respx` (mocked HTTP — no test needs live credentials) |

**Planned for later phases:** `numpy`, `scipy` and `statsmodels` for the
statistics, `plotly`, `geopandas` and `networkx` for charts and trade-network
maps, and `streamlit` for the dashboard.

## How the data is organised

Data moves through three layers, so that raw source values are never lost:

```
FAOSTAT API  →  BRONZE  →  SILVER  →  GOLD (Phase 3)
                 raw       cleaned     analysis features
```

- **Bronze** — exactly what FAO returned, untouched, with a record of when and
  by which run it arrived.
- **Silver** — cleaned and standardised: regional aggregates like "World" and
  "Africa" removed, duplicates resolved, units validated, country codes
  harmonised. **This is the layer to analyse.**
- **Gold** — engineered features (crop diversity, supplier concentration, the
  scores above). Not built yet.

Nine FAOSTAT datasets are ingested for **2014–2024**, all countries:

| Code | Contents | Role |
|---|---|---|
| `QCL` | Crop production, area, yield | core |
| `TM` | Bilateral food trade — who sells to whom | core |
| `FBS` | Food balance sheets | core |
| `FS` | Food-security indicators | core |
| `CAHD` | Cost & affordability of a healthy diet | core |
| `RFM` | Fertilizer trade | supporting |
| `CP` | Food price indices | supporting |
| `GT` | Agrifood emissions | supporting |
| `RL` | Land use | supporting |

## Project structure

```
├── src/                      Reusable code — all logic lives here
│   ├── api/                  FAOSTAT client: auth, endpoints, retries, paging
│   ├── elt/                  The pipeline: extract → bronze → silver → reports
│   ├── database/             Table definitions and database connection
│   └── validation/           Data quality checks on API responses and tables
│
├── config/                   What to fetch, and how to clean it
│   ├── variables.yaml        Which datasets, items, indicators and years
│   ├── harmonisation.yaml    Cleaning rules: aggregates, units, duplicates
│   └── qcl_crop_items.yaml   The crop list (auto-generated from FAO)
│
├── notebooks/                Exploration and review (not pipeline logic)
│   ├── 01_dataset_exploration.ipynb
│   └── 02_data_quality.ipynb
│
├── data/                     SQLite database (gitignored, ~5 GB)
├── data_quality/             Generated quality evidence (CSV)
├── reports/                  Generated data-quality report (Markdown)
├── tests/                    Test suite — all HTTP mocked
├── script/                   Ad-hoc check scripts
│
├── DATA.md                   Which FAOSTAT variables the project needs
├── DESIGN.md                 Analytical design and methodology
├── DB.md                     Database schema notes
├── PHASE2.md                 How the pipeline treats the data (full detail)
└── CLAUDE.md                 Project rules and scope
```

> **Rule of thumb:** reusable logic belongs in `src/`. Notebooks are for
> looking at things, never for transforming them.

## Getting started

### 1. Prerequisites

- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/)
- A **free FAOSTAT API account** from the
  [FAO developer portal](https://www.fao.org/faostat/en/#developer-portal)

### 2. Install

```bash
git clone <repo-url>
cd food-system-resilience-agent
uv sync --extra dev
```

### 3. Add your credentials

```bash
cp .env.example .env
```

Then edit `.env` and fill in **either** your login **or** a token:

```bash
FAOSTAT_USERNAME=your-username
FAOSTAT_PASSWORD=your-password
# or, instead:
FAOSTAT_ACCESS_TOKEN=your-token
```

Leave the other settings blank to use the defaults. `.env` is gitignored —
never commit credentials.

### 4. Check everything works

```bash
uv run pytest
```

All tests use mocked HTTP, so this works without credentials or internet.

### 5. Build the database

```bash
uv run python -m src.elt.pipeline
```

This downloads all nine datasets and builds the database at
`data/food_system.db`.

> ⏱️ **A full run takes several hours** — the bilateral trade dataset alone is
> over 6 million rows. It logs progress as it goes.

### 6. Review the results

```bash
uv run python notebooks/build_notebook_02.py
```

Then open `notebooks/02_data_quality.ipynb`, or read
`reports/phase2_data_quality.md`.

## Everyday commands

The pipeline is resumable, so you never have to redo hours of downloading:

```bash
# Re-run one dataset
uv run python -m src.elt.pipeline --resume --domains RFM

# Rebuild a dataset's cleaned tables from data already downloaded
uv run python -m src.elt.pipeline --resume --domains TM --reuse-bronze TM

# Rebuild only the reports (a few minutes)
uv run python -m src.elt.pipeline --resume --domains none
```

⚠️ **Only run one at a time.** SQLite allows a single writer — starting a
second run, or leaving a database browser open, will cause
`database is locked`. Check first with:

```bash
pgrep -fl "src.elt.pipeline"     # silence means it's safe to start
```

## Querying the data

```bash
sqlite3 data/food_system.db
```

```sql
-- Health check: one row per dataset
SELECT domain, status, bronze_rows, silver_rows FROM etl_runs;

-- Wheat exports from Australia (always use LIMIT — these tables are large)
SELECT * FROM silver_trade
WHERE reporter_area_code = '10' AND item_code = '15'
LIMIT 100;
```

Main tables to analyse: `silver_production`, `silver_trade`,
`silver_food_balance`, `silver_food_security`, `silver_affordability`, with
`silver_country` and `silver_commodity` as lookups.

## Status

| Phase | State |
|---|---|
| 1 — FAOSTAT API client | ✅ Complete |
| 2 — Bronze → Silver pipeline | ✅ Complete — all 9 datasets, ~10.5M cleaned rows, 0 validation failures |
| 3 — Feature engineering | ⬜ Next |
| 4 — Scoring (CFSS / SRS / LVG) | ⬜ |
| 5 — Dashboard & shock simulator | ⬜ |

See `PHASE2.md` for exactly how the data is cleaned, and
`reports/phase2_data_quality.md` for the current run's results.

## Ground rules for the analysis

These hold throughout the project:

- association ≠ causation
- exposure ≠ predicted shortage
- structural vulnerability ≠ current food insecurity
- food availability ≠ food access
- relying on imports is not automatically bad

Unexpected or null findings are kept and reported, never tuned away.
