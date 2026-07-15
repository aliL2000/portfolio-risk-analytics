# Portfolio Risk & Anomaly Monitoring Pipeline

An automated pipeline that ingests daily price data for a fixed 20-stock S&P 500
watchlist, computes risk-adjusted return metrics via SQL, and flags statistically
anomalous trading days — built to answer the question a risk desk actually asks:
*"is this asset behaving normally, and how is it compensating for the risk it carries?"*

This is a personal project, not a production system or investment advice.

## Why this project

Most "stock analysis" portfolio projects predict prices — a noisy, overclaimed
problem that doesn't reflect what a fintech risk or analytics team is actually
asked to build. This project instead focuses on the more tractable, more useful
question: given a fixed universe of assets, which ones are compensating investors
for the risk they carry, and which ones are behaving abnormally right now?

## Architecture

```
yfinance API
     │
     ▼
ingest_watchlist_yfinance.py  ──►  Postgres: daily_prices (raw, append-only)
                                          │
                                          ▼
                        sql/schema.sql computes:
                        daily_returns (SQL, from raw prices)
                                          │
                                          ▼
                        computed_metrics (20-day rolling vol,
                        rolling return, z-score anomaly flag —
                        all via SQL window functions)
                                          │
                                          ▼
                        sql/risk_metrics_query.sql
                        (Sharpe, Sortino, annualized return/vol)
                                          │
                                          ▼
                        analysis/generate_visuals.py ──► visuals/*.png
```

## Watchlist

Fixed set of the top 20 S&P 500 constituents by market capitalization (selected
July 2026): NVDA, AAPL, GOOGL, MSFT, AMZN, META, TSLA, AMD, WMT, JPM, V, JNJ,
XOM, INTC, CSCO, ABBV, BAC, COST, UNH, GE.

The list is intentionally fixed rather than re-screened live, so results are
reproducible and comparable over time.

## Methodology

- **Data source:** yfinance (Yahoo Finance), daily adjusted close, 1-year lookback
- **Returns:** simple daily percentage change, computed in SQL via `LAG()`
- **Volatility:** rolling 20-day standard deviation of daily returns, annualized (×√252)
- **Sharpe ratio:** `(annualized return − risk-free rate) / annualized volatility`,
  risk-free rate assumed at 4% (placeholder — see Limitations)
- **Sortino ratio:** same as Sharpe but denominator uses only downside deviation
  (standard deviation of negative-return days)
- **Anomaly flag:** a trading day is flagged when its return is more than 2 standard
  deviations from its own trailing 20-day mean (z-score threshold), computed per-symbol
- **Max drawdown:** largest peak-to-trough decline in cumulative return over the window

All metrics beyond raw ingestion are computed in SQL (window functions), not pandas —
this was a deliberate choice to demonstrate SQL-native analytics rather than pulling
data into Python for every calculation.

## Findings (Jul 2025 – Jul 2026 window)

| Symbol | Ann. Return | Ann. Vol | Sharpe | Sortino | % Anomalous Days |
|---|---|---|---|---|---|
| JNJ | 53.9% | 18.8% | **2.66** | 4.96 | 5.6% |
| INTC | 185.3% | 77.0% | 2.35 | 4.19 | 5.6% |
| GOOGL | 73.4% | 30.0% | 2.31 | 4.54 | 4.8% |
| MSFT | -23.0% | 27.3% | **-0.99** | -1.31 | 6.8% |
| COST | -2.4% | 19.6% | -0.33 | -0.47 | 5.6% |

*(Full 20-ticker table in `data_summary.csv`.)*

**JNJ was the top risk-adjusted performer** in the set — a 2.66 Sharpe with the
lowest volatility of any name (18.8%). This lines up with JNJ's actual 2025
fundamentals: the stock hit an all-time high in Q3 2025 on beat-and-raise earnings
(6% sales growth, 8.1% adjusted EPS growth), consistent with a low-volatility,
steady-compounding profile rather than a data artifact.

**INTC and AMD posted the highest raw Sharpe ratios but are not the most
"efficient" names in the set** — both carry 65-77% annualized volatility, roughly
2x the next-highest name. High Sharpe with high absolute volatility means the
return happened to be large enough to compensate for outsized risk in this
specific window, not that the risk was small.

**MSFT was the only major underperformer, both on raw and risk-adjusted terms**
(-23% return, -0.99 Sharpe) and had the highest anomaly rate (6.8%) in the set.
This was verified against independent market data: MSFT fell from an all-time high
of $555.45 (July 2025) to ~$385 (July 2026), a real, well-documented drawdown, not
a pipeline error.

## Setup

```bash
# 1. Create a free Postgres database (e.g. neon.tech), no card required
# 2. Copy .env.example to .env and fill in your DATABASE_URL
cp .env.example .env

# 3. Install dependencies
pip install -r requirements.txt

# 4. Create the schema and seed the watchlist
psql $DATABASE_URL -f sql/schema.sql

# 5. Run the ingestion pipeline (pulls 1yr of daily data for all 20 tickers)
export DATABASE_URL="postgresql://..."
python ingest_watchlist_yfinance.py

# 6. Compute risk metrics
psql $DATABASE_URL -f sql/risk_metrics_query.sql

# 7. Generate visuals (requires raw daily data already loaded)
python analysis/generate_visuals.py
```

## Limitations & next steps

- **Risk-free rate is a fixed 4% placeholder.** A production version would pull the
  live 3-month T-bill rate rather than hardcode it.
- **No dividend reinvestment modeling** — returns are price-only, not total return.
  This understates returns for high-dividend names (JNJ, XOM) relative to reality.
- **Anomaly detection is a simple z-score, not a volatility-regime-aware model.**
  A GARCH or EWMA-based approach would adapt the threshold to changing market
  conditions rather than using a flat trailing-20-day window throughout.
- **Fixed watchlist, not dynamically re-screened.** A stock that fell out of the
  top 20 by market cap mid-window is still tracked; this is intentional for
  reproducibility but means the universe drifts from "current top 20" over time.
- **yfinance is an unofficial, unsupported data source.** A production version
  would use a licensed API (e.g. Financial Modeling Prep) for reliability guarantees.

## Stack

Python (pandas, yfinance) · PostgreSQL (window functions, incremental loads) ·
matplotlib/seaborn (visualization) · designed for Power BI integration via the
`risk_metrics_query.sql` output.
