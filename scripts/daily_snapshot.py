"""
Daily Snapshot Generator

Runs after each ingestion + metrics cycle. Does four things that go beyond a
plain "re-pull and re-chart" job:

1. Change detection — identifies stocks that newly entered an anomalous
   state today (flagged today, not flagged yesterday), not just which
   stocks are currently flagged. This is the interesting part: a static
   report tells you *what* is anomalous; this tells you *when it started*.
2. Writes anomaly_history.csv: every flag event over the stored history,
   so the repo shows a real time series of flag events instead of only
   ever showing "right now."
3. Reports today's VaR breaches and the rolling VaR backtest (Kupiec test).
4. Regenerates the charts, writes data_summary.csv / var_backtest.csv, and
   writes a human-readable SNAPSHOT.md summarizing the day.

All metrics are read from the SQL views built by sql/metrics.sql; nothing is
recomputed here except the Kupiec p-value.
"""

import math
import os
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import psycopg2
from datetime import datetime

DATABASE_URL = os.environ["DATABASE_URL"]

BENCHMARK = "SPY"
# Reference categorical palette (slots 1-2) and neutral inks
METHOD_COLORS = {"historical": "#2a78d6", "ewma_normal": "#eb6834"}
METHOD_LABELS = {"historical": "Historical simulation (250d)", "ewma_normal": "EWMA normal (λ=0.94)"}
INK, INK_MUTED, GRID = "#0b0b0b", "#52514e", "#e4e3df"


def get_conn():
    return psycopg2.connect(DATABASE_URL)


def load_latest_two_days_flags(conn):
    """Get anomaly flags for the two most recent trading dates, per symbol."""
    query = """
        WITH ranked AS (
            SELECT cm.symbol, cm.trade_date, cm.is_anomalous,
                   ROW_NUMBER() OVER (PARTITION BY cm.symbol ORDER BY cm.trade_date DESC) AS rn
            FROM computed_metrics cm
            JOIN watchlist w USING (symbol)
            WHERE NOT w.is_benchmark
        )
        SELECT symbol, trade_date, is_anomalous, rn
        FROM ranked
        WHERE rn IN (1, 2)
        ORDER BY symbol, rn;
    """
    return pd.read_sql(query, conn)


def detect_new_anomalies(flags_df):
    """A symbol is 'newly anomalous' if today (rn=1) is flagged and
    yesterday (rn=2) was not."""
    today = flags_df[flags_df["rn"] == 1].set_index("symbol")
    yesterday = flags_df[flags_df["rn"] == 2].set_index("symbol")
    merged = today.join(yesterday, lsuffix="_today", rsuffix="_yesterday", how="left")
    newly_flagged = merged[
        (merged["is_anomalous_today"] == True) &
        (merged["is_anomalous_yesterday"].fillna(False) == False)
    ]
    return newly_flagged.index.tolist(), today["trade_date"].max() if len(today) else None


def load_daily_movers(conn, trade_date):
    """Today's best and worst performing tickers by daily return."""
    query = """
        SELECT dr.symbol, dr.daily_return::float8 AS daily_return
        FROM daily_returns dr
        JOIN watchlist w USING (symbol)
        WHERE dr.trade_date = %s AND NOT w.is_benchmark
        ORDER BY dr.daily_return DESC;
    """
    return pd.read_sql(query, conn, params=(trade_date,))


