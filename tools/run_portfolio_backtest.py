"""
tools/run_portfolio_backtest.py

Runs backtest/portfolio.PortfolioBacktester over the cached 15-min data using
the CURRENT config in settings.yaml, and supports named experiment overrides
so strategy/exit/risk changes can be A/B'd before touching the live config.

Usage:
    python -m tools.run_portfolio_backtest                 # baseline (current config)
    python -m tools.run_portfolio_backtest --experiments   # run the A/B matrix
    python -m tools.run_portfolio_backtest --symbols 60 --start 2026-06-01
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml

from backtest.portfolio import PortfolioBacktester, PortfolioConfig

logging.basicConfig(level=logging.ERROR, format="%(asctime)s %(levelname)s %(message)s")
SETTINGS = Path(__file__).parent.parent / "config" / "settings.yaml"


def base_kwargs(cfg: dict) -> dict:
    s, r, t = cfg["strategy"], cfg["risk"], cfg["trading"]
    return dict(
        candle_minutes=t["candle_interval_minutes"],
        orb_minutes=s["orb_minutes"], ema_fast=s["ema_fast"], ema_slow=s["ema_slow"],
        rsi_period=s["rsi_period"], rsi_overbought=s["rsi_overbought"], rsi_oversold=s["rsi_oversold"],
        atr_period=s["atr_period"], atr_stop_mult=r["atr_stop_multiplier"],
        atr_target_mult=r["atr_target_multiplier"], vwap_filter=s["vwap_filter"],
        min_confidence=s.get("min_confidence", 70.0),
        volume_avg_periods=s.get("volume_avg_periods", 20),
        breakeven_r=s.get("breakeven_r", 1.0), trail_atr_mult=s.get("trail_atr_mult", 1.0),
        max_holding_candles=s.get("max_holding_candles", 0),
        early_cut_candles=s.get("early_cut_candles", 0), early_cut_min_r=s.get("early_cut_min_r", 0.3),
        min_atr_pct=s.get("min_atr_pct", 0.0), max_atr_pct=s.get("max_atr_pct", 100.0),
        adx_period=s.get("adx_period", 14), min_adx=s.get("min_adx", 0.0),
        min_ema_trend_factor=s.get("min_ema_trend_factor", 0.0),
        confirmation_candles=s.get("confirmation_candles", 0),
        min_entry_time=s.get("min_entry_time"),
        target_lock_pct=s.get("target_lock_pct") or 0.0,
        target_lock_giveback_pct=s.get("target_lock_giveback_pct") or 0.7,
        sudden_move_atr_mult=s.get("sudden_move_atr_mult") or 0.0,
        orb_range_min_pct=s.get("orb_range_min_pct") or 0.0,
        orb_range_max_pct=s.get("orb_range_max_pct") or 100.0,
        ema_exit=s.get("ema_exit", True),
    )


def run_one(name, base_kw, kw_over, pcfg, pcfg_over=None) -> None:
    import copy
    kw = {**base_kw, **kw_over}
    if pcfg_over:
        pcfg = copy.copy(pcfg)
        for k, v in pcfg_over.items():
            setattr(pcfg, k, v)
    cfg = yaml.safe_load(open(SETTINGS))
    bt = PortfolioBacktester(cfg["watchlist"]["instruments"], kw, pcfg)
    r = bt.run()
    print(f"{name:<26} trades={r.total_trades:<5} win%={r.win_rate:<5} "
          f"PF={r.profit_factor:<5} exp=Rs{r.expectancy:<8} "
          f"P&L=Rs{r.total_pnl:<11,.0f} ret={r.return_pct:>6}% "
          f"maxDD={r.max_drawdown_pct:>5}% avgW={r.avg_win:<7} avgL={r.avg_loss:<7}")
    return r


def exit_breakdown(r) -> None:
    from collections import defaultdict
    import numpy as np
    def cat(reason):
        rl = reason.lower()
        if "stalled" in rl: return "early_cut"
        if "stop hit" in rl: return "stop_loss"
        if "target" in rl: return "target"
        if "max holding" in rl: return "time_stop"
        if "ema" in rl: return "ema_flip"
        if "rotated" in rl: return "rotation"
        if "eod" in rl: return "eod_squareoff"
        if "end of data" in rl: return "end_of_data"
        return "other"
    by = defaultdict(list)
    for t in r.trades:
        by[cat(t["reason"])].append(t["pnl"])
    print(f"\n  {'exit reason':<16}{'n':<6}{'win%':<7}{'totPnL':<12}{'avgPnL':<10}")
    for k, v in sorted(by.items(), key=lambda kv: -sum(kv[1])):
        a = np.array(v)
        print(f"  {k:<16}{len(a):<6}{(a>0).mean()*100:<7.0f}{a.sum():<12.0f}{a.mean():<10.1f}")
    longs = [t for t in r.trades if t["direction"] == "long"]
    shorts = [t for t in r.trades if t["direction"] == "short"]
    print(f"  longs={len(longs)} (P&L {sum(t['pnl'] for t in longs):+,.0f})  "
          f"shorts={len(shorts)} (P&L {sum(t['pnl'] for t in shorts):+,.0f})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", type=int, default=None)
    ap.add_argument("--start", type=str, default=None)
    ap.add_argument("--end", type=str, default=None)
    ap.add_argument("--window", type=int, default=160)
    ap.add_argument("--experiments", action="store_true")
    args = ap.parse_args()

    cfg = yaml.safe_load(open(SETTINGS))
    r, t = cfg["risk"], cfg["trading"]
    base_kw = base_kwargs(cfg)

    pcfg = PortfolioConfig(
        capital=float(t["capital"]),
        per_trade_risk_pct=float(r["per_trade_risk_pct"]),
        max_position_value_pct=float(r.get("max_position_value_pct", 50.0)),
        max_concurrent=int(t["max_concurrent_positions"]),
        max_trades_per_day=t.get("max_trades_per_day"),
        square_off=t["square_off_time"], new_entry_cutoff=t.get("new_entry_cutoff_time", "14:15"),
        allow_rotation=bool(t.get("allow_position_rotation", False)),
        rotation_min_confidence=float(t.get("rotation_min_confidence", 85)),
        history_window=args.window, start_date=args.start, end_date=args.end,
        symbol_limit=args.symbols,
    )

    print(f"\nData: symbols={args.symbols or 'all'} start={args.start or 'cache-min'} "
          f"end={args.end or 'cache-max'} | capital=Rs{pcfg.capital:,.0f} "
          f"risk={pcfg.per_trade_risk_pct}% maxConc={pcfg.max_concurrent}\n")

    baseline = run_one("BASELINE (current cfg)", base_kw, {}, pcfg)
    exit_breakdown(baseline)

    if args.experiments:
        # Common "clean exits" base: no early-cut, no discretionary EMA-flip exit.
        clean = {"early_cut_candles": 0, "ema_exit": False}
        print("\n=== A/B: let winners run (loosen trail / breakeven / time-stop) ===")
        run_one("clean_only", base_kw, clean, pcfg)
        run_one("clean_trail2", base_kw, {**clean, "trail_atr_mult": 2.0}, pcfg)
        run_one("clean_trail3", base_kw, {**clean, "trail_atr_mult": 3.0}, pcfg)
        run_one("clean_be2_trail3", base_kw, {**clean, "breakeven_r": 2.0, "trail_atr_mult": 3.0}, pcfg)
        run_one("clean_notime_trail3", base_kw, {**clean, "max_holding_candles": 0, "trail_atr_mult": 3.0}, pcfg)
        run_one("clean_pure_stop_target", base_kw,
                {**clean, "breakeven_r": 999, "trail_atr_mult": 999, "max_holding_candles": 0}, pcfg)
        print("\n=== A/B: selectivity + concurrency (with clean_trail3) ===")
        ct3 = {**clean, "trail_atr_mult": 3.0}
        run_one("ct3_conf75", base_kw, {**ct3, "min_confidence": 75.0}, pcfg)
        run_one("ct3_conf75_maxc3", base_kw, {**ct3, "min_confidence": 75.0}, pcfg, {"max_concurrent": 3})
        run_one("ct3_conf80_maxc3", base_kw, {**ct3, "min_confidence": 80.0}, pcfg, {"max_concurrent": 3})
        run_one("ct3_conf75_maxc3_2R", base_kw, {**ct3, "min_confidence": 75.0, "atr_target_mult": 2.0}, pcfg, {"max_concurrent": 3})


if __name__ == "__main__":
    main()
