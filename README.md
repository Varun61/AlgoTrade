# Angel One SmartAPI Intraday Algo Trader

Production-grade, fully automatable intraday algo trading system for Angel One SmartAPI.
**Strategy: ORB + EMA 9/21 + VWAP filter + RSI filter + ATR-based stops**

> ⚠️ **This is real money software. Always backtest first. Start in paper mode. Scale gradually.**

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
