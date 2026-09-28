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

---
## More structures + stock options tested — nothing beats the NIFTY weekly condor

### Weekly structure variants (vix>=55, 5yr year-by-year)
Tested market-standard alternatives against the base 3%/2% condor, judged on
RETURN-ON-MARGIN (capital efficiency) + robustness, all defined-risk unless noted:
| structure          | PF   | Sharpe | ₹/yr/lot | margin | ROM/yr | +yrs |
|--------------------|------|--------|----------|--------|--------|------|
| **condor 3%/2%**   | 1.87 | 1.23   | 17,065   | 36k    | **47%**| 5/6  |
| broken-wing c2/p4  | 2.11 | 1.44   | 23,304   | 74k    | 31%    | 5/6  |
| jade lizard p3/c2  | 1.88 | 1.57   | 27,442   | 1.83M  | ~2%    | 6/6  |
| condor 3%/1%       | 1.28 | 0.51   | 4,351    | 18k    | 24%    | 4/6  |
Jade lizard's 6/6 and high Sharpe are a MIRAGE: its naked short put needs ~₹18L
margin and carries uncapped crash risk. Broken-wing makes more ₹/lot but needs
2x margin => worse ROM. Narrower wings kill the edge. The base 3%/2% condor is the
most capital-efficient and already near-optimal. NO weekly variant beats it.

### Daily 0DTE variants (5yr) — all fail
iron-fly ATM, condors 0.5%/1%, various wings: every variant is +2-3/6 years and
LOSES over 5yr. 0DTE premium selling is confirmed a 2024-2026 regime artifact.

### Stock options vs index (MONTHLY condor, same 2024-07..2026-09 window)
Single stocks are monthly-only. Fetched RELIANCE/HDFCBANK/ICICIBANK/SBIN/INFY/TCS
(tools/fetch_nse_stock_opt.py) and ran the same condor (backtest/options_monthly_bt.py):
| symbol   | win% | PF   | Sharpe |
|----------|------|------|--------|
| **NIFTY**| 90.0 | 2.94 | 1.19   |
| RELIANCE | 68.0 | 0.73 | -0.38  |
| HDFCBANK | 63.6 | 0.37 | -0.99  |
| ICICIBANK| 76.0 | 0.55 | -0.64  |
| SBIN     | 44.0 | 0.14 | -2.21  |
| INFY     | 61.1 | 0.81 | -0.28  |
| TCS      | 52.9 | 0.48 | -1.07  |
**Even in the favorable regime where NIFTY thrived, EVERY stock condor lost.**
Single-stock premium selling is destroyed by idiosyncratic jump/event risk
(earnings gaps, news) that a 5% condor can't contain; the index's diversification
is precisely why index premium-selling works. Stock options: REJECTED.

### Final: the NIFTY weekly VIX-timed 3%/2% iron condor remains the single best,
most robust, most capital-efficient strategy found. Nothing tested beats it.

---
## Exhaustive single-day (0DTE) research — no robust edge found

Tested every major single-day option family on 5yr real NIFTF data (enter at open,
exit at close; strikes from index open; costs+slippage), year-by-year:

| 0DTE family                          | verdict | detail |
|--------------------------------------|---------|--------|
| Sell near-ATM premium (fly/condor)   | FAIL    | regime artifact — loses 2021-2023, only 2024-26 |
| Sell far-OTM 5-7% (cheap wings)       | FAIL    | PF ~0.0 — tiny premium can't cover costs+breach |
| Buy directional on gap (momentum/fade)| FAIL    | 0/6 to 2/6 yrs — gap not predictive, theta bleeds |
| Buy straddle/strangle (long gamma)   | FAIL    | loses EVERY year — 0DTE theta destroys buyers |
| Directional credit spread (sell w/ gap)| MARGINAL/FRAGILE | 6/6 yrs at 2pt slip BUT only 3/6 at realistic 3pt (PF 1.43), dead at 4pt (PF 1.10); win% 80%->49% with slippage; small n (10-38/yr) |

