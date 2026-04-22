# Code Documentation

Technical reference for the VN30 MA Crossover event-driven backtester.
This document describes every module, class, and function in the `src/` folder.

---

## Module Overview

```
src/
├── events.py      # Event dataclasses — the messaging vocabulary
├── data.py        # Data download, universe selection, event generation
├── strategy.py    # MA crossover signal computation and IS optimisation
├── portfolio.py   # Position management, cash tracking, P&L
├── execution.py   # Order simulation with realistic market frictions
├── backtest.py    # Main event loop connecting all components
└── performance.py # Metrics, statistical tests, visualisation
```

---

## `events.py`

Defines the five dataclasses that flow through the event queue.
No logic — pure data containers. Imported by every other module.

### `MarketEvent`
Emitted by `DataHandler` once per stock per trading day.
Contains OHLCV data for one stock on one date.

```python
MarketEvent(timestamp, symbol, open, high, low, close, volume)
```

| Field | Type | Description |
|---|---|---|
| timestamp | datetime | Trading date |
| symbol | str | Stock ticker e.g. 'VCB' |
| open/high/low/close | float | Daily prices in VND |
| volume | float | Shares traded |

---

### `SignalEvent`
Emitted by `Strategy` when a crossover is detected.
Consumed by `Portfolio` to decide whether to place an order.

```python
SignalEvent(timestamp, symbol, signal_type, strength=1.0)
```

| Field | Type | Description |
|---|---|---|
| signal_type | str | 'LONG', 'FLAT' |
| strength | float | Signal confidence 0–1 (always 1.0 for MA crossover) |

---

### `OrderEvent`
Emitted by `Portfolio` after receiving a signal.
Consumed by `ExecutionHandler` to simulate the fill.

```python
OrderEvent(timestamp, symbol, order_type, quantity, direction)
```

| Field | Type | Description |
|---|---|---|
| order_type | str | 'MKT' (market order only) |
| quantity | int | Number of shares |
| direction | str | 'BUY' or 'SELL' |

---

### `FillEvent`
Emitted by `ExecutionHandler` after simulating order execution.
Consumed by `Portfolio` to update holdings and cash.

```python
FillEvent(timestamp, symbol, quantity, direction,
          fill_price, commission, sales_tax, slippage)
```

| Field | Type | Description |
|---|---|---|
| fill_price | float | Actual execution price (next day open + slippage) |
| commission | float | Brokerage fee in VND |
| sales_tax | float | 0.1% SSC tax on sell orders only |
| slippage | float | Market impact estimate |

**Properties:**
- `total_cost` — sum of commission + sales_tax + slippage
- `net_value` — net cash flow (negative for buy, positive for sell)

---

### `UniverseUpdateEvent`
Emitted at each rebalancing date (January/July for 6M, quarterly for 3M).
Consumed by both `Strategy` (resets price buffers) and `Portfolio` (force closes removed stocks).

```python
UniverseUpdateEvent(timestamp, stocks_added, stocks_removed, new_universe)
```

| Field | Type | Description |
|---|---|---|
| stocks_added | list | Tickers entering watchlist |
| stocks_removed | list | Tickers leaving watchlist |
| new_universe | list | Complete new watchlist |

---

## `data.py`

Handles all data acquisition and universe construction.

### Constants

**`VN30_CONSTITUENTS`**
Dict mapping period keys (`"2018-01"`, `"2018-07"` etc.) to lists of 30 official VN30 tickers.
Sourced from HOSE official announcements. 14 periods covering 2018-01 through 2024-07.

**`VN30_MASTER`**
Sorted list of all unique tickers ever in VN30. Auto-computed from `VN30_CONSTITUENTS`.
Used as the download universe.

**`REBALANCE_DATES_6M`**
Dict mapping period keys to exact `pd.Timestamp` rebalancing dates from official HOSE announcements.
Jan/Jul dates only.

**`REBALANCE_DATES_3M`**
Dict mapping period keys to `pd.Timestamp` rebalancing dates.
Jan/Jul use exact HOSE dates. Apr/Oct approximated as 1st of month.

---

### Functions

**`get_vn30_constituents(date)`**
Returns the official VN30 constituent list active on a given date.
Falls back to most recent available period if exact period not found.

```python
constituents = get_vn30_constituents("2021-06-15")
# Returns 2021-01 list (most recent before June)
```

---

**`download_single(symbol, start, end, retries, delay)`**
Downloads daily OHLCV data for one ticker from vnstock (KBS source).
Returns DataFrame with columns: open, high, low, close, volume.
Includes retry logic with configurable delay between attempts.
Deduplicates any duplicate dates (vnstock occasionally returns duplicates).

