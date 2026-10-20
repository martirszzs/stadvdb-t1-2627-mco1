"""STADVDB MCO1 Group 7 - OLAP dashboard for the finance_mco1 data warehouse.

Run with:  streamlit run streamlit_app.py

All the numbers come from SQL run on the warehouse. The queries are built in
olap/queries.py and each one is shown under "SQL executed".
"""
import mysql.connector
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from db import run_query
from olap import queries as q

st.set_page_config(page_title="STADVDB MCO1 - OLAP Dashboard", layout="wide")

# chart colors and style
# colorblind-safe colors, always used in this order
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
# each member keeps its color (Credit is always blue, Debit always orange)
TYPE_COLORS = {"Credit": SERIES[0], "Debit (Withdrawal)": SERIES[1], "Unknown": "#898781"}
SEQUENTIAL = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]
DIVERGING = [[0, "#a83232"], [0.17, "#e34948"], [0.33, "#f2a3a2"], [0.5, "#f0efec"],
             [0.67, "#9ec5f4"], [0.83, "#2a78d6"], [1, "#184f95"]]
INK, INK_2, MUTED, GRID, AXIS = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"
MAX_BARS = 80
SIGNED_MEASURES = {"net_flow"}


def style(fig, height=420):
    fig.update_layout(
        template="plotly_white", height=height, margin=dict(l=10, r=10, t=80, b=10),
        title=dict(y=0.98, yref="container", yanchor="top"),
        font=dict(family="system-ui, -apple-system, Segoe UI, sans-serif", color=INK_2, size=13),
        title_font=dict(color=INK, size=15), hoverlabel=dict(font_size=13),
        legend=dict(orientation="h", yanchor="bottom", y=1.01, x=0, title_text=""),
        barcornerradius=4, bargap=0.25, plot_bgcolor="#fcfcfb", paper_bgcolor="rgba(0,0,0,0)",
    )
    fig.update_xaxes(showgrid=False, linecolor=AXIS, tickfont_color=MUTED, title_font_color=INK_2)
    fig.update_yaxes(gridcolor=GRID, zerolinecolor=AXIS, linecolor=AXIS,
                     tickfont_color=MUTED, title_font_color=INK_2)
    fig.update_traces(selector=dict(type="bar"), marker_line_width=0)
    fig.update_traces(selector=dict(type="scatter"), line_width=2, marker_size=8)
    return fig


def mtitle(name):
    return q.MEASURES[name].title if name in q.MEASURES else name.replace("_", " ")


# data access
@st.cache_data(ttl=600, show_spinner=False)
def fetch(sql, params=()):
    return run_query(sql, list(params))


def execute(oq):
    with st.spinner(f"Running {oq.operation} query on the data warehouse..."):
        return fetch(oq.sql, tuple(oq.params))


@st.cache_data(ttl=600, show_spinner=False)
def members(level):
    df = fetch(q.members_query(level))
    if "label" in df.columns:
        return list(zip(df["member"], df["label"]))
    return [(m, m) for m in df["member"]]


def member_values(level):
    return [m for m, _ in members(level)]


def member_label(level, value):
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if level == "quarter":
        return f"Q{value}"
    if level == "month":
        return dict(members("month")).get(value, value)
    return str(value)


@st.cache_data(ttl=600, show_spinner=False)
def location_pairs():
    return fetch(q.location_members_query())


# display helpers
def column_config(df):
    cfg = {}
    for col in df.columns:
        if col in q.MEASURES:
            fmt = "%d" if q.MEASURES[col].agg.startswith("COUNT") else "%,.2f"
            cfg[col] = st.column_config.NumberColumn(mtitle(col), format=fmt)
        elif col.startswith("pct_"):
            cfg[col] = st.column_config.NumberColumn(col.replace("_", " "), format="%.2f%%")
        elif col in ("prev_total_amount", "running_total_amount"):
            cfg[col] = st.column_config.NumberColumn(col.replace("_", " "), format="%,.2f")
        elif col in q.LEVELS:
            cfg[col] = st.column_config.Column(q.LEVELS[col].title)
    return cfg


def show_table(df, key):
    st.dataframe(df, width="stretch", hide_index=True, column_config=column_config(df), key=key)
    st.caption(f"{len(df):,} rows")


