"""
Cross-check: every metric computed in SQL (sql/metrics.sql) is recomputed
independently in pandas/NumPy and compared with numpy.isclose.

No network: the prices are a seeded, deterministic random walk (3 stocks + SPY,
400 trading days, long enough for the 250-day VaR window to start forecasting).

Database: set TEST_DATABASE_URL to any Postgres you can write to (CI uses a
throwaway postgres:16 service container). If it isn't set and `pgserver` is
installed (`pip install pgserver`), a temporary local Postgres is started
instead. Everything is created in a uniquely named Postgres schema that is
dropped afterwards, so the tests never touch existing tables.

Tolerances: values stored as NUMERIC are compared to within half a unit of
their stored precision (returns and vol: 6 dp, z-score: 4 dp). Values stored
as double precision are compared with rtol=1e-9.

Each downstream metric is recomputed from the returns as stored in
daily_returns (rounded to 6 dp), so every check isolates one formula instead
of compounding rounding differences from upstream steps.
"""

import math
import os
import uuid
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg2
import psycopg2.errors
import psycopg2.extensions
import pytest
from psycopg2.extras import execute_values

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_SQL = (ROOT / "sql" / "schema.sql").read_text(encoding="utf-8")
METRICS_SQL = (ROOT / "sql" / "metrics.sql").read_text(encoding="utf-8")

# Seeded watchlist symbols (the FK requires them) with the beta each fake series
# is generated with. Fat-tailed noise guarantees some VaR breaches and anomalies.
FAKE_BETAS = {"AAPL": 1.2, "JNJ": 0.5, "INTC": 2.0}
BENCHMARK = "SPY"
N_DAYS = 400
SEED = 42

LAMBDA = 0.94
Z = {0.95: 1.6448536, 0.99: 2.3263479}

STORED_6DP = 5e-7 + 1e-12   # half a unit in the 6th decimal place
STORED_4DP = 5e-5 + 1e-12
RTOL = 1e-9

# NUMERIC -> float instead of Decimal
DEC2FLOAT = psycopg2.extensions.new_type(
    psycopg2.extensions.DECIMAL.values, "DEC2FLOAT",
    lambda value, cur: float(value) if value is not None else None,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def db_url(tmp_path_factory):
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        yield url
        return
    try:
        import pgserver
    except ImportError:
        pytest.skip("Set TEST_DATABASE_URL, or `pip install pgserver` for a temporary local Postgres")
    server = pgserver.get_server(tmp_path_factory.mktemp("pgdata"), cleanup_mode="stop")
    yield server.get_uri()
    server.cleanup()


def fake_prices() -> pd.DataFrame:
    """Deterministic prices: market random walk + beta-scaled exposure + t-distributed noise."""
    rng = np.random.default_rng(SEED)
    dates = pd.bdate_range("2024-01-02", periods=N_DAYS)
    market = rng.normal(0.0004, 0.01, N_DAYS)
    returns = {BENCHMARK: market}
    for symbol, beta in FAKE_BETAS.items():
        returns[symbol] = beta * market + 0.012 * rng.standard_t(df=4, size=N_DAYS)
    rets = pd.DataFrame(returns, index=dates)
    rets.iloc[0] = 0.0  # first price is the base
    # Stored as NUMERIC(12,4), so round here and the reference sees what SQL sees
    return (100 * (1 + rets).cumprod()).round(4)


@pytest.fixture(scope="module")
def conn(db_url):
    """A fresh Postgres schema with the fake prices loaded and sql/metrics.sql run once."""
    schema = f"crosscheck_{uuid.uuid4().hex[:8]}"
    c = psycopg2.connect(db_url)
    c.autocommit = True  # metrics.sql manages its own BEGIN/COMMIT
    psycopg2.extensions.register_type(DEC2FLOAT, c)
    cur = c.cursor()
    cur.execute(f"CREATE SCHEMA {schema}")
    cur.execute(f"SET search_path TO {schema}")
    try:
        cur.execute(SCHEMA_SQL)
        px = fake_prices()
        rows = [(sym, d.date(), float(p), 1_000_000) for sym in px.columns for d, p in px[sym].items()]
        execute_values(cur, "INSERT INTO daily_prices (symbol, trade_date, close_price, volume) VALUES %s", rows)
        cur.execute(METRICS_SQL)
        yield c
    finally:
        cur.execute(f"DROP SCHEMA {schema} CASCADE")
        c.close()


def query(conn, sql, params=None) -> pd.DataFrame:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=cols)


