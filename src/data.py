"""
data.py — Data handler for the event-driven MA crossover backtester.

Responsibilities:
- Download daily OHLCV data from vnstock for all VN30 master tickers
- Compute point-in-time top N universe at each rebalancing date
  ranked by average daily trading value (price × volume)
- Generate MarketEvents and UniverseUpdateEvents for the backtest engine
- Handle missing data, late IPOs, and delistings gracefully

Rate limiting: 3s between requests, 60s pause every 15 requests.
"""

import pandas as pd
import numpy as np
import os
import time
from queue import Queue
from events import MarketEvent, UniverseUpdateEvent

# ── Point-in-time VN30 constituents ────────────────────────────────────────────
# Source: HOSE official announcements
# Same verified lists as VN30-Momentum project

VN30_CONSTITUENTS = {
    "2018-01": ["BID","BMP","BVH","CII","CTD","CTG","DHG","DPM","FPT","GAS","GMD","HPG","HSG","KDC","MBB","MSN","MWG","NT2","NVL","PLX","REE","ROS","SAB","SBT","SSI","STB","VCB","VIC","VJC","VNM"],
    "2018-07": ["BMP","CII","CTD","CTG","DHG","DPM","FPT","GAS","GMD","HPG","HSG","KDC","MBB","MSN","MWG","NVL","PLX","PNJ","REE","ROS","SAB","SBT","SSI","STB","VCB","VIC","VJC","VNM","VPB","VRE"],
    "2019-01": ["CII","CTD","CTG","DHG","DPM","EIB","FPT","GAS","GMD","HDB","HPG","MBB","MSN","MWG","NVL","PNJ","REE","ROS","SAB","SBT","SSI","STB","TCB","VCB","VHM","VIC","VJC","VNM","VPB","VRE"],
    "2019-07": ["BID","BVH","CTD","CTG","DPM","EIB","FPT","GAS","GMD","HDB","HPG","MBB","MSN","MWG","NVL","PNJ","REE","ROS","SAB","SBT","SSI","STB","TCB","VCB","VHM","VIC","VJC","VNM","VPB","VRE"],
    "2020-01": ["BID","BVH","CTD","CTG","EIB","FPT","GAS","HDB","HPG","MBB","MSN","MWG","NVL","PLX","PNJ","POW","REE","ROS","SAB","SBT","SSI","STB","TCB","VCB","VHM","VIC","VJC","VNM","VPB","VRE"],
    "2020-07": ["BID","CTG","EIB","FPT","GAS","HDB","HPG","KDH","MBB","MSN","MWG","NVL","PLX","PNJ","POW","REE","ROS","SAB","SBT","SSI","STB","TCB","TCH","VCB","VHM","VIC","VJC","VNM","VPB","VRE"],
    "2021-01": ["BID","BVH","CTG","FPT","GAS","HDB","HPG","KDH","MBB","MSN","MWG","NVL","PDR","PLX","PNJ","POW","REE","SBT","SSI","STB","TCB","TCH","TPB","VCB","VHM","VIC","VJC","VNM","VPB","VRE"],
    "2021-07": ["ACB","BID","BVH","CTG","FPT","GAS","GVR","HDB","HPG","KDH","MBB","MSN","MWG","NVL","PDR","PLX","PNJ","POW","SAB","SSI","STB","TCB","TPB","VCB","VHM","VIC","VJC","VNM","VPB","VRE"],
    "2022-01": ["ACB","BID","BVH","CTG","FPT","GAS","GVR","HDB","HPG","KDH","MBB","MSN","MWG","NVL","PDR","PLX","PNJ","POW","SAB","SSI","STB","TCB","TPB","VCB","VHM","VIC","VJC","VNM","VPB","VRE"],
    "2022-07": ["ACB","BID","BVH","CTG","FPT","GAS","GVR","HDB","HPG","KDH","MBB","MSN","MWG","NVL","PDR","PLX","POW","SAB","SSI","STB","TCB","TPB","VCB","VHM","VIB","VIC","VJC","VNM","VPB","VRE"],
    "2023-01": ["ACB","BCM","BID","BVH","CTG","FPT","GAS","GVR","HDB","HPG","MBB","MSN","MWG","NVL","PDR","PLX","POW","SAB","SSI","STB","TCB","TPB","VCB","VHM","VIB","VIC","VJC","VNM","VPB","VRE"],
    "2023-07": ["ACB","BCM","BID","BVH","CTG","FPT","GAS","GVR","HDB","HPG","MBB","MSN","MWG","PLX","POW","SAB","SHB","SSB","SSI","STB","TCB","TPB","VCB","VHM","VIB","VIC","VJC","VNM","VPB","VRE"],
    "2024-01": ["ACB","BCM","BID","BVH","CTG","FPT","GAS","GVR","HDB","HPG","MBB","MSN","MWG","PLX","POW","SAB","SHB","SSB","SSI","STB","TCB","TPB","VCB","VHM","VIB","VIC","VJC","VNM","VPB","VRE"],
    "2024-07": ["ACB","BCM","BID","BVH","CTG","FPT","GAS","GVR","HDB","HPG","MBB","MSN","MWG","PLX","POW","SAB","SHB","SSB","SSI","STB","TCB","TPB","VCB","VHM","VIB","VIC","VJC","VNM","VPB","VRE"],
}

