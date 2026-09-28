# Plan: Validate an Edge Before Any Live Trading

> **Status change (this revision):** A proper portfolio backtester was built and
> run over the full cached year. **The current ORB+EMA+VWAP+RSI strategy has
> negative expectancy in every quarter tested.** The plan is no longer "paper →
> live"; it is "find a real edge first". Live trading is now hard-gated off in
> code (`trading.live_trading_authorized: false`).

---

## STRATEGY LEADERBOARD — everything tested (1yr cache, ~290 NSE names, real costs)

Best profit factor (PF) per family. PF > 1.0 = breakeven; need > ~1.3 to trade.
**None cleared 1.0 durably.** Fades/reversion (0.77–0.80) beat breakout/
continuation (0.33–0.62) — i.e. on this data momentum is harmful and reversion
is only mildly protective. The one "profitable" result (daily swing H2) was
market beta, not edge (it lost in H1).

| Strategy (best config)                     | PF   | Win% | Verdict            |
|--------------------------------------------|------|------|--------------------|
| ORB breakout (original)                    | 0.44 | 33%  | neg every quarter  |
| ORB + fakeout filters (retest/confirm/range)| 0.67*| 43%  | *overfit (0.29–1.09 by qtr) |
| ORB 1:1 range-sweep                        | 0.62 | 39%  | negative           |
| VWAP mean-reversion (intraday)             | 0.46 | 42%  | negative           |
| Intraday momentum (1st-30m→close)          | 0.48 | 35%  | negative           |
| Gap fade                                   | 0.57 | 34%  | negative           |
| Gap + first-candle continuation ("9:16")   | 0.80 | 42%  | negative (best-ish)|
| Prev-day momentum continuation (leaders)   | 0.58 | 28%  | negative           |
| Prev-day reversal / fade                   | 0.77 | 36%  | negative (best fade)|
| Afternoon 2PM breakout                     | 0.33 | 31%  | negative (worst)   |
| Supertrend + intraday strength             | 0.79 | 39%  | negative           |
| Inside-bar / NR7 breakout                  | 0.69 | 42%  | negative           |
| Daily swing RSI(2) (positional/overnight)  | 1.12 H2 / 0.14 H1 | 51–64% | regime-dependent = beta |

**X-post strategies skipped, with reasons:** ICT liquidity-sweep + IFVG
(discretionary/multi-timeframe, not faithfully mechanizable); EMA + premarket
high/low (no premarket data in the cache — starts 09:15); pullback-to-HOD/LOD
with hammer/engulfing (discretionary candlesticks — tested mechanical proxies);
2PM options CE/PE version (options Greeks/liquidity not modeled — tested the
underlying-equity breakout proxy, PF 0.33); ORB "which extreme formed first"
(needs sub-15-min data).

### ⚠️ MULTI-YEAR (5yr, 2021-2026) VALIDATION — the definitive result
With the 5-year data (Phase 1 done), the best two intraday leads were validated
across every calendar year AND cost-decomposed:

- **Regime-switched prev-day** (continuation in up-regimes / fade in down): PF
  0.52-0.66 in *every* year 2022-2026. The regime switch actually HURT — always
  fading beat switching to continuation in all years. The user's "trade
  yesterday's leaders (continuation)" hypothesis is empirically wrong intraday.
