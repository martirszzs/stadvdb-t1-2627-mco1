"olap > export_sql.py"
"""Writes finance-db/STADVDB-MCO1-Group7-OLAP-Queries.sql with an example query
for each OLAP operation. The parameters are filled in so the queries can be
run straight in MySQL Workbench.

    python -m olap.export_sql
"""
import datetime as dt
from pathlib import Path

from db import PROJECT_ROOT, run_query
from olap import queries as q

OUT = PROJECT_ROOT / "finance-db" / "STADVDB-MCO1-Group7-OLAP-Queries.sql"


def literal(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(v)
    if isinstance(v, dt.date):
        return f"'{v.isoformat()}'"
    return "'" + str(v).replace("'", "''") + "'"


def inline(oq):
    parts = oq.sql.split("%s")
    assert len(parts) == len(oq.params) + 1
    out = parts[0]
    for value, rest in zip(oq.params, parts[1:]):
        out += literal(value) + rest
    return out


def main():
    sql, params = q.pivot_columns_query("year")
    years = list(run_query(sql, params).itertuples(index=False, name=None))
    examples = [
        ("ROLL-UP", "Time hierarchy rolled up Month -> Quarter -> Year -> All",
         q.rollup("time", "month")),
        ("ROLL-UP", "Location hierarchy rolled up District -> Region -> All, for 1997",
         q.rollup("location", "district", filters={"year_from": 1997, "year_to": 1997})),
        ("DRILL-DOWN", "All years -> Year level",
         q.drilldown("time", [])),
        ("DRILL-DOWN", "Year 1997 > Q2 -> its months (share of parent, rank, change vs previous month)",
         q.drilldown("time", [1997, 2])),
        ("DRILL-DOWN", "Region south Moravia -> its districts",
         q.drilldown("location", ["south Moravia"])),
        ("SLICE", "Year = 1997, analysed by Region",
         q.slice_("year", 1997, "region")),
        ("SLICE", "Region = Prague, analysed by Month (with running total)",
         q.slice_("region", "Prague", "month")),
        ("DICE", "Years 1996-1997 x regions Prague/south Moravia x type Credit x amount >= 1000, "
                 "by Region and Operation",
         q.dice({"year_from": 1996, "year_to": 1997, "regions": ["Prague", "south Moravia"],
                 "types": ["Credit"], "amount_min": 1000}, "region", by_level2="operation")),
        ("PIVOT", "Total amount: Region (rows) x Year (columns) with totals",
         q.pivot("region", "year", "total_amount", years)),
        ("PIVOT (swapped)", "Total amount: Year (rows) x Region (columns)",
         q.pivot("year", "region", "total_amount",
                 list(run_query(*q.pivot_columns_query("region")).itertuples(index=False, name=None)))),
    ]
    lines = [
        "-- STADVDB MCO1 - Group 7: OLAP queries on finance_mco1",
        "-- made with `python -m olap.export_sql` (same queries the dashboard runs,",
        "-- with example values filled in)",
        "USE finance_mco1;",
        "",
    ]
    for op, desc, oq in examples:
        lines += [f"-- ---------------------------------------------------------------------",
                  f"-- {op}: {desc}",
                  f"-- ---------------------------------------------------------------------",
                  inline(oq) + ";", ""]
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
