"""
performance.py — Performance analysis for the event-driven backtester.

Computes:
- Standard metrics: annualised return, volatility, Sharpe, max drawdown
- IS vs OOS comparison
- Trade statistics: win rate, avg win/loss, profit factor
- Rolling Sharpe
- Equity curve and drawdown charts
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from scipy import stats


def compute_metrics(equity_curve, name="Strategy",
                    rf=0.0, freq=252):
    """
    Compute standard performance metrics from equity curve.

    Parameters
    ----------
    equity_curve : Series of portfolio values (not returns)
    name         : strategy label
    rf           : annual risk-free rate
    freq         : periods per year (252 for daily)
    """
    returns = equity_curve.pct_change().dropna()
    r       = returns

    ann_return  = r.mean() * freq
    ann_vol     = r.std() * np.sqrt(freq)
    sharpe      = (ann_return - rf) / ann_vol if ann_vol > 0 else np.nan

    cum         = equity_curve / equity_curve.iloc[0]
    rolling_max = cum.cummax()
    drawdown    = (cum - rolling_max) / rolling_max
    max_dd      = drawdown.min()

    # Calmar ratio
    calmar = ann_return / abs(max_dd) if max_dd != 0 else np.nan

    # Win rate
    win_rate    = (r > 0).mean()
    best_day    = r.max()
    worst_day   = r.min()

    # Total return
    total_ret = (equity_curve.iloc[-1] / equity_curve.iloc[0]) - 1

    metrics = {
        "Total Return (%)"   : round(total_ret * 100, 2),
        "Ann. Return (%)"    : round(ann_return * 100, 2),
        "Ann. Volatility (%)": round(ann_vol * 100, 2),
        "Sharpe Ratio"       : round(sharpe, 3),
        "Calmar Ratio"       : round(calmar, 3),
        "Max Drawdown (%)"   : round(max_dd * 100, 2),
        "Win Rate (%)"       : round(win_rate * 100, 2),
        "Best Day (%)"       : round(best_day * 100, 2),
        "Worst Day (%)"      : round(worst_day * 100, 2),
        "N Days"             : len(r),
    }

    return metrics, drawdown


def compute_trade_stats(trade_log):
    """
    Compute trade-level statistics from trade log.

    Parameters
    ----------
    trade_log : DataFrame from portfolio.get_trade_log()
    """
    if trade_log.empty:
        return {}

    buys  = trade_log[trade_log["direction"] == "BUY"]
    sells = trade_log[trade_log["direction"] == "SELL"]

    total_commission = trade_log["commission"].sum()
    total_tax        = trade_log["sales_tax"].sum()
    total_slippage   = trade_log["slippage"].sum()
    total_cost       = trade_log["total_cost"].sum()

    return {
        "Total Trades"       : len(trade_log),
        "Buy Orders"         : len(buys),
        "Sell Orders"        : len(sells),
        "Total Commission"   : round(total_commission, 0),
        "Total Sales Tax"    : round(total_tax, 0),
        "Total Slippage"     : round(total_slippage, 0),
        "Total Costs (VND)"  : round(total_cost, 0),
        "Cost as % of Init"  : round(total_cost / 1_000_000_000 * 100, 3),
    }


def print_metrics(metrics, name="Strategy"):
    """Print formatted performance metrics."""
    print(f"\n{'='*50}")
    print(f"  {name}")
    print(f"{'='*50}")
    for k, v in metrics.items():
        print(f"  {k:<25} {v:>15}")
    print(f"{'='*50}")


def print_comparison(is_metrics, oos_metrics):
    """Print IS vs OOS comparison table."""
    print(f"\n{'='*65}")
    print(f"  IS vs OOS Comparison")
    print(f"{'='*65}")
    print(f"  {'Metric':<25} {'IS (2015-2020)':>18} {'OOS (2021-2024)':>18}")
    print(f"  {'─'*60}")
    for key in is_metrics:
        iv = is_metrics[key]
        ov = oos_metrics.get(key, "N/A")
        print(f"  {key:<25} {str(iv):>18} {str(ov):>18}")
    print(f"{'='*65}")


def plot_results(is_equity, oos_equity,
                 is_drawdown, oos_drawdown,
                 short_window, long_window,
                 top_n, rebalance_freq):
    """
    Plot equity curves and drawdowns for IS and OOS periods.
    """
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    fig.suptitle(
        f"VN30 MA Crossover — MA({short_window},{long_window}) "
        f"Top {top_n} | {rebalance_freq} Rebalancing",
        fontsize=13, fontweight="bold"
    )

    # Normalise equity curves to 1.0
    is_norm  = is_equity  / is_equity.iloc[0]
    oos_norm = oos_equity / oos_equity.iloc[0]

    # ── IS equity curve ─────────────────────────────────────────────
    ax = axes[0][0]
    ax.plot(is_norm.index, is_norm.values,
            color="steelblue", linewidth=1.5)
    ax.axhline(y=1, color="black", linestyle=":", linewidth=0.8)
    ax.set_title(f"IS Equity Curve (2015-2020)")
    ax.set_ylabel("Normalised Value")
    ax.grid(alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))

    # ── OOS equity curve ────────────────────────────────────────────
    ax = axes[0][1]
    ax.plot(oos_norm.index, oos_norm.values,
            color="darkorange", linewidth=1.5)
    ax.axhline(y=1, color="black", linestyle=":", linewidth=0.8)
    ax.set_title(f"OOS Equity Curve (2021-2024)")
    ax.set_ylabel("Normalised Value")
    ax.grid(alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))

    # ── IS drawdown ─────────────────────────────────────────────────
    ax = axes[1][0]
    ax.fill_between(is_drawdown.index, is_drawdown.values * 100, 0,
                    color="steelblue", alpha=0.5)
    ax.set_title("IS Drawdown")
    ax.set_ylabel("Drawdown (%)")
    ax.grid(alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))

    # ── OOS drawdown ────────────────────────────────────────────────
    ax = axes[1][1]
    ax.fill_between(oos_drawdown.index, oos_drawdown.values * 100, 0,
                    color="darkorange", alpha=0.5)
    ax.set_title("OOS Drawdown")
    ax.set_ylabel("Drawdown (%)")
    ax.grid(alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))

    plt.tight_layout()
    os.makedirs("output", exist_ok=True)
    plt.savefig("output/performance.png", dpi=150, bbox_inches="tight")
    plt.show()
    print("Saved to output/performance.png")


def plot_rolling_sharpe(is_equity, oos_equity, window=63):
    """
    Plot rolling Sharpe ratio for IS and OOS periods.
    window=63 ≈ 3 months of trading days.
    """
    is_ret  = is_equity.pct_change().dropna()
    oos_ret = oos_equity.pct_change().dropna()

    def rolling_sharpe(returns, w):
        rm = returns.rolling(w).mean() * 252
        rs = returns.rolling(w).std() * np.sqrt(252)
        return rm / rs

    is_rs  = rolling_sharpe(is_ret, window)
    oos_rs = rolling_sharpe(oos_ret, window)

    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    fig.suptitle("Rolling 63-Day Sharpe Ratio",
                 fontsize=13, fontweight="bold")

    for ax, rs, label, color in [
        (axes[0], is_rs,  "IS (2015-2020)",  "steelblue"),
        (axes[1], oos_rs, "OOS (2021-2024)", "darkorange"),
    ]:
        ax.plot(rs.index, rs.values, color=color, linewidth=1.2)
        ax.axhline(y=0, color="black", linewidth=0.8)
        ax.axhline(y=1, color="green", linestyle=":", linewidth=1.0,
                   alpha=0.7, label="Sharpe=1")
        ax.axhline(y=-1, color="red", linestyle=":", linewidth=1.0,
                   alpha=0.7)
        pct_pos = (rs.dropna() >= 0).mean() * 100
        ax.set_title(f"{label} — {pct_pos:.0f}% days positive Sharpe")
        ax.set_ylabel("Rolling Sharpe")
        ax.grid(alpha=0.3)
        ax.legend(fontsize=8)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))

    plt.tight_layout()
    plt.savefig("output/rolling_sharpe.png", dpi=150, bbox_inches="tight")
    plt.show()
    print("Saved to output/rolling_sharpe.png")


if __name__ == "__main__":
    import sys
    sys.path.insert(0, ".")

    # Load equity curves from backtest output
    is_equity  = pd.read_csv("output/equity_is.csv",
                              index_col=0, parse_dates=True).squeeze()
    oos_equity = pd.read_csv("output/equity_oos.csv",
                              index_col=0, parse_dates=True).squeeze()
    is_trades  = pd.read_csv("output/trades_is.csv")
    oos_trades = pd.read_csv("output/trades_oos.csv")

    # Compute metrics
    is_metrics,  is_dd  = compute_metrics(is_equity,  "IS  (2015-2020)")
    oos_metrics, oos_dd = compute_metrics(oos_equity, "OOS (2021-2024)")

    # Print results
    print_metrics(is_metrics,  "IS  (2015-2020)")
    print_metrics(oos_metrics, "OOS (2021-2024)")
    print_comparison(is_metrics, oos_metrics)

    # Trade stats
    print("\nIS Trade Statistics:")
    for k, v in compute_trade_stats(is_trades).items():
        print(f"  {k:<25} {v}")

    print("\nOOS Trade Statistics:")
    for k, v in compute_trade_stats(oos_trades).items():
        print(f"  {k:<25} {v}")

    # Plots
    plot_results(
        is_equity, oos_equity,
        is_dd, oos_dd,
        short_window=15, long_window=50,
        top_n=5, rebalance_freq="6M"
    )
    plot_rolling_sharpe(is_equity, oos_equity)

    # Save metrics
    pd.DataFrame([is_metrics, oos_metrics],
                  index=["IS", "OOS"]).to_csv("output/metrics.csv")
    print("\nSaved to output/metrics.csv")