- **Pure fade** (fade yesterday's biggest movers): PF 0.61-0.73 every year,
  remarkably consistent (win ~42%). A *genuine, stable* behavioral effect — but
  still net-negative.
- **Cost decomposition (the decisive test):** fade GROSS profit factor (zero
  costs, zero slippage) = **0.96** — still below breakeven. avg win ₹78 vs avg
  loss ₹58 at 42% win. **The raw edge is ~zero; costs then make it clearly
  losing.** No execution improvement can create an edge that isn't there.

**Conclusion:** across 13 strategies, 5 years / multiple regimes, and full cost
decomposition, NO public-technical intraday strategy on this NSE universe has a
gross edge large enough to survive costs. This is now a robust, multi-regime,
cost-adjusted finding — not a one-year artifact.

### ✅ BREAKTHROUGH — OPTIONS PREMIUM-SELLING has a real edge (unlike chart patterns)
After all directional strategies failed, we tested the volatility-risk-premium:
- **VRP probe (NIFTY + India VIX, 2021-2026):** implied 1-day move (0.95%) >
  realized (0.63%); IV exceeded realized on **78.8% of days**. A genuine
  structural edge — options are systematically overpriced.
- **Synthetic weekly iron-condor backtester** (`backtest/options_bt.py`, Black-
  Scholes priced with VIX as IV, expiry intrinsic settlement, defined-risk
  wings, costs, trading-day time convention):
  - Iron-fly (sell ATM): marginal (+₹43k, sharpe 0.14, negative 2026).
  - **Iron-condor (sell ~2% OTM, 1% wings): +₹196k, win 82%, sharpe 1.35,
    margin ~₹13k, and POSITIVE EVERY YEAR 2022-2026.** First strategy in the
    whole project that is consistent across regimes with affordable, DEFINED
    risk (fits ₹25k capital).
  - Naked straddle scored higher (sharpe 1.71) but needs ~₹238k margin and has
    uncapped tail risk — not viable/safe at this capital.

**CRITICAL caveats (why this is a validated hypothesis, NOT yet tradeable):**
1. SYNTHETIC prices (BS + VIX). Real options have **skew** (OTM puts pricier
   than symmetric BS → less put credit) and **fat tails** (real crashes breach
   short strikes far more than lognormal → the model UNDERSTATES tail losses).
   An 82% win rate is exactly the profile that hides tail risk.
2. Weekly-grouping is an expiry proxy; no real bid/ask, liquidity, or intra-week
   gap path modeled.
3. Angel's master is live-only → no historical option data to backtest for real.

**Next steps to confirm (before any real money):**
1. **Forward-collect real NIFTY option data** (start now; the fetcher works on
   live option tokens) and re-run the condor on REAL prices over several weeks.
2. Stress-test explicitly against the worst historical gap weeks with fat-tail
   assumptions, not lognormal.
3. Paper-trade the defined-risk iron condor; only then consider live, small.
This is the one direction with a real edge — pursue it with real-data validation.

### PAPER-MODE OPTIONS SYSTEM (built — real-data validation, NO live orders)
To validate the condor on REAL prices (the synthetic BS+VIX result is only
directional), a paper-trading pipeline is now in place:

- **`tools/collect_options.py`** — daily forward-collector. Fetches real NIFTY
  weekly option candles (ATM ± N strikes, CE+PE) into `data/.cache/options/`,
  idempotent. Run daily to build the real dataset.
- **`execution/options_paper.py`** — paper iron-condor engine (leg selection,
  entry credit, defined-risk expiry P&L, JSONL book). Pure + fully tested.
- **`tools/run_options_paper.py`** — paper runner (NO orders):
    - `--action open`  : opens a paper condor for the nearest expiry at REAL LTP.
    - `--action settle`: settles expired condors vs real NIFTY close.
    - `--action status`: running paper P&L summary.
  Logs to `logs/options_paper.jsonl`.

**How to run it forward (suggested cron, IST):**
```
# collect real option data daily after close
30 15 * * 1-5  cd <repo> && .venv/bin/python -m tools.collect_options
# open the weekly paper condor Monday morning; settle on expiry afternoon
05 10 * * 1    cd <repo> && .venv/bin/python -m tools.run_options_paper --action open --offset-pct 2.0 --wing-pct 1.0
35 15 * * 2,4  cd <repo> && .venv/bin/python -m tools.run_options_paper --action settle
```
Verified end-to-end live: opens with real premiums, logs P&L, places no orders.

### Confidence-gated cleanup (NOT done yet — per user)
Once the paper condor shows a positive, tail-survivable edge on REAL data over
several weeks, THEN: remove the failed intraday strategy code (ORB confidence
scoring, VWAP-reversion, research_intraday/xpost_extra strategies, swing) and the
now-unused intraday knobs in settings.yaml, and make the options condor the
primary system. Kept for now as the validated backtester + fallback; cleanup is
deliberately deferred until paper results justify committing to options only.

### Synthesis / root cause
Opposite strategies (momentum AND mean-reversion) both lose → at 15-min on this
liquid universe, price is ~unpredictable **net of costs**. Universal signature:
~30–45% win, avg-loss > avg-win. Two real blockers: **(1) data** — 1 year is a
single regime and 15-min is too coarse for several setups' entry timing;
**(2) cost drag + efficiency** — small intraday moves vs fixed round-trip costs on
signals everyone can see.

### Agreed path forward (see end of file for the phased plan)
1. **Data upgrade** — ✅ DONE. `tools/fetch_history.py` (resumable) pulled **5
   years of 15-min** data for all 302 symbols into `data/.cache/hist_15m/`
   (260 with full 5yr, 42 shorter = recent listings; 0 empty/corrupt). Angel
   serves ~5yr of 15-min and ~1yr+ of 1-min. Backtester now takes `cache_dir`.
   Spans the 2022 correction + 2023-24 bull + 2025-26 → real regime variation.
2. **Regime signal** (equal-weight index vs MAs) + relative-strength ranking +
   volume filters + limit-order execution modeling.
3. **Test the prioritized lead**: a *regime-switched prev-day* strategy
   (continuation in uptrends, fade in down/chop; break-of-yesterday's-high entry;
   RS selection) — the synthesis of the user's idea + our fade finding.
4. **Decision gate**: PF > ~1.3 across multiple regimes out-of-sample → paper →
   live; else pivot (options/microstructure/alt-data) or stay notification-only.

---

## What changed and why

### 1. A real backtester now exists
The old `backtest/engine.py` was **single-symbol only** — it ran each symbol
independently with the *full* capital, so it could never model the portfolio
dynamics the config actually controls (concurrency, top-N confidence ranking,
rotation), and it required a live Angel One login. Its aggregate P&L was, by its
own docstring, "not a realistic portfolio P&L".

New: **`backtest/portfolio.py`** (+ runner `tools/run_portfolio_backtest.py`).
It replays the whole watchlist on one shared timeline and reproduces the live
`main.py` model: one shared capital pool + circuit breaker, `max_concurrent`
cap, per-candle confidence ranking, optional rotation, `new_entry_cutoff` /
EOD square-off, the **same** `execution/position_manager.zerodha_intraday_costs`
cost model used live, plus entry/exit slippage, intrabar (high/low) stop/target,
and the exact `ORBEMAVWAPStrategy` code path. It runs offline from the 302
cached 15-min CSVs in `data/.cache/backtest/` — no login needed.

### 2. The verdict (portfolio backtest, cleanest config)
Cleanest config = no `early_cut`, no discretionary EMA-flip exit, wider trail,
`min_confidence` 75, `max_concurrent` 3, VWAP bug fixed:

| Period      | Trades | Win% | Profit Factor | Return |
|-------------|-------:|-----:|--------------:|-------:|
| FULL YEAR   |  1338  | 33.9 | 0.44          | −93.8% |
| 2025 Q4     |   311  | 32.5 | 0.38          | −29.6% |
| 2026 Q1     |   347  | 35.2 | 0.51          | −31.7% |
| 2026 Q2     |   343  | 35.3 | 0.48          | −32.4% |
| 2026 Q3     |   311  | 32.5 | 0.39          | −33.5% |

Negative in **every** quarter. Profit factor ~0.44 (needs > 1.0 to break even,
> ~1.3 to be worth trading net of costs). Both longs and shorts lose, so it is
not a directional-regime artifact — 15-min opening-range breakouts on a broad
~300-name NSE universe get faded intraday.

### 3. Root cause (why no lever fixed it)
- **Win rate ~33% with realized avg-loss > avg-win** = double-negative. Losers
  ran to the full 1.5-ATR stop; winners were capped well below the ATR target.
- **The confidence score is not predictive.** In both the live alert logs and
  the backtest, raising `min_confidence` did *not* raise win rate — the 80+
  bucket did *worse* (PF 0.33). The 8-factor score is mostly noise.
- Every exit/target/trail/time-stop/concurrency permutation stayed net-negative.
  This is an **entry-quality** problem; exit tuning cannot rescue it.

---

## Changes applied in this revision (all validated, 122 tests green)

### Correctness / safety fixes (unambiguously good regardless of edge)
- **VWAP intraday-reset bug fixed** (`strategy/signal_engine.py`). `history` has
  an integer index, so `vwap()` was silently using its cumulative fallback and
  computing a ~30-day VWAP instead of a per-session one — meaning `vwap_filter`
  and the `vwap_position` factor were comparing price to a slow multi-day mean,
  not the intraday VWAP. Now re-indexed by timestamp so it resets each day.
- **Paper-fill key fix** (`execution/order_manager.py`): paper orders stored
  `avgprice`, but `OrderTracker` reads `averageprice`/`filledshares` — so paper
  fills reported 0. Aligned to the live `orderBook()` keys.
- **Capital-floor circuit breaker** (`risk/circuit_breaker.py`, config
  `risk.capital_floor_buffer_pct`): worst-case-aware soft gate that blocks a new
  entry if realized + open worst-case loss would breach a % of the day's start
  capital. Backward-compatible signatures; 6 new tests.
- **EOD square-off now logs EXIT records** (`main.py`) — previously EOD/kill
  closes never wrote an EXIT to `alerts.jsonl`, leaving ~91 entries "open" for
  `tools/evaluate_pnl.py`.
- **Hard live gate** (`main.py` + `trading.live_trading_authorized`): refuses to
  start in `mode: live` unless explicitly authorized, and refuses live while
  `data_collection_mode` is on.

### Config set to the least-harmful RESEARCH profile (`config/settings.yaml`)
- `early_cut_candles: 3 → 0` — the single most destructive setting (875 trades @
  2% win, −₹21k on one run; cut trades at median −0.03R, 44% while still green).
- `ema_exit: false` (new) — the discretionary EMA-flip exit capped winners.
- `trail_atr_mult: 1.0 → 2.5` — 1-ATR trail stopped winners on normal pullbacks.
- `min_confidence: 60 → 75` — cut overtrading (not a win-rate fix; score is weak).
- `max_concurrent_positions: 10 → 3` — 10 was inconsistent with the 2% daily cap.
- `atr_target_multiplier: 3.0 → 2.5` — marginally more reachable target.
- `allow_position_rotation: true → false` — rotation lost in logs+backtest and
  leans on the non-predictive confidence score.

These make the *paper* research profile clean; **they do not make it profitable.**

---

## Research roadmap (the actual path to a tradeable system)

Do these in order. Use `tools/run_portfolio_backtest.py` — a change is only
"done" when it shows a positive profit factor across multiple quarters net of
costs, not just on one window.

### Phase A — ORB entry-quality experiments: TESTED, NO EDGE
1. **Fakeout filters** (`require_orb_retest`, `confirmation_candles`,
   `orb_range` band): swept. Best was `retest_range_conf80` — PF 0.67 on H1 2026,
   but cross-quarter it was 0.56 / 1.09 / 0.53 / 0.29 (one lucky quarter, overfit
   to the window). `confirm2` was stable but capped at ~0.6 PF, never > 1.0.
2. **Shrink the universe**: the watchlist is large-caps first, so the 25–40
   symbol runs already WERE the liquid subset; `top12_liquid_retest` was PF 0.43.
   Shrinking did not create an edge.
3. **Confidence score**: deferred. A score only helps rank trades *within* a
   positive-edge signal; with no base edge yet, rebuilding it is premature.
   (Already stopped relying on it: gate lowered, rotation disabled.)

### Phase B — Alternative strategy — VWAP mean-reversion: TESTED, ALSO NO EDGE
Built `strategy/vwap_reversion.py` (short when price is stretched > k·ATR above
the session VWAP + overbought in a low-ADX/ranging regime; long mirror; target =
reversion toward VWAP). Swept band (1.0/1.5/2.0 ATR), ADX cap, RSI extremity,
stop width, and target fraction over 40 symbols, H1 2026:

| variant                 | trades | win% | PF   |
|-------------------------|-------:|-----:|-----:|
| band 1.0                |   780  | 38.5 | 0.41 |
| band 1.5                |   627  | 36.0 | 0.42 |
| band 2.0                |   452  | 33.6 | 0.45 |
| band1.5 + ADX≤20        |   383  | 33.4 | 0.34 |
| band1.5 + RSI 30/70     |   302  | 34.4 | 0.43 |
| band1.5 + wider stop    |   530  | 42.3 | 0.45 |
| band1.5 + full-VWAP tgt |   605  | 34.5 | 0.46 |

Every variant PF 0.26–0.46. Nothing cleared 1.0, so nothing was worth
cross-quarter validation.

### ⚠️ Synthesized finding: no simple technical edge at 15-min on this universe
Trend-continuation (ORB) **and** its opposite, mean-reversion (VWAP), both lose
on the same data. If breakouts don't continue and stretches don't revert, then
at 15-min granularity on this ~300-name universe, price is effectively
unpredictable **net of costs**. The tell is identical in every experiment:
**avg-loss (₹65–79) > avg-win (₹45–60)** at ~35% win rate. The targeted moves
are small 15-min swings, while a full 1-ATR stop + the fixed intraday round-trip
cost eat the edge. **The timeframe is too short for move-size to beat cost drag.**

### Phase C — Daily swing (positional): TESTED, regime-dependent, NO durable edge
Built `strategy/swing.py` (`DailySwingStrategy`): Connors-style RSI(2) pullback in
the direction of an SMA trend filter, ATR stop, signal-based exit. Added a
delivery cost model (`zerodha_delivery_costs`) and a `swing_mode`/daily-timeframe
path to the backtester (holds overnight, no square-off, resampled daily bars).

Results were far *better-behaved* than intraday (win rate > 50%, small drawdown),
and on a 60-name large-cap subset `rsi5_stop2` reached PF 0.87 — but:
- On the **full ~290-name universe** it fell to PF ~0.4–0.5 (the faint edge was a
  large-cap-subset effect, not universal).
- The **half-year consistency split is damning**: `rsi5_stop2.5` was PF **0.14**
  (win 32%) in Oct'25–Mar'26 and PF **1.12** (win 64%) in Apr'26–Sep'26.

That split is the whole story: buy-the-dip is **long-biased market beta** — it
prints money in a rising half-year and bleeds in a falling/choppy one. The H2
profit is not a durable, regime-independent edge.

### ⚠️ Overall conclusion after testing 3 strategy families
| family (best config)              | verdict            | PF    |
|-----------------------------------|--------------------|-------|
| ORB breakout (intraday, 15m)      | negative all Qs    | ~0.44 |
| VWAP mean-reversion (intraday)    | negative all params| 0.26–0.46 |
| Daily swing RSI(2) (positional)   | regime-dependent   | 0.14 / 1.12 |

No approach shows a **durable, regime-independent** edge on the available data.
The daily swing is the most promising *shape*, but its profitability is pure
market timing on a single favorable half-year.

### The real blocker: DATA, not strategy ideas
The cache is **one year** of 15-min data (~249 daily bars). That is a single
market regime. Any daily/swing result — good or bad — is curve-fit to
2025–2026 and cannot be trusted as an edge. Continuing to tune parameters on this
one window is overfitting, not research.

### Recommended path forward (requires a decision)
1. **Get multi-year daily data (5–10 years) via `data/historical_fetcher.py`**
   (Angel `getCandleData`, chunked) or another source, then properly validate the
   daily swing strategy across bull/bear/sideways regimes. This is the ONLY path
   with a real chance of confirming an edge.
2. **Add a market-regime filter to the swing strategy** (e.g. only take longs when
   the index / equal-weight universe is above its 200-day MA). This directly
   attacks the H1-type bloodbath — but must be validated on multi-year data, not
   this one year. `risk/market_regime.py` already exists as a starting point.
3. **Otherwise, keep the system notification-only** (its original design). Do not
   auto-execute — no strategy here has earned it.

Exhausted / not worth more effort on the current data: 15-min ORB and VWAP filter
tuning, and rebuilding the confidence score.

### Phase C — Only if Phase A or B yields PF > ~1.3 across quarters
1. Forward paper-trade the winning config 3–4 weeks; confirm live behavior
   matches backtest (fills, costs, circuit-breaker trips).
2. Angel One / SEBI compliance check (static-IP whitelisting, algo-ID order
   tagging) — verify directly with the broker; wire the tag into
   `execution/order_manager.py` `params` if required.
3. Flip `live_trading_authorized: true`, `mode: live`, `data_collection_mode:
   false`, start at ₹25k / 0.5% risk / 2 concurrent, scale only on stable P&L.

---

## How to reproduce the backtest
```bash
# Baseline (current config) + the exit/target/confidence A/B matrix:
python -m tools.run_portfolio_backtest --experiments --symbols 25 --start 2026-06-01

# Full config over the current settings.yaml on the whole cache:
python -m tools.run_portfolio_backtest --symbols 40
```
Outputs per-config trades / win% / profit factor / expectancy / return / maxDD
and an exit-reason + long/short breakdown.

## Key files
- `backtest/portfolio.py` — portfolio backtester (the source of truth now)
- `tools/run_portfolio_backtest.py` — runner + experiment matrix
- `backtest/engine.py` — legacy single-symbol engine (kept; regression-tested)
- `strategy/signal_engine.py` — strategy (VWAP fix, `ema_exit` toggle)
- `risk/circuit_breaker.py` — capital-floor guard
- `config/settings.yaml` — research profile + `live_trading_authorized` gate

## Decisions
- Live trading is hard-gated off until a positive backtested edge exists.
- Backtesting is now portfolio-level and offline (cached data), not single-symbol.
- Exit management is no longer the bottleneck; the entry signal is. Research
  effort goes to entry quality / a mean-reversion alternative, not exit tuning.

---
## REAL-DATA options verdict (definitive — supersedes the synthetic +₹196k)

We stopped guessing and backtested the weekly NIFTY iron condor on ACTUAL settled
option prices from NSE's F&O bhavcopy archive (533 trading days, 2024-08 .. 2026-09,
112 weekly cycles). Fetcher: `tools/fetch_nse_fo.py` -> `data/.cache/nse_fo/`.
Backtester: `backtest/options_real_bt.py` (real entry ClsPric + real expiry
intrinsic vs settlement underlying; costs + slippage included).

