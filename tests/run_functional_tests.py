# tests > run_functional_tests.py

"""Runs all the OLAP functional tests and writes the test documentation.

    python tests/run_functional_tests.py            # all cases
    python tests/run_functional_tests.py RU DD-03   # only IDs starting with these

Output (overwritten every run with that run's results):
    tests/results/functional_test_results.csv   - one row per test case
    tests/results/FUNCTIONAL_TEST_REPORT.md     - summary + table of all cases
"""
from pathlib import Path
import csv
import datetime as dt
import platform
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from db import get_connection, run_query  # noqa: E402
from functional_cases import CASES, Ctx  # noqa: E402

RESULTS_DIR = Path(__file__).resolve().parent / "results"
FIELDS = ["Test Case ID", "OLAP Operation", "Test Objective", "Input Parameters / Filters",
          "Steps to Execute", "Expected Result", "Actual Result", "Status", "Duration (s)"]


def execute_case(case, ctx):
    t0 = time.time()
    try:
        actual = case.check(ctx)
        status = "PASS"
    except AssertionError as e:
        actual, status = f"FAILED: {e}", "FAIL"
    except Exception as e:  # an unexpected error means the test couldn't finish
        actual, status = f"ERROR: {type(e).__name__}: {e}", "ERROR"
    return {
        "Test Case ID": case.id, "OLAP Operation": case.operation, "Test Objective": case.objective,
        "Input Parameters / Filters": case.inputs, "Steps to Execute": case.steps,
        "Expected Result": case.expected, "Actual Result": actual, "Status": status,
        "Duration (s)": f"{time.time() - t0:.2f}",
    }


def run(selected=None):
    cases = [c for c in CASES if not selected or any(c.id.startswith(s) for s in selected)]
    ctx = Ctx(get_connection("warehouse"), get_connection("source"))
    results = []
    for case in cases:
        r = execute_case(case, ctx)
        results.append(r)
        print(f"{r['Status']:5s} {case.id:6s} ({r['Duration (s)']:>6s}s) {case.objective}")
        if r["Status"] != "PASS":
            print(f"        {r['Actual Result']}")
    env = run_query("SELECT VERSION() v, (SELECT COUNT(*) FROM transaction_fact) n", conn=ctx.wh).iloc[0]
    ctx.wh.close()
    ctx.src.close()
    return results, env


def md(text):
    return str(text).replace("|", "\\|").replace("\n", "<br>")


def write_reports(results, env, partial):
    RESULTS_DIR.mkdir(exist_ok=True)
    with open(RESULTS_DIR / "functional_test_results.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(results)

    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    ops = ["Roll-up", "Drill-down", "Slice", "Dice", "Pivot", "All"]
    lines = [
        "# OLAP Functional Test Report - STADVDB MCO1 Group 7",
        "",
        f"- **Executed:** {now}" + (" (partial run)" if partial else ""),
        f"- **Environment:** MySQL {env.v}, Python {platform.python_version()}, "
        f"warehouse `finance_mco1` with {int(env.n):,} fact rows",
        "- **How to reproduce:** `python tests/run_functional_tests.py` (or `pytest tests`)",
        "- **Method:** each case runs the same query builder the dashboard uses "
        "(`olap/queries.py`) on the warehouse and compares the result with an independent "
        "query on the source database `financedata` and/or internal consistency rules. "
        "UI cases drive the real dashboard headlessly (Streamlit AppTest) and compare the "
        "displayed table with the warehouse query.",
        "",
        "## Summary",
        "",
        "| Operation | Cases | Pass | Fail | Error |",
        "|---|---|---|---|---|",
    ]
    for op in ops:
        rs = [r for r in results if r["OLAP Operation"] == op]
        if rs:
            lines.append(f"| {op} | {len(rs)} | {sum(r['Status'] == 'PASS' for r in rs)} | "
                         f"{sum(r['Status'] == 'FAIL' for r in rs)} | {sum(r['Status'] == 'ERROR' for r in rs)} |")
    lines.append(f"| **Total** | **{len(results)}** | **{sum(r['Status'] == 'PASS' for r in results)}** | "
                 f"**{sum(r['Status'] == 'FAIL' for r in results)}** | "
                 f"**{sum(r['Status'] == 'ERROR' for r in results)}** |")
    lines += ["", "## Test cases", ""]
    for op in ops:
        rs = [r for r in results if r["OLAP Operation"] == op]
        if not rs:
            continue
        lines += [f"### {op}" if op != "All" else "### Cross-cutting (NULL / Unknown handling)", "",
                  "| ID | Objective | Input parameters / filters | Steps to execute | Expected result | Actual result | Status |",
                  "|---|---|---|---|---|---|---|"]
        for r in rs:
            lines.append("| " + " | ".join(md(r[k]) for k in [
                "Test Case ID", "Test Objective", "Input Parameters / Filters", "Steps to Execute",
                "Expected Result", "Actual Result"]) + f" | **{r['Status']}** |")
        lines.append("")
    (RESULTS_DIR / "FUNCTIONAL_TEST_REPORT.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    selected = sys.argv[1:]
    results, env = run(selected)
    write_reports(results, env, partial=bool(selected))
    passed = sum(r["Status"] == "PASS" for r in results)
    print(f"\n{passed}/{len(results)} passed. Reports written to {RESULTS_DIR}")
    sys.exit(0 if passed == len(results) else 1)
