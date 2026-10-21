"""Functional test cases for the five OLAP operations.

Each case runs the same query builder the dashboard uses (olap/queries.py) on
the warehouse and checks the result against either:
- a separate query on the source database (financedata) with the same cleaning
  rules as the ETL, so ETL mistakes get caught too, or
- consistency checks (subtotals add up to their children, pivot totals add up
  to the cells, etc.)

Each check returns a short "actual result" string, and raises AssertionError
if it doesn't match the expected result.
"""
from dataclasses import dataclass
from typing import Callable
import datetime as dt
import math

import pandas as pd

from db import run_query
from olap import queries as q

TOL = 0.005 

AMT = "CAST(t.amount AS DECIMAL(15,2))"
SRC_TYPE = "(CASE WHEN t.type = 'VYBER' THEN 'Debit (Withdrawal)' ELSE t.type END)"
SRC_FROM = """FROM trans t
JOIN account ac ON ac.account_id = t.account_id
LEFT JOIN district di ON di.district_id = ac.district_id"""
SRC_MEASURES = (f"COUNT(*) AS txn_count, SUM({AMT}) AS total_amount, "
                f"SUM(CASE WHEN {SRC_TYPE} = 'Credit' THEN {AMT} ELSE 0 END) AS total_credit, "
                f"SUM(CASE WHEN {SRC_TYPE} = 'Debit (Withdrawal)' THEN {AMT} ELSE 0 END) AS total_debit")


@dataclass
class Ctx:
    wh: object      # warehouse connection
    src: object     # source connection

    def run(self, oq):
        return run_query(oq.sql, oq.params, conn=self.wh)

    def wh_sql(self, sql, params=()):
        return run_query(sql, list(params), conn=self.wh)

    def src_sql(self, sql, params=()):
        return run_query(sql, list(params), conn=self.src)


@dataclass
class Case:
    id: str
    operation: str
    objective: str
    inputs: str
    steps: str
    expected: str
    check: Callable[[Ctx], str]


# assertion helpers
def close(a, b, tol=TOL):
    if a is None or b is None or (isinstance(a, float) and math.isnan(a)) \
            or (isinstance(b, float) and math.isnan(b)):
        return (a is None or (isinstance(a, float) and math.isnan(a))) and \
               (b is None or (isinstance(b, float) and math.isnan(b)))
    return abs(float(a) - float(b)) <= tol


def assert_close(actual, expected, what, tol=TOL):
    assert close(actual, expected, tol), f"{what}: expected {expected}, got {actual}"


def assert_frames_match(actual, expected, keys, cols, what):
    """Check that both have the same groups with the same values (nothing
    missing or extra)."""
    a = actual.copy()
    e = expected.copy()
    for k in keys:
        a[k] = a[k].astype(str)
        e[k] = e[k].astype(str)
    a = a.set_index(keys)
    e = e.set_index(keys)
    missing = set(e.index) - set(a.index)
    extra = set(a.index) - set(e.index)
    assert not missing, f"{what}: groups missing from OLAP result: {sorted(missing)[:5]}"
    assert not extra, f"{what}: unexpected groups in OLAP result: {sorted(extra)[:5]}"
    for idx in e.index:
        for c in cols:
            assert close(a.at[idx, c], e.at[idx, c]), \
                f"{what}: {idx} {c} expected {e.at[idx, c]}, got {a.at[idx, c]}"
    return len(e)


def expect_error(fn, *fragments):
    try:
        fn()
    except ValueError as e:
        msg = str(e)
        for frag in fragments:
            assert frag in msg, f"error raised but message lacks '{frag}': {msg}"
        return msg
    raise AssertionError("no error was raised for invalid parameters")


def money(x):
    return f"{x:,.2f}"


def pivot_members(ctx, col_level, filters=None):
    sql, params = q.pivot_columns_query(col_level, filters)
    return list(ctx.wh_sql(sql, params).itertuples(index=False, name=None))


# roll-up
def ru01(ctx):
    df = ctx.run(q.rollup("time", "quarter"))
    detail = df[df.aggregation_level == "Quarter"]
    years = df[df.aggregation_level == "Year"]
    grand = df[df.aggregation_level == "All"]
    assert len(grand) == 1, "expected exactly one grand-total row"
    for _, y in years.iterrows():
        kids = detail[detail.year == y.year]
        assert len(kids) <= 4
        assert kids.txn_count.sum() == y.txn_count, f"year {y.year} count != sum of quarters"
        assert_close(kids.total_amount.sum(), y.total_amount, f"year {y.year} amount vs quarters")
    assert years.txn_count.sum() == grand.txn_count.iloc[0]
    assert_close(years.total_amount.sum(), grand.total_amount.iloc[0], "grand total vs years")
    src = ctx.src_sql(f"SELECT {SRC_MEASURES} FROM trans t")
    assert grand.txn_count.iloc[0] == src.txn_count.iloc[0], "grand total count differs from source"
    assert_close(grand.total_amount.iloc[0], src.total_amount.iloc[0], "grand total amount vs source")
    return (f"{len(detail)} quarter rows, {len(years)} year subtotals, 1 grand total; all subtotals "
            f"equal the sum of their children; grand total {int(grand.txn_count.iloc[0]):,} txns / "
            f"{money(grand.total_amount.iloc[0])} = source")


def ru02(ctx):
    df = ctx.run(q.rollup("time", "month"))
    years = df[df.aggregation_level == "Year"][["year", "txn_count", "total_amount", "total_credit", "total_debit"]]
    src = ctx.src_sql(f"SELECT YEAR(t.newdate) AS year, {SRC_MEASURES} FROM trans t GROUP BY YEAR(t.newdate)")
    years = years.assign(year=years.year.astype(int))
    n = assert_frames_match(years, src, ["year"], ["txn_count", "total_amount", "total_credit", "total_debit"],
                            "year subtotals")
    months = df[df.aggregation_level == "Month"]
    assert months.month_name.notna().all() and df[df.aggregation_level != "Month"].month_name.isna().all(), \
        "month_name must be filled only on month rows"
    return f"{len(months)} month rows; {n} year subtotals match source (count, amount, credits, debits)"


