# VN30 MA Crossover — Event-Driven Backtester

A production-style event-driven backtesting framework implementing a dual moving average crossover strategy on the Vietnamese VN30 equity index. The primary purpose of this project is to demonstrate **event-driven architecture** — the same design pattern used in live trading systems — applied to a systematic strategy on Vietnamese equities.

---

## Why Event-Driven?

Most academic backtests use **vectorised** computation — loading all historical data into memory and applying signals across the entire array at once. This is fast and convenient for research, but has two fundamental problems:

1. **Lookahead bias risk** — it is easy to accidentally use future data when computing signals on the whole array simultaneously. A one-period shift error can inflate returns dramatically.

2. **Not production-transferable** — live trading systems process one market event at a time. Vectorised research code cannot be deployed directly; it must be rewritten from scratch.

An **event-driven** system processes information exactly as it would arrive in production:

```
Day 500 price arrives → Strategy reacts → Signal generated → Order placed → Fill executed
Day 501 price arrives → ...
```

Day 501 literally does not exist when Day 500 is being processed. Lookahead bias is architecturally impossible.

**When to use each approach:**

| Approach | Use case |
|---|---|
| Vectorised | Signal research, parameter screening, fast iteration |
| Event-driven | Production deployment, strategies with dynamic trading frequency, realistic cost modelling |

A fixed-schedule monthly rebalancing strategy gains little from event-driven architecture. A signal-driven strategy like MA crossover — which may trade 3 times in a week or not at all for 2 months — is the natural use case.

---

## Architecture

```
EventQueue (central message bus)
    ↑                    ↑
DataHandler          Strategy
(MarketEvent)        (SignalEvent)
    ↑                    ↑
ExecutionHandler     Portfolio
(FillEvent)          (OrderEvent)
```

Five event types flow through the system:

| Event | Emitted by | Consumed by |
|---|---|---|
| `MarketEvent` | DataHandler | Strategy, Portfolio |
| `SignalEvent` | Strategy | Portfolio |
| `OrderEvent` | Portfolio | ExecutionHandler |
| `FillEvent` | ExecutionHandler | Portfolio |
| `UniverseUpdateEvent` | DataHandler | Strategy, Portfolio |

Processing order each day:
1. `UniverseUpdateEvent` if today is a rebalancing date
2. `MarketEvents` for all stocks in current universe
3. Strategy processes prices → emits `SignalEvents` on crossover
4. Portfolio sizes positions → emits `OrderEvents`
5. Execution fills at **next day's open** → emits `FillEvents`
6. Portfolio updates holdings and cash
7. Daily snapshot recorded

---

## Strategy

**Signal**: Dual Moving Average crossover
- **LONG** when short MA crosses above long MA
- **FLAT** (exit) when short MA crosses below long MA
- No short selling — restricted in Vietnamese market

**Optimised parameters** (grid search on IS data):
- Short window: **15 days**
- Long window: **50 days**
- IS Sharpe: 0.52, IS Ann. Return: 12.2%

**Parameters tested in grid search:**
- Short windows: [5, 10, 15, 20]
- Long windows: [30, 50, 100, 200]
- 16 combinations evaluated on IS period (2015–2020)

---

## Universe Selection

At each rebalancing date, the top N stocks are selected from the point-in-time VN30 constituents ranked by **average daily trading value** (close price × volume) over the prior 126 trading days (~6 months).

Using trading value as the ranking criterion serves as a natural institutional quality filter. Stocks involved in fraud, regulatory sanctions, or suspension events (e.g. ROS/FLC) collapse in genuine liquidity and are automatically excluded without requiring an explicit blacklist — consistent with how institutional trading desks maintain approved securities lists.

**Configurations tested:**
- Top N: 3, 5, 10
- Rebalancing frequency: 3-month, 6-month
- MA parameters: best IS parameters applied consistently across all configurations

**Universe rules:**
- Stocks require minimum 63 days of history before entering the universe (avoids cold-start signal errors)
- Stocks removed from VN30 at rebalancing trigger forced position closure
- New entrants require a fresh crossover signal before position entry — pre-entry signals are ignored
- Suspended stocks held at last known price until rebalancing date, then closed

---

## Position Sizing

- Capital split equally across top N stocks: `1/N × total portfolio value`
- Target position size recalculated at each rebalancing date based on **current** portfolio value (not fixed initial capital)
- Existing positions are **not force-rebalanced** — they retain their size until the next crossover signal closes and reopens them
- This avoids generating artificial trades purely for size rebalancing, consistent with a signal-driven system

---

## Transaction Costs

Applied on entry (first month) and exit only:

| Component | Rate | Applied on |
|---|---|---|
| Commission | 0.125% | Buy and sell |
| Sales tax | 0.10% | Sell only (SSC mandatory regulation) |
| Slippage | 0.05% | Buy and sell |
| **Total round-trip** | **~0.40%** | Per trade |

Lot size: 100 shares minimum (HOSE requirement) — all orders rounded down to nearest lot.

Execution assumption: orders placed at close of day T are filled at open of day T+1. This eliminates same-bar execution and is the most realistic assumption for a daily strategy.

---

## IS vs OOS Results