# All unique tickers ever in VN30
VN30_MASTER = sorted(set(
    t for tickers in VN30_CONSTITUENTS.values() for t in tickers
))

# Rebalancing dates for 3-month and 6-month frequencies
REBALANCE_DATES_6M = {
    "2018-01": pd.Timestamp("2018-01-22"),
    "2018-07": pd.Timestamp("2018-07-23"),
    "2019-01": pd.Timestamp("2019-02-11"),
    "2019-07": pd.Timestamp("2019-08-05"),
    "2020-01": pd.Timestamp("2020-02-03"),
    "2020-07": pd.Timestamp("2020-08-03"),
    "2021-01": pd.Timestamp("2021-02-01"),
    "2021-07": pd.Timestamp("2021-08-02"),
    "2022-01": pd.Timestamp("2022-02-07"),
    "2022-07": pd.Timestamp("2022-08-01"),
    "2023-01": pd.Timestamp("2023-02-06"),
    "2023-07": pd.Timestamp("2023-08-07"),
    "2024-01": pd.Timestamp("2024-02-05"),
    "2024-07": pd.Timestamp("2024-08-05"),
}

REBALANCE_DATES_3M = {
    "2018-01": pd.Timestamp("2018-01-22"),
    "2018-04": pd.Timestamp("2018-04-01"),
    "2018-07": pd.Timestamp("2018-07-23"),
    "2018-10": pd.Timestamp("2018-10-01"),
    "2019-01": pd.Timestamp("2019-02-11"),
    "2019-04": pd.Timestamp("2019-04-01"),
    "2019-07": pd.Timestamp("2019-08-05"),
    "2019-10": pd.Timestamp("2019-10-01"),
    "2020-01": pd.Timestamp("2020-02-03"),
    "2020-04": pd.Timestamp("2020-04-01"),
    "2020-07": pd.Timestamp("2020-08-03"),
    "2020-10": pd.Timestamp("2020-10-01"),
    "2021-01": pd.Timestamp("2021-02-01"),
    "2021-04": pd.Timestamp("2021-04-01"),
    "2021-07": pd.Timestamp("2021-08-02"),
    "2021-10": pd.Timestamp("2021-10-01"),
    "2022-01": pd.Timestamp("2022-02-07"),
    "2022-04": pd.Timestamp("2022-04-01"),
    "2022-07": pd.Timestamp("2022-08-01"),
    "2022-10": pd.Timestamp("2022-10-01"),
    "2023-01": pd.Timestamp("2023-02-06"),
    "2023-04": pd.Timestamp("2023-04-01"),
    "2023-07": pd.Timestamp("2023-08-07"),
    "2023-10": pd.Timestamp("2023-10-01"),
    "2024-01": pd.Timestamp("2024-02-05"),
    "2024-04": pd.Timestamp("2024-04-01"),
    "2024-07": pd.Timestamp("2024-08-05"),
    "2024-10": pd.Timestamp("2024-10-01"),
}
def get_vn30_constituents(date):
    """
    Return point-in-time VN30 constituents for a given date.
    Uses the most recent official list as of that date.
    """
    date = pd.Timestamp(date)
    year  = date.year
    month = date.month
    key   = f"{year}-01" if month < 7 else f"{year}-07"

    periods = sorted(VN30_CONSTITUENTS.keys())
    if key not in VN30_CONSTITUENTS:
        available = [p for p in periods if p <= key]
        key = available[-1] if available else periods[0]

    return VN30_CONSTITUENTS[key]


