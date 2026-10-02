"""
Portfolio Risk & Anomaly Monitoring Pipeline — Ingestion Script (yfinance + Postgres)
Pulls daily prices for the watchlist (plus the SPY benchmark) using yfinance
(no API key needed) and loads them into a Postgres database (e.g. a free Neon project).

Each run re-pulls the full lookback window and replaces the stored series per
symbol. Adjusted closes are restated retroactively on every dividend and split,
so appending only the newest days would splice two adjustment bases together and
put a false return on the seam. A full refresh keeps the series internally
consistent, and it also fills any gaps left by bad rows.

Setup:
    pip install -r requirements.txt
    psql $DATABASE_URL -f sql/schema.sql   # creates tables and seeds the watchlist

Usage:
    export DATABASE_URL="postgresql://user:password@host/dbname?sslmode=require"
    python ingest_watchlist_yfinance.py
"""

import os
import time
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta, timezone
import psycopg2
from psycopg2.extras import execute_values
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# 3 years: 1 year for the point-in-time metrics, plus a 250-day estimation window
# and ~1 year of out-of-sample days for the VaR backtest.
LOOKBACK_DAYS = 3 * 365

DATABASE_URL = os.environ["DATABASE_URL"]


def get_connection():
    return psycopg2.connect(DATABASE_URL)


def get_symbols(conn) -> list:
    """The watchlist table (seeded by sql/schema.sql) is the single source of truth."""
    cur = conn.cursor()
    cur.execute("SELECT symbol FROM watchlist ORDER BY is_benchmark, symbol")
    symbols = [row[0] for row in cur.fetchall()]
    cur.close()
    return symbols


def count_stored_rows(conn, symbol: str, from_date: str) -> int:
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM daily_prices WHERE symbol = %s AND trade_date >= %s",
        (symbol, from_date),
    )
    n = cur.fetchone()[0]
    cur.close()
    return n


def fetch_prices(symbol: str, from_date: str, to_date: str, max_retries: int = 2) -> pd.DataFrame:
    """Pull daily adjusted close prices for one symbol via yfinance, with retry."""
    for attempt in range(max_retries + 1):
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(start=from_date, end=to_date, auto_adjust=True)
            if hist.empty:
                return pd.DataFrame()
            df = hist.reset_index()[["Date", "Close", "Volume"]].rename(
                columns={"Date": "trade_date", "Close": "close_price", "Volume": "volume"}
            )
            # yfinance occasionally returns NaN closes (e.g. a not-yet-settled bar).
            # Drop them here; the table also has a CHECK constraint as a backstop.
            df = df.dropna(subset=["close_price"])
            df = df[df["close_price"] > 0]
            df["trade_date"] = df["trade_date"].dt.strftime("%Y-%m-%d")
            df["volume"] = df["volume"].fillna(0).astype("int64")
            df["symbol"] = symbol
            return df[["symbol", "trade_date", "close_price", "volume"]]
        except Exception as e:
            logger.warning(f"{symbol}: attempt {attempt + 1} failed ({e})")
            time.sleep(2 ** attempt)  # exponential backoff
    raise RuntimeError(f"{symbol}: failed after {max_retries + 1} attempts")


def replace_series(conn, symbol: str, df: pd.DataFrame):
    """Atomically swap the stored series for the freshly pulled one."""
    cur = conn.cursor()
    cur.execute("DELETE FROM daily_prices WHERE symbol = %s", (symbol,))
    execute_values(
        cur,
        "INSERT INTO daily_prices (symbol, trade_date, close_price, volume) VALUES %s",
        [(r.symbol, r.trade_date, float(r.close_price), int(r.volume)) for r in df.itertuples()],
    )
    conn.commit()
    cur.close()


def run_pipeline():
    conn = get_connection()
    symbols = get_symbols(conn)
    from_date = (datetime.today() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    # yfinance's `end` is exclusive. Ask for tomorrow to include today's bar, but
    # only once the US close has passed (21:00 UTC covers both EST and EDT);
    # earlier in the day that bar is a partial intraday print, not a close.
    after_close = datetime.now(timezone.utc).hour >= 21
    to_date = (datetime.today() + timedelta(days=1 if after_close else 0)).strftime("%Y-%m-%d")
    succeeded, failed = [], []
    latest_date = None

    for symbol in symbols:
        try:
            df = fetch_prices(symbol, from_date, to_date)
            if df.empty:
                raise RuntimeError("yfinance returned no rows")
            # Don't let a truncated response wipe out good history
            stored = count_stored_rows(conn, symbol, from_date)
            if len(df) < 0.9 * stored:
                raise RuntimeError(f"got {len(df)} rows but {stored} already stored; keeping stored data")
            replace_series(conn, symbol, df)
            succeeded.append(symbol)
            latest_date = max(latest_date or df["trade_date"].max(), df["trade_date"].max())
            logger.info(f"{symbol}: loaded {len(df)} rows ({df['trade_date'].min()} to {df['trade_date'].max()})")
        except Exception as e:
            conn.rollback()
            failed.append((symbol, str(e)))
            logger.error(f"{symbol}: FAILED — {e}")
        time.sleep(0.5)  # be polite, yfinance has no official rate limit but don't hammer it

    cur = conn.cursor()
    cur.execute(
        """INSERT INTO ingestion_log
           (symbols_attempted, symbols_succeeded, symbols_failed, error_detail, latest_trade_date_pulled)
           VALUES (%s, %s, %s, %s, %s)""",
        (len(symbols), len(succeeded), len(failed), str(failed), latest_date),
    )
    conn.commit()
    cur.close()
    conn.close()

    logger.info(f"Pipeline complete: {len(succeeded)} succeeded, {len(failed)} failed")
    if failed:
        logger.warning(f"Failed symbols: {failed}")


if __name__ == "__main__":
    run_pipeline()
