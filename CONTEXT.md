# Angel One SmartAPI Algo Trading — Context File

> **Purpose**: Single source of truth for any agent or session resuming this project. Read this FIRST.

---

## Project Location
`C:\Users\varun\.gemini\antigravity\scratch\angel-algo\`

---

## Current Mode: NOTIFICATION-ONLY (no auto-execution)

The system scans markets and sends Telegram alerts — it does NOT place orders.
Auto-execution code exists as clearly commented placeholders in `main.py` and `signal_engine.py`.

---

## Build Status

| Phase | Description | Status |
|-------|-------------|--------|
| 0 | Project scaffold & config | ✅ Done |
| 1 | Auth layer (SessionManager + TOTP) | ✅ Done |
| 2 | Instrument master + historical fetcher | ✅ Done |
| 2b | WebSocket live feed (CandleAggregator + auto-reconnect) | ✅ Done |
| 3 | Strategy base (Signal, TradeSignal + confidence fields) | ✅ Done |
| 3b | ORB/EMA/VWAP/RSI/Volume confidence scoring engine | ✅ Done |
| 4 | Event-driven backtester (same strategy code) | ✅ Done |
| 5 | Order manager (paper/live, idempotent) — PLACEHOLDER | ✅ Stubbed |
| 5b | Order tracker + position manager — PLACEHOLDER | ✅ Stubbed |
| 6 | Risk (position sizer + circuit breaker) — PLACEHOLDER | ✅ Stubbed |
| 7 | Rich Telegram notifications (notification-only mode) | ✅ Done |
| 8 | Main orchestrator (notification-only, execution as placeholders) | ✅ Done |
| 8b | Deployment (daily_runner.sh + systemd unit) | ✅ Done |

---

## Architecture: How Confidence Works

```
WebSocket Tick → CandleAggregator → ORBEMAVWAPStrategy.on_candle_close()
                                              ↓
                                   6-Factor Confidence Score (0-100)
                                   ┌──────────────────────────────┐
                                   │ 1. ORB breakout strength      │ 0-20
                                   │ 2. EMA trend separation       │ 0-20
                                   │ 3. VWAP position quality      │ 0-20
                                   │ 4. RSI entry zone             │ 0-20
                                   │ 5. Volume vs avg (confirm)    │ 0-20
                                   │ 6. Risk:Reward ratio          │ 0-20
                                   └──────────────────────────────┘
                                              ↓
                              confidence >= 70?  ──NO──→ HOLD (no alert)
                                              ↓ YES
                                   TradeSignal (BUY/SELL)
                                              ↓
                                   Telegram Alert (rich format)
                                              ↓
                              ┌── PLACEHOLDER: Auto-execution ────┐
                              │ (commented out in main.py)        │
                              │ Uncomment to enable live trading  │
                              └───────────────────────────────────┘
```

---

## Telegram Alert Format (what you receive)

```
⚡ TRADE ALERT — 10:32:15
───────────────────────────────────
📊 Stock:     SBIN-EQ
📈 Direction: 🟢 LONG (BUY)

ACTION REQUIRED:
───────────────────────────────────
🎯 Buy/Sell at:   ₹455.20  (market order)
🛑 Stop Loss:     ₹451.60  (−₹3.60 from entry)
✅ Target:        ₹464.20  (+₹9.00 from entry)
📐 Risk:Reward:   1 : 2.50
⏳ Valid for:     15 min

Confidence: 82/100 — 🔥 VERY HIGH
───────────────────────────────────
  Orb Breakout         18/20
  Ema Trend            16/20
  Vwap Position        20/20
  Rsi Quality          15/20
  Volume               20/20
  Rr Ratio             15/20

📌 LONG | Score=82/100 | Close=₹455.20 > ORB_H=₹453.10 ...
───────────────────────────────────
⚠️ This is a suggestion. You execute manually.
   Place SL order immediately after entry.