The directional credit spread (gap up -> sell put spread, gap down -> sell call
spread) was the only 0DTE idea with a pulse, but it is too slippage-fragile and
small-sample to trust with real money — its apparent edge evaporates at realistic
execution costs.

CONCLUSION: single-day/0DTE option trading has NO durable edge in 5yr of real
data. The theta edge exists for SELLERS but is regime-dependent (fails in volatile
years) and slippage-fragile; BUYERS bleed theta every year; directional gap
signals aren't predictive enough to overcome costs. This is consistent across
selling near/far, buying directional/straddle, and directional credit spreads.
The durable edge remains the WEEKLY VIX-timed condor (held to expiry, execution-
tolerant). 0DTE is left as research only; not for real money.

---
## Calendar spreads tested (the last untested structural family) — also fail

Built backtest/options_calendar_bt.py: sell near-week option, buy far-month same
strike; short settles at intrinsic at near expiry, long valued at its real market
price that day. DATA CAVEAT: monthly-ATM daily bhavcopy prints are frequently
stale/erroneous (found a 15800CE marked ₹1613 when spot 15809 ~ should be ~₹450),
which corrupts calendar P&L; added a sanity filter (far leg must be 1.2-3x the
near leg) to drop bad prints.

5yr results (clean): call calendar PF 0.91 (-₹32k, 2/6 yrs); put calendar PF 0.63
(-₹199k, 1/6 yrs); double calendar PF 0.69 (-₹286k, 2/6 yrs). All LOSE. Reasons:
long-vega structure entered blindly (often buys expensive vol), and NIFTY weekly
moves are too large for the ATM "pin" the calendar needs. Also fundamentally hard
to backtest reliably on daily data (far-leg pricing is the crux and is illiquid).

### SEARCH COMPLETE — every structural family now tested on 5yr real data:
- iron condor / fly (premium selling, defined risk) -> WINS (weekly VIX>=55) — the
  ONLY durable edge (~15%/yr on capital, PF 1.87, uncorrelated to market)
- directional debit/credit spreads -> market beta, not a durable edge
- jade lizard / ratio spreads -> uncapped tail risk / huge margin, rejected
- calendar spreads -> lose (this section)
- 0DTE / single-day (all families: sell near/far, buy directional/straddle,
  directional credit spreads) -> no durable edge, slippage-fragile
- stock options (monthly, 6 liquid names) -> every one loses vs index
The weekly VIX-timed iron condor is the singular durable edge. Search converged.

---
## BankNifty / Sensex tested — not a gap-filler, not better than NIFTY

Q: VIX-time BankNifty/Sensex? Trade BankNifty on NIFTY's skipped weeks?

Facts (confirmed with real bhavcopy expiry data):
- India VIX is derived from NIFTY only — no native BankNifty/Sensex VIX. Any VIX
  timing on them uses India VIX as an imperfect proxy.
- BankNifty WEEKLY options were DISCONTINUED (~Nov 2024). 2025-2026 data shows
  MONTHLY-only expiries. So there is NO weekly BankNifty to trade -> it cannot
  fill NIFTY's skipped weeks. (And skipped weeks are market-wide low-vol, so
  BankNifty premium is thin then too — selling it then = the losing sell-every-week
  behaviour.)
- Sensex is a BSE product (separate data pipeline) and ~99% correlated to NIFTY —
  a Sensex condor is essentially a NIFTY duplicate, no diversification, no own VIX.

Fetched BANKNIFTY (tools/fetch_nse_stock_opt.py --instr IDO) and ran a MONTHLY
condor (backtest/options_monthly_bt.py, strike_step=100) vs NIFTY monthly, SAME
window 2024-07..2026-09:
  NIFTY 5%/3%:      PF 2.94 sharpe 1.19 worst -37.5k
  BANKNIFTY 5%/3%:  PF 1.90 sharpe 0.79 worst -58.6k
  BANKNIFTY 6%/3%:  PF 2.80 sharpe 1.18 worst -60.2k
