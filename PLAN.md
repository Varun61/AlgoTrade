# Plan: Path to Safe Auto-Execution (Paper → Live)

Move AlgoTrade from notification-only (top-2 Telegram alerts) to auto-executing trades, staged as: validate in paper mode → go live with small, protected capital and a consistent concurrent-position/risk budget. No live web-search tool was available; SEBI/compliance specifics are from trained knowledge and flagged for verification with the broker.

## Current state (verified in code)
- Watchlist: ~90 NSE symbols (symbols.txt → tools/build_watchlist.py → config/settings.yaml tokens)
- Signals scored 0-100 (6 factors); `MIN_CONFIDENCE_THRESHOLD=70` (strategy/signal_engine.py#L32)
- main.py buffers entry signals 3s, sorts by confidence, sends only **top 2** (main.py#L246)
- Auto-execution fully wired but disabled: `trading.auto_execute: false` (config/settings.yaml); gated at main.py#L85-L86
- OrderManager (execution/order_manager.py) has idempotency (client_ref dedup) + retry/backoff for live `placeOrder`; paper mode simulates instant fills
- PositionSizer (risk/position_sizer.py#L58): `qty = floor(capital * risk_pct/100 / stop_distance)` — sizes each trade to risk a fixed **% of capital**, not an equal ₹ capital slice. Keep this model (already backtested); do NOT switch to splitting capital into N equal parts.
- CircuitBreaker (risk/circuit_breaker.py): daily loss 2% (L48), max 10 trades/day (L49), max 4 consecutive losses (L50), max 3 concurrent positions (L51) — all % or count based, no ₹ floor
- Backtest engine already models slippage (0.05%) + brokerage (₹40/order) realistically

## ⚠️ Key finding
With very small capital (e.g. ₹1,000) and 0.5-1% per-trade risk, `risk_amount` (₹5-10) is smaller than the ATR-based stop distance for most liquid NSE stocks → `PositionSizer.compute_qty()` returns **0** most of the time. A tiny live capital pool mostly won't generate any live trades under the current risk model.

## Capital allocation / concurrent positions (decided)
User asked whether ₹25,000 should be split into N equal parts (e.g. 10 x ₹2,500) and traded that way.
**Recommendation: no.** Reasons:
- Flat ₹40/order brokerage eats a much bigger % of a small slice (1.6% of a ₹2,500 slice vs 0.48% of an ₹8,300 slice).
- The signal engine realistically produces only ~2 high-confidence (≥70) setups on a typical day — 10 slots would mostly sit idle in cash.
- Risk-based sizing (existing PositionSizer) already normalizes risk per trade regardless of price; capital-splitting is a worse, redundant model.

**What actually matters is how many positions are open *at the same time*.** Existing caps:
`max_concurrent_positions = 3`, `max_trades_per_day = 10` (sequential total, not concurrent).

There's a consistency gap: 3 concurrent × 0.5% risk each = 1.5% worst-case same-day loss, which exceeds the
1.0% daily-loss cap chosen for the live pilot (see Phase 2 below). Fix:
- **During the live pilot: `max_concurrent_positions = 2`**, not 3. 2 × 0.5% = 1.0%, exactly matching the tightened daily-loss cap — no silent gap in the safety math.
- After the pilot proves stable, raise back to 3 concurrent, paired with either per-trade risk ~0.33% or a relaxed daily-loss cap of 1.5% (decide later, not now).
- `max_trades_per_day = 10` stays as-is (turnover cap, independent of concurrency).

## Phases

### Phase 1 — Paper-mode validation
1. Flip `trading.auto_execute: true`, `trading.mode: paper` in config/settings.yaml, capital set to the eventual live target (₹25,000 — see Phase 2) so sizing math matches reality. **Applied**: `max_concurrent_positions` kept at **3** per explicit user decision (not lowered to 2) — the existing `daily_loss_limit_pct` circuit breaker still caps worst-case same-day loss regardless of how many positions are technically allowed open.
2. Run unattended for 3-4 weeks (~15-20 trading days).
3. Daily reconciliation via the already-built `tools/evaluate_pnl.py` (parses monitoring/alerts.py logged alerts) against likely real fills from historical data.
4. Exit criteria: circuit breaker verified to trip at least once, zero unhandled exceptions, paper P&L directionally consistent with backtest.

### Phase 2 — Capital & risk model for live (*depends on Phase 1 passing*)
- **Live starting capital: ₹25,000** (not ₹1,000 — avoids the qty=0 problem; not ₹50,000 — pilot goal is validating mechanics, not maximizing P&L)
- **per_trade_risk_pct: 0.5%** for the first ~2 weeks live (half the backtested 1.0%), stepping up to full 1.0% once live behavior matches paper
- **daily_loss_limit_pct: 1.0%** for the same pilot window (tighter than default 2.0%), relaxed back to 2.0% once stable
- **max_concurrent_positions: 2** for the pilot (see Capital Allocation section) — raise to 3 only after the pilot is stable, together with a matching risk/daily-loss adjustment
- New **capital-floor circuit breaker** in risk/circuit_breaker.py: track `day_start_capital`; block new entries if realized+worst-case-unrealized would breach `day_start_capital - buffer`. Explicitly a best-effort soft safety net, **not a guarantee** — gaps/slippage past SL can still breach it.

### Phase 3 — Compliance (Angel One / SEBI) (*parallel with Phase 1/2, hard gate before live*)
1. Confirm with Angel One whether API auto-trading requires static-IP whitelisting and/or broker-side "Algo ID" order tagging (SEBI's algo-trading framework for retail API users) — verify current process directly with the broker.
2. If required, add the tag to the `params` dict in execution/order_manager.py#L96-L107.
3. Do not flip `trading.mode: live` until this is confirmed complete.

### Phase 4 — Live pilot & ops (*depends on Phase 2 + 3*)
1. Flip `trading.mode: live` with Phase-2 capital/risk/concurrency settings.
2. Add a manual kill-switch checked each loop iteration in main.py (e.g. a `config/KILL_SWITCH` file or settings flag).
3. Daily reconciliation via `tools/evaluate_pnl.py` against SmartAPI's actual order/trade book.
4. Confirm existing alerts (circuit break, order placed/failed/closed, session start/stop, errors) fire correctly during the pilot.
5. After a stable pilot window, gradually raise capital / risk_pct / max_concurrent_positions together, keeping the worst-case-simultaneous-loss math consistent with the daily-loss cap.

## Strategy backlog / future exploration
- **VWAP Mean Reversion** (candidate, not built): fade price back toward VWAP when it's extended by k std-dev — same equity universe, same risk engine (`risk/position_sizer.py`, `risk/circuit_breaker.py`) as ORB, just a new strategy class alongside `ORBEMAVWAPStrategy` in `strategy/signal_engine.py`. VWAP is already computed in `strategy/indicators.py`, currently only used as an ORB directional filter, not its own entry signal. To be prototyped/backtested only *after* the ORB pilot shows positive expectancy.
- **09:20 AM Short Straddle**: rejected for now — requires options (ATM Call+Put), a different instrument type than the current equity-only stack (`data/instrument_master.py`, `execution/order_manager.py`), has theoretically unbounded risk on the short strikes, and there's no options-Greeks/margin risk model in `risk/` to support it.
- **Scalping / Momentum Grid**: rejected for now — needs tick-level/sub-second execution; current loop runs on 15-min candles with a 3-second signal buffer, and Angel One's per-trade brokerage would likely erase the edge on rapid micro-trades.
- Applied today as part of Phase 1 tuning (already live in code/config, not yet re-validated with fresh paper data): `atr_target_multiplier` 2.5→3.0 (wider R:R), `max_holding_candles` 8→12, plus a new cascading early-exit (`early_cut_candles: 5`, `early_cut_min_r: 0.3` in `strategy/signal_engine.py`) that cuts stalled trades early instead of waiting out the full holding window, based on real alert data showing most exits were time-stops that cut winners short.

## Relevant files
- `config/settings.yaml` — capital, risk_pct, daily_loss_limit_pct, max_concurrent_positions knobs
- `risk/circuit_breaker.py` — capital-floor check, tighter live-pilot thresholds, concurrency cap
- `risk/position_sizer.py` — reference only; qty=0 issue originates in `compute_qty()` (L58)
- `main.py` — add kill-switch check
- `execution/order_manager.py` — Algo ID param in live `place_order()` params if required (L96-107)
- `tools/evaluate_pnl.py` — reuse for paper-validation and live reconciliation
- `tests/test_risk.py` — capital-floor and concurrency-cap test cases

## Verification
1. `pytest tests/ -v` after each phase (repo currently at 71 passed)
2. Phase 1: 3-4 week paper run, zero unhandled exceptions, ≥1 verified circuit-breaker trip
3. Phase 2: manual forced losing-streak test confirms capital-floor breaker halts new entries, and confirms 2-concurrent × 0.5% risk matches the 1.0% daily-loss cap in practice
4. Phase 4: cross-check `tools/evaluate_pnl.py` vs SmartAPI trade book for first live week

## Decisions
- Rollout order: paper-mode validation first, then live money
- Diversification/sector-cap logic: dropped from scope per user request — not part of this plan
- Capital: "never end day below start" treated as best-effort capital-floor breaker, not a guarantee
- Compliance section included per request; exact SEBI/Angel One process needs direct broker verification
- Capital/risk finalized per AI recommendation: ₹25,000 start, 0.5%→1.0% risk ramp, 1.0%→2.0% daily-loss ramp
- Capital allocation: risk-based sizing kept as-is (no equal-parts capital split); concurrency capped at 2 during the pilot to keep worst-case daily loss consistent with the 1.0% cap