```

---

## Confidence Thresholds

| Score | Label | Action |
|-------|-------|--------|
| 85–100 | 🔥 VERY HIGH | Strong alert — act quickly |
| 70–84 | ✅ HIGH | Alert sent — good setup |
| 55–69 | ⚠️ MODERATE | NOT alerted (below threshold) |
| 0–54 | ❌ LOW | NOT alerted |

**Threshold is 70 by default** — configurable via `min_confidence` in strategy kwargs.

---

## Key Design Principles

1. **Strategy never calls API** — emits `TradeSignal` objects only
2. **WebSocket thread only enqueues ticks** — no logic in callbacks
3. **Only one alert per candle per symbol** — `alerted_tokens` set prevents duplicates
4. **No setup? Say so honestly** — "No Setup Found" message at 14:45 if nothing fired
5. **Auto-execution = placeholder** — clearly commented in `main.py`, `signal_engine.py`

---

## Next Steps for New Agent/Session

### Immediate (user action needed)
1. Fill in `config/secrets.env` with real credentials
2. Enable TOTP in Angel One account settings; save the Base32 secret key
3. Set `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` in secrets.env

### Testing sequence
1. `python -m auth.session_manager` — verify headless login works
2. `python -m data.instrument_master` — verify instrument master downloads
3. `python -m backtest.engine` — validate strategy on 6 months of data
4. `python main.py` — run notification-only live (paper mode)
5. After 2–4 weeks of paper alerts: enable auto-execution (see placeholders in main.py)

### To enable auto-execution later
Search for `# PLACEHOLDER:` in `main.py` — there are 6 commented blocks.
Uncomment them in order (risk check → size → place → track → position → summary).
The execution infrastructure (`OrderManager`, `CircuitBreaker`, etc.) is fully implemented
in `execution/` and `risk/` — just not wired to the live loop yet.

---

## Environment Variables Required
```
ANGEL_API_KEY=
ANGEL_CLIENT_ID=
ANGEL_PASSWORD=
ANGEL_TOTP_SECRET=       # Base32 secret (NOT the QR screenshot)
TELEGRAM_BOT_TOKEN=      # Required for alerts
TELEGRAM_CHAT_ID=        # Your personal Telegram chat ID
```

---

## File Map
```
angel-algo/
├── CONTEXT.md                    ← YOU ARE HERE
├── README.md
├── requirements.txt
├── main.py                       ← NOTIFICATION-ONLY ORCHESTRATOR
├── config/
│   ├── secrets.env.example
│   └── settings.yaml
├── auth/
│   ├── session_manager.py        ← headless login, retry, token storage
│   └── totp_generator.py
├── data/
│   ├── instrument_master.py      ← daily download + token lookup
│   ├── historical_fetcher.py     ← OHLCV fetch with rate limit
│   └── websocket_feed.py         ← live feed + candle aggregator
├── strategy/
│   ├── strategy_base.py          ← Signal, TradeSignal (with confidence)
│   ├── indicators.py             ← pure functions: EMA, RSI, ATR, VWAP, ORB
│   └── signal_engine.py         ← ORB+EMA+VWAP+RSI+Vol confidence scoring
├── execution/                    ← PLACEHOLDER (fully implemented, not wired live)
│   ├── order_manager.py
│   ├── order_tracker.py
│   └── position_manager.py
├── risk/                         ← PLACEHOLDER (fully implemented, not wired live)
│   ├── position_sizer.py
│   └── circuit_breaker.py
├── backtest/
│   └── engine.py                 ← event-driven replay, same strategy code
├── monitoring/
│   ├── logger.py                 ← JSON-lines + SQLite
│   └── alerts.py                 ← rich Telegram alerts (no-setup-found + trade alerts)
└── scheduler/
    ├── daily_runner.sh
    └── angel-algo.service
```

---

## Known Angel One Gotchas
- Instrument tokens change on corporate actions — master re-downloaded daily
- TOTP must be enabled once in Angel One account settings
- Historical data rate-limited: fetcher has 0.5s delay + retry
- SDK pinned: `smartapi-python==1.3.4` — check changelog before upgrading
- TOTP is time-sensitive — ensure VM clock is NTP-synced
