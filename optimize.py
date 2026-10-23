# optimize.py

"""Optimization experiments for the OLAP queries (MCO1 Step 6, part 2).

Put this file in the project root, next to benchmark.py and db.py, then run:

    python optimize.py                    # all three experiments, 5 timed runs per query
    python optimize.py --runs 3           # quicker
    python optimize.py --only indexing    # just one experiment (reformulation / denormalization / indexing)
    python optimize.py --keep             # leave the new summary table / indexes in place afterwards

Method (follows the spec: find the bottleneck first, then pick the matching strategy)

  Bottleneck found in Script 1 (benchmark.py --configs baseline --variants current):
    A. Broad roll-ups / drill-downs / pivots read, join, copy and sort all 1.06M fact rows
       just to return a handful of totals.
    B. Filtered queries find rows through a single-column foreign-key index, then make an
       extra trip to the main table for EVERY matching row to read amount / type_key.
    C. Type / amount filters use the 3-value type_key index, which matches ~38% of the table.

  Strategy chosen for each bottleneck:
    denormalization       (A) a small pre-summed table  summary_month_district
                          -> RU-1, RU-6, DD-1, DD-4, PV-1, PV-2
    query reformulation   (A, where a month/district summary cannot answer the question:
                          day level, account level) aggregate the fact table by its own keys
                          FIRST, then join the dimensions to the small result
                          -> RU-3, RU-5   (RU-1 too, to compare with the summary table)
    secondary indexes     (B, C) covering composite indexes that already contain every column
                          the query needs, so no trip to the main table
                          -> 15 filtered queries

  Every optimized query is compared with the original query: its result set must be
  identical, otherwise the case is flagged. 'Before' times are measured in the same run,
  right before the experiments, so the comparison is fair.

Outputs (results/optimized/)
    optimization_summary.csv   before / after median (ms) and speedup per query and strategy
    optimization_raw.csv       every timed run
    optimization_plans.txt     EXPLAIN ANALYZE before and after for every query
    optimization_storage.txt   what each optimization costs (extra rows / MB / build time)

Unless --keep is given, every experimental table and index is removed at the end,
even if the run is interrupted.
"""
import argparse
import re
import statistics
import time
from pathlib import Path

import pandas as pd

from db import get_connection
from olap import queries as q
from benchmark import make_cases, time_query, result_hash, explain_analyze

OUT_DIR = Path(__file__).resolve().parent / "results" / "optimized"
SUMMARY_TABLE = "summary_month_district"
FK_COLS = ["date_key", "account_key", "type_key", "operation_key"]

COVERING_INDEXES = {
    # date first: queries filtered by year / quarter / month
    "ix_cov_date": "(date_key, type_key, account_key, operation_key, amount)",
    # account first: queries filtered by region / district (accounts -> fact rows)
    "ix_cov_account": "(account_key, date_key, type_key, amount)",
    # type first: queries filtered by transaction type and amount range
    "ix_cov_type": "(type_key, amount, date_key, account_key)",
}

STORAGE = []   # lines for optimization_storage.txt


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- housekeeping
def ensure_fk_indexes(conn):
    """Every foreign key must keep an index of its own. MySQL silently drops the
    automatic one when a composite index starting with the same column is added, which
    would make that composite index impossible to drop later. So we make sure each FK
    column has an explicitly named index (idx_fk_<col>) that MySQL never removes by itself."""
    cur = conn.cursor()
    cur.execute("""SELECT index_name, column_name, seq_in_index FROM information_schema.statistics
                   WHERE table_schema = DATABASE() AND table_name = 'transaction_fact'""")
    leading = {col for name, col, seq in cur.fetchall()
               if seq == 1 and name not in COVERING_INDEXES}
    for col in FK_COLS:
        if col not in leading:
            cur.execute(f"ALTER TABLE transaction_fact ADD INDEX idx_fk_{col} ({col})")
            log(f"  (restored foreign-key index on {col})")
    conn.commit()
    cur.close()