---

**`download_all(symbols, start, end, delay, batch_pause, batch_size)`**
Downloads all tickers in `VN30_MASTER` with rate limiting.
Rate limit: 3 seconds between requests, 60 second pause every 15 requests.
Returns two DataFrames: closes (date × ticker) and volumes (date × ticker).
Prints progress and reports failed tickers.

---

**`get_top_n_universe(closes, volumes, rebalance_date, top_n, lookback_days, min_history)`**
Selects top N stocks from VN30 by average daily trading value at a rebalancing date.

Trading value = close price × volume (VND per day), averaged over `lookback_days`.
Only considers stocks in the official VN30 constituent list at that date.
Excludes stocks with fewer than `min_history` days of data (avoids cold-start bias).
Uses only data strictly before `rebalance_date` — no lookahead.

```python
top5 = get_top_n_universe(closes, volumes, 
                           pd.Timestamp("2021-01-22"), 
                           top_n=5)
# Returns ['HPG', 'STB', 'VNM', 'TCB', 'VHM'] (example)
```

---

**`build_universe_schedule(closes, volumes, top_n, rebalance_freq)`**
Builds the complete watchlist schedule across all rebalancing dates.
Returns dict of `{pd.Timestamp: [list of tickers]}`.
Supports `rebalance_freq='3M'` or `'6M'`.

---

**`generate_universe_update_events(schedule)`**
Converts the universe schedule into a list of `UniverseUpdateEvent` objects.
Detects which stocks were added and removed at each rebalancing by diffing consecutive periods.
First event has `stocks_removed=[]` since there is no previous universe.

---

**`save_data(closes, volumes, path)`**
Saves closes and volumes DataFrames to CSV in `data/processed/`.

---

**`load_data(path)`**
Loads saved closes and volumes from CSV.
Called at the start of backtest to avoid re-downloading.

---

## `strategy.py`

### Class: `MovingAverageCrossover`

Stateful signal generator. Maintains a rolling price buffer for each stock
and detects crossovers by tracking the sign of (short_ma - long_ma).

**`__init__(events, short_window, long_window)`**
Initialises empty price buffers and signal state.

| Attribute | Description |
|---|---|
| `price_buffer` | Dict of `{ticker: deque(maxlen=long_window+1)}` |
| `prev_signal` | Dict of `{ticker: +1 or -1}` — previous MA relationship |
| `universe` | List of currently watched tickers |
| `new_entries` | Set of tickers that just entered — suppresses day-1 signals |

---

**`update_universe(event: UniverseUpdateEvent)`**
Called when universe rebalancing occurs.
- Adds new stocks with fresh empty price buffers (no historical prices carried over)
- Removes exited stocks and clears their state
- Marks new entrants in `new_entries` so no signal fires on entry day

---

**`calculate_signals(event: MarketEvent)`**
Core signal logic. Called once per stock per day.

1. Appends new close price to that stock's buffer
2. Returns immediately if fewer than `long_window` prices available
3. Computes short MA and long MA from buffer
4. Detects crossover by comparing `curr_signal` vs `prev_signal`
5. If crossover detected → puts `SignalEvent` on queue
6. Updates `prev_signal` for next day

Crossover types:
- `prev=-1, curr=+1` → short crossed above long → `LONG` signal
- `prev=+1, curr=-1` → short crossed below long → `FLAT` signal

---

### Functions

**`optimise_parameters(closes, universe_schedule, short_windows, long_windows, is_end_date, top_n)`**
Grid search over all (short, long) window combinations on IS data.
Uses a simplified vectorised approximation (not full event-driven) for speed.
For each combination: computes daily returns when in-position, calculates annualised Sharpe.
Returns dict with best parameters and full results table sorted by Sharpe.

```python
best = optimise_parameters(
    closes, schedule,
    short_windows=[5, 10, 15, 20],
    long_windows=[30, 50, 100, 200],
    is_end_date=pd.Timestamp("2020-12-31")
)
# Returns {'short_window': 15, 'long_window': 50, 'is_sharpe': 0.52, ...}
```

---

## `portfolio.py`

### Class: `Portfolio`

Manages cash, holdings, and position sizing throughout the backtest.
Responds to four event types: `SignalEvent`, `UniverseUpdateEvent`, `FillEvent`, and `MarketEvent` (for valuation).

**`__init__(events, initial_capital, top_n, closes)`**