def show_sql(oq):
    with st.expander("SQL executed on the data warehouse"):
        st.code(oq.sql, language="sql")
        st.write("Bound parameters:", oq.params if oq.params else "(none)")


def row_labels(df, levels):
    """Label for each row made from its level columns, e.g. 1997 / Q2 / May."""
    def one(row):
        parts = []
        for lv in levels:
            v = row.get(lv)
            if v is None or pd.isna(v):
                continue
            if lv == "month" and "month_name" in row and pd.notna(row["month_name"]):
                parts.append(str(row["month_name"]))
            elif lv == "quarter":
                parts.append(f"Q{int(v)}")
            elif isinstance(v, float) and v.is_integer():
                parts.append(str(int(v)))
            else:
                parts.append(str(v))
        return " / ".join(parts) if parts else "All"
    return df.apply(one, axis=1)


def bar_chart(df, x_labels, measure, title, horizontal=False, color=None, color_map=None):
    plot = df.assign(_label=x_labels.values)
    if len(plot) > MAX_BARS:
        plot = plot.nlargest(MAX_BARS, measure)
        title += f" (top {MAX_BARS} of {len(df):,})"
    kwargs = dict(title=title, labels={measure: mtitle(measure), "_label": ""})
    if color:
        kwargs.update(color=color, color_discrete_map=color_map,
                      category_orders={color: list(color_map)} if color_map else None)
    else:
        kwargs.update(color_discrete_sequence=[SERIES[0]])
    if horizontal:
        plot = plot.sort_values(measure)
        fig = px.bar(plot, x=measure, y="_label", orientation="h", **kwargs)
        fig.update_traces(hovertemplate="%{y}<br>%{x:,.2f}<extra></extra>")
        height = max(320, 22 * len(plot) + 80)
    else:
        fig = px.bar(plot, x="_label", y=measure, **kwargs)
        fig.update_traces(hovertemplate="%{x}<br>%{y:,.2f}<extra></extra>")
        fig.update_xaxes(type="category")
        height = 420
    return style(fig, height)


def line_chart(df, x_labels, measure, title):
    plot = df.assign(_label=x_labels.values)
    fig = px.line(plot, x="_label", y=measure, title=title, markers=len(plot) <= 60,
                  labels={measure: mtitle(measure), "_label": ""},
                  color_discrete_sequence=[SERIES[0]])
    fig.update_traces(hovertemplate="%{x}<br>%{y:,.2f}<extra></extra>")
    fig.update_xaxes(type="category")
    return style(fig)


def labels_of(df, level):
    """Labels for one level column (done per column so ints stay ints)."""
    return pd.Series([member_label(level, v) for v in df[level]], index=df.index)


def heatmap(matrix, measure, title, x_title, y_title):
    signed = measure in SIGNED_MEASURES and (matrix.min().min() < 0)
    fig = go.Figure(go.Heatmap(
        z=matrix.values, x=[str(c) for c in matrix.columns], y=[str(i) for i in matrix.index],
        colorscale=DIVERGING if signed else SEQUENTIAL, zmid=0 if signed else None,
        xgap=2, ygap=2, colorbar=dict(title=mtitle(measure)),
        hovertemplate=f"{y_title}: %{{y}}<br>{x_title}: %{{x}}<br>{mtitle(measure)}: "
                      "%{z:,.2f}<extra></extra>"))
    fig.update_layout(title=title, xaxis_title=x_title, yaxis_title=y_title)
    fig.update_yaxes(autorange="reversed", gridcolor="rgba(0,0,0,0)")
    return style(fig, max(360, 28 * len(matrix) + 140))


def entity_colors(level):
    """Give each member a fixed color based on its place in the full member
    list, so colors don't change when you filter."""
    if level == "type":
        return TYPE_COLORS
    vals = member_values(level)
    if len(vals) > len(SERIES):
        return None
    return {member_label(level, v): SERIES[i] for i, v in enumerate(vals)}


def year_range_filter(key, label="Year range"):
    years = member_values("year")
    lo, hi = st.slider(label, min(years), max(years), (min(years), max(years)), key=key)
    # only filter if the range was narrowed (the unfiltered query is faster)
    if (lo, hi) == (min(years), max(years)):
        return {}
    return {"year_from": lo, "year_to": hi}


