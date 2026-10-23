# benchmark.py

"""Performance benchmark for the OLAP queries (MCO1 Step 6).

Place this file in the project root (next to db.py and streamlit_app.py) and run:

    python benchmark.py                      # 10 timed runs per query, all configs
    python benchmark.py --runs 5             # quicker smoke test
    python benchmark.py --configs baseline idx_all

What it measures
  Two QUERY VARIANTS x several INDEX CONFIGURATIONS x every test query:

  variants
    original the query as originally written: all four dimension tables are
             joined whether or not the query uses them, and no optimizer hint.
             (rebuilt from the current builders - see original_variant())
    current  exactly what olap/queries.py generates today (unused joins
             pruned by star_from(), JOIN_PREFIX(f) hint on unfiltered queries)

  index configurations (extra secondary indexes on transaction_fact)
    baseline     only the PK and the FK indexes InnoDB creates automatically
    idx_time     (date_key, type_key, amount)
    idx_account  (account_key, date_key, type_key, amount)
    idx_all      idx_time + idx_account + (operation_key, date_key, type_key, amount)

Per (config, variant, query): 1 untimed warm-up run, then --runs timed runs.
Every run's wall-clock time (execute + fetchall) is recorded. The result set of
each query is hashed and compared across ALL variants/configs, so an
optimization that changes the answer is reported as a MISMATCH.

Outputs (results/performance/)
    timings_raw.csv      one row per timed run
    summary.csv          median / mean / min / max / stdev per combination
    comparison.csv       median ms per query, side by side, with speedups
    explain_plans.txt    EXPLAIN ANALYZE for every combination
    environment.txt      hardware, MySQL version, buffer pool, table sizes
"""
import argparse
import hashlib
import os
import platform
import re
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pandas as pd

from db import get_connection, run_query
from olap import queries as q

OUT_DIR = Path(__file__).resolve().parent / "results" / "performance"

# ---------------------------------------------------------------- index configs
INDEXES = {
    "ix_date_type_amt": "(date_key, type_key, amount)",
    "ix_acct_date_type_amt": "(account_key, date_key, type_key, amount)",
    "ix_op_date_type_amt": "(operation_key, date_key, type_key, amount)",
}
CONFIGS = {
    "baseline": [],
    "idx_time": ["ix_date_type_amt"],
    "idx_account": ["ix_acct_date_type_amt"],
    "idx_all": ["ix_date_type_amt", "ix_acct_date_type_amt", "ix_op_date_type_amt"],
}


def apply_config(conn, config):
    cur = conn.cursor()
    for name in CONFIGS[config]:
        cur.execute(f"ALTER TABLE transaction_fact ADD INDEX {name} {INDEXES[name]}")
    cur.execute("ANALYZE TABLE transaction_fact")
    cur.fetchall()
    conn.commit()
    cur.close()


FK_COLUMNS = ["date_key", "account_key", "type_key", "operation_key"]


def restore_fk_indexes(cur):
    """When a composite index starting with a FK column is added, MySQL silently
    drops the single-column index it had created for that foreign key. A foreign
    key must always keep an index, so before an experimental index is dropped we
    make sure every FK column still has some other index that starts with it."""
    cur.execute("""SELECT index_name, column_name, seq_in_index
                   FROM information_schema.statistics
                   WHERE table_schema = DATABASE() AND table_name = 'transaction_fact'""")
    leading = {col for name, col, seq in cur.fetchall() if seq == 1 and name not in INDEXES}
    for col in FK_COLUMNS:
        if col not in leading:
            cur.execute(f"ALTER TABLE transaction_fact ADD INDEX idx_fk_{col} ({col})")
            print(f"  (restored foreign-key index on {col})")


def remove_config(conn, config):
    cur = conn.cursor()
    restore_fk_indexes(cur)
    for name in CONFIGS[config]:
        try:
            cur.execute(f"ALTER TABLE transaction_fact DROP INDEX {name}")
        except Exception as e:  # already gone
            print(f"  (could not drop {name}: {e})")
    conn.commit()
    cur.close()


