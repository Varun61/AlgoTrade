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
import pandas as pd

from auth.session_manager import SessionManager
from data.historical_fetcher import HistoricalFetcher
from strategy.signal_engine import ORBEMAVWAPStrategy
from strategy.indicators import adx
from backtest.engine import BacktestEngine, BacktestConfig
from tools.backtest_compare import build_base_kwargs, NEW_KW, TOTAL_DAYS

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

SETTINGS_PATH = Path(__file__).parent.parent / "config" / "settings.yaml"
SUBSET_SIZE = 40  # first N watchlist symbols (large-caps) — fast + representative
ADX_PERIOD = 14


def compute_context_features(df: pd.DataFrame) -> dict:
    """
    Per-candle context features NOT already in the confidence score, keyed by
    candle timestamp, for entry-quality diagnosis:
      - adx_val         : ADX(14) at that candle (trend strength, regardless of gate)
      - prior_day_high/low : previous calendar day's high/low (no lookahead)
      - daily_trend_up  : daily EMA(3) > EMA(8) as of the prior day's close
      - entry_hour      : hour-of-day bucket
    """
    df = df.sort_values("timestamp").reset_index(drop=True)
    adx_series = adx(df["high"], df["low"], df["close"], ADX_PERIOD)

    dates = df["timestamp"].dt.date
    daily = df.groupby(dates).agg(
        daily_high=("high", "max"), daily_low=("low", "min"), daily_close=("close", "last")
    )
    daily["prior_high"] = daily["daily_high"].shift(1)
    daily["prior_low"]  = daily["daily_low"].shift(1)
    ema_fast = daily["daily_close"].ewm(span=3, adjust=False).mean()
    ema_slow = daily["daily_close"].ewm(span=8, adjust=False).mean()
    daily["trend_up"] = (ema_fast > ema_slow).shift(1)  # known as of prior day's close only

    ctx = {}
    for i, row in df.iterrows():
        d = row["timestamp"].date()
        drow = daily.loc[d]
        ctx[row["timestamp"]] = {
            "adx_val": float(adx_series.iloc[i]) if pd.notna(adx_series.iloc[i]) else None,
            "prior_high": float(drow["prior_high"]) if pd.notna(drow["prior_high"]) else None,
            "prior_low": float(drow["prior_low"]) if pd.notna(drow["prior_low"]) else None,
            "daily_trend_up": bool(drow["trend_up"]) if pd.notna(drow["trend_up"]) else None,
            "entry_hour": row["timestamp"].hour,
        }
    return ctx


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
        ctx_map = compute_context_features(df)
        engine = BacktestEngine(
            strategy_class=ORBEMAVWAPStrategy, symbol=symbol, token=token,
            strategy_kwargs={**base_kw, **NEW_KW},
            config=bt_cfg,
        )
        result = engine.run(df)
        for t in result.trades:
            ctx = ctx_map.get(pd.Timestamp(t["entry_time"])) if t.get("entry_time") is not None else None
            if ctx:
                direction = t["direction"]
                entry_price = t["entry_price"]
                if ctx["prior_high"] and ctx["prior_low"]:
                    if direction == "long":
                        ctx["prior_ext_dist_pct"] = (entry_price - ctx["prior_high"]) / ctx["prior_high"] * 100
                        ctx["trend_aligned"] = ctx["daily_trend_up"]
                    else:
                        ctx["prior_ext_dist_pct"] = (ctx["prior_low"] - entry_price) / ctx["prior_low"] * 100
                        ctx["trend_aligned"] = (ctx["daily_trend_up"] is False)
                else:
                    ctx["prior_ext_dist_pct"] = None
                    ctx["trend_aligned"] = None
            t["ctx"] = ctx
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

    # Entry-time indicator comparison — do "stalled" entries look any different
    # at entry time (confidence + its 6 sub-factors) than "target hit" entries?
    factor_keys = ["orb_breakout", "ema_trend", "vwap_position", "rsi_quality", "volume"]
    by_bucket = defaultdict(list)
    for t in all_trades:
        if t.get("confidence") is not None:
            by_bucket[categorize(t["reason"])].append(t)

    print("\n--- Entry-time indicators by outcome bucket ---")
    header = f"{'Bucket':<22}{'N':<6}{'Confidence':<12}" + "".join(f"{k:<15}" for k in factor_keys)
    print(header)
    for bucket, trs in sorted(by_bucket.items(), key=lambda kv: -len(kv[1])):
        confs = np.array([t["confidence"] for t in trs])
        row = f"{bucket:<22}{len(trs):<6}{confs.mean():<12.1f}"
        for k in factor_keys:
            vals = np.array([t["confidence_factors"].get(k, np.nan) for t in trs])
            row += f"{np.nanmean(vals):<15.1f}"
        print(row)

    print("=" * 62)

    # Context-feature comparison — ADX-at-entry, distance beyond the prior
    # day's range, daily-trend alignment, and time-of-day. None of these are
    # currently used as graded confidence factors (ADX is only a hard gate).
    by_bucket_ctx = defaultdict(list)
    for t in all_trades:
        if t.get("ctx"):
            by_bucket_ctx[categorize(t["reason"])].append(t["ctx"])

    print("\n--- Context features by outcome bucket (not in confidence score) ---")
    header2 = f"{'Bucket':<22}{'N':<6}{'ADX':<10}{'PriorExtDist%':<16}{'TrendAligned%':<16}{'AvgEntryHour':<14}"
    print(header2)
    for bucket, ctxs in sorted(by_bucket_ctx.items(), key=lambda kv: -len(kv[1])):
        adx_vals = np.array([c["adx_val"] for c in ctxs if c["adx_val"] is not None])
        dist_vals = np.array([c["prior_ext_dist_pct"] for c in ctxs if c.get("prior_ext_dist_pct") is not None])
        aligned = np.array([c["trend_aligned"] for c in ctxs if c.get("trend_aligned") is not None])
        hours = np.array([c["entry_hour"] for c in ctxs])
        adx_mean = adx_vals.mean() if len(adx_vals) else float("nan")
        dist_mean = dist_vals.mean() if len(dist_vals) else float("nan")
        aligned_pct = aligned.mean() * 100 if len(aligned) else float("nan")
        hour_mean = hours.mean() if len(hours) else float("nan")
        print(f"{bucket:<22}{len(ctxs):<6}{adx_mean:<10.1f}{dist_mean:<16.2f}{aligned_pct:<16.1f}{hour_mean:<14.1f}")

    print("=" * 62)

    # Position-sizing sanity check — is the intended 1%-of-capital risk per
    # trade actually being realized, or is max_position_value (50% of capital)
    # silently capping qty (and therefore realized risk) below the target?
    intended_risk = bt_cfg.capital * bt_cfg.per_trade_risk / 100
    max_pos_value = bt_cfg.capital * bt_cfg.max_position_value_pct / 100
    sized_trades = [t for t in all_trades if t.get("stop_loss") is not None]
    if sized_trades:
        actual_risks = np.array([t["qty"] * abs(t["entry_price"] - t["stop_loss"]) for t in sized_trades])
        pos_values   = np.array([t["qty"] * t["entry_price"] for t in sized_trades])
        capped_pct   = (pos_values >= max_pos_value * 0.98).mean() * 100
        print("\n--- Position sizing sanity check ---")
        print(f"Intended risk/trade (1% capital): Rs {intended_risk:.2f}")
        print(f"Actual avg risk/trade (qty * stop distance): Rs {actual_risks.mean():.2f}")
        print(f"Avg position value: Rs {pos_values.mean():.2f}  (cap: Rs {max_pos_value:.2f})")
        print(f"Trades capped at max_position_value: {capped_pct:.1f}%")
        print("=" * 62)


if __name__ == "__main__":
    main()
