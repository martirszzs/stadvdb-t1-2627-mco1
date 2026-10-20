-- STADVDB MCO1 - Group 7: OLAP queries on finance_mco1
-- made with `python -m olap.export_sql` (same queries the dashboard runs,
-- with example values filled in)
USE finance_mco1;

-- ---------------------------------------------------------------------
-- ROLL-UP: Time hierarchy rolled up Month -> Quarter -> Year -> All
-- ---------------------------------------------------------------------
SELECT /*+ JOIN_PREFIX(f) */
    d.year_number AS `year`,
    d.quarter_number AS `quarter`,
    d.month_number AS `month`,
    IF(GROUPING(d.month_number) = 1, NULL, ANY_VALUE(d.month_name)) AS `month_name`,
    CASE
        WHEN GROUPING(d.year_number) = 1 THEN 'All'
        WHEN GROUPING(d.quarter_number) = 1 THEN 'Year'
        WHEN GROUPING(d.month_number) = 1 THEN 'Quarter'
        ELSE 'Month'
    END AS aggregation_level,
    COUNT(*) AS txn_count,
    ROUND(SUM(f.amount), 2) AS total_amount,
    ROUND(SUM(CASE WHEN tt.transaction_type = 'Credit' THEN f.amount ELSE 0 END), 2) AS total_credit,
    ROUND(SUM(CASE WHEN tt.transaction_type = 'Debit (Withdrawal)' THEN f.amount ELSE 0 END), 2) AS total_debit,
    ROUND(SUM(CASE WHEN tt.transaction_type = 'Credit' THEN f.amount WHEN tt.transaction_type = 'Debit (Withdrawal)' THEN -f.amount ELSE 0 END), 2) AS net_flow
FROM transaction_fact f
JOIN `date` d                  ON d.date_key = f.date_key
JOIN transaction_type tt       ON tt.type_key = f.type_key
GROUP BY d.year_number, d.quarter_number, d.month_number WITH ROLLUP
ORDER BY GROUPING(d.year_number), d.year_number, GROUPING(d.quarter_number), d.quarter_number, GROUPING(d.month_number), d.month_number;

-- ---------------------------------------------------------------------
-- ROLL-UP: Location hierarchy rolled up District -> Region -> All, for 1997
-- ---------------------------------------------------------------------
SELECT
    a.region AS `region`,
    a.district_name AS `district`,
    CASE
        WHEN GROUPING(a.region) = 1 THEN 'All'
        WHEN GROUPING(a.district_name) = 1 THEN 'Region'
        ELSE 'District'
    END AS aggregation_level,
    COUNT(*) AS txn_count,
    ROUND(SUM(f.amount), 2) AS total_amount,
    ROUND(SUM(CASE WHEN tt.transaction_type = 'Credit' THEN f.amount ELSE 0 END), 2) AS total_credit,
    ROUND(SUM(CASE WHEN tt.transaction_type = 'Debit (Withdrawal)' THEN f.amount ELSE 0 END), 2) AS total_debit,
    ROUND(SUM(CASE WHEN tt.transaction_type = 'Credit' THEN f.amount WHEN tt.transaction_type = 'Debit (Withdrawal)' THEN -f.amount ELSE 0 END), 2) AS net_flow
FROM transaction_fact f
JOIN `date` d                  ON d.date_key = f.date_key
JOIN account a                 ON a.account_key = f.account_key
JOIN transaction_type tt       ON tt.type_key = f.type_key
WHERE d.year_number >= 1997
  AND d.year_number <= 1997
GROUP BY a.region, a.district_name WITH ROLLUP
ORDER BY GROUPING(a.region), a.region, GROUPING(a.district_name), a.district_name;