Full-period results (lot=75):
| config                        | win% | PF   | ₹ P&L    | worst wk | margin |
|-------------------------------|------|------|----------|----------|--------|
| condor short2% / wing1%       | 71.4 | 0.55 | -133,380 | -230 pts | 16k    |
| condor short2% / wing1.5%     | 74.1 | 0.61 | -130,065 | -373 pts | 24k    |
| condor short3% / wing1%       | 63.4 | 0.94 |   -4,410 | -149 pts | 17k    |
| condor short3% / wing2%       | 77.7 | 1.58 |  +48,008 | -357 pts | 35k    |
| iron-fly short0% / wing1%     | 31.2 | 0.75 |  -87,765 |  -95 pts |  5k    |
| naked strangle short3%        | 92.9 | 2.65 | +157,410 | -622 pts | 275k   |
| naked strangle short4%        | 99.1 | 4.33 | +109,838 | -440 pts | 275k   |

Two hard conclusions:
1. **The synthetic condor edge (+₹196k) was a MODEL ARTIFACT.** Black-Scholes+VIX
   underpriced tail weeks and ignored real skew. On real prices, nearly every
   defined-risk condor LOSES. Wings are priced efficiently; you overpay for
   protection and short strikes get breached more than the model assumed.
2. **The one positive condor (short3%/wing2%, PF 1.58) is NOT robust — it is a
   low-vol REGIME artifact.** Sub-period PF: 2024H2=0.6, 2025H1=0.76, 2025H2=4.2,
   2026H1=30.7, 2026H2=23.1. All the profit came from the unusually calm late-2025→2026
   regime; it LOST in the two earlier, more volatile windows. Same pattern for the
   naked strangle (2025H1 had a single -622 pt = -₹46k week).