def measure_picker(key, default="total_amount", label="Measure"):
    names = list(q.MEASURES)
    return st.selectbox(label, names, index=names.index(default), format_func=mtitle, key=key)


HIERARCHY_TITLES = {"time": "Time (Year > Quarter > Month > Day)",
                    "location": "Location (Region > District > Account)",
                    "transaction": "Transaction (Type > Operation)"}


# roll-up
def render_rollup():
    st.subheader("Roll-up")
    st.write("Aggregates detailed data up the dimension hierarchy - from the chosen "
             "detail level through every coarser level to the grand total "
             "(`GROUP BY ... WITH ROLLUP`, subtotal rows identified with `GROUPING()`).")
    c1, c2, c3 = st.columns([2, 1.3, 2])
    hierarchy = c1.selectbox("Hierarchy", list(q.HIERARCHIES), format_func=HIERARCHY_TITLES.get,
                             key="ru_hierarchy")
    path = q.HIERARCHIES[hierarchy]
    level = c2.selectbox("Detail level (roll up from)", path, index=min(1, len(path) - 1),
                         format_func=lambda n: q.LEVELS[n].title, key="ru_level")
    with c3:
        filters = year_range_filter("ru_years") if hierarchy != "time" else {}
        if hierarchy == "time":
            st.caption("Tip: the Day level returns ~2,200 detail rows plus subtotals.")
    chosen = st.multiselect("Measures", list(q.MEASURES), default=q.DEFAULT_MEASURES,
                            format_func=mtitle, key="ru_measures")

    oq = q.rollup(hierarchy, level, chosen, filters)
    df = execute(oq)
    if df.empty:
        st.info("No data for the selected parameters.")
        return

    levels_in = path[: path.index(level) + 1]
    agg_levels = [q.LEVELS[n].title for n in levels_in]
    c1, c2 = st.columns(2)
    chart_level = c1.radio("Chart granularity (rolled-up view)", agg_levels[::-1] + ["All"],
                           horizontal=True, key="ru_chart_level")
    chart_measure = c2.selectbox("Chart measure", chosen or q.DEFAULT_MEASURES,
                                 format_func=mtitle, key="ru_chart_measure")
    rows = df[df["aggregation_level"] == chart_level]
    labels = row_labels(rows, levels_in)
    title = f"{mtitle(chart_measure)} by {chart_level}"
    if chart_level == "All":
        st.metric(f"Grand total - {mtitle(chart_measure)}", f"{rows[chart_measure].iloc[0]:,.2f}")
    elif hierarchy == "time" and chart_level != agg_levels[0]:
        st.plotly_chart(line_chart(rows, labels, chart_measure, title), width="stretch")
    else:
        top = levels_in[0]
        depth = agg_levels.index(chart_level)
        colors = entity_colors(top) if depth > 0 else None
        if colors:
            own = levels_in[depth]
            own_labels = labels if own in ("quarter", "month") else labels_of(rows, own)
            fig = bar_chart(rows.assign(_group=labels_of(rows, top).values), own_labels,
                            chart_measure, title, color="_group", color_map=colors)
        else:
            fig = bar_chart(rows, labels, chart_measure, title)
        st.plotly_chart(fig, width="stretch")

    st.markdown("**Roll-up report** (detail rows followed by their subtotals)")
    display = df.copy()
    for lv in levels_in:
        if q.LEVELS[lv].value_type is int:
            display[lv] = display[lv].astype("Int64")
    show_table(display, "ru_table")
    show_sql(oq)