def series(conn, sql, symbol, value_col) -> pd.Series:
    df = query(conn, sql, (symbol,))
    return pd.Series(df[value_col].astype(float).values, index=pd.to_datetime(df["trade_date"]))


@pytest.fixture(scope="module")
def stored_returns(conn) -> pd.DataFrame:
    """Returns as stored in daily_returns: the input for every downstream reference calc."""
    df = query(conn, "SELECT symbol, trade_date, daily_return FROM daily_returns")
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df.pivot(index="trade_date", columns="symbol", values="daily_return").astype(float).sort_index()


def assert_close(sql: pd.Series, ref: pd.Series, name: str, atol=0.0, rtol=RTOL):
    """Same index, NULLs in the same places, and values within tolerance."""
    sql = sql.sort_index()
    ref = ref.reindex(sql.index)
    assert len(sql) > 0, f"{name}: no rows to compare"
    assert (sql.isna() == ref.isna()).all(), (
        f"{name}: NULL pattern differs on {list(sql.index[sql.isna() != ref.isna()][:5])}"
    )
    mask = sql.notna()
    ok = np.isclose(sql[mask], ref[mask], rtol=rtol, atol=atol)
    worst = (sql[mask] - ref[mask]).abs().max()
    assert ok.all(), f"{name}: {int((~ok).sum())} of {int(mask.sum())} values differ (max abs diff {worst:.3e})"


def ewma_var(r: pd.Series) -> pd.Series:
    """RiskMetrics recursion seeded with the sample variance of the first 20 returns.
    Value at t is the variance as of close t (the forecast for t+1)."""
    v = r.iloc[:20].var(ddof=1)
    out = []
    for x in r:
        v = LAMBDA * v + (1 - LAMBDA) * x * x
        out.append(v)
    return pd.Series(out, index=r.index)


def kupiec_lr(T: int, x: int, p: float) -> float:
    lr = -2 * ((T - x) * math.log(1 - p) + x * math.log(p))
    if x < T:
        lr += 2 * (T - x) * math.log(1 - x / T)
    if x > 0:
        lr += 2 * x * math.log(x / T)
    return lr


ALL_SYMBOLS = [*FAKE_BETAS, BENCHMARK]


# ---------------------------------------------------------------------------
# Cross-checks
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("symbol", ALL_SYMBOLS)
def test_daily_returns(conn, symbol):
    px = fake_prices()[symbol]
    ref = (px / px.shift(1) - 1).dropna()
    sql = series(conn, "SELECT trade_date, daily_return FROM daily_returns WHERE symbol = %s", symbol, "daily_return")
    assert len(sql) == N_DAYS - 1
    assert_close(sql, ref, "daily_return", atol=STORED_6DP, rtol=0)


@pytest.mark.parametrize("symbol", ALL_SYMBOLS)
def test_rolling_vol_20d(conn, stored_returns, symbol):
    r = stored_returns[symbol]
    ref = r.rolling(20).std(ddof=1) * np.sqrt(252)
    sql = series(conn, "SELECT trade_date, rolling_vol_20d FROM computed_metrics WHERE symbol = %s",
                 symbol, "rolling_vol_20d")
    assert_close(sql, ref, "rolling_vol_20d", atol=STORED_6DP, rtol=0)