**In-Sample (2015–2020) — parameters optimised here:**

| Metric | Value |
|---|---|
| Total Return | 30.22% |
| Ann. Return | 9.32% |
| Ann. Volatility | 9.46% |
| Sharpe Ratio | 0.986 |
| Calmar Ratio | 0.68 |
| Max Drawdown | -13.71% |
| Win Rate | 34.0% |
| Total Trades | 53 |
| Total Costs | 21,875,233 VND (2.19% of capital) |

**Out-of-Sample (2021–2024) — parameters fixed from IS:**

| Metric | Value |
|---|---|
| Total Return | 8.35% |
| Ann. Return | 2.93% |
| Ann. Volatility | 13.42% |
| Sharpe Ratio | 0.218 |
| Calmar Ratio | 0.167 |
| Max Drawdown | -17.60% |
| Win Rate | 38.65% |
| Total Trades | 95 |
| Total Costs | 43,638,486 VND (4.36% of capital) |

**Interpretation:**

OOS Sharpe (0.218) is significantly weaker than IS Sharpe (0.986), indicating mild overfitting to the IS period. However the strategy remains profitable on OOS — total return of 8.35% over 4 years with a Sharpe above zero.

The OOS period was considerably harder than IS: the 2022 Vietnamese market crash (-47% VN-Index) falls entirely in OOS. OOS volatility (13.42%) is also higher than IS (9.46%), suggesting the IS period was unusually calm. This partially explains the Sharpe deterioration — it is not purely a parameter overfitting issue.

Notable: OOS trade count (95) is nearly double IS (53) over a shorter period, suggesting more frequent crossovers during the volatile 2022-2023 regime.

---

## Limitations and Frictions Neglected

**Modelled:**
- Brokerage commission, sales tax, slippage
- Lot size rounding
- Next-day execution (T+1 fill)
- Transaction costs on forced exits at rebalancing

**Not modelled:**
- Market impact for large orders
- Borrowing costs (short selling not used)
- Foreign ownership limits on some VN30 stocks
- T+2 settlement lag
- Dividend adjustments
- Gap risk on suspension events

**Known limitations:**
- IS/OOS split is not regime-neutral — IS captured a bull market, OOS captured a crash and recovery. Walk-forward validation and bootstrap significance testing are planned extensions
- MA crossover is a weak signal on its own — designed here primarily to demonstrate event-driven architecture rather than maximise alpha
- Short sample: ~10 years of daily data is limited for robust signal inference

---

## Project Structure

```
VN30-MA-Crossover/
├── main.py                  <- run full backtest pipeline
├── src/
│   ├── events.py            <- event type definitions
│   ├── data.py              <- vnstock download, universe selection
│   ├── strategy.py          <- MA crossover signal + IS optimisation
│   ├── portfolio.py         <- position management, P&L tracking
│   ├── execution.py         <- order simulation with realistic costs
│   ├── backtest.py          <- main event loop engine
│   └── performance.py       <- metrics, IS/OOS comparison, charts
├── data/
│   └── processed/           <- generated by src/data.py (not tracked)
└── output/
    ├── performance.png
    ├── rolling_sharpe.png
    ├── equity_is.csv
    ├── equity_oos.csv
    ├── trades_is.csv
    ├── trades_oos.csv
    └── metrics.csv
```

---

## How to Run

```bash
# Install dependencies
pip install pandas numpy matplotlib vnstock scipy

# Download daily price data (~10 minutes due to rate limiting)
python src/data.py

# Run IS optimisation + full backtest
python src/backtest.py

# Compute metrics and generate charts
python src/performance.py
```

> **Note:** Price data is not included in this repository. Run `python src/data.py` first.

---

## Planned Extensions

- Walk-forward validation to eliminate period selection bias
- Block bootstrap and trade timing permutation test for statistical significance
- Additional signals: RSI mean reversion, Bollinger Band breakout
- Multi-strategy portfolio combining momentum (VN30-Momentum project) with MA crossover
- Risk metrics integration from VN30-Market-Risk project

---

## Connection to Other Projects

This project is part of a three-project series studying Vietnamese equity markets:

| Project | Frequency | Method | Focus |
|---|---|---|---|
| [VN30-Momentum](https://github.com/quocanh1906/VN30-Momentum) | Monthly | JT overlapping portfolios | Signal research |
| [VN30-MA-Crossover](https://github.com/quocanh1906/VN30-MA-Crossover) | Daily | Event-driven MA crossover | Production architecture |
| [VN30-Market-Risk](https://github.com/quocanh1906/VN30-Market-Risk) | Daily | VaR, CVaR, GARCH | Risk management |

---

## References

- Jegadeesh, N. & Titman, S. (1993). *Returns to Buying Winners and Selling Losers.* Journal of Finance, 48(1), 65–91.
- Chan, E. (2009). *Quantitative Trading.* Wiley.
- State Securities Commission of Vietnam. *Securities Law No. 54/2019/QH14*, effective January 2021.

---

## Author

Vu Quoc Anh Nguyen — MSc Risk Management & Financial Engineering, Imperial College London
GitHub: [quocanh1906](https://github.com/quocanh1906)
