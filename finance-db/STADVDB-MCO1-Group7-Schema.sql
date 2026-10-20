-- STADVDB MCO1 - Group 7
-- data warehouse (star schema) for the Berka financial dataset
--
-- source db: financedata (from MCO1 - Dataset financedataclean.sql)
-- warehouse db: finance_mco1 (made by this script)
--
-- 1 fact table (transaction_fact) and 4 dimensions:
-- date, account, transaction_type, transaction_operation
--
-- hierarchies:
--   time: year > quarter > month > day
--   location: region > district > account
--   transaction: type > operation
--
-- the keys are assigned by the ETL (finance-db/etl.py). key -1 is the
-- "Unknown" row in each dimension, so the fact table never has NULL keys.
-- safe to run again, it drops and recreates the tables.

CREATE DATABASE IF NOT EXISTS finance_mco1;
USE finance_mco1;

-- drop the fact table first because of the foreign keys
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
    balance DECIMAL(15,2) NOT NULL,         -- snapshot value, don't add it up over time

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