def cleanup(conn):
    """Remove everything this script creates (safe to call at any time)."""
    ensure_fk_indexes(conn)
    cur = conn.cursor()
    cur.execute(f"DROP TABLE IF EXISTS {SUMMARY_TABLE}")
    cur.execute("""SELECT DISTINCT index_name FROM information_schema.statistics
                   WHERE table_schema = DATABASE() AND table_name = 'transaction_fact'""")
    existing = {r[0] for r in cur.fetchall()}
    for name in COVERING_INDEXES:
        if name in existing:
            cur.execute(f"ALTER TABLE transaction_fact DROP INDEX {name}")
    conn.commit()
    cur.close()


def table_mb(conn, table):
    cur = conn.cursor()
    cur.execute("""SELECT table_rows, ROUND(data_length/1024/1024, 1), ROUND(index_length/1024/1024, 1)
                   FROM information_schema.tables
                   WHERE table_schema = DATABASE() AND table_name = %s""", (table,))
    row = cur.fetchone()
    cur.close()
    return row


def analyze(conn, table):
    cur = conn.cursor()
    cur.execute(f"ANALYZE TABLE {table}")
    cur.fetchall()
    cur.close()


# ---------------------------------------------------------------- strategy: denormalization
SUMMARY_COLS = {
    r"\bd\.year_number\b": "s.year_number",
    r"\bd\.quarter_number\b": "s.quarter_number",
    r"\bd\.month_number\b": "s.month_number",
    r"\bd\.month_name\b": "s.month_name",
    r"\ba\.region\b": "s.region",
    r"\ba\.district_name\b": "s.district_name",
    r"\btt\.transaction_type\b": "s.transaction_type",
    r"\bop\.operation_name\b": "s.operation_name",
    r"\bf\.amount\b": "s.total_amount",
}


def build_summary_table(conn):
    """Pre-summed table: one row per month x district x transaction type x operation."""
    cur = conn.cursor()
    cur.execute(f"DROP TABLE IF EXISTS {SUMMARY_TABLE}")
    cur.execute(f"""
        CREATE TABLE {SUMMARY_TABLE} (
            year_number INT NOT NULL,
            quarter_number INT NOT NULL,
            month_number INT NOT NULL,
            month_name VARCHAR(20) NOT NULL,
            region VARCHAR(100) NOT NULL,
            district_name VARCHAR(100) NOT NULL,
            transaction_type VARCHAR(100) NOT NULL,
            operation_name VARCHAR(150) NOT NULL,
            txn_count BIGINT NOT NULL,
            total_amount DECIMAL(18,2) NOT NULL,
            INDEX idx_sum_time (year_number, quarter_number, month_number),
            INDEX idx_sum_loc (region, district_name)
        )""")
    t0 = time.perf_counter()
    cur.execute(f"""
        INSERT INTO {SUMMARY_TABLE}
        SELECT d.year_number, d.quarter_number, d.month_number, MAX(d.month_name),
               a.region, a.district_name, tt.transaction_type, op.operation_name,
               COUNT(*), SUM(f.amount)
        FROM transaction_fact f
        JOIN `date` d                  ON d.date_key = f.date_key
        JOIN account a                 ON a.account_key = f.account_key
        JOIN transaction_type tt       ON tt.type_key = f.type_key
        JOIN transaction_operation op  ON op.operation_key = f.operation_key
        GROUP BY d.year_number, d.quarter_number, d.month_number,
                 a.region, a.district_name, tt.transaction_type, op.operation_name""")
    build_s = time.perf_counter() - t0
    conn.commit()
    cur.close()
    analyze(conn, SUMMARY_TABLE)
    rows, data_mb, idx_mb = table_mb(conn, SUMMARY_TABLE)
    fact_rows = table_mb(conn, "transaction_fact")[0]
    STORAGE.append(f"denormalization: {SUMMARY_TABLE} built in {build_s:.1f} s, ~{rows:,} rows "
                   f"(fact table ~{fact_rows:,}), data {data_mb} MB + indexes {idx_mb} MB. "
                   "It must be rebuilt (or refreshed) after every ETL load.")
    log(f"  summary table: ~{rows:,} rows built in {build_s:.1f} s")