def ru03(ctx):
    df = ctx.run(q.rollup("location", "district"))
    regions = df[df.aggregation_level == "Region"][["region", "txn_count", "total_amount"]]
    src = ctx.src_sql(f"SELECT di.region AS region, {SRC_MEASURES} {SRC_FROM} GROUP BY di.region")
    n = assert_frames_match(regions, src, ["region"], ["txn_count", "total_amount"], "region subtotals")
    districts = df[df.aggregation_level == "District"]
    n_src = ctx.src_sql(f"SELECT COUNT(DISTINCT ac.district_id) AS n {SRC_FROM}").n.iloc[0]
    assert len(districts) == n_src, f"expected {n_src} districts, got {len(districts)}"
    for _, r in regions.iterrows():
        kids = districts[districts.region == r.region]
        assert kids.txn_count.sum() == r.txn_count
    return f"{len(districts)} district rows; {n} region subtotals match source and equal sum of their districts"


def ru04(ctx):
    df = ctx.run(q.rollup("transaction", "operation"))
    types = df[df.aggregation_level == "Transaction type"][["type", "txn_count", "total_amount"]]
    src = ctx.src_sql(f"SELECT {SRC_TYPE} AS type, {SRC_MEASURES} FROM trans t GROUP BY 1")
    n = assert_frames_match(types, src, ["type"], ["txn_count", "total_amount"], "type subtotals")
    assert "VYBER" not in set(df.type.dropna()), "untranslated code VYBER present"
    interest = df[(df.aggregation_level == "Operation") & (df.operation == "Interest Credited")]
    blanks = ctx.src_sql("SELECT COUNT(*) AS n FROM trans WHERE operation IS NULL OR TRIM(operation) = ''").n.iloc[0]
    assert int(interest.txn_count.iloc[0]) == blanks, "Interest Credited count != source blank operations"
    return (f"{n} type subtotals match source (VYBER folded into Debit); blank source operations "
            f"({blanks:,}) all appear as 'Interest Credited'; no NULL/blank operation member")


def ru05(ctx):
    filters = {"year_from": 1997, "year_to": 1997}
    df = ctx.run(q.rollup("location", "region", filters=filters))
    grand = df[df.aggregation_level == "All"].iloc[0]
    src = ctx.src_sql(f"SELECT {SRC_MEASURES} FROM trans t WHERE YEAR(t.newdate) = 1997").iloc[0]
    assert grand.txn_count == src.txn_count
    assert_close(grand.total_amount, src.total_amount, "1997 grand total")
    return f"{len(df) - 1} region rows; grand total {int(grand.txn_count):,} txns / {money(grand.total_amount)} = source for 1997"


def ru06(ctx):
    m1 = expect_error(lambda: q.rollup("time", "region"), "not part of the time hierarchy")
    m2 = expect_error(lambda: q.rollup("weather", "year"), "unknown hierarchy")
    m3 = expect_error(lambda: q.rollup("time", "year", ["profit"]), "unknown measure")
    return f"ValueError raised for all 3 invalid inputs, e.g. '{m1}'"


# drill-down
def dd01(ctx):
    df = ctx.run(q.drilldown("time", []))
    src = ctx.src_sql(f"SELECT YEAR(t.newdate) AS year, {SRC_MEASURES} FROM trans t GROUP BY 1")
    n = assert_frames_match(df, src, ["year"], ["txn_count", "total_amount", "total_credit", "total_debit"], "years")
    assert_close(df.pct_of_parent_amount.sum(), 100, "shares", tol=0.05)
    first = df.sort_values("year").iloc[0]
    assert pd.isna(first.prev_total_amount), "first period must have no previous value (LAG = NULL)"
    return f"{n} years match source; shares sum to {df.pct_of_parent_amount.sum():.2f}%; first year has NULL LAG"


def dd02(ctx):
    years = ctx.run(q.drilldown("time", []))
    df = ctx.run(q.drilldown("time", [1997]))
    assert sorted(df.quarter) == [1, 2, 3, 4], f"expected quarters 1-4, got {list(df.quarter)}"
    parent = years[years.year == 1997].iloc[0]
    assert df.txn_count.sum() == parent.txn_count
    assert_close(df.total_amount.sum(), parent.total_amount, "quarters vs year 1997")
    src = ctx.src_sql(f"SELECT QUARTER(t.newdate) AS quarter, {SRC_MEASURES} FROM trans t "
                      "WHERE YEAR(t.newdate) = 1997 GROUP BY 1")
    assert_frames_match(df, src, ["quarter"], ["txn_count", "total_amount"], "1997 quarters")
    return f"4 quarters; they sum to the 1997 parent ({money(parent.total_amount)}) and match source"


def dd03(ctx):
    df = ctx.run(q.drilldown("time", [1997, 2])).sort_values("month")
    assert list(df.month) == [4, 5, 6], f"expected months 4,5,6 got {list(df.month)}"
    assert list(df.month_name) == ["April", "May", "June"]
    src = ctx.src_sql(f"SELECT MONTH(t.newdate) AS month, {SRC_MEASURES} FROM trans t "
                      "WHERE YEAR(t.newdate) = 1997 AND QUARTER(t.newdate) = 2 GROUP BY 1")
    assert_frames_match(df, src, ["month"], ["txn_count", "total_amount", "total_credit", "total_debit"], "months")
    apr, may = df.iloc[0], df.iloc[1]
    expected_change = round(100 * (may.total_amount - apr.total_amount) / apr.total_amount, 2)
    assert_close(may.pct_change_vs_prev, expected_change, "May % change vs April", tol=0.01)
    return f"April-June 1997 match source; May change vs April = {may.pct_change_vs_prev}% (LAG correct)"


