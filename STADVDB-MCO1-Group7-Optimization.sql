/* =====================================================================
   STADVDB MCO1 - Group 7 : Step 6 - Performance Optimization (MySQL Workbench)
   Database: finance_mco1

   How to use:
     * Run each numbered block on its own (select it, then Ctrl+Shift+Enter).
     * Every query is written as  EXPLAIN ANALYZE <query>  - MySQL runs the query
       and prints the plan with the REAL time of each step. Compare the time on the
       top line (and the 'Table scan on f' line) of ORIGINAL vs OPTIMIZED.
     * Results from our Python run (median of 5 runs, ms) are noted in the comments.
     * Always check that ORIGINAL and OPTIMIZED return the same rows (we verified this
       with hashes in optimize.py: 'same result: yes' for all 24 pairs).

   Bottleneck -> strategy (from EXPLAIN ANALYZE of the original queries):
     A. Join + group all ~1.05M fact rows before aggregating ........ QUERY REFORMULATION
     B. Same full scan, but the answer is only a handful of rows ..... DENORMALIZATION (summary table)
     C. Filtered queries still do 'Table scan on f' ................... SECONDARY (covering) INDEXES
   ===================================================================== */

USE finance_mco1;

-- Make sure each foreign key has its own named index (MySQL silently drops the automatic
-- one when a composite index with the same first column is added). Skip any that exist.
-- ALTER TABLE transaction_fact ADD INDEX idx_fk_date_key (date_key);
-- ALTER TABLE transaction_fact ADD INDEX idx_fk_account_key (account_key);
-- ALTER TABLE transaction_fact ADD INDEX idx_fk_type_key (type_key);
-- ALTER TABLE transaction_fact ADD INDEX idx_fk_operation_key (operation_key);

-- Baseline size of the fact table (for the report)
SELECT table_name, table_rows, ROUND(data_length/1024/1024,1) AS data_mb, ROUND(index_length/1024/1024,1) AS index_mb
FROM information_schema.tables WHERE table_schema = 'finance_mco1' AND table_name = 'transaction_fact';


/* =====================================================================
   A. QUERY REFORMULATION  (no extra storage, no maintenance)
   Bottleneck: ORIGINAL joins the dimension tables to all ~1.05M fact rows, then
   groups them (EXPLAIN: 'Table scan on f' feeding nested-loop joins + temp table).
   Fix: add up the fact table by its own foreign keys first (a few thousand groups),
   THEN join the dimensions to that small result and apply WITH ROLLUP.
   ===================================================================== */

-- A1. RU-1 Time, month level, no filter: 2859.3 ms -> 323.5 ms (x8.84)
-- A1a. ORIGINAL
EXPLAIN ANALYZE
SELECT /*+ JOIN_PREFIX(f) */
    d.year_number AS `year`,
    d.quarter_number AS `quarter`,
    d.month_number AS `month`,
    IF(GROUPING(d.month_number) = 1, NULL, MAX(d.month_name)) AS `month_name`,
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

-- A1b. OPTIMIZED (pre-aggregate, then join)
EXPLAIN ANALYZE
SELECT
    d.year_number AS `year`,
    d.quarter_number AS `quarter`,
    d.month_number AS `month`,
    IF(GROUPING(d.month_number) = 1, NULL, MAX(d.month_name)) AS `month_name`,
    CASE
        WHEN GROUPING(d.year_number) = 1 THEN 'All'
        WHEN GROUPING(d.quarter_number) = 1 THEN 'Year'
        WHEN GROUPING(d.month_number) = 1 THEN 'Quarter'
        ELSE 'Month'
    END AS aggregation_level,
    CAST(SUM(p.n) AS SIGNED) AS txn_count,
    ROUND(SUM(p.amt), 2) AS total_amount,
    ROUND(SUM(CASE WHEN tt.transaction_type = 'Credit' THEN p.amt ELSE 0 END), 2) AS total_credit,
    ROUND(SUM(CASE WHEN tt.transaction_type = 'Debit (Withdrawal)' THEN p.amt ELSE 0 END), 2) AS total_debit,
    ROUND(SUM(CASE WHEN tt.transaction_type = 'Credit' THEN p.amt WHEN tt.transaction_type = 'Debit (Withdrawal)' THEN -p.amt ELSE 0 END), 2) AS net_flow
FROM (
    SELECT f.date_key AS k, f.type_key AS type_key, COUNT(*) AS n, SUM(f.amount) AS amt
    FROM transaction_fact f
    GROUP BY f.date_key, f.type_key
) p
JOIN `date` d ON d.date_key = p.k
JOIN transaction_type tt ON tt.type_key = p.type_key
GROUP BY d.year_number, d.quarter_number, d.month_number WITH ROLLUP
ORDER BY GROUPING(d.year_number), d.year_number, GROUPING(d.quarter_number), d.quarter_number, GROUPING(d.month_number), d.month_number;

-- A2. RU-3 Time, day level, no filter (deepest): 3553.1 ms -> 343.7 ms (x10.34)
-- A2a. ORIGINAL
EXPLAIN ANALYZE
SELECT /*+ JOIN_PREFIX(f) */
    d.year_number AS `year`,
    d.quarter_number AS `quarter`,
    d.month_number AS `month`,
    IF(GROUPING(d.month_number) = 1, NULL, MAX(d.month_name)) AS `month_name`,
    d.full_date AS `day`,
    CASE
        WHEN GROUPING(d.year_number) = 1 THEN 'All'
        WHEN GROUPING(d.quarter_number) = 1 THEN 'Year'
        WHEN GROUPING(d.month_number) = 1 THEN 'Quarter'
        WHEN GROUPING(d.full_date) = 1 THEN 'Month'
        ELSE 'Day'
    END AS aggregation_level,
    COUNT(*) AS txn_count,
    ROUND(SUM(f.amount), 2) AS total_amount,
    ROUND(SUM(CASE WHEN tt.transaction_type = 'Credit' THEN f.amount ELSE 0 END), 2) AS total_credit,
    ROUND(SUM(CASE WHEN tt.transaction_type = 'Debit (Withdrawal)' THEN f.amount ELSE 0 END), 2) AS total_debit,
    ROUND(SUM(CASE WHEN tt.transaction_type = 'Credit' THEN f.amount WHEN tt.transaction_type = 'Debit (Withdrawal)' THEN -f.amount ELSE 0 END), 2) AS net_flow
