"""
tools/evaluate_pnl.py

Reconstructs an estimated P&L from the bot's Telegram alert log
(logs/alerts.jsonl, written by monitoring/alerts.py).

For each ENTRY alert:
  - If a matching EXIT alert already carries a real P&L (auto_execute mode),
    use it as-is.
  - If a matching EXIT alert exists but has no P&L (notification-only mode),
    estimate P&L using the reported exit price and a risk-based quantity.
  - If there's no EXIT alert at all, fetch real intraday candles (Yahoo
    Finance) for that symbol/day and simulate whether SL or target was hit
    first (or square-off at EOD), then estimate P&L the same way.

Usage:
    python -m tools.evaluate_pnl
    python -m tools.evaluate_pnl --file logs/alerts.jsonl --capital 100000 --risk-pct 1.0
    python -m tools.evaluate_pnl --no-fetch   # skip network calls, only use logged exits
"""

from __future__ import annotations
import argparse, json, sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import yaml
from tabulate import tabulate

sys.path.insert(0, str(Path(__file__).parent.parent))
from risk.position_sizer import PositionSizer

_SETTINGS_PATH = Path(__file__).parent.parent / "config" / "settings.yaml"


def _load_settings() -> dict:
    with open(_SETTINGS_PATH) as f:
        return yaml.safe_load(f)


def _default_alerts_path(cfg: dict) -> Path:
    return Path(cfg["monitoring"]["log_dir"]) / "alerts.jsonl"


def _to_yahoo_symbol(symbol: str) -> str:
    """'SBIN-EQ' -> 'SBIN.NS' (assumes NSE equity)."""
    base = symbol.split("-")[0]
    return f"{base}.NS"


@dataclass
class TradeRecord:
    symbol: str
    direction: str          # "BUY"/"SELL" (from entry action)
    entry_time: datetime
    entry_price: float
    stop_loss: float
    target: float
    exit_time: datetime | None = None
    exit_price: float | None = None
    exit_reason: str = ""
    pnl: float | None = None    # real pnl if bot reported it
    pnl_source: str = ""        # "reported" | "estimated_from_exit_alert" | "estimated_from_market_data" | "open"


def _load_alerts(path: Path, on_date: str | None = None) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(f"Alert log not found: {path}")
    records = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                rec = json.loads(line)
                if on_date and not rec["timestamp"].startswith(on_date):
                    continue
                records.append(rec)
    return records


def _pair_trades(records: list[dict]) -> list[TradeRecord]:
    """Match ENTRY alerts to the next EXIT alert for the same symbol, in order."""
    pending: dict[str, list[dict]] = {}
    trades: list[TradeRecord] = []

    for rec in records:
        symbol = rec["symbol"]

        if rec["event"] == "ENTRY":
            pending.setdefault(symbol, []).append(rec)

        elif rec["event"] == "EXIT":
            queue = pending.get(symbol, [])
            if not queue:
                continue  # exit alert with no matching entry — ignore
            entry_rec = queue.pop(0)
            trades.append(TradeRecord(
                symbol      = symbol,
                direction   = entry_rec["direction"],
                entry_time  = datetime.fromisoformat(entry_rec["timestamp"]),
                entry_price = entry_rec["entry_price"],
                stop_loss   = entry_rec["stop_loss"],
                target      = entry_rec["target"],
                exit_time   = datetime.fromisoformat(rec["timestamp"]),
                exit_price  = rec["exit_price"],
                exit_reason = rec.get("reason", ""),
                pnl         = rec.get("pnl"),
                pnl_source  = "reported" if rec.get("pnl") is not None else "",
            ))

    # Anything left in `pending` never got an exit alert
    for symbol, queue in pending.items():
        for entry_rec in queue:
            trades.append(TradeRecord(
                symbol      = symbol,
                direction   = entry_rec["direction"],
                entry_time  = datetime.fromisoformat(entry_rec["timestamp"]),
                entry_price = entry_rec["entry_price"],
                stop_loss   = entry_rec["stop_loss"],
                target      = entry_rec["target"],
            ))

    return sorted(trades, key=lambda t: t.entry_time)


def _fetch_intraday(symbol: str, day: datetime):
    try:
        import yfinance as yf
    except ImportError:
        raise RuntimeError(
            "yfinance is required to estimate P&L for trades without a logged exit. "
            "Install it with: pip install yfinance"
        )
    ticker = _to_yahoo_symbol(symbol)
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end   = start + timedelta(days=1)
    df = yf.download(ticker, start=start, end=end, interval="5m", progress=False, auto_adjust=False)
    if df.empty:
        return None
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    return df


