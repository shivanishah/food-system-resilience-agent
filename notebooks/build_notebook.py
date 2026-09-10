"""Builds and executes notebooks/01_dataset_exploration.ipynb.

Run with: uv run python notebooks/build_notebook.py

Regenerates the notebook from the cell source below (so the notebook's
logic is reviewable as a diff, not just as opaque .ipynb JSON) and then
executes it live against the FAOSTAT API, writing outputs back into the
.ipynb and a CSV summary to notebooks/reports/phase1_domain_coverage.csv.
"""

from __future__ import annotations

from pathlib import Path

import nbformat
from nbclient import NotebookClient
from nbformat.v4 import new_code_cell, new_markdown_cell, new_notebook

REPO_ROOT = Path(__file__).resolve().parent.parent
NOTEBOOK_PATH = REPO_ROOT / "notebooks" / "01_dataset_exploration.ipynb"

CELLS: list[tuple[str, str]] = [
    ("markdown", """\
# Phase 1 -- FAOSTAT Dataset Exploration

Live discovery + a bounded data sample for each of the 9 target FAOSTAT
domains (`QCL`, `TM`, `FBS`, `FS`, `CAHD`, `RFM`, `CP`, `GT`, `RL`), run
against the requested 2014-2024 analysis window.

Everything here goes through `FAOSTATClient` (`src/api/client.py`) -- no
raw HTTP calls, no hard-coded dimension IDs. This notebook only explores;
it does not build Bronze/Silver/Gold, features, scores, the dashboard, or
the AI analyst (Phase 1 scope, per `CLAUDE.md`).
"""),
    ("code", """\
import sys
from pathlib import Path

REPO_ROOT = Path.cwd().parent if Path.cwd().name == "notebooks" else Path.cwd()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import logging
import yaml
import pandas as pd

from src.config import Settings, configure_logging
from src.api.client import FAOSTATClient, FAOSTATAPIError

configure_logging(logging.WARNING)  # keep notebook output readable; details still in client.request_log
pd.set_option("display.max_colwidth", 80)

settings = Settings()
client = FAOSTATClient(settings)
client.authenticate()
print("Authenticated:", client.is_authenticated)
"""),
    ("code", """\
with open(REPO_ROOT / "config" / "datasets.yaml") as f:
    dataset_config = yaml.safe_load(f)

DOMAINS = [d["code"] for d in dataset_config["domains"]]
REQUESTED_START = dataset_config["analysis_period"]["start_year"]
REQUESTED_END = dataset_config["analysis_period"]["end_year"]
REQUESTED_YEARS = set(range(REQUESTED_START, REQUESTED_END + 1))

print("Domains:", DOMAINS)
print(f"Requested period: {REQUESTED_START}-{REQUESTED_END}")
"""),
    ("markdown", "## Group/domain catalogue\n\nConfirms every requested domain code is a real, current FAOSTAT domain."),
    ("code", """\
catalogue = client.get_groups_and_domains().data
catalogue_by_code = {row["domain_code"]: row for row in catalogue}
for code in DOMAINS:
    row = catalogue_by_code.get(code)
    status = f"{row['domain_name']!r} (current year: {row.get('year_current')})" if row else "NOT FOUND in catalogue"
    print(f"{code:5s} {status}")
"""),
    ("markdown", """\
## Per-domain discovery + bounded sample

For each domain: available years vs. the requested window, area/item/
element counts, units, a small live data sample (bounded so TM's bilateral
matrix doesn't pull millions of rows), missingness in that sample, and one
example observation. A domain-specific area dimension is required for TM
and RFM (`reporterarea`/`partnerarea` instead of `area`); everything else
uses `area`, and `FS` uses `year3` instead of `year` -- all discovered via
`get_domain_metadata`, not assumed.
"""),
    ("code", """\
SAMPLE_AREA_LIMIT = 20   # keep the live sample bounded, esp. for TM/RFM
SAMPLE_ITEM_LIMIT = 5    # ditto, for domains with large item lists (TM/RFM)


def explore_domain(client: FAOSTATClient, domain: str) -> dict:
    result: dict = {"domain": domain, "domain_name": catalogue_by_code.get(domain, {}).get("domain_name")}
    try:
        dims = client.get_domain_metadata(domain)
        dim_codes = {d["code"] for d in dims}
        is_bilateral = "reporterarea" in dim_codes

        years_data = client.get_years(domain).data
        year_key = next(iter(years_data[0]), None) if years_data else None
        # FAOSTAT keys 3-year-average indicators (e.g. FS) to an 8-digit
        # "20182020"-style code (start+end year concatenated), not a real
        # year -- keep those separate rather than letting them corrupt a
        # min/max range over genuine annual codes (see CLAUDE.md: FS year
        # codes are handled as year_type='3yr_avg' for exactly this reason).
        annual_years, period_codes = set(), []
        for row in years_data:
            code = str(row[year_key])
            if code.isdigit() and len(code) == 4:
                annual_years.add(int(code))
            elif code.isdigit():
                period_codes.append(code)
        result["n_available_years"] = len(annual_years)
        result["n_period_year_codes"] = len(period_codes)
        result["available_year_range"] = f"{min(annual_years)}-{max(annual_years)}" if annual_years else None
        covered = sorted(annual_years & REQUESTED_YEARS)
        missing = sorted(REQUESTED_YEARS - annual_years)
        result["requested_years_covered"] = len(covered)
        result["requested_years_missing"] = missing

        areas = client.get_areas(domain).data
        result["n_areas"] = len(areas)
        # The area code/name columns are named differently per dimension
        # (e.g. "Country Code" for `area`, "Reporter Country Code" for
        # `reporterarea`) -- but FAOSTAT consistently puts the code column
        # first in every dimension-value object, so read it positionally
        # instead of assuming a column name.
        area_code_key = next(iter(areas[0]), None) if areas else None

        items = client.get_items(domain).data
        result["n_items"] = len(items)
        item_code_key = next(iter(items[0]), None) if items else None

        elements_result = client.get_elements(domain).data
        result["n_elements"] = len(elements_result)
        result["elements_sample"] = ", ".join(e.get("Element", "") for e in elements_result[:6])
        units = sorted({e.get("Unit") for e in elements_result if e.get("Unit")})
        result["units"] = ", ".join(units) if units else None

        # Bounded live data sample for this domain.
        sample_year = max(covered) if covered else (max(annual_years) if annual_years else None)
        area_codes = [a[area_code_key] for a in areas[:SAMPLE_AREA_LIMIT]] if area_code_key else None
        item_codes = [i[item_code_key] for i in items[:SAMPLE_ITEM_LIMIT]] if is_bilateral and item_code_key else None

        if sample_year is not None:
            filters = {"year": sample_year}
            if is_bilateral:
                if area_codes:
                    filters["reporterarea"] = area_codes
                if item_codes:
                    filters["item"] = item_codes
            elif area_codes:
                filters["area"] = area_codes
            data_result = client.get_data(domain, **filters)
            rows = [row.model_dump(by_alias=True) for row in data_result.rows]
            result["sample_validation_errors"] = data_result.validation.n_errors
        else:
            rows = []
            result["sample_validation_errors"] = 0

        result["sample_rows"] = len(rows)
        if rows:
            missing_values = sum(1 for r in rows if r.get("Value") is None)
            result["sample_missing_value_pct"] = round(100 * missing_values / len(rows), 1)
            result["example_observation"] = {
                k: v for k, v in rows[0].items() if k in (
                    "Area", "Reporter Countries", "Partner Countries",
                    "Item", "Element", "Year", "Value", "Unit", "Flag Description",
                )
            }
        else:
            result["sample_missing_value_pct"] = None
            result["example_observation"] = None

    except FAOSTATAPIError as exc:
        result["error"] = str(exc)

    return result
"""),
    ("code", """\
results = [explore_domain(client, domain) for domain in DOMAINS]
for r in results:
    if "error" in r:
        print(f"{r['domain']:5s} ERROR: {r['error']}")
    else:
        print(
            f"{r['domain']:5s} years={r['n_available_years']:>3} "
            f"({r['requested_years_covered']}/{len(REQUESTED_YEARS)} of requested) "
            f"areas={r['n_areas']:>4} items={r['n_items']:>5} elements={r['n_elements']:>3} "
            f"sample_rows={r['sample_rows']:>4} missing%={r['sample_missing_value_pct']}"
        )
"""),
    ("markdown", "## Summary table"),
    ("code", """\
summary_df = pd.DataFrame(results)
column_order = [
    "domain", "domain_name", "n_available_years", "available_year_range", "n_period_year_codes",
    "requested_years_covered", "requested_years_missing",
    "n_areas", "n_items", "n_elements", "elements_sample", "units",
    "sample_rows", "sample_missing_value_pct", "sample_validation_errors",
    "example_observation", "error",
]
summary_df = summary_df.reindex(columns=[c for c in column_order if c in summary_df.columns])
summary_df
"""),
    ("markdown", "## Coverage gaps found (computed live, not assumed)"),
    ("code", """\
for r in results:
    if r.get("requested_years_missing"):
        print(f"{r['domain']}: missing requested years {r['requested_years_missing']} "
              f"(available: {r.get('available_year_range')})")
"""),
    ("markdown", """\
## Confirmed API quirks (found by exercising the live API, not hypothesised)

* **Dimension names are not uniform across domains.** Most domains expose a
  plain `area` dimension; `TM` and `RFM` split it into `reporterarea` /
  `partnerarea`; `FS` calls its year dimension `year3` instead of `year`.
  `FAOSTATClient.get_domain_metadata` discovers the real codes per domain.
* **An `element` filter on `QCL` reproducibly returns zero rows**, even
  when the same query without it returns matching data. `FAOSTATClient.
  get_data` detects this case, re-fetches without the filter, and applies
  it client-side -- see the warning this logs when it triggers.
* **Area/name columns are not named consistently across dimensions either.**
  Plain `area` values come back as `"Country Code"`/`"Country"`, but
  `reporterarea` values come back as `"Reporter Country Code"`/
  `"Reporter Countries"` (and presumably `partnerarea` similarly for
  partners) -- confirmed live for TM. The code column is reliably the
  *first* key in each dimension-value object, which this notebook uses
  instead of assuming a column name.
* **FS mixes real years with 3-year-average pseudo-years in one dimension.**
  Its `year3` codes include both plain years (`"2022"`) and rolling
  3-year averages encoded as a start+end concatenation (`"20142016"` for
  2014-2016) -- both are all-digit, so treating every code as a literal
  year corrupts a min/max range. This notebook keeps them in a separate
  `n_period_year_codes` count instead.
* **FS also reports some values as censored strings, e.g. `"<0.1"`**,
  which is a real, meaningful value (not missing) but not a parseable
  number -- `DataObservation` correctly rejects these as validation
  errors (reported via `sample_validation_errors`) rather than silently
  coercing or dropping them.
"""),
    ("code", "print(f\"pipeline_run_id={client.pipeline_run_id}  requests_made={len(client.request_log)}\")"),
    ("code", """\
reports_dir = REPO_ROOT / "notebooks" / "reports"
reports_dir.mkdir(parents=True, exist_ok=True)
out_path = reports_dir / "phase1_domain_coverage.csv"
summary_df.to_csv(out_path, index=False)
print(f"Wrote {out_path}")
"""),
    ("code", "client.close()"),
]


def build() -> nbformat.NotebookNode:
    nb = new_notebook()
    for cell_type, source in CELLS:
        if cell_type == "markdown":
            nb.cells.append(new_markdown_cell(source))
        else:
            nb.cells.append(new_code_cell(source))
    return nb


def main() -> None:
    nb = build()
    NotebookClient(nb, timeout=300, kernel_name="python3").execute()
    NOTEBOOK_PATH.parent.mkdir(parents=True, exist_ok=True)
    nbformat.write(nb, NOTEBOOK_PATH)
    print(f"Wrote executed notebook to {NOTEBOOK_PATH}")


if __name__ == "__main__":
    main()