The volatility-risk-premium edge is REAL but thin and regime-dependent. Harvesting
it safely needs (a) a vol-regime filter, (b) far more than ₹25k capital, and
(c) uncapped-tail tolerance the account doesn't have. A naive weekly condor on
₹25k is not a validated edge. Real bhavcopy in UDiFF format only goes back to
mid-2024, so we also can't test a real crash (2020/2022) — the sample lacks a
true stress week, and even within it half the windows lost.

Net: no simple, capital-appropriate, regime-robust edge found — intraday technical
(gross PF ~1.0) OR weekly options condor. Live trading stays hard-gated off.

### How to reproduce
```bash
python -m tools.fetch_nse_fo --start 2024-08-01 --end 2026-09-25   # ~10 min, resumable
python -m backtest.options_real_bt --short-offset 3.0 --wing 2.0    # single config
```

---
## Multi-strategy + regime routing on real data (the one thing that survives)

Built a regime-aware, multi-structure backtester on real NSE prices:
`backtest/options_multi_bt.py` + `tools/run_options_multi.py`. Library of
DEFINED-RISK structures (iron condor, iron fly, bull-put / bear-call credit
spreads, long iron-strangle) routed per weekly cycle by a regime classifier
(VIX level + 60d VIX percentile + VIX 5d change + NIFTY 20d trend/momentum),
all using only entry-day info (no lookahead).

