# STADVDB MCO1 - Group 7: Financial Data Warehouse & OLAP Dashboard

Berka / PKDD'99 Czech bank dataset (1,056,320 transactions, 4,500 accounts, 1993-1998)
loaded into a MySQL star-schema data warehouse, explored through a Streamlit OLAP dashboard.

**Streamlit Docs Link:** https://docs.streamlit.io

## Project structure

| Path | What it is |
|---|---|
| `MCO1 - Dataset financedataclean.sql` | Source database dump (`financedata`). **NOTE: This is not in the repo** (since it is 112 MB, and over GitHub's file limit); get it from the team Drive and put it in the project root |
| `finance-db/STADVDB-MCO1-Group7-Schema.sql` | Data warehouse schema (`finance_mco1`, star schema) |
| `finance-db/etl.py` | **ETL pipeline** (Python): extract -> transform -> load -> reconcile |
| `finance-db/STADVDB-MCO1-Group7-ETL.sql` | Legacy SQL-only ETL, kept for reference (superseded by `etl.py`) |
| `finance-db/STADVDB-MCO1-Group7-OLAP-Queries.sql` | Example OLAP queries (generated, runnable in MySQL Workbench) |
| `db.py` | MySQL connection helpers (shared by ETL, dashboard, tests, benchmarks) |
| `olap/queries.py` | OLAP query builders: roll-up, drill-down, slice, dice, pivot |
| `olap/export_sql.py` | Generates the example OLAP SQL file |
| `streamlit_app.py` | Web OLAP dashboard |
| `tests/` | Functional test cases, runner and pytest entry point |
| `tests/results/` | Test documentation produced by the last test run |
| `benchmark.py` | Performance benchmark: query variants x index configurations (Step 6, part 1) |
| `optimize.py` | Optimization experiments: reformulation, denormalization, indexing (Step 6, part 2) |
| `STADVDB-MCO1-Group7-Optimization.sql` | Original vs optimized queries as `EXPLAIN ANALYZE` blocks, runnable in MySQL Workbench |
| `results/performance/` | Output of `benchmark.py` (timings, summary, EXPLAIN plans, environment) |
| `results/optimized/` | Output of `optimize.py` (before/after timings, plans, storage cost) |
| `.streamlit/` | Streamlit theme (`config.toml`) and the credentials template (`secrets.toml.example`) |
| `.devcontainer/` | GitHub Codespaces / VS Code dev container config |

## Setup (first time)

**Prerequisites:** Python 3.11 or newer (the code uses `tomllib`), MySQL Server 8.0+
(`EXPLAIN ANALYZE` and optimizer hints are used; tested on 8.0.43 and 8.0.46) and the
source dump `MCO1 - Dataset financedataclean.sql` in the project root.

1. **Python environment**
   ```
   python -m venv .venv
   .venv\Scripts\activate          (Windows)   |   source .venv/bin/activate   (Mac/Linux)
   pip install -r requirements.txt
   ```
2. **MySQL credentials** - copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml`
   and put your MySQL password in it. This file is git-ignored, so every teammate keeps their own.
3. **Source database** - import the dump (creates the `financedata` database; run this from the
   folder that contains the dump):
   ```
   mysql -u root -p < "MCO1 - Dataset financedataclean.sql"
   ```
   (or MySQL Workbench > Server > Data Import > Import from Self-Contained File)
4. **Build the data warehouse** - creates `finance_mco1` and loads it (about 40 seconds):
   ```
   python finance-db/etl.py
   ```
   The ETL prints a reconciliation check at the end; the totals per year and type in the
   warehouse must match the source.

## Run

* **Dashboard:** `streamlit run streamlit_app.py` -> http://localhost:8501
* **Functional tests + documentation:** `python tests/run_functional_tests.py`
  (or `pytest tests -v`)
* **Regenerate the OLAP SQL file:** `python -m olap.export_sql`
* **Performance benchmark:** `python benchmark.py` (see [Performance optimization](#performance-optimization-step-6))
* **Optimization experiments:** `python optimize.py` (same section)
* **To stop the dashboard:** `Ctrl + C`
* **To deactivate the environment:** `deactivate`

## Data warehouse (star schema)

```
            date  (year > quarter > month > day)
              |
account ---- transaction_fact ---- transaction_type
(region >     (amount, balance)          |
 district >          |            transaction_operation
 account)            +-------------------+
```

* **Fact:** `transaction_fact` - one row per transaction; measures `amount` (additive) and
  `balance` (semi-additive snapshot: averaged, never summed).
* **Hierarchies:** Time (Year > Quarter > Month > Day), Location (Region > District > Account),
  Transaction (Type > Operation). `account_frequency` is an extra attribute for slicing.
* Money is stored as `DECIMAL(15,2)`. Summing the source's `DOUBLE` column gives
  floating-point error (e.g. 3,030,321,135.793 instead of .80); the tests caught this.
* Every dimension has an **Unknown** member (key -1), so fact keys are never NULL and no
  transaction is lost in joins.

## ETL transformations (`finance-db/etl.py`)

| Transformation | What it does |
|---|---|
| Cleaning | Trims text; blank strings become NULL before handling |
| Standardizing | Untranslated Czech type `VYBER` (16,666 rows, all cash withdrawals) -> `Debit (Withdrawal)` |
| Deriving / NULL handling | 183,114 blank operations all have `k_symbol = 'Interest Credited'` -> operation `Interest Credited`; anything else missing -> `Unknown` |
| Merging | `account` + `district` -> account dimension with the location hierarchy |
| Splitting | Each date -> day, month, month name, quarter, year, `1997-Q1` and `1997-05` labels |
| Type conversion | Dates -> `DATE`, money -> `DECIMAL(15,2)`, surrogate keys -> `INT` |
| Aggregating | After loading, totals per year and type are aggregated from the cleaned source and from the warehouse, and must match exactly (reconciliation) |

## OLAP operations (dashboard)

| Operation | How it works | SQL techniques |
|---|---|---|
| **Roll-up** | Pick a hierarchy and a detail level; shows details, subtotals for each higher level, and a grand total; chart at any granularity | `GROUP BY ... WITH ROLLUP`, `GROUPING()` |
| **Drill-down** | Start at the top of a hierarchy, pick a member, drill into its children (e.g. 1997 > Q2 > months > days) | CTE, `SUM() OVER ()` share of parent, `RANK()`, `LAG()` change vs previous period |
| **Slice** | Fix one dimension member (e.g. Year = 1997), view by another level | CTE, window share, running total |
| **Dice** | Conditions on 2+ dimensions (year range, quarters, regions, districts, types, operations, frequency, amount range), grouped by 1 or 2 levels | CTE, `IN` / range predicates, `RANK() OVER (PARTITION BY ...)` |
| **Pivot** | Choose row level, column level and measure; **Swap** rotates them | Conditional aggregation `SUM(CASE WHEN ...)`, `WITH ROLLUP` totals |

Each report shows the SQL that was executed and its bound parameters. Filter values are
always bound parameters; level/measure names are checked against a whitelist.

### Why Plotly?

Streamlit's built-in charts are simple line/bar charts. The OLAP reports also need
**heatmaps** (pivot and 2-level dice), **horizontal ranked bars**, **hover tooltips** with
exact values, and **fixed colors per member** (e.g. Credit is always blue). Plotly provides all
of these and Streamlit displays Plotly figures directly (`st.plotly_chart`). How it works:
the dashboard runs the SQL, gets a pandas DataFrame, and passes it to `plotly.express`
(`px.bar`, `px.line`) or `plotly.graph_objects.Heatmap`, which build an interactive chart
in the browser. Hovering shows values; you can zoom, and the camera icon saves a PNG.

## Performance optimization (Step 6)

Run all commands from the project root, with the warehouse already built. Both scripts need
the MySQL credentials in `.streamlit/secrets.toml`.

**1. Benchmark (`benchmark.py`)** measures the OLAP test queries as originally written vs the
current builders, across four index configurations (`baseline`, `idx_time`, `idx_account`,
`idx_all`). Each query gets one warm-up run and then timed runs, and every result set is hashed
so an "optimization" that changes the answer is reported as a MISMATCH.

```
python benchmark.py                          # 10 timed runs per query, all configs
python benchmark.py --runs 5                 # quicker smoke test
python benchmark.py --configs baseline idx_all
python benchmark.py --only RU DD-3           # only cases whose ID starts with these
```

Output goes to `results/performance/` (`timings_raw.csv`, `summary.csv`, `comparison.csv`,
`explain_plans.txt`, `environment.txt`).

**2. Optimization experiments (`optimize.py`)** applies one strategy per bottleneck found in
the benchmark and compares before/after on the same run:

| Bottleneck | Strategy | Applies to |
|---|---|---|
| Broad queries read, join and sort all ~1.05M fact rows to return a few totals | **Denormalization**: pre-summed table `summary_month_district` | RU-1, RU-6, DD-1, DD-4, PV-1, PV-2 |
| Same, but a month/district summary cannot answer it (day or account level) | **Query reformulation**: aggregate the fact table by its keys first, then join the dimensions | RU-1, RU-3, RU-5 |
| Filtered queries make an extra table trip for every matching row | **Secondary (covering) indexes** | 15 filtered queries |

```
python optimize.py                    # all three experiments, 5 timed runs per query
python optimize.py --runs 3           # quicker
python optimize.py --only indexing    # reformulation | denormalization | indexing
python optimize.py --keep             # keep the summary table / indexes afterwards
```

Output goes to `results/optimized/` (`optimization_summary.csv`, `optimization_raw.csv`,
`optimization_plans.txt`, `optimization_storage.txt`). Unless `--keep` is given, the
experimental table and indexes are dropped at the end, even if the run is interrupted.

All 24 optimized queries returned the same result set as the original
(`same_result = yes` in `optimization_summary.csv`). Highlights from our run (median ms,
MySQL 8.0.46, 128 MiB buffer pool):

| Query | Strategy | Before | After | Speedup |
|---|---|---|---|---|
| PV-1 Total amount: region x year | Summary table | 2203.9 | 28.9 | x76.3 |
| RU-1 Time, month level | Summary table | 2859.3 | 55.5 | x51.5 |
| RU-3 Time, day level | Reformulation | 3553.1 | 343.7 | x10.3 |
| SL-3 Type = Credit by year | Covering index | 1109.5 | 127.7 | x8.7 |

**Trade-offs:** the summary table adds ~28,000 rows (~7.5 MB) and must be rebuilt after every
ETL load; the three covering indexes add ~89 MB to `transaction_fact` and slow down loads.
Reformulation costs nothing. Full numbers are in `results/optimized/optimization_storage.txt`.

To inspect the plans by hand, run the numbered blocks in `STADVDB-MCO1-Group7-Optimization.sql`
one at a time in MySQL Workbench (select a block, then `Ctrl+Shift+Enter`).

> Timings depend on hardware and MySQL settings, so re-run the scripts on your machine and use
> your own numbers in the report.

## Functional testing

`python tests/run_functional_tests.py` runs **38 test cases** (Roll-up, Drill-down, Slice,
Dice, Pivot, NULL handling, and dashboard-vs-warehouse checks) and writes:

* `tests/results/FUNCTIONAL_TEST_REPORT.md` - summary + table with ID, operation,
  objective, inputs, steps, expected result, **actual result** and **status**
* `tests/results/functional_test_results.csv` - the same table (opens in Excel)

Each case runs the dashboard's own query builder on the warehouse and compares it with an
**independent query on the source database** (`financedata`) and with consistency rules
(subtotals = sum of children, pivot totals = sum of cells, swapped pivot = transpose).
UI cases drive the real dashboard headlessly (Streamlit `AppTest`) and compare the displayed
table with the warehouse query. Results are always produced by an actual run - re-run after
any change.
