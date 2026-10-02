# Portfolio Risk & Anomaly Monitoring Pipeline

![Daily Pipeline](https://github.com/aliL2000/portfolio-risk-analytics/actions/workflows/daily_pipeline.yml/badge.svg)
![Tests](https://github.com/aliL2000/portfolio-risk-analytics/actions/workflows/ci.yml/badge.svg)
![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)
![Python](https://img.shields.io/badge/python-3.11-blue.svg)

An automated pipeline that ingests daily price data for a fixed watchlist of 20
large-cap S&P 500 stocks (plus SPY as the market benchmark) and computes, in SQL:
risk-adjusted returns, market beta, EWMA volatility, historical and parametric
VaR / CVaR, an out-of-sample VaR backtest with the Kupiec test, and z-score
anomaly flags. It is built to answer the questions a market-risk desk actually asks:
*"how much can this position lose on a bad day, is our VaR model telling the truth,
and is anything behaving abnormally right now?"*

This is a personal project, not a production system or investment advice.

## Why this project

Most "stock analysis" portfolio projects predict prices — a noisy, overclaimed
problem that doesn't reflect what a fintech risk or analytics team is actually
asked to build. This project instead focuses on the more tractable, more useful
questions: given a fixed universe of assets, how much risk does each one carry,
which ones are compensating investors for that risk, does the risk model hold up
out of sample, and which names are behaving abnormally right now?

## Architecture

```
yfinance API
     │
     ▼
ingest_watchlist_yfinance.py  ──►  Postgres: daily_prices (3-year window, refreshed
                                   in full each run; NaN/non-positive closes rejected)
                                          │
                                          ▼
                        sql/metrics.sql (one transaction):
                        daily_returns ─► computed_metrics
                                         (20d vol, z-score flag, EWMA vol,
                                          60d rolling beta vs SPY)
                                    ─► var_forecasts
                                         (out-of-sample 1-day VaR, 2 methods
                                          × 2 confidence levels, breach flags)
                                    ─► views: risk_summary, var_summary,
                                              var_backtest, trailing_returns
                                          │
                       ┌──────────────────┴──────────────────┐
                       ▼                                     ▼
     scripts/daily_snapshot.py                       Power BI / any SQL client
     (SNAPSHOT.md, CSVs, charts)                     (reads the views directly)
```

A GitHub Actions job runs the whole pipeline every weekday after the US close and
commits the refreshed snapshot, CSVs and charts back to the repo.

## Watchlist

A fixed set of 20 large-cap S&P 500 names chosen in July 2026, spread across
sectors: NVDA, AAPL, GOOGL, MSFT, AMZN, META, TSLA, AMD, WMT, JPM, V, JNJ, XOM,
INTC, CSCO, ABBV, BAC, COST, UNH, GE. SPY is tracked as the market benchmark for
beta and is excluded from rankings.

The list is intentionally fixed rather than re-screened live, so results are
reproducible and comparable over time. It is not the top 20 by market cap.

## Methodology

**Data**
- **Source:** yfinance (Yahoo Finance), daily close adjusted for splits and
  dividends, 3-year lookback.
- **Refresh, not append:** adjusted closes are restated retroactively whenever a
  dividend or split occurs, so each run re-pulls the full window and replaces the
  stored series. Appending only the newest days would splice two adjustment bases
  together and create a false return at the seam. A Postgres `CHECK` constraint
  rejects `NaN` and non-positive prices (see Limitations for why that matters).

**Performance** (trailing 252 trading days, `risk_summary` view)
- **Returns:** simple daily percentage change, computed in SQL via `LAG()`.
- **Annualized return / volatility:** mean daily return × 252, sample std dev × √252.
- **Sharpe ratio:** `(annualized return − rf) / annualized volatility`, rf = 4%
  (placeholder — see Limitations).
- **Sortino ratio:** `(annualized return − rf) / annualized downside deviation`,
  where downside deviation = `√(mean over all days of min(rᵢ − rf/252, 0)²) × √252`.
  Every day is in the denominator. Days above the target count as zero rather
  than being dropped, which is the standard target downside deviation.
- **Max drawdown:** worst peak-to-trough fall of the compounded wealth index.
- **Beta vs SPY:** OLS slope of the stock's daily return on SPY's (`REGR_SLOPE`),
  over the trailing year and as a 60-day rolling series.

**Volatility**
- **Rolling 20-day volatility:** sample std dev of the last 20 returns, annualized.
- **EWMA volatility (RiskMetrics, λ = 0.94):** `σ²ₜ = λσ²ₜ₋₁ + (1 − λ)rₜ²`,
  computed with a recursive CTE. It weights recent days most heavily, so it reacts
  to a volatility shock within days rather than waiting for it to enter and leave
  a flat window.

**Value at Risk and expected shortfall** (1-day, 95% and 99%, `var_summary` view)
- **Historical VaR:** the empirical 5th / 1st percentile loss over the trailing
  252 days. **Historical CVaR:** the average loss on days at or beyond that cut-off.
- **Parametric (Gaussian) VaR / CVaR:** `VaR = zσ − μ`, `CVaR = σ·φ(z)/(1 − c) − μ`.
- **EWMA VaR:** `z × σ_EWMA`, the RiskMetrics forecast for the next session.

**VaR backtest** (`var_forecasts` table, `var_backtest` view)
- Every day, each model forecasts the next day's 99% and 95% VaR **using only data
  up to the previous close**: historical simulation over the previous 250 returns,
  and EWMA-normal. A breach (exception) is a realized loss larger than the forecast.
- **Kupiec proportion-of-failures test:**
  `LR = −2 ln[(1−p)^(T−x) pˣ] + 2 ln[(1−x/T)^(T−x) (x/T)ˣ] ~ χ²(1)`; reject the model
  at 5% when LR > 3.84. A rejection can mean too many breaches (risk understated)
  or too few (risk overstated).
- **Basel traffic light** on the latest 250 days at 99%: green 0–4 breaches,
  yellow 5–9, red 10+.

**Anomaly flag**
- A day is flagged when its return is more than 2 standard deviations from the
  mean of the **prior** 20 days, per symbol. The day being tested is excluded from
  its own baseline. Including it would shrink the z-score, capping it at about 4.25.

All metrics beyond raw ingestion are computed in SQL (window functions, ordered-set
aggregates, `LATERAL` joins, a recursive CTE), not pandas — a deliberate choice to
demonstrate SQL-native analytics. Every SQL metric is
[cross-checked against an independent pandas/NumPy implementation](tests/test_metrics_crosscheck.py)
on every push (see [Testing](#testing)).

## Findings (trailing year to 14 Jul 2026)

### Risk-adjusted performance

| Symbol | Ann. Return | Ann. Vol | Sharpe | Sortino | Max DD | Beta | 99% 1-day VaR (hist.) |
|---|---|---|---|---|---|---|---|
| JNJ | 52.4% | 18.7% | **2.58** | 4.61 | −11.0% | −0.13 | 2.3% |
| GOOGL | 73.8% | 29.9% | 2.34 | 4.31 | −20.4% | 1.34 | 3.6% |
| INTC | 181.6% | 76.8% | 2.31 | 4.13 | −26.8% | 2.63 | **9.3%** |
| AMD | 155.4% | 68.8% | 2.20 | 3.81 | −27.8% | 3.02 | 7.8% |
| MSFT | −22.3% | 27.2% | **−0.97** | −1.27 | −34.5% | 0.76 | 4.1% |
| *SPY (benchmark)* | *20.6%* | *12.6%* | *1.32* | *1.91* | *−8.9%* | *1.00* | *1.9%* |

*(The live, full table for all 20 names is regenerated daily in
[`data_summary.csv`](data_summary.csv); today's summary is in [`SNAPSHOT.md`](SNAPSHOT.md).)*

**JNJ was the top risk-adjusted performer**: a 2.58 Sharpe with the lowest
volatility of any name (18.7%), the shallowest drawdown (−11%) and the smallest
99% VaR. Its beta to SPY was slightly *negative* (−0.13), so its return came from
something other than market exposure, which makes it a diversifier in this set as
well as a strong performer. This lines up with JNJ's actual 2025
fundamentals: the stock hit an all-time high in Q3 2025 on beat-and-raise earnings
(6% sales growth, 8.1% adjusted EPS growth), consistent with a low-volatility,
steady-compounding profile rather than a data artifact.

**INTC and AMD posted by far the highest raw returns (182% and 155%) but are not
the most efficient names.** On Sharpe they rank third and fourth, essentially tied
with GOOGL, because both carry 69–77% annualized volatility, more than double
GOOGL's. The tail-risk view makes this concrete. INTC's 99% historical VaR is
9.3%: a 1-in-100-day loss four times JNJ's 2.3%. With a beta of 2.6, it also
moved about 2.6× the market on average. High Sharpe with high absolute volatility means the
return happened to be large enough to compensate for outsized risk in this window,
not that the risk was small.

**MSFT was the worst performer on both raw and risk-adjusted terms**
(−22% return, −0.97 Sharpe, −34.5% max drawdown). This was verified against
independent market data: MSFT fell from an all-time high of $555.45 (July 2025)
to ~$385 (July 2026), a real, well-documented drawdown, not a pipeline error.

### Does the VaR model tell the truth?

Out-of-sample backtest from Oct 2024 to Jul 2026: 445 trading days per stock,
20 stocks, 8,900 forecasts per model.

| Model | Confidence | Breaches | Expected | Kupiec rejections (5%) |
|---|---|---|---|---|
| EWMA normal | 95% | 430 | 445 | 1 of 20 |
| EWMA normal | 99% | **173** | 89 | **9 of 20** |
| Historical simulation | 95% | 539 | 445 | 2 of 20 |
| Historical simulation | 99% | 137 | 89 | 3 of 20 |

![VaR backtest](visuals/var_backtest.png)

- **EWMA-normal is well calibrated at 95% and fails at 99%.** At 95% it breached 430
  times against 445 expected. At 99% it breached nearly twice as often as it should,
  and all nine rejections were for *too many* breaches. Daily equity returns have
  fatter tails than a normal distribution, so the error appears where it matters
  most: the far tail.
- **Historical simulation handles the 99% tail better but adapts slowly.** Its
  quantile comes from real returns, so fat tails are built in (3 rejections at
  99%). But it treats all 250 days equally. After the April 2025 sell-off its VaR
  stayed high for a full year and then dropped abruptly as those days left the
  window (the step in the chart above). At 95% it breached 21% more often than
  expected.
- The practical takeaway is the one risk teams act on: no single model wins. A
  filtered-historical or Student-t approach (EWMA scaling plus empirical or
  fat-tailed quantiles) is the natural next step.

Breaches cluster across names on market-wide sell-off days, so the pooled counts
are descriptive. The Kupiec test is applied per stock, where it is valid.

## Testing

[`tests/test_metrics_crosscheck.py`](tests/test_metrics_crosscheck.py) loads
`sql/schema.sql` into a throwaway database, inserts a seeded synthetic price set
(3 stocks + SPY over 400 trading days, with fat-tailed noise so VaR breaches and
anomalies actually occur), runs `sql/metrics.sql`, and recomputes every metric
independently in pandas/NumPy:

| Checked | Reference implementation |
|---|---|
| Daily returns | `price / price.shift(1) - 1` |
| 20-day rolling vol | `rolling(20).std() × √252` |
| Anomaly z-score and flag | mean/std of the **prior** 20 days (`shift(1).rolling(20)`) |
| EWMA volatility | the λ = 0.94 recursion, seeded with the first 20 returns' variance |
| 60-day beta | `rolling(60).cov(SPY) / SPY.rolling(60).var()` |
| Historical VaR / CVaR | `np.percentile` (linear interpolation, matching `PERCENTILE_CONT`) |
| EWMA VaR forecasts, breach flags | `z × σ` from the previous close |
| Kupiec LR | the closed-form formula, from independently counted breaches |

Values are compared with `numpy.isclose`. Doubles use `rtol=1e-9`. Values stored as
`NUMERIC` use half a unit of their stored precision: 5×10⁻⁷ for returns and vol,
5×10⁻⁵ for the z-score. Some tests also assert that the plausible *wrong* answer
does **not** match: the z-score window including today, and `PERCENTILE_DISC`
instead of `PERCENTILE_CONT`. Without that, a pass couldn't tell the two apart.
Edge cases cover the September 2026 NaN bug (a `NaN`, zero or negative close must
violate the `CHECK` constraint) and a Kupiec test with zero breaches, where the
formula contains `0 · ln 0` and must reduce to `−2T·ln(1−p)` rather than NULL.

CI ([`ci.yml`](.github/workflows/ci.yml)) runs the suite on every push and pull
request against a `postgres:16` service container. The daily pipeline runs it
first, so a broken metric can't publish a snapshot.

Run locally:

```bash
pip install -r requirements-dev.txt
export TEST_DATABASE_URL="postgresql://..."   # any Postgres you can write to
pytest
```

The tests create and then drop their own uniquely named Postgres schema, so they
never touch existing tables. With no Postgres at hand, `pip install pgserver` and
leave `TEST_DATABASE_URL` unset: the tests start a temporary local Postgres.

## Setup

```bash
# 1. Create a free Postgres database (e.g. neon.tech), no card required
export DATABASE_URL="postgresql://user:password@host/dbname?sslmode=require"

# 2. Install dependencies
pip install -r requirements.txt

# 3. Create the schema and seed the watchlist (idempotent, also migrates old databases)
psql $DATABASE_URL -v ON_ERROR_STOP=1 -f sql/schema.sql

# 4. Run the ingestion pipeline (pulls 3 years of daily data for 20 stocks + SPY)
python ingest_watchlist_yfinance.py

# 5. Compute all metrics, VaR forecasts and the backtest, then print the report
psql $DATABASE_URL -v ON_ERROR_STOP=1 -f sql/metrics.sql
psql $DATABASE_URL -f sql/risk_metrics_query.sql

# 6. Generate the snapshot, CSVs and charts
python scripts/daily_snapshot.py
python analysis/generate_visuals.py
```

## Connecting Power BI

The reporting layer is a set of plain Postgres views, so Power BI can read them
directly: **Get Data → PostgreSQL database**, then use the host and database from
your `DATABASE_URL` (Neon requires SSL, which Power BI uses by default).

| Object | Grain | Use it for |
|---|---|---|
| `risk_summary` | symbol | KPI cards and ranking tables (Sharpe, Sortino, drawdown, beta) |
| `var_summary` | symbol | Current VaR / CVaR by method and confidence |
| `var_backtest` | symbol × method × confidence | Backtest scorecard, Kupiec result, Basel zone |
| `var_forecasts` | symbol × day × method × confidence | VaR band vs. realized return time series |
| `computed_metrics` | symbol × day | Rolling vol, EWMA vol, rolling beta, anomaly flags |
| `watchlist` | symbol | Dimension table (sector, benchmark flag) |

Values are stored as fractions (0.023 = 2.3%), so apply percentage formatting in
the model rather than in SQL.

## Limitations & next steps

- **Risk-free rate is a fixed 4% placeholder.** A production version would pull the
  live 3-month T-bill rate rather than hardcode it.
- **VaR is per position, not portfolio-level.** The natural extension is a
  portfolio VaR from a weights table, with component/marginal VaR showing which
  names drive the total and how much diversification saves.
- **Both VaR models have the known weaknesses the backtest exposes**: thin normal
  tails (EWMA) and slow adaptation (historical). Filtered historical simulation
  or a Student-t EWMA would address both. The Christoffersen independence test
  would add a check for breach clustering on top of Kupiec's frequency check.
- **Anomaly detection is a simple z-score against a flat 20-day baseline.** Scoring
  returns against the EWMA volatility instead would make the threshold adapt to
  the volatility regime.
- **Fixed watchlist, not dynamically re-screened.** This is intentional for
  reproducibility, but the set reflects one point in time.
- **yfinance is an unofficial, unsupported data source.** In September 2026 it
  returned a `NaN` close that Postgres stored as `NUMERIC 'NaN'`. Because NaN
  propagates through `AVG`/`STDDEV` and sorts above every number, it silently
  blanked every metric and floated an arbitrary ticker to the top of the ranking.
  The ingest now drops such rows, and a `CHECK` constraint backs that up. A
  production version would use a licensed API (e.g. Financial Modeling Prep) for
  reliability guarantees.

## Stack

Python (pandas, yfinance) · PostgreSQL (window functions, ordered-set aggregates,
recursive CTEs, regression aggregates) · GitHub Actions (scheduled pipeline) ·
matplotlib/seaborn (visualization) · Power BI-ready reporting views.
