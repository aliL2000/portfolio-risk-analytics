-- Portfolio Risk & Anomaly Monitoring Pipeline — metric computation
-- Run after sql/schema.sql and after ingestion. Rebuilds every derived table and
-- reporting view from daily_prices inside one transaction, so readers (Power BI, the snapshot script)
-- never see a half-built state. All metrics are computed here in SQL.
--
-- Conventions: returns are simple daily returns stored as fractions (0.01 = 1%).
-- VaR/CVaR are 1-day, expressed as positive loss fractions.

SET client_min_messages = warning;

BEGIN;

-- ---------------------------------------------------------------------------
-- Step 1: daily returns from raw prices
-- ---------------------------------------------------------------------------
DELETE FROM daily_returns;
INSERT INTO daily_returns (symbol, trade_date, daily_return)
SELECT symbol, trade_date, daily_return
FROM (
    SELECT
        symbol,
        trade_date,
        close_price / LAG(close_price) OVER (PARTITION BY symbol ORDER BY trade_date) - 1 AS daily_return
    FROM daily_prices
) r
WHERE daily_return IS NOT NULL;

-- Working copy: float returns, a per-symbol sequence number, and the benchmark
-- (SPY) return on the same date for beta.
CREATE TEMP TABLE tmp_ret ON COMMIT DROP AS
SELECT
    d.symbol,
    d.trade_date,
    d.daily_return::float8 AS r,
    b.daily_return::float8 AS mkt_r,
    ROW_NUMBER() OVER (PARTITION BY d.symbol ORDER BY d.trade_date) AS rn
FROM daily_returns d
LEFT JOIN daily_returns b
       ON b.trade_date = d.trade_date
      AND b.symbol = (SELECT symbol FROM watchlist WHERE is_benchmark LIMIT 1);
CREATE INDEX ON tmp_ret (symbol, rn);
ANALYZE tmp_ret;

-- ---------------------------------------------------------------------------
-- Step 2: EWMA variance (RiskMetrics, lambda = 0.94)
--   var_t = lambda * var_{t-1} + (1 - lambda) * r_t^2
-- var_t is the variance estimate as of close t, i.e. the forecast for day t+1.
-- The recursion is seeded with the sample variance of the first 20 returns;
-- that burn-in uses a little look-ahead, so backtests start much later (Step 4).
-- ---------------------------------------------------------------------------
CREATE TEMP TABLE tmp_ewma ON COMMIT DROP AS
WITH RECURSIVE seed AS (
    SELECT symbol, VAR_SAMP(r) AS var0
    FROM tmp_ret
    WHERE rn <= 20
    GROUP BY symbol
),
ewma (symbol, rn, ewma_var) AS (
    SELECT t.symbol, t.rn, 0.94 * s.var0 + 0.06 * t.r * t.r
    FROM tmp_ret t
    JOIN seed s USING (symbol)
    WHERE t.rn = 1
    UNION ALL
    SELECT t.symbol, t.rn, 0.94 * e.ewma_var + 0.06 * t.r * t.r
    FROM ewma e
    JOIN tmp_ret t ON t.symbol = e.symbol AND t.rn = e.rn + 1
)
SELECT symbol, rn, ewma_var FROM ewma;
CREATE INDEX ON tmp_ewma (symbol, rn);

-- ---------------------------------------------------------------------------
-- Step 3: rolling metrics per symbol-day
--   rolling_vol_20d   20-day sample std dev of returns (incl. today), annualized
--   z_score           today's return vs. the mean/std of the PRIOR 20 days.
--                     Today is excluded from its own baseline; including it
--                     shrinks the z-score and caps it at (n-1)/sqrt(n) ~ 4.25.
--   ewma_vol          EWMA volatility as of close, annualized
--   rolling_beta_60d  OLS slope of the stock's return on SPY's over 60 days
-- ---------------------------------------------------------------------------
DELETE FROM computed_metrics;
INSERT INTO computed_metrics
    (symbol, trade_date, rolling_vol_20d, rolling_return_20d, z_score, is_anomalous, ewma_vol, rolling_beta_60d)