| Attribute | Description |
|---|---|
| `cash` | Current cash balance in VND |
| `holdings` | Dict `{ticker: shares}` |
| `position_value` | Dict `{ticker: market value in VND}` |
| `avg_cost` | Dict `{ticker: average cost per share}` |
| `target_position_size` | Current target allocation per stock (updated at rebalancing) |
| `current_signals` | Dict `{ticker: 'LONG' or 'FLAT'}` |
| `all_holdings` | List of daily snapshot dicts |
| `trade_log` | List of fill records |

---

**`total_value` (property)**
Returns total portfolio value = cash + sum of all position market values.

---

**`update_market_value(event: MarketEvent)`**
Updates `position_value[ticker]` using latest close price.
Called every day for all held positions to keep valuations current.

---

**`on_signal(event: SignalEvent)`**
Routes signal to order placement logic.
- `LONG` signal + no current position → calls `_place_order('BUY')`
- `FLAT` signal + current position exists → calls `_place_order('SELL')`
- All other cases → no action (already in correct state)

---

**`on_universe_update(event: UniverseUpdateEvent)`**
Handles rebalancing:
1. Force closes any open positions in `stocks_removed` via `_place_order('SELL')`
2. Clears signal state for removed stocks
3. Recalculates `target_position_size = total_value / top_n`
   (used for all future BUY orders — existing positions untouched)
4. Updates `self.universe`

Position sizing design decision: existing positions are not force-rebalanced.
They retain their size until the next crossover signal closes and reopens them.
This avoids generating artificial calendar-driven trades.

---

**`_place_order(timestamp, ticker, direction, quantity, forced)`**
Internal method that creates and queues an `OrderEvent`.

For BUY orders:
- Looks up latest available close price from `self.closes`
- Calculates shares = `target_position_size / est_price`
- Checks sufficient cash — reduces quantity if needed

For SELL orders:
- Uses current `self.holdings[ticker]` as quantity

---

**`on_fill(event: FillEvent)`**
Updates portfolio state after execution:
- BUY: adds shares to holdings, deducts net cash (price + costs)
- SELL: removes shares, adds net cash (price - costs)
- Appends full fill record to `trade_log`

---

**`snapshot(date)`**
Records daily portfolio state to `all_holdings`.
Captures: cash, total value, position count, shares and value per stock.

---

**`get_equity_curve()`**
Returns `pd.Series` of daily total portfolio value.

---

**`get_returns()`**
Returns daily percentage returns from equity curve.

---

**`get_trade_log()`**
Returns trade log as `pd.DataFrame`.

---

**`print_summary()`**
Prints current cash, all open positions with share counts and values, total value, and P&L.

---

## `execution.py`

### Constants

| Constant | Value | Description |
|---|---|---|
| `COMMISSION_RATE` | 0.00125 | 0.125% one-way brokerage (institutional VN30 rate) |
| `SALES_TAX_RATE` | 0.001 | 0.10% on sell side only (SSC mandatory) |
| `SLIPPAGE_RATE` | 0.0005 | 0.05% market impact estimate |
| `LOT_SIZE` | 100 | HOSE minimum lot (100 shares) |

### Class: `SimulatedExecutionHandler`

**`__init__(events, closes, opens)`**
Stores reference to price data for execution price lookup.
If no `opens` DataFrame provided, uses `closes` as proxy.

---

**`execute_order(event: OrderEvent)`**
Simulates realistic order execution:

1. Finds next available trading day after signal date
2. Gets open price on that day (T+1 execution assumption)
3. Applies directional slippage (buy higher, sell lower)
4. Computes commission, sales tax (sell only), slippage in VND
5. Rounds quantity to nearest lot via `_round_to_lot()`
6. Emits `FillEvent` with all cost components

Handles edge cases:
- Ticker not in price data → skips with warning
- No future price data (delisted stock) → skips with warning
- Invalid price (NaN or zero) → skips with warning
- Quantity rounds to 0 lots → skips with warning

---

**`_round_to_lot(quantity)`**
Rounds quantity down to nearest multiple of `LOT_SIZE` (100).
HOSE requires all trades in multiples of 100 shares.

```python
_round_to_lot(1547) → 1500
_round_to_lot(99)   → 0  # order skipped
```

---

## `backtest.py`

### Class: `Backtest`

The main engine connecting all components through the event queue.

**`__init__(closes, volumes, short_window, long_window, top_n, rebalance_freq, initial_capital, start_date, end_date, verbose)`**

Initialises all components and pre-computes:
- Universe schedule (which stocks are watched at each rebalancing date)
- Universe update events indexed by nearest trading day
- List of all trading days in backtest period

Nearest trading day mapping ensures rebalancing dates that fall on weekends or holidays are correctly shifted to the next actual trading day.

---