def to_summary_sql(sql):
    """Same query, but reading the summary table instead of the fact table + dimensions."""
    out = re.sub(r"/\*\+ JOIN_PREFIX\(f\) \*/\s*", "", sql)
    out = re.sub(r"^[ \t]*JOIN .*$\n?", "", out, flags=re.M)
    out = out.replace("FROM transaction_fact f", f"FROM {SUMMARY_TABLE} s")
    out = out.replace("COUNT(*)", "CAST(SUM(s.txn_count) AS SIGNED)")
    for pattern, repl in SUMMARY_COLS.items():
        out = re.sub(pattern, repl, out)
    return out


# ---------------------------------------------------------------- strategy: query reformulation
def preagg_rollup(hierarchy, level):
    """Roll-up that first adds up the fact table by its OWN foreign keys (a few thousand
    groups), and only then joins the dimension tables to that small result. The original
    joins and sorts all 1.06M fact rows before grouping."""
    path = q.HIERARCHIES[hierarchy]
    levels = [q.LEVELS[n] for n in path[: path.index(level) + 1]]
    fk, dim_join = {
        "time": ("date_key", "JOIN `date` d ON d.date_key = p.k"),
        "location": ("account_key", "JOIN account a ON a.account_key = p.k"),
    }[hierarchy]

    select = []
    for lv in levels:
        select += q._level_select(lv, rollup=True)
    case = ["CASE", f"        WHEN GROUPING({levels[0].key}) = 1 THEN 'All'"]
    for parent, child in zip(levels, levels[1:]):
        case.append(f"        WHEN GROUPING({child.key}) = 1 THEN '{parent.title}'")
    case.append(f"        ELSE '{levels[-1].title}'\n    END AS aggregation_level")
    select.append("\n".join(case))
    c, d = q.CREDIT, q.DEBIT
    select += [
        "CAST(SUM(p.n) AS SIGNED) AS txn_count",
        "ROUND(SUM(p.amt), 2) AS total_amount",
        f"ROUND(SUM(CASE WHEN tt.transaction_type = '{c}' THEN p.amt ELSE 0 END), 2) AS total_credit",
        f"ROUND(SUM(CASE WHEN tt.transaction_type = '{d}' THEN p.amt ELSE 0 END), 2) AS total_debit",
        f"ROUND(SUM(CASE WHEN tt.transaction_type = '{c}' THEN p.amt "
        f"WHEN tt.transaction_type = '{d}' THEN -p.amt ELSE 0 END), 2) AS net_flow",
    ]
    order = []
    for lv in levels:
        order += [f"GROUPING({lv.key})", lv.key]
    return "\n".join([
        "SELECT",
        "    " + ",\n    ".join(select),
        "FROM (",
        f"    SELECT f.{fk} AS k, f.type_key AS type_key, COUNT(*) AS n, SUM(f.amount) AS amt",
        "    FROM transaction_fact f",
        f"    GROUP BY f.{fk}, f.type_key",
        ") p",
        dim_join,
        "JOIN transaction_type tt ON tt.type_key = p.type_key",
        "GROUP BY " + ", ".join(lv.key for lv in levels) + " WITH ROLLUP",
        "ORDER BY " + ", ".join(order),
    ])