SELECT
    symbol,
    trade_date,
    CASE WHEN n_20 = 20 THEN std_20 * SQRT(252) END,
    CASE WHEN n_20 = 20 THEN avg_20 END,
    CASE WHEN n_prior = 20 AND std_prior > 0 THEN (r - avg_prior) / std_prior END,
    COALESCE(n_prior = 20 AND std_prior > 0 AND ABS((r - avg_prior) / std_prior) > 2, FALSE),
    SQRT(ewma_var * 252),
    CASE WHEN n_beta = 60 THEN beta_60 END
FROM (
    SELECT
        t.symbol,
        t.trade_date,
        t.r,
        e.ewma_var,
        COUNT(*)          OVER w20    AS n_20,
        STDDEV_SAMP(t.r)  OVER w20    AS std_20,
        AVG(t.r)          OVER w20    AS avg_20,
        COUNT(*)          OVER prior  AS n_prior,
        STDDEV_SAMP(t.r)  OVER prior  AS std_prior,
        AVG(t.r)          OVER prior  AS avg_prior,
        REGR_COUNT(t.r, t.mkt_r) OVER w60 AS n_beta,
        REGR_SLOPE(t.r, t.mkt_r) OVER w60 AS beta_60
    FROM tmp_ret t
    JOIN tmp_ewma e USING (symbol, rn)
    WINDOW
        w20   AS (PARTITION BY t.symbol ORDER BY t.rn ROWS BETWEEN 19 PRECEDING AND CURRENT ROW),
        prior AS (PARTITION BY t.symbol ORDER BY t.rn ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING),
        w60   AS (PARTITION BY t.symbol ORDER BY t.rn ROWS BETWEEN 59 PRECEDING AND CURRENT ROW)
) m;

-- ---------------------------------------------------------------------------
-- Step 4: out-of-sample 1-day VaR forecasts for backtesting
-- Each forecast for day t uses only information up to the close of t-1.
--   historical   empirical quantile of the previous 250 returns
--   ewma_normal  z * EWMA sigma from the previous close (zero mean, RiskMetrics)
-- Both methods start at the same day (once 250 prior returns exist) so their
-- backtests cover an identical sample.
-- ---------------------------------------------------------------------------
DELETE FROM var_forecasts;

INSERT INTO var_forecasts (symbol, trade_date, method, confidence, var_1d, actual_return, is_exception)
SELECT t.symbol, t.trade_date, 'historical', v.confidence, v.var_1d, t.r, t.r < -v.var_1d
FROM tmp_ret t
CROSS JOIN LATERAL (
    SELECT
        PERCENTILE_CONT(0.05) WITHIN GROUP (ORDER BY h.r) AS q05,
        PERCENTILE_CONT(0.01) WITHIN GROUP (ORDER BY h.r) AS q01
    FROM tmp_ret h
    WHERE h.symbol = t.symbol AND h.rn BETWEEN t.rn - 250 AND t.rn - 1
) q
CROSS JOIN LATERAL (VALUES (0.95, -q.q05), (0.99, -q.q01)) v (confidence, var_1d)
WHERE t.rn > 250;

INSERT INTO var_forecasts (symbol, trade_date, method, confidence, var_1d, actual_return, is_exception)
SELECT t.symbol, t.trade_date, 'ewma_normal', v.confidence, v.z * SQRT(e.ewma_var), t.r,
       t.r < -v.z * SQRT(e.ewma_var)
FROM tmp_ret t
JOIN tmp_ewma e ON e.symbol = t.symbol AND e.rn = t.rn - 1
CROSS JOIN (VALUES (0.95, 1.6448536), (0.99, 2.3263479)) v (confidence, z)
WHERE t.rn > 250;

-- ---------------------------------------------------------------------------
-- Reporting views (the layer Power BI and scripts/daily_snapshot.py read)
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS risk_summary, var_summary, var_backtest, trailing_returns CASCADE;

-- The most recent 252 trading days (~1 year) of returns per symbol, with the
-- benchmark's return on the same date. All point-in-time summaries use this window.
CREATE VIEW trailing_returns AS
SELECT symbol, trade_date, r, mkt_r
FROM (
    SELECT
        d.symbol,
        d.trade_date,
        d.daily_return::float8 AS r,
        b.daily_return::float8 AS mkt_r,
        ROW_NUMBER() OVER (PARTITION BY d.symbol ORDER BY d.trade_date DESC) AS age
    FROM daily_returns d
    LEFT JOIN daily_returns b
           ON b.trade_date = d.trade_date
          AND b.symbol = (SELECT symbol FROM watchlist WHERE is_benchmark LIMIT 1)
) x
WHERE age <= 252;

