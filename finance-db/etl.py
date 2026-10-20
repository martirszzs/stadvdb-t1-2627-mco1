"""ETL pipeline: financedata (source) -> finance_mco1 (star-schema warehouse).

Run from the project root:
    python finance-db/etl.py

Steps
  1. Create   - (re)creates the warehouse tables using STADVDB-MCO1-Group7-Schema.sql
  2. Extract  - reads trans, account and district from the source database
  3. Transform- cleans, standardizes, merges, splits and converts the data
  4. Load     - bulk-inserts the dimensions, then the fact table
  5. Verify   - reconciles aggregated totals between source and warehouse

Data-quality rules (found by profiling the source data)
  * type 'VYBER' is an untranslated Czech code. All 16,666 such rows are
    "Withdrawal in Cash", so they are standardized to 'Debit (Withdrawal)'.
  * 183,114 rows have a blank operation. Every one of them has
    k_symbol = 'Interest Credited' (interest the bank credits), so the
    operation is derived from k_symbol. Any other blank becomes 'Unknown'.
  * Missing account / district / type / operation values map to the
    'Unknown' member (key -1) instead of NULL, so no fact row is lost in joins.
  * Rows without a date or amount cannot be placed in the cube; they are
    rejected and counted (none exist in the current dataset).
"""
from pathlib import Path
import sys
import time

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import get_connection, load_config  # noqa: E402

SCHEMA_FILE = Path(__file__).resolve().parent / "STADVDB-MCO1-Group7-Schema.sql"
UNKNOWN_KEY = -1
UNKNOWN = "Unknown"
BATCH_SIZE = 20_000

TYPE_FIXES = {"VYBER": "Debit (Withdrawal)"}


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------- helpers
def fetch_df(conn, sql):
    cur = conn.cursor()
    cur.execute(sql)
    cols = [c[0] for c in cur.description]
    df = pd.DataFrame(cur.fetchall(), columns=cols)
    cur.close()
    return df


def clean_text(series):
    """Trim whitespace and turn empty strings into NULL (NaN)."""
    s = series.astype("string").str.strip()
    return s.mask(s == "")


def insert_rows(conn, table, columns, rows):
    col_list = ", ".join(f"`{c}`" for c in columns)
    placeholders = ", ".join(["%s"] * len(columns))
    sql = f"INSERT INTO `{table}` ({col_list}) VALUES ({placeholders})"
    cur = conn.cursor()
    for start in range(0, len(rows), BATCH_SIZE):
        cur.executemany(sql, rows[start:start + BATCH_SIZE])
    conn.commit()
    cur.close()
    log(f"  loaded {len(rows):>9,} rows into {table}")


def df_to_rows(df, columns):
    """DataFrame -> list of tuples with NaN/NA converted to None."""
    out = df[columns].astype(object).where(df[columns].notna(), None)
    return list(out.itertuples(index=False, name=None))


# ---------------------------------------------------------------- 1. create
def create_schema(conn):
    log("1. CREATE  - (re)creating warehouse tables")
    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    statements = []
    for raw in sql.split(";"):
        lines = [ln for ln in raw.splitlines() if not ln.strip().startswith("--")]
        stmt = "\n".join(lines).strip()
        if stmt:
            statements.append(stmt)
    cur = conn.cursor()
    for stmt in statements:
        cur.execute(stmt)
    conn.commit()
    cur.close()


# ---------------------------------------------------------------- 2. extract
def extract(src):
    log("2. EXTRACT - reading source tables")
    trans = fetch_df(src, """
        SELECT trans_id, account_id, newdate, type, operation,
               amount, balance, k_symbol
        FROM trans""")
    account = fetch_df(src, "SELECT account_id, district_id, frequency, newdate FROM account")
    district = fetch_df(src, "SELECT district_id, district_name, region FROM district")
    log(f"  trans={len(trans):,}  account={len(account):,}  district={len(district):,}")
    return trans, account, district