# ---------------------------------------------------------------- strategy: secondary indexes
def add_covering_indexes(conn):
    ensure_fk_indexes(conn)
    cur = conn.cursor()
    analyze(conn, "transaction_fact")
    prev = table_mb(conn, "transaction_fact")[2]
    for name, cols in COVERING_INDEXES.items():
        t0 = time.perf_counter()
        cur.execute(f"ALTER TABLE transaction_fact ADD INDEX {name} {cols}")
        build_s = time.perf_counter() - t0
        conn.commit()
        analyze(conn, "transaction_fact")
        now = table_mb(conn, "transaction_fact")[2]
        STORAGE.append(f"indexing: {name} {cols} built in {build_s:.1f} s, adds ~{now - prev:.1f} MB")
        log(f"  index {name}: built in {build_s:.1f} s, ~{now - prev:.1f} MB")
        prev = now
    cur.close()


# ---------------------------------------------------------------- experiments
INDEXING_CASES = ["RU-2", "RU-4", "DD-2", "DD-3", "DD-5", "DD-6", "SL-1", "SL-2", "SL-4",
                  "DC-1", "DC-2", "DC-3", "PV-3", "SL-3", "DC-4"]
SUMMARY_CASES = ["RU-1", "RU-6", "DD-1", "DD-4", "PV-1", "PV-2"]

EXPERIMENTS = [
    # key, label, cases, prepare(conn, base) -> {case_id: optimized sql}
    ("reformulation", "Query reformulation (aggregate first, join after)", ["RU-1", "RU-3", "RU-5"],
     lambda conn, base: {
         "RU-1": preagg_rollup("time", "month"),
         "RU-3": preagg_rollup("time", "day"),
         "RU-5": preagg_rollup("location", "account")}),
    ("denormalization", "Denormalization (pre-summed table)", SUMMARY_CASES,
     lambda conn, base: (build_summary_table(conn),
                         {cid: to_summary_sql(base[cid]["sql"]) for cid in SUMMARY_CASES})[1]),
    ("indexing", "Secondary indexes (covering)", INDEXING_CASES,
     lambda conn, base: (add_covering_indexes(conn),
                         {cid: base[cid]["sql"] for cid in INDEXING_CASES})[1]),
]


# ---------------------------------------------------------------- measuring
def measure(cur, sql, params, runs):
    _, rows = time_query(cur, sql, params)          # warm-up, not recorded
    times = []
    for _ in range(runs):
        ms, rows = time_query(cur, sql, params)
        times.append(ms)
    return times, rows