-- Risk-adjusted performance over the trailing window.
--   sharpe   (annualized mean return - rf) / annualized std dev
--   sortino  (annualized mean return - rf) / annualized downside deviation, where
--            downside deviation = sqrt(mean over ALL days of min(r - rf_daily, 0)^2).
--            Days above the target count as zero rather than being dropped.
--   max_drawdown  worst peak-to-trough fall of the compounded wealth index
CREATE VIEW risk_summary AS
WITH params AS (
    SELECT 0.04::float8 AS rf_annual   -- risk-free rate placeholder (see README)
),
wealth AS (
    SELECT symbol, trade_date,
           EXP(SUM(LN(1 + r)) OVER (PARTITION BY symbol ORDER BY trade_date)) AS wealth
    FROM trailing_returns
),
drawdown AS (
    SELECT symbol, MIN(wealth / GREATEST(peak, 1.0) - 1) AS max_drawdown
    FROM (
        SELECT symbol, wealth, MAX(wealth) OVER (PARTITION BY symbol ORDER BY trade_date) AS peak
        FROM wealth
    ) p
    GROUP BY symbol
),
latest AS (
    SELECT DISTINCT ON (symbol) symbol, ewma_vol
    FROM computed_metrics
    ORDER BY symbol, trade_date DESC
),
anomalies AS (
    SELECT tr.symbol, AVG(CASE WHEN cm.is_anomalous THEN 1.0 ELSE 0.0 END) AS pct_anomalous_days
    FROM trailing_returns tr
    JOIN computed_metrics cm USING (symbol, trade_date)
    GROUP BY tr.symbol
),
agg AS (
    SELECT
        tr.symbol,
        COUNT(*)                                              AS n_days,
        MIN(tr.trade_date)                                    AS window_start,
        MAX(tr.trade_date)                                    AS window_end,
        AVG(tr.r) * 252                                       AS ann_return,
        STDDEV_SAMP(tr.r) * SQRT(252)                         AS ann_vol,
        SQRT(AVG(POWER(LEAST(tr.r - p.rf_annual / 252, 0), 2))) * SQRT(252) AS ann_downside_dev,
        REGR_SLOPE(tr.r, tr.mkt_r)                            AS beta,
        CORR(tr.r, tr.mkt_r)                                  AS corr_to_benchmark,
        p.rf_annual
    FROM trailing_returns tr
    CROSS JOIN params p
    GROUP BY tr.symbol, p.rf_annual
)
SELECT
    a.symbol,
    w.is_benchmark,
    a.n_days,
    a.window_start,
    a.window_end,
    a.ann_return,
    a.ann_vol,
    (a.ann_return - a.rf_annual) / NULLIF(a.ann_vol, 0)          AS sharpe,
    (a.ann_return - a.rf_annual) / NULLIF(a.ann_downside_dev, 0) AS sortino,
    d.max_drawdown,
    a.beta,
    a.corr_to_benchmark,
    l.ewma_vol                                                   AS ewma_vol_current,
    an.pct_anomalous_days
FROM agg a
JOIN watchlist w USING (symbol)
JOIN drawdown d USING (symbol)
JOIN latest l USING (symbol)
JOIN anomalies an USING (symbol);