# ---------------------------------------------------------------- 3. transform
def transform(trans, account, district):
    log("3. TRANSFORM")
    stats = {}

    # ---- account dimension: merge account + district (location hierarchy)
    account = account.copy()
    account["frequency"] = clean_text(account["frequency"]).fillna(UNKNOWN)
    account["newdate"] = pd.to_datetime(account["newdate"], errors="coerce").dt.date
    district = district.copy()
    district["district_name"] = clean_text(district["district_name"])
    district["region"] = clean_text(district["region"])

    dim_account = account.merge(district, on="district_id", how="left")
    stats["accounts_without_district"] = int(dim_account["district_name"].isna().sum())
    dim_account["district_name"] = dim_account["district_name"].fillna(UNKNOWN)
    dim_account["region"] = dim_account["region"].fillna(UNKNOWN)
    dim_account = dim_account.sort_values("account_id").reset_index(drop=True)
    dim_account["account_key"] = range(1, len(dim_account) + 1)
    dim_account = dim_account.rename(columns={
        "frequency": "account_frequency", "newdate": "account_open_date"})
    unknown_account = pd.DataFrame([{
        "account_key": UNKNOWN_KEY, "account_id": UNKNOWN_KEY,
        "account_frequency": UNKNOWN, "account_open_date": None,
        "district_id": None, "district_name": UNKNOWN, "region": UNKNOWN}])
    dim_account = pd.concat([unknown_account, dim_account], ignore_index=True)
    dim_account["district_id"] = dim_account["district_id"].astype("Int64")
    log(f"  account dim: {len(dim_account):,} rows "
        f"({stats['accounts_without_district']} accounts without a district -> Unknown)")

    # ---- transactions: cleaning and type conversion
    t = trans.copy()
    before = len(t)
    t = t.drop_duplicates(subset="trans_id")
    stats["duplicate_trans_ids"] = before - len(t)

    t["newdate"] = pd.to_datetime(t["newdate"], errors="coerce")
    t["amount"] = pd.to_numeric(t["amount"], errors="coerce").round(2)
    t["balance"] = pd.to_numeric(t["balance"], errors="coerce").round(2)

    rejected = t["newdate"].isna() | t["amount"].isna() | t["balance"].isna()
    stats["rejected_rows"] = int(rejected.sum())
    t = t[~rejected].copy()

    t["type"] = clean_text(t["type"])
    stats["vyber_standardized"] = int((t["type"] == "VYBER").sum())
    t["type"] = t["type"].replace(TYPE_FIXES).fillna(UNKNOWN)

    t["operation"] = clean_text(t["operation"])
    t["k_symbol"] = clean_text(t["k_symbol"])
    interest = t["operation"].isna() & (t["k_symbol"] == "Interest Credited")
    stats["operation_from_k_symbol"] = int(interest.sum())
    t.loc[interest, "operation"] = "Interest Credited"
    stats["operation_unknown"] = int(t["operation"].isna().sum())
    t["operation"] = t["operation"].fillna(UNKNOWN)

    log(f"  duplicates removed={stats['duplicate_trans_ids']}  "
        f"rejected (no date/amount/balance)={stats['rejected_rows']}")
    log(f"  type 'VYBER' -> 'Debit (Withdrawal)': {stats['vyber_standardized']:,}")
    log(f"  blank operation -> 'Interest Credited' (from k_symbol): "
        f"{stats['operation_from_k_symbol']:,}   remaining blanks -> Unknown: "
        f"{stats['operation_unknown']:,}")

    # ---- date dimension: split dates into the time hierarchy.
    # Contiguous calendar covering every transaction and account-open date.
    open_dates = pd.to_datetime(dim_account["account_open_date"], errors="coerce").dropna()
    start = min(t["newdate"].min(), open_dates.min())
    end = max(t["newdate"].max(), open_dates.max())
    cal = pd.DataFrame({"full_date": pd.date_range(start, end, freq="D")})
    cal["date_key"] = cal["full_date"].dt.strftime("%Y%m%d").astype(int)
    cal["day_number"] = cal["full_date"].dt.day
    cal["month_number"] = cal["full_date"].dt.month
    cal["month_name"] = cal["full_date"].dt.strftime("%B")
    cal["quarter_number"] = cal["full_date"].dt.quarter
    cal["year_number"] = cal["full_date"].dt.year
    cal["quarter_label"] = cal["year_number"].astype(str) + "-Q" + cal["quarter_number"].astype(str)
    cal["month_label"] = cal["full_date"].dt.strftime("%Y-%m")
    cal["full_date"] = cal["full_date"].dt.date
    dim_date = cal
    log(f"  date dim: {len(dim_date):,} days ({start.date()} to {end.date()})")

    # ---- small dimensions: distinct values + surrogate keys
    def small_dim(values, key_col, name_col):
        names = sorted(v for v in set(values) if v != UNKNOWN)
        dim = pd.DataFrame({name_col: [UNKNOWN] + names})
        dim[key_col] = [UNKNOWN_KEY] + list(range(1, len(names) + 1))
        return dim

    dim_type = small_dim(t["type"], "type_key", "transaction_type")
    dim_op = small_dim(t["operation"], "operation_key", "operation_name")
    log(f"  transaction_type: {sorted(dim_type['transaction_type'])}")
    log(f"  transaction_operation: {sorted(dim_op['operation_name'])}")

    # ---- fact table: replace natural keys with surrogate keys
    acct_map = dict(zip(dim_account["account_id"], dim_account["account_key"]))
    type_map = dict(zip(dim_type["transaction_type"], dim_type["type_key"]))
    op_map = dict(zip(dim_op["operation_name"], dim_op["operation_key"]))

    fact = pd.DataFrame({
        "trans_id": t["trans_id"].astype(int),
        "date_key": t["newdate"].dt.strftime("%Y%m%d").astype(int),
        "account_key": t["account_id"].map(acct_map).fillna(UNKNOWN_KEY).astype(int),
        "type_key": t["type"].map(type_map).astype(int),
        "operation_key": t["operation"].map(op_map).astype(int),
        "amount": t["amount"],
        "balance": t["balance"],
    })
    stats["fact_unknown_account"] = int((fact["account_key"] == UNKNOWN_KEY).sum())
    log(f"  fact rows: {len(fact):,} "
        f"(transactions with unknown account: {stats['fact_unknown_account']})")

    # cleaned transactions are kept for the reconciliation step
    return dim_account, dim_date, dim_type, dim_op, fact, t, stats