# drill-down
def render_drilldown():
    st.subheader("Drill-down")
    st.write("Moves from summarized data to more detailed levels. Pick a member and drill "
             "into its children; each level shows its share of the parent "
             "(`SUM() OVER ()`), rank (`RANK()`), and for time, the change vs the "
             "previous period (`LAG()`).")
    c1, c2 = st.columns([2, 1])
    hierarchy = c1.selectbox("Hierarchy", list(q.HIERARCHIES), format_func=HIERARCHY_TITLES.get,
                             key="dd_hierarchy")
    measure = c2.selectbox("Chart measure", q.DEFAULT_MEASURES, index=1, format_func=mtitle,
                           key="dd_measure")
    levels = q.HIERARCHIES[hierarchy]
    path_key = f"dd_path_{hierarchy}"
    path = st.session_state.setdefault(path_key, [])

    # breadcrumb
    crumbs = ["All"] + [member_label(levels[i], v) for i, v in enumerate(path)]
    st.markdown("**Current position:** " + " > ".join(crumbs))

    oq = q.drilldown(hierarchy, path, q.DEFAULT_MEASURES + ["active_accounts"])
    df = execute(oq)
    child = levels[len(path)]
    child_title = q.LEVELS[child].title

    nav1, nav2, nav3, nav4 = st.columns([2.2, 1, 1, 1])
    can_drill = len(path) + 1 < len(levels)
    options = list(df[child]) if not df.empty else []
    if can_drill and options:
        pick = nav1.selectbox(f"{child_title} to drill into", options,
                              format_func=lambda v: member_label(child, v), key=f"dd_child_{hierarchy}")
        if nav2.button("Drill down", key="dd_down", type="primary"):
            st.session_state[path_key] = path + [pick]
            st.rerun()
    else:
        nav1.info(f"{child_title} is the finest level of this hierarchy.")
    if nav3.button("Up one level", key="dd_up", disabled=not path):
        st.session_state[path_key] = path[:-1]
        st.rerun()
    if nav4.button("Reset", key="dd_reset", disabled=not path):
        st.session_state[path_key] = []
        st.rerun()

    if df.empty:
        st.info("No data below this member.")
        return
    labels = labels_of(df, child)
    title = f"{mtitle(measure)} by {child_title} - {' > '.join(crumbs)}"
    # time periods stay in time order, everything else is sorted by value
    st.plotly_chart(bar_chart(df, labels, measure, title, horizontal=hierarchy != "time"),
                    width="stretch")
    show_table(df, "dd_table")
    show_sql(oq)


# slice
SLICE_LEVELS = ["year", "quarter", "month", "region", "district", "type", "operation", "frequency"]


def render_slice():
    st.subheader("Slice")
    st.write("Fixes **one** dimension to a single member (e.g. Year = 1997) and analyzes the "
             "resulting sub-cube along another level.")
    c1, c2, c3, c4 = st.columns(4)
    level = c1.selectbox("Slice on", SLICE_LEVELS, format_func=lambda n: q.LEVELS[n].title,
                         key="sl_level")
    value = c2.selectbox("Member", member_values(level),
                         format_func=lambda v: member_label(level, v), key=f"sl_value_{level}")
    by_opts = [n for n in SLICE_LEVELS if n != level]
    by = c3.selectbox("View by", by_opts, format_func=lambda n: q.LEVELS[n].title, key="sl_by")
    measure = c4.selectbox("Chart measure", q.DEFAULT_MEASURES, index=1, format_func=mtitle,
                           key="sl_measure")

    oq = q.slice_(level, value, by, q.DEFAULT_MEASURES + ["avg_amount", "active_accounts"])
    df = execute(oq)
    if df.empty:
        st.info("This slice contains no transactions.")
        return
    overall = fetch(q.overview_query())
    share = 100 * df["total_amount"].sum() / overall["total_amount"].iloc[0]
    m1, m2, m3 = st.columns(3)
    m1.metric("Transactions in slice", f"{int(df['txn_count'].sum()):,}")
    m2.metric("Total amount in slice", f"{df['total_amount'].sum():,.2f}")
    m3.metric("Share of all transaction amount", f"{share:.2f}%")

    labels = labels_of(df, by)
    title = f"{mtitle(measure)} by {q.LEVELS[by].title} where {q.LEVELS[level].title} = " \
            f"{member_label(level, value)}"
    if q.LEVELS[by].dimension == "time":
        st.plotly_chart(line_chart(df, labels, measure, title), width="stretch")
    else:
        st.plotly_chart(bar_chart(df, labels, measure, title, horizontal=True), width="stretch")
    show_table(df, "sl_table")
    show_sql(oq)


# dice
DICE_BY = ["year", "quarter", "month", "region", "district", "type", "operation", "frequency"]


