"""
tools/review_stalled_trades.py

One-off diagnostic: for the 8 "stalled — cut early" trades from a given day's
alerts.jsonl, fetch real intraday candles via Angel One and simulate what
would have happened had early_cut_candles NOT fired — i.e. holding through to
stop/target/max_holding_candles/EOD square-off instead.

Usage: python -m tools.review_stalled_trades
"""
from __future__ import annotations
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))
from auth.session_manager import SessionManager
from data.historical_fetcher import HistoricalFetcher

LOG_DIR = Path("24-09-26_Trade_logs")
SETTINGS_PATH = Path("config/settings.yaml")

TRADES = [
    # symbol, direction, entry_price, stop_loss, target, actual_exit_price, actual_pnl
    ("WAAREEENER-EQ", "SELL", 2489.1, 2501.58, 2464.14, 2489.3, -8.4676),
    ("IFCI-EQ",       "SELL", 75.8,   77.01,   73.38,   75.96,  -33.7066),
    ("TATACAP-EQ",    "SELL", 339.7,  343.15,  332.79,  338.85, 23.2717),
    ("BHEL-EQ",       "SELL", 418.15, 421.09,  412.27,  417.8,  2.8772),
    ("SWANCORP-EQ",   "SELL", 296.4,  298.84,  291.52,  296.65, -17.9724),
    ("NYKAA-EQ",      "BUY",  341.7,  339.25,  346.6,   341.7,  -7.3807),
    ("ANANTRAJ-EQ",   "BUY",  632.5,  626.53,  644.43,  630.0,  -54.6963),
    ("SAGILITY-EQ",   "SELL", 45.25,  45.51,   44.73,   45.35,  -35.1017),
]
ENTRY_TS = {  # candle-close time of the entry (from alerts.jsonl ENTRY timestamps, rounded to candle)
    "WAAREEENER-EQ": "10:30", "IFCI-EQ": "10:30", "TATACAP-EQ": "10:30", "BHEL-EQ": "10:30",
    "SWANCORP-EQ": "10:30", "NYKAA-EQ": "10:30", "ANANTRAJ-EQ": "10:30", "SAGILITY-EQ": "10:45",
}


def _load_settings() -> dict:
    with open(SETTINGS_PATH) as f:
        return yaml.safe_load(f)


def main() -> None:
    cfg = _load_settings()
    wl = {i["symbol"]: (str(i["token"]), i["exchange"]) for i in cfg["watchlist"]["instruments"]}
    max_holding_candles = cfg["strategy"].get("max_holding_candles", 8)
    interval_min = cfg["trading"]["candle_interval_minutes"]
    sq_off_time = cfg["trading"]["square_off_time"]

    sm = SessionManager()
    obj = sm.login()
    fetcher = HistoricalFetcher(obj)

    today = datetime.now().date()
    from_dt = datetime.combine(today, datetime.min.time()).replace(hour=9, minute=15)
    to_dt = datetime.combine(today, datetime.min.time()).replace(hour=15, minute=30)

    print(f"{'Symbol':16s} {'Dir':4s} {'ActualPnL':>10s} | {'WouldHit':10s} {'AtTime':8s} {'Price':>8s} {'HoldPnL':>10s} | Delta")
    total_actual = 0.0
    total_extended = 0.0

    for symbol, direction, entry_price, stop_loss, target, actual_exit, actual_pnl in TRADES:
        token, exch = wl[symbol]
        df = fetcher.fetch(exch, token, interval_min, from_dt, to_dt)
        if df.empty:
            print(f"{symbol:16s} NO DATA")
            continue
        df = df.sort_values("timestamp").reset_index(drop=True)

        entry_time_str = ENTRY_TS[symbol]
        eh, em = map(int, entry_time_str.split(":"))
        entry_ts = datetime.combine(today, datetime.min.time()).replace(hour=eh, minute=em)

        # Bars strictly after the entry candle
        post_entry = df[df["timestamp"] > entry_ts].reset_index(drop=True)
        sq_h, sq_m = map(int, sq_off_time.split(":"))
        square_off_ts = datetime.combine(today, datetime.min.time()).replace(hour=sq_h, minute=sq_m)
        max_holding_ts = entry_ts + timedelta(minutes=interval_min * max_holding_candles)
        cutoff_ts = min(square_off_ts, max_holding_ts)

        outcome = "max_hold/EOD"
        exit_price = None
        exit_ts = None
        for _, bar in post_entry.iterrows():
            if bar["timestamp"] > cutoff_ts:
                break
            hi, lo = bar["high"], bar["low"]
            if direction == "SELL":
                hit_stop = hi >= stop_loss
                hit_target = lo <= target
            else:
                hit_stop = lo <= stop_loss
                hit_target = hi >= target
            if hit_stop and hit_target:
                # Ambiguous same-bar hit — conservatively assume stop first (worst case)
                outcome, exit_price, exit_ts = "stop_hit", stop_loss, bar["timestamp"]
                break
            elif hit_stop:
                outcome, exit_price, exit_ts = "stop_hit", stop_loss, bar["timestamp"]
                break
            elif hit_target:
                outcome, exit_price, exit_ts = "target_hit", target, bar["timestamp"]
                break

        if exit_price is None:
            # Never hit stop/target within the window — exit at cutoff bar's close
            window = post_entry[post_entry["timestamp"] <= cutoff_ts]
            if not window.empty:
                exit_price = window.iloc[-1]["close"]
                exit_ts = window.iloc[-1]["timestamp"]
            else:
                exit_price = entry_price
                exit_ts = entry_ts

        qty = round(abs(actual_pnl) / max(abs(entry_price - stop_loss), 0.01)) or 1  # rough qty proxy from actual trade's risk
        # Better: reconstruct qty from the ratio used in the real trade (risk-based sizing keeps qty constant regardless of exit)
        if direction == "SELL":
            hold_pnl = (entry_price - exit_price)
        else:
            hold_pnl = (exit_price - entry_price)

        total_actual += actual_pnl
        # scale hold_pnl per-share into an approx trade pnl using the same $ per point as the actual trade
        pts_actual = (entry_price - actual_exit) if direction == "SELL" else (actual_exit - entry_price)
        per_point_value = actual_pnl / pts_actual if abs(pts_actual) > 1e-6 else 0.0
        hold_pnl_scaled = hold_pnl * per_point_value
        total_extended += hold_pnl_scaled

        print(f"{symbol:16s} {direction:4s} {actual_pnl:>10.2f} | {outcome:10s} "
              f"{exit_ts.strftime('%H:%M') if exit_ts else '?':8s} {exit_price:>8.2f} {hold_pnl_scaled:>10.2f} | "
              f"{hold_pnl_scaled - actual_pnl:+.2f}")

    print(f"\nTotal actual (early-cut) PnL:   ₹{total_actual:+.2f}")
    print(f"Total if held to stop/target/max-hold: ₹{total_extended:+.2f}")
    print(f"Difference: ₹{total_extended - total_actual:+.2f}")

    sm.logout()


if __name__ == "__main__":
    main()