-- Current 1-day VaR / CVaR (expected shortfall) over the trailing window.
--   hist_*   empirical: VaR = -quantile, CVaR = -mean of returns at or below it
--   param_*  Gaussian with the window's sample mean/std:
--            VaR = z*sigma - mu,  CVaR = sigma*phi(z)/(1-c) - mu
--   ewma_var_99_next  RiskMetrics forecast for the next session
-- At 99% the trailing window holds only ~2-3 tail observations, so hist_cvar_99
-- is noisy by construction.
CREATE VIEW var_summary AS
WITH q AS (
    SELECT
        symbol,
        PERCENTILE_CONT(0.05) WITHIN GROUP (ORDER BY r) AS q05,
        PERCENTILE_CONT(0.01) WITHIN GROUP (ORDER BY r) AS q01,
        AVG(r)         AS mu,
        STDDEV_SAMP(r) AS sigma
    FROM trailing_returns
    GROUP BY symbol
),
tail AS (
    SELECT
        q.symbol,
        -AVG(tr.r) FILTER (WHERE tr.r <= q.q05) AS hist_cvar_95,
        -AVG(tr.r) FILTER (WHERE tr.r <= q.q01) AS hist_cvar_99
    FROM trailing_returns tr
    JOIN q USING (symbol)
    GROUP BY q.symbol
),
latest AS (
    SELECT DISTINCT ON (symbol) symbol, ewma_vol
    FROM computed_metrics
    ORDER BY symbol, trade_date DESC
)
SELECT
    q.symbol,
    -q.q05                            AS hist_var_95,
    t.hist_cvar_95,
    -q.q01                            AS hist_var_99,
    t.hist_cvar_99,
    1.6448536 * q.sigma - q.mu        AS param_var_95,
    2.0627128 * q.sigma - q.mu        AS param_cvar_95,   -- phi(1.645)/0.05
    2.3263479 * q.sigma - q.mu        AS param_var_99,
    2.6652142 * q.sigma - q.mu        AS param_cvar_99,   -- phi(2.326)/0.01
    2.3263479 * l.ewma_vol / SQRT(252) AS ewma_var_99_next
FROM q
JOIN tail t USING (symbol)
JOIN latest l USING (symbol);

-- VaR backtest: Kupiec (1995) proportion-of-failures test.
--   H0: the true exception probability equals p = 1 - confidence.
--   LR_pof = -2 ln[(1-p)^(T-x) p^x] + 2 ln[(1-x/T)^(T-x) (x/T)^x]  ~  chi2(1)
--   Reject at 5% when LR_pof > 3.841. Rejection can mean too many exceptions
--   (risk understated) or too few (risk overstated); compare x with expected.
-- basel_zone applies the Basel traffic light to the latest 250 days at 99%:
--   green 0-4 exceptions, yellow 5-9, red 10+.
CREATE VIEW var_backtest AS
WITH agg AS (
    SELECT
        symbol, method, confidence,
        MIN(trade_date)                AS test_start,
        MAX(trade_date)                AS test_end,
        COUNT(*)                       AS n_obs,
        COUNT(*) FILTER (WHERE is_exception) AS exceptions,
        (1 - confidence)::float8       AS p
    FROM var_forecasts
    GROUP BY symbol, method, confidence
),
last_250 AS (
    SELECT symbol, method, confidence, COUNT(*) FILTER (WHERE is_exception) AS exceptions_250d
    FROM (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY symbol, method, confidence ORDER BY trade_date DESC) AS k
        FROM var_forecasts
    ) x
    WHERE k <= 250
    GROUP BY symbol, method, confidence
),
lr AS (
    SELECT
        a.*,
        -2 * ((a.n_obs - a.exceptions) * LN(1 - a.p) + a.exceptions * LN(a.p))
        + 2 * (CASE WHEN a.exceptions < a.n_obs
                    THEN (a.n_obs - a.exceptions) * LN(1 - a.exceptions::float8 / a.n_obs) ELSE 0 END
             + CASE WHEN a.exceptions > 0
                    THEN a.exceptions * LN(a.exceptions::float8 / a.n_obs) ELSE 0 END) AS kupiec_lr
    FROM agg a
)
SELECT
    lr.symbol,
    w.is_benchmark,
    lr.method,
    lr.confidence,
    lr.test_start,
    lr.test_end,
    lr.n_obs,
    lr.exceptions,
    lr.n_obs * lr.p                     AS expected_exceptions,
    lr.exceptions::float8 / lr.n_obs    AS exception_rate,
    lr.kupiec_lr,
    lr.kupiec_lr > 3.841459             AS kupiec_reject_5pct,
    l.exceptions_250d,
    CASE WHEN lr.confidence = 0.99 THEN
        CASE WHEN l.exceptions_250d <= 4 THEN 'green'
             WHEN l.exceptions_250d <= 9 THEN 'yellow'
             ELSE 'red' END
    END                                 AS basel_zone
FROM lr
JOIN last_250 l USING (symbol, method, confidence)
JOIN watchlist w USING (symbol);

COMMIT;