**`_process_pending_events()`**
Drains the event queue until empty, routing each event to the correct handler:

```
UNIVERSE_UPDATE → strategy.update_universe() + portfolio.on_universe_update()
MARKET          → strategy.calculate_signals() + portfolio.update_market_value()
SIGNAL          → portfolio.on_signal()
ORDER           → execution.execute_order()
FILL            → portfolio.on_fill()
```

Called after emitting each batch of events to ensure events are processed
in the correct causal order before the next day begins.

---

**`run()`**
Main backtest loop iterating over every trading day:

```
for date in trading_days:
    1. If rebalancing date → emit UniverseUpdateEvent → process
    2. Skip if no universe established yet
    3. For each stock in universe → emit MarketEvent
    4. Process all events (signals → orders → fills)
    5. Update market values for valuation
    6. Take daily portfolio snapshot
```

Returns the `Portfolio` object with complete history.

---

## `performance.py`

### Functions

**`compute_metrics(equity_curve, name, rf, freq)`**
Computes standard performance statistics from a portfolio equity curve.

Returns dict of metrics and a drawdown series.

| Metric | Formula |
|---|---|
| Total Return | `(final / initial) - 1` |
| Ann. Return | `mean(daily_returns) × 252` |
| Ann. Volatility | `std(daily_returns) × √252` |
| Sharpe Ratio | `ann_return / ann_vol` |
| Calmar Ratio | `ann_return / abs(max_drawdown)` |
| Max Drawdown | `min((cum - cum_max) / cum_max)` |
| Win Rate | `fraction of days with positive return` |

---

**`compute_trade_stats(trade_log)`**
Computes trade-level statistics from the trade log DataFrame.
Returns counts of buy/sell orders and total costs broken down by component (commission, tax, slippage).

---

**`print_metrics(metrics, name)`**
Prints formatted performance table for one strategy.

---

**`print_comparison(is_metrics, oos_metrics)`**
Prints side-by-side IS vs OOS comparison table.

---

**`plot_results(is_equity, oos_equity, is_drawdown, oos_drawdown, short_window, long_window, top_n, rebalance_freq)`**
Produces a 2×2 chart:
- Top left: IS equity curve (normalised to 1.0)
- Top right: OOS equity curve (normalised to 1.0)
- Bottom left: IS drawdown
- Bottom right: OOS drawdown

Saved to `output/performance.png`.

---

**`plot_rolling_sharpe(is_equity, oos_equity, window)`**
Plots rolling Sharpe ratio for IS and OOS periods.
Default window: 63 days (~3 months of trading days).
Shows percentage of days with positive rolling Sharpe in chart title.
Saved to `output/rolling_sharpe.png`.

---

## Data Flow Summary

```
src/data.py
    download_all() → closes_daily.csv, volumes_daily.csv
    build_universe_schedule() → {date: [tickers]}
    generate_universe_update_events() → [UniverseUpdateEvent, ...]

src/strategy.py
    optimise_parameters() → best (short_window, long_window) on IS data

src/backtest.py → Backtest.run()
    Day loop:
        UniverseUpdateEvent → strategy.update_universe()
                            → portfolio.on_universe_update()
        MarketEvent         → strategy.calculate_signals()
                            → portfolio.update_market_value()
        SignalEvent         → portfolio.on_signal()
        OrderEvent          → execution.execute_order()
        FillEvent           → portfolio.on_fill()
        portfolio.snapshot()

    Returns Portfolio with:
        get_equity_curve() → daily total value Series
        get_trade_log()    → DataFrame of all fills

src/performance.py
    compute_metrics()     → performance statistics
    compute_trade_stats() → cost breakdown
    plot_results()        → equity curve + drawdown charts
    plot_rolling_sharpe() → rolling Sharpe chart
```

---

## Key Design Decisions

**T+1 execution**
Orders generated at close of day T are filled at open of day T+1.
Eliminates same-bar execution and reflects realistic market access.

**Fresh buffer on universe entry**
When a stock enters the watchlist, its price history buffer starts empty.
Pre-entry prices are not used — the strategy has no knowledge of price action before it started watching the stock.

**No signal on entry day**
New entrants are tracked in `new_entries` and suppressed for the first day.
Prevents immediately acting on a crossover that happened before monitoring began.

**Conservative lot rounding**
All quantities rounded DOWN to nearest 100 shares.
Ensures we never exceed available cash due to rounding.

**Position size recalculated at rebalancing, not continuously**
`target_position_size` updates at each universe rebalancing date.
Existing positions are not adjusted — only new entries use the updated size.
Avoids calendar-driven trades that would inflate transaction costs.
