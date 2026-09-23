"""
tools/backtest_compare.py

Fetches REAL historical 15-min candles (via Angel One SmartAPI) for the full
watchlist and runs backtest.engine.BacktestEngine with the OLD (pre-today)
strategy config vs. the NEW (today's tuned) config, per symbol, then
aggregates results — to empirically check whether today's changes
(min_confidence, max_holding_candles, early_cut_candles, min_adx,
min_ema_trend_factor, confirmation_candles) actually improve outcomes rather
than just being unit-tested-as-mechanically-correct.

Caveats (please read before trusting the numbers):
  - Each symbol is backtested independently with the FULL configured capital
    (no shared/portfolio-level capital allocation across symbols), so the
    aggregated total P&L is NOT a realistic portfolio P&L — it's a directional
    signal only. Since both OLD and NEW runs use the identical assumption,
    the *relative* comparison between them is still meaningful.
  - Does not simulate main.py-level gates that sit above the per-symbol
    strategy loop: new_entry_cutoff_time and the market_regime gate.
  - Only covers ~TOTAL_DAYS trading days — still a modest sample, not a
    substitute for weeks/months of forward paper validation.

Usage:
    python -m tools.backtest_compare
"""
from __future__ import annotations
import sys
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml

from auth.session_manager import SessionManager
from data.historical_fetcher import HistoricalFetcher
from strategy.signal_engine import ORBEMAVWAPStrategy
from backtest.engine import BacktestEngine, BacktestConfig

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

SETTINGS_PATH = Path(__file__).parent.parent / "config" / "settings.yaml"

TOTAL_DAYS = 60

# Config as it behaved BEFORE today's session (min_confidence/early_cut/etc.
# were silently unused due to the main.py wiring bug, so "old" = truly disabled).
OLD_KW = dict(
    min_confidence=70.0,
    max_holding_candles=12,
    early_cut_candles=0,
    early_cut_min_r=0.3,
    min_adx=0.0,
    min_ema_trend_factor=0.0,
    confirmation_candles=0,
)

# Config as it stands after today's changes (now actually wired in main.py).
NEW_KW = dict(
    min_confidence=75.0,
    max_holding_candles=8,
    early_cut_candles=3,
    early_cut_min_r=0.3,
    min_adx=15.0,
    min_ema_trend_factor=8.0,
    confirmation_candles=2,
)


def build_base_kwargs(strategy_cfg: dict, risk_cfg: dict, interval_min: int) -> dict:
    return dict(
        candle_minutes=interval_min,
        orb_minutes=strategy_cfg["orb_minutes"],
        ema_fast=strategy_cfg["ema_fast"],
        ema_slow=strategy_cfg["ema_slow"],
        rsi_period=strategy_cfg["rsi_period"],
        rsi_overbought=strategy_cfg["rsi_overbought"],
        rsi_oversold=strategy_cfg["rsi_oversold"],
        atr_period=strategy_cfg["atr_period"],
        atr_stop_mult=risk_cfg["atr_stop_multiplier"],
        atr_target_mult=risk_cfg["atr_target_multiplier"],
        vwap_filter=strategy_cfg["vwap_filter"],
        volume_avg_periods=strategy_cfg.get("volume_avg_periods", 20),
        breakeven_r=strategy_cfg.get("breakeven_r", 1.0),
        trail_atr_mult=strategy_cfg.get("trail_atr_mult", 1.0),
        min_atr_pct=strategy_cfg.get("min_atr_pct", 0.0),
        max_atr_pct=strategy_cfg.get("max_atr_pct", 100.0),
        adx_period=strategy_cfg.get("adx_period", 14),
    )


def aggregate(results: list) -> dict:
    total_trades = sum(r.total_trades for r in results)
    winning = sum(r.winning_trades for r in results)
    total_pnl = sum(r.total_pnl for r in results)
    win_rate = (winning / total_trades * 100) if total_trades else 0.0
    rr_vals = [r.avg_rr for r in results if r.total_trades > 0]
    avg_rr = sum(rr_vals) / len(rr_vals) if rr_vals else 0.0
    dds = [r.max_drawdown for r in results if r.total_trades > 0]
    max_dd = max(dds) if dds else 0.0
    return dict(
        total_trades=total_trades, winning=winning, total_pnl=total_pnl,
        win_rate=win_rate, avg_rr=avg_rr, max_dd=max_dd,
    )


def main() -> None:
    cfg = yaml.safe_load(open(SETTINGS_PATH))
    strategy_cfg = cfg["strategy"]
    risk_cfg = cfg["risk"]
    interval_min = cfg["trading"]["candle_interval_minutes"]
    capital = cfg["trading"]["capital"]
    watchlist = cfg["watchlist"]["instruments"]

    logger.info("Logging in to Angel One ...")
    sm = SessionManager()
    obj = sm.login()
    fetcher = HistoricalFetcher(obj)

    base_kw = build_base_kwargs(strategy_cfg, risk_cfg, interval_min)

    old_results, new_results, skipped = [], [], []

    for i, inst in enumerate(watchlist, 1):
        token = str(inst["token"])
        symbol = inst["symbol"]
        exch = inst["exchange"]

        logger.info(f"({i}/{len(watchlist)}) Fetching {symbol} ...")
        df = fetcher.fetch_in_chunks(
            exch, token, interval_min, total_days=TOTAL_DAYS, chunk_days=TOTAL_DAYS
        )
        if df.empty or len(df) < 50:
            logger.warning(f"[{symbol}] insufficient data ({len(df)} candles) — skipping")
            skipped.append(symbol)
            continue

        logger.info(f"[{symbol}] {len(df)} candles — running OLD vs NEW backtest")

        old_engine = BacktestEngine(
            strategy_class=ORBEMAVWAPStrategy, symbol=symbol, token=token,
            strategy_kwargs={**base_kw, **OLD_KW},
            config=BacktestConfig(capital=capital),
        )
        old_results.append(old_engine.run(df))

        new_engine = BacktestEngine(
            strategy_class=ORBEMAVWAPStrategy, symbol=symbol, token=token,
            strategy_kwargs={**base_kw, **NEW_KW},
            config=BacktestConfig(capital=capital),
        )
        new_results.append(new_engine.run(df))

    sm.logout()

    old_agg = aggregate(old_results)
    new_agg = aggregate(new_results)

    print("\n" + "=" * 62)
    print(f"BACKTEST COMPARISON — {len(old_results)} symbols, {TOTAL_DAYS} days, {interval_min}-min candles")
    print(f"Skipped (insufficient data): {len(skipped)}" + (f" -> {skipped[:10]}..." if len(skipped) > 10 else f" -> {skipped}"))
    print("=" * 62)
    print(f"{'Metric':<20}{'OLD config':<20}{'NEW config':<20}")
    print(f"{'Total trades':<20}{old_agg['total_trades']:<20}{new_agg['total_trades']:<20}")
    print(f"{'Winning trades':<20}{old_agg['winning']:<20}{new_agg['winning']:<20}")
    print(f"{'Win rate %':<20}{old_agg['win_rate']:<20.2f}{new_agg['win_rate']:<20.2f}")
    print(f"{'Total P&L (Rs)':<20}{old_agg['total_pnl']:<20,.2f}{new_agg['total_pnl']:<20,.2f}")
    print(f"{'Avg R:R':<20}{old_agg['avg_rr']:<20.2f}{new_agg['avg_rr']:<20.2f}")
    print(f"{'Max drawdown %':<20}{old_agg['max_dd']:<20.2f}{new_agg['max_dd']:<20.2f}")
    print("=" * 62)


if __name__ == "__main__":
    main()