def load_risk_summary(conn):
    """One row per symbol: performance, risk and VaR over the trailing 252 days."""
    query = """
        SELECT
            rs.symbol,
            rs.is_benchmark,
            ROUND((rs.ann_return * 100)::numeric, 2)         AS annualized_return_pct,
            ROUND((rs.ann_vol * 100)::numeric, 2)            AS annualized_vol_pct,
            ROUND(rs.sharpe::numeric, 2)                     AS sharpe,
            ROUND(rs.sortino::numeric, 2)                    AS sortino,
            ROUND((rs.max_drawdown * 100)::numeric, 1)       AS max_drawdown_pct,
            ROUND(rs.beta::numeric, 2)                       AS beta_vs_spy,
            ROUND((rs.ewma_vol_current * 100)::numeric, 1)   AS ewma_vol_pct,
            ROUND((vs.hist_var_95 * 100)::numeric, 2)        AS hist_var_95_pct,
            ROUND((vs.hist_cvar_95 * 100)::numeric, 2)       AS hist_cvar_95_pct,
            ROUND((vs.hist_var_99 * 100)::numeric, 2)        AS hist_var_99_pct,
            ROUND((vs.hist_cvar_99 * 100)::numeric, 2)       AS hist_cvar_99_pct,
            ROUND((vs.param_var_95 * 100)::numeric, 2)       AS param_var_95_pct,
            ROUND((vs.param_cvar_95 * 100)::numeric, 2)      AS param_cvar_95_pct,
            ROUND((vs.param_var_99 * 100)::numeric, 2)       AS param_var_99_pct,
            ROUND((vs.param_cvar_99 * 100)::numeric, 2)      AS param_cvar_99_pct,
            ROUND((vs.ewma_var_99_next * 100)::numeric, 2)   AS ewma_var_99_next_pct,
            ROUND((rs.pct_anomalous_days * 100)::numeric, 1) AS pct_anomalous,
            rs.window_start,
            rs.window_end
        FROM risk_summary rs
        JOIN var_summary vs USING (symbol)
        ORDER BY rs.is_benchmark, rs.sharpe DESC;
    """
    df = pd.read_sql(query, conn)
    num_cols = df.columns.drop(["symbol", "is_benchmark", "window_start", "window_end"])
    df[num_cols] = df[num_cols].astype(float)
    return df


def kupiec_p_value(lr):
    """Upper tail of chi-squared(1): P(X > lr) = erfc(sqrt(lr / 2))."""
    return math.erfc(math.sqrt(max(lr, 0.0) / 2))


def load_backtest(conn):
    df = pd.read_sql("SELECT * FROM var_backtest ORDER BY confidence, method, symbol;", conn)
    df["confidence"] = df["confidence"].astype(float)
    df["kupiec_p_value"] = df["kupiec_lr"].apply(kupiec_p_value)
    return df


def load_todays_breaches(conn, trade_date):
    query = """
        SELECT symbol, method, var_1d, actual_return
        FROM var_forecasts
        WHERE trade_date = %s AND confidence = 0.99 AND is_exception
        ORDER BY symbol, method;
    """
    return pd.read_sql(query, conn, params=(trade_date,))


def load_forecasts(conn, symbol):
    query = """
        SELECT trade_date, method, var_1d, actual_return, is_exception
        FROM var_forecasts
        WHERE symbol = %s AND confidence = 0.99
        ORDER BY trade_date;
    """
    return pd.read_sql(query, conn, params=(symbol,), parse_dates=["trade_date"])