FROM transaction_fact f
JOIN `date` d                  ON d.date_key = f.date_key
JOIN transaction_type tt       ON tt.type_key = f.type_key
GROUP BY d.year_number, d.quarter_number, d.month_number, d.full_date WITH ROLLUP
ORDER BY GROUPING(d.year_number), d.year_number, GROUPING(d.quarter_number), d.quarter_number, GROUPING(d.month_number), d.month_number, GROUPING(d.full_date), d.full_date;

-- A2b. OPTIMIZED (pre-aggregate, then join)
EXPLAIN ANALYZE
SELECT
    d.year_number AS `year`,
    d.quarter_number AS `quarter`,
    d.month_number AS `month`,
    IF(GROUPING(d.month_number) = 1, NULL, MAX(d.month_name)) AS `month_name`,
    d.full_date AS `day`,
    CASE
        WHEN GROUPING(d.year_number) = 1 THEN 'All'
        WHEN GROUPING(d.quarter_number) = 1 THEN 'Year'
        WHEN GROUPING(d.month_number) = 1 THEN 'Quarter'
        WHEN GROUPING(d.full_date) = 1 THEN 'Month'
        ELSE 'Day'
    END AS aggregation_level,
    CAST(SUM(p.n) AS SIGNED) AS txn_count,
    ROUND(SUM(p.amt), 2) AS total_amount,
    ROUND(SUM(CASE WHEN tt.transaction_type = 'Credit' THEN p.amt ELSE 0 END), 2) AS total_credit,
    ROUND(SUM(CASE WHEN tt.transaction_type = 'Debit (Withdrawal)' THEN p.amt ELSE 0 END), 2) AS total_debit,
    ROUND(SUM(CASE WHEN tt.transaction_type = 'Credit' THEN p.amt WHEN tt.transaction_type = 'Debit (Withdrawal)' THEN -p.amt ELSE 0 END), 2) AS net_flow
FROM (
    SELECT f.date_key AS k, f.type_key AS type_key, COUNT(*) AS n, SUM(f.amount) AS amt
    FROM transaction_fact f
    GROUP BY f.date_key, f.type_key
) p
JOIN `date` d ON d.date_key = p.k
JOIN transaction_type tt ON tt.type_key = p.type_key
GROUP BY d.year_number, d.quarter_number, d.month_number, d.full_date WITH ROLLUP
ORDER BY GROUPING(d.year_number), d.year_number, GROUPING(d.quarter_number), d.quarter_number, GROUPING(d.month_number), d.month_number, GROUPING(d.full_date), d.full_date;

-- A3. RU-5 Location, account level, no filter (deepest): 2484.9 ms -> 364.0 ms (x6.83)
-- A3a. ORIGINAL
EXPLAIN ANALYZE
SELECT /*+ JOIN_PREFIX(f) */
    a.region AS `region`,
    a.district_name AS `district`,
    a.account_id AS `account`,
    CASE
        WHEN GROUPING(a.region) = 1 THEN 'All'
        WHEN GROUPING(a.district_name) = 1 THEN 'Region'
        WHEN GROUPING(a.account_id) = 1 THEN 'District'
        ELSE 'Account'
    END AS aggregation_level,
    COUNT(*) AS txn_count,
    ROUND(SUM(f.amount), 2) AS total_amount,
    ROUND(SUM(CASE WHEN tt.transaction_type = 'Credit' THEN f.amount ELSE 0 END), 2) AS total_credit,
    ROUND(SUM(CASE WHEN tt.transaction_type = 'Debit (Withdrawal)' THEN f.amount ELSE 0 END), 2) AS total_debit,
    ROUND(SUM(CASE WHEN tt.transaction_type = 'Credit' THEN f.amount WHEN tt.transaction_type = 'Debit (Withdrawal)' THEN -f.amount ELSE 0 END), 2) AS net_flow
FROM transaction_fact f
JOIN account a                 ON a.account_key = f.account_key
JOIN transaction_type tt       ON tt.type_key = f.type_key
GROUP BY a.region, a.district_name, a.account_id WITH ROLLUP
ORDER BY GROUPING(a.region), a.region, GROUPING(a.district_name), a.district_name, GROUPING(a.account_id), a.account_id;

-- A3b. OPTIMIZED (pre-aggregate, then join)
EXPLAIN ANALYZE
SELECT
    a.region AS `region`,
    a.district_name AS `district`,
    a.account_id AS `account`,
    CASE
        WHEN GROUPING(a.region) = 1 THEN 'All'
        WHEN GROUPING(a.district_name) = 1 THEN 'Region'
        WHEN GROUPING(a.account_id) = 1 THEN 'District'
        ELSE 'Account'
    END AS aggregation_level,
    CAST(SUM(p.n) AS SIGNED) AS txn_count,
    ROUND(SUM(p.amt), 2) AS total_amount,
    ROUND(SUM(CASE WHEN tt.transaction_type = 'Credit' THEN p.amt ELSE 0 END), 2) AS total_credit,
    ROUND(SUM(CASE WHEN tt.transaction_type = 'Debit (Withdrawal)' THEN p.amt ELSE 0 END), 2) AS total_debit,
    ROUND(SUM(CASE WHEN tt.transaction_type = 'Credit' THEN p.amt WHEN tt.transaction_type = 'Debit (Withdrawal)' THEN -p.amt ELSE 0 END), 2) AS net_flow
