"""
portfolio.py — Portfolio manager for the event-driven backtester.

Responsibilities:
- Receive SignalEvents → size positions → emit OrderEvents
- Receive FillEvents → update cash and holdings
- Receive UniverseUpdateEvents → force close removed stocks
- Track daily P&L, positions, and cash throughout backtest

Position sizing:
- Capital split equally across top_n stocks (equal allocation)
- Each stock gets 1/top_n of initial capital maximum
- If stock not yet signalled, its allocation sits in cash

Vietnamese market costs applied in ExecutionHandler:
- Commission: 0.125% one-way (realistic institutional)
- Sales tax : 0.10% on sell side only (SSC regulation)
- Slippage  : 0.05% estimate
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

import pandas as pd
import numpy as np
from events import SignalEvent, OrderEvent, FillEvent, UniverseUpdateEvent


class Portfolio:
    """
    Manages positions, cash, and P&L for the backtest.

    Parameters
    ----------
    events        : Queue for emitting OrderEvents
    initial_capital: starting cash in VND (default 1,000,000,000 = 1B VND)
    top_n         : number of stocks in universe (for position sizing)
    """

    def __init__(self, events, initial_capital=1_000_000_000, 
                top_n=5, closes=None):
        self.events           = events
        self.initial_capital  = initial_capital
        self.top_n            = top_n
        self.closes           = closes if closes is not None else pd.DataFrame()

        # Current state
        self.cash                = initial_capital
        self.holdings            = {}   # {ticker: shares}
        self.position_value      = {}   # {ticker: market value}
        self.avg_cost            = {}   # {ticker: average cost per share}
        self.target_position_size = initial_capital / top_n  # updated at each rebalancing

        # Current universe and signals
        self.universe        = []
        self.current_signals = {}  # {ticker: 'LONG' or 'FLAT'}

        # Historical records
        self.all_holdings    = []  # daily snapshots
        self.trade_log       = []  # record of every trade

    @property
    def total_value(self):
        """Total portfolio value = cash + all position values."""
        pos_value = sum(self.position_value.values())
        return self.cash + pos_value



    def update_market_value(self, event):
        """
        Update market value of a position using latest price.
        Called on every MarketEvent for stocks we hold.
        """
        ticker = event.symbol
        if ticker in self.holdings and self.holdings[ticker] > 0:
            self.position_value[ticker] = (
                self.holdings[ticker] * event.close
            )

    def on_signal(self, event: SignalEvent):
        """
        Receive signal from strategy → decide whether to place order.

        LONG signal:
        - If not already long → place BUY order for position_size worth of shares
        - If already long → do nothing

        FLAT signal:
        - If currently long → place SELL order to close position
        - If already flat → do nothing
        """
        ticker = event.symbol
        signal = event.signal_type

        self.current_signals[ticker] = signal

        current_shares = self.holdings.get(ticker, 0)

        if signal == "LONG" and current_shares == 0:
            # Enter long position
            self._place_order(
                timestamp = event.timestamp,
                ticker    = ticker,
                direction = "BUY",
            )

        elif signal == "FLAT" and current_shares > 0:
            # Exit long position
            self._place_order(
                timestamp = event.timestamp,
                ticker    = ticker,
                direction = "SELL",
                quantity  = current_shares,
            )

    def on_universe_update(self, event: UniverseUpdateEvent):
        """
        Handle universe rebalancing.
        Force close positions in stocks being removed.
        Update target position size based on current portfolio value.
        Existing positions are NOT rebalanced — they retain their size
        until the next crossover signal closes and reopens them.
        """
        # Force close removed stocks first
        for ticker in event.stocks_removed:
            shares = self.holdings.get(ticker, 0)
            if shares > 0:
                print(f"  Forced close: {ticker} "
                    f"({shares} shares) — removed from universe")
                self._place_order(
                    timestamp = event.timestamp,
                    ticker    = ticker,
                    direction = "SELL",
                    quantity  = shares,
                    forced    = True,
                )
            self.current_signals.pop(ticker, None)

        # Recalculate target position size from current total value
        # Used for all future new BUY entries — existing positions untouched
        self.target_position_size = self.total_value / self.top_n
        print(f"  Target position size updated: "
            f"{self.target_position_size:,.0f} VND per stock "
            f"(total: {self.total_value:,.0f} VND)")

        self.universe = event.new_universe

    def _place_order(self, timestamp, ticker, direction,
                     quantity=None, forced=False):
        """
        Calculate order quantity and emit OrderEvent.

        For BUY: quantity = position_size / current_price (estimated)
        For SELL: quantity = current holdings
        """
        if direction == "BUY":
            # Get latest available close price for this ticker
            if ticker in self.closes.columns:
                prices = self.closes[ticker].dropna()
                available = prices[prices.index <= pd.Timestamp(timestamp)]
                if len(available) == 0:
                    print(f"  Warning: no price data for {ticker} — skipping")
                    return
                est_price = available.iloc[-1]
            elif ticker in self.position_value and self.position_value[ticker] > 0:
                est_price = (self.position_value[ticker] /
                            max(self.holdings.get(ticker, 1), 1))
            else:
                print(f"  Warning: cannot estimate price for {ticker} BUY")
                return

            shares = int(self.target_position_size / est_price)
            if shares <= 0:
                return

            # Check we have enough cash
            est_cost = shares * est_price * 1.002  # rough cost estimate
            if est_cost > self.cash:
                shares = int(self.cash / (est_price * 1.002))
                if shares <= 0:
                    print(f"  Insufficient cash for {ticker} BUY")
                    return

        else:  # SELL
            shares = quantity or self.holdings.get(ticker, 0)
            if shares <= 0:
                return

        self.events.put(OrderEvent(
            timestamp  = timestamp,
            symbol     = ticker,
            order_type = "MKT",
            quantity   = shares,
            direction  = direction,
        ))

    def on_fill(self, event: FillEvent):
        """
        Update portfolio state after order execution.
        """
        ticker    = event.symbol
        shares    = event.quantity
        direction = event.direction
        price     = event.fill_price

        if direction == "BUY":
            # Add shares, deduct cash
            self.holdings[ticker]      = self.holdings.get(ticker, 0) + shares
            self.position_value[ticker] = self.holdings[ticker] * price
            self.avg_cost[ticker]      = price  # simplified (no averaging)
            self.cash                  += event.net_value  # negative for buy

        else:  # SELL
            # Remove shares, add cash
            self.holdings[ticker]       = max(
                self.holdings.get(ticker, 0) - shares, 0
            )
            if self.holdings[ticker] == 0:
                self.position_value.pop(ticker, None)
                self.avg_cost.pop(ticker, None)
            else:
                self.position_value[ticker] = self.holdings[ticker] * price
            self.cash += event.net_value  # positive for sell

        # Log the trade
        self.trade_log.append({
            "date"       : event.timestamp,
            "ticker"     : ticker,
            "direction"  : direction,
            "shares"     : shares,
            "price"      : price,
            "commission" : event.commission,
            "sales_tax"  : event.sales_tax,
            "slippage"   : event.slippage,
            "total_cost" : event.total_cost,
            "cash_after" : self.cash,
        })

    def snapshot(self, date):
        """
        Record daily portfolio snapshot for performance calculation.
        """
        snapshot = {
            "date"       : date,
            "cash"       : self.cash,
            "total_value": self.total_value,
            "n_positions": sum(1 for s in self.holdings.values() if s > 0),
        }
        for ticker in self.universe:
            snapshot[f"shares_{ticker}"] = self.holdings.get(ticker, 0)
            snapshot[f"value_{ticker}"]  = self.position_value.get(ticker, 0)

        self.all_holdings.append(snapshot)

    def get_equity_curve(self):
        """Return daily total portfolio value as a Series."""
        df = pd.DataFrame(self.all_holdings).set_index("date")
        return df["total_value"]

    def get_returns(self):
        """Return daily portfolio returns."""
        equity = self.get_equity_curve()
        return equity.pct_change().dropna()

    def get_trade_log(self):
        """Return trade log as DataFrame."""
        return pd.DataFrame(self.trade_log)

    def print_summary(self):
        """Print current portfolio state."""
        print(f"\n{'='*50}")
        print(f"  Portfolio Summary")
        print(f"{'='*50}")
        print(f"  Cash          : {self.cash:>15,.0f} VND")
        print(f"  Positions     : {sum(1 for s in self.holdings.values() if s > 0)}")
        for ticker, shares in self.holdings.items():
            if shares > 0:
                val = self.position_value.get(ticker, 0)
                print(f"    {ticker}: {shares:>8,} shares  "
                      f"Value: {val:>12,.0f} VND")
        print(f"  Total Value   : {self.total_value:>15,.0f} VND")
        print(f"  P&L           : {self.total_value - self.initial_capital:>+15,.0f} VND")
        print(f"{'='*50}")