"""
monitoring/alerts.py

Telegram notifications — notification-only mode (no auto-execution).

Message format is designed to be clear and actionable:
  - What to trade (symbol)
  - Direction (LONG / SHORT)
  - Entry price and window
  - Stop loss (with ₹ distance from entry)
  - Target (with ₹ potential gain and R:R)
  - Confidence score with breakdown
  - "No setup found" message when nothing qualifies

Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in config/secrets.env.
Falls back to console logging if not configured.
"""

from __future__ import annotations
import logging, os, asyncio
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / "config" / "secrets.env")

logger = logging.getLogger(__name__)

_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

_bot = None


async def _send_async(message: str) -> None:
    import telegram  # type: ignore
    async with telegram.Bot(token=_BOT_TOKEN) as bot:
        await bot.send_message(chat_id=_CHAT_ID, text=message, parse_mode="HTML")


def _send_sync(message: str) -> None:
    """Send a Telegram message synchronously."""
    if _BOT_TOKEN and _CHAT_ID:
        try:
            asyncio.run(_send_async(message))
            return
        except Exception as exc:
            logger.error(f"[Alerts] Telegram send failed: {exc}")

    # Fallback: print to console
    print(f"\n{'='*60}\n[ALERT] {message}\n{'='*60}\n")
    logger.info(f"[ALERT] {message}")


class TelegramAlerter:
    """
    Sends clear, actionable trade notifications to Telegram.
    Does NOT trigger any order execution.
    """

    # ---------------------------------------------------------------
    # Trade alerts (notification-only — NO automatic execution)
    # ---------------------------------------------------------------

    def send_trade_alert(self, trade_signal) -> None:
        """
        Main entry alert — sent when strategy finds a high-confidence setup.
        Ultra-minimal format: only what to trade.
        """
        from strategy.strategy_base import Signal

        action = "BUY" if trade_signal.signal == Signal.BUY else "SELL"
        symbol = trade_signal.symbol
        entry  = trade_signal.entry_price
        sl     = trade_signal.stop_loss
        target = trade_signal.target

        msg = (
            f"⚡ <b>{action} {symbol}</b>\n"
            f"Entry: ₹{entry:.2f}\n"
            f"SL: ₹{sl:.2f}\n"
            f"Target: ₹{target:.2f}"
        )
        _send_sync(msg)

    def send_exit_alert(self, symbol: str, direction: str, exit_price: float,
                         entry_price: float, reason: str, pnl: float | None = None) -> None:
        """
        Exit alert — tells you to close your position.
        """
        action = "SELL" if "long" in direction.lower() else "BUY TO COVER"
        msg = (
            f"🚨 <b>EXIT {symbol}</b>\n"
            f"Action: {action}\n"
            f"Exit Price: ₹{exit_price:.2f}"
        )
        _send_sync(msg)

    def send_no_setup_found(self, watchlist_symbols: list[str]) -> None:
        _send_sync("📭 No Setup Found Today")

    # ---------------------------------------------------------------
    # System alerts
    # ---------------------------------------------------------------

    def send_circuit_break(self, reason: str) -> None:
        _send_sync(f"🛑 CIRCUIT BREAKER: {reason}")

    def send_daily_summary(self, summary: dict, capital_start: float) -> None:
        pnl = summary.get("realized_pnl", 0)
        _send_sync(f"💰 Daily P&L: ₹{pnl:+,.2f}")

    def send_session_start(self, mode: str, symbols: list[str], capital: float) -> None:
        _send_sync("🤖 ALGO STARTED")

    def send_session_end(self) -> None:
        _send_sync("🔴 ALGO STOPPED")

    def send_error(self, message: str) -> None:
        _send_sync(f"⚠️ <b>SYSTEM ERROR</b>\n{message}")

    def send_system(self, message: str) -> None:
        _send_sync(f"🤖 {message}")

    # ---------------------------------------------------------------
    # PLACEHOLDER: Future auto-execution notification
    # ---------------------------------------------------------------
    # When auto-execution is enabled, call this AFTER an order is placed
    # to confirm execution vs. just alerting.
    #
    # def send_order_placed(self, order_id: str, trade_signal, qty: int) -> None:
    #     """PLACEHOLDER — fires when auto-execution is enabled."""
    #     raise NotImplementedError("Auto-execution not yet enabled.")
