"""
tools/evaluate_skipped_pnl.py

Reconstructs hypothetical P&L for signals that were ALERTED as a top pick but
never got a real order placed (skipped due to new-entry cutoff, circuit
breaker halt, or max-concurrent-positions), using real Angel One intraday
candles for the day in question.

Data sources (per-day log folder, e.g. 24-09-26_Trade_logs/):
  - algo.log:     "[AutoExec] Skipped SYMBOL: reason" lines (which setups were skipped, when, why)
  - trades.jsonl: SIGNAL events (entry_price/stop_loss/target/token per symbol)

Usage:
    python -m tools.evaluate_skipped_pnl --dir 24-09-26_Trade_logs --date 2026-09-24
"""

from __future__ import annotations
import argparse, json, re, sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml
from tabulate import tabulate

sys.path.insert(0, str(Path(__file__).parent.parent))
from risk.position_sizer import PositionSizer

_SETTINGS_PATH = Path(__file__).parent.parent / "config" / "settings.yaml"

_SKIP_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \S+ +\S+ \| \[AutoExec\] Skipped ([A-Z0-9&]+-EQ): (.*)$"
)


def _categorize(reason: str) -> str:
    if "cutoff" in reason:
        return "cutoff"
    if "Consecutive losses" in reason or "HALTED" in reason:
        return "consec_halt"
    if "Max concurrent" in reason:
        return "max_concurrent"
    if "regime" in reason:
        return "regime"
    return "other"


@dataclass
class SkippedSignal:
    symbol: str
    token: str
    direction: str          # "BUY" / "SELL"
    entry_time: datetime
    entry_price: float
    stop_loss: float
    target: float
    skip_reason: str
    skip_category: str
    exit_time: datetime | None = None
    exit_price: float | None = None
    exit_reason: str = ""
    pnl: float | None = None


def _load_settings() -> dict:
    with open(_SETTINGS_PATH) as f:
        return yaml.safe_load(f)


def _parse_skips(algo_log: Path) -> list[tuple[datetime, str, str, str]]:
    """Returns list of (timestamp, symbol, reason, category)."""
    out = []
    with open(algo_log) as f:
        for line in f:
            m = _SKIP_RE.match(line)
            if not m:
                continue
            ts_str, symbol, reason = m.groups()
            ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
            out.append((ts, symbol, reason, _categorize(reason)))
    return out


def _parse_signals(trades_jsonl: Path) -> dict[str, list[dict]]:
    """symbol -> list of SIGNAL records (signal != HOLD), sorted by time."""
    by_symbol: dict[str, list[dict]] = {}
    with open(trades_jsonl) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("event") != "SIGNAL" or rec.get("signal") == "HOLD":
                continue
            rec["_ts"] = datetime.fromisoformat(rec["timestamp"])
            by_symbol.setdefault(rec["symbol"], []).append(rec)
    for sigs in by_symbol.values():
        sigs.sort(key=lambda r: r["_ts"])
    return by_symbol


def _match_signal(signals: list[dict], skip_ts: datetime, window_secs: float = 30.0) -> dict | None:
    """Find the SIGNAL record for this symbol closest to (and at/before) the skip timestamp."""
    best = None
    for rec in signals:
        delta = (skip_ts - rec["_ts"]).total_seconds()
        if 0 <= delta <= window_secs:
            best = rec  # keep the latest one within the window
    return best


def build_skipped_trades(log_dir: Path) -> list[SkippedSignal]:
    skips = _parse_skips(log_dir / "algo.log")
    signals_by_symbol = _parse_signals(log_dir / "trades.jsonl")

    trades: list[SkippedSignal] = []
    seen_keys: set[tuple[str, str]] = set()  # (symbol, entry_time) dedupe — same setup logged as skipped more than once
    for ts, symbol, reason, category in skips:
        sig = _match_signal(signals_by_symbol.get(symbol, []), ts)
        if sig is None:
            continue
        key = (symbol, sig["timestamp"])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        trades.append(SkippedSignal(
            symbol=symbol, token=sig["token"], direction=sig["signal"],
            entry_time=sig["_ts"], entry_price=sig["entry_price"],
            stop_loss=sig["stop_loss"], target=sig["target"],
            skip_reason=reason, skip_category=category,
        ))
    return sorted(trades, key=lambda t: t.entry_time)


def _fetch_today_candles(fetcher, exchange: str, token: str, day: datetime, interval_minutes: int):
    from_date = day.replace(hour=9, minute=15, second=0, microsecond=0)
    to_date   = day.replace(hour=15, minute=30, second=0, microsecond=0)
    return fetcher.fetch(exchange, token, interval_minutes, from_date, to_date)


