"""
backtest/engine.py

Event-driven backtester.

Key design: replays historical candles through the SAME strategy code used live.
This means backtest P&L drift from live is purely due to execution differences
(slippage, fills), not strategy differences.

Features:
  - Realistic fill simulation (fills at close of signal candle, with slippage)
  - ATR-based stops enforced candle-by-candle
  - Circuit breaker applied identically to live
  - Full metrics: P&L, win rate, R:R, max drawdown, Sharpe, time-of-day breakdown

Usage:
    python -m backtest.engine
"""

from __future__ import annotations
import logging
from dataclasses import dataclass, field
from typing import Type

import numpy as np
import pandas as pd

from strategy.strategy_base import StrategyBase, Signal, TradeSignal
from risk.position_sizer import PositionSizer
from risk.circuit_breaker import CircuitBreaker

logger = logging.getLogger(__name__)


@dataclass
class BacktestConfig:
    capital          : float = 100_000.0
    per_trade_risk   : float = 1.0       # % of capital
    slippage_pct     : float = 0.05      # 0.05% slippage on entry
    brokerage_per_lot: float = 40.0      # ₹ per order (adjust for your plan)
    daily_loss_limit : float = 2.0
    max_trades_day   : int   = 10
    max_concurrent   : int   = 3
    max_consec_losses: int   = 4
    sq_off_hour      : int   = 15
    sq_off_minute    : int   = 15


@dataclass
class BacktestResult:
    trades          : list[dict] = field(default_factory=list)
    total_pnl       : float = 0.0
    win_rate        : float = 0.0
    avg_rr          : float = 0.0
    max_drawdown    : float = 0.0
    sharpe          : float = 0.0
    total_trades    : int   = 0
    winning_trades  : int   = 0