def regenerate_charts(risk_df):
    os.makedirs("visuals", exist_ok=True)
    risk_df = risk_df[~risk_df["is_benchmark"]]

    fig, ax = plt.subplots(figsize=(11, 8))
    colors = ["#2ca02c" if s > 1.5 else "#1f77b4" if s > 0 else "#d62728" for s in risk_df["sharpe"]]
    ax.scatter(risk_df["annualized_vol_pct"], risk_df["annualized_return_pct"],
               s=risk_df["sharpe"].abs() * 120 + 40, c=colors, alpha=0.7,
               edgecolors="black", linewidth=0.8)
    for _, row in risk_df.iterrows():
        ax.annotate(row["symbol"], (row["annualized_vol_pct"], row["annualized_return_pct"]),
                    xytext=(6, 4), textcoords="offset points", fontsize=9, fontweight="bold")
    ax.axhline(0, color="gray", linewidth=0.8, linestyle="--")
    ax.set_xlabel("Annualized Volatility (%)")
    ax.set_ylabel("Annualized Return (%)")
    ax.set_title(f"Risk-Return Profile — Updated {datetime.today().strftime('%Y-%m-%d')}", fontweight="bold")
    ax.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig("visuals/risk_return_scatter.png", dpi=150)
    plt.close()

    fig, ax = plt.subplots(figsize=(10, 9))
    df_sorted = risk_df.sort_values("sharpe")
    colors_bar = ["#d62728" if s < 0 else "#2ca02c" if s > 1.5 else "#1f77b4" for s in df_sorted["sharpe"]]
    ax.barh(df_sorted["symbol"], df_sorted["sharpe"], color=colors_bar, edgecolor="black", linewidth=0.5)
    ax.axvline(0, color="black", linewidth=1)
    ax.set_xlabel("Sharpe Ratio (rf=4%)")
    ax.set_title(f"Risk-Adjusted Ranking — Updated {datetime.today().strftime('%Y-%m-%d')}", fontweight="bold")
    ax.grid(True, alpha=0.3, axis="x")
    plt.tight_layout()
    plt.savefig("visuals/sharpe_ranking_bar.png", dpi=150)
    plt.close()