def dd04(ctx):
    months = ctx.run(q.drilldown("time", [1997, 2]))
    df = ctx.run(q.drilldown("time", [1997, 2, 5]))
    assert len(df) == 31, f"May has 31 days, got {len(df)}"
    assert all(d.month == 5 and d.year == 1997 for d in df["day"])
    may = months[months.month == 5].iloc[0]
    assert df.txn_count.sum() == may.txn_count
    assert_close(df.total_amount.sum(), may.total_amount, "days vs May")
    return f"31 days of May 1997; sum {money(df.total_amount.sum())} = May total"


def dd05(ctx):
    regions = ctx.run(q.drilldown("location", []))
    df = ctx.run(q.drilldown("location", ["south Moravia"]))
    parent = regions[regions.region == "south Moravia"].iloc[0]
    n_src = ctx.src_sql("SELECT COUNT(*) AS n FROM district WHERE region = 'south Moravia'").n.iloc[0]
    assert len(df) == n_src, f"expected {n_src} districts"
    assert df.txn_count.sum() == parent.txn_count
    accts = ctx.run(q.drilldown("location", ["south Moravia", "Brno - mesto"]))
    district = df[df.district == "Brno - mesto"].iloc[0]
    n_acc = ctx.src_sql("SELECT COUNT(DISTINCT ac.account_id) AS n FROM account ac JOIN district di "
                        "USING (district_id) WHERE di.district_name = 'Brno - mesto'").n.iloc[0]
    assert len(accts) == n_acc, f"expected {n_acc} accounts, got {len(accts)}"
    assert_close(accts.total_amount.sum(), district.total_amount, "accounts vs district")
    return (f"south Moravia -> {len(df)} districts (sum = region); Brno - mesto -> {len(accts)} accounts "
            f"(sum {money(accts.total_amount.sum())} = district)")


def dd06(ctx):
    credit = ctx.run(q.drilldown("transaction", ["Credit"]))
    debit = ctx.run(q.drilldown("transaction", ["Debit (Withdrawal)"]))
    assert set(credit.operation) == {"Credit in Cash", "Collection from Another Bank", "Interest Credited"}
    assert set(debit.operation) == {"Withdrawal in Cash", "Remittance to Another Bank", "Credit Card Withdrawal"}
    wic = debit[debit.operation == "Withdrawal in Cash"].txn_count.iloc[0]
    src = ctx.src_sql("SELECT COUNT(*) AS n FROM trans WHERE operation = 'Withdrawal in Cash'").n.iloc[0]
    assert wic == src, f"Withdrawal in Cash: expected {src} (incl. VYBER rows), got {wic}"
    assert (credit.total_debit == 0).all() and (debit.total_credit == 0).all()
    return f"Credit -> 3 operations, Debit -> 3 operations; Withdrawal in Cash = {wic:,} (incl. 16,666 former VYBER rows)"


def dd07(ctx):
    m1 = expect_error(lambda: q.drilldown("transaction", ["Credit", "Credit in Cash"]), "cannot drill below")
    m2 = expect_error(lambda: q.drilldown("time", ["abc"]), "invalid value")
    df = ctx.run(q.drilldown("time", [2005]))
    assert df.empty, "a year outside the data must give an empty result"
    return f"drilling below finest level and non-integer year rejected ('{m1}'); year 2005 -> 0 rows (no error)"


# slice
def sl01(ctx):
    df = ctx.run(q.slice_("year", 1997, "region"))
    src = ctx.src_sql(f"SELECT di.region AS region, {SRC_MEASURES} {SRC_FROM} "
                      "WHERE YEAR(t.newdate) = 1997 GROUP BY di.region")
    n = assert_frames_match(df, src, ["region"], ["txn_count", "total_amount", "total_credit", "total_debit"],
                            "1997 by region")
    assert_close(df.pct_of_slice_amount.sum(), 100, "shares", tol=0.05)
    assert list(df.amount_rank) == sorted(df.amount_rank)
    return f"{n} regions match source for 1997; shares sum to 100%; ranks ordered"


def sl02(ctx):
    df = ctx.run(q.slice_("region", "Prague", "month"))
    assert len(df) == 12
    src = ctx.src_sql(f"SELECT MONTH(t.newdate) AS month, {SRC_MEASURES} {SRC_FROM} "
                      "WHERE di.region = 'Prague' GROUP BY 1")
    assert_frames_match(df, src, ["month"], ["txn_count", "total_amount"], "Prague by month")
    assert_close(df.running_total_amount.iloc[-1], df.total_amount.sum(), "final running total")
    return f"12 months match source; running total ends at {money(df.running_total_amount.iloc[-1])} = Prague total"


def sl03(ctx):
    df = ctx.run(q.slice_("type", "Credit", "year"))
    assert (df.total_debit == 0).all(), "a Credit slice must contain no debits"
    for _, r in df.iterrows():
        assert_close(r.net_flow, r.total_credit, f"net flow {r.year}")
    src = ctx.src_sql(f"SELECT YEAR(t.newdate) AS year, {SRC_MEASURES} FROM trans t "
                      f"WHERE {SRC_TYPE} = 'Credit' GROUP BY 1")
    n = assert_frames_match(df, src, ["year"], ["txn_count", "total_amount"], "Credit by year")
    return f"{n} years match source; debits = 0 and net flow = credits in every year"


def sl04(ctx):
    df = ctx.run(q.slice_("frequency", "Weekly", "type"))
    src = ctx.src_sql(f"SELECT {SRC_TYPE} AS type, {SRC_MEASURES} {SRC_FROM} "
                      "WHERE ac.frequency = 'Weekly' GROUP BY 1")
    n = assert_frames_match(df, src, ["type"], ["txn_count", "total_amount"], "Weekly by type")
    return f"{n} transaction types match source for weekly-statement accounts"


def sl05(ctx):
    df = ctx.run(q.slice_("region", "Atlantis", "year"))
    assert df.empty
    return "region 'Atlantis' -> 0 rows, no error"