def download_single(symbol, start="2015-01-01", end="2024-12-31",
                    retries=3, delay=5):
    """
    Download daily OHLCV from vnstock with retry wrapper.
    Returns DataFrame with columns: open, high, low, close, volume
    """
    from vnstock import Quote

    for attempt in range(retries):
        try:
            quote = Quote(symbol=symbol, source='KBS')
            df    = quote.history(start=start, end=end, interval='1D')

            if df is None or df.empty:
                return None

            df['time'] = pd.to_datetime(df['time'])
            df = df.set_index('time')
            df = df[['open', 'high', 'low', 'close', 'volume']]
            df = df.sort_index()
            df = df[~df.index.duplicated(keep='last')]
            df.index.name = 'date'
            return df

        except Exception as e:
            if attempt < retries - 1:
                print(f"  ↻ {symbol} attempt {attempt+1} failed — "
                      f"retrying in {delay}s")
                time.sleep(delay)
            else:
                print(f"  ✗ {symbol}: {e}")
                return None


def download_all(symbols, start="2015-01-01", end="2024-12-31",
                 delay=3, batch_pause=60, batch_size=15):
    """
    Download daily OHLCV for all symbols with rate limiting.
    Saves close prices and volume separately for easy access.
    """
    closes  = {}
    volumes = {}
    failed  = []
    total   = len(symbols)

    print(f"\nDownloading {total} tickers (daily OHLCV)...")
    print(f"Est. time: ~{(total*delay + (total//batch_size)*batch_pause)//60+1} mins\n")

    opens = {}
    for i, symbol in enumerate(symbols):
        print(f"[{i+1}/{total}] {symbol}...", end=" ", flush=True)
        df = download_single(symbol, start, end)

        if df is not None and not df.empty:
            closes[symbol]  = df['close']
            volumes[symbol] = df['volume']
            opens[symbol]   = df['open']
            print(f"✓ {len(df)} days")
        else:
            failed.append(symbol)
            print("✗ no data")

        if (i + 1) % batch_size == 0 and i + 1 < total:
            print(f"\n  --- Pausing {batch_pause}s ---\n")
            time.sleep(batch_pause)
        else:
            time.sleep(delay)

    if failed:
        print(f"\nFailed ({len(failed)}): {failed}")

    closes_df  = pd.DataFrame(closes)
    volumes_df = pd.DataFrame(volumes)
    opens_df   = pd.DataFrame(opens)

    return closes_df, volumes_df, opens_df, failed


def get_top_n_universe(closes, volumes, rebalance_date,
                       top_n=5, lookback_days=126, min_history=63):
    """
    At a rebalancing date, select top N stocks from VN30 by
    average daily trading value over the previous lookback_days.

    Trading value = close price × volume (VND per day)
    Only stocks with at least min_history days of data are eligible.

    Parameters
    ----------
    closes         : DataFrame of daily close prices
    volumes        : DataFrame of daily volumes
    rebalance_date : pd.Timestamp
    top_n          : number of stocks to select (default 5)
    lookback_days  : days to compute avg trading value (default 126 = 6 months)
    min_history    : minimum days of history required (default 63 = 3 months)

    Returns
    -------
    list of selected tickers
    """
    date         = pd.Timestamp(rebalance_date)
    constituents = get_vn30_constituents(date)

    # Only consider stocks in VN30 at this date
    eligible = [t for t in constituents if t in closes.columns]

    # Get lookback window — data BEFORE rebalance date only
    window_close  = closes.loc[:date].tail(lookback_days)
    window_volume = volumes.loc[:date].tail(lookback_days)

    scores = {}
    for ticker in eligible:
        if ticker not in window_close.columns:
            continue

        c = window_close[ticker].dropna()
        v = window_volume[ticker].dropna()

        # Need minimum history
        if len(c) < min_history or len(v) < min_history:
            continue

        # Align and compute average daily trading value
        common     = c.index.intersection(v.index)
        trade_val  = (c.loc[common] * v.loc[common]).mean()
        scores[ticker] = trade_val

    # Rank by trading value descending, take top N
    ranked  = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    top_n_stocks = [t for t, _ in ranked[:top_n]]

    return top_n_stocks


