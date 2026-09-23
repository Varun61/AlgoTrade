"""
tools/fit_outcome_model.py

Quick, honest check on whether ANY combination of currently-available
per-trade features (confidence sub-factors + context features gathered in
tools/backtest_diagnose.py) has real predictive power over trade outcome —
rather than continuing to hand-tune heuristic gates/weights on intuition.

Fits a simple logistic regression (win/loss) with a train/test split and
reports per-feature correlation with outcome, model coefficients, and
out-of-sample AUC. An AUC close to 0.5 means the features carry ~no signal;
meaningfully above 0.5 (e.g. >0.60) means there's something real to exploit.

Usage:
    python -m tools.fit_outcome_model
"""
from __future__ import annotations
import sys
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split
from sklearn.metrics import roc_auc_score
from sklearn.preprocessing import StandardScaler

from auth.session_manager import SessionManager
from data.historical_fetcher import HistoricalFetcher
from strategy.signal_engine import ORBEMAVWAPStrategy
from backtest.engine import BacktestEngine, BacktestConfig
from tools.backtest_compare import build_base_kwargs, NEW_KW, TOTAL_DAYS
from tools.backtest_diagnose import compute_context_features

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")

SETTINGS_PATH = Path(__file__).parent.parent / "config" / "settings.yaml"
SUBSET_SIZE = 40

FEATURE_KEYS = [
    "confidence", "orb_breakout", "ema_trend", "vwap_position", "rsi_quality", "volume",
    "adx_val", "prior_ext_dist_pct", "trend_aligned", "entry_hour",
]


def main() -> None:
    cfg = yaml.safe_load(open(SETTINGS_PATH))
    strategy_cfg = cfg["strategy"]
    risk_cfg = cfg["risk"]
    interval_min = cfg["trading"]["candle_interval_minutes"]
    capital = cfg["trading"]["capital"]
    watchlist = cfg["watchlist"]["instruments"][:SUBSET_SIZE]

    print(f"Logging in to Angel One ... (subset: {len(watchlist)} symbols)")
    sm = SessionManager()
    obj = sm.login()
    fetcher = HistoricalFetcher(obj)

    base_kw = build_base_kwargs(strategy_cfg, risk_cfg, interval_min)
    # Disable the confidence gate for THIS diagnostic run only (not settings.yaml/live) —
    # confidence has shown no correlation with outcome, so filtering by it would only
    # shrink the sample arbitrarily, not select "better" trades. We want every setup
    # that clears the actual hard boolean gates, then let the regression judge honestly
    # whether confidence (or anything else) has real signal.
    diag_kw = {**base_kw, **NEW_KW, "min_confidence": 0.0}

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
            strategy_kwargs=diag_kw,
            config=BacktestConfig(capital=capital),
        )
        result = engine.run(df)
        for t in result.trades:
            ctx = ctx_map.get(pd.Timestamp(t["entry_time"])) if t.get("entry_time") is not None else None
            if ctx and ctx.get("prior_high") and ctx.get("prior_low"):
                direction = t["direction"]
                entry_price = t["entry_price"]
                if direction == "long":
                    ctx["prior_ext_dist_pct"] = (entry_price - ctx["prior_high"]) / ctx["prior_high"] * 100
                    ctx["trend_aligned"] = ctx["daily_trend_up"]
                else:
                    ctx["prior_ext_dist_pct"] = (ctx["prior_low"] - entry_price) / ctx["prior_low"] * 100
                    ctx["trend_aligned"] = (ctx["daily_trend_up"] is False)
            t["ctx"] = ctx
        all_trades.extend(result.trades)

    sm.logout()

    # Build a flat feature dataframe
    rows = []
    for t in all_trades:
        if t.get("confidence") is None or not t.get("ctx"):
            continue
        row = {"confidence": t["confidence"], **t["confidence_factors"]}
        ctx = t["ctx"]
        row["adx_val"] = ctx.get("adx_val")
        row["prior_ext_dist_pct"] = ctx.get("prior_ext_dist_pct")
        row["trend_aligned"] = 1.0 if ctx.get("trend_aligned") else 0.0
        row["entry_hour"] = ctx.get("entry_hour")
        row["win"] = 1 if t["pnl"] > 0 else 0
        rows.append(row)

    df = pd.DataFrame(rows).dropna()
    print(f"\n{len(df)} labeled trades with complete features (of {len(all_trades)} total)")
    print(f"Base win rate: {df['win'].mean() * 100:.1f}%\n")

    print("--- Per-feature correlation with outcome (win=1/loss=0) ---")
    corrs = df[FEATURE_KEYS + ["win"]].corr()["win"].drop("win").sort_values(key=abs, ascending=False)
    for feat, corr in corrs.items():
        print(f"{feat:<22}{corr:+.3f}")

    X = df[FEATURE_KEYS].values
    y = df["win"].values

    if len(df) < 30 or df["win"].nunique() < 2:
        print("\nNot enough labeled data / outcome variance to fit a model — stopping here.")
        return

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.3, random_state=42, stratify=y
    )
    scaler = StandardScaler().fit(X_train)
    X_train_s, X_test_s = scaler.transform(X_train), scaler.transform(X_test)

    model = LogisticRegression(max_iter=1000)
    model.fit(X_train_s, y_train)

    train_auc = roc_auc_score(y_train, model.predict_proba(X_train_s)[:, 1])
    test_auc = roc_auc_score(y_test, model.predict_proba(X_test_s)[:, 1])

    print(f"\n--- Logistic regression fit ({len(y_train)} train / {len(y_test)} test) ---")
    print(f"Train AUC: {train_auc:.3f}   Test AUC: {test_auc:.3f}   (0.5 = no signal, 1.0 = perfect)")
    print("\nStandardized coefficients (magnitude = relative importance):")
    for feat, coef in sorted(zip(FEATURE_KEYS, model.coef_[0]), key=lambda kv: -abs(kv[1])):
        print(f"{feat:<22}{coef:+.3f}")


if __name__ == "__main__":
    main()