def drop_leftover_indexes(conn):
    """Drop any experimental index that still exists (e.g. after an interrupted run)."""
    cur = conn.cursor()
    try:
        restore_fk_indexes(cur)
        cur.execute("""SELECT DISTINCT index_name FROM information_schema.statistics
                       WHERE table_schema = DATABASE() AND table_name = 'transaction_fact'""")
        existing = {r[0] for r in cur.fetchall()}
        for name in INDEXES:
            if name in existing:
                cur.execute(f"ALTER TABLE transaction_fact DROP INDEX {name}")
        conn.commit()
    except Exception as e:
        print(f"  (cleanup problem: {e})")
    finally:
        cur.close()


def log_indexes(conn, path):
    """Print (and append to environment.txt) the indexes on transaction_fact so the
    baseline is documented: it should be the PK plus one index per foreign key."""
    cur = conn.cursor()
    cur.execute("""SELECT index_name, GROUP_CONCAT(column_name ORDER BY seq_in_index)
                   FROM information_schema.statistics
                   WHERE table_schema = DATABASE() AND table_name = 'transaction_fact'
                   GROUP BY index_name ORDER BY index_name""")
    rows = cur.fetchall()
    cur.close()
    lines = ["", "Indexes on transaction_fact at the start of the run (baseline):"] + \
            [f"  {name:<28} ({cols})" for name, cols in rows]
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("\n".join(lines))


# ---------------------------------------------------------------- query variants
JOIN_ALIASES = list(q.DIMENSION_JOINS.values())


def original_variant(oq):
    """Re-create the unoptimized form of a query: remove the JOIN_PREFIX hint and
    join every dimension table even when the query never references it. Because
    every fact FK is non-NULL (Unknown member = -1), the extra INNER JOINs do not
    change the result - they only add work."""
    sql = re.sub(r"/\*\+ JOIN_PREFIX\(f\) \*/\s*", "", oq.sql)
    missing = [j for j in JOIN_ALIASES if j not in sql]
    if missing:
        sql = sql.replace("FROM transaction_fact f",
                          "FROM transaction_fact f\n" + "\n".join(missing), 1)
    return sql


VARIANTS: dict[str, Callable] = {
    "original": original_variant,
    "current": lambda oq: oq.sql,
}


# ---------------------------------------------------------------- test queries
@dataclass
class Case:
    id: str
    operation: str
    label: str
    build: Callable


