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
    Never places orders itself — order placement (when trading.auto_execute
    is enabled) happens in main.py; this class only reports on it.
    """

    # ---------------------------------------------------------------
    # Trade alerts (notification-only — NO automatic execution)
    # ---------------------------------------------------------------

    def send_trade_alert(self, trade_signal) -> None:
        """
        Main entry alert — sent when strategy finds a high-confidence setup.
        Includes R:R and a per-factor confidence breakdown so the reasoning
        behind the pick is transparent, not just the raw numbers.
        """
        from strategy.strategy_base import Signal

        action = "BUY" if trade_signal.signal == Signal.BUY else "SELL"
        symbol = trade_signal.symbol
        entry  = trade_signal.entry_price
        sl     = trade_signal.stop_loss
        target = trade_signal.target
        risk   = abs(entry - sl)
        reward = abs(target - entry)

        factors_txt = ""
        if trade_signal.confidence_factors:
            factors_txt = "\n" + "\n".join(
                f"  • {name.replace('_', ' ').title()}: {score:.0f}/20"
                for name, score in trade_signal.confidence_factors.items()
            )

        msg = (
            f"⚡ <b>{action} {symbol}</b>\n"
            f"Entry: ₹{entry:.2f}\n"
            f"SL: ₹{sl:.2f} (₹{risk:.2f} risk)\n"
            f"Target: ₹{target:.2f} (₹{reward:.2f} reward)\n"
            f"R:R: {trade_signal.rr_ratio:.2f}\n"
            f"Confidence: {trade_signal.confidence:.0f}/100 {trade_signal.confidence_label()}"
            f"{factors_txt}"
        )
        _send_sync(msg)

    def send_exit_alert(self, symbol: str, direction: str, exit_price: float,
                         entry_price: float, reason: str, pnl: float | None = None) -> None:
        """
        Exit alert — tells you to close your position.
        """
        action  = "SELL" if "long" in direction.lower() else "BUY TO COVER"
        pnl_txt = f"\nP&L: ₹{pnl:+,.2f}" if pnl is not None else ""
        msg = (
            f"🚨 <b>EXIT {symbol}</b>\n"
            f"Action: {action}\n"
            f"Entry: ₹{entry_price:.2f}\n"
            f"Exit Price: ₹{exit_price:.2f}\n"
            f"Reason: {reason}"
            f"{pnl_txt}"
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
    # Auto-execution notifications — only fired when trading.auto_execute
    # is true in config/settings.yaml. Distinguishes an actually-placed
    # order from a plain alert so you can tell paper/live fills apart.
    # ---------------------------------------------------------------

    def send_order_placed(self, order_id: str, trade_signal, qty: int) -> None:
        from strategy.strategy_base import Signal
        action = "BUY" if trade_signal.signal == Signal.BUY else "SELL"
        msg = (
            f"✅ <b>ORDER PLACED</b> {action} {qty}x{trade_signal.symbol}\n"
            f"Entry: ₹{trade_signal.entry_price:.2f} | SL: ₹{trade_signal.stop_loss:.2f} "
            f"| Target: ₹{trade_signal.target:.2f}\n"
            f"Order ID: {order_id}"
        )
        _send_sync(msg)

    def send_order_failed(self, symbol: str, reason: str) -> None:
        _send_sync(f"❌ <b>ORDER FAILED</b> {symbol}\nReason: {reason}")

    def send_order_closed(self, order_id: str, symbol: str, exit_price: float, pnl: float) -> None:
        emoji = "✅" if pnl >= 0 else "❌"
        msg = (
            f"{emoji} <b>ORDER CLOSED</b> {symbol}\n"
            f"Exit: ₹{exit_price:.2f} | P&L: ₹{pnl:+,.2f}\n"
            f"Order ID: {order_id}"
        )
        _send_sync(msg)