-- ---------------------------------------------------------------------
-- DRILL-DOWN: All years -> Year level
-- ---------------------------------------------------------------------
WITH children AS (
    SELECT /*+ JOIN_PREFIX(f) */
        d.year_number AS `year`,
        COUNT(*) AS txn_count,
        ROUND(SUM(f.amount), 2) AS total_amount,
        ROUND(SUM(CASE WHEN tt.transaction_type = 'Credit' THEN f.amount ELSE 0 END), 2) AS total_credit,
        ROUND(SUM(CASE WHEN tt.transaction_type = 'Debit (Withdrawal)' THEN f.amount ELSE 0 END), 2) AS total_debit,
        ROUND(SUM(CASE WHEN tt.transaction_type = 'Credit' THEN f.amount WHEN tt.transaction_type = 'Debit (Withdrawal)' THEN -f.amount ELSE 0 END), 2) AS net_flow
    FROM transaction_fact f
    JOIN `date` d                  ON d.date_key = f.date_key
    JOIN transaction_type tt       ON tt.type_key = f.type_key
    GROUP BY d.year_number
)
SELECT
    c.*,
    ROUND(100 * c.total_amount / NULLIF(SUM(c.total_amount) OVER (), 0), 2) AS pct_of_parent_amount,
    RANK() OVER (ORDER BY c.total_amount DESC) AS amount_rank,
    LAG(c.total_amount) OVER w AS prev_total_amount,
    ROUND(100 * (c.total_amount - LAG(c.total_amount) OVER w) / NULLIF(LAG(c.total_amount) OVER w, 0), 2) AS pct_change_vs_prev
FROM children c
WINDOW w AS (ORDER BY c.`year`)
ORDER BY c.`year`;

-- ---------------------------------------------------------------------
-- DRILL-DOWN: Year 1997 > Q2 -> its months (share of parent, rank, change vs previous month)
-- ---------------------------------------------------------------------
WITH children AS (
    SELECT
        d.month_number AS `month`,
        ANY_VALUE(d.month_name) AS `month_name`,
        COUNT(*) AS txn_count,
        ROUND(SUM(f.amount), 2) AS total_amount,
        ROUND(SUM(CASE WHEN tt.transaction_type = 'Credit' THEN f.amount ELSE 0 END), 2) AS total_credit,
        ROUND(SUM(CASE WHEN tt.transaction_type = 'Debit (Withdrawal)' THEN f.amount ELSE 0 END), 2) AS total_debit,
        ROUND(SUM(CASE WHEN tt.transaction_type = 'Credit' THEN f.amount WHEN tt.transaction_type = 'Debit (Withdrawal)' THEN -f.amount ELSE 0 END), 2) AS net_flow
    FROM transaction_fact f
    JOIN `date` d                  ON d.date_key = f.date_key
    JOIN transaction_type tt       ON tt.type_key = f.type_key
    WHERE d.year_number = 1997
      AND d.quarter_number = 2
    GROUP BY d.month_number
)
SELECT
    c.*,
    ROUND(100 * c.total_amount / NULLIF(SUM(c.total_amount) OVER (), 0), 2) AS pct_of_parent_amount,
    RANK() OVER (ORDER BY c.total_amount DESC) AS amount_rank,
    LAG(c.total_amount) OVER w AS prev_total_amount,
    ROUND(100 * (c.total_amount - LAG(c.total_amount) OVER w) / NULLIF(LAG(c.total_amount) OVER w, 0), 2) AS pct_change_vs_prev
FROM children c
WINDOW w AS (ORDER BY c.`month`)
ORDER BY c.`month`;

-- ---------------------------------------------------------------------
-- DRILL-DOWN: Region south Moravia -> its districts
-- ---------------------------------------------------------------------
WITH children AS (
    SELECT
        a.district_name AS `district`,
        COUNT(*) AS txn_count,
        ROUND(SUM(f.amount), 2) AS total_amount,
        ROUND(SUM(CASE WHEN tt.transaction_type = 'Credit' THEN f.amount ELSE 0 END), 2) AS total_credit,
        ROUND(SUM(CASE WHEN tt.transaction_type = 'Debit (Withdrawal)' THEN f.amount ELSE 0 END), 2) AS total_debit,
        ROUND(SUM(CASE WHEN tt.transaction_type = 'Credit' THEN f.amount WHEN tt.transaction_type = 'Debit (Withdrawal)' THEN -f.amount ELSE 0 END), 2) AS net_flow
    FROM transaction_fact f
    JOIN account a                 ON a.account_key = f.account_key
    JOIN transaction_type tt       ON tt.type_key = f.type_key
    WHERE a.region = 'south Moravia'
    GROUP BY a.district_name
)
SELECT
    c.*,
    ROUND(100 * c.total_amount / NULLIF(SUM(c.total_amount) OVER (), 0), 2) AS pct_of_parent_amount,
    RANK() OVER (ORDER BY c.total_amount DESC) AS amount_rank
