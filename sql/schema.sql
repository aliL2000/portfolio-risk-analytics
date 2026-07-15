-- Portfolio Risk & Anomaly Monitoring Pipeline — PostgreSQL Schema
-- Works on any free Postgres host (Neon, Supabase, local Postgres via Docker).
-- Run once to set up tables before the first pipeline execution.

CREATE TABLE IF NOT EXISTS watchlist (
    symbol VARCHAR(10) PRIMARY KEY,
    company_name VARCHAR(100),
    sector VARCHAR(50),
    market_cap_at_selection NUMERIC(20,2),
    date_added DATE
);

CREATE TABLE IF NOT EXISTS daily_prices (
    symbol VARCHAR(10),
    trade_date DATE,
    close_price NUMERIC(12,4),
    volume BIGINT,
    ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, trade_date),
    FOREIGN KEY (symbol) REFERENCES watchlist(symbol)
);

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

-- Step 1: compute daily returns from raw prices
INSERT INTO daily_returns (symbol, trade_date, daily_return)
SELECT
    symbol,
    trade_date,
    close_price / LAG(close_price) OVER (PARTITION BY symbol ORDER BY trade_date) - 1 AS daily_return
FROM daily_prices
ON CONFLICT (symbol, trade_date) DO UPDATE SET daily_return = EXCLUDED.daily_return;

-- Step 2: rolling 20-day volatility, return, and z-score anomaly flag
-- Postgres supports STDDEV / STDDEV_SAMP as a window function natively
INSERT INTO computed_metrics (symbol, trade_date, rolling_vol_20d, rolling_return_20d, z_score, is_anomalous)
SELECT
    symbol,
    trade_date,
    rolling_std * SQRT(252) AS rolling_vol_20d,
    rolling_avg AS rolling_return_20d,
    CASE WHEN rolling_std = 0 OR rolling_std IS NULL THEN NULL
         ELSE (daily_return - rolling_avg) / rolling_std END AS z_score,
    CASE WHEN rolling_std = 0 OR rolling_std IS NULL THEN FALSE
         ELSE ABS((daily_return - rolling_avg) / rolling_std) > 2 END AS is_anomalous
FROM (
    SELECT
        symbol,
        trade_date,
        daily_return,
        STDDEV_SAMP(daily_return) OVER (
            PARTITION BY symbol ORDER BY trade_date
            ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
        ) AS rolling_std,
        AVG(daily_return) OVER (
            PARTITION BY symbol ORDER BY trade_date
            ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
        ) AS rolling_avg
    FROM daily_returns
    WHERE daily_return IS NOT NULL
) sub
ON CONFLICT (symbol, trade_date) DO UPDATE SET
    rolling_vol_20d = EXCLUDED.rolling_vol_20d,
    rolling_return_20d = EXCLUDED.rolling_return_20d,
    z_score = EXCLUDED.z_score,
    is_anomalous = EXCLUDED.is_anomalous;

-- Portfolio-level summary query — this is the one you'd screenshot for PowerBI
SELECT
    dr.symbol,
    ROUND(AVG(dr.daily_return) * 252 * 100, 2) AS annualized_return_pct,
    ROUND(STDDEV_SAMP(dr.daily_return) * SQRT(252) * 100, 2) AS annualized_vol_pct,
    ROUND(SUM(CASE WHEN cm.is_anomalous THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 1) AS pct_anomalous_days
FROM daily_returns dr
JOIN computed_metrics cm USING (symbol, trade_date)
GROUP BY dr.symbol
ORDER BY annualized_vol_pct DESC;
