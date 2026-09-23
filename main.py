"""
main.py

Orchestrator — NOTIFICATION-ONLY MODE.

Current behavior:
  - Scans all watchlist symbols every candle close
  - Uses a 3-second buffer to gather all valid signals for the current candle
  - Ranks them by confidence score
  - Sends ONLY the #1 absolute best setup (Top Pick) via Telegram
  - If nothing qualifies all day → sends "No Setup Found" at 14:45 IST
  - Does NOT place any orders automatically

Auto-execution is stubbed as a clearly marked placeholder.
To enable: implement the _execute_placeholder() call sites below.

Run:
    python main.py

Kill switch:
    Create file: .killswitch
"""

from __future__ import annotations
import logging, os, sys, time
from datetime import datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv(Path("config/secrets.env"))

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "algo.log"),
    ]
)
logger = logging.getLogger("main")

SETTINGS_PATH = Path("config/settings.yaml")
KILLSWITCH    = Path(".killswitch")


def load_settings() -> dict:
    with open(SETTINGS_PATH) as f:
        return yaml.safe_load(f)


def build_ws_token_list(watchlist: list[dict]) -> list[dict]:
    groups: dict[int, list[str]] = {}
    for inst in watchlist:
        seg = int(inst["segment"])
        groups.setdefault(seg, []).append(str(inst["token"]))
    return [{"exchangeType": seg, "tokens": toks} for seg, toks in groups.items()]


def retry_failed_warmups(
    watchlist: list[dict],
    histories: dict,
    fetcher,
    interval_min: int,
    warmup_days: int,
    cooldown_secs: float = 5.0,
) -> list[str]:
    """
    Retry warm-up fetch for any symbol whose history came back empty.
    Mutates `histories` in place. Returns symbols still empty after the retry.
    """
    failed = [inst for inst in watchlist if histories[str(inst["token"])].empty]
    if failed:
        logger.warning(f"Retrying warm-up for {len(failed)} symbol(s) that failed the first pass: "
                        f"{[inst['symbol'] for inst in failed]}")
        time.sleep(cooldown_secs)
        for inst in failed:
            token  = str(inst["token"])
            symbol = inst["symbol"]
            exch   = inst["exchange"]
            logger.info(f"Retry warm-up for {symbol} ...")
            warmup = fetcher.fetch_warmup(exch, token, interval_min, lookback_days=warmup_days)
            histories[token] = warmup
            if warmup.empty:
                logger.error(f"[Warmup] {symbol} ({token}) still has no history after retry pass.")

    return [inst["symbol"] for inst in watchlist if histories[str(inst["token"])].empty]