def sl06(ctx):
    m1 = expect_error(lambda: q.slice_("year", 1997, "year"), "must be different")
    m2 = expect_error(lambda: q.slice_("year", None, "region"), "is required")
    m3 = expect_error(lambda: q.slice_("quarter", "x", "region"), "invalid value")
    m4 = expect_error(lambda: q.slice_("planet", "Mars", "region"), "unknown level")
    return f"4 invalid inputs rejected with ValueError, e.g. '{m1}'"


# dice
def di01(ctx):
    filters = {"year_from": 1996, "year_to": 1997, "regions": ["Prague", "south Moravia"],
               "types": ["Credit"], "amount_min": 1000}
    df = ctx.run(q.dice(filters, "region", by_level2="operation"))
    src = ctx.src_sql(f"SELECT di.region AS region, t.operation AS operation, {SRC_MEASURES} {SRC_FROM} "
                      "WHERE YEAR(t.newdate) BETWEEN 1996 AND 1997 AND di.region IN ('Prague','south Moravia') "
                      f"AND {SRC_TYPE} = 'Credit' AND t.amount >= 1000 GROUP BY 1, 2")
    n = assert_frames_match(df, src, ["region", "operation"], ["txn_count", "total_amount"], "dice cells")
    for reg, g in df.groupby("region"):
        assert_close(g.pct_within_region.sum(), 100, f"{reg} shares", tol=0.05)
    return f"{n} region x operation cells match source; shares within each region sum to 100%"


def di02(ctx):
    filters = {"quarters": [1, 4], "operations": ["Withdrawal in Cash"], "frequencies": ["Monthly"]}
    df = ctx.run(q.dice(filters, "year"))
    src = ctx.src_sql(f"SELECT YEAR(t.newdate) AS year, {SRC_MEASURES} {SRC_FROM} "
                      "WHERE QUARTER(t.newdate) IN (1, 4) AND t.operation = 'Withdrawal in Cash' "
                      "AND ac.frequency = 'Monthly' GROUP BY 1")
    n = assert_frames_match(df, src, ["year"], ["txn_count", "total_amount"], "dice by year")
    return f"{n} years match source (Q1+Q4, Withdrawal in Cash, Monthly statements)"


def di03(ctx):
    filters = {"date_from": "1998-01-01", "date_to": "1998-03-31",
               "districts": ["Hl.m. Praha", "Brno - mesto"]}
    df = ctx.run(q.dice(filters, "month", by_level2="district"))
    assert set(df.month) == {1, 2, 3}
    src = ctx.src_sql(f"SELECT MONTH(t.newdate) AS month, di.district_name AS district, {SRC_MEASURES} "
                      f"{SRC_FROM} WHERE t.newdate BETWEEN '1998-01-01' AND '1998-03-31' "
                      "AND di.district_name IN ('Hl.m. Praha', 'Brno - mesto') GROUP BY 1, 2")
    n = assert_frames_match(df, src, ["month", "district"], ["txn_count", "total_amount"], "dice cells")
    return f"{n} month x district cells match source for Jan-Mar 1998"


def di04(ctx):
    filters = {"amount_min": 5000, "amount_max": 10000, "types": ["Debit (Withdrawal)"]}
    df = ctx.run(q.dice(filters, "region"))
    rng = ctx.wh_sql("SELECT MIN(f.amount) lo, MAX(f.amount) hi FROM transaction_fact f "
                     "JOIN transaction_type tt ON tt.type_key = f.type_key "
                     "WHERE f.amount BETWEEN 5000 AND 10000 AND tt.transaction_type = 'Debit (Withdrawal)'").iloc[0]
    assert 5000 <= rng.lo and rng.hi <= 10000
    src = ctx.src_sql(f"SELECT di.region AS region, {SRC_MEASURES} {SRC_FROM} WHERE t.amount BETWEEN 5000 AND 10000 "
                      f"AND {SRC_TYPE} = 'Debit (Withdrawal)' GROUP BY 1")
    n = assert_frames_match(df, src, ["region"], ["txn_count", "total_amount"], "dice by region")
    assert (df.total_credit == 0).all()
    return f"{n} regions match source; amount range inclusive ({rng.lo:,.0f}-{rng.hi:,.0f}); no credits"


def di05(ctx):
    df = ctx.run(q.dice({"regions": ["Prague"], "amount_min": 90000}, "year"))
    mx = ctx.src_sql("SELECT MAX(amount) m FROM trans").m.iloc[0]
    assert df.empty
    return f"0 rows (largest transaction is {mx:,.0f} < 90,000); no error"


def di06(ctx):
    m1 = expect_error(lambda: q.dice({"regions": ["Prague"]}, "year"), "at least two different dimensions")
    m2 = expect_error(lambda: q.dice({"year_from": 1998, "year_to": 1995, "types": ["Credit"]}, "region"),
                      "invalid year range")
    m3 = expect_error(lambda: q.dice({"amount_min": 5000, "amount_max": 100, "types": ["Credit"]}, "region"),
                      "invalid amount range")
    m4 = expect_error(lambda: q.dice({"amount_min": -5, "types": ["Credit"]}, "region"), "cannot be negative")
    m5 = expect_error(lambda: q.dice({"colour": ["red"], "types": ["Credit"]}, "region"), "unknown filter")
    return f"5 invalid inputs rejected, e.g. '{m1}'"


# pivot
def _region_year(ctx, measure="total_amount"):
    oq = q.pivot("region", "year", measure, pivot_members(ctx, "year"))
    return oq, ctx.run(oq)


