# db.py

"""MySQL connection helpers used by the ETL, the dashboard and the tests.

The login details are in .streamlit/secrets.toml (not committed, see
.streamlit/secrets.toml.example).
"""
from decimal import Decimal
from pathlib import Path
import tomllib

import mysql.connector
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent
SECRETS_PATH = PROJECT_ROOT / ".streamlit" / "secrets.toml"


def load_config():
    if not SECRETS_PATH.exists():
        raise FileNotFoundError(
            f"{SECRETS_PATH} not found. Copy .streamlit/secrets.toml.example "
            "to .streamlit/secrets.toml and fill in your MySQL credentials."
        )
    with open(SECRETS_PATH, "rb") as f:
        return tomllib.load(f)["mysql"]


def get_connection(database=None):
    """Open a connection. database can be "source", "warehouse" or None."""
    cfg = load_config()
    db_name = {"source": cfg["source_db"], "warehouse": cfg["warehouse_db"]}.get(database)
    return mysql.connector.connect(
        host=cfg["host"],
        port=int(cfg["port"]),
        user=cfg["user"],
        password=cfg["password"],
        database=db_name,
    )


def run_query(sql, params=None, database="warehouse", conn=None):
    """Run a SELECT and return the result as a DataFrame."""
    own_conn = conn is None
    if own_conn:
        conn = get_connection(database)
    try:
        cur = conn.cursor()
        cur.execute(sql, params or ())
        columns = [c[0] for c in cur.description]
        rows = cur.fetchall()
        cur.close()
        return _decimals_to_float(pd.DataFrame(rows, columns=columns))
    finally:
        if own_conn:
            conn.close()


def _decimals_to_float(df):
    """MySQL gives back Decimal objects for DECIMAL and SUM() columns, so turn
    them into floats for pandas and Plotly."""
    for col in df.columns:
        non_null = df[col].dropna()
        if not non_null.empty and isinstance(non_null.iloc[0], Decimal):
            df[col] = df[col].astype(float)
    return df