def run():
    cfg         = load_settings()
    trading_cfg = cfg["trading"]
    risk_cfg    = cfg["risk"]
    strategy_cfg= cfg["strategy"]
    watchlist   = cfg["watchlist"]["instruments"]
    capital     = float(trading_cfg["capital"])
    mode        = trading_cfg["mode"]     # "paper" (default) or "live" (future)
    interval_min= int(trading_cfg["candle_interval_minutes"])
    warmup_days = int(cfg.get("data", {}).get("warmup_lookback_days", 30))

    symbols = [i["symbol"] for i in watchlist]

    # ------------------------------------------------------------------
    # Imports
    # ------------------------------------------------------------------
    from auth.session_manager       import SessionManager
    from data.instrument_master     import download_instrument_master
    from data.historical_fetcher    import HistoricalFetcher
    from data.websocket_feed        import LiveFeed, CandleAggregator, parse_snap_quote_tick
    from strategy.signal_engine     import ORBEMAVWAPStrategy
    from strategy.strategy_base     import Signal
    from monitoring.logger          import AlgoLogger
    from monitoring.alerts          import TelegramAlerter
    from execution.order_manager    import OrderManager
    from execution.position_manager import PositionManager
    from risk.position_sizer        import PositionSizer
    from risk.circuit_breaker       import CircuitBreaker

    algo_logger = AlgoLogger()
    alerter     = TelegramAlerter()

    # ------------------------------------------------------------------
    # 1. Auth
    # ------------------------------------------------------------------
    logger.info("=== Angel One Algo — NOTIFICATION-ONLY MODE ===")
    sm  = SessionManager()
    obj = sm.login()
    tokens = sm.get_tokens()

    # In live mode, size positions and risk limits off the broker's actual
    # available balance rather than blindly trusting the static config value.
    if mode == "live":
        try:
            rms = obj.rmsLimit()
            data = rms.get("data", {}) if isinstance(rms, dict) else {}
            live_balance = float(data.get("availablecash") or data.get("net") or 0)
            if live_balance <= 0:
                raise ValueError(f"RMS response had no usable balance: {rms}")
            if abs(live_balance - capital) / capital > 0.10:
                logger.warning(f"[Capital] Configured capital ₹{capital:,.0f} differs from live "
                               f"account balance ₹{live_balance:,.0f} by >10% — using live balance.")
            capital = live_balance
            logger.info(f"[Capital] Using live account balance ₹{capital:,.0f} for sizing/risk limits.")
        except Exception as exc:
            logger.error(f"[Capital] Failed to fetch live account balance via RMS API: {exc} "
                         f"— falling back to configured capital ₹{capital:,.0f}.")

    algo_logger.log_system("Session started — notification-only mode (TOP PICK ONLY)")
    alerter.send_session_start(mode, symbols, capital)

    # Auto-execution is OFF by default (trading.auto_execute in settings.yaml).
    # With it off, order_mgr/position_mgr/sizer/breaker are never touched —
    # the loop below behaves exactly as it always has (alerts only).
    auto_execute = bool(trading_cfg.get("auto_execute", False))
    order_mgr    = OrderManager(smart_obj=obj) if auto_execute else None
    position_mgr = PositionManager() if auto_execute else None
    sizer        = PositionSizer(capital=capital, per_trade_risk_pct=risk_cfg["per_trade_risk_pct"]) if auto_execute else None
    breaker      = CircuitBreaker(
        capital=capital,
        daily_loss_limit_pct=risk_cfg["daily_loss_limit_pct"],
        max_trades_per_day=trading_cfg["max_trades_per_day"],
        max_concurrent=trading_cfg["max_concurrent_positions"],
        max_consecutive_losses=risk_cfg["max_consecutive_losses"],
    ) if auto_execute else None
    token_exchange = {str(inst["token"]): inst["exchange"] for inst in watchlist}
    logger.info(f"Auto-execute: {'ENABLED — orders will be placed (' + mode.upper() + ' mode)' if auto_execute else 'disabled — alerts only'}")

    # ------------------------------------------------------------------
    # 2. Instrument master
    # ------------------------------------------------------------------
    download_instrument_master()

    # ------------------------------------------------------------------
    # 3. Strategies + warm-up
    # ------------------------------------------------------------------
    fetcher    = HistoricalFetcher(obj)
    strategies = {}    # token -> ORBEMAVWAPStrategy
    histories  = {}    # token -> pd.DataFrame

    for inst in watchlist:
        token  = str(inst["token"])
        symbol = inst["symbol"]
        exch   = inst["exchange"]

        strat = ORBEMAVWAPStrategy(
            symbol=symbol, token=token,
            candle_minutes  = interval_min,
            orb_minutes     = strategy_cfg["orb_minutes"],
            ema_fast        = strategy_cfg["ema_fast"],
            ema_slow        = strategy_cfg["ema_slow"],
            rsi_period      = strategy_cfg["rsi_period"],
            rsi_overbought  = strategy_cfg["rsi_overbought"],
            rsi_oversold    = strategy_cfg["rsi_oversold"],
            atr_period      = strategy_cfg["atr_period"],
            atr_stop_mult   = risk_cfg["atr_stop_multiplier"],
            atr_target_mult = risk_cfg["atr_target_multiplier"],
            vwap_filter     = strategy_cfg["vwap_filter"],
            breakeven_r         = strategy_cfg.get("breakeven_r", 1.0),
            trail_atr_mult      = strategy_cfg.get("trail_atr_mult", 1.0),
            max_holding_candles = strategy_cfg.get("max_holding_candles", 0),
            min_atr_pct         = strategy_cfg.get("min_atr_pct", 0.0),
            max_atr_pct         = strategy_cfg.get("max_atr_pct", 100.0),
        )
        strategies[token] = strat

        logger.info(f"Fetching warm-up for {symbol} ...")
        warmup = fetcher.fetch_warmup(exch, token, interval_min, lookback_days=warmup_days)
        histories[token] = warmup

    # Second pass: retry any symbols that came back empty (usually rate-limit
    # stragglers) now that request pressure from the first pass has cooled down.
    still_failed = retry_failed_warmups(watchlist, histories, fetcher, interval_min, warmup_days)
    if still_failed:
        algo_logger.log_system(f"Warm-up failed for: {', '.join(still_failed)}")
        alerter.send_system(f"⚠️ Warm-up failed for {len(still_failed)} symbol(s): {', '.join(still_failed)}")

    # ------------------------------------------------------------------
    # 4. WebSocket feed
    # ------------------------------------------------------------------
    ws_tokens    = build_ws_token_list(watchlist)
    feed         = LiveFeed(
        tokens     = ws_tokens,
        api_key    = tokens["api_key"],
        client_id  = tokens["client_id"],
        feed_token = tokens["feed_token"],
        jwt_token  = tokens["jwt_token"],
    )
    aggregators : dict[str, CandleAggregator] = {
        str(inst["token"]): CandleAggregator(interval_min) for inst in watchlist
    }
    feed.start()
    logger.info("WebSocket feed started. Tracking Top Picks.")

    # ------------------------------------------------------------------
    # 5. Session state
    # ------------------------------------------------------------------
    sq_off_h, sq_off_m  = map(int, trading_cfg["square_off_time"].split(":"))
    allow_after_hours   = bool(trading_cfg.get("allow_after_hours", False))
    if allow_after_hours:
        logger.warning("allow_after_hours=true — EOD square-off is disabled for WebSocket debugging.")
    no_setup_alert_sent = False    
    alerted_tokens      = set()    # Prevent duplicate alerts
    alerted_directions  : dict[str, str] = {}  # token -> "long"/"short" for entries actually alerted
    current_ltps        : dict[str, float] = {}

    # NEW: Signal buffering for Top Pick logic
    signal_buffer = []             # List of TradeSignals for the current interval
    buffer_flush_time = 0.0        # System timestamp when we sort and send

    import pandas as pd

    def _handle_exit(token: str, symbol: str, direction: str, exit_price: float,
                      entry_price: float, reason: str) -> None:
        """Shared exit handling for both candle-close and intrabar (tick-level) triggers."""
        if alerted_directions.get(token) != direction:
            return  # This token's entry was never actually alerted (not a top pick) — skip.
        alerted_directions.pop(token, None)

        pnl = None
        if auto_execute and position_mgr.has_position(token):
            pos = position_mgr.get_position(token)
            exit_txn = "SELL" if pos["direction"] == "long" else "BUY"
            exit_order_id = order_mgr.place_order(
                symbol=pos["symbol"], token=token,
                exchange=token_exchange.get(token, "NSE"),
                transaction=exit_txn, qty=pos["qty"], order_type="MARKET",
            )
            pnl = position_mgr.close_position(token, exit_price, exit_order_id or "")
            breaker.on_trade_close(pnl)
            alerter.send_order_closed(exit_order_id or "?", symbol, exit_price, pnl)

        alerter.send_exit_alert(
            symbol      = symbol,
            direction   = direction,
            exit_price  = exit_price,
            entry_price = entry_price,
            reason      = reason,
            pnl         = pnl,
        )

    # ------------------------------------------------------------------
    # 6. Main event loop
    # ------------------------------------------------------------------
    try:
        while True:
            # Kill switch
            if KILLSWITCH.exists():
                logger.warning("Kill switch activated — shutting down.")
                alerter.send_system("🛑 Kill switch — shutting down")
                break

            now = datetime.now()

            # "No setup found" message
            if (not no_setup_alert_sent
                    and now.hour == 14 and now.minute >= 45
                    and not alerted_tokens):
                alerter.send_no_setup_found(symbols)
                algo_logger.log_system("No setup found today")
                no_setup_alert_sent = True

            # EOD square-off time (skipped while allow_after_hours is true)
            if (not allow_after_hours
                    and (now.hour > sq_off_h or (now.hour == sq_off_h and now.minute >= sq_off_m))):
                logger.info("EOD time reached — ending session.")
                if auto_execute and position_mgr.open_count() > 0:
                    for pos in position_mgr.get_all_positions():
                        ltp = current_ltps.get(pos["token"], pos["entry_price"])
                        exit_txn = "SELL" if pos["direction"] == "long" else "BUY"
                        exit_order_id = order_mgr.place_order(
                            symbol=pos["symbol"], token=pos["token"],
                            exchange=token_exchange.get(pos["token"], "NSE"),
                            transaction=exit_txn, qty=pos["qty"], order_type="MARKET",
                        )
                        pnl = position_mgr.close_position(pos["token"], ltp, exit_order_id or "")
                        breaker.on_trade_close(pnl)
                        alerter.send_order_closed(exit_order_id or "?", pos["symbol"], ltp, pnl)
                break

            # ----------------------------------------------------------
            # Buffer Flush Logic (Top Pick Ranking)
            # ----------------------------------------------------------
            if signal_buffer and time.time() >= buffer_flush_time:
                # Sort signals by confidence (highest first)
                signal_buffer.sort(key=lambda s: s.confidence, reverse=True)
                
                # Pick Top 2
                top_signals = signal_buffer[:2]
                ignored_count = len(signal_buffer) - len(top_signals)
                
                logger.info(f"🏆 TOP {len(top_signals)} PICK(S) SELECTED. Ignored {ignored_count} other setups.")
                algo_logger.log_system(f"Sent Top {len(top_signals)} Picks", {"ignored_count": ignored_count})

                for top_signal in top_signals:
                    # Mark as alerted so we don't resend it today
                    alert_key = f"{top_signal.token}_{top_signal.signal.value}_{top_signal.entry_window_mins}"
                    alerted_tokens.add(alert_key)
                    alerted_directions[top_signal.token] = "long" if top_signal.signal == Signal.BUY else "short"
                    
                    # Send Alert
                    alerter.send_trade_alert(top_signal)

                    # ----------------------------------------------------
                    # Auto-execution (only when trading.auto_execute: true)
                    # ----------------------------------------------------
                    if auto_execute:
                        can, block_reason = breaker.can_trade()
                        if not can:
                            logger.warning(f"[AutoExec] Skipped {top_signal.symbol}: {block_reason}")
                            alerter.send_order_skipped(top_signal.symbol, block_reason)
                            continue
                        if position_mgr.has_position(top_signal.token):
                            logger.warning(f"[AutoExec] Already in a position for {top_signal.symbol} — skipping.")
                            alerter.send_order_skipped(top_signal.symbol, "Already in a position for this symbol")
                            continue

                        qty = sizer.compute_qty(top_signal.entry_price, top_signal.stop_loss)
                        if qty <= 0:
                            logger.warning(f"[AutoExec] Qty computed as 0 for {top_signal.symbol} — skipping.")
                            alerter.send_order_skipped(top_signal.symbol, "Computed qty was 0 (stop distance too tight for risk budget)")
                            continue

                        txn = "BUY" if top_signal.signal == Signal.BUY else "SELL"
                        order_id = order_mgr.place_order(
                            symbol=top_signal.symbol, token=top_signal.token,
                            exchange=token_exchange.get(top_signal.token, "NSE"),
                            transaction=txn, qty=qty, order_type="MARKET",
                        )
                        if order_id:
                            position_mgr.open_position(
                                symbol=top_signal.symbol, token=top_signal.token,
                                direction="long" if txn == "BUY" else "short",
                                qty=qty, entry_price=top_signal.entry_price,
                                stop_loss=top_signal.stop_loss, target=top_signal.target,
                                order_id=order_id,
                            )
                            breaker.on_trade_open()
                            alerter.send_order_placed(order_id, top_signal, qty)
                        else:
                            alerter.send_order_failed(top_signal.symbol, "OrderManager.place_order returned None")

                # Reset buffer
                signal_buffer.clear()
                buffer_flush_time = 0.0

            # ----------------------------------------------------------
            # Consume tick
            # ----------------------------------------------------------
            # Timeout is 0.1 so we don't block the flush logic
            raw_tick = feed.get_tick(timeout=0.1)
            if raw_tick is None:
                continue

            tick = parse_snap_quote_tick(raw_tick)
            if tick is None:
                continue

            token = tick["token"]
            current_ltps[token] = tick["ltp"]

            # ----------------------------------------------------------
            # Intrabar stop/target check — runs on every tick, not just at
            # candle close, so an open position isn't exposed to a full
            # 15-min bar's worth of adverse movement before we react.
            # ----------------------------------------------------------
            open_strat = strategies.get(token)
            if open_strat is not None:
                direction = open_strat.get_position_direction()
                if direction is not None:
                    ltp    = tick["ltp"]
                    stop   = open_strat.get_current_stop()
                    target = open_strat.get_current_target()
                    hit_stop   = (direction == "long"  and ltp <= stop)   or (direction == "short" and ltp >= stop)
                    hit_target = (direction == "long"  and ltp >= target) or (direction == "short" and ltp <= target)
                    if hit_stop or hit_target:
                        reason = f"{'Stop' if hit_stop else 'Target'} hit (intrabar): ₹{ltp:.2f}"
                        entry_price = open_strat.get_entry_price()
                        symbol = open_strat.symbol
                        logger.info(f"[EXIT-INTRABAR] {symbol} | {reason}")
                        open_strat.force_exit()
                        _handle_exit(token, symbol, direction, ltp, entry_price, reason)

            agg = aggregators.get(token)
            if agg is None:
                continue

            agg.process_tick(token, tick["ltp"], tick["timestamp"], tick.get("volume", 0))
            completed = agg.get_completed(timeout=0)
            if completed is None:
                continue

            # ----------------------------------------------------------
            # Got a closed candle — run strategy
            # ----------------------------------------------------------
            new_row  = pd.DataFrame([completed])
            history  = histories.get(token, pd.DataFrame())
            history  = pd.concat([history, new_row], ignore_index=True)
            histories[token] = history

            strat = strategies.get(token)
            if strat is None:
                continue

            signal = strat.on_candle_close(new_row.iloc[0], history)
            algo_logger.log_signal(signal)

            if signal.signal == Signal.HOLD:
                continue

            # ----------------------------------------------------------
            # Entry signal — Add to Buffer
            # ----------------------------------------------------------
            if signal.is_entry():
                # We use a key without exact timestamp here so we don't re-enter 
                # the same trade direction for the same token all day
                alert_key = f"{token}_{signal.signal.value}_{signal.entry_window_mins}"
                
                if alert_key in alerted_tokens:
                    continue  # Already traded this today
                
                # Add to our ranking buffer
                signal_buffer.append(signal)
                logger.debug(f"Added {signal.symbol} to buffer with score {signal.confidence}")
                
                # Start the 3-second countdown if this is the first signal in the burst
                if buffer_flush_time == 0.0:
                    buffer_flush_time = time.time() + 3.0

            # ----------------------------------------------------------
            # Exit signal — notify immediately (DO NOT BUFFER EXITS)
            # ----------------------------------------------------------
            elif signal.is_exit():
                ltp = current_ltps.get(token, signal.entry_price)
                logger.info(f"[EXIT] {signal.symbol} | {signal.reason} | LTP=₹{ltp:.2f}")

                exit_direction = "long" if signal.signal == Signal.EXIT_LONG else "short"
                _handle_exit(token, signal.symbol, exit_direction, ltp, signal.entry_price, signal.reason)

    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt — shutting down.")

    finally:
        feed.stop()

        if not alerted_tokens and not no_setup_alert_sent:
            alerter.send_no_setup_found(symbols)
            algo_logger.log_system("Session ended — no setups found")
        else:
            alerter.send_session_end()

        if auto_execute:
            alerter.send_daily_summary(position_mgr.session_summary(), capital)

        sm.logout()
        logger.info("=== Session ended ===")


if __name__ == "__main__":
    run()