BankNifty monthly condor IS viable but NOT better than NIFTY risk-adjusted:
comparable PF, but ~1.6x bigger worst-trade losses (BankNifty is more volatile).
Caveats: only ~2yr of data (favorable calm regime; no 2022-style stress), and
~90% correlated to NIFTY (limited diversification). VERDICT: not worth adding —
can't gap-fill (monthly only), adds tail risk, correlated, optimistic sample.
The NIFTY weekly VIX-timed condor remains the single recommended strategy.

---
## Robustness deep-dive on the IC (sensitivity / concentration / sizing / exits)

Instead of chasing 15%->30%, stress-tested the EXISTING IC for robustness and
"more return per unit of drawdown". Results (5yr, 3%/2% condor):

### VIX threshold sensitivity (overfitting check) — PASSES
| threshold | PF | Sharpe | ₹/yr |    | threshold | PF | Sharpe | ₹/yr |
|-----------|----|--------|------|----|-----------|----|--------|------|
| 45 | 1.88 | 1.18 | 19.5k |  | 60 | 1.74 | 1.10 | 14.5k |
| 50 | 1.79 | 1.12 | 17.5k |  | 65 | 1.98 | 1.38 | 15.5k |
| 55 | 1.87 | 1.23 | 17.1k |  | 70 | 1.77 | 1.15 | 12.2k |
Smooth PLATEAU across 45-70 (PF 1.74-1.98). 55 is NOT a lonely spike => the edge
is not overfit to a magic number. maxDD = -26,759 at every threshold (the one
worst week is high-VIX, included by all) — the tail is structural.

### Profit concentration — lumpy, take every trade
Top 5 weeks = 44% of profit; top 10 = 75%; the other ~88 trades net ~flat/negative.
Implication: cannot cherry-pick (unknown which weeks win in advance) — must take
ALL qualifying trades; and judge only over a full sample, not a few weeks.

### VIX-scaled sizing (0.75/1.0/1.25x) — no risk-adjusted gain
+10% return but +25% drawdown, PF 1.87->1.84. A wash; adds a fragile parameter. Skip.

### Exits (TP/stop, tested earlier on 5yr) — hold-to-expiry stays best
TP 50/60% ~ neutral (lower Sharpe); stops hurt (whipsaw). No change.

CONCLUSION: the IC is already at its efficient point. Threshold-robust, execution-
tolerant, PF 1.87, -27k maxDD, uncorrelated to market. No tweak (sizing, exits,
complements, other underlyings, 0DTE, more structures) improves it risk-adjusted.
Priority now: validate live/paper execution, NOT more backtest optimization.

---
## Attacking the SOURCE of the edge (IV/RV, expected-move strikes, width grid)

Per the "attack the edge, don't bolt on strategies" priority. Added rv10 + iv_rv
(VIX/realized-vol) to the regime classifier. 5yr results (VIX>=55 gate):

### 1. IV vs realized volatility (IV/RV) — thesis PARTIALLY confirmed
IC P&L by IV/RV bucket: sweet spot 1.1-1.6 (PF 1.3-1.8); IV/RV~1 LOSES (PF 0.85);
IV/RV>1.6 LOSES (PF 0.68, post-spike weeks). IV/RV ALONE is a WORSE filter than VIX
pctile (PF 1.21 vs 1.87). BUT combined:
  VIX>=55 AND IV/RV>=1.1: PF 2.59, Sharpe 1.83, maxDD -23.7k (vs current PF 1.87,
  Sharpe 1.23, -26.8k) — higher QUALITY but fewer trades so LESS total (60k vs 89k).
=> The edge is best described as "sell when VIX elevated AND options price more vol
   than realized". A quality-vs-quantity lever, not a free return boost.