FROM (
    SELECT f.account_key AS k, f.type_key AS type_key, COUNT(*) AS n, SUM(f.amount) AS amt
    FROM transaction_fact f
    GROUP BY f.account_key, f.type_key
) p
JOIN account a ON a.account_key = p.k
JOIN transaction_type tt ON tt.type_key = p.type_key
GROUP BY a.region, a.district_name, a.account_id WITH ROLLUP
ORDER BY GROUPING(a.region), a.region, GROUPING(a.district_name), a.district_name, GROUPING(a.account_id), a.account_id;


/* =====================================================================
   B. DENORMALIZATION  (pre-summed table)
   Bottleneck: these queries produce only 6-103 rows but still scan and join all
   ~1.05M fact rows. Fix: store the totals once in one flat table
   (month x district x transaction type x operation, ~28K rows) and let the
   queries read that instead of the fact table + 4 dimension joins.
   Cost: ~7.5 MB extra, ~6.5 s to build, MUST be rebuilt after every ETL load,
   and it only answers questions at month / district / type / operation grain.
   ===================================================================== */

DROP TABLE IF EXISTS summary_month_district;

CREATE TABLE summary_month_district (
    year_number      INT NOT NULL,
    quarter_number   INT NOT NULL,
    month_number     INT NOT NULL,
    month_name       VARCHAR(20) NOT NULL,
    region           VARCHAR(100) NOT NULL,
    district_name    VARCHAR(100) NOT NULL,
    transaction_type VARCHAR(100) NOT NULL,
    operation_name   VARCHAR(150) NOT NULL,
    txn_count        BIGINT NOT NULL,
    total_amount     DECIMAL(18,2) NOT NULL,
    INDEX idx_sum_time (year_number, quarter_number, month_number),
    INDEX idx_sum_loc  (region, district_name)
);

INSERT INTO summary_month_district
SELECT d.year_number, d.quarter_number, d.month_number, MAX(d.month_name),
       a.region, a.district_name, tt.transaction_type, op.operation_name,
       COUNT(*), SUM(f.amount)
FROM transaction_fact f
JOIN `date` d                 ON d.date_key = f.date_key
JOIN account a                ON a.account_key = f.account_key
JOIN transaction_type tt      ON tt.type_key = f.type_key
JOIN transaction_operation op ON op.operation_key = f.operation_key
GROUP BY d.year_number, d.quarter_number, d.month_number,
         a.region, a.district_name, tt.transaction_type, op.operation_name;

ANALYZE TABLE summary_month_district;
SELECT COUNT(*) AS summary_rows FROM summary_month_district;   -- ~28,216 vs ~1,052,827 fact rows

-- B1. RU-1 Time, month level, no filter: 2859.3 ms -> 55.5 ms (x51.49)
-- B1a. ORIGINAL
EXPLAIN ANALYZE
SELECT /*+ JOIN_PREFIX(f) */
    d.year_number AS `year`,
    d.quarter_number AS `quarter`,
    d.month_number AS `month`,
    IF(GROUPING(d.month_number) = 1, NULL, MAX(d.month_name)) AS `month_name`,
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

-- B1b. OPTIMIZED (reads summary_month_district)
EXPLAIN ANALYZE
SELECT s.year_number AS `year`,
    s.quarter_number AS `quarter`,
    s.month_number AS `month`,
    IF(GROUPING(s.month_number) = 1, NULL, MAX(s.month_name)) AS `month_name`,
    CASE
        WHEN GROUPING(s.year_number) = 1 THEN 'All'
        WHEN GROUPING(s.quarter_number) = 1 THEN 'Year'
        WHEN GROUPING(s.month_number) = 1 THEN 'Quarter'
        ELSE 'Month'
    END AS aggregation_level,
    CAST(SUM(s.txn_count) AS SIGNED) AS txn_count,
    ROUND(SUM(s.total_amount), 2) AS total_amount,
    ROUND(SUM(CASE WHEN s.transaction_type = 'Credit' THEN s.total_amount ELSE 0 END), 2) AS total_credit,
    ROUND(SUM(CASE WHEN s.transaction_type = 'Debit (Withdrawal)' THEN s.total_amount ELSE 0 END), 2) AS total_debit,
    ROUND(SUM(CASE WHEN s.transaction_type = 'Credit' THEN s.total_amount WHEN s.transaction_type = 'Debit (Withdrawal)' THEN -s.total_amount ELSE 0 END), 2) AS net_flow
FROM summary_month_district s
GROUP BY s.year_number, s.quarter_number, s.month_number WITH ROLLUP
ORDER BY GROUPING(s.year_number), s.year_number, GROUPING(s.quarter_number), s.quarter_number, GROUPING(s.month_number), s.month_number;

-- B2. RU-6 Transaction, operation level, no filter: 2330.8 ms -> 51.3 ms (x45.45)
-- B2a. ORIGINAL
EXPLAIN ANALYZE
SELECT /*+ JOIN_PREFIX(f) */
    tt.transaction_type AS `type`,
    op.operation_name AS `operation`,
    CASE
        WHEN GROUPING(tt.transaction_type) = 1 THEN 'All'
        WHEN GROUPING(op.operation_name) = 1 THEN 'Transaction type'
        ELSE 'Operation'
    END AS aggregation_level,
    COUNT(*) AS txn_count,
    ROUND(SUM(f.amount), 2) AS total_amount,
    ROUND(SUM(CASE WHEN tt.transaction_type = 'Credit' THEN f.amount ELSE 0 END), 2) AS total_credit,
    ROUND(SUM(CASE WHEN tt.transaction_type = 'Debit (Withdrawal)' THEN f.amount ELSE 0 END), 2) AS total_debit,
    ROUND(SUM(CASE WHEN tt.transaction_type = 'Credit' THEN f.amount WHEN tt.transaction_type = 'Debit (Withdrawal)' THEN -f.amount ELSE 0 END), 2) AS net_flow