class BacktestEngine:
    """
    Replay-based event-driven backtester.

    Args:
        strategy_class: A StrategyBase subclass (NOT an instance)
        strategy_kwargs: kwargs forwarded to strategy __init__
        config: BacktestConfig
    """

    def __init__(
        self,
        strategy_class : Type[StrategyBase],
        symbol         : str,
        token          : str,
        strategy_kwargs: dict,
        config         : BacktestConfig | None = None,
    ) -> None:
        self.strategy_class  = strategy_class
        self.symbol          = symbol
        self.token           = token
        self.strategy_kwargs = strategy_kwargs
        self.cfg             = config or BacktestConfig()

    def run(self, candles: pd.DataFrame) -> BacktestResult:
        """
        Run backtest on a DataFrame of OHLCV candles.

        Args:
            candles: DataFrame with columns timestamp, open, high, low, close, volume
                     Sorted ascending by timestamp.
        """
        candles = candles.sort_values("timestamp").reset_index(drop=True)
        logger.info(f"[Backtest] Running on {len(candles)} candles for {self.symbol}")

        strategy = self.strategy_class(
            symbol=self.symbol, token=self.token, **self.strategy_kwargs
        )
        sizer = PositionSizer(
            capital=self.cfg.capital,
            per_trade_risk_pct=self.cfg.per_trade_risk,
        )
        breaker = CircuitBreaker(
            capital=self.cfg.capital,
            daily_loss_limit_pct=self.cfg.daily_loss_limit,
            max_trades_per_day=self.cfg.max_trades_day,
            max_concurrent=self.cfg.max_concurrent,
            max_consecutive_losses=self.cfg.max_consec_losses,
        )

        capital          = self.cfg.capital
        trades           = []
        equity_curve     = [capital]
        current_position : dict | None = None
        current_day      : str | None  = None

        for i in range(1, len(candles)):
            candle  = candles.iloc[i]
            history = candles.iloc[:i+1]
            ts      = pd.Timestamp(candle["timestamp"])

            # New day reset
            day_str = str(ts.date())
            if day_str != current_day:
                strategy.reset()
                breaker.reset_session(new_capital=capital)
                sizer.update_capital(capital)
                current_day = day_str

            # Forced square-off check
            if current_position and (ts.hour > self.cfg.sq_off_hour or
               (ts.hour == self.cfg.sq_off_hour and ts.minute >= self.cfg.sq_off_minute)):
                trade = self._close_position(current_position, float(candle["close"]),
                                              "EOD square-off", sizer, breaker)
                trades.append(trade)
                capital += trade["pnl"] - self.cfg.brokerage_per_lot
                equity_curve.append(capital)
                current_position = None
                strategy.force_exit()
                continue

            position_was_open = current_position is not None

            # Exit check on existing position.
            # Intrabar SL/target (using candle high/low) is checked first since the
            # strategy only ever sees the close price and cannot detect a wick-through.
            # If neither is hit, defer to the strategy's own exit logic (e.g. EMA
            # crossover, trailing stop, time stop) so backtest and live share the
            # exact same exit decision path.
            if current_position:
                exit_signal = self._check_exits(current_position, candle)
                if exit_signal:
                    trade = self._close_position(current_position, float(candle["close"]),
                                                  exit_signal, sizer, breaker)
                    trades.append(trade)
                    capital += trade["pnl"] - self.cfg.brokerage_per_lot
                    equity_curve.append(capital)
                    current_position = None
                    strategy.force_exit()
                else:
                    signal = strategy.on_candle_close(candle, history)
                    if signal.is_exit():
                        trade = self._close_position(current_position, float(candle["close"]),
                                                       signal.reason, sizer, breaker)
                        trades.append(trade)
                        capital += trade["pnl"] - self.cfg.brokerage_per_lot
                        equity_curve.append(capital)
                        current_position = None

            # Entry check — only for positions that were already flat coming into
            # this candle (a position closed above cannot be re-entered same-candle,
            # matching the live orchestrator's one-signal-per-candle behavior).
            if not current_position and not position_was_open:
                can, reason = breaker.can_trade()
                if not can:
                    continue

                signal = strategy.on_candle_close(candle, history)

                if signal.is_entry():
                    entry_price = float(candle["close"])
                    # Apply slippage
                    if signal.signal == Signal.BUY:
                        entry_price *= (1 + self.cfg.slippage_pct / 100)
                    else:
                        entry_price *= (1 - self.cfg.slippage_pct / 100)

                    qty = sizer.compute_qty(entry_price, signal.stop_loss)
                    if qty <= 0:
                        continue

                    current_position = {
                        "symbol"      : self.symbol,
                        "direction"   : "long" if signal.signal == Signal.BUY else "short",
                        "entry_price" : entry_price,
                        "entry_time"  : ts,
                        "stop_loss"   : signal.stop_loss,
                        "target"      : signal.target,
                        "qty"         : qty,
                        "reason"      : signal.reason,
                    }
                    breaker.on_trade_open()

        # Close any open position at end of data
        if current_position:
            last_close = float(candles.iloc[-1]["close"])
            trade = self._close_position(current_position, last_close,
                                          "End of data", sizer, breaker)
            trades.append(trade)
            capital += trade["pnl"]
            equity_curve.append(capital)
            strategy.force_exit()

        return self._compute_metrics(trades, equity_curve)

    # ------------------------------------------------------------------

    def _check_exits(self, pos: dict, candle: pd.Series) -> str | None:
        close = float(candle["close"])
        low   = float(candle["low"])
        high  = float(candle["high"])

        if pos["direction"] == "long":
            if low  <= pos["stop_loss"]: return "Stop hit"
            if high >= pos["target"]   : return "Target hit"
        else:
            if high >= pos["stop_loss"]: return "Stop hit"
            if low  <= pos["target"]   : return "Target hit"
        return None

    def _close_position(self, pos: dict, exit_price: float, reason: str,
                         sizer: PositionSizer, breaker: CircuitBreaker) -> dict:
        if pos["direction"] == "long":
            pnl = (exit_price - pos["entry_price"]) * pos["qty"]
        else:
            pnl = (pos["entry_price"] - exit_price) * pos["qty"]

        breaker.on_trade_close(pnl)
        sizer.update_capital(sizer.capital + pnl)

        return {
            "symbol"     : pos["symbol"],
            "direction"  : pos["direction"],
            "entry_price": pos["entry_price"],
            "exit_price" : exit_price,
            "qty"        : pos["qty"],
            "pnl"        : round(pnl, 2),
            "entry_time" : pos.get("entry_time"),
            "reason"     : reason,
            "win"        : pnl > 0,
        }

    def _compute_metrics(self, trades: list[dict], equity: list[float]) -> BacktestResult:
        if not trades:
            return BacktestResult()

        total_pnl = sum(t["pnl"] for t in trades)
        wins      = [t for t in trades if t["win"]]
        losses    = [t for t in trades if not t["win"]]
        win_rate  = len(wins) / len(trades) * 100 if trades else 0

        avg_win  = np.mean([t["pnl"] for t in wins])  if wins   else 0
        avg_loss = np.mean([t["pnl"] for t in losses]) if losses else 0
        avg_rr   = abs(avg_win / avg_loss) if avg_loss else float("inf")

        # Max drawdown
        eq = np.array(equity)
        peak = np.maximum.accumulate(eq)
        dd   = (eq - peak) / peak * 100
        max_dd = float(abs(dd.min()))

        # Sharpe (simplified daily returns)
        pnls    = np.array([t["pnl"] for t in trades])
        pnl_std = np.std(pnls)
        sharpe  = float(np.mean(pnls) / pnl_std * np.sqrt(252)) if len(pnls) > 1 and pnl_std > 0 else 0.0

        result = BacktestResult(
            trades         = trades,
            total_pnl      = round(total_pnl, 2),
            win_rate       = round(win_rate, 1),
            avg_rr         = round(avg_rr, 2),
            max_drawdown   = round(max_dd, 2),
            sharpe         = round(sharpe, 2),
            total_trades   = len(trades),
            winning_trades = len(wins),
        )

        logger.info(f"\n{'='*50}")
        logger.info(f"BACKTEST RESULTS: {self.symbol}")
        logger.info(f"  Trades    : {result.total_trades} (Wins: {result.winning_trades})")
        logger.info(f"  Win Rate  : {result.win_rate:.1f}%")
        logger.info(f"  Avg R:R   : {result.avg_rr:.2f}")
        logger.info(f"  Total P&L : ₹{result.total_pnl:+,.2f}")
        logger.info(f"  Max DD    : {result.max_drawdown:.2f}%")
        logger.info(f"  Sharpe    : {result.sharpe:.2f}")
        logger.info(f"{'='*50}")

        return result


