"""
backtest.py — Event-driven backtest engine.

The main loop that connects all components:
    DataHandler → Strategy → Portfolio → ExecutionHandler

Processing order each day:
    1. Emit UniverseUpdateEvent if today is a rebalancing date
    2. Emit MarketEvents for all stocks in current universe
    3. Strategy processes MarketEvents → emits SignalEvents
    4. Portfolio processes SignalEvents → emits OrderEvents
    5. Execution processes OrderEvents → emits FillEvents
    6. Portfolio processes FillEvents → updates positions
    7. Portfolio takes daily snapshot

Key design decisions:
    - Universe update happens BEFORE market events on rebalancing day
      (you know the new universe before trading that day)
    - Orders filled at NEXT day's open (no same-bar execution)
    - Pending orders carried to next day if market closed
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

import pandas as pd
import numpy as np
from queue import Queue, Empty

from events import (MarketEvent, SignalEvent, OrderEvent,
                    FillEvent, UniverseUpdateEvent)
from data import (load_data, build_universe_schedule,
                  generate_universe_update_events)
from strategy import MovingAverageCrossover
from portfolio import Portfolio
from execution import SimulatedExecutionHandler


class Backtest:
    """
    Event-driven backtesting engine.

    Parameters
    ----------
    closes          : DataFrame of daily close prices
    volumes         : DataFrame of daily volumes
    short_window    : MA short window in days
    long_window     : MA long window in days
    top_n           : number of stocks in universe
    rebalance_freq  : '3M' or '6M'
    initial_capital : starting capital in VND
    start_date      : backtest start date
    end_date        : backtest end date
    verbose         : print trade details if True
    """

    def __init__(self, closes, volumes, opens=None,
                 short_window=15, long_window=50,
                 top_n=5, rebalance_freq="6M",
                 initial_capital=1_000_000_000,
                 start_date="2015-01-01",
                 end_date="2024-12-31",
                 verbose=False):

        self.closes         = closes
        self.volumes        = volumes
        self.opens          = opens if opens is not None else closes
        if opens is None:
            print("Warning: no open prices available — using closes as proxy. "
                  "Re-download data to get actual open prices.")
        self.short_window   = short_window
        self.long_window    = long_window
        self.top_n          = top_n
        self.rebalance_freq = rebalance_freq
        self.initial_capital= initial_capital
        self.start_date     = pd.Timestamp(start_date)
        self.end_date       = pd.Timestamp(end_date)
        self.verbose        = verbose

        # Central event queue
        self.events = Queue()

        # Build universe schedule and update events
        self.universe_schedule = build_universe_schedule(
            closes, volumes,
            top_n=top_n,
            rebalance_freq=rebalance_freq
        )
        self.universe_events = generate_universe_update_events(
            self.universe_schedule
        )
        # Index universe events by nearest trading day
        # Rebalancing dates may fall on weekends/holidays
        self.universe_event_map = {}
        for e in self.universe_events:
            # Find the nearest trading day on or after the rebalancing date
            future_days = closes.index[closes.index >= e.timestamp]
            if len(future_days) > 0:
                nearest_trading_day = future_days[0]
                # Update event timestamp to match actual trading day
                e.timestamp = nearest_trading_day
                self.universe_event_map[nearest_trading_day] = e

        # Initialise components
        self.strategy  = MovingAverageCrossover(
            events       = self.events,
            short_window = short_window,
            long_window  = long_window,
        )
        self.portfolio  = Portfolio(
            events          = self.events,
            initial_capital = initial_capital,
            top_n           = top_n,
            closes          = closes,
        )
        self.execution  = SimulatedExecutionHandler(
            events = self.events,
            closes = closes,
            opens  = self.opens,
        )

        # Fills dated T+1 are deferred here and released on that day
        self.deferred_fills = {}

        # Get all trading days in backtest period
        all_dates = closes.index
        self.trading_days = all_dates[
            (all_dates >= self.start_date) &
            (all_dates <= self.end_date)
        ]

    def _process_pending_events(self, current_date):
        while True:
            try:
                event = self.events.get(block=False)
            except Empty:
                break

            if event.event_type == "UNIVERSE_UPDATE":
                self.strategy.update_universe(event)
                self.portfolio.on_universe_update(event)

            elif event.event_type == "MARKET":
                self.strategy.calculate_signals(event)
                self.portfolio.update_market_value(event)

            elif event.event_type == "SIGNAL":
                self.portfolio.on_signal(event)

            elif event.event_type == "ORDER":
                self.execution.execute_order(event)

            elif event.event_type == "FILL":
                exec_date = pd.Timestamp(event.timestamp)
                if exec_date > current_date:
                    # Defer fill to its actual execution day
                    self.deferred_fills.setdefault(exec_date, []).append(event)
                else:
                    self.portfolio.on_fill(event)

    def run(self):
        """
        Main backtest loop — processes one trading day at a time.

        For each day:
        1. Check if universe rebalancing needed → emit UniverseUpdateEvent
        2. Emit MarketEvents for all stocks in current universe
        3. Process all resulting events (signals, orders, fills)
        4. Take portfolio snapshot
        """
        print(f"\n{'='*60}")
        print(f"Starting backtest")
        print(f"  Period    : {self.start_date.date()} to "
              f"{self.end_date.date()}")
        print(f"  Universe  : top {self.top_n}, "
              f"{self.rebalance_freq} rebalancing")
        print(f"  MA params : short={self.short_window}, "
              f"long={self.long_window}")
        print(f"  Capital   : {self.initial_capital:,.0f} VND")
        print(f"{'='*60}\n")

        for date in self.trading_days:

            # ── Step 0: Release fills whose execution date is today ────
            for fill in self.deferred_fills.pop(date, []):
                self.portfolio.on_fill(fill)

            # ── Step 1: Universe rebalancing ──────────────────────────
            if date in self.universe_event_map:
                universe_event = self.universe_event_map[date]
                self.events.put(universe_event)
                self._process_pending_events(date)

            # Skip if no universe yet
            if not self.strategy.universe:
                continue

            # ── Step 2: Emit MarketEvents for current universe ─────────
            for ticker in self.strategy.universe:
                if ticker not in self.closes.columns:
                    continue

                price_row = self.closes[ticker]
                if date not in price_row.index:
                    continue

                close = price_row.loc[date]
                if pd.isna(close):
                    continue

                open_price = (self.opens[ticker].get(date, close)
                              if ticker in self.opens.columns else close)

                market_event = MarketEvent(
                    timestamp = date,
                    symbol    = ticker,
                    open      = open_price,
                    high      = close,   # proxy (no high/low data)
                    low       = close,   # proxy (no high/low data)
                    close     = close,
                    volume    = self.volumes[ticker].get(date, 0)
                               if ticker in self.volumes.columns else 0,
                )
                self.events.put(market_event)

            # ── Step 3: Process all events ─────────────────────────────
            # MarketEvents → Strategy → SignalEvents
            # SignalEvents → Portfolio → OrderEvents
            # OrderEvents → Execution → FillEvents (deferred to T+1)
            self._process_pending_events(date)

            # ── Step 4: Update market values and snapshot ──────────────
            for ticker in self.strategy.universe:
                if ticker not in self.closes.columns:
                    continue
                close = self.closes[ticker].get(date, np.nan)
                if pd.notna(close):
                    # Create a lightweight market event just for valuation
                    val_event = MarketEvent(
                        timestamp=date, symbol=ticker,
                        open=close, high=close, low=close,
                        close=close, volume=0
                    )
                    self.portfolio.update_market_value(val_event)

            self.portfolio.snapshot(date)

        print(f"\nBacktest complete.")
        self.portfolio.print_summary()

        return self.portfolio


if __name__ == "__main__":
    import sys
    sys.path.insert(0, ".")

    # Load data
    print("Loading data...")
    closes, volumes, opens = load_data()

    # ── IS backtest (2015-2020) with best parameters ───────────────────
    print("\nRunning IS backtest (2015-2020)...")
    bt_is = Backtest(
        closes         = closes,
        volumes        = volumes,
        opens          = opens,
        short_window   = 15,
        long_window    = 50,
        top_n          = 5,
        rebalance_freq = "6M",
        initial_capital= 1_000_000_000,
        start_date     = "2015-01-01",
        end_date       = "2020-12-31",
        verbose        = True,
    )
    portfolio_is = bt_is.run()

    # ── OOS backtest (2021-2024) with same parameters ──────────────────
    print("\nRunning OOS backtest (2021-2024)...")
    bt_oos = Backtest(
        closes         = closes,
        volumes        = volumes,
        opens          = opens,
        short_window   = 15,
        long_window    = 50,
        top_n          = 5,
        rebalance_freq = "6M",
        initial_capital= 1_000_000_000,
        start_date     = "2021-01-01",
        end_date       = "2024-12-31",
        verbose        = True,
    )
    portfolio_oos = bt_oos.run()

    # Save results
    os.makedirs("output", exist_ok=True)
    portfolio_is.get_equity_curve().to_csv("output/equity_is.csv")
    portfolio_oos.get_equity_curve().to_csv("output/equity_oos.csv")
    portfolio_is.get_trade_log().to_csv("output/trades_is.csv", index=False)
    portfolio_oos.get_trade_log().to_csv("output/trades_oos.csv", index=False)

    print("\nSaved to output/")