def diff_note(ref_rows, rows):
    a, b = set(map(repr, ref_rows)), set(map(repr, rows))
    return (f"{len(a - b)} rows only in the original, {len(b - a)} only in the optimized; "
            f"e.g. {sorted(a - b)[:1]} vs {sorted(b - a)[:1]}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--runs", type=int, default=5, help="timed runs per query (default 5)")
    ap.add_argument("--only", nargs="*", choices=[e[0] for e in EXPERIMENTS],
                    help="run only these experiments")
    ap.add_argument("--keep", action="store_true",
                    help="keep the summary table / covering indexes after the run")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_connection("warehouse")
    cur = conn.cursor()
    cur.execute("SET SESSION information_schema_stats_expiry = 0")   # fresh size statistics
    cur.close()

    raw, plans, summary, problems = [], [], [], []
    chosen = [e for e in EXPERIMENTS if not args.only or e[0] in args.only]
    try:
        log("Preparing: removing leftovers of earlier runs and rebuilding the fact table ...")
        cleanup(conn)
        cur = conn.cursor()
        cur.execute("OPTIMIZE TABLE transaction_fact")
        cur.fetchall()
        cur.close()
        analyze(conn, "transaction_fact")
        rows, data_mb, idx_mb = table_mb(conn, "transaction_fact")
        STORAGE.append(f"baseline: transaction_fact ~{rows:,} rows, data {data_mb} MB, indexes {idx_mb} MB")

        cases = {c.id: c for c in make_cases()}
        needed = sorted({cid for e in chosen for cid in e[2]})

        # ---- BEFORE: the original queries, timed right now
        log(f"\nBEFORE: timing {len(needed)} original queries ({args.runs} runs each)")
        base = {}
        cur = conn.cursor()
        for cid in needed:
            oq = cases[cid].build()
            times, result = measure(cur, oq.sql, oq.params, args.runs)
            base[cid] = {"sql": oq.sql, "params": oq.params, "median": statistics.median(times),
                         "hash": result_hash(result), "rows": result, "case": cases[cid]}
            for i, ms in enumerate(times, 1):
                raw.append({"strategy": "original", "case_id": cid, "run": i, "ms": round(ms, 2)})
            plans.append(f"##### ORIGINAL | {cid} - {cases[cid].label}\n"
                         f"{explain_analyze(cur, oq.sql, oq.params)}\n")
            log(f"  {cid:<5} median {base[cid]['median']:>8.1f} ms   rows {len(result):>5}")
        cur.close()

        # ---- AFTER: each strategy on the queries that have its bottleneck
        for key, label, case_ids, prepare in chosen:
            log(f"\nAFTER: {label}")
            try:
                optimized = prepare(conn, base)
            except Exception as e:
                log(f"  could not prepare this experiment: {e}")
                problems.append(f"{key}: could not prepare ({e})")
                if not args.keep:
                    cleanup(conn)
                continue
            cur = conn.cursor()
            for cid in case_ids:
                b = base[cid]
                try:
                    times, result = measure(cur, optimized[cid], b["params"], args.runs)
                except Exception as e:
                    log(f"  {cid:<5} ERROR: {e}")
                    problems.append(f"{key} {cid}: {e}")
                    summary.append({"strategy": key, "case_id": cid, "operation": b["case"].operation,
                                    "description": b["case"].label, "before_ms": round(b["median"], 1),
                                    "after_ms": None, "speedup": None, "rows": None,
                                    "same_result": f"ERROR: {e}"})
                    continue
                after = statistics.median(times)
                same = result_hash(result) == b["hash"]
                if not same:
                    problems.append(f"{key} {cid}: {diff_note(b['rows'], result)}")
                for i, ms in enumerate(times, 1):
                    raw.append({"strategy": key, "case_id": cid, "run": i, "ms": round(ms, 2)})
                summary.append({"strategy": key, "case_id": cid, "operation": b["case"].operation,
                                "description": b["case"].label, "before_ms": round(b["median"], 1),
                                "after_ms": round(after, 1), "speedup": round(b["median"] / after, 2),
                                "rows": len(result), "same_result": "yes" if same else "NO"})
                plans.append(f"##### {key.upper()} | {cid} - {b['case'].label}\n"
                             f"{explain_analyze(cur, optimized[cid], b['params'])}\n")
                log(f"  {cid:<5} {b['median']:>8.1f} ms -> {after:>8.1f} ms   "
                    f"x{b['median'] / after:>5.1f}   same result: {'yes' if same else 'NO'}")
            cur.close()
            if not args.keep:
                cleanup(conn)           # next experiment starts from the original design
    finally:
        if not args.keep:
            try:
                cleanup(conn)
            except Exception as e:
                log(f"(cleanup problem: {e})")
        conn.close()
        if summary:
            df = pd.DataFrame(summary)
            df.to_csv(OUT_DIR / "optimization_summary.csv", index=False)
            pd.DataFrame(raw).to_csv(OUT_DIR / "optimization_raw.csv", index=False)
            (OUT_DIR / "optimization_plans.txt").write_text("\n".join(plans), encoding="utf-8")
            (OUT_DIR / "optimization_storage.txt").write_text("\n".join(STORAGE), encoding="utf-8")
            print("\n" + df[["strategy", "case_id", "before_ms", "after_ms", "speedup",
                              "same_result"]].to_string(index=False))

    print("\n" + ("RESULT CHECK: every optimized query returned the same result as the original."
                  if not problems else "PROBLEMS:\n  " + "\n  ".join(problems)))
    print("\n".join(["", "Cost of the optimizations:"] + ["  " + s for s in STORAGE]))
    print(f"Outputs written to {OUT_DIR}")


if __name__ == "__main__":
    main()