def render_dice():
    st.subheader("Dice")
    st.write("Selects a sub-cube using conditions on **two or more** dimensions - member "
             "lists and ranges - then summarizes it (CTE + `RANK()` / share window functions).")
    years = member_values("year")
    c1, c2, c3 = st.columns(3)
    yr = c1.slider("Year range", min(years), max(years), (1996, 1997), key="di_years")
    quarters = c2.multiselect("Quarters", [1, 2, 3, 4], format_func=lambda v: f"Q{v}",
                              key="di_quarters")
    regions = c3.multiselect("Regions", member_values("region"),
                             default=["Prague", "south Moravia"], key="di_regions")
    pairs = location_pairs()
    district_opts = list(pairs[pairs["region"].isin(regions)]["district"]) if regions \
        else list(pairs["district"])
    c1, c2, c3 = st.columns(3)
    districts = c1.multiselect("Districts", district_opts, key="di_districts")
    types = c2.multiselect("Transaction types", member_values("type"), key="di_types")
    operations = c3.multiselect("Operations", member_values("operation"), key="di_operations")
    c1, c2, c3 = st.columns(3)
    freqs = c1.multiselect("Statement frequency", member_values("frequency"), key="di_freq")
    amin = c2.number_input("Min amount", min_value=0.0, value=0.0, step=1000.0, key="di_amount_min")
    amax = c3.number_input("Max amount (0 = no limit)", min_value=0.0, value=0.0, step=1000.0,
                           key="di_amount_max")

    filters = {
        "year_from": yr[0], "year_to": yr[1], "quarters": quarters, "regions": regions,
        "districts": districts, "types": types, "operations": operations,
        "frequencies": freqs, "amount_min": amin if amin > 0 else None,
        "amount_max": amax if amax > 0 else None,
    }
    if (yr[0], yr[1]) == (min(years), max(years)):
        filters.pop("year_from"), filters.pop("year_to")

    c1, c2, c3 = st.columns(3)
    by = c1.selectbox("Group by", DICE_BY, index=3, format_func=lambda n: q.LEVELS[n].title,
                      key="di_by")
    by2 = c2.selectbox("Then by (optional)", [None] + [n for n in DICE_BY if n != by],
                       format_func=lambda n: "(none)" if n is None else q.LEVELS[n].title,
                       key="di_by2")
    measure = c3.selectbox("Chart measure", q.DEFAULT_MEASURES, index=1, format_func=mtitle,
                           key="di_measure")

    oq = q.dice(filters, by, q.DEFAULT_MEASURES + ["avg_amount", "active_accounts"], by2)
    df = execute(oq)
    if df.empty:
        st.info("No transactions match all of the selected conditions.")
        show_sql(oq)
        return
    m1, m2 = st.columns(2)
    m1.metric("Transactions in dice", f"{int(df['txn_count'].sum()):,}")
    m2.metric("Total amount in dice", f"{df['total_amount'].sum():,.2f}")
    if by2:
        matrix = df.assign(
            _r=labels_of(df, by).values,
            _c=labels_of(df, by2).values,
        ).pivot_table(index="_r", columns="_c", values=measure, aggfunc="sum", sort=False)
        st.plotly_chart(heatmap(matrix, measure, f"{mtitle(measure)} by {q.LEVELS[by].title} "
                                f"and {q.LEVELS[by2].title}", q.LEVELS[by2].title,
                                q.LEVELS[by].title), width="stretch")
    else:
        labels = labels_of(df, by)
        st.plotly_chart(bar_chart(df, labels, measure,
                                  f"{mtitle(measure)} by {q.LEVELS[by].title}"), width="stretch")
    show_table(df, "di_table")
    show_sql(oq)


# pivot
def _swap_pivot():
    st.session_state["pv_rows"], st.session_state["pv_cols"] = \
        st.session_state["pv_cols"], st.session_state["pv_rows"]