def build_universe_schedule(closes, volumes, top_n=5,
                             rebalance_freq="6M"):
    """
    Build the full universe schedule — which stocks are in the
    watchlist at each rebalancing date.

    Parameters
    ----------
    closes         : DataFrame of daily close prices
    volumes        : DataFrame of daily volumes
    top_n          : number of stocks (3, 5, or 10)
    rebalance_freq : '3M' or '6M'

    Returns
    -------
    dict of {pd.Timestamp: [list of tickers]}
    """
    dates = REBALANCE_DATES_3M if rebalance_freq == "3M" \
            else REBALANCE_DATES_6M

    schedule = {}

    print(f"\nBuilding universe schedule "
          f"(top {top_n}, {rebalance_freq} rebalancing)...")

    for period in dates:
        # Convert period string to first trading day of that month
        date = pd.Timestamp(f"{period}-01")

        # Skip if before our data starts
        if date < closes.index[0]:
            continue

        universe = get_top_n_universe(
            closes, volumes, date, top_n=top_n
        )

        if universe:
            schedule[date] = universe
            print(f"  {period}: {universe}")

    return schedule


def generate_universe_update_events(schedule):
    """
    Convert universe schedule into UniverseUpdateEvents.
    Detects which stocks were added and removed at each rebalancing.
    """
    events    = []
    dates     = sorted(schedule.keys())
    prev_univ = []

    for date in dates:
        curr_univ    = schedule[date]
        added        = [t for t in curr_univ if t not in prev_univ]
        removed      = [t for t in prev_univ if t not in curr_univ]

        events.append(UniverseUpdateEvent(
            timestamp      = date,
            stocks_added   = added,
            stocks_removed = removed,
            new_universe   = curr_univ,
        ))

        prev_univ = curr_univ

    return events


def save_data(closes, volumes, opens=None, path="data/processed"):
    """Save price and volume data to CSV."""
    os.makedirs(path, exist_ok=True)
    closes.to_csv(f"{path}/closes_daily.csv")
    volumes.to_csv(f"{path}/volumes_daily.csv")
    if opens is not None:
        opens.to_csv(f"{path}/opens_daily.csv")
    print(f"\nSaved to {path}/")
    print(f"  closes : {closes.shape}")
    print(f"  volumes: {volumes.shape}")
    if opens is not None:
        print(f"  opens  : {opens.shape}")


def load_data(path="data/processed"):
    """Load saved price and volume data.

    Returns closes, volumes, opens. Opens is None if not yet downloaded
    (re-run download_all to generate opens_daily.csv).
    """
    closes  = pd.read_csv(f"{path}/closes_daily.csv",
                           index_col=0, parse_dates=True)
    volumes = pd.read_csv(f"{path}/volumes_daily.csv",
                           index_col=0, parse_dates=True)
    opens_path = f"{path}/opens_daily.csv"
    if os.path.exists(opens_path):
        opens = pd.read_csv(opens_path, index_col=0, parse_dates=True)
    else:
        opens = None
    return closes, volumes, opens


if __name__ == "__main__":
    import sys
    sys.path.insert(0, ".")

    # Load existing data if available, otherwise download
    if os.path.exists("data/processed/closes_daily.csv"):
        print("Loading existing data...")
        closes, volumes, opens = load_data()
        print(f"  Closes : {closes.shape}")
        print(f"  Volumes: {volumes.shape}")
        if opens is not None:
            print(f"  Opens  : {opens.shape}")
        else:
            print("  Opens  : not available (re-download to generate)")
    else:
        print("No existing data found — downloading...")
        closes, volumes, opens, failed = download_all(
            VN30_MASTER,
            start="2015-01-01",
            end  ="2024-12-31"
        )
        save_data(closes, volumes, opens)

    # Test universe schedule — 6M rebalancing, top 5
    schedule_6m_5 = build_universe_schedule(
        closes, volumes, top_n=5, rebalance_freq="6M"
    )

    # Test universe schedule — 3M rebalancing, top 5
    schedule_3m_5 = build_universe_schedule(
        closes, volumes, top_n=5, rebalance_freq="3M"
    )

    # Show universe update events
    print("\nUniverse update events (6M, top 5):")
    events = generate_universe_update_events(schedule_6m_5)
    for e in events[:5]:
        print(f"  {e.timestamp.date()}: "
              f"added={e.stocks_added} removed={e.stocks_removed}")