def pv01(ctx):
    oq, df = _region_year(ctx)
    years = [c for c in oq.measure_columns if c != "Total"]
    body = df[df.region != "Total"]
    src = ctx.src_sql(f"SELECT di.region AS region, YEAR(t.newdate) AS year, SUM({AMT}) AS v "
                      f"{SRC_FROM} GROUP BY 1, 2")
    cells = 0
    for _, s in src.iterrows():
        actual = body[body.region == s.region][str(s.year)].iloc[0]
        assert_close(actual, s.v, f"cell {s.region}/{s.year}")
        cells += 1
    for _, r in body.iterrows():
        assert_close(sum(r[y] for y in years), r.Total, f"row total {r.region}")
    total_row = df[df.region == "Total"].iloc[0]
    for y in years + ["Total"]:
        assert_close(body[y].sum(), total_row[y], f"column total {y}")
    return f"{cells} cells match source; every row total and column total is consistent; grand total {money(total_row.Total)}"


def pv02(ctx):
    oq1, a = _region_year(ctx)
    oq2 = q.pivot("year", "region", "total_amount", pivot_members(ctx, "region"))
    b = ctx.run(oq2)
    a_m = a[a.region != "Total"].set_index("region")[[c for c in oq1.measure_columns if c != "Total"]]
    b_m = b[b.year != "Total"].set_index("year")[[c for c in oq2.measure_columns if c != "Total"]]
    diff = (a_m.T.sort_index().sort_index(axis=1).values - b_m.sort_index().sort_index(axis=1).values)
    assert abs(diff).max() <= TOL, "swapped pivot is not the transpose"
    return f"region x year ({a_m.shape[0]}x{a_m.shape[1]}) swapped to year x region ({b_m.shape[0]}x{b_m.shape[1]}) is its exact transpose"


def pv03(ctx):
    filters = {"year_from": 1998, "year_to": 1998}
    oq = q.pivot("operation", "month", "txn_count", pivot_members(ctx, "month", filters), filters)
    df = ctx.run(oq)
    assert len(oq.measure_columns) == 13, "expected 12 months + Total"
    total = df[df.operation == "Total"].Total.iloc[0]
    src_total = ctx.src_sql("SELECT COUNT(*) n FROM trans WHERE YEAR(newdate) = 1998").n.iloc[0]
    assert total == src_total, f"1998 total {total} != source {src_total}"
    jan = df[df.operation == "Withdrawal in Cash"]["January"].iloc[0]
    src_jan = ctx.src_sql("SELECT COUNT(*) n FROM trans WHERE YEAR(newdate) = 1998 AND MONTH(newdate) = 1 "
                          "AND operation = 'Withdrawal in Cash'").n.iloc[0]
    assert jan == src_jan
    assert all(float(v).is_integer() for v in df[oq.measure_columns].values.flatten())
    return f"7 operations x 12 months (1998); total {int(total):,} = source; Withdrawal in Cash/January = {int(jan):,} = source; all counts integers"


def pv04(ctx):
    oq = q.pivot("type", "quarter", "net_flow", pivot_members(ctx, "quarter"))
    df = ctx.run(oq).set_index("type")
    qs = ["Q1", "Q2", "Q3", "Q4"]
    assert list(oq.measure_columns) == qs + ["Total"]
    assert (df.loc["Credit", qs] > 0).all() and (df.loc["Debit (Withdrawal)", qs] < 0).all()
    src = ctx.src_sql(f"SELECT SUM(CASE WHEN {SRC_TYPE}='Credit' THEN {AMT} ELSE -{AMT} END) v FROM trans t").v.iloc[0]
    assert_close(df.loc["Total", "Total"], src, "overall net flow")
    return f"Credit row positive, Debit row negative in every quarter; overall net flow {money(src)} = source"


def pv05(ctx):
    oq = q.pivot("frequency", "type", "avg_amount", pivot_members(ctx, "type"))
    df = ctx.run(oq).set_index("frequency")
    src = ctx.src_sql(f"SELECT ac.frequency AS frequency, {SRC_TYPE} AS type, ROUND(AVG({AMT}), 2) v "
                      f"{SRC_FROM} GROUP BY 1, 2")
    for _, s in src.iterrows():
        assert_close(df.loc[s.frequency, s.type], s.v, f"avg {s.frequency}/{s.type}")
    overall = ctx.src_sql(f"SELECT ac.frequency AS frequency, ROUND(AVG({AMT}), 2) v {SRC_FROM} GROUP BY 1")
    for _, s in overall.iterrows():
        assert_close(df.loc[s.frequency, "Total"], s.v, f"row total avg {s.frequency}")
    return f"{len(src)} average cells match source; row totals are true averages (not average of averages)"


def pv06(ctx):
    m1 = expect_error(lambda: q.pivot("region", "region", "txn_count", [("Prague", "Prague")]), "must be different")
    m2 = expect_error(lambda: q.pivot("region", "district", "txn_count", [("x", "x")]), "cannot be used as pivot columns")
    filters = {"year_from": 2005, "year_to": 2005}
    members = pivot_members(ctx, "type", filters)
    assert members == []
    m3 = expect_error(lambda: q.pivot("region", "type", "txn_count", members, filters), "no column members")
    return f"3 invalid pivots rejected (same level, too-many-member columns, empty filter), e.g. '{m2}'"


# NULL / Unknown handling
def nv01(ctx):
    nulls = ctx.wh_sql("SELECT SUM(date_key IS NULL)+SUM(account_key IS NULL)+SUM(type_key IS NULL)"
                       "+SUM(operation_key IS NULL) n FROM transaction_fact").n.iloc[0]
    assert nulls == 0, f"{nulls} NULL foreign keys in fact table"
    total = ctx.wh_sql("SELECT COUNT(*) n FROM transaction_fact").n.iloc[0]
    for level in ["year", "region", "type", "operation"]:
        df = ctx.run(q.rollup(q.LEVELS[level].dimension if level != "region" else "location", level, ["txn_count"]))
        g = df[df.aggregation_level == "All"].txn_count.iloc[0]
        assert g == total, f"{level} roll-up drops rows: {g} vs {total}"
    unknown = ctx.wh_sql("SELECT (SELECT COUNT(*) FROM account WHERE account_key=-1) + "
                         "(SELECT COUNT(*) FROM transaction_type WHERE type_key=-1) + "
                         "(SELECT COUNT(*) FROM transaction_operation WHERE operation_key=-1) n").n.iloc[0]
    assert unknown == 3
    return f"0 NULL foreign keys; 'Unknown' members exist; every hierarchy's grand total = {int(total):,} (no rows lost in joins)"