### 2. Expected-move strike selection — the one genuine return improvement
Strikes at k x (weekly expected move = spot*VIX/100*sqrt(7/365)) instead of fixed 3%:
| variant           | +yrs | total | PF   | Sharpe | worst wk | ret/maxDD |
|-------------------|------|-------|------|--------|----------|-----------|
| fixed 3%/5%       | 5/6  |  90.7k| 1.87 | 1.23   | -26.8k   | 3.31      |
| EM 1.0x (=1 SD)   | 4/6  | 161.5k| 1.82 | 1.39   | -40.2k   | 4.02      |
| EM 1.2x           | 5/6  | 122.9k| 1.84 | 1.20   | -42.8k   | 2.87      |
| EM 1.0x+IV/RV>=1.1| 4/6  | 136.0k| 2.62 | 2.42   | -24.7k   | —         |
Economically principled (sell at ~1 SD, adapts to vol) — the good kind of change,
not a curve-fit. EM 1.0x: +78% return, better Sharpe, better return/maxDD, BUT
bigger single-week tail (-40k) and 4/6 yrs (lost 2022+2023). EM 1.0x+IV/RV: best
quality (PF 2.62, Sharpe 2.42) AND lowest tail (-24.7k) but 4/6 yrs + high variance.

### 3. Width grid — 3%/5% confirmed best-balanced
2/4 PF1.37, 2.5/4.5 PF1.65, 3/5 PF1.87 (best PF, lowest DD), 3.5/5.5 PF1.88,
4/6 PF1.37, 3/6 PF1.91 (more $ but -40k DD). Current 3/5 is the efficient point.

### Verdict
Expected-move strikes (esp. EM 1.0x + IV/RV>=1.1) is the FIRST real refinement that
improves profitability/quality — because it attacks the edge's source, not bolt-on
complexity. NOT switching live blindly: bigger tail + 4/6-yr robustness need
out-of-sample proof. Right move = paper-test EM variant in PARALLEL with fixed-3%,
compare live fills, then decide. Do NOT abandon the robust fixed-3% baseline yet.

---
## Remaining suggested experiments — status & stopping point

| suggestion            | status        | result |
|-----------------------|---------------|--------|
| capital/leverage      | tested         | scales return AND DD 1:1, no edge |
| IC width grid         | tested         | 3/5 best fixed; EM (vol-adaptive) beats it |
| expected-move strikes | tested ✅      | main win -> weekly_em (~21%/yr, PF 2.62) |
| IV vs realized vol    | tested ✅      | confirmed; combined gate in weekly_em |
| range/trend filter    | tested         | trend-adaptive + regime-router WORSE |
| entry timing intraday | NOT testable   | only daily OHLC, no intraday bars |
| post-move entry       | tested (proxy) | inconclusive, only 7 qualifying trades |
| dynamic wings         | tested         | wider 1.5x EM looked better in-sample (PF 2.91) — NOT adopting (overfit DoF) |
| re-centering/adjust   | tested         | = stops; hurt (whipsaw) |
| scale after live      | agreed         | the plan |

weekly_em is direction-NEUTRAL (corr with NIFTY move = 0.04). It loses on big
weekly moves EITHER way (up>2%: -19k; down<-2%: -20k) and wins on calm weeks
(+-2%: +171k, 98% win). It does NOT fail in bull runs per se — 2023 (bull) lost
because it was a thin-premium grind, 2024 (bull) won on rich premium. Edge = premium
richness, not direction.

STOPPING POINT: nearly every tweak improves the in-sample backtest (EM, wider
wings, IV/RV, ...). Each is a degree of freedom = overfitting risk. Deliberately
NOT stacking more. Baseline weekly (fixed 3/2, minimal DoF, 5/6 yrs) + one
principled refinement weekly_em (EM+IV/RV) run in PARALLEL on paper. Decision by
out-of-sample validation, not more optimization.

---
## COMPLETE 10-item experiment scorecard (all tested)

