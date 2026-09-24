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
import json, logging, os, asyncio
from datetime import datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / "config" / "secrets.env")

logger = logging.getLogger(__name__)

_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

_bot = None

_SETTINGS_PATH = Path(__file__).parent.parent / "config" / "settings.yaml"


def _redact(text: str) -> str:
    """Strip the bot token out of error text (e.g. embedded request URLs) before logging."""
    if _BOT_TOKEN:
        text = text.replace(_BOT_TOKEN, "***REDACTED***")
    return text

_DIVIDER = "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬"


def _bar(score: float, max_score: float, length: int = 10) -> str:
    """Render a filled/empty block bar, e.g. ▰▰▰▰▰▰▰▱▱▱ for 7/10."""
    filled = round(max(0.0, min(1.0, score / max_score)) * length)
    return "▰" * filled + "▱" * (length - filled)


def _alerts_log_path() -> Path:
    with open(_SETTINGS_PATH) as f:
        cfg = yaml.safe_load(f)
    log_dir = Path(cfg["monitoring"]["log_dir"])
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "alerts.jsonl"


def _log_alert(record: dict) -> None:
    """Append a structured alert record so P&L can be reconstructed later (see tools/evaluate_pnl.py)."""
    record = {"timestamp": datetime.now().isoformat(), **record}
    try:
        with open(_alerts_log_path(), "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as exc:
        logger.error(f"[Alerts] Failed to write alert log: {exc}")


async def _send_async(message: str) -> None:
    import telegram  # type: ignore
    from telegram.request import HTTPXRequest  # type: ignore
    request = HTTPXRequest(connect_timeout=10.0, read_timeout=15.0, write_timeout=15.0)
    async with telegram.Bot(token=_BOT_TOKEN, request=request) as bot:
        await bot.send_message(chat_id=_CHAT_ID, text=message, parse_mode="HTML")


def _send_sync(message: str) -> None:
    """Send a Telegram message synchronously, retrying once on transient failure."""
    if _BOT_TOKEN and _CHAT_ID:
        for attempt in (1, 2):
            try:
                asyncio.run(_send_async(message))
                return
            except Exception as exc:
                logger.error(f"[Alerts] Telegram send failed (attempt {attempt}/2): {_redact(str(exc))}")

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
        dir_emoji = "📈" if action == "BUY" else "📉"
        symbol = trade_signal.symbol
        entry  = trade_signal.entry_price
        sl     = trade_signal.stop_loss
        target = trade_signal.target
        risk   = abs(entry - sl)
        reward = abs(target - entry)
        conf   = trade_signal.confidence

        levels = (
            f"🎯 Entry   ₹{entry:>10.2f}\n"
            f"🛑 SL      ₹{sl:>10.2f}   (-₹{risk:.2f})\n"
            f"🏁 Target  ₹{target:>10.2f}   (+₹{reward:.2f})\n"
            f"⚖️ R:R      {trade_signal.rr_ratio:>7.2f}"
        )

        factors_txt = ""
        if trade_signal.confidence_factors:
            factor_rows = "\n".join(
                f"{name.replace('_', ' ').title():<15}{_bar(score, 20, 8)} {score:>4.0f}/20"
                for name, score in trade_signal.confidence_factors.items()
            )
            factors_txt = f"\n\n<code>{factor_rows}</code>"

        msg = (
            f"{dir_emoji} <b>{action} {symbol}</b>\n"
            f"{_DIVIDER}\n"
            f"<code>{levels}</code>\n\n"
            f"{trade_signal.confidence_label()}  <b>{conf:.0f}/100</b>\n"
            f"{_bar(conf, 100, 14)}"
            f"{factors_txt}"
        )
        _send_sync(msg)
        _log_alert({
            "event"      : "ENTRY",
            "symbol"     : symbol,
            "direction"  : action,
            "entry_price": entry,
            "stop_loss"  : sl,
            "target"     : target,
            "rr_ratio"   : trade_signal.rr_ratio,
            "confidence" : trade_signal.confidence,
        })

    def send_exit_alert(self, symbol: str, direction: str, exit_price: float,
                         entry_price: float, reason: str, pnl: float | None = None) -> None:
        """
        Exit alert — tells you to close your position.
        """
        action  = "SELL" if "long" in direction.lower() else "BUY TO COVER"
        is_win  = pnl is not None and pnl >= 0
        emoji   = "🟢" if pnl is None else ("✅" if is_win else "❌")
        pnl_txt = f"\n\n<b>P&L: ₹{pnl:+,.2f}</b>" if pnl is not None else ""
        msg = (
            f"{emoji} <b>EXIT {symbol}</b> — {action}\n"
            f"{_DIVIDER}\n"
            f"<code>Entry  ₹{entry_price:>10.2f}\n"
            f"Exit   ₹{exit_price:>10.2f}</code>\n\n"
            f"📝 {reason}"
            f"{pnl_txt}"
        )
        _send_sync(msg)
        _log_alert({
            "event"      : "EXIT",
            "symbol"     : symbol,
            "direction"  : direction,
            "entry_price": entry_price,
            "exit_price" : exit_price,
            "reason"     : reason,
            "pnl"        : pnl,
        })

    def send_no_setup_found(self, watchlist_symbols: list[str]) -> None:
        _send_sync("📭 <b>No Setup Found Today</b>")

    # ---------------------------------------------------------------
    # System alerts
    # ---------------------------------------------------------------

    def send_circuit_break(self, reason: str) -> None:
        _send_sync(f"🛑 <b>CIRCUIT BREAKER TRIPPED</b>\n{_DIVIDER}\n{reason}")

    def send_daily_summary(self, summary: dict, capital_start: float) -> None:
        pnl = summary.get("realized_pnl", 0)
        emoji = "💰" if pnl >= 0 else "📉"
        pct = (pnl / capital_start * 100) if capital_start else 0.0
        _send_sync(
            f"{emoji} <b>Daily P&L</b>\n{_DIVIDER}\n"
            f"<code>₹{pnl:+,.2f}  ({pct:+.2f}%)</code>"
        )

    def send_session_start(self, mode: str, symbols: list[str], capital: float) -> None:
        _send_sync(
            f"🟢 <b>ALGO STARTED</b>\n{_DIVIDER}\n"
            f"Mode: <b>{mode.upper()}</b> | Capital: ₹{capital:,.0f} | Watchlist: {len(symbols)} symbols"
        )

    def send_session_end(self) -> None:
        _send_sync("🔴 <b>ALGO STOPPED</b>")

    def send_error(self, message: str) -> None:
        _send_sync(f"⚠️ <b>SYSTEM ERROR</b>\n{_DIVIDER}\n{message}")

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
            f"✅ <b>ORDER PLACED</b> — {action} {qty}x{trade_signal.symbol}\n"
            f"{_DIVIDER}\n"
            f"<code>Entry   ₹{trade_signal.entry_price:>10.2f}\n"
            f"SL      ₹{trade_signal.stop_loss:>10.2f}\n"
            f"Target  ₹{trade_signal.target:>10.2f}</code>\n\n"
            f"Order ID: <code>{order_id}</code>"
        )
        _send_sync(msg)

    def send_order_failed(self, symbol: str, reason: str) -> None:
        _send_sync(f"❌ <b>ORDER FAILED</b> — {symbol}\n{_DIVIDER}\n{reason}")

    def send_order_skipped(self, trade_signal, reason: str) -> None:
        """
        `trade_signal` may be a full TradeSignal (preferred — logs entry/stop/target
        so we can later reconstruct what the trade WOULD have done) or a bare symbol
        string (legacy call sites with no signal object in scope).
        """
        from strategy.strategy_base import Signal, TradeSignal

        if isinstance(trade_signal, TradeSignal):
            symbol = trade_signal.symbol
            _send_sync(f"⏭️ <b>NOT EXECUTED</b> — {symbol}\n{_DIVIDER}\nAlerted but no order was placed.\nReason: {reason}")
            _log_alert({
                "event"      : "SKIPPED",
                "symbol"     : symbol,
                "direction"  : "BUY" if trade_signal.signal == Signal.BUY else "SELL",
                "entry_price": trade_signal.entry_price,
                "stop_loss"  : trade_signal.stop_loss,
                "target"     : trade_signal.target,
                "confidence" : trade_signal.confidence,
                "reason"     : reason,
            })
        else:
            symbol = trade_signal
            _send_sync(f"⏭️ <b>NOT EXECUTED</b> — {symbol}\n{_DIVIDER}\nAlerted but no order was placed.\nReason: {reason}")
            _log_alert({"event": "SKIPPED", "symbol": symbol, "reason": reason})

    def send_order_closed(self, order_id: str, symbol: str, exit_price: float, pnl: float) -> None:
        emoji = "✅" if pnl >= 0 else "❌"
        msg = (
            f"{emoji} <b>ORDER CLOSED</b> — {symbol}\n"
            f"{_DIVIDER}\n"
            f"<code>Exit   ₹{exit_price:>10.2f}\n"
            f"P&L    ₹{pnl:>+10,.2f}</code>\n\n"
            f"Order ID: <code>{order_id}</code>"
        )
        _send_sync(msg)