def _simulate_exit(trade: SkippedSignal, df, square_off_time: str) -> None:
    if df is None or df.empty:
        trade.exit_reason = "no_data"
        return

    is_long = trade.direction == "BUY"
    sq_h, sq_m = (int(x) for x in square_off_time.split(":"))
    square_off_dt = trade.entry_time.replace(hour=sq_h, minute=sq_m, second=0, microsecond=0)

    bars = df[df["timestamp"] >= trade.entry_time]
    for _, bar in bars.iterrows():
        ts = bar["timestamp"]
        if ts >= square_off_dt:
            break
        high, low = float(bar["high"]), float(bar["low"])
        if is_long:
            hit_sl, hit_target = low <= trade.stop_loss, high >= trade.target
        else:
            hit_sl, hit_target = high >= trade.stop_loss, low <= trade.target

        if hit_sl:
            trade.exit_time, trade.exit_price, trade.exit_reason = ts, trade.stop_loss, "stop_loss (simulated)"
            return
        if hit_target:
            trade.exit_time, trade.exit_price, trade.exit_reason = ts, trade.target, "target (simulated)"
            return

    eod_bars = df[df["timestamp"] < square_off_dt]
    if eod_bars.empty:
        trade.exit_reason = "no_data"
        return
    last = eod_bars.iloc[-1]
    trade.exit_time, trade.exit_price, trade.exit_reason = last["timestamp"], float(last["close"]), "square_off (simulated)"


def _compute_pnl(trade: SkippedSignal, sizer: PositionSizer) -> None:
    if trade.exit_price is None:
        return
    qty = sizer.compute_qty(trade.entry_price, trade.stop_loss)
    if qty == 0:
        trade.pnl = 0.0
        return
    sign = 1 if trade.direction == "BUY" else -1
    trade.pnl = round(sign * (trade.exit_price - trade.entry_price) * qty, 2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Estimate hypothetical P&L for skipped (not-executed) signals.")
    parser.add_argument("--dir", type=Path, required=True, help="Day's log folder, e.g. 24-09-26_Trade_logs")
    args = parser.parse_args()

    cfg = _load_settings()
    trading_cfg = cfg["trading"]
    interval_minutes = trading_cfg["candle_interval_minutes"]
    square_off_time  = trading_cfg["square_off_time"]

    trades = build_skipped_trades(args.dir)
    if not trades:
        print("No skipped signals found / matched to a SIGNAL record.")
        return

    print(f"Found {len(trades)} skipped setups to evaluate.\n")

    from auth.session_manager import SessionManager
    from data.historical_fetcher import HistoricalFetcher

    sm = SessionManager()
    obj = sm.login()
    fetcher = HistoricalFetcher(obj)

    day = trades[0].entry_time
    for i, t in enumerate(trades, 1):
        print(f"  [{i}/{len(trades)}] Fetching {t.symbol} ({t.token}) ...", end="\r")
        df = _fetch_today_candles(fetcher, "NSE", t.token, day, interval_minutes)
        _simulate_exit(t, df, square_off_time)
    print()

    sm.logout()

    sizer = PositionSizer(capital=trading_cfg["capital"], per_trade_risk_pct=cfg["risk"]["per_trade_risk_pct"])
    for t in trades:
        _compute_pnl(t, sizer)

    rows = []
    total_pnl, resolved, wins = 0.0, 0, 0
    cat_pnl: dict[str, float] = {}
    for t in trades:
        pnl_display = f"{t.pnl:+,.2f}" if t.pnl is not None else "-"
        rows.append([
            t.entry_time.strftime("%H:%M:%S"), t.symbol, t.direction, t.skip_category,
            f"{t.entry_price:.2f}", f"{t.stop_loss:.2f}", f"{t.target:.2f}",
            f"{t.exit_price:.2f}" if t.exit_price is not None else "-",
            t.exit_reason or "-", pnl_display,
        ])
        if t.pnl is not None:
            total_pnl += t.pnl
            resolved += 1
            wins += 1 if t.pnl > 0 else 0
            cat_pnl[t.skip_category] = cat_pnl.get(t.skip_category, 0.0) + t.pnl

    print(tabulate(rows, headers=[
        "Time", "Symbol", "Dir", "Skip Reason", "Entry", "SL", "Target", "Exit", "Exit Reason", "P&L (₹)",
    ]))
    print()
    print(f"Skipped setups: {len(trades)} | Resolved: {resolved} | No data: {len(trades) - resolved}")
    if resolved:
        print(f"Win rate: {wins}/{resolved} ({wins / resolved * 100:.1f}%)")
    print(f"Total hypothetical P&L if these had been taken: \u20b9{total_pnl:+,.2f}")
    print("\nBy skip reason:")
    for cat, pnl in sorted(cat_pnl.items(), key=lambda kv: -kv[1]):
        print(f"  {cat:<15} \u20b9{pnl:+,.2f}")


if __name__ == "__main__":
    main()
