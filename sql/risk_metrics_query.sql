-- Human-readable risk report over the reporting views built by sql/metrics.sql.
-- Run after sql/schema.sql and sql/metrics.sql.

-- 1. Risk-adjusted performance ranking, trailing 252 trading days
SELECT
    rs.symbol,
    ROUND((rs.ann_return * 100)::numeric, 2)          AS ann_return_pct,
    ROUND((rs.ann_vol * 100)::numeric, 2)             AS ann_vol_pct,
    ROUND(rs.sharpe::numeric, 2)                      AS sharpe,
    ROUND(rs.sortino::numeric, 2)                     AS sortino,
    ROUND((rs.max_drawdown * 100)::numeric, 1)        AS max_drawdown_pct,
    ROUND(rs.beta::numeric, 2)                        AS beta_vs_spy,
    ROUND((rs.ewma_vol_current * 100)::numeric, 1)    AS ewma_vol_pct,
    ROUND((rs.pct_anomalous_days * 100)::numeric, 1)  AS pct_anomalous_days
FROM risk_summary rs
ORDER BY rs.is_benchmark, rs.sharpe DESC;

-- 2. Current 1-day VaR / CVaR (% of position value)
SELECT
    vs.symbol,
    ROUND((vs.hist_var_95 * 100)::numeric, 2)      AS hist_var_95_pct,
    ROUND((vs.hist_cvar_95 * 100)::numeric, 2)     AS hist_cvar_95_pct,
    ROUND((vs.hist_var_99 * 100)::numeric, 2)      AS hist_var_99_pct,
    ROUND((vs.hist_cvar_99 * 100)::numeric, 2)     AS hist_cvar_99_pct,
    ROUND((vs.param_var_99 * 100)::numeric, 2)     AS param_var_99_pct,
    ROUND((vs.param_cvar_99 * 100)::numeric, 2)    AS param_cvar_99_pct,
    ROUND((vs.ewma_var_99_next * 100)::numeric, 2) AS ewma_var_99_next_pct
FROM var_summary vs
ORDER BY vs.hist_var_99 DESC;

-- 3. VaR backtest at 99%: exceptions vs. expected, Kupiec POF test, Basel zone
SELECT
    vb.symbol,
    vb.method,
    vb.n_obs,
    vb.exceptions,
    ROUND(vb.expected_exceptions::numeric, 1) AS expected,
    ROUND(vb.kupiec_lr::numeric, 2)           AS kupiec_lr,
    vb.kupiec_reject_5pct,
    vb.exceptions_250d,
    vb.basel_zone
FROM var_backtest vb
WHERE vb.confidence = 0.99
ORDER BY vb.method, vb.kupiec_lr DESC;