def plot_var_backtest(forecasts, backtest, symbol=BENCHMARK):
    """Realized daily returns against each method's 99% VaR forecast; breaches marked."""
    os.makedirs("visuals", exist_ok=True)
    fig, ax = plt.subplots(figsize=(12, 6))
    one = forecasts[forecasts["method"] == "historical"]
    ax.bar(one["trade_date"], one["actual_return"] * 100, width=1.0, color="#b9b8b2", label="Daily return")

    for method, color in METHOD_COLORS.items():
        f = forecasts[forecasts["method"] == method]
        row = backtest[(backtest["symbol"] == symbol) & (backtest["method"] == method)
                       & (backtest["confidence"] == 0.99)].iloc[0]
        label = (f"{METHOD_LABELS[method]}: {row['exceptions']} breaches vs "
                 f"{row['expected_exceptions']:.0f} expected, Kupiec p={row['kupiec_p_value']:.3f}")
        ax.plot(f["trade_date"], -f["var_1d"] * 100, color=color, linewidth=2, label=label)
        hits = f[f["is_exception"]]
        if method == "historical":  # hollow ring, so breaches shared with EWMA stay visible
            ax.scatter(hits["trade_date"], hits["actual_return"] * 100, s=110, facecolors="none",
                       edgecolors=color, linewidths=2, zorder=4)
        else:
            ax.scatter(hits["trade_date"], hits["actual_return"] * 100, s=36, marker="D", color=color,
                       edgecolors="white", linewidths=1, zorder=3)

    ax.axhline(0, color=INK_MUTED, linewidth=0.8)
    ax.set_ylabel("Daily return (%)", color=INK)
    ax.set_title(f"{symbol} — 1-day 99% VaR backtest (out-of-sample)", color=INK,
                 fontweight="bold", loc="left")
    ax.grid(True, axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.tick_params(colors=INK_MUTED)
    ax.legend(loc="upper right", frameon=False, fontsize=9, labelcolor=INK,
              title="Markers = breaches (ring: historical, diamond: EWMA)", title_fontsize=8.5)
    plt.tight_layout()
    plt.savefig("visuals/var_backtest.png", dpi=150)
    plt.close()


def write_anomaly_history(conn):
    """Rebuild the full flag-event history (every false -> true transition) from
    the database. Rebuilding rather than appending means a bad day of data, once
    corrected upstream, can't leave phantom events in the history."""
    query = """
        SELECT trade_date, symbol, 'newly_flagged' AS event
        FROM (
            SELECT cm.symbol, cm.trade_date, cm.is_anomalous,
                   LAG(cm.is_anomalous) OVER (PARTITION BY cm.symbol ORDER BY cm.trade_date) AS prev
            FROM computed_metrics cm
            JOIN watchlist w USING (symbol)
            WHERE NOT w.is_benchmark
        ) x
        WHERE is_anomalous AND NOT COALESCE(prev, FALSE)
        ORDER BY trade_date, symbol;
    """
    pd.read_sql(query, conn).to_csv("anomaly_history.csv", index=False)


def write_snapshot(trade_date, movers_df, newly_flagged, risk_df, backtest, breaches):
    stocks = risk_df[~risk_df["is_benchmark"]]
    top = movers_df.iloc[0]
    bottom = movers_df.iloc[-1]
    top_sharpe = stocks.iloc[0]
    riskiest = stocks.sort_values("hist_var_99_pct", ascending=False).iloc[0]

    lines = [
        f"# Daily Risk Snapshot — {trade_date}",
        "",
        f"**Best mover:** {top['symbol']} ({top['daily_return']*100:+.2f}%)",
        f"**Worst mover:** {bottom['symbol']} ({bottom['daily_return']*100:+.2f}%)",
        f"**Top risk-adjusted performer (trailing 252 days):** {top_sharpe['symbol']} "
        f"(Sharpe {top_sharpe['sharpe']:.2f})",
        f"**Highest tail risk:** {riskiest['symbol']} "
        f"(1-day 99% historical VaR {riskiest['hist_var_99_pct']:.2f}%, "
        f"CVaR {riskiest['hist_cvar_99_pct']:.2f}%)",
        "",
    ]
    if newly_flagged:
        lines.append(f"**⚠ Newly anomalous today:** {', '.join(newly_flagged)} — "
                     f"return moved more than 2 std. deviations from its prior 20-day mean.")
    else:
        lines.append("**No new anomaly flags today.**")

    if len(breaches):
        desc = ", ".join(f"{r.symbol} ({r.method}: {r.actual_return*100:+.2f}% vs VaR {r.var_1d*100:.2f}%)"
                         for r in breaches.itertuples())
        lines.append(f"**99% VaR breaches today:** {desc}")
    else:
        lines.append("**No 99% VaR breaches today.**")

    lines += ["", "## VaR model backtest (99%, all symbols)", "",
              "| Method | Breaches | Expected | Kupiec rejects (5%) | Basel green / yellow / red |",
              "|---|---|---|---|---|"]
    bt = backtest[backtest["confidence"] == 0.99]
    for method, g in bt.groupby("method"):
        zones = g["basel_zone"].value_counts()
        lines.append(f"| {METHOD_LABELS[method]} | {g['exceptions'].sum()} | {g['expected_exceptions'].sum():.0f} "
                     f"| {g['kupiec_reject_5pct'].sum()} of {len(g)} "
                     f"| {zones.get('green', 0)} / {zones.get('yellow', 0)} / {zones.get('red', 0)} |")

    lines.append("")
    lines.append("_Auto-generated by `scripts/daily_snapshot.py` via GitHub Actions._")

    with open("SNAPSHOT.md", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main():
    conn = get_conn()
    flags_df = load_latest_two_days_flags(conn)
    newly_flagged, trade_date = detect_new_anomalies(flags_df)

    if trade_date is None:
        print("No data found — skipping snapshot.")
        return

    movers_df = load_daily_movers(conn, trade_date)
    risk_df = load_risk_summary(conn)
    backtest = load_backtest(conn)
    breaches = load_todays_breaches(conn, trade_date)

    regenerate_charts(risk_df)
    plot_var_backtest(load_forecasts(conn, BENCHMARK), backtest)
    write_anomaly_history(conn)
    write_snapshot(trade_date, movers_df, newly_flagged, risk_df, backtest, breaches)
    risk_df.to_csv("data_summary.csv", index=False)
    backtest.round(4).to_csv("var_backtest.csv", index=False)

    conn.close()
    print(f"Snapshot complete for {trade_date}. Newly flagged: {newly_flagged}")


if __name__ == "__main__":
    main()