@pytest.mark.parametrize("symbol", ALL_SYMBOLS)
def test_z_score_uses_prior_20_days(conn, stored_returns, symbol):
    r = stored_returns[symbol]
    prior = r.shift(1).rolling(20)  # the 20 days BEFORE t, excluding t itself
    ref = (r - prior.mean()) / prior.std(ddof=1)
    sql = query(conn, "SELECT trade_date, z_score, is_anomalous FROM computed_metrics WHERE symbol = %s", (symbol,))
    sql.index = pd.to_datetime(sql["trade_date"])
    assert_close(sql["z_score"].astype(float), ref, "z_score", atol=STORED_4DP, rtol=0)

    expected_flag = (ref.abs() > 2).reindex(sql.index).fillna(False)
    assert (sql["is_anomalous"] == expected_flag).all()
    assert sql["is_anomalous"].any(), "fake data should produce at least one anomaly"

    # Sensitivity check: the off-by-one version (window including today) must NOT
    # match, otherwise this test couldn't tell the two apart.
    incl = r.rolling(20)
    off_by_one = ((r - incl.mean()) / incl.std(ddof=1)).reindex(sql.index)
    mask = sql["z_score"].notna()
    assert not np.allclose(sql.loc[mask, "z_score"].astype(float), off_by_one[mask], atol=STORED_4DP)


@pytest.mark.parametrize("symbol", ALL_SYMBOLS)
def test_ewma_vol(conn, stored_returns, symbol):
    ref = np.sqrt(ewma_var(stored_returns[symbol]) * 252)
    sql = series(conn, "SELECT trade_date, ewma_vol FROM computed_metrics WHERE symbol = %s", symbol, "ewma_vol")
    assert_close(sql, ref, "ewma_vol")


@pytest.mark.parametrize("symbol", ALL_SYMBOLS)
def test_rolling_beta_60d(conn, stored_returns, symbol):
    r, m = stored_returns[symbol], stored_returns[BENCHMARK]
    ref = r.rolling(60).cov(m) / m.rolling(60).var()
    sql = series(conn, "SELECT trade_date, rolling_beta_60d FROM computed_metrics WHERE symbol = %s",
                 symbol, "rolling_beta_60d")
    assert_close(sql, ref, "rolling_beta_60d")


def reference_forecasts(r: pd.Series, method: str, confidence: float) -> pd.Series:
    """1-day VaR forecast for each day t >= 251st return, using returns up to t-1 only."""
    values = r.to_numpy()
    out = {}
    if method == "historical":
        for i in range(250, len(values)):
            out[r.index[i]] = -np.percentile(values[i - 250:i], 100 * (1 - confidence))  # linear interpolation
    else:
        v = ewma_var(r)
        for i in range(250, len(values)):
            out[r.index[i]] = Z[confidence] * np.sqrt(v.iloc[i - 1])
    return pd.Series(out)


def sql_forecasts(conn, symbol, method, confidence) -> pd.DataFrame:
    df = query(conn, """
        SELECT trade_date, var_1d, actual_return, is_exception
        FROM var_forecasts
        WHERE symbol = %s AND method = %s AND confidence = %s
        ORDER BY trade_date
    """, (symbol, method, confidence))
    df.index = pd.to_datetime(df["trade_date"])
    return df


@pytest.mark.parametrize("confidence", [0.95, 0.99])
@pytest.mark.parametrize("method", ["historical", "ewma_normal"])
@pytest.mark.parametrize("symbol", ALL_SYMBOLS)
def test_var_forecasts(conn, stored_returns, symbol, method, confidence):
    r = stored_returns[symbol]
    ref = reference_forecasts(r, method, confidence)
    sql = sql_forecasts(conn, symbol, method, confidence)
    assert len(sql) == len(r) - 250 > 0
    assert_close(sql["var_1d"].astype(float), ref, f"{method} VaR {confidence}")
    assert (sql["is_exception"] == (r.reindex(sql.index) < -ref.reindex(sql.index))).all()