FROM children c
ORDER BY c.total_amount DESC;

-- ---------------------------------------------------------------------
-- SLICE: Year = 1997, analysed by Region
-- ---------------------------------------------------------------------
WITH slice AS (
    SELECT
        a.region AS `region`,
        COUNT(*) AS txn_count,
        ROUND(SUM(f.amount), 2) AS total_amount,
        ROUND(SUM(CASE WHEN tt.transaction_type = 'Credit' THEN f.amount ELSE 0 END), 2) AS total_credit,
        ROUND(SUM(CASE WHEN tt.transaction_type = 'Debit (Withdrawal)' THEN f.amount ELSE 0 END), 2) AS total_debit,
        ROUND(SUM(CASE WHEN tt.transaction_type = 'Credit' THEN f.amount WHEN tt.transaction_type = 'Debit (Withdrawal)' THEN -f.amount ELSE 0 END), 2) AS net_flow
    FROM transaction_fact f
    JOIN `date` d                  ON d.date_key = f.date_key
    JOIN account a                 ON a.account_key = f.account_key
    JOIN transaction_type tt       ON tt.type_key = f.type_key
    WHERE d.year_number = 1997
    GROUP BY a.region
)
SELECT
    s.*,
    ROUND(100 * s.total_amount / NULLIF(SUM(s.total_amount) OVER (), 0), 2) AS pct_of_slice_amount,
    RANK() OVER (ORDER BY s.total_amount DESC) AS amount_rank
FROM slice s
ORDER BY s.total_amount DESC;

