"""
backtest/portfolio.py

Offline PORTFOLIO backtester.

Unlike backtest/engine.py (which backtests one symbol at a time with the FULL
capital and therefore cannot model concurrency, ranking or rotation), this
replays the ENTIRE watchlist on a single shared timeline and reproduces the
real main.py execution model:

  - one shared capital pool + one CircuitBreaker for the whole portfolio
  - max_concurrent_positions cap
  - per-candle confidence ranking: when more entry signals fire on the same
    candle than there are free slots, only the highest-confidence ones are taken
  - optional position rotation (close the weakest open position to free a slot
    for a much higher-confidence new setup), gated by rotation_min_confidence
  - new_entry_cutoff_time and EOD square_off_time
  - realistic costs via the SAME execution/position_manager.zerodha_intraday_costs
    model used live, plus configurable entry/exit slippage
  - intrabar stop/target using each candle's high/low, then the strategy's own
    exit logic (breakeven / trail / early-cut / time-stop / EMA-flip) — the
    exact same StrategyBase code path used live and in engine.py

Data source: the cached 15-min CSVs in data/.cache/backtest/<token>_15m.csv
(one per watchlist token), so it runs with NO Angel One login.

This is a research tool, not production — kept deliberately dependency-light.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from strategy.signal_engine import ORBEMAVWAPStrategy
from strategy.strategy_base import Signal
from risk.position_sizer import PositionSizer
from risk.circuit_breaker import CircuitBreaker
from execution.position_manager import (
    zerodha_intraday_costs, zerodha_delivery_costs, find_weakest_position,
)

logger = logging.getLogger(__name__)

_CACHE_DIR = Path(__file__).parent.parent / "data" / ".cache" / "backtest"


@dataclass
class PortfolioConfig:
    capital: float = 25_000.0
    per_trade_risk_pct: float = 1.0
    max_position_value_pct: float = 50.0
    max_concurrent: int = 3
    max_trades_per_day: int | None = None
    daily_loss_limit_pct: float = 100.0      # research default: don't halt the day
    max_consecutive_losses: int = 999
    slippage_pct: float = 0.05               # applied to entry AND exit
    square_off: str = "15:10"
    new_entry_cutoff: str = "14:15"
    allow_rotation: bool = True
    rotation_min_confidence: float = 77.0
    # replay controls
    history_window: int = 160                # trailing candles fed to the strategy
    start_date: str | None = None            # 'YYYY-MM-DD' — ignore candles before this
    end_date: str | None = None
    symbol_limit: int | None = None          # cap number of symbols (None = all cached)
    # timeframe / holding style
    timeframe: str = "15m"                   # "15m" (cache native) or "day" (resampled)
    swing_mode: bool = False                 # True = hold overnight (no EOD square-off / daily reset)
    cost_model: str = "intraday"             # "intraday" or "delivery"
    # market-regime injection (equal-weight universe index vs its SMA)
    regime_enabled: bool = False
    regime_sma_days: int = 20
    # data source
    cache_dir: str | None = None      # override the default 15m cache dir
    cache_suffix: str = "15m"         # file suffix: <token>_<suffix>.csv


@dataclass
class PortfolioResult:
    trades: list[dict] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    start_capital: float = 0.0

    # ---- metrics ----
    @property
    def total_trades(self) -> int:
        return len(self.trades)

    @property
    def total_pnl(self) -> float:
        return round(sum(t["pnl"] for t in self.trades), 2)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        return round(sum(1 for t in self.trades if t["pnl"] > 0) / len(self.trades) * 100, 1)

    @property
    def avg_win(self) -> float:
        w = [t["pnl"] for t in self.trades if t["pnl"] > 0]
        return round(float(np.mean(w)), 2) if w else 0.0

    @property
    def avg_loss(self) -> float:
        l = [t["pnl"] for t in self.trades if t["pnl"] <= 0]
        return round(float(np.mean(l)), 2) if l else 0.0

    @property
    def profit_factor(self) -> float:
        gains = sum(t["pnl"] for t in self.trades if t["pnl"] > 0)
        losses = -sum(t["pnl"] for t in self.trades if t["pnl"] < 0)
        return round(gains / losses, 2) if losses else float("inf")

    @property
    def expectancy(self) -> float:
        return round(self.total_pnl / self.total_trades, 2) if self.trades else 0.0

    @property
    def return_pct(self) -> float:
        return round(self.total_pnl / self.start_capital * 100, 2) if self.start_capital else 0.0

    @property
    def max_drawdown_pct(self) -> float:
        if len(self.equity_curve) < 2:
            return 0.0
        eq = np.array(self.equity_curve)
        peak = np.maximum.accumulate(eq)
        dd = (eq - peak) / peak * 100
        return round(float(abs(dd.min())), 2)

    @property
    def sharpe(self) -> float:
        pnls = np.array([t["pnl"] for t in self.trades], dtype=float)
        if len(pnls) < 2 or pnls.std() == 0:
            return 0.0
        return round(float(pnls.mean() / pnls.std() * np.sqrt(252)), 2)


class PortfolioBacktester:
    def __init__(self, watchlist: list[dict], strategy_kwargs: dict,
                 config: PortfolioConfig | None = None,
                 strategy_class=ORBEMAVWAPStrategy) -> None:
        self.watchlist = watchlist
        self.strategy_kwargs = strategy_kwargs
        self.cfg = config or PortfolioConfig()
        self.strategy_class = strategy_class

    # ------------------------------------------------------------------
    def _load(self) -> dict[str, pd.DataFrame]:
        """token -> full OHLCV DataFrame (with a datetime `timestamp` column)."""
        data: dict[str, pd.DataFrame] = {}
        insts = self.watchlist
        if self.cfg.symbol_limit:
            insts = insts[: self.cfg.symbol_limit]
        cache_dir = Path(self.cfg.cache_dir) if self.cfg.cache_dir else _CACHE_DIR
        for inst in insts:
            token = str(inst["token"])
            path = cache_dir / f"{token}_{self.cfg.cache_suffix}.csv"
            if not path.exists():
                continue
            df = pd.read_csv(path)
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            # Newly fetched files carry a tz (+05:30); the old cache is tz-naive.
            # Normalize to tz-naive so all downstream date comparisons/resampling
            # work uniformly.
            if getattr(df["timestamp"].dt, "tz", None) is not None:
                df["timestamp"] = df["timestamp"].dt.tz_localize(None)
            if self.cfg.timeframe == "day":
                df = (df.set_index("timestamp")
                        .resample("1D")
                        .agg({"open": "first", "high": "max", "low": "min",
                              "close": "last", "volume": "sum"})
                        .dropna()
                        .reset_index())
            if self.cfg.start_date:
                df = df[df["timestamp"] >= pd.Timestamp(self.cfg.start_date)]
            if self.cfg.end_date:
                df = df[df["timestamp"] <= pd.Timestamp(self.cfg.end_date)]
            df = df.sort_values("timestamp").reset_index(drop=True)
            if len(df) > 30:
                data[token] = df
        return data

    @staticmethod
    def _hm(s: str) -> tuple[int, int]:
        h, m = s.split(":")
        return int(h), int(m)

    def _compute_regime(self, data: dict[str, pd.DataFrame]) -> dict:
        """
        Equal-weight universe index -> daily regime, keyed by date.
        regime[d] = +1 if the index (as of the PRIOR trading day's close, so it's
        known at day d's open — no lookahead) is above its SMA(regime_sma_days),
        else -1. Empty dict if disabled.
        """
        if not self.cfg.regime_enabled:
            return {}
        # daily last-close per symbol, normalized to its own first value (equal weight)
        norm_closes = []
        for df in data.values():
            dd = df[["timestamp", "close"]].copy()
            dd["d"] = dd["timestamp"].dt.date
            dc = dd.groupby("d")["close"].last()
            if len(dc) < 2 or dc.iloc[0] <= 0:
                continue
            norm_closes.append(dc / dc.iloc[0])
        if not norm_closes:
            return {}
        idx = pd.concat(norm_closes, axis=1).mean(axis=1).sort_index()
        sma = idx.rolling(self.cfg.regime_sma_days, min_periods=1).mean()
        above = (idx > sma).shift(1)   # prior day's verdict -> known at today's open
        regime = {}
        for d, v in above.items():
            regime[d] = 1 if bool(v) else -1
        return regime

    def run(self) -> PortfolioResult:
        cfg = self.cfg
        data = self._load()
        token_symbol = {str(i["token"]): i["symbol"] for i in self.watchlist}
        token_exch = {str(i["token"]): i["exchange"] for i in self.watchlist}

        strategies = {
            tok: self.strategy_class(symbol=token_symbol.get(tok, tok), token=tok,
                                     **self.strategy_kwargs)
            for tok in data
        }

        # Unified, sorted list of all (timestamp, token) candle events.
        events: dict[pd.Timestamp, list[str]] = {}
        row_index: dict[tuple[str, pd.Timestamp], int] = {}
        for tok, df in data.items():
            for pos, ts in enumerate(df["timestamp"]):
                events.setdefault(ts, []).append(tok)
                row_index[(tok, ts)] = pos
        timeline = sorted(events.keys())

        sizer = PositionSizer(
            capital=cfg.capital, per_trade_risk_pct=cfg.per_trade_risk_pct,
            max_position_value=cfg.capital * cfg.max_position_value_pct / 100,
        )
        breaker = CircuitBreaker(
            capital=cfg.capital, daily_loss_limit_pct=cfg.daily_loss_limit_pct,
            max_trades_per_day=cfg.max_trades_per_day, max_concurrent=cfg.max_concurrent,
            max_consecutive_losses=cfg.max_consecutive_losses,
        )

        capital = cfg.capital
        open_pos: dict[str, dict] = {}       # token -> position dict
        trades: list[dict] = []
        equity = [capital]
        current_day = None
        sq_h, sq_m = self._hm(cfg.square_off)
        cut_h, cut_m = self._hm(cfg.new_entry_cutoff)
        win = cfg.history_window

        def close_trade(tok, exit_price, reason, ts, slip=True):
            nonlocal capital
            pos = open_pos.pop(tok)
            px = exit_price
            if slip:
                # exit slippage always hurts: sells fill lower, covers fill higher
                px = px * (1 - cfg.slippage_pct / 100) if pos["direction"] == "long" \
                    else px * (1 + cfg.slippage_pct / 100)
            qty = pos["qty"]
            if pos["direction"] == "long":
                gross = (px - pos["entry_price"]) * qty
                buy_val, sell_val = pos["entry_price"] * qty, px * qty
            else:
                gross = (pos["entry_price"] - px) * qty
                buy_val, sell_val = px * qty, pos["entry_price"] * qty
            if cfg.cost_model == "none":
                costs = 0.0
            else:
                cost_fn = zerodha_delivery_costs if cfg.cost_model == "delivery" else zerodha_intraday_costs
                costs = cost_fn(buy_val, sell_val).total
            pnl = gross - costs
            capital += pnl
            breaker.on_trade_close(pnl)
            sizer.update_capital(capital)
            strategies[tok].force_exit()
            trades.append({
                "symbol": pos["symbol"], "token": tok, "direction": pos["direction"],
                "entry_time": pos["entry_time"], "exit_time": ts,
                "entry_price": round(pos["entry_price"], 2), "exit_price": round(px, 2),
                "qty": qty, "gross_pnl": round(gross, 2), "costs": round(costs, 2),
                "pnl": round(pnl, 2), "reason": reason, "win": pnl > 0,
                "confidence": pos.get("confidence"), "hold_candles": pos.get("hold_candles", 0),
            })
            equity.append(capital)

        regime_by_day = self._compute_regime(data)
        swing = cfg.swing_mode
        for ts in timeline:
            day = ts.date()
            if day != current_day:
                if swing:
                    # Positional: never reset strategy/position state per day
                    # (trades are held overnight). Keep sizing capital current.
                    sizer.update_capital(capital)
                else:
                    # Intraday: fresh session each day (any position left open is
                    # squared off by the EOD block on the prior day first).
                    for s in strategies.values():
                        s.reset()
                    breaker.reset_session(new_capital=capital)
                    sizer.update_capital(capital)
                # Inject today's market regime AFTER reset so it isn't wiped.
                if regime_by_day:
                    reg = regime_by_day.get(day, 0)
                    for s in strategies.values():
                        s.set_market_regime(reg)
                current_day = day

            after_cutoff = (not swing) and (ts.hour, ts.minute) >= (cut_h, cut_m)
            at_squareoff = (not swing) and (ts.hour, ts.minute) >= (sq_h, sq_m)

            toks = events[ts]

            # ---- 1. EOD square-off (intraday only) ----
            if at_squareoff:
                for tok in list(open_pos.keys()):
                    df = data[tok]
                    pos_i = row_index.get((tok, ts))
                    px = float(df["close"].iloc[pos_i]) if pos_i is not None else open_pos[tok]["entry_price"]
                    close_trade(tok, px, "EOD square-off", ts)
                continue

            # ---- 2. Manage open positions (exits) first, to free slots ----
            for tok in toks:
                if tok not in open_pos:
                    continue
                strat = strategies[tok]
                df = data[tok]
                i = row_index[(tok, ts)]
                candle = df.iloc[i]
                hi, lo, cl = float(candle["high"]), float(candle["low"]), float(candle["close"])
                pos = open_pos[tok]
                pos["hold_candles"] = pos.get("hold_candles", 0) + 1
                stop = strat.get_current_stop()
                target = strat.get_current_target()
                # intrabar hard stop / target (wick-through), stop checked first
                if pos["direction"] == "long":
                    if stop is not None and lo <= stop:
                        close_trade(tok, stop, "Stop hit", ts); continue
                    if target is not None and hi >= target:
                        close_trade(tok, target, "Target hit", ts); continue
                else:
                    if stop is not None and hi >= stop:
                        close_trade(tok, stop, "Stop hit", ts); continue
                    if target is not None and lo <= target:
                        close_trade(tok, target, "Target hit", ts); continue
                # no hard hit → let the strategy decide (trail/early-cut/time/EMA flip)
                hist = df.iloc[max(0, i - win): i + 1]
                sig = strat.on_candle_close(candle, hist)
                if sig.is_exit():
                    close_trade(tok, cl, sig.reason, ts)

            # ---- 3. Collect entry candidates (flat symbols) ----
            candidates = []
            if not after_cutoff:
                for tok in toks:
                    if tok in open_pos:
                        continue
                    strat = strategies[tok]
                    df = data[tok]
                    i = row_index[(tok, ts)]
                    candle = df.iloc[i]
                    hist = df.iloc[max(0, i - win): i + 1]
                    sig = strat.on_candle_close(candle, hist)
                    if sig.is_entry():
                        candidates.append((tok, sig))

            # ---- 4. Rank by confidence, fill slots (+ optional rotation) ----
            candidates.sort(key=lambda ts_: ts_[1].confidence, reverse=True)
            for tok, sig in candidates:
                can, reason = breaker.can_trade()
                took = False
                if can and len(open_pos) < cfg.max_concurrent:
                    took = self._open(tok, sig, ts, cfg, sizer, breaker, open_pos,
                                      strategies, data, row_index, token_symbol)
                elif (cfg.allow_rotation and "concurrent" in reason.lower()
                      and sig.confidence >= cfg.rotation_min_confidence):
                    weakest = self._weakest(open_pos, strategies, data, row_index, ts)
                    if weakest and weakest != tok:
                        wdf = data[weakest]
                        wi = row_index.get((weakest, ts))
                        wpx = float(wdf["close"].iloc[wi]) if wi is not None else open_pos[weakest]["entry_price"]
                        close_trade(weakest, wpx, "Rotated out for higher-confidence setup", ts)
                        if breaker.can_trade()[0]:
                            took = self._open(tok, sig, ts, cfg, sizer, breaker, open_pos,
                                              strategies, data, row_index, token_symbol)
                if not took:
                    strategies[tok].discard_pending_entry()

        # Close anything still open at the very end of data
        last_ts = timeline[-1] if timeline else None
        for tok in list(open_pos.keys()):
            df = data[tok]
            close_trade(tok, float(df["close"].iloc[-1]), "End of data", last_ts)

        res = PortfolioResult(trades=trades, equity_curve=equity, start_capital=cfg.capital)
        return res

    # ------------------------------------------------------------------
    def _open(self, tok, sig, ts, cfg, sizer, breaker, open_pos, strategies,
              data, row_index, token_symbol) -> bool:
        # entry slippage hurts: buys fill higher, sells fill lower
        entry = sig.entry_price
        entry = entry * (1 + cfg.slippage_pct / 100) if sig.signal == Signal.BUY \
            else entry * (1 - cfg.slippage_pct / 100)
        qty = sizer.compute_qty(entry, sig.stop_loss)
        if qty <= 0:
            return False
        open_pos[tok] = {
            "symbol": token_symbol.get(tok, tok), "token": tok,
            "direction": "long" if sig.signal == Signal.BUY else "short",
            "qty": qty, "entry_price": entry, "stop_loss": sig.stop_loss,
            "target": sig.target, "entry_time": ts, "confidence": sig.confidence,
            "hold_candles": 0,
        }
        breaker.on_trade_open()
        return True

    def _weakest(self, open_pos, strategies, data, row_index, ts) -> str | None:
        ltps = {}
        for tok in open_pos:
            i = row_index.get((tok, ts))
            ltps[tok] = float(data[tok]["close"].iloc[i]) if i is not None else open_pos[tok]["entry_price"]
        live_stops = {tok: strategies[tok].get_current_stop() for tok in open_pos
                      if strategies[tok].get_current_stop() is not None}
        return find_weakest_position(list(open_pos.values()), ltps, live_stops)
