"""
tools/backtest_diagnose.py

Root-cause diagnostic on top of tools/backtest_compare.py: instead of just the
aggregate P&L/win-rate, this dumps trade-level detail (exit reason breakdown,
brokerage/slippage cost drag, realized R-multiple by exit type) to understand
*why* the strategy is net-negative, not just confirm that it is.

Runs the NEW (current) config only, on a representative subset of the most
liquid large-cap symbols (fast to fetch, most representative of real fills).

Usage:
    python -m tools.backtest_diagnose
"""
from __future__ import annotations
import sys
import logging
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml
import numpy as np

from auth.session_manager import SessionManager
from data.historical_fetcher import HistoricalFetcher
from strategy.signal_engine import ORBEMAVWAPStrategy
from backtest.engine import BacktestEngine, BacktestConfig
from tools.backtest_compare import build_base_kwargs, NEW_KW, TOTAL_DAYS

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

SETTINGS_PATH = Path(__file__).parent.parent / "config" / "settings.yaml"
SUBSET_SIZE = 40  # first N watchlist symbols (large-caps) — fast + representative


def main() -> None:
    cfg = yaml.safe_load(open(SETTINGS_PATH))
    strategy_cfg = cfg["strategy"]
    risk_cfg = cfg["risk"]
    interval_min = cfg["trading"]["candle_interval_minutes"]
    capital = cfg["trading"]["capital"]
    watchlist = cfg["watchlist"]["instruments"][:SUBSET_SIZE]

    bt_cfg = BacktestConfig(capital=capital)

    print(f"Logging in to Angel One ... (subset: {len(watchlist)} symbols)")
    sm = SessionManager()
    obj = sm.login()
    fetcher = HistoricalFetcher(obj)

    base_kw = build_base_kwargs(strategy_cfg, risk_cfg, interval_min)

    all_trades = []
    for i, inst in enumerate(watchlist, 1):
        token = str(inst["token"])
        symbol = inst["symbol"]
        exch = inst["exchange"]

        df = fetcher.fetch_in_chunks(exch, token, interval_min, total_days=TOTAL_DAYS, chunk_days=TOTAL_DAYS)
        if df.empty or len(df) < 50:
            continue

        print(f"({i}/{len(watchlist)}) {symbol}: {len(df)} candles")
        engine = BacktestEngine(
            strategy_class=ORBEMAVWAPStrategy, symbol=symbol, token=token,
            strategy_kwargs={**base_kw, **NEW_KW},
            config=bt_cfg,
        )
        result = engine.run(df)
        all_trades.extend(result.trades)

    sm.logout()

    n_trades = len(all_trades)
    gross_pnl = sum(t["gross_pnl"] for t in all_trades)
    brokerage_total = sum(t["brokerage"] for t in all_trades)
    net_pnl = sum(t["pnl"] for t in all_trades)
    avg_brokerage = brokerage_total / n_trades if n_trades else 0.0

    print("\n" + "=" * 62)
    print(f"TRADE-LEVEL DIAGNOSTIC — {len(watchlist)} symbols, {TOTAL_DAYS} days, NEW config")
    print("=" * 62)
    print(f"Total trades           : {n_trades}")
    print(f"Gross P&L (excl. brokerage): Rs {gross_pnl:+,.2f}")
    print(f"Brokerage cost (@Rs{bt_cfg.brokerage_pct}% capped Rs{bt_cfg.brokerage_cap}/order): Rs {brokerage_total:,.2f} (avg Rs{avg_brokerage:.2f}/trade)")
    print(f"Net P&L (incl. brokerage)  : Rs {net_pnl:+,.2f}")
    print(f"Brokerage as % of gross risk budget (Rs250/trade): {avg_brokerage / 250 * 100:.1f}%")

    # Exit-reason breakdown (categorize — raw reason strings embed per-trade
    # R-values/candle-counts, e.g. "Stalled (3 candles, -0.34R) — cut early",
    # which would otherwise explode into hundreds of unique "reasons")
    def categorize(reason: str) -> str:
        if reason.startswith("Stalled"):
            return "Early-cut (stalled)"
        if reason.startswith("Max holding"):
            return "Max holding candles"
        return reason

    by_reason = defaultdict(list)
    for t in all_trades:
        by_reason[categorize(t["reason"])].append(t["pnl"])

    print("\n--- Exit reason breakdown ---")
    print(f"{'Reason':<30}{'Count':<10}{'Win%':<10}{'AvgPnL':<12}{'TotalPnL':<12}")
    for reason, pnls in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        pnls_arr = np.array(pnls)
        win_pct = (pnls_arr > 0).mean() * 100
        print(f"{reason:<30}{len(pnls):<10}{win_pct:<10.1f}{pnls_arr.mean():<12.2f}{pnls_arr.sum():<12.2f}")

    print("=" * 62)


if __name__ == "__main__":
    main()