# ---------------------------------------------------------------- 4. load
def load(wh, dim_account, dim_date, dim_type, dim_op, fact):
    log("4. LOAD")
    insert_rows(wh, "account",
                ["account_key", "account_id", "account_frequency", "account_open_date",
                 "district_id", "district_name", "region"],
                df_to_rows(dim_account, ["account_key", "account_id", "account_frequency",
                                         "account_open_date", "district_id",
                                         "district_name", "region"]))
    date_cols = ["date_key", "full_date", "day_number", "month_number", "month_name",
                 "quarter_number", "year_number", "quarter_label", "month_label"]
    insert_rows(wh, "date", date_cols, df_to_rows(dim_date, date_cols))
    insert_rows(wh, "transaction_type", ["type_key", "transaction_type"],
                df_to_rows(dim_type, ["type_key", "transaction_type"]))
    insert_rows(wh, "transaction_operation", ["operation_key", "operation_name"],
                df_to_rows(dim_op, ["operation_key", "operation_name"]))
    fact_cols = ["trans_id", "date_key", "account_key", "type_key", "operation_key",
                 "amount", "balance"]
    insert_rows(wh, "transaction_fact", fact_cols, df_to_rows(fact, fact_cols))


# ---------------------------------------------------------------- 5. verify
def reconcile(wh, cleaned):
    """Aggregate the cleaned source data in pandas and compare it with the
    same aggregate computed by SQL on the loaded warehouse."""
    log("5. VERIFY  - reconciling source vs warehouse totals by year and type")
    expected = (cleaned.assign(year=cleaned["newdate"].dt.year)
                .groupby(["year", "type"])
                .agg(txn_count=("trans_id", "size"), total_amount=("amount", "sum"))
                .reset_index())
    actual = fetch_df(wh, """
        SELECT d.year_number AS year, tt.transaction_type AS type,
               COUNT(*) AS txn_count, SUM(f.amount) AS total_amount
        FROM transaction_fact f
        JOIN `date` d ON d.date_key = f.date_key
        JOIN transaction_type tt ON tt.type_key = f.type_key
        GROUP BY d.year_number, tt.transaction_type""")
    actual["total_amount"] = actual["total_amount"].astype(float)
    actual["txn_count"] = actual["txn_count"].astype(int)
    actual["type"] = actual["type"].astype("string")
    expected["type"] = expected["type"].astype("string")

    cmp = expected.merge(actual, on=["year", "type"], how="outer",
                         suffixes=("_source", "_warehouse"))
    cmp["ok"] = ((cmp["txn_count_source"] == cmp["txn_count_warehouse"]) &
                 ((cmp["total_amount_source"] - cmp["total_amount_warehouse"]).abs() < 0.005))
    print(cmp.to_string(index=False))
    if not cmp["ok"].all():
        raise SystemExit("RECONCILIATION FAILED - warehouse totals do not match the source")
    log(f"  reconciliation OK: {int(cmp['txn_count_warehouse'].sum()):,} transactions, "
        f"{cmp['total_amount_warehouse'].sum():,.2f} total amount")


def main():
    t0 = time.time()
    cfg = load_config()
    log(f"source={cfg['source_db']}  warehouse={cfg['warehouse_db']}")
    server = get_connection()
    create_schema(server)
    server.close()

    src = get_connection("source")
    trans, account, district = extract(src)
    src.close()

    dim_account, dim_date, dim_type, dim_op, fact, cleaned, _ = transform(trans, account, district)

    wh = get_connection("warehouse")
    load(wh, dim_account, dim_date, dim_type, dim_op, fact)
    reconcile(wh, cleaned)
    wh.close()
    log(f"ETL finished in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
