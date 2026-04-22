"""
events.py — Event type definitions for the event-driven backtester.

Four event types flow through the system in sequence:
    MarketEvent → SignalEvent → OrderEvent → FillEvent

This file has no dependencies and is imported by all other modules.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class MarketEvent:
    """
    Triggered when new market data is available.
    Emitted by DataHandler once per bar (day).

    Attributes
    ----------
    timestamp : date of this bar
    symbol    : stock ticker e.g. 'VCB'
    open      : opening price
    high      : daily high
    low       : daily low
    close     : closing price
    volume    : trading volume
    """
    timestamp : datetime
    symbol    : str
    open      : float
    high      : float
    low       : float
    close     : float
    volume    : float
    event_type: str = field(default="MARKET", init=False)


@dataclass
class SignalEvent:
    """
    Triggered when strategy generates a trading signal.
    Emitted by Strategy, consumed by Portfolio.

    Attributes
    ----------
    timestamp   : date of signal
    symbol      : stock ticker
    signal_type : 'LONG', 'SHORT', or 'FLAT' (exit)
    strength    : signal strength 0-1 (default 1.0 for MA crossover)
    """
    timestamp  : datetime
    symbol     : str
    signal_type: str   # 'LONG', 'SHORT', 'FLAT'
    strength   : float = 1.0
    event_type : str   = field(default="SIGNAL", init=False)


@dataclass
class OrderEvent:
    """
    Triggered when portfolio decides to place an order.
    Emitted by Portfolio, consumed by ExecutionHandler.

    Attributes
    ----------
    timestamp  : date of order
    symbol     : stock ticker
    order_type : 'MKT' (market) only for now
    quantity   : number of shares (positive = buy, negative = sell)
    direction  : 'BUY' or 'SELL'
    """
    timestamp : datetime
    symbol    : str
    order_type: str    # 'MKT'
    quantity  : int
    direction : str    # 'BUY' or 'SELL'
    event_type: str = field(default="ORDER", init=False)


@dataclass
class FillEvent:
    """
    Triggered when an order is filled by the broker.
    Emitted by ExecutionHandler, consumed by Portfolio.

    Attributes
    ----------
    timestamp     : date of fill
    symbol        : stock ticker
    quantity      : shares filled (positive = bought, negative = sold)
    direction     : 'BUY' or 'SELL'
    fill_price    : actual execution price
    commission    : brokerage commission paid
    sales_tax     : 0.1% sales tax on sell orders (Vietnamese regulation)
    slippage      : price impact estimate
    """
    timestamp  : datetime
    symbol     : str
    quantity   : int
    direction  : str
    fill_price : float
    commission : float
    sales_tax  : float
    slippage   : float
    event_type : str = field(default="FILL", init=False)

    @property
    def total_cost(self):
        """Total transaction cost including all components."""
        return self.commission + self.sales_tax + self.slippage

    @property
    def net_value(self):
        """Net cash flow from this fill (negative = cash out for buy)."""
        gross = self.fill_price * self.quantity
        if self.direction == "BUY":
            return -(gross + self.total_cost)
        else:
            return gross - self.total_cost
@dataclass
class UniverseUpdateEvent:
    """
    VN30 universe rebalancing — stocks added and removed from watchlist.
    New stocks require a fresh signal before entry.
    Removed stocks trigger forced position closure.
    """
    timestamp     : datetime
    stocks_added  : list
    stocks_removed: list
    new_universe  : list
    event_type    : str = field(default="UNIVERSE_UPDATE", init=False)