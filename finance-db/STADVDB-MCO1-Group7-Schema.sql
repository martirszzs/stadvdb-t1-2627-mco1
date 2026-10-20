-- =====================================================================
-- STADVDB MCO1 - Group 7
-- Data warehouse (star schema) for the Berka / PKDD'99 financial dataset
--
--   Source DB    : financedata   (MCO1 - Dataset financedataclean.sql)
--   Warehouse DB : finance_mco1  (this script)
--
-- Star schema: 1 fact table + 4 dimension tables
--   transaction_fact  -> date, account, transaction_type, transaction_operation
--
-- Hierarchies used by the OLAP application
--   Time        : year_number > quarter_number > month_number > full_date
--   Location    : region > district_name > account_id   (in dimension `account`)
--   Transaction : transaction_type > operation_name
--
-- Surrogate keys are assigned by the ETL (finance-db/etl.py).
-- Key -1 is the "Unknown" member of a dimension, so fact foreign keys are
-- never NULL and INNER JOINs never silently drop fact rows.
--
-- The script is re-runnable: it drops and recreates every warehouse table.
-- =====================================================================

CREATE DATABASE IF NOT EXISTS finance_mco1;
USE finance_mco1;

-- drop in dependency order (fact first)
DROP TABLE IF EXISTS transaction_fact;
DROP TABLE IF EXISTS account;
DROP TABLE IF EXISTS `date`;
DROP TABLE IF EXISTS transaction_type;
DROP TABLE IF EXISTS transaction_operation;

-- creation of tables
CREATE TABLE account (
    account_key INT PRIMARY KEY,
    account_id INT NOT NULL UNIQUE,
    account_frequency VARCHAR(50) NOT NULL DEFAULT 'Unknown',
    account_open_date DATE,
    district_id INT,
    district_name VARCHAR(100) NOT NULL DEFAULT 'Unknown',
    region VARCHAR(100) NOT NULL DEFAULT 'Unknown',

    INDEX idx_account_location (region, district_name)
);

CREATE TABLE `date` (
    date_key INT PRIMARY KEY,               -- YYYYMMDD
    full_date DATE NOT NULL UNIQUE,
    day_number INT NOT NULL,
    month_number INT NOT NULL,
    month_name VARCHAR(20) NOT NULL,
    quarter_number INT NOT NULL,
    year_number INT NOT NULL,
    quarter_label CHAR(7) NOT NULL,         -- e.g. 1997-Q1
    month_label CHAR(7) NOT NULL,           -- e.g. 1997-01

    INDEX idx_date_hierarchy (year_number, quarter_number, month_number)
);

CREATE TABLE transaction_type (
    type_key INT PRIMARY KEY,
    transaction_type VARCHAR(100) NOT NULL UNIQUE
);

CREATE TABLE transaction_operation (
    operation_key INT PRIMARY KEY,
    operation_name VARCHAR(150) NOT NULL UNIQUE
);

CREATE TABLE transaction_fact (
    trans_id INT PRIMARY KEY,
    date_key INT NOT NULL,
    account_key INT NOT NULL,
    type_key INT NOT NULL,
    operation_key INT NOT NULL,
    amount DECIMAL(15,2) NOT NULL,
    balance DECIMAL(15,2) NOT NULL,         -- semi-additive: never SUM across time

    CHECK (amount >= 0),

    FOREIGN KEY (date_key)
        REFERENCES `date`(date_key),

    FOREIGN KEY (account_key)
        REFERENCES account(account_key),

    FOREIGN KEY (type_key)
        REFERENCES transaction_type(type_key),

    FOREIGN KEY (operation_key)
        REFERENCES transaction_operation(operation_key)
);
