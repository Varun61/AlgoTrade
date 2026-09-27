# Angel One SmartAPI Algo Trading

> ⚠️ **This is real money software. Always backtest first. Start in paper mode. Scale gradually.**

---

## 📍 Current direction (read this first)

This project was built around an **intraday equity ORB strategy** (below). We then
built a proper portfolio backtester and validated it (and ~12 other technical
strategies) over **5 years of data**. The honest finding: **no public technical
intraday pattern had an edge that survives costs** (gross profit factor ≈ 1.0
across ORB, VWAP-reversion, gap, prev-day momentum, supertrend, etc.).

We then tested the **volatility risk premium** and found a real, structural edge:
NIFTY implied vol exceeds realized vol ~79% of days. A **defined-risk weekly iron
condor** (sell ~2% OTM, buy 1% wings) was positive in a synthetic backtest **every
year 2021–2026**, at affordable margin. It is now running in **PAPER mode on real
prices** to validate before any live trading. See [`PLAN.md`](PLAN.md) for the full
research trail, results, and the confidence-gated plan.

### Run the options paper system (one command, once a day after 3:35 PM IST)
```bash
cd /path/to/AlgoTrade && source .venv/bin/activate && python -m tools.options_daily
```
It does everything itself (collect real option data → open the weekly paper
condor at real prices → settle at expiry → print running P&L). **No live orders.**
Idempotent — safe to run repeatedly. Schedule via `scheduler/options_runner.sh`
at **15:35 IST / 10:05 UTC, Mon–Fri** (NOT pre-market — prices would be stale).

- Paper trades log: `logs/options_paper.jsonl`
- Check summary: `python -m tools.run_options_paper --action status`
- Real option data collector (also run daily): `python -m tools.collect_options`

Key modules: `strategy/options_pricing.py` (Black-Scholes), `backtest/options_bt.py`
(synthetic iron-condor backtester), `execution/options_paper.py` (paper engine),
`tools/options_daily.py` (the one-command runner).

---

## Legacy: Intraday Equity ORB system (kept, not profitable)

Fully automatable intraday algo for Angel One SmartAPI.
**Strategy: ORB + EMA 9/21 + VWAP filter + RSI filter + ATR-based stops.**
Retained for the backtester and as reference; it has **no validated edge** and
live trading is hard-gated off (`trading.live_trading_authorized: false`).

---

## Quick Start

### 1. Setup
```bash
cd angel-algo
python -m venv .venv
# Windows:  .venv\Scripts\activate
# Linux/Mac: source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure credentials
```bash
cp config/secrets.env.example config/secrets.env
# Edit config/secrets.env with your Angel One credentials
```

### 3. Test auth (Phase 1 validation)
```bash
python -m auth.session_manager
```

### 4. Download instrument master
```bash
python -m data.instrument_master
```

### 5. Run backtest (Phase 3 — do this before any live trading)
```bash
python -m backtest.engine
```

### 6. Run live (paper mode by default)
```bash
python main.py
```

> Set `trading.mode: live` in `config/settings.yaml` only after paper trading for 2–4 weeks.

---

## Kill Switch
```bash
touch .killswitch        # Linux/Mac
New-Item .killswitch     # Windows PowerShell
```
The system will detect this file within 1 second and shut down cleanly.

---

## Execution Order (follow this sequence)

| Step | What to do | Command |
|------|-----------|---------|
| 1 | Test headless login | `python -m auth.session_manager` |
| 2 | Download instrument master + check tokens | `python -m data.instrument_master` |
| 3 | Fetch historical data + plot candles | `python -m data.historical_fetcher` |
| 4 | Run 6-month backtest, review metrics | `python -m backtest.engine` |
| 5 | Live feed: signal-only mode (no orders) | Set `mode: paper` + run `main.py` |
| 6 | Add real orders with tiny qty (1 share) | Set qty limits in `settings.yaml` |
| 7 | Paper trade 2–4 weeks, review daily logs | — |
| 8 | Scale capital gradually | Update `capital` in `settings.yaml` |

---

## Architecture

```
WebSocket Ticks → CandleAggregator → Strategy → TradeSignal
                                                     ↓
                                          CircuitBreaker.can_trade()?
                                                     ↓
                                          PositionSizer.compute_qty()
                                                     ↓
                                          OrderManager.place_order()
                                                     ↓
                                          OrderTracker → PositionManager
                                                     ↓
                                          AlgoLogger + TelegramAlerter
```

**Core principle: strategy never talks to the API. It only emits TradeSignal objects.**

---

## Configuration

All parameters are in [`config/settings.yaml`](config/settings.yaml). Key ones:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `trading.mode` | `paper` | `paper` or `live` |
| `trading.capital` | 100000 | Trading capital in INR |
| `risk.per_trade_risk_pct` | 1.0 | % of capital risked per trade |
| `risk.daily_loss_limit_pct` | 2.0 | Circuit breaker daily loss % |
| `strategy.orb_minutes` | 15 | Opening range window |
| `strategy.ema_fast/slow` | 9/21 | EMA periods |

---

## Monitoring

- **Logs**: `logs/algo.jsonl`, `logs/algo.db` (SQLite)
- **Telegram**: Set `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` in `secrets.env`
- **Kill switch**: create file `.killswitch` in project root

---

## Deployment (Cloud VM)

```bash
# On EC2/GCP VM (Ubuntu):
sudo cp scheduler/angel-algo.service /etc/systemd/system/
sudo systemctl enable angel-algo
sudo systemctl start angel-algo

# Or via cron (8:55 AM IST = 3:25 UTC):
# 25 3 * * 1-5 /home/ubuntu/angel-algo/scheduler/daily_runner.sh
```

---

## Angel One Gotchas

1. **Token changes**: Instrument tokens change on corporate actions — master is re-downloaded daily
2. **TOTP**: Enable TOTP once in Angel One account settings, save the Base32 secret
3. **Rate limits**: Historical data has per-second limits — fetcher includes 0.5s delay + retry
4. **SDK version**: Pinned to `smartapi-python==1.3.4` — check release notes before upgrading
5. **Clock sync**: TOTP is time-sensitive — ensure your VM uses NTP (`timedatectl status`)

---

## Context File

See [`CONTEXT.md`](CONTEXT.md) for full build status, design decisions, and next steps for any agent resuming this project.
