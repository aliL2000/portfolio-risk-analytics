-- Portfolio Risk & Anomaly Monitoring Pipeline — PostgreSQL Schema
-- Works on any free Postgres host (Neon, Supabase, local Postgres via Docker).
-- Idempotent: safe to re-run on every pipeline execution. Metric computation
-- lives in sql/metrics.sql.

SET client_min_messages = warning;

CREATE TABLE IF NOT EXISTS watchlist (
    symbol VARCHAR(10) PRIMARY KEY,
    company_name VARCHAR(100),
    sector VARCHAR(50),
    market_cap_at_selection NUMERIC(20,2),
    date_added DATE,
    is_benchmark BOOLEAN NOT NULL DEFAULT FALSE
);
-- Migration for databases created before the benchmark column existed
ALTER TABLE watchlist ADD COLUMN IF NOT EXISTS is_benchmark BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS daily_prices (
    symbol VARCHAR(10),
    trade_date DATE,
    close_price NUMERIC(12,4),
    volume BIGINT,
    ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, trade_date),
    FOREIGN KEY (symbol) REFERENCES watchlist(symbol)
);

-- Data-quality guard. Postgres NUMERIC accepts 'NaN', and NaN compares greater
-- than every number, so a single NaN close from yfinance silently poisons every
-- AVG/STDDEV for that symbol and floats it to the top of any DESC ranking.
DELETE FROM daily_prices WHERE close_price = 'NaN' OR close_price <= 0;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'daily_prices_close_valid') THEN
        ALTER TABLE daily_prices ADD CONSTRAINT daily_prices_close_valid
            CHECK (close_price <> 'NaN' AND close_price > 0);
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS daily_returns (
    symbol VARCHAR(10),
    trade_date DATE,
    daily_return NUMERIC(10,6),
    PRIMARY KEY (symbol, trade_date)
);

CREATE TABLE IF NOT EXISTS computed_metrics (
    symbol VARCHAR(10),
    trade_date DATE,
    rolling_vol_20d NUMERIC(10,6),
    rolling_return_20d NUMERIC(10,6),
    z_score NUMERIC(10,4),
    is_anomalous BOOLEAN,
    PRIMARY KEY (symbol, trade_date)
);
ALTER TABLE computed_metrics ADD COLUMN IF NOT EXISTS ewma_vol DOUBLE PRECISION;
ALTER TABLE computed_metrics ADD COLUMN IF NOT EXISTS rolling_beta_60d DOUBLE PRECISION;

-- One row per (symbol, day, method, confidence): the 1-day VaR forecast made at
-- the previous close, and whether the realized return breached it.
CREATE TABLE IF NOT EXISTS var_forecasts (
    symbol VARCHAR(10),
    trade_date DATE,
    method VARCHAR(20),          -- 'historical' | 'ewma_normal'
    confidence NUMERIC(4,3),     -- 0.95 | 0.99
    var_1d DOUBLE PRECISION,     -- positive number = loss, as a fraction of value
    actual_return DOUBLE PRECISION,
    is_exception BOOLEAN,
    PRIMARY KEY (symbol, trade_date, method, confidence)
);

CREATE TABLE IF NOT EXISTS ingestion_log (
    run_id SERIAL PRIMARY KEY,
    run_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    symbols_attempted INTEGER,
    symbols_succeeded INTEGER,
    symbols_failed INTEGER,
    error_detail TEXT,
    latest_trade_date_pulled DATE
);

-- Seed the watchlist (run once)
INSERT INTO watchlist (symbol, company_name, sector, market_cap_at_selection, date_added) VALUES
    ('NVDA','NVIDIA Corporation','Technology',5109662160000,CURRENT_DATE),
    ('AAPL','Apple Inc.','Technology',4631217093920,CURRENT_DATE),
    ('GOOGL','Alphabet Inc.','Communication Services',4320045771254,CURRENT_DATE),
    ('MSFT','Microsoft Corporation','Technology',2860688393000,CURRENT_DATE),
    ('AMZN','Amazon.com, Inc.','Consumer Cyclical',2639146914000,CURRENT_DATE),
    ('META','Meta Platforms, Inc.','Communication Services',1698738339575,CURRENT_DATE),
    ('TSLA','Tesla, Inc.','Consumer Cyclical',1531432387200,CURRENT_DATE),
    ('AMD','Advanced Micro Devices, Inc.','Technology',909695434000,CURRENT_DATE),
    ('WMT','Walmart Inc.','Consumer Defensive',906425312000,CURRENT_DATE),
    ('JPM','JPMorgan Chase & Co.','Financial Services',893174938519,CURRENT_DATE),
    ('V','Visa Inc.','Financial Services',668914895198,CURRENT_DATE),
    ('JNJ','Johnson & Johnson','Healthcare',618607395600,CURRENT_DATE),
    ('XOM','Exxon Mobil Corporation','Energy',571878774436,CURRENT_DATE),
    ('INTC','Intel Corp.','Technology',552055840000,CURRENT_DATE),
    ('CSCO','Cisco Systems, Inc.','Technology',478135439211,CURRENT_DATE),
    ('ABBV','AbbVie Inc.','Healthcare',438305963034,CURRENT_DATE),
    ('BAC','Bank of America Corporation','Financial Services',419877592116,CURRENT_DATE),
    ('COST','Costco Wholesale Corporation','Consumer Defensive',406337633750,CURRENT_DATE),
    ('UNH','UnitedHealth Group Incorporated','Healthcare',385616276826,CURRENT_DATE),
    ('GE','GE Aerospace','Industrials',375375921051,CURRENT_DATE)
ON CONFLICT (symbol) DO NOTHING;

-- Market benchmark for beta. Tracked like any other symbol but excluded from rankings.
INSERT INTO watchlist (symbol, company_name, sector, market_cap_at_selection, date_added, is_benchmark) VALUES
    ('SPY','SPDR S&P 500 ETF Trust','Benchmark',NULL,CURRENT_DATE,TRUE)
ON CONFLICT (symbol) DO UPDATE SET is_benchmark = TRUE;
