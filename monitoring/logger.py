"""
monitoring/logger.py

Structured logging for the algo system.

Two outputs:
  1. JSON-lines log file (logs/trades.jsonl) — one JSON object per event
  2. SQLite database (logs/algo.db) — queryable trade history

Event types: SIGNAL, ORDER, FILL, REJECTION, ERROR, SYSTEM, DAILY_SUMMARY

Usage:
    log = AlgoLogger()
    log.log_signal(trade_signal)
    log.log_order(order_id, symbol, ...)
    log.log_fill(order_id, avg_price, qty, pnl)
"""

from __future__ import annotations
import json, logging, sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

_SETTINGS_PATH = Path(__file__).parent.parent / "config" / "settings.yaml"


def _get_log_dir() -> Path:
    with open(_SETTINGS_PATH) as f:
        cfg = yaml.safe_load(f)
    p = Path(cfg["monitoring"]["log_dir"])
    p.mkdir(parents=True, exist_ok=True)
    return p


class AlgoLogger:
    """
    Dual-output structured logger (JSON-lines + SQLite).
    Thread-safe for the main trading loop.
    """

    def __init__(self) -> None:
        log_dir         = _get_log_dir()
        self._jsonl_path = log_dir / "trades.jsonl"
        self._db_path    = log_dir / "algo.db"
        self._init_db()

    # ------------------------------------------------------------------
    # Event loggers
    # ------------------------------------------------------------------

    def log_signal(self, trade_signal) -> None:
        self._write({
            "event"      : "SIGNAL",
            "signal"     : trade_signal.signal.value,
            "symbol"     : trade_signal.symbol,
            "token"      : trade_signal.token,
            "entry_price": trade_signal.entry_price,
            "stop_loss"  : trade_signal.stop_loss,
            "target"     : trade_signal.target,
            "atr"        : trade_signal.atr,
            "reason"     : trade_signal.reason,
        })

    def log_order(
        self, order_id: str, symbol: str, direction: str,
        qty: int, price: float, order_type: str, status: str = "placed"
    ) -> None:
        self._write({
            "event"     : "ORDER",
            "order_id"  : order_id,
            "symbol"    : symbol,
            "direction" : direction,
            "qty"       : qty,
            "price"     : price,
            "order_type": order_type,
            "status"    : status,
        })

    def log_fill(
        self, order_id: str, symbol: str, direction: str,
        qty: int, avg_price: float, pnl: float = 0.0
    ) -> None:
        self._write({
            "event"    : "FILL",
            "order_id" : order_id,
            "symbol"   : symbol,
            "direction": direction,
            "qty"      : qty,
            "avg_price": avg_price,
            "pnl"      : pnl,
        })

    def log_error(self, message: str, exc: Optional[Exception] = None) -> None:
        self._write({
            "event"    : "ERROR",
            "message"  : message,
            "exception": str(exc) if exc else None,
        })

    def log_system(self, message: str, data: dict | None = None) -> None:
        self._write({
            "event"  : "SYSTEM",
            "message": message,
            **(data or {}),
        })

    def log_daily_summary(self, summary: dict) -> None:
        self._write({"event": "DAILY_SUMMARY", **summary})

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _write(self, payload: dict) -> None:
        payload["timestamp"] = datetime.now().isoformat()
        line = json.dumps(payload)

        # JSON-lines file
        try:
            with open(self._jsonl_path, "a") as f:
                f.write(line + "\n")
        except Exception as exc:
            logger.error(f"[AlgoLogger] JSONL write error: {exc}")

        # SQLite
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute(
                    "INSERT INTO events (timestamp, event_type, payload) VALUES (?,?,?)",
                    (payload["timestamp"], payload["event"], line)
                )
        except Exception as exc:
            logger.error(f"[AlgoLogger] SQLite write error: {exc}")

    def _init_db(self) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp  TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload    TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_event_type ON events(event_type)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON events(timestamp)")