Standalone (full period, real prices, lot 75):
| structure            | win% | PF   | ₹ P&L   | worst wk | note |
|----------------------|------|------|---------|----------|------|
| iron condor 3/2      | 77.7 | 1.58 | +47,983 | -26,759  | regime-driven |
| iron condor 2/1      | 71.4 | 0.55 |-133,398 | -17,298  | loses |
| iron fly /2          | 46.4 | 0.76 |-173,919 | -21,449  | loses |
| bull-put 3/2         | 83.9 | 2.99 | +63,565 | -10,891  | bull-market beta |
| bear-call 3/2        | 70.5 | 0.76 | -15,576 | -30,264  | loses (mkt rose) |
| long strangle        | 25.0 | 0.96 | -13,323 | -13,311  | buying vol loses |

Regime-routed mixes: MIX(all-regime) PF 0.88 (-₹17k); MIX(sell/flat) PF 1.0
(breakeven). Naive routing did NOT create an edge. Buying premium on "volatile"
days LOSES (long strangle 25% win) — high-VIX options are expensive and realized
rarely beats implied (VRP again, in reverse).

### The real finding: VRP TIMING, not regime routing
Per-regime breakdown of the condor showed the profit comes from HIGH-VIX and
falling weeks, not calm ones — the opposite of intuition. So we timed the condor
to sell ONLY when premium is rich (VIX >= 40th pct of its 60d range):