def render_pivot():
    st.subheader("Pivot")
    st.write("Rotates the cube: members of one level become columns (conditional aggregation "
             "`SUM(CASE WHEN ...)`), compared across the members of another level, with row and "
             "column totals (`WITH ROLLUP`). Use **Swap** to rotate rows and columns.")
    st.session_state.setdefault("pv_rows", "region")
    st.session_state.setdefault("pv_cols", "year")
    c1, c2, c3, c4 = st.columns([1.3, 0.6, 1.3, 1.5])
    rows = c1.selectbox("Rows", q.PIVOT_ROW_LEVELS, format_func=lambda n: q.LEVELS[n].title,
                        key="pv_rows")
    c2.write("")
    c2.button("Swap", on_click=_swap_pivot, key="pv_swap",
              disabled=st.session_state["pv_rows"] not in q.PIVOT_COLUMN_LEVELS)
    cols = c3.selectbox("Columns", q.PIVOT_COLUMN_LEVELS, format_func=lambda n: q.LEVELS[n].title,
                        key="pv_cols")
    measure = c4.selectbox("Measure", list(q.MEASURES), index=1, format_func=mtitle,
                           key="pv_measure")
    c1, c2 = st.columns(2)
    with c1:
        filters = year_range_filter("pv_years", "Year range (optional slice)")
    types = c2.multiselect("Transaction types (optional)", member_values("type"), key="pv_types")
    if types:
        filters["types"] = types
    if rows == cols:
        st.warning("Rows and columns must be different levels.")
        return

    sql, params = q.pivot_columns_query(cols, filters)
    col_df = fetch(sql, tuple(params))
    oq = q.pivot(rows, cols, measure, list(col_df.itertuples(index=False, name=None)), filters)
    df = execute(oq)
    body = df[df[rows] != "Total"].set_index(rows)[[c for c in oq.measure_columns if c != "Total"]]
    st.plotly_chart(heatmap(body, measure, f"{mtitle(measure)}: {q.LEVELS[rows].title} x "
                            f"{q.LEVELS[cols].title}", q.LEVELS[cols].title, q.LEVELS[rows].title),
                    width="stretch")
    fmt = "%d" if q.MEASURES[measure].agg.startswith("COUNT") else "%,.2f"
    cfg = {c: st.column_config.NumberColumn(c, format=fmt) for c in oq.measure_columns}
    cfg[rows] = st.column_config.Column(q.LEVELS[rows].title)
    st.dataframe(df, width="stretch", hide_index=True, column_config=cfg, key="pv_table")
    st.caption(f"{len(df) - 1} rows x {len(oq.measure_columns) - 1} columns, plus totals")
    show_sql(oq)


# page layout
OPERATIONS = {
    "Roll-up": render_rollup,
    "Drill-down": render_drilldown,
    "Slice": render_slice,
    "Dice": render_dice,
    "Pivot": render_pivot,
}

st.title("STADVDB MCO1 - OLAP Dashboard")
st.caption("Group 7 - Berka (PKDD'99) Czech bank transactions - data warehouse `finance_mco1`")

try:
    ov = fetch(q.overview_query())
except mysql.connector.Error as e:
    st.error(f"Cannot connect to the data warehouse: {e}. Check .streamlit/secrets.toml and "
             "make sure the ETL (python finance-db/etl.py) has been run.")
    st.stop()

k1, k2, k3, k4 = st.columns(4)
k1.metric("Transactions", f"{int(ov['txn_count'].iloc[0]):,}")
k2.metric("Total amount", f"{ov['total_amount'].iloc[0]:,.2f}")
k3.metric("Active accounts", f"{int(ov['active_accounts'].iloc[0]):,}")
k4.metric("Period", f"{ov['first_date'].iloc[0]} to {ov['last_date'].iloc[0]}")

with st.sidebar:
    st.header("OLAP operation")
    op = st.radio("Select an operation", list(OPERATIONS), key="operation")
    st.divider()
    st.markdown(
        "**Star schema**\n\n"
        "- Fact: `transaction_fact` (amount, balance)\n"
        "- `date`: Year > Quarter > Month > Day\n"
        "- `account`: Region > District > Account\n"
        "- `transaction_type` > `transaction_operation`\n\n"
        "Amounts are in Czech koruna (CZK). Balance is a snapshot, so it is averaged, "
        "never summed.")
    if st.button("Clear query cache", key="clear_cache"):
        st.cache_data.clear()

try:
    OPERATIONS[op]()
except ValueError as e:
    st.warning(f"Invalid parameters: {e}")
except mysql.connector.Error as e:
    st.error(f"Database error: {e}")