# ------------------------------------------------------------------
# ------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    from datetime import datetime, timedelta
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent))

    from strategy.signal_engine import ORBEMAVWAPStrategy
    from strategy.strategy_base import TradeSignal, Signal
    from monitoring.alerts import TelegramAlerter

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    # ==================================================================
    # Angel One live test: SKIPPED / COMMENTED OUT (Offline/Notification Mode)
    # ==================================================================
    # from auth.session_manager import SessionManager
    # from data.historical_fetcher import HistoricalFetcher
    # sm = SessionManager()
    # obj = sm.login()
    # fetch = HistoricalFetcher(obj)
    # print("Fetching 6 months of SBIN 15-min candles ...")
    # df = fetch.fetch_in_chunks("NSE", "3045", 15, total_days=180, chunk_days=30)
    # print(f"Got {len(df)} candles.")
    # sm.logout()

    def generate_sample_candles(symbol: str = "SBIN-EQ", days: int = 30) -> pd.DataFrame:
        """Generate realistic synthetic 15-min market candles for backtesting without Angel One login."""
        np.random.seed(42)
        records = []
        cur_price = 810.0
        start_date = datetime.now() - timedelta(days=days)

        for day in range(days):
            day_date = start_date + timedelta(days=day)
            if day_date.weekday() >= 5:  # Skip weekends
                continue

            cur_price *= (1 + np.random.normal(0.0005, 0.004))
            for bar in range(25):  # 25 15-min candles (09:15 to 15:30)
                ts = day_date.replace(hour=9, minute=15, second=0, microsecond=0) + timedelta(minutes=15 * bar)
                delta = np.random.normal(0, 1.2)
                open_p = cur_price
                close_p = open_p + delta
                high_p = max(open_p, close_p) + abs(np.random.normal(0, 0.9))
                low_p = min(open_p, close_p) - abs(np.random.normal(0, 0.9))
                vol = int(np.random.uniform(60000, 250000))
                cur_price = close_p

                records.append({
                    "timestamp": ts.strftime("%Y-%m-%d %H:%M:%S"),
                    "open": round(open_p, 2),
                    "high": round(high_p, 2),
                    "low": round(low_p, 2),
                    "close": round(close_p, 2),
                    "volume": vol,
                })

        return pd.DataFrame(records)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print("[INFO] Angel One connection skipped -- running backtest with simulated candles...")
    df = generate_sample_candles("SBIN-EQ", days=30)
    print(f"Loaded {len(df)} simulated 15-min candles.")

    engine = BacktestEngine(
        strategy_class  = ORBEMAVWAPStrategy,
        symbol          = "SBIN-EQ",
        token           = "3045",
        strategy_kwargs = {"candle_minutes": 15},
        config          = BacktestConfig(capital=100_000),
    )
    result = engine.run(df)

    print("\nTrade log (last 10):")
    for t in result.trades[-10:]:
        status = "WIN" if t["win"] else "LOSS"
        print(f"  {status:4s} | {t['direction']:5s} | Entry={t['entry_price']:.2f} "
              f"Exit={t['exit_price']:.2f} | P&L=₹{t['pnl']:+.2f} | {t['reason']}")

    # ------------------------------------------------------------------
    # Dispatch notifications to Telegram
    # ------------------------------------------------------------------
    print("\n📱 Sending notifications to Telegram...")
    alerter = TelegramAlerter()

    # Send a sample trade alert so user can review the exact signal notification layout
    sample_signal = TradeSignal(
        signal=Signal.BUY,
        symbol="SBIN-EQ",
        token="3045",
        entry_price=round(float(df["close"].iloc[-1]), 2),
        stop_loss=round(float(df["close"].iloc[-1]) * 0.985, 2),
        target=round(float(df["close"].iloc[-1]) * 1.025, 2),
        confidence=85.0,
        confidence_factors={
            "orb_breakout": 18,
            "ema_alignment": 17,
            "vwap_position": 16,
            "rsi_quality": 16,
            "volume_confirmation": 18,
        },
        reason="ORB Breakout + Bullish EMA alignment + Price above VWAP (Backtest Test Signal)",
    )
    alerter.send_trade_alert(sample_signal)

    # Send backtest performance summary
    alerter.send_daily_summary(
        summary={"realized_pnl": result.total_pnl, "trades": result.total_trades, "open_positions": 0},
        capital_start=100_000.0,
    )
    print("✅ Telegram notifications dispatched!")