FROM transaction_fact f
JOIN transaction_type tt       ON tt.type_key = f.type_key
JOIN transaction_operation op  ON op.operation_key = f.operation_key
GROUP BY tt.transaction_type, op.operation_name WITH ROLLUP
ORDER BY GROUPING(tt.transaction_type), tt.transaction_type, GROUPING(op.operation_name), op.operation_name;

-- B2b. OPTIMIZED (reads summary_month_district)
EXPLAIN ANALYZE
SELECT s.transaction_type AS `type`,
    s.operation_name AS `operation`,
    CASE
        WHEN GROUPING(s.transaction_type) = 1 THEN 'All'
        WHEN GROUPING(s.operation_name) = 1 THEN 'Transaction type'
        ELSE 'Operation'
    END AS aggregation_level,
    CAST(SUM(s.txn_count) AS SIGNED) AS txn_count,
    ROUND(SUM(s.total_amount), 2) AS total_amount,
    ROUND(SUM(CASE WHEN s.transaction_type = 'Credit' THEN s.total_amount ELSE 0 END), 2) AS total_credit,
    ROUND(SUM(CASE WHEN s.transaction_type = 'Debit (Withdrawal)' THEN s.total_amount ELSE 0 END), 2) AS total_debit,
    ROUND(SUM(CASE WHEN s.transaction_type = 'Credit' THEN s.total_amount WHEN s.transaction_type = 'Debit (Withdrawal)' THEN -s.total_amount ELSE 0 END), 2) AS net_flow
FROM summary_month_district s
GROUP BY s.transaction_type, s.operation_name WITH ROLLUP
ORDER BY GROUPING(s.transaction_type), s.transaction_type, GROUPING(s.operation_name), s.operation_name;

-- B3. DD-1 Time: all -> years: 1181.3 ms -> 24.2 ms (x48.79)
-- B3a. ORIGINAL
EXPLAIN ANALYZE
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