| # | experiment              | verdict | detail |
|---|-------------------------|---------|--------|
| 1 | capital/leverage        | no edge | 1.25/1.5/2x -> identical PF 1.87 & ret/DD 3.32 (pure leverage) |
| 2 | IC width grid           | done    | 3/5 best fixed; EM (vol-adaptive) beats it |
| 3 | expected-move strikes   | ✅ win  | weekly_em (~21%/yr PF 2.62) |
| 4 | IV vs realized vol      | ✅ win  | combined gate in weekly_em |
| 5 | range/trend filter      | interesting | FAR-from-20DMA (>=2%) PF 3.11 vs NEAR PF 1.14 — condor better when EXTENDED, not rangey. In-sample; 4/6 yrs |
| 6 | entry timing (intraday) | untestable | only daily OHLC |
| 7 | post-move entry         | interesting | prior-wk |ret5|>=2% -> PF 2.96, FIXES 2022+2023 but cuts trades 58% & total (75k vs 91k), new weak 2025. In-sample |
| 8 | dynamic wings           | done    | wider 1.5xEM better in-sample (PF 2.91) — not adopting (DoF) |
| 9 | re-centering/adjust     | done    | =stops; hurt (whipsaw) |
| 10| scale after live proof  | agreed  | the plan; leverage table (#1) supports linear scaling |

KEY THEME: #5 and #7 agree — the condor's edge is strongest AFTER a big move /
when the market is EXTENDED (rich premium + mean-reversion), weakest when calm and
on its average. Economically coherent, and fixes the historically-weak 2022/2023.
BUT: in-sample, cuts trade count ~58%, lowers total, shifts the weak year to 2025,
and is ANOTHER degree of freedom. Candidate to VALIDATE on paper, NOT to adopt now.

DISCIPLINE: found 4 in-sample improvements (EM, IV/RV, wider wings, post-move).
Each is a DoF. Refuse to stack them into an overfit monster. Live candidates stay
just two: weekly (minimal) + weekly_em (one refinement). Decide by OOS paper, not
more backtest tuning. (post-move could be added as an optional 3rd paper variant.)

---
## RESEARCH FREEZE 🔒 — 3 paper variants, decided by out-of-sample only

Tail analysis (tools/analyze_tail.py) confirmed: the condor's entire tail comes
from >2% weekly moves (EM: <1% PF inf, 1-2% PF 53, >2% PF 0.51). And these
dangerous weeks are NOT cleanly predictable at entry — entry VIX percentile only
mildly separates them (safe ~77 vs danger ~87, heavy overlap), IV/RV/ret5 barely
differ. So the tail is largely IRREDUCIBLE; the wide fixed IC survives big moves
rather than predicting them. That is why fixed 3/2 stays the control.

THREE frozen paper variants now run in parallel (tools/options_daily.py), each to
its own log, with full diagnostic columns (vix, vix_pctile, iv_rv, prev_5d_return,
strikes, credit, settlement, P&L):
| variant           | hypothesis                              | backtest (5yr) |
|-------------------|-----------------------------------------|----------------|
| A weekly          | original robust fixed 3/2 (control)     | PF 1.87        |
| B weekly_em       | expected-move (~1 SD) strikes + IV/RV   | PF 2.62        |
| C weekly_postmove | fixed 3/2 only after >=2% prior-wk move | PF 2.96        |

RULES ARE FROZEN. Do NOT tune (the 2% cutoff, the 55 VIX gate, the 1.1 IV/RV, the
EM mult were all found in-sample — each a degree of freedom). Do NOT add a 4th
variant. Do NOT let paper results re-tune the rules (that reintroduces the
optimization loop). The paper-vs-backtest validator (tools/compare_paper_backtest.py)
is now the key infrastructure: after a full cycle of live paper fills, compare
A/B/C to their backtests and let the MARKET decide which (if any) survives — B/C
are observation-only and may NOT be promoted to real money on backtest alone.