def make_cases():
    years = list(run_query(*q.pivot_columns_query("year")).itertuples(index=False, name=None))
    regions = list(run_query(*q.pivot_columns_query("region")).itertuples(index=False, name=None))
    return [
        # ROLL-UP: different hierarchies, depths and filters
        Case("RU-1", "Roll-up", "Time, month level, no filter",
             lambda: q.rollup("time", "month")),
        Case("RU-2", "Roll-up", "Time, month level, year 1997",
             lambda: q.rollup("time", "month", filters={"year_from": 1997, "year_to": 1997})),
        Case("RU-3", "Roll-up", "Time, day level, no filter (deepest)",
             lambda: q.rollup("time", "day")),
        Case("RU-4", "Roll-up", "Location, district level, year 1997",
             lambda: q.rollup("location", "district", filters={"year_from": 1997, "year_to": 1997})),
        Case("RU-5", "Roll-up", "Location, account level, no filter (deepest)",
             lambda: q.rollup("location", "account")),
        Case("RU-6", "Roll-up", "Transaction, operation level, no filter",
             lambda: q.rollup("transaction", "operation")),
        # DRILL-DOWN: each level of each hierarchy
        Case("DD-1", "Drill-down", "Time: all -> years",
             lambda: q.drilldown("time", [])),
        Case("DD-2", "Drill-down", "Time: 1997 -> quarters",
             lambda: q.drilldown("time", [1997])),
        Case("DD-3", "Drill-down", "Time: 1997 Q2 -> months",
             lambda: q.drilldown("time", [1997, 2])),
        Case("DD-4", "Drill-down", "Location: all -> regions",
             lambda: q.drilldown("location", [])),
        Case("DD-5", "Drill-down", "Location: south Moravia -> districts",
             lambda: q.drilldown("location", ["south Moravia"])),
        Case("DD-6", "Drill-down", "Location: Prague -> districts",
             lambda: q.drilldown("location", ["Prague"])),
        # SLICE: different dimensions fixed
        Case("SL-1", "Slice", "Year = 1997 by region",
             lambda: q.slice_("year", 1997, "region")),
        Case("SL-2", "Slice", "Region = Prague by month",
             lambda: q.slice_("region", "Prague", "month")),
        Case("SL-3", "Slice", "Type = Credit by year",
             lambda: q.slice_("type", "Credit", "year")),
        Case("SL-4", "Slice", "Year = 1996 by operation",
             lambda: q.slice_("year", 1996, "operation")),
        # DICE: two or more dimensions, narrow and wide
        Case("DC-1", "Dice", "1996-97 x Prague/south Moravia x Credit x amount>=1000",
             lambda: q.dice({"year_from": 1996, "year_to": 1997,
                             "regions": ["Prague", "south Moravia"], "types": ["Credit"],
                             "amount_min": 1000}, "region", by_level2="operation")),
        Case("DC-2", "Dice", "1995-97 x 3 regions x Debit, by region and year",
             lambda: q.dice({"year_from": 1995, "year_to": 1997,
                             "regions": ["Prague", "south Moravia", "central Bohemia"],
                             "types": [q.DEBIT]}, "region", by_level2="year")),
        Case("DC-3", "Dice", "Q1-Q2 x Prague, by month",
             lambda: q.dice({"quarters": [1, 2], "regions": ["Prague"]}, "month")),
        Case("DC-4", "Dice", "All years x amount 5000-20000 x Credit, by region",
             lambda: q.dice({"amount_min": 5000, "amount_max": 20000, "types": ["Credit"]},
                            "region")),
        # PIVOT: swapped axes, filtered and unfiltered
        Case("PV-1", "Pivot", "Total amount: region x year",
             lambda: q.pivot("region", "year", "total_amount", years)),
        Case("PV-2", "Pivot", "Total amount: year x region",
             lambda: q.pivot("year", "region", "total_amount", regions)),
        Case("PV-3", "Pivot", "Net flow: district x year, region Prague",
             lambda: q.pivot("district", "year", "net_flow", years,
                             filters={"regions": ["Prague"]})),
        Case("PV-4", "Pivot", "Active accounts: region x year",
             lambda: q.pivot("region", "year", "active_accounts", years)),
    ]


# ---------------------------------------------------------------- measuring
def time_query(cur, sql, params):
    t0 = time.perf_counter()
    cur.execute(sql, params)
    rows = cur.fetchall()
    return (time.perf_counter() - t0) * 1000, rows


def result_hash(rows):
    return hashlib.md5("\n".join(sorted(map(repr, rows))).encode()).hexdigest()


def explain_analyze(cur, sql, params):
    try:
        cur.execute("EXPLAIN ANALYZE " + sql, params)
        return "\n".join(str(r[0]) for r in cur.fetchall())
    except Exception as e:
        return f"(EXPLAIN ANALYZE failed: {e})"