-- ---------------------------------------------------------------------
-- SLICE: Region = Prague, analysed by Month (with running total)
-- ---------------------------------------------------------------------
WITH slice AS (
    SELECT
        d.month_number AS `month`,
        ANY_VALUE(d.month_name) AS `month_name`,
        COUNT(*) AS txn_count,
        ROUND(SUM(f.amount), 2) AS total_amount,
        ROUND(SUM(CASE WHEN tt.transaction_type = 'Credit' THEN f.amount ELSE 0 END), 2) AS total_credit,
        ROUND(SUM(CASE WHEN tt.transaction_type = 'Debit (Withdrawal)' THEN f.amount ELSE 0 END), 2) AS total_debit,
        ROUND(SUM(CASE WHEN tt.transaction_type = 'Credit' THEN f.amount WHEN tt.transaction_type = 'Debit (Withdrawal)' THEN -f.amount ELSE 0 END), 2) AS net_flow
    FROM transaction_fact f
    JOIN `date` d                  ON d.date_key = f.date_key
    JOIN account a                 ON a.account_key = f.account_key
    JOIN transaction_type tt       ON tt.type_key = f.type_key
    WHERE a.region = 'Prague'
    GROUP BY d.month_number
)
SELECT
    s.*,
    ROUND(100 * s.total_amount / NULLIF(SUM(s.total_amount) OVER (), 0), 2) AS pct_of_slice_amount,
    SUM(s.total_amount) OVER (ORDER BY s.`month` ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS running_total_amount
FROM slice s
ORDER BY s.`month`;

-- ---------------------------------------------------------------------
-- DICE: Years 1996-1997 x regions Prague/south Moravia x type Credit x amount >= 1000, by Region and Operation
-- ---------------------------------------------------------------------
WITH diced AS (
    SELECT
        a.region AS `region`,
        op.operation_name AS `operation`,
        COUNT(*) AS txn_count,
        ROUND(SUM(f.amount), 2) AS total_amount,
        ROUND(SUM(CASE WHEN tt.transaction_type = 'Credit' THEN f.amount ELSE 0 END), 2) AS total_credit,
        ROUND(SUM(CASE WHEN tt.transaction_type = 'Debit (Withdrawal)' THEN f.amount ELSE 0 END), 2) AS total_debit,
        ROUND(SUM(CASE WHEN tt.transaction_type = 'Credit' THEN f.amount WHEN tt.transaction_type = 'Debit (Withdrawal)' THEN -f.amount ELSE 0 END), 2) AS net_flow
    FROM transaction_fact f
    JOIN `date` d                  ON d.date_key = f.date_key
    JOIN account a                 ON a.account_key = f.account_key
    JOIN transaction_type tt       ON tt.type_key = f.type_key
    JOIN transaction_operation op  ON op.operation_key = f.operation_key
    WHERE d.year_number >= 1996
      AND d.year_number <= 1997
      AND a.region IN ('Prague', 'south Moravia')
      AND tt.transaction_type IN ('Credit')
      AND f.amount >= 1000.0
    GROUP BY a.region, op.operation_name
)
SELECT
    s.*,
    RANK() OVER (PARTITION BY s.`region` ORDER BY s.total_amount DESC) AS rank_within_region,
    ROUND(100 * s.total_amount / NULLIF(SUM(s.total_amount) OVER (PARTITION BY s.`region`), 0), 2) AS pct_within_region
FROM diced s
ORDER BY s.`region`, s.`operation`;

-- ---------------------------------------------------------------------
-- PIVOT: Total amount: Region (rows) x Year (columns) with totals
-- ---------------------------------------------------------------------
SELECT /*+ JOIN_PREFIX(f) */
    IF(GROUPING(a.region) = 1, 'Total', CAST(a.region AS CHAR)) AS `region`,
    ROUND(SUM(CASE WHEN d.year_number = 1993 THEN f.amount ELSE 0 END), 2) AS `1993`,
    ROUND(SUM(CASE WHEN d.year_number = 1994 THEN f.amount ELSE 0 END), 2) AS `1994`,
    ROUND(SUM(CASE WHEN d.year_number = 1995 THEN f.amount ELSE 0 END), 2) AS `1995`,
    ROUND(SUM(CASE WHEN d.year_number = 1996 THEN f.amount ELSE 0 END), 2) AS `1996`,
    ROUND(SUM(CASE WHEN d.year_number = 1997 THEN f.amount ELSE 0 END), 2) AS `1997`,
    ROUND(SUM(CASE WHEN d.year_number = 1998 THEN f.amount ELSE 0 END), 2) AS `1998`,
    ROUND(SUM(f.amount), 2) AS `Total`
FROM transaction_fact f
JOIN `date` d                  ON d.date_key = f.date_key
JOIN account a                 ON a.account_key = f.account_key
GROUP BY a.region WITH ROLLUP
ORDER BY GROUPING(a.region), a.region;

-- ---------------------------------------------------------------------
-- PIVOT (swapped): Total amount: Year (rows) x Region (columns)
-- ---------------------------------------------------------------------
SELECT /*+ JOIN_PREFIX(f) */
    IF(GROUPING(d.year_number) = 1, 'Total', CAST(d.year_number AS CHAR)) AS `year`,
    ROUND(SUM(CASE WHEN a.region = 'central Bohemia' THEN f.amount ELSE 0 END), 2) AS `central Bohemia`,
    ROUND(SUM(CASE WHEN a.region = 'east Bohemia' THEN f.amount ELSE 0 END), 2) AS `east Bohemia`,
    ROUND(SUM(CASE WHEN a.region = 'north Bohemia' THEN f.amount ELSE 0 END), 2) AS `north Bohemia`,
    ROUND(SUM(CASE WHEN a.region = 'north Moravia' THEN f.amount ELSE 0 END), 2) AS `north Moravia`,
    ROUND(SUM(CASE WHEN a.region = 'Prague' THEN f.amount ELSE 0 END), 2) AS `Prague`,
    ROUND(SUM(CASE WHEN a.region = 'south Bohemia' THEN f.amount ELSE 0 END), 2) AS `south Bohemia`,
    ROUND(SUM(CASE WHEN a.region = 'south Moravia' THEN f.amount ELSE 0 END), 2) AS `south Moravia`,
    ROUND(SUM(CASE WHEN a.region = 'west Bohemia' THEN f.amount ELSE 0 END), 2) AS `west Bohemia`,
    ROUND(SUM(f.amount), 2) AS `Total`
FROM transaction_fact f
JOIN `date` d                  ON d.date_key = f.date_key
JOIN account a                 ON a.account_key = f.account_key
GROUP BY d.year_number WITH ROLLUP
ORDER BY GROUPING(d.year_number), d.year_number;
