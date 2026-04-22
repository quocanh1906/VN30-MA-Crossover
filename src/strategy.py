"""
strategy.py — MA crossover signal generation.

Receives MarketEvents one day at a time, maintains a rolling
price buffer for each stock, and emits SignalEvents when a
crossover is detected.

Key design decisions:
- Signal only generated if stock is in current watchlist
- Requires full long_window of history before any signal
- Crossover detected by sign change in (short_ma - long_ma)
- No signal generated on the day a stock enters the watchlist
  (requires at least one full day of monitoring first)
- IS/OOS split supported via date parameter
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

import pandas as pd
import numpy as np
from collections import deque
from events import MarketEvent, SignalEvent, UniverseUpdateEvent


class MovingAverageCrossover:
    """
    Dual moving average crossover strategy.

    Generates:
    - LONG signal when short MA crosses ABOVE long MA
    - FLAT signal when short MA crosses BELOW long MA
      (we go flat rather than short — no short selling in Vietnam)

    Parameters
    ----------
    events         : Queue to put SignalEvents into
    short_window   : short MA period in days
    long_window    : long MA period in days
    """

    def __init__(self, events, short_window=10, long_window=50):
        self.events       = events
        self.short_window = short_window
        self.long_window  = long_window

        # Price buffer — rolling window per stock
        # deque with maxlen automatically drops old prices
        self.price_buffer = {}

        # Current universe — only generate signals for these stocks
        self.universe = []

        # Track previous MA difference to detect crossovers
        # +1 = short above long, -1 = short below long, 0 = not enough data
        self.prev_signal = {}

        # Track which stocks just entered watchlist this period
        # No signal on entry day — need at least one prior observation
        self.new_entries = set()

    def update_universe(self, event: UniverseUpdateEvent):
        """
        Handle universe rebalancing.
        - Add new stocks to monitoring (with fresh buffer)
        - Remove exited stocks from monitoring
        - Mark new entries so we don't signal on day 1
        """
        # Add new stocks — fresh price buffer, no history carried over
        for ticker in event.stocks_added:
            self.price_buffer[ticker] = deque(maxlen=self.long_window + 1)
            self.prev_signal[ticker]  = 0
            self.new_entries.add(ticker)

        # Remove exited stocks
        for ticker in event.stocks_removed:
            if ticker in self.price_buffer:
                del self.price_buffer[ticker]
            if ticker in self.prev_signal:
                del self.prev_signal[ticker]
            self.new_entries.discard(ticker)

        self.universe = event.new_universe
        print(f"  Strategy universe updated: {self.universe}")

    def calculate_signals(self, event: MarketEvent):
        """
        Process a new price bar and generate signal if crossover detected.

        Called once per stock per day by the backtest engine.
        """
        ticker = event.symbol

        # Only process stocks in current universe
        if ticker not in self.universe:
            return

        # Initialise buffer if needed
        if ticker not in self.price_buffer:
            self.price_buffer[ticker] = deque(maxlen=self.long_window + 1)
            self.prev_signal[ticker]  = 0

        # Add new price to buffer
        self.price_buffer[ticker].append(event.close)

        # Need full long_window of history before generating signal
        if len(self.price_buffer[ticker]) < self.long_window:
            return

        # Remove from new entries after first full day of data
        self.new_entries.discard(ticker)

        # Compute MAs
        prices      = list(self.price_buffer[ticker])
        short_ma    = np.mean(prices[-self.short_window:])
        long_ma     = np.mean(prices[-self.long_window:])

        # Current state: +1 if short above long, -1 if short below
        curr_signal = 1 if short_ma > long_ma else -1

        # Detect crossover — sign change from previous bar
        prev = self.prev_signal.get(ticker, 0)

        if prev != 0 and curr_signal != prev:
            # Crossover detected
            if curr_signal == 1:
                # Short MA crossed ABOVE long MA → go long
                signal_type = "LONG"
            else:
                # Short MA crossed BELOW long MA → go flat (exit)
                signal_type = "FLAT"

            self.events.put(SignalEvent(
                timestamp   = event.timestamp,
                symbol      = ticker,
                signal_type = signal_type,
                strength    = 1.0,
            ))

            print(f"  [{event.timestamp.date()}] {ticker}: "
                  f"{signal_type} signal "
                  f"(short_ma={short_ma:.2f}, long_ma={long_ma:.2f})")

        # Update previous signal state
        self.prev_signal[ticker] = curr_signal


def optimise_parameters(closes, universe_schedule,
                        short_windows, long_windows,
                        is_end_date, top_n=5):
    """
    Grid search MA parameters on in-sample data.

    Tests all combinations of short and long windows and returns
    the combination with the best Sharpe ratio on IS data.

    Parameters
    ----------
    closes          : DataFrame of daily close prices
    universe_schedule: dict of {date: [tickers]} from build_universe_schedule
    short_windows   : list of short MA periods to test e.g. [5, 10, 15, 20]
    long_windows    : list of long MA periods to test e.g. [30, 50, 100, 200]
    is_end_date     : pd.Timestamp, end of in-sample period
    top_n           : number of stocks in universe

    Returns
    -------
    dict with best parameters and IS performance metrics
    """
    from queue import Queue
    import itertools

    results = []
    combos  = [(s, l) for s, l in itertools.product(short_windows, long_windows)
               if s < l]  # short must be less than long

    print(f"\nOptimising MA parameters on IS data "
          f"(up to {is_end_date.date()})...")
    print(f"Testing {len(combos)} combinations...\n")

    for short_w, long_w in combos:
        # Run simplified vectorised backtest for speed during optimisation
        # (event-driven is used for final OOS backtest)
        daily_returns = []

        is_closes = closes.loc[:is_end_date]

        for date in is_closes.index:
            # Get active universe at this date
            active_universe = []
            for reb_date in sorted(universe_schedule.keys()):
                if reb_date <= date:
                    active_universe = universe_schedule[reb_date]

            if not active_universe:
                continue

            day_returns = []
            for ticker in active_universe:
                if ticker not in is_closes.columns:
                    continue

                prices = is_closes[ticker].loc[:date].dropna()

                if len(prices) < long_w:
                    continue

                short_ma = prices.iloc[-short_w:].mean()
                long_ma  = prices.iloc[-long_w:].mean()

                # Previous day MAs
                if len(prices) < long_w + 1:
                    continue

                prev_short = prices.iloc[-short_w-1:-1].mean()
                prev_long  = prices.iloc[-long_w-1:-1].mean()

                # Check if in position
                in_long = prev_short > prev_long

                if in_long and ticker in is_closes.columns:
                    next_idx = is_closes.index.get_loc(date)
                    if next_idx + 1 < len(is_closes):
                        next_date = is_closes.index[next_idx + 1]
                        ret = (is_closes[ticker].get(next_date, np.nan) /
                               is_closes[ticker].get(date, np.nan) - 1)
                        if pd.notna(ret):
                            day_returns.append(ret)

            if day_returns:
                daily_returns.append(np.mean(day_returns))

        if len(daily_returns) < 60:
            continue

        r       = pd.Series(daily_returns).dropna()
        ann_ret = r.mean() * 252
        ann_vol = r.std() * np.sqrt(252)
        sharpe  = ann_ret / ann_vol if ann_vol > 0 else -999

        results.append({
            "short_window": short_w,
            "long_window" : long_w,
            "ann_return"  : round(ann_ret * 100, 2),
            "ann_vol"     : round(ann_vol * 100, 2),
            "sharpe"      : round(sharpe, 3),
            "n_days"      : len(r),
        })

        print(f"  short={short_w:3d} long={long_w:3d} → "
              f"Sharpe={sharpe:.3f}  Ann.Ret={ann_ret*100:.1f}%")

    if not results:
        print("No valid results — check data")
        return None

    # Sort by Sharpe
    results_df = pd.DataFrame(results).sort_values("sharpe", ascending=False)

    best = results_df.iloc[0]
    print(f"\nBest IS parameters:")
    print(f"  Short window : {int(best['short_window'])} days")
    print(f"  Long window  : {int(best['long_window'])} days")
    print(f"  IS Sharpe    : {best['sharpe']}")
    print(f"  IS Ann.Return: {best['ann_return']}%")

    return {
        "short_window": int(best["short_window"]),
        "long_window" : int(best["long_window"]),
        "is_sharpe"   : best["sharpe"],
        "is_return"   : best["ann_return"],
        "all_results" : results_df,
    }


if __name__ == "__main__":
    import sys
    sys.path.insert(0, ".")
    from data import load_data, build_universe_schedule

    closes, volumes = load_data()

    # Build universe schedule
    schedule = build_universe_schedule(
        closes, volumes, top_n=5, rebalance_freq="6M"
    )

    # IS period: 2015-2020, OOS: 2021-2024
    IS_END = pd.Timestamp("2020-12-31")

    # Grid search
    best_params = optimise_parameters(
        closes          = closes,
        universe_schedule = schedule,
        short_windows   = [5, 10, 15, 20],
        long_windows    = [30, 50, 100, 200],
        is_end_date     = IS_END,
        top_n           = 5,
    )