def _simulate_exit(trade: TradeRecord, square_off_time: str) -> None:
    """Fill in exit_price/exit_reason/pnl_source by replaying real market candles."""
    df = _fetch_intraday(trade.symbol, trade.entry_time)
    if df is None:
        trade.pnl_source = "open"  # couldn't fetch data — leave unresolved
        return

    is_long = trade.direction == "BUY"
    sq_h, sq_m = (int(x) for x in square_off_time.split(":"))
    square_off_dt = trade.entry_time.replace(hour=sq_h, minute=sq_m, second=0, microsecond=0)

    bars = df[df.index >= trade.entry_time]
    for ts, bar in bars.iterrows():
        if ts >= square_off_dt:
            break
        high, low = float(bar["High"]), float(bar["Low"])
        if is_long:
            hit_sl     = low  <= trade.stop_loss
            hit_target = high >= trade.target
        else:
            hit_sl     = high >= trade.stop_loss
            hit_target = low  <= trade.target

        # Conservative: if both SL and target fall inside the same candle, assume SL hit first
        if hit_sl:
            trade.exit_time, trade.exit_price, trade.exit_reason = ts.to_pydatetime(), trade.stop_loss, "stop_loss (simulated)"
            trade.pnl_source = "estimated_from_market_data"
            return
        if hit_target:
            trade.exit_time, trade.exit_price, trade.exit_reason = ts.to_pydatetime(), trade.target, "target (simulated)"
            trade.pnl_source = "estimated_from_market_data"
            return

    # Neither hit — square off at the last close before square-off time
    eod_bars = df[df.index < square_off_dt]
    if eod_bars.empty:
        trade.pnl_source = "open"
        return
    last = eod_bars.iloc[-1]
    trade.exit_time   = eod_bars.index[-1].to_pydatetime()
    trade.exit_price  = float(last["Close"])
    trade.exit_reason = "square_off (simulated)"
    trade.pnl_source  = "estimated_from_market_data"


def _compute_pnl(trade: TradeRecord, sizer: PositionSizer) -> None:
    if trade.exit_price is None:
        trade.pnl_source = trade.pnl_source or "open"
        return
    qty = sizer.compute_qty(trade.entry_price, trade.stop_loss)
    if qty == 0:
        trade.pnl = 0.0
        return
    sign = 1 if trade.direction == "BUY" else -1
    trade.pnl = round(sign * (trade.exit_price - trade.entry_price) * qty, 2)
    if not trade.pnl_source:
        trade.pnl_source = "estimated_from_exit_alert"


def main() -> None:
    cfg = _load_settings()

    parser = argparse.ArgumentParser(description="Estimate P&L from the bot's Telegram alert log.")
    parser.add_argument("--file", type=Path, default=_default_alerts_path(cfg))
    parser.add_argument("--capital", type=float, default=cfg["trading"]["capital"])
    parser.add_argument("--risk-pct", type=float, default=cfg["risk"]["per_trade_risk_pct"])
    parser.add_argument("--no-fetch", action="store_true", help="Don't fetch market data for trades with no logged exit.")
    parser.add_argument("--date", type=str, default=None,
                        help="Only evaluate alerts from this date (YYYY-MM-DD). Defaults to today; use --all to disable.")
    parser.add_argument("--all", action="store_true", help="Evaluate the entire log, ignoring --date (default: today only).")
    args = parser.parse_args()

    on_date = None if args.all else (args.date or datetime.now().strftime("%Y-%m-%d"))
    records = _load_alerts(args.file, on_date)
    if on_date:
        print(f"(Filtered to {on_date} — pass --all to evaluate the full log, or --date YYYY-MM-DD for another day)\n")
    trades  = _pair_trades(records)
    sizer   = PositionSizer(capital=args.capital, per_trade_risk_pct=args.risk_pct)
    square_off_time = cfg["trading"]["square_off_time"]

    for trade in trades:
        if trade.pnl is not None:
            continue  # already have a real, reported pnl
        if trade.exit_price is None and not args.no_fetch:
            _simulate_exit(trade, square_off_time)
        _compute_pnl(trade, sizer)

    rows = []
    total_pnl, resolved, wins = 0.0, 0, 0
    for t in trades:
        pnl_display = f"{t.pnl:+,.2f}" if t.pnl is not None else "-"
        rows.append([
            t.entry_time.strftime("%Y-%m-%d %H:%M"), t.symbol, t.direction,
            f"{t.entry_price:.2f}", f"{t.stop_loss:.2f}", f"{t.target:.2f}",
            f"{t.exit_price:.2f}" if t.exit_price is not None else "-",
            t.exit_reason or "-", pnl_display, t.pnl_source or "open",
        ])
        if t.pnl is not None:
            total_pnl += t.pnl
            resolved  += 1
            wins      += 1 if t.pnl > 0 else 0

    print(tabulate(rows, headers=[
        "Entry Time", "Symbol", "Dir", "Entry", "SL", "Target",
        "Exit", "Reason", "P&L (₹)", "Source",
    ]))
    print()
    print(f"Trades: {len(trades)} | Resolved: {resolved} | Open/Unresolved: {len(trades) - resolved}")
    if resolved:
        print(f"Win rate: {wins}/{resolved} ({wins / resolved * 100:.1f}%)")
    print(f"Total estimated P&L: \u20b9{total_pnl:+,.2f}")


if __name__ == "__main__":
    main()