@pytest.mark.parametrize("symbol", ALL_SYMBOLS)
def test_historical_var_matches_percentile_cont_not_disc(conn, stored_returns, symbol):
    """percentile_cont == NumPy's default 'linear' method. A discrete quantile
    (percentile_disc / 'inverted_cdf') gives different numbers, so the method matters."""
    r = stored_returns[symbol].to_numpy()
    sql = sql_forecasts(conn, symbol, "historical", 0.99)["var_1d"].astype(float).to_numpy()
    windows = [r[i - 250:i] for i in range(250, len(r))]
    linear = np.array([-np.percentile(w, 1) for w in windows])
    discrete = np.array([-np.percentile(w, 1, method="inverted_cdf") for w in windows])
    assert np.allclose(sql, linear, rtol=RTOL, atol=0)
    assert not np.allclose(sql, discrete, rtol=RTOL, atol=0)


@pytest.mark.parametrize("symbol", ALL_SYMBOLS)
def test_trailing_var_summary(conn, stored_returns, symbol):
    w = stored_returns[symbol].iloc[-252:]
    row = query(conn, "SELECT * FROM var_summary WHERE symbol = %s", (symbol,)).iloc[0]
    for c, q in [(0.95, 5), (0.99, 1)]:
        tag = int(c * 100)
        cut = np.percentile(w, q)
        assert np.isclose(row[f"hist_var_{tag}"], -cut, rtol=RTOL)
        assert np.isclose(row[f"hist_cvar_{tag}"], -w[w <= cut].mean(), rtol=RTOL)


@pytest.mark.parametrize("confidence", [0.95, 0.99])
@pytest.mark.parametrize("method", ["historical", "ewma_normal"])
@pytest.mark.parametrize("symbol", ALL_SYMBOLS)
def test_kupiec_lr(conn, stored_returns, symbol, method, confidence):
    r = stored_returns[symbol]
    ref_var = reference_forecasts(r, method, confidence)
    exceptions = int((r.reindex(ref_var.index) < -ref_var).sum())
    T = len(ref_var)
    row = query(conn, """
        SELECT n_obs, exceptions, kupiec_lr, kupiec_reject_5pct
        FROM var_backtest WHERE symbol = %s AND method = %s AND confidence = %s
    """, (symbol, method, confidence)).iloc[0]
    assert (row["n_obs"], row["exceptions"]) == (T, exceptions)
    expected = kupiec_lr(T, exceptions, 1 - confidence)
    assert np.isclose(row["kupiec_lr"], expected, rtol=RTOL, atol=1e-12)
    assert row["kupiec_reject_5pct"] == (expected > 3.841459)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_price", ["NaN", 0, -1])
def test_invalid_close_rejected(conn, bad_price):
    """Regression test for the September 2026 bug: a NaN close stored as NUMERIC
    'NaN' poisoned every AVG/STDDEV and sorted above every real number."""
    with pytest.raises(psycopg2.errors.CheckViolation):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO daily_prices (symbol, trade_date, close_price, volume) VALUES (%s, %s, %s, %s)",
                ("AAPL", "2030-01-02", bad_price, 0),
            )


def test_kupiec_zero_breaches(conn):
    """With x = 0 the textbook formula contains 0 * ln(0). The SQL must take the
    limit (that term is 0), so LR reduces to -2 * T * ln(1 - p), not NULL or an error."""
    T, p = 300, 0.01
    symbol = "XOM"  # seeded in the watchlist, but has no prices in this test
    try:
        with conn.cursor() as cur:
            execute_values(cur, """
                INSERT INTO var_forecasts (symbol, trade_date, method, confidence, var_1d, actual_return, is_exception)
                VALUES %s
            """, [(symbol, d.date(), "historical", 0.99, 0.05, 0.0, False)
                  for d in pd.bdate_range("2024-01-02", periods=T)])
        row = query(conn, "SELECT * FROM var_backtest WHERE symbol = %s", (symbol,)).iloc[0]
        assert row["exceptions"] == 0
        assert row["kupiec_lr"] is not None and not math.isnan(row["kupiec_lr"])
        assert np.isclose(row["kupiec_lr"], -2 * T * math.log(1 - p), rtol=RTOL)
        assert row["kupiec_reject_5pct"]  # 0 breaches in 300 days at 99% (3 expected): rejected as too conservative
        assert row["basel_zone"] == "green"
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM var_forecasts WHERE symbol = %s", (symbol,))