def write_environment(conn, path):
    cur = conn.cursor()
    cur.execute("SELECT VERSION()")
    version = cur.fetchone()[0]
    cur.execute("SHOW VARIABLES LIKE 'innodb_buffer_pool_size'")
    pool = int(cur.fetchone()[1])
    cur.execute("""SELECT table_name, table_rows,
                          ROUND(data_length/1024/1024,1), ROUND(index_length/1024/1024,1)
                   FROM information_schema.tables WHERE table_schema = DATABASE()""")
    tables = cur.fetchall()
    cur.close()
    ram = "unknown"
    try:
        ram = f"{os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES') / 2**30:.1f} GiB"
    except (AttributeError, ValueError, OSError):
        try:
            import psutil
            ram = f"{psutil.virtual_memory().total / 2**30:.1f} GiB"
        except ImportError:
            pass
    lines = [
        f"Run at           : {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"OS               : {platform.platform()}",
        f"CPU              : {platform.processor() or platform.machine()}  ({os.cpu_count()} logical cores)",
        f"RAM              : {ram}",
        f"Python           : {platform.python_version()}",
        f"MySQL            : {version}",
        f"innodb_buffer_pool_size : {pool / 2**20:.0f} MiB",
        "",
        "Warehouse tables (approx. rows, data MB, index MB):",
    ] + [f"  {t[0]:<24} rows~{t[1]:>10,}  data {t[2]:>8} MB  index {t[3]:>8} MB" for t in tables]
    path.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--runs", type=int, default=10, help="timed runs per query (default 10)")
    ap.add_argument("--configs", nargs="*", default=list(CONFIGS), choices=list(CONFIGS))
    ap.add_argument("--variants", nargs="*", default=list(VARIANTS), choices=list(VARIANTS),
                    help="query variants to time (default: both). 'current' = the queries as "
                         "the dashboard runs them")
    ap.add_argument("--only", nargs="*", help="only cases whose ID starts with these, e.g. RU DD-3")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cases = make_cases()
    if args.only:
        cases = [c for c in cases if any(c.id.startswith(p) for p in args.only)]

    conn = get_connection("warehouse")
    write_environment(conn, OUT_DIR / "environment.txt")
    drop_leftover_indexes(conn)
    log_indexes(conn, OUT_DIR / "environment.txt")

    raw, plans, reference, mismatches = [], [], {}, []
    try:
        for config in args.configs:
            print(f"\n=== index config: {config} {CONFIGS[config]}")
            apply_config(conn, config)
            cur = conn.cursor()
            for variant in args.variants:
                transform = VARIANTS[variant]
                for case in cases:
                    oq = case.build()
                    sql = transform(oq)
                    # warm-up (not recorded): loads pages into the buffer pool
                    _, rows = time_query(cur, sql, oq.params)
                    h = result_hash(rows)
                    ref = reference.setdefault(case.id, (h, config, variant, rows))
                    if ref[0] != h:
                        a, b = set(map(repr, ref[3])), set(map(repr, rows))
                        only_ref, only_now = sorted(a - b), sorted(b - a)
                        mismatches.append(
                            f"{case.id} [{config}/{variant}] differs from [{ref[1]}/{ref[2]}]: "
                            f"{len(only_ref)} rows only in reference, {len(only_now)} rows only here\n"
                            f"      reference: {only_ref[:2]}\n      this run : {only_now[:2]}")
                    times = []
                    for i in range(1, args.runs + 1):
                        ms, rows = time_query(cur, sql, oq.params)
                        times.append(ms)
                        raw.append({"config": config, "variant": variant, "case_id": case.id,
                                    "operation": case.operation, "description": case.label,
                                    "run": i, "ms": round(ms, 2), "rows": len(rows),
                                    "cols": len(cur.description)})
                    print(f"  {config:<11} {variant:<8} {case.id:<5} median "
                          f"{statistics.median(times):>9.1f} ms   rows {len(rows):>6}")
                    plans.append(f"##### {config} | {variant} | {case.id} - {case.label}\n"
                                 f"{explain_analyze(cur, sql, oq.params)}\n")
            cur.close()
            remove_config(conn, config)
    finally:
        # never leave experimental indexes behind, even after Ctrl-C
        drop_leftover_indexes(conn)
        conn.close()
        if raw:
            raw_df = pd.DataFrame(raw)
            raw_df.to_csv(OUT_DIR / "timings_raw.csv", index=False)
            (OUT_DIR / "explain_plans.txt").write_text("\n".join(plans), encoding="utf-8")
            write_summaries(raw_df)

    print("\n" + ("RESULT CHECK: all variants/configs returned identical result sets."
                  if not mismatches else "RESULT MISMATCHES:\n  " + "\n  ".join(mismatches)))
    print(f"Outputs written to {OUT_DIR}")


def write_summaries(raw_df):
    g = raw_df.groupby(["config", "variant", "case_id", "operation", "description"], sort=False)
    summary = g["ms"].agg(median="median", mean="mean", min="min", max="max", stdev="std",
                          runs="count").round(2).reset_index()
    summary["rows"] = g["rows"].first().values
    summary.to_csv(OUT_DIR / "summary.csv", index=False)

    wide = summary.pivot_table(index=["case_id", "operation", "description"],
                               columns=["config", "variant"], values="median", sort=False)
    wide.columns = [f"{c}|{v}" for c, v in wide.columns]
    wide = wide.reset_index()
    base = "baseline|original"
    if base in wide.columns:
        for col in [c for c in wide.columns if "|" in c and c != base]:
            wide[f"speedup {col}"] = (wide[base] / wide[col]).round(2)
    wide.to_csv(OUT_DIR / "comparison.csv", index=False)


if __name__ == "__main__":
    main()