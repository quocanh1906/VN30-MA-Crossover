"""
execution.py — Order execution simulation for the event-driven backtester.

Receives OrderEvents and simulates realistic order fills including:
- Execution at next day's open price (realistic — you can't trade
  at the same bar's close that generated the signal)
- Vietnamese market transaction costs:
    Commission : 0.125% one-way (institutional rate, VN30 stocks)
    Sales tax  : 0.10% on sell side only (SSC regulation, mandatory)
    Slippage   : 0.05% estimate (market impact for VN30 liquid stocks)
- Lot size rounding (Vietnam trades in lots of 100 shares on HOSE)

Key assumption:
    Orders placed at close of day T are filled at open of day T+1.
    This is the most realistic assumption for a daily strategy and
    eliminates any possibility of lookahead bias in execution.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

import pandas as pd
import numpy as np
from events import OrderEvent, FillEvent


# ── Vietnamese market transaction costs ────────────────────────────────────────
# Source: SSC regulations + broker rate cards (2024)
COMMISSION_RATE = 0.00125   # 0.125% one-way — institutional VN30 rate
SALES_TAX_RATE  = 0.001     # 0.10% on sell side only — SSC mandatory
SLIPPAGE_RATE   = 0.0005    # 0.05% — market impact estimate for VN30 stocks
LOT_SIZE        = 100       # HOSE minimum lot size


class SimulatedExecutionHandler:
    """
    Simulates order execution with realistic Vietnamese market frictions.

    Parameters
    ----------
    events  : Queue for emitting FillEvents
    closes  : DataFrame of daily close prices (for price lookup)
    opens   : DataFrame of daily open prices (execution at next open)
    """

    def __init__(self, events, closes, opens=None):
        self.events = events
        self.closes = closes
        # If no open prices available, use close as approximation
        self.opens  = opens if opens is not None else closes

    def execute_order(self, event: OrderEvent):
        """
        Simulate order fill.

        Execution price = next trading day's open price.
        If next day has no data (holiday, suspension), skip the fill
        and log a warning — position remains unchanged.

        Parameters
        ----------
        event : OrderEvent to execute
        """
        ticker    = event.symbol
        direction = event.direction
        quantity  = event.quantity

        # Find execution date — next trading day after signal
        signal_date = pd.Timestamp(event.timestamp)

        # Get all available dates for this ticker after signal date
        if ticker not in self.opens.columns:
            print(f"  ⚠ {ticker}: not in price data — order skipped")
            return

        ticker_opens = self.opens[ticker].dropna()
        future_dates = ticker_opens.index[ticker_opens.index > signal_date]

        if len(future_dates) == 0:
            print(f"  ⚠ {ticker}: no future price data after "
                  f"{signal_date.date()} — order skipped (delisted?)")
            return

        exec_date  = future_dates[0]
        exec_price = ticker_opens.loc[exec_date]

        if pd.isna(exec_price) or exec_price <= 0:
            print(f"  ⚠ {ticker}: invalid price on {exec_date.date()} "
                  f"— order skipped")
            return

        # Round quantity to nearest lot (100 shares on HOSE)
        quantity = self._round_to_lot(quantity)
        if quantity <= 0:
            print(f"  ⚠ {ticker}: quantity rounds to 0 lots — order skipped")
            return

        # Apply slippage via price impact — buy higher, sell lower
        if direction == "BUY":
            fill_price = exec_price * (1 + SLIPPAGE_RATE)
        else:
            fill_price = exec_price * (1 - SLIPPAGE_RATE)

        # Compute transaction costs
        trade_value = fill_price * quantity
        commission  = trade_value * COMMISSION_RATE
        sales_tax   = trade_value * SALES_TAX_RATE \
                      if direction == "SELL" else 0.0
        # Slippage cost is already embedded in fill_price; track for reporting only
        slippage    = abs(fill_price - exec_price) * quantity

        fill = FillEvent(
            timestamp  = exec_date,
            symbol     = ticker,
            quantity   = quantity,
            direction  = direction,
            fill_price = fill_price,
            commission = commission,
            sales_tax  = sales_tax,
            slippage   = slippage,
        )

        self.events.put(fill)

        print(f"  Fill: {direction} {quantity} {ticker} @ "
              f"{fill_price:,.0f} VND on {exec_date.date()} "
              f"(cost: {(commission+sales_tax+slippage):,.0f} VND)")

    def _round_to_lot(self, quantity):
        """
        Round quantity down to nearest lot size (100 shares).
        Vietnam's HOSE requires trades in multiples of 100 shares.
        """
        return (quantity // LOT_SIZE) * LOT_SIZE


if __name__ == "__main__":
    import sys
    sys.path.insert(0, ".")
    from queue import Queue
    from data import load_data
    from events import OrderEvent
    import pandas as pd

    closes, volumes, _ = load_data()

    # Quick test — simulate a BUY order for VCB
    q = Queue()
    handler = SimulatedExecutionHandler(events=q, closes=closes)

    test_order = OrderEvent(
        timestamp  = pd.Timestamp("2021-01-15"),
        symbol     = "VCB",
        order_type = "MKT",
        quantity   = 1000,
        direction  = "BUY",
    )

    print("Testing execution handler...")
    print(f"Order: BUY 1000 VCB on 2021-01-15")
    handler.execute_order(test_order)

    if not q.empty():
        fill = q.get()
        print(f"\nFill received:")
        print(f"  Exec date  : {fill.timestamp.date()}")
        print(f"  Fill price : {fill.fill_price:,.0f} VND")
        print(f"  Quantity   : {fill.quantity} shares")
        print(f"  Commission : {fill.commission:,.0f} VND")
        print(f"  Sales tax  : {fill.sales_tax:,.0f} VND")
        print(f"  Slippage   : {fill.slippage:,.0f} VND")
        print(f"  Total cost : {fill.total_cost:,.0f} VND")
        print(f"  Net value  : {fill.net_value:,.0f} VND")