def nv02(ctx):
    oq = q.pivot("type", "operation", "avg_amount", pivot_members(ctx, "operation"))
    df = ctx.run(oq).set_index("type")
    assert pd.isna(df.loc["Credit", "Withdrawal in Cash"]), "AVG of an empty cell must be NULL"
    oq2 = q.pivot("type", "operation", "txn_count", pivot_members(ctx, "operation"))
    df2 = ctx.run(oq2).set_index("type")
    assert df2.loc["Credit", "Withdrawal in Cash"] == 0, "COUNT of an empty cell must be 0"
    return "empty pivot cell: AVG = NULL (no data), COUNT = 0 - conditional aggregation handles NULLs correctly"


# dashboard checks
def _app():
    from streamlit.testing.v1 import AppTest
    from db import PROJECT_ROOT
    at = AppTest.from_file(str(PROJECT_ROOT / "streamlit_app.py"), default_timeout=180)
    at.run()
    return at


def _shown(at):
    """Returns the one results table shown on the page."""
    assert len(at.dataframe) == 1, f"expected 1 table on the page, found {len(at.dataframe)}"
    return at.dataframe[0].value.reset_index(drop=True)


def _same(shown, direct, what):
    assert list(shown.columns) == list(direct.columns), f"{what}: columns differ"
    assert len(shown) == len(direct), f"{what}: {len(shown)} rows shown vs {len(direct)} from SQL"
    for c in direct.columns:
        for a, b in zip(shown[c], direct[c]):
            if isinstance(b, (int, float)) and not isinstance(b, bool):
                assert close(a, b), f"{what}: column {c} shows {a}, SQL gives {b}"
            else:
                assert str(a) == str(b), f"{what}: column {c} shows {a}, SQL gives {b}"


def ui01(ctx):
    at = _app()
    at.sidebar.radio(key="operation").set_value("Slice").run()
    at.selectbox(key="sl_level").set_value("year").run()
    at.selectbox(key="sl_value_year").set_value(1997).run()
    at.selectbox(key="sl_by").set_value("region").run()
    shown = _shown(at)
    direct = ctx.run(q.slice_("year", 1997, "region", q.DEFAULT_MEASURES + ["avg_amount", "active_accounts"]))
    _same(shown, direct, "slice table")
    return f"dashboard Slice table ({len(shown)} rows x {len(shown.columns)} cols) identical to direct SQL"


def ui02(ctx):
    at = _app()
    at.sidebar.radio(key="operation").set_value("Pivot").run()
    shown = _shown(at)
    oq = q.pivot("region", "year", "total_amount", pivot_members(ctx, "year"))
    _same(shown, ctx.run(oq), "pivot table")
    at.button(key="pv_swap").click().run()
    shown2 = _shown(at)
    oq2 = q.pivot("year", "region", "total_amount", pivot_members(ctx, "region"))
    _same(shown2, ctx.run(oq2), "swapped pivot table")
    return "dashboard Pivot (region x year) and its swap (year x region) identical to direct SQL"


def ui03(ctx):
    at = _app()
    at.sidebar.radio(key="operation").set_value("Drill-down").run()
    at.selectbox(key="dd_child_time").set_value(1995).run()
    at.button(key="dd_down").click().run()
    crumb = [m.value for m in at.markdown if m.value.startswith("**Current position")][0]
    assert "1995" in crumb, crumb
    shown = _shown(at)
    direct = ctx.run(q.drilldown("time", [1995], q.DEFAULT_MEASURES + ["active_accounts"]))
    _same(shown, direct, "drill-down table")
    return f"after drilling into 1995 the dashboard shows {len(shown)} quarters identical to direct SQL; breadcrumb '{crumb}'"


def ui04(ctx):
    at = _app()
    at.sidebar.radio(key="operation").set_value("Dice").run()
    at.slider(key="di_years").set_value((1993, 1998)).run()
    warnings = [w.value for w in at.warning]
    assert any("at least two different dimensions" in w for w in warnings), warnings
    assert len(at.dataframe) == 0, "no table should be shown"
    at.multiselect(key="di_types").set_value(["Credit"]).run()
    shown = _shown(at)
    direct = ctx.run(q.dice({"regions": ["Prague", "south Moravia"], "types": ["Credit"]}, "region",
                            q.DEFAULT_MEASURES + ["avg_amount", "active_accounts"]))
    _same(shown, direct, "dice table")
    return "single-dimension dice shows a warning and no table; adding a 2nd dimension shows a table identical to direct SQL"


def ui05(ctx):
    at = _app()
    shown = _shown(at)
    direct = ctx.run(q.rollup("time", "quarter", q.DEFAULT_MEASURES))
    for c in ["year", "quarter"]:
        direct[c] = direct[c].astype("Int64")
    _same(shown, direct, "roll-up table")
    return f"default Roll-up table ({len(shown)} rows incl. subtotals) identical to direct SQL"


# list of all test cases
STEP_BUILD = "1. Build the query with olap.queries.{fn}({args})\n2. Execute it on finance_mco1"
STEP_ORACLE = "3. Compute the expected values independently from the source DB financedata\n4. Compare row by row"
STEP_CONSIST = "3. Check internal consistency of the result"
STEP_ERR = "1. Call the builder with each invalid parameter set\n2. Confirm a ValueError with a clear message is raised"
STEP_UI = ("1. Load streamlit_app.py with Streamlit AppTest (headless)\n2. Set the widgets as listed\n"
           "3. Read the table displayed by the dashboard\n4. Run the same builder directly on the warehouse and compare")