-- B3b. OPTIMIZED (reads summary_month_district)
EXPLAIN ANALYZE
WITH children AS (
    SELECT s.year_number AS `year`,
        CAST(SUM(s.txn_count) AS SIGNED) AS txn_count,
        ROUND(SUM(s.total_amount), 2) AS total_amount,
        ROUND(SUM(CASE WHEN s.transaction_type = 'Credit' THEN s.total_amount ELSE 0 END), 2) AS total_credit,
        ROUND(SUM(CASE WHEN s.transaction_type = 'Debit (Withdrawal)' THEN s.total_amount ELSE 0 END), 2) AS total_debit,
        ROUND(SUM(CASE WHEN s.transaction_type = 'Credit' THEN s.total_amount WHEN s.transaction_type = 'Debit (Withdrawal)' THEN -s.total_amount ELSE 0 END), 2) AS net_flow
    FROM summary_month_district s
    GROUP BY s.year_number
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

-- B4. DD-4 Location: all -> regions: 956.6 ms -> 25.4 ms (x37.7)
-- B4a. ORIGINAL
EXPLAIN ANALYZE
WITH children AS (
    SELECT /*+ JOIN_PREFIX(f) */
        a.region AS `region`,
        COUNT(*) AS txn_count,
        ROUND(SUM(f.amount), 2) AS total_amount,
        ROUND(SUM(CASE WHEN tt.transaction_type = 'Credit' THEN f.amount ELSE 0 END), 2) AS total_credit,
        ROUND(SUM(CASE WHEN tt.transaction_type = 'Debit (Withdrawal)' THEN f.amount ELSE 0 END), 2) AS total_debit,
        ROUND(SUM(CASE WHEN tt.transaction_type = 'Credit' THEN f.amount WHEN tt.transaction_type = 'Debit (Withdrawal)' THEN -f.amount ELSE 0 END), 2) AS net_flow
    FROM transaction_fact f
    JOIN account a                 ON a.account_key = f.account_key
    JOIN transaction_type tt       ON tt.type_key = f.type_key
    GROUP BY a.region
)
SELECT
    c.*,
    ROUND(100 * c.total_amount / NULLIF(SUM(c.total_amount) OVER (), 0), 2) AS pct_of_parent_amount,
    RANK() OVER (ORDER BY c.total_amount DESC) AS amount_rank
FROM children c
ORDER BY c.total_amount DESC;

-- B4b. OPTIMIZED (reads summary_month_district)
EXPLAIN ANALYZE
WITH children AS (
    SELECT s.region AS `region`,
        CAST(SUM(s.txn_count) AS SIGNED) AS txn_count,
        ROUND(SUM(s.total_amount), 2) AS total_amount,
        ROUND(SUM(CASE WHEN s.transaction_type = 'Credit' THEN s.total_amount ELSE 0 END), 2) AS total_credit,
        ROUND(SUM(CASE WHEN s.transaction_type = 'Debit (Withdrawal)' THEN s.total_amount ELSE 0 END), 2) AS total_debit,
        ROUND(SUM(CASE WHEN s.transaction_type = 'Credit' THEN s.total_amount WHEN s.transaction_type = 'Debit (Withdrawal)' THEN -s.total_amount ELSE 0 END), 2) AS net_flow
    FROM summary_month_district s
    GROUP BY s.region
)
SELECT
    c.*,
    ROUND(100 * c.total_amount / NULLIF(SUM(c.total_amount) OVER (), 0), 2) AS pct_of_parent_amount,
    RANK() OVER (ORDER BY c.total_amount DESC) AS amount_rank
FROM children c
ORDER BY c.total_amount DESC;

-- B5. PV-1 Total amount: region x year: 2203.9 ms -> 28.9 ms (x76.32)
-- B5a. ORIGINAL
EXPLAIN ANALYZE
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

-- B5b. OPTIMIZED (reads summary_month_district)
EXPLAIN ANALYZE
SELECT IF(GROUPING(s.region) = 1, 'Total', CAST(s.region AS CHAR)) AS `region`,
    ROUND(SUM(CASE WHEN s.year_number = 1993 THEN s.total_amount ELSE 0 END), 2) AS `1993`,
    ROUND(SUM(CASE WHEN s.year_number = 1994 THEN s.total_amount ELSE 0 END), 2) AS `1994`,
    ROUND(SUM(CASE WHEN s.year_number = 1995 THEN s.total_amount ELSE 0 END), 2) AS `1995`,
    ROUND(SUM(CASE WHEN s.year_number = 1996 THEN s.total_amount ELSE 0 END), 2) AS `1996`,
    ROUND(SUM(CASE WHEN s.year_number = 1997 THEN s.total_amount ELSE 0 END), 2) AS `1997`,
    ROUND(SUM(CASE WHEN s.year_number = 1998 THEN s.total_amount ELSE 0 END), 2) AS `1998`,
    ROUND(SUM(s.total_amount), 2) AS `Total`
FROM summary_month_district s
GROUP BY s.region WITH ROLLUP
ORDER BY GROUPING(s.region), s.region;

-- B6. PV-2 Total amount: year x region: 2380.9 ms -> 39.2 ms (x60.71)
-- B6a. ORIGINAL
EXPLAIN ANALYZE
SELECT /*+ JOIN_PREFIX(f) */
    IF(GROUPING(d.year_number) = 1, 'Total', CAST(d.year_number AS CHAR)) AS `year`,
    ROUND(SUM(CASE WHEN a.region = 'Prague' THEN f.amount ELSE 0 END), 2) AS `Prague`,
    ROUND(SUM(CASE WHEN a.region = 'south Moravia' THEN f.amount ELSE 0 END), 2) AS `south Moravia`,
    ROUND(SUM(CASE WHEN a.region = 'central Bohemia' THEN f.amount ELSE 0 END), 2) AS `central Bohemia`,
    ROUND(SUM(CASE WHEN a.region = 'east Bohemia' THEN f.amount ELSE 0 END), 2) AS `east Bohemia`,
    ROUND(SUM(CASE WHEN a.region = 'north Bohemia' THEN f.amount ELSE 0 END), 2) AS `north Bohemia`,
    ROUND(SUM(CASE WHEN a.region = 'north Moravia' THEN f.amount ELSE 0 END), 2) AS `north Moravia`,
    ROUND(SUM(CASE WHEN a.region = 'south Bohemia' THEN f.amount ELSE 0 END), 2) AS `south Bohemia`,
    ROUND(SUM(CASE WHEN a.region = 'west Bohemia' THEN f.amount ELSE 0 END), 2) AS `west Bohemia`,
    ROUND(SUM(f.amount), 2) AS `Total`
FROM transaction_fact f
JOIN `date` d                  ON d.date_key = f.date_key
JOIN account a                 ON a.account_key = f.account_key
GROUP BY d.year_number WITH ROLLUP
ORDER BY GROUPING(d.year_number), d.year_number;

-- B6b. OPTIMIZED (reads summary_month_district)
EXPLAIN ANALYZE
SELECT IF(GROUPING(s.year_number) = 1, 'Total', CAST(s.year_number AS CHAR)) AS `year`,
    ROUND(SUM(CASE WHEN s.region = 'Prague' THEN s.total_amount ELSE 0 END), 2) AS `Prague`,
    ROUND(SUM(CASE WHEN s.region = 'south Moravia' THEN s.total_amount ELSE 0 END), 2) AS `south Moravia`,
    ROUND(SUM(CASE WHEN s.region = 'central Bohemia' THEN s.total_amount ELSE 0 END), 2) AS `central Bohemia`,
    ROUND(SUM(CASE WHEN s.region = 'east Bohemia' THEN s.total_amount ELSE 0 END), 2) AS `east Bohemia`,
    ROUND(SUM(CASE WHEN s.region = 'north Bohemia' THEN s.total_amount ELSE 0 END), 2) AS `north Bohemia`,
    ROUND(SUM(CASE WHEN s.region = 'north Moravia' THEN s.total_amount ELSE 0 END), 2) AS `north Moravia`,
    ROUND(SUM(CASE WHEN s.region = 'south Bohemia' THEN s.total_amount ELSE 0 END), 2) AS `south Bohemia`,
    ROUND(SUM(CASE WHEN s.region = 'west Bohemia' THEN s.total_amount ELSE 0 END), 2) AS `west Bohemia`,
    ROUND(SUM(s.total_amount), 2) AS `Total`
FROM summary_month_district s
GROUP BY s.year_number WITH ROLLUP
ORDER BY GROUPING(s.year_number), s.year_number;


/* =====================================================================
   C. SECONDARY (COVERING) INDEXES
   Bottleneck: filtered queries (year, region, type, amount range) still show
   'Table scan on f' - the filter is on a dimension, so MySQL scans every fact row.
   Fix: composite indexes that contain every fact column the query needs, so MySQL
   answers from the index ('Covering index lookup on f using ix_cov_...') and never
   touches the table rows.
   Cost: ~89 MB extra (fact table is 65.6 MB data + 66.1 MB indexes) and slower inserts.
   ===================================================================== */

ALTER TABLE transaction_fact ADD INDEX ix_cov_date (date_key, type_key, account_key, operation_key, amount);
ALTER TABLE transaction_fact ADD INDEX ix_cov_account (account_key, date_key, type_key, amount);
ALTER TABLE transaction_fact ADD INDEX ix_cov_type (type_key, amount, date_key, account_key);
ANALYZE TABLE transaction_fact;

-- C1. RU-2 Time, month level, year 1997: 1080.0 ms -> 605.8 ms (x1.78)
-- C1a. BEFORE: run this with the indexes dropped (see CLEAN-UP) to see 'Table scan on f'.
-- C1b. AFTER (same query; MySQL now picks the covering index):
EXPLAIN ANALYZE
SELECT
    d.year_number AS `year`,
    d.quarter_number AS `quarter`,
    d.month_number AS `month`,
    IF(GROUPING(d.month_number) = 1, NULL, MAX(d.month_name)) AS `month_name`,
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
WHERE d.year_number >= 1997
  AND d.year_number <= 1997
GROUP BY d.year_number, d.quarter_number, d.month_number WITH ROLLUP
ORDER BY GROUPING(d.year_number), d.year_number, GROUPING(d.quarter_number), d.quarter_number, GROUPING(d.month_number), d.month_number;

-- C2. RU-4 Location, district level, year 1997: 1419.7 ms -> 690.2 ms (x2.06)
-- C2a. BEFORE: run this with the indexes dropped (see CLEAN-UP) to see 'Table scan on f'.
-- C2b. AFTER (same query; MySQL now picks the covering index):
EXPLAIN ANALYZE
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

-- C3. DD-2 Time: 1997 -> quarters: 954.4 ms -> 170.6 ms (x5.59)
-- C3a. BEFORE: run this with the indexes dropped (see CLEAN-UP) to see 'Table scan on f'.
-- C3b. AFTER (same query; MySQL now picks the covering index):
EXPLAIN ANALYZE
WITH children AS (
    SELECT
        d.quarter_number AS `quarter`,
        COUNT(*) AS txn_count,
        ROUND(SUM(f.amount), 2) AS total_amount,
        ROUND(SUM(CASE WHEN tt.transaction_type = 'Credit' THEN f.amount ELSE 0 END), 2) AS total_credit,
        ROUND(SUM(CASE WHEN tt.transaction_type = 'Debit (Withdrawal)' THEN f.amount ELSE 0 END), 2) AS total_debit,
        ROUND(SUM(CASE WHEN tt.transaction_type = 'Credit' THEN f.amount WHEN tt.transaction_type = 'Debit (Withdrawal)' THEN -f.amount ELSE 0 END), 2) AS net_flow
    FROM transaction_fact f
    JOIN `date` d                  ON d.date_key = f.date_key
    JOIN transaction_type tt       ON tt.type_key = f.type_key
    WHERE d.year_number = 1997
    GROUP BY d.quarter_number
)
SELECT
    c.*,
    ROUND(100 * c.total_amount / NULLIF(SUM(c.total_amount) OVER (), 0), 2) AS pct_of_parent_amount,
    RANK() OVER (ORDER BY c.total_amount DESC) AS amount_rank,
    LAG(c.total_amount) OVER w AS prev_total_amount,
    ROUND(100 * (c.total_amount - LAG(c.total_amount) OVER w) / NULLIF(LAG(c.total_amount) OVER w, 0), 2) AS pct_change_vs_prev
FROM children c
WINDOW w AS (ORDER BY c.`quarter`)
ORDER BY c.`quarter`;

-- C4. DD-3 Time: 1997 Q2 -> months: 208.4 ms -> 50.6 ms (x4.12)
-- C4a. BEFORE: run this with the indexes dropped (see CLEAN-UP) to see 'Table scan on f'.
-- C4b. AFTER (same query; MySQL now picks the covering index):
EXPLAIN ANALYZE
WITH children AS (
    SELECT
        d.month_number AS `month`,
        MAX(d.month_name) AS `month_name`,
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

-- C5. DD-5 Location: south Moravia -> districts: 492.8 ms -> 182.0 ms (x2.71)
-- C5a. BEFORE: run this with the indexes dropped (see CLEAN-UP) to see 'Table scan on f'.
-- C5b. AFTER (same query; MySQL now picks the covering index):
EXPLAIN ANALYZE
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

-- C6. DD-6 Location: Prague -> districts: 356.4 ms -> 133.1 ms (x2.68)
-- C6a. BEFORE: run this with the indexes dropped (see CLEAN-UP) to see 'Table scan on f'.
-- C6b. AFTER (same query; MySQL now picks the covering index):
EXPLAIN ANALYZE
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
    WHERE a.region = 'Prague'
    GROUP BY a.district_name
)
SELECT
    c.*,
    ROUND(100 * c.total_amount / NULLIF(SUM(c.total_amount) OVER (), 0), 2) AS pct_of_parent_amount,
    RANK() OVER (ORDER BY c.total_amount DESC) AS amount_rank
FROM children c
ORDER BY c.total_amount DESC;

-- C7. SL-1 Year = 1997 by region: 1059.0 ms -> 343.4 ms (x3.08)
-- C7a. BEFORE: run this with the indexes dropped (see CLEAN-UP) to see 'Table scan on f'.
-- C7b. AFTER (same query; MySQL now picks the covering index):
EXPLAIN ANALYZE
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

-- C8. SL-2 Region = Prague by month: 407.3 ms -> 217.3 ms (x1.87)
-- C8a. BEFORE: run this with the indexes dropped (see CLEAN-UP) to see 'Table scan on f'.
-- C8b. AFTER (same query; MySQL now picks the covering index):
EXPLAIN ANALYZE
WITH slice AS (
    SELECT
        d.month_number AS `month`,
        MAX(d.month_name) AS `month_name`,
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

-- C9. SL-4 Year = 1996 by operation: 675.9 ms -> 252.9 ms (x2.67)
-- C9a. BEFORE: run this with the indexes dropped (see CLEAN-UP) to see 'Table scan on f'.
-- C9b. AFTER (same query; MySQL now picks the covering index):
EXPLAIN ANALYZE
WITH slice AS (
    SELECT
        op.operation_name AS `operation`,
        COUNT(*) AS txn_count,
        ROUND(SUM(f.amount), 2) AS total_amount,
        ROUND(SUM(CASE WHEN tt.transaction_type = 'Credit' THEN f.amount ELSE 0 END), 2) AS total_credit,
        ROUND(SUM(CASE WHEN tt.transaction_type = 'Debit (Withdrawal)' THEN f.amount ELSE 0 END), 2) AS total_debit,
        ROUND(SUM(CASE WHEN tt.transaction_type = 'Credit' THEN f.amount WHEN tt.transaction_type = 'Debit (Withdrawal)' THEN -f.amount ELSE 0 END), 2) AS net_flow
    FROM transaction_fact f
    JOIN `date` d                  ON d.date_key = f.date_key
    JOIN transaction_type tt       ON tt.type_key = f.type_key
    JOIN transaction_operation op  ON op.operation_key = f.operation_key
    WHERE d.year_number = 1996
    GROUP BY op.operation_name
)
SELECT
    s.*,
    ROUND(100 * s.total_amount / NULLIF(SUM(s.total_amount) OVER (), 0), 2) AS pct_of_slice_amount,
    RANK() OVER (ORDER BY s.total_amount DESC) AS amount_rank
FROM slice s
ORDER BY s.total_amount DESC;

-- C10. DC-1 1996-97 x Prague/south Moravia x Credit x amount>=1000: 299.0 ms -> 109.3 ms (x2.74)
-- C10a. BEFORE: run this with the indexes dropped (see CLEAN-UP) to see 'Table scan on f'.
-- C10b. AFTER (same query; MySQL now picks the covering index):
EXPLAIN ANALYZE
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

-- C11. DC-2 1995-97 x 3 regions x Debit, by region and year: 1461.8 ms -> 350.9 ms (x4.17)
-- C11a. BEFORE: run this with the indexes dropped (see CLEAN-UP) to see 'Table scan on f'.
-- C11b. AFTER (same query; MySQL now picks the covering index):
EXPLAIN ANALYZE
WITH diced AS (
    SELECT
        a.region AS `region`,
        d.year_number AS `year`,
        COUNT(*) AS txn_count,
        ROUND(SUM(f.amount), 2) AS total_amount,
        ROUND(SUM(CASE WHEN tt.transaction_type = 'Credit' THEN f.amount ELSE 0 END), 2) AS total_credit,
        ROUND(SUM(CASE WHEN tt.transaction_type = 'Debit (Withdrawal)' THEN f.amount ELSE 0 END), 2) AS total_debit,
        ROUND(SUM(CASE WHEN tt.transaction_type = 'Credit' THEN f.amount WHEN tt.transaction_type = 'Debit (Withdrawal)' THEN -f.amount ELSE 0 END), 2) AS net_flow
    FROM transaction_fact f
    JOIN `date` d                  ON d.date_key = f.date_key
    JOIN account a                 ON a.account_key = f.account_key
    JOIN transaction_type tt       ON tt.type_key = f.type_key
    WHERE d.year_number >= 1995
      AND d.year_number <= 1997
      AND a.region IN ('Prague', 'south Moravia', 'central Bohemia')
      AND tt.transaction_type IN ('Debit (Withdrawal)')
    GROUP BY a.region, d.year_number
)
SELECT
    s.*,
    RANK() OVER (PARTITION BY s.`region` ORDER BY s.total_amount DESC) AS rank_within_region,
    ROUND(100 * s.total_amount / NULLIF(SUM(s.total_amount) OVER (PARTITION BY s.`region`), 0), 2) AS pct_within_region
FROM diced s
ORDER BY s.`region`, s.`year`;

-- C12. DC-3 Q1-Q2 x Prague, by month: 409.6 ms -> 170.2 ms (x2.41)
-- C12a. BEFORE: run this with the indexes dropped (see CLEAN-UP) to see 'Table scan on f'.
-- C12b. AFTER (same query; MySQL now picks the covering index):
EXPLAIN ANALYZE
WITH diced AS (
    SELECT
        d.month_number AS `month`,
        MAX(d.month_name) AS `month_name`,
        COUNT(*) AS txn_count,
        ROUND(SUM(f.amount), 2) AS total_amount,
        ROUND(SUM(CASE WHEN tt.transaction_type = 'Credit' THEN f.amount ELSE 0 END), 2) AS total_credit,
        ROUND(SUM(CASE WHEN tt.transaction_type = 'Debit (Withdrawal)' THEN f.amount ELSE 0 END), 2) AS total_debit,
        ROUND(SUM(CASE WHEN tt.transaction_type = 'Credit' THEN f.amount WHEN tt.transaction_type = 'Debit (Withdrawal)' THEN -f.amount ELSE 0 END), 2) AS net_flow
    FROM transaction_fact f
    JOIN `date` d                  ON d.date_key = f.date_key
    JOIN account a                 ON a.account_key = f.account_key
    JOIN transaction_type tt       ON tt.type_key = f.type_key
    WHERE d.quarter_number IN (1, 2)
      AND a.region IN ('Prague')
    GROUP BY d.month_number
)
SELECT
    s.*,
    RANK() OVER (ORDER BY s.total_amount DESC) AS amount_rank,
    ROUND(100 * s.total_amount / NULLIF(SUM(s.total_amount) OVER (), 0), 2) AS pct_of_dice_amount
FROM diced s
ORDER BY s.`month`;

-- C13. PV-3 Net flow: district x year, region Prague: 420.0 ms -> 235.8 ms (x1.78)
-- C13a. BEFORE: run this with the indexes dropped (see CLEAN-UP) to see 'Table scan on f'.
-- C13b. AFTER (same query; MySQL now picks the covering index):
EXPLAIN ANALYZE
SELECT
    IF(GROUPING(a.district_name) = 1, 'Total', CAST(a.district_name AS CHAR)) AS `district`,
    ROUND(SUM(CASE WHEN d.year_number = 1993 THEN CASE WHEN tt.transaction_type = 'Credit' THEN f.amount WHEN tt.transaction_type = 'Debit (Withdrawal)' THEN -f.amount ELSE 0 END ELSE 0 END), 2) AS `1993`,
    ROUND(SUM(CASE WHEN d.year_number = 1994 THEN CASE WHEN tt.transaction_type = 'Credit' THEN f.amount WHEN tt.transaction_type = 'Debit (Withdrawal)' THEN -f.amount ELSE 0 END ELSE 0 END), 2) AS `1994`,
    ROUND(SUM(CASE WHEN d.year_number = 1995 THEN CASE WHEN tt.transaction_type = 'Credit' THEN f.amount WHEN tt.transaction_type = 'Debit (Withdrawal)' THEN -f.amount ELSE 0 END ELSE 0 END), 2) AS `1995`,
    ROUND(SUM(CASE WHEN d.year_number = 1996 THEN CASE WHEN tt.transaction_type = 'Credit' THEN f.amount WHEN tt.transaction_type = 'Debit (Withdrawal)' THEN -f.amount ELSE 0 END ELSE 0 END), 2) AS `1996`,
    ROUND(SUM(CASE WHEN d.year_number = 1997 THEN CASE WHEN tt.transaction_type = 'Credit' THEN f.amount WHEN tt.transaction_type = 'Debit (Withdrawal)' THEN -f.amount ELSE 0 END ELSE 0 END), 2) AS `1997`,
    ROUND(SUM(CASE WHEN d.year_number = 1998 THEN CASE WHEN tt.transaction_type = 'Credit' THEN f.amount WHEN tt.transaction_type = 'Debit (Withdrawal)' THEN -f.amount ELSE 0 END ELSE 0 END), 2) AS `1998`,
    ROUND(SUM(CASE WHEN tt.transaction_type = 'Credit' THEN f.amount WHEN tt.transaction_type = 'Debit (Withdrawal)' THEN -f.amount ELSE 0 END), 2) AS `Total`
FROM transaction_fact f
JOIN `date` d                  ON d.date_key = f.date_key
JOIN account a                 ON a.account_key = f.account_key
JOIN transaction_type tt       ON tt.type_key = f.type_key
WHERE a.region IN ('Prague')
GROUP BY a.district_name WITH ROLLUP
ORDER BY GROUPING(a.district_name), a.district_name;

-- C14. SL-3 Type = Credit by year: 1109.5 ms -> 127.7 ms (x8.69)
-- C14a. BEFORE: run this with the indexes dropped (see CLEAN-UP) to see 'Table scan on f'.
-- C14b. AFTER (same query; MySQL now picks the covering index):
EXPLAIN ANALYZE
WITH slice AS (
    SELECT
        d.year_number AS `year`,
        COUNT(*) AS txn_count,
        ROUND(SUM(f.amount), 2) AS total_amount,
        ROUND(SUM(CASE WHEN tt.transaction_type = 'Credit' THEN f.amount ELSE 0 END), 2) AS total_credit,
        ROUND(SUM(CASE WHEN tt.transaction_type = 'Debit (Withdrawal)' THEN f.amount ELSE 0 END), 2) AS total_debit,
        ROUND(SUM(CASE WHEN tt.transaction_type = 'Credit' THEN f.amount WHEN tt.transaction_type = 'Debit (Withdrawal)' THEN -f.amount ELSE 0 END), 2) AS net_flow
    FROM transaction_fact f
    JOIN `date` d                  ON d.date_key = f.date_key
    JOIN transaction_type tt       ON tt.type_key = f.type_key
    WHERE tt.transaction_type = 'Credit'
    GROUP BY d.year_number
)
SELECT
    s.*,
    ROUND(100 * s.total_amount / NULLIF(SUM(s.total_amount) OVER (), 0), 2) AS pct_of_slice_amount,
    SUM(s.total_amount) OVER (ORDER BY s.`year` ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS running_total_amount
FROM slice s
ORDER BY s.`year`;

-- C15. DC-4 All years x amount 5000-20000 x Credit, by region: 893.3 ms -> 164.1 ms (x5.44)
-- C15a. BEFORE: run this with the indexes dropped (see CLEAN-UP) to see 'Table scan on f'.
-- C15b. AFTER (same query; MySQL now picks the covering index):
EXPLAIN ANALYZE
WITH diced AS (
    SELECT
        a.region AS `region`,
        COUNT(*) AS txn_count,
        ROUND(SUM(f.amount), 2) AS total_amount,
        ROUND(SUM(CASE WHEN tt.transaction_type = 'Credit' THEN f.amount ELSE 0 END), 2) AS total_credit,
        ROUND(SUM(CASE WHEN tt.transaction_type = 'Debit (Withdrawal)' THEN f.amount ELSE 0 END), 2) AS total_debit,
        ROUND(SUM(CASE WHEN tt.transaction_type = 'Credit' THEN f.amount WHEN tt.transaction_type = 'Debit (Withdrawal)' THEN -f.amount ELSE 0 END), 2) AS net_flow
    FROM transaction_fact f
    JOIN account a                 ON a.account_key = f.account_key
    JOIN transaction_type tt       ON tt.type_key = f.type_key
    WHERE tt.transaction_type IN ('Credit')
      AND f.amount >= 5000.0
      AND f.amount <= 20000.0
    GROUP BY a.region
)
SELECT
    s.*,
    RANK() OVER (ORDER BY s.total_amount DESC) AS amount_rank,
    ROUND(100 * s.total_amount / NULLIF(SUM(s.total_amount) OVER (), 0), 2) AS pct_of_dice_amount
FROM diced s
ORDER BY s.`region`;


/* =====================================================================
   COMPARING BEFORE vs AFTER FOR THE INDEXES
   Run block C once with the indexes in place, then drop them and run it again:
   ===================================================================== */
-- ALTER TABLE transaction_fact DROP INDEX ix_cov_date;
-- ALTER TABLE transaction_fact DROP INDEX ix_cov_account;
-- ALTER TABLE transaction_fact DROP INDEX ix_cov_type;
-- (Do not drop idx_fk_* : they are needed by the foreign keys.)

/* =====================================================================
   CLEAN-UP  (only if you want the original schema back)
   ===================================================================== */
-- DROP TABLE IF EXISTS summary_month_district;