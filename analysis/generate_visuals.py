"""
Generates the two visuals that require raw daily price/return series
(correlation heatmap, cumulative returns) by pulling directly from Postgres.

Usage:
    export DATABASE_URL="postgresql://..."
    pip install psycopg2-binary pandas matplotlib seaborn
    python generate_visuals.py
"""

import os
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
import psycopg2

DATABASE_URL = os.environ["DATABASE_URL"]


def load_returns():
    conn = psycopg2.connect(DATABASE_URL)
    query = "SELECT symbol, trade_date, daily_return FROM daily_returns ORDER BY trade_date;"
    df = pd.read_sql(query, conn)
    conn.close()
    return df


def plot_correlation_heatmap(df):
    pivot = df.pivot(index="trade_date", columns="symbol", values="daily_return")
    corr = pivot.corr()

    fig, ax = plt.subplots(figsize=(12, 10))
    sns.heatmap(corr, annot=True, fmt=".2f", cmap="RdYlGn", center=0,
                square=True, linewidths=0.5, ax=ax, vmin=-1, vmax=1)
    ax.set_title("Daily Return Correlation Matrix — 20-Stock Watchlist", fontsize=13, fontweight="bold")
    plt.tight_layout()
    plt.savefig("visuals/correlation_heatmap.png", dpi=150)
    plt.close()
    print("Saved visuals/correlation_heatmap.png")


def plot_cumulative_returns(df):
    pivot = df.pivot(index="trade_date", columns="symbol", values="daily_return")
    cumulative = (1 + pivot).cumprod() - 1

    fig, ax = plt.subplots(figsize=(14, 8))
    # Highlight the top and bottom performer, gray out the rest to avoid clutter
    top = cumulative.iloc[-1].idxmax()
    bottom = cumulative.iloc[-1].idxmin()
    for col in cumulative.columns:
        if col == top:
            ax.plot(cumulative.index, cumulative[col] * 100, label=f"{col} (best)", color="#2ca02c", linewidth=2.5)
        elif col == bottom:
            ax.plot(cumulative.index, cumulative[col] * 100, label=f"{col} (worst)", color="#d62728", linewidth=2.5)
        else:
            ax.plot(cumulative.index, cumulative[col] * 100, color="gray", alpha=0.25, linewidth=0.8)
    ax.axhline(0, color="black", linewidth=0.8, linestyle="--")
    ax.set_xlabel("Date")
    ax.set_ylabel("Cumulative Return (%)")
    ax.set_title("Cumulative Returns — 20-Stock Watchlist (Jul 2025–Jul 2026)", fontsize=13, fontweight="bold")
    ax.legend(loc="upper left")
    ax.grid(True, alpha=0.3)
    plt.xticks(rotation=45)
    plt.tight_layout()
    plt.savefig("visuals/cumulative_returns.png", dpi=150)
    plt.close()
    print("Saved visuals/cumulative_returns.png")


if __name__ == "__main__":
    os.makedirs("visuals", exist_ok=True)
    returns_df = load_returns()
    plot_correlation_heatmap(returns_df)
    plot_cumulative_returns(returns_df)