CASES = [
    # roll-up
    Case("RU-01", "Roll-up", "Quarter -> Year -> All subtotals are correct and the grand total equals the source",
         "hierarchy=time, level=quarter, measures=default", STEP_BUILD.format(fn="rollup", args="'time','quarter'")
         + "\n" + STEP_CONSIST + " and compare the grand total with financedata.trans",
         "Each Year row = sum of its Quarter rows; All = sum of Years = 1,056,320 txns and source total amount", ru01),
    Case("RU-02", "Roll-up", "Year subtotals of a Month-level roll-up match the source per year",
         "hierarchy=time, level=month", STEP_BUILD.format(fn="rollup", args="'time','month'") + "\n" + STEP_ORACLE,
         "6 year subtotals equal source count, amount, credits and debits; month_name only on month rows", ru02),
    Case("RU-03", "Roll-up", "Location hierarchy rolls districts up into regions correctly",
         "hierarchy=location, level=district", STEP_BUILD.format(fn="rollup", args="'location','district'") + "\n" + STEP_ORACLE,
         "8 region subtotals equal source; one detail row per district with accounts; region = sum of districts", ru03),
    Case("RU-04", "Roll-up", "Transaction hierarchy roll-up and cleaning of VYBER / blank operations",
         "hierarchy=transaction, level=operation", STEP_BUILD.format(fn="rollup", args="'transaction','operation'") + "\n" + STEP_ORACLE,
         "Type subtotals equal source with VYBER counted as Debit; blank source operations = 'Interest Credited' count", ru04),
    Case("RU-05", "Roll-up", "Roll-up respects a time filter",
         "hierarchy=location, level=region, filters={year_from:1997, year_to:1997}",
         STEP_BUILD.format(fn="rollup", args="'location','region', filters") + "\n" + STEP_ORACLE,
         "Grand total equals the source totals for 1997 only", ru05),
    Case("RU-06", "Roll-up", "Invalid roll-up parameters are rejected",
         "(time, region) / (weather, year) / measure 'profit'", STEP_ERR,
         "ValueError for a level outside the hierarchy, an unknown hierarchy and an unknown measure", ru06),
    # drill-down
    Case("DD-01", "Drill-down", "Top level of the time hierarchy (All -> Year) is correct",
         "hierarchy=time, path=[]", STEP_BUILD.format(fn="drilldown", args="'time', []") + "\n" + STEP_ORACLE,
         "6 years equal source; shares sum to 100%; first year has NULL previous-period value", dd01),
    Case("DD-02", "Drill-down", "Drilling Year 1997 -> Quarters keeps totals consistent",
         "hierarchy=time, path=[1997]", STEP_BUILD.format(fn="drilldown", args="'time', [1997]") + "\n" + STEP_ORACLE,
         "Quarters 1-4; their sum equals the 1997 row of DD-01 and the source", dd02),
    Case("DD-03", "Drill-down", "Drilling 1997 Q2 -> Months, with correct period-over-period change",
         "hierarchy=time, path=[1997, 2]", STEP_BUILD.format(fn="drilldown", args="'time', [1997, 2]") + "\n" + STEP_ORACLE,
         "Only April, May, June; values equal source; pct_change_vs_prev = (May-Apr)/Apr", dd03),
    Case("DD-04", "Drill-down", "Drilling to the finest level (days of May 1997)",
         "hierarchy=time, path=[1997, 2, 5]", STEP_BUILD.format(fn="drilldown", args="'time', [1997, 2, 5]") + "\n" + STEP_CONSIST,
         "31 days, all in May 1997; their sum equals the May row of DD-03", dd04),
    Case("DD-05", "Drill-down", "Location drill-down Region -> District -> Account",
         "path=['south Moravia'] then ['south Moravia', 'Brno - mesto']",
         STEP_BUILD.format(fn="drilldown", args="'location', path") + "\n" + STEP_ORACLE,
         "14 districts summing to the region; one row per account of Brno - mesto summing to the district", dd05),
    Case("DD-06", "Drill-down", "Transaction drill-down Type -> Operation",
         "path=['Credit'] and ['Debit (Withdrawal)']", STEP_BUILD.format(fn="drilldown", args="'transaction', [type]") + "\n" + STEP_ORACLE,
         "3 credit and 3 debit operations; Withdrawal in Cash count equals source (incl. former VYBER rows)", dd06),
    Case("DD-07", "Drill-down", "Invalid drill paths and members with no data",
         "path below finest level; year 'abc'; year 2005", STEP_ERR + "\n3. Run a drill-down on a year with no data",
         "ValueError for invalid paths; an empty result (not an error) for year 2005", dd07),
    # slice
    Case("SL-01", "Slice", "Slice Year = 1997 viewed by Region",
         "level=year, value=1997, by=region", STEP_BUILD.format(fn="slice_", args="'year', 1997, 'region'") + "\n" + STEP_ORACLE,
         "8 regions equal source for 1997; shares sum to 100%; ranks ordered", sl01),
    Case("SL-02", "Slice", "Slice Region = Prague viewed by Month with running total",
         "level=region, value=Prague, by=month", STEP_BUILD.format(fn="slice_", args="'region', 'Prague', 'month'") + "\n" + STEP_ORACLE,
         "12 months equal source; final running total equals the Prague total", sl02),
    Case("SL-03", "Slice", "Slice Type = Credit contains only credits",
         "level=type, value=Credit, by=year", STEP_BUILD.format(fn="slice_", args="'type', 'Credit', 'year'") + "\n" + STEP_ORACLE,
         "Debits are 0, net flow = credits, years equal source", sl03),
    Case("SL-04", "Slice", "Slice on an account attribute (statement frequency)",
         "level=frequency, value=Weekly, by=type", STEP_BUILD.format(fn="slice_", args="'frequency', 'Weekly', 'type'") + "\n" + STEP_ORACLE,
         "Transaction types equal source for weekly-statement accounts", sl04),
    Case("SL-05", "Slice", "Slice on a member that does not exist returns an empty result",
         "level=region, value=Atlantis, by=year", STEP_BUILD.format(fn="slice_", args="'region', 'Atlantis', 'year'"),
         "0 rows and no error", sl05),
    Case("SL-06", "Slice", "Invalid slice parameters are rejected",
         "same level twice; missing value; quarter 'x'; level 'planet'", STEP_ERR,
         "ValueError for all 4", sl06),
    # dice
    Case("DI-01", "Dice", "Dice on 4 dimensions (time range, regions, type, amount) by Region x Operation",
         "years 1996-1997, regions [Prague, south Moravia], types [Credit], amount >= 1000",
         STEP_BUILD.format(fn="dice", args="filters, 'region', by_level2='operation'") + "\n" + STEP_ORACLE,
         "Every cell equals the source; shares within each region sum to 100%", di01),
    Case("DI-02", "Dice", "Dice on quarters, operation and account frequency",
         "quarters [1,4], operations [Withdrawal in Cash], frequencies [Monthly]",
         STEP_BUILD.format(fn="dice", args="filters, 'year'") + "\n" + STEP_ORACLE, "Years equal source", di02),
    Case("DI-03", "Dice", "Dice with a date range and districts",
         "date 1998-01-01..1998-03-31, districts [Hl.m. Praha, Brno - mesto]",
         STEP_BUILD.format(fn="dice", args="filters, 'month', by_level2='district'") + "\n" + STEP_ORACLE,
         "Only months 1-3; cells equal source", di03),
    Case("DI-04", "Dice", "Dice with an amount range is inclusive and exact",
         "amount 5000..10000, types [Debit (Withdrawal)]", STEP_BUILD.format(fn="dice", args="filters, 'region'") + "\n" + STEP_ORACLE,
         "All amounts within the range; regions equal source; no credits", di04),
    Case("DI-05", "Dice", "Dice that matches nothing returns an empty result",
         "regions [Prague], amount >= 90000", STEP_BUILD.format(fn="dice", args="filters, 'year'"),
         "0 rows and no error", di05),
    Case("DI-06", "Dice", "Invalid dice parameters are rejected",
         "single dimension; year 1998..1995; amount 5000..100; amount -5; unknown filter", STEP_ERR,
         "ValueError for all 5", di06),
    # pivot
    Case("PV-01", "Pivot", "Region x Year pivot of total amount with row/column totals",
         "rows=region, cols=year, measure=total_amount", STEP_BUILD.format(fn="pivot", args="'region', 'year', 'total_amount', members") + "\n" + STEP_ORACLE,
         "Each cell equals the source; Total column = row sum; Total row = column sums", pv01),
    Case("PV-02", "Pivot", "Swapping rows and columns rotates the report",
         "region x year vs year x region", STEP_BUILD.format(fn="pivot", args="both orientations") + "\n3. Compare matrices",
         "The swapped pivot is the exact transpose", pv02),
    Case("PV-03", "Pivot", "Operation x Month pivot of transaction counts for 1998",
         "rows=operation, cols=month, measure=txn_count, years 1998..1998",
         STEP_BUILD.format(fn="pivot", args="'operation', 'month', 'txn_count', members, filters") + "\n" + STEP_ORACLE,
         "12 month columns + Total; total = 1998 source count; counts are integers", pv03),
    Case("PV-04", "Pivot", "Type x Quarter pivot of net flow (signed measure)",
         "rows=type, cols=quarter, measure=net_flow", STEP_BUILD.format(fn="pivot", args="'type', 'quarter', 'net_flow', members") + "\n" + STEP_ORACLE,
         "Credit row positive, Debit row negative; overall net flow equals source", pv04),
    Case("PV-05", "Pivot", "Frequency x Type pivot of average amount (non-additive measure)",
         "rows=frequency, cols=type, measure=avg_amount", STEP_BUILD.format(fn="pivot", args="'frequency', 'type', 'avg_amount', members") + "\n" + STEP_ORACLE,
         "Cells and row totals equal true source averages", pv05),
    Case("PV-06", "Pivot", "Invalid pivot parameters are rejected",
         "rows=cols; cols=district; filter year 2005 (no columns)", STEP_ERR, "ValueError for all 3", pv06),
    # NULL / Unknown handling
    Case("NV-01", "All", "No fact rows are lost because of NULL dimension keys",
         "fact table; roll-ups of year, region, type, operation", "1. Count NULL foreign keys in transaction_fact\n"
         "2. Run a roll-up on each dimension and compare its grand total with COUNT(*)",
         "0 NULL keys; Unknown members exist; every grand total = total fact rows", nv01),
    Case("NV-02", "Pivot", "Empty pivot cells: AVG gives NULL, COUNT gives 0",
         "rows=type, cols=operation, measures avg_amount and txn_count",
         STEP_BUILD.format(fn="pivot", args="'type', 'operation', measure, members") + "\n3. Inspect the Credit x Withdrawal in Cash cell",
         "AVG cell is NULL, COUNT cell is 0", nv02),
    # dashboard vs warehouse
    Case("UI-01", "Slice", "Dashboard Slice output equals the warehouse query",
         "Slice on Year = 1997, view by Region", STEP_UI, "Displayed table identical to direct SQL result", ui01),
    Case("UI-02", "Pivot", "Dashboard Pivot output (and Swap) equals the warehouse query",
         "Pivot rows=Region, cols=Year, Total amount; then Swap", STEP_UI, "Both displayed tables identical to direct SQL", ui02),
    Case("UI-03", "Drill-down", "Dashboard drill-down navigation shows the correct children",
         "Drill-down Time: select 1995, click Drill down", STEP_UI, "Breadcrumb shows 1995; quarters identical to direct SQL", ui03),
    Case("UI-04", "Dice", "Dashboard rejects a single-dimension dice and accepts a valid one",
         "Dice: full year range + regions only, then add type Credit", STEP_UI,
         "Warning and no table first; then table identical to direct SQL", ui04),
    Case("UI-05", "Roll-up", "Dashboard Roll-up output equals the warehouse query",
         "Roll-up default (Time, Quarter)", STEP_UI, "Displayed table identical to direct SQL", ui05),
]