| filter          | trades | win% | PF   | sharpe | ₹ P&L   |
|-----------------|--------|------|------|--------|---------|
| always-on       | 112    | 77.7 | 1.58 | 0.81   | +47,983 |
| VIX pct >= 40   | 58     | 89.7 | 3.13 | 2.10   | +73,333 |
| VIX pct >= 55   | 46     | 91.3 | 2.75 | 1.94   | +59,704 |

VIX-timed condor (>=40) is POSITIVE in EVERY 6-month sub-period (2024H2 PF2.46,
2025H1 1.06, 2025H2 5.77, 2026H1 inf) — the first regime-robust result in the
whole project. This is the genuine volatility-risk-premium edge, correctly timed.

### Why it STILL doesn't fit ₹25k
Defined-risk cap = one full-width breach ≈ ₹27k loss (2025H1 had exactly one such
week; it wiped that half's profit). ₹27k > the ₹25k account = single-week ruin.
Wing/margin ≈ ₹35k already exceeds capital. To size one contract so a breach is
<=15% of equity you need ≈₹1.5-2L. At that capital the strategy is legitimate
(~PF 3, sharpe ~2, positive every window). Sizing — not signal — is the blocker.

Bottom line: we finally found a real, timeable, sub-period-robust options edge
(sell iron condor only when IV is rich). It needs ~₹1.5-2L capital, not ₹25k, and
still stays hard-gated off until live order code + paper validation exist.

### Reproduce
```bash
python -m tools.run_options_multi
```

---
## Drawdown reduction, same-day (0DTE) options, and capital sizing

### 1. Cutting the weekly condor's tail (stop-loss overlay)
Added a mark-to-market stop/take-profit to `backtest/options_multi_bt.py`
(`stop_mult`, `tp_frac`; marks on real daily closes). On the VIX-timed condor:
| overlay              | PF   | ₹ total | worst wk | maxDD   |
|----------------------|------|---------|----------|---------|
| hold to expiry       | 3.13 | +73,333 | -26,759  | -26,759 |
| stop @1.0x credit    | 1.69 | +44,936 | -11,762  | -22,069 |
| stop @2.0x credit    | 1.45 | +32,839 | -16,285  | -29,422 |
The 1x-credit stop **more than halves the worst week** (-26.8k -> -11.8k) but
whipsaws calm weeks (exits dips that recover), cutting total 73k->45k. It is a
lever for a smaller account, not free. Take-profit alone doesn't help the tail.

### 2. Same-day / 0DTE options (enter at OPEN, exit at CLOSE on expiry day)
New backtester `backtest/options_intraday_bt.py` (real per-contract daily OHLC;
strikes chosen from NIFTY index OPEN = no lookahead). Selling the expiry-day
premium and letting it decay into the close is the highest-theta trade:
| structure (0DTE)        | PF   | sharpe | ₹ total | worst   | margin | risk |
|-------------------------|------|--------|---------|---------|--------|------|
| short straddle ATM      | 1.75 | 3.54   | +206k   | -23k    | big    | naked |
| short strangle 1% OTM   | 2.36 | 4.12   | +94k    | -13.6k  | big    | naked |
| **iron-fly ATM w1%**    | 1.37 | 2.09   | +87k    | -14k    | ~10k   | DEFINED |
The **defined-risk 0DTE iron-fly fits a ₹25k margin (~₹10k/lot)** and is POSITIVE
in every 6-month window. BUT it is EXECUTION-SENSITIVE:
slip 1pt/leg PF1.37 -> 2pt 1.21 -> 3pt 1.08 -> 5pt 0.84 (dies). It is a 4-leg
structure filled at the open (widest spreads), so fills matter. VIX-timing helps
here too: at 3pt slip, vix_pct>=40 -> PF1.23 (calm days actually lose). Intraday
stops hurt (whipsaw). Only trades on expiry days (~weekly cadence).

### 3. Comfortable capital (the real answer to "what margin makes money")
Rule: size so ONE worst-case loss is <=15% of the account AND margin <=50%.
Period Aug-2024..Sep-2026 (~2.15 yr), 1 lot (NIFTY lot 75):
| strategy                         | worst loss | margin | comfy capital | est %/yr |
|----------------------------------|------------|--------|---------------|----------|
| Weekly condor VIX>=40 (hold)     | -26,759    | 37k    | ~₹1.8 L       | ~19%     |
| Weekly condor VIX>=40 (1x stop)  | -11,762    | 37k    | ~₹80 k        | ~27%     |
| 0DTE iron-fly VIX>=40 (slip 2pt) | -14,399    | 12k    | ~₹1.0 L       | ~24%     |
| 0DTE iron-fly VIX>=40 (slip 3pt) | -14,699    | 12k    | ~₹1.0 L       | ~15%     |

**Takeaways:**
- ~₹80k-1L is the comfortable floor for ONE lot with a survivable tail; expected
  ~15-27%/yr, sharpe ~1.4-2.1. This is a genuine, realistic edge at that capital.
- ₹25k: margin fits, but one bad week/day = 50-100% of the account = ruin risk.
  Not "comfortable" — it's all-in on every trade.
- Scale linearly: ~₹2L -> 2 lots or run condor + 0DTE together for diversification.
- The 0DTE iron-fly is the most capital-efficient (₹12k margin) but demands tight
  limit-order execution; the weekly condor is execution-tolerant but needs more
  capital for the same tail safety.

Live trading stays hard-gated until (a) live multi-leg order code exists and
(b) forward paper-trading confirms real fills match these assumptions.

### Reproduce
```bash
python -m backtest.options_intraday_bt      # 0DTE same-day table
python -m tools.run_options_multi           # weekly structures + VIX timing
```

---
## FINAL: Daily (0DTE) vs Weekly — profit by capital tier (no stop-loss)

`tools/capital_sizing.py` — sizing rule: worst historical event <=35% of capital
AND margin <=75%. Per-lot from real-data backtests (~2.15 yr). NIFTY lot 75.

DAILY — 0DTE iron-fly ATM, VIX>=40, ~2pt/leg slippage (PF 1.37, Sharpe 2.13,
margin ₹11.6k/lot, worst event ₹14.4k, maxDD ₹19.1k, ~27 trades/yr):
| capital | lots | profit/yr | return% | worst DD | DD% |
|---------|------|-----------|---------|----------|-----|
| 25,000  | 0    | too small (1 lot = all-in, ~58% risk) | | | |
| 50,000  | 1    | 23,026    | 46%     | -19,142  | 38% |
| 100,000 | 2    | 46,051    | 46%     | -38,284  | 38% |
| 200,000 | 4    | 92,103    | 46%     | -76,568  | 38% |
| 500,000 | 12   | 276,308   | 55%     | -229,704 | 46% |

WEEKLY — VIX-timed condor 3%/2%, hold to expiry (PF 3.13, Sharpe 2.10,
margin ₹37k/lot, worst event ₹26.8k, maxDD ₹26.8k, ~27 trades/yr):
| capital | lots | profit/yr | return% | worst DD | DD% |
|---------|------|-----------|---------|----------|-----|
| 25,000  | 0    | impossible (margin ₹37k > capital) | | | |
| 50,000  | 0    | too small | | | |
| 100,000 | 1    | 34,121    | 34%     | -26,759  | 27% |
| 200,000 | 2    | 68,242    | 34%     | -53,518  | 27% |
| 500,000 | 6    | 204,725   | 41%     | -160,554 | 32% |

### Verdict for real money
- DAILY wins on RETURN % (~46% vs ~34%) — it's capital-light (₹11.6k/lot), so
  more lots per rupee, and it works from ₹50k. BUT its edge is THIN (PF 1.37) and
  execution-sensitive: it needs ~2pt/leg fills; at ~5pt it stops working. Real risk.
- WEEKLY wins on SAFETY: PF 3.13 (makes ~3x what it loses), execution-tolerant
  (enter, hold to expiry, no intraday fills), but needs ~₹100k for one safe lot.
- Both are DEFINED-RISK (max loss = wing width) so even a crash week is capped —
  the key protection given the sample has no crash.
- ₹25k: only the 0DTE is even openable (1 lot, but that's all-in ~58-77% tail).
  Not recommended for real money at ₹25k.

RECOMMENDED: if capital >= ~₹1L, run the WEEKLY VIX-timed condor (best risk-
adjusted, high PF, execution-tolerant). Use/add the DAILY 0DTE only after paper
trading confirms your real fills are ~2pt/leg. For ₹50k-1L the 0DTE is the only
participant but treat it as higher-risk. Everything stays paper-gated until live
multi-leg order code + a forward paper run confirm the fills.

### Reproduce
```bash
python -m tools.capital_sizing
```

---
## HYBRID (daily + weekly together) + return context

Weekly and daily monthly-P&L are only **0.18 correlated**, so blending them earns
more per unit of drawdown. `tools/capital_sizing.py` now grid-searches the best
lot mix at equal risk (worst DD <=35% capital, margin <=75%):

| capital | HYBRID | weekly-only | daily-only |
|---------|--------|-------------|------------|
| 100k    | 34% (1wk)          | 34% | 23% |
| 200k    | **46%** (2wk+1dy)  | 34% | 35% |
| 500k    | **53%** (5wk+4dy)  | 41% | 41% |
| 1M      | **53%** (10wk+8dy) | 44% | 41% |

Hybrid beats either alone by ~10-19 pts/yr once capital >=200k (room for >1 lot to
diversify). Below 200k there's no room, so hybrid == weekly.

Return context (are ~34-53%/yr "low"? No): NIFTY ~12%/yr, Buffett ~20%/yr, elite
quant funds ~30-40%/yr. 34-53% is exceptional IF it holds forward — but this is a
~2yr calm/bull backtest, so expect LOWER live. Higher headline returns only come
from more lots = bigger drawdown (there is no free lunch): e.g. levering the
hybrid to ~80-96%/yr came with 56-67% drawdowns. For real money a ~20% DD budget
(=> ~20-30%/yr) is the sane choice.

---
## 5-YEAR REAL-DATA VERDICT (2021-07..2026-09, incl. 2022 selloff) — the honest reset

Extended the NSE fetch to the legacy bhavcopy format (tools/fetch_nse_fo.py) and
re-ran everything on ~5.2 years. This overturns the optimistic 2-year picture.

WEEKLY VIX-timed condor 3%/2%, per year (PF):
| filter    | 21H2 | 2022 | 2023 | 2024 | 2025 | 2026 | +yrs | ₹/lot/yr | Sharpe |
|-----------|------|------|------|------|------|------|------|----------|--------|
| vix>=40   | 0.85 | 0.96 | 0.63 | 5.42 | 1.10 | inf  | 3/6  | ~16,000  | 0.84   |
| **vix>=55** | inf | 1.17 | 0.61 | 4.69 | 1.10 | inf | **5/6** | **~18,000** | **1.23** |

DAILY 0DTE iron-fly, per year (PF): 21H2 0.65, 2022 0.57, 2023 0.45, 2024 1.44,
2025 1.70, 2026 1.13 → **LOSES over 5yr (PF 0.95)**. Stricter VIX filters make it
worse. REJECTED as a durable edge — it only worked in the 2024-2026 calm regime.

### Conclusions (supersede earlier optimistic numbers)
1. **The 2-year backtest was regime-lucky.** 2024-2026 was an unusually calm/bull
   window; both strategies shine there. On the full 5 years the true weekly edge
   is ~₹18k/yr per lot (~16%/yr on ₹110k), NOT the ₹34k/yr the 2-year window showed.
2. **Weekly (vix>=55) is the only survivor** — positive in 5 of 6 years (only 2023
   lost), PF 1.87, Sharpe 1.23. Real but modest and regime-sensitive; expect
   losing years.
3. **Daily 0DTE and the hybrid FAIL the 5-year test.** Kept in the registry for
   research but flagged with warnings; not for real money on current evidence.
4. **"Use more trading days" is counterproductive.** Trading MORE weeks (lower VIX
   threshold, filling calm weeks) LOST more (vix40 3/6 yrs vs vix55 5/6). The
   discipline of sitting out thin-premium weeks IS the edge. No calm-week filler
   beat sitting out.

### Default changed
options/registry.py weekly now uses vix>=55 (more robust). Realistic expectation
for real money: ~12-16%/yr on ₹1.1-1.5L, one lot, with down months (worst month
in 5yr ≈ -₹22.7k) and occasional down YEARS. Defined-risk throughout.
