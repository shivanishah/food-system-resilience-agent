"""Builds and executes notebooks/02_data_quality.ipynb.

Run with: uv run python notebooks/build_notebook_02.py

Regenerates the notebook from the cell source below and executes it against
the already-built `data/food_system.db` + `data_quality/*.csv` +
`reports/phase2_data_quality.md` (produced by `uv run python -m
src.elt.pipeline`). This notebook does not call the FAOSTAT API or transform
anything -- it only reviews Phase 2's output.
"""

from __future__ import annotations

from pathlib import Path

import nbformat
from nbclient import NotebookClient
from nbformat.v4 import new_code_cell, new_markdown_cell, new_notebook

REPO_ROOT = Path(__file__).resolve().parent.parent
NOTEBOOK_PATH = REPO_ROOT / "notebooks" / "02_data_quality.ipynb"

CELLS: list[tuple[str, str]] = [
    ("markdown", """\
# Phase 2 -- Data Quality Review

**WID Datathon 2026 -- Food System Resilience Agent**

Reviews the output of the Phase 2 Bronze -> Silver ELT pipeline
(`src/elt/pipeline.py`). Does **not** call the FAOSTAT API or transform
anything -- it loads the already-built `data/food_system.db`, the generated
`data_quality/*.csv`, and `reports/phase2_data_quality.md`.

Rebuild the inputs with:

```bash
uv run python -m src.elt.pipeline
```

No feature engineering, scoring, or Phase 3+ analysis happens here --
see `PHASE2.md` / `CLAUDE.md` for scope.
"""),
    ("code", """\
import sys
from pathlib import Path

REPO_ROOT = Path.cwd().parent if Path.cwd().name == "notebooks" else Path.cwd()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pandas as pd
from sqlalchemy import inspect, text

from src.database.connection import get_engine

pd.set_option("display.max_colwidth", 70)
pd.set_option("display.width", 180)
pd.set_option("display.max_rows", 120)

DQ = REPO_ROOT / "data_quality"
engine = get_engine(REPO_ROOT / "data" / "food_system.db")
insp = inspect(engine)
print("database:", engine.url)
print("tables:", len(insp.get_table_names()))
"""),
    ("markdown", "## 1. Ingestion runs (`etl_runs`) -- one auditable row per domain"),
    ("code", """\
runs = pd.read_sql_query("SELECT * FROM etl_runs ORDER BY domain", engine)
runs[["domain", "priority", "status", "requested_start_year", "requested_end_year",
      "actual_min_year", "actual_max_year", "missing_requested_years",
      "requested_item_count", "retrieved_item_count", "country_count", "partner_count",
      "bronze_rows", "silver_rows", "error_message"]]
"""),
    ("markdown", """\
`status` is per-domain and independent. A **core** domain
(QCL, TM, FBS, FS, CAHD) at `failed` blocks Phase 2 (non-zero pipeline exit
code); a **supporting** domain (CP, GT, RL, RFM) at `failed`/`partial` is
reported but does not."""),
    ("markdown", "## 2. QCL crops-only scope"),
    ("code", """\
from src.elt.qcl_items import DEFAULT_QCL_CROP_ITEMS_PATH, load_qcl_crop_items

crop_items = load_qcl_crop_items(DEFAULT_QCL_CROP_ITEMS_PATH)
print(f"{len(crop_items)} crop items (FAOSTAT itemgroup QC, leaf items only)")
pd.DataFrame(crop_items).head(10)
"""),
    ("markdown", "## 3. FAOSTAT flags -- preserved through Bronze, never used to drop rows"),
    ("code", """\
flags = pd.read_csv(DQ / "bronze_flag_summary.csv")
flags.pivot_table(index="domain", columns="flag", values="n_rows", aggfunc="sum", fill_value=0)
"""),
    ("markdown", "## 4. Unit inventory and conversions"),
    ("code", """\
print("Bronze unit inventory (domain x unit x element):")
display(pd.read_csv(DQ / "bronze_unit_inventory.csv").head(40))
conv_path = DQ / "unit_conversions_applied.csv"
conv = pd.read_csv(conv_path)
if not conv.empty:
    print("\\nUnit conversions applied in Silver:")
    display(conv)
else:
    print("\\nNo unit conversions were applicable to the rows actually ingested.")
"""),
    ("markdown", "## 5. Duplicates"),
    ("code", """\
dc = pd.read_csv(DQ / "duplicate_conflicts.csv")
cov = pd.read_csv(DQ / "duplicate_conflicts_coverage.csv")
print("Domains whose Silver transform ran in the invocation that wrote these files:")
display(cov)
if not dc.empty:
    print(f"{len(dc)} conflicting-duplicate key(s) resolved (prefer official flag, then largest |value|):")
    display(dc.head(30))
else:
    print("No conflicting duplicates found. Exact duplicates (if any) were collapsed silently in Silver.")
"""),
    ("markdown", "## 6. Commodity mapping status"),
    ("code", """\
cm = pd.read_csv(DQ / "commodity_mapping.csv")
print(cm["mapping_status"].value_counts())
print("\\nA sample of unmapped commodity items (kept visible, never force-mapped):")
cm[cm.mapping_status == "unmapped"][["domain_code", "item_code", "item"]].head(30)
"""),
    ("markdown", "## 7. Historical coverage (country level) -- reported, not filtered"),
    ("code", """\
hc_path = DQ / "historical_coverage_country.csv"
hc = pd.read_csv(hc_path)
if not hc.empty:
    n_insufficient = int((~hc.sufficient_history).sum())
    print(f"{len(hc)} countries with QCL production data; {n_insufficient} flagged insufficient_history")
    display(hc.sort_values("coverage_pct").head(25))
else:
    print("silver_production is empty (QCL did not load) -- see etl_runs above.")
"""),
    ("markdown", "## 8. Missingness"),
    ("code", 'pd.read_csv(DQ / "bronze_missingness.csv")'),
    ("markdown", """\
## 9. Silver validation (domain-aware)

Hard checks (e.g. no livestock item may reach `silver_production`) block
core-table validity; soft checks are reported only -- see
`src/validation/silver_schemas.py`."""),
    ("code", """\
val = pd.read_csv(DQ / "silver_validation.csv")
hard = val[(val.severity == "hard") & (val.n_violations > 0)]
soft = val[(val.severity == "soft") & (val.n_violations > 0)]
print("HARD violations:", len(hard), "| SOFT (reported only):", len(soft))
display(val[val.n_violations > 0] if (val.n_violations > 0).any() else val.head(20))
"""),
    ("markdown", "## 10. Structural tables/views -- the Phase 3 inputs"),
    ("code", """\
for view in ["silver_country_year", "silver_country_commodity_year", "silver_trade_matrix"]:
    try:
        df = pd.read_sql_query(f"SELECT * FROM {view} LIMIT 5", engine)
        n = pd.read_sql_query(f"SELECT COUNT(*) AS n FROM {view}", engine)["n"].iloc[0]
        print(f"=== {view} ({n:,} rows) ===")
        display(df)
    except Exception as exc:  # noqa: BLE001
        print(f"{view}: not available ({exc})")
"""),
    ("markdown", """\
## 11. Requested vs retrieved analytical variables

`config/variables.yaml` (translated from `DATA.md`) is the source of truth for
what this project asked FAOSTAT for. Anything requested that did not come back
is listed here rather than quietly replaced with a substitute."""),
    ("code", """\
scope = pd.read_csv(DQ / "variable_scope.csv")
summary = scope.groupby(["domain", "scope_kind"]).agg(
    requested=("requested", "size"), retrieved=("retrieved", "sum")
).reset_index()
summary["unavailable"] = summary["requested"] - summary["retrieved"]
display(summary)
missing = scope[~scope["retrieved"]]
if missing.empty:
    print("Every requested variable was returned by the API.")
else:
    print(f"{len(missing)} requested variable(s) not returned by the API:")
    display(missing)
"""),
    ("markdown", "## 12. Full per-domain report"),
    ("code", 'print((REPO_ROOT / "reports" / "phase2_data_quality.md").read_text())'),
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
