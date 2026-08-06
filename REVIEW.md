# Swing Desk — Pre-Production Review (2026-08-02)

One-shot adversarial review before rollout. Four independent perspectives were
run against the live system, each trying to break it in a different way, then
every finding was argued against before being accepted. Fixes were implemented
and re-verified in the same pass.

**Verdict: SHIP, with three named limitations you must trade around (§4).**

---

## 1. What was tested, and how

| Tester | Question asked | Method |
|---|---|---|
| 1 — Quant / data integrity | Is the math right? | Recomputed every indicator independently in pandas and compared |
| 2 — Adversarial input | Does it break or lie on bad input? | 6 garbage inputs, 6 instrument classes, 5 concurrent lookups against the live server |
| 3 — Red team | Where could it *mislead a trade*? | Base rates, internal contradictions, staleness, disclosure gaps |
| 4 — Devil's advocate | Are the findings themselves wrong? | Argued the opposite case for every finding; overturned one |

Scores are computed from real market data (163-name universe, 2 years of
adjusted daily bars), not fixtures.

---

## 2. Results

### Tester 1 — Quant / data integrity: **19 pass, 0 fail**
SMA200, RSI14 (Wilder), ATR14 (Wilder), MACD line and histogram all match an
independent pandas implementation to within 1e-6 / 0.01. ADX and DMI bounded
correctly. No look-ahead bias (truncated inputs produce correctly shifted
state). Split/dividend adjustment verified on NVDA. Dates monotonic, no NaN,
OHLC internally consistent. All risk arithmetic (risk-per-share, R:R, share
sizing, T1<T2) reconciles exactly across every name.

**Nothing in the indicator layer is wrong.** That was the most important gate
and it passed cleanly.

### Tester 2 — Adversarial input: **3 real bugs found, all fixed**
| Bug | Before | After |
|---|---|---|
| Typo'd ticker (`ZZZZQQQ`) | HTTP 500, raw stack-trace text | HTTP 404, `'ZZZZQQQ' not found — check the symbol` |
| `BRK.B` | HTTP 500 | Normalized to `BRK-B`, returns full card |
| 300-char input | HTTP 500 | HTTP 400, rejected |
Path traversal and XSS attempts were already rejected. 5 concurrent lookups
returned 200 in 5.3s with no `report.json` corruption. Repeat lookups are
deterministic.

### Tester 3 — Red team: **4 HIGH, 3 MED, 4 LOW → fixed or documented**

**HIGH-1 — Post-event bars were completely undisclosed. (FIXED — highest-value finding.)**
5 of 15 top-ranked ideas had gapped ≥3% or moved ≥5% within 10 sessions.
FTNT gapped **+7.0%** one session before the scan and the card labelled it
`BASE_BUILDING` with `earnings: —`. A trader reading that card would believe
they were buying a quiet base; they would actually be buying a spent catalyst
with gap-fill risk below and IV crush ahead.
*Fix:* shock-bar detection, a new `POST_EVENT_DRIFT` setup type, an orange
EVENT BAR banner on the card, and thesis text naming the gap-fill level.

**HIGH-2 — Confluence could show 2 green votes on a broken trend. (FIXED.)**
MSFT and PLTR showed `FUNDAMENTAL: SUPPORTS` + bullish options against a
`BROKEN` trend. This violated the project's own first design principle
(*"Gates before scores. Never let a strong oscillator rescue a name that failed
the regime gate"*).
*Fix:* trend is now a gate — when it fails, the strip reads
**TREND GATE FAILED — no long here, whatever else says**, regardless of the
other two votes.

**HIGH-3 — Untradable tickets were ranked as top ideas. (FIXED.)**
10 of 25 ranked names sat below the spec's 2:1 R:R floor; FTNT was **#1 with a
0.3R ticket**. The composite scored chart quality and never asked whether the
trade was takeable at today's price.
*Fix:* R:R tiering in the ranker — sub-1R names are demoted below everything
takeable, and cards flag `POOR` (red) / `BELOW_SPEC` (amber).
**This changed the desk's output: top 3 went from FTNT/BAC/RTX to BAC/RTX/V.**

**HIGH-4 — Backtest does not validate the shipped scorer. (FIXED post-review — see §4b.)**

**MED — ETF/index cards looked broken** (perpetual "Fetching…" where
fundamentals don't exist). *Fixed:* they now read *"No company fundamentals —
this is an ETF, index or fund. Judge it on trend, flow and the MARKET ENV tab."*

**MED — Earnings `—` was ambiguous** between "none upcoming" and "we have no
data". *Fixed:* renders `none 21d` with an explicit `status` field.

**MED — Option flow side-inference is often inconclusive** (4/5 to 8/12 rows
land at MID). Genuine limitation, see §4.

**LOW — GEX "pin" language over-claimed** when the king strike sat 11-13% from
spot. *Fixed:* pin phrasing is suppressed beyond 7%; it now reads *"too far
from spot to act as a pin; treat as a magnet only if price gets there."*

### Tester 4 — Devil's advocate: **one finding overturned**
I originally flagged *"TR06 earnings gate has thin coverage — fails open"* as
HIGH because only 6/25 ranked names carried an earnings date. Investigation
disproved my own flag: the Nasdaq calendar returned **2,903 entries across 21
days** and is working correctly — those names genuinely have no earnings inside
the window, most having reported in late July. **Downgraded HIGH → MED and
reframed** as a UI ambiguity (since fixed). Recorded here because a review that
never overturns its own first read isn't a review.

The advocate also reframed HIGH-1: post-earnings-announcement drift is a
well-supported anomaly, so surfacing those names is legitimate — the bug was
*mislabelling* them, not showing them. That is why the fix names the setup
honestly rather than filtering it out.

---

## 3. Post-fix state

| Suite | Before | After |
|---|---|---|
| Quant / data integrity | 19 pass, 1 warn | **19 pass, 0 warn** |
| Adversarial input | 13 pass, 3 fail | **16 pass, 0 fail** |
| Ranked names below 1R | 6 | **0** (demoted) |
| Ranked names below 2R | 10/25 | 4/25, all flagged amber (1.34–1.98R) |
| Undisclosed event bars | 5/15 | **0** (all banner-flagged) |

---

## 4. Limitations you must trade around (not fixable in software)

**1. ~~The backtest does not validate the shipped scorer.~~ (RESOLVED — see §4c.)**
This was the highest-value gap flagged by the review. `backtest_wf.py` now
calls `engine.analyze` directly on point-in-time bar slices and reports the
real scorer's edge, with a Deflated Sharpe and PBO to guard against overfitting.
The full run cut the claimed Q5-Q1 spread roughly in half from the old
simplified backtest — see §4c for numbers.

**2. Option flow side-inference is a guess, not a print.**
We infer BOUGHT@ASK / SOLD@BID from the closing last-vs-bid/ask on EOD chains.
Half the rows land at MID, and we cannot see sweeps, blocks, or intraday
sequencing. GEX is likewise a naive dealer model (long calls / short puts,
single expiry). **Use these as a map of where positioning sits, never as
evidence of who is doing what.** A real-time tape needs a paid IBKR feed.

**3. Feeds can disagree by a full session.**
During testing IBKR returned FTNT's July 30 close ($154.25) while Yahoo had
July 31 ($161.95) — a 5% gap that was itself the event bar. Before acting on
any level near the open, confirm the price against your broker.

Also standing: fundamentals are point-in-time from a free feed; earnings dates
are unconfirmed until the company confirms them; and the whole system is
mechanical screening plus rule-based commentary — **not investment advice.**

---

## 4b. Earnings preview — validation (added post-review)

The preview's default advice ("don't hold through the print; wait and trade the
reaction") was tested rather than asserted. `backtest_earnings.py`, 585 prints
across 30 large caps over 5 years, forward 10 sessions:

| Strategy | Mean | Median | Hit | SD | 5th pct | Worst |
|---|---|---|---|---|---|---|
| A — hold through the print | **+1.66%** | +1.37% | 57.6% | 8.35 | −11.58% | −25.10% |
| B — wait, buy up-reaction | +1.15% | +0.96% | 59.0% | 5.51 | −7.09% | −19.66% |
| C — wait, buy any reaction | +1.21% | +0.96% | 59.3% | 5.57 | −6.98% | −19.66% |

Risk-adjusted (mean/sd): A 0.198 · B 0.210 · **C 0.217**

**Honest reading:** holding through earns a *higher average* — waiting is not
free, it costs ~0.5% of expected return per event. What waiting buys is a much
smaller tail: the 5th-percentile outcome improves from −11.6% to −7.1%, and
variance drops ~35%. For a stop-based swing book that asymmetry is decisive,
because a gap straight through a stop cannot be managed at any size — the
modelled A returns are *better* than a real stopped-out trader would achieve.
The recommendation text in the app states this trade-off explicitly rather than
claiming waiting is simply better.

Note also that filtering to up-reactions only (B) did **not** beat taking any
reaction (C) in this sample — a caution against over-fitting the entry rule.

Two known limits: the name list is survivorship-biased, and the earnings-move
history per ticker is only 7–8 events (~2 years), which the app flags in-card
when the sample is under 5.

## 4c. Walk-forward on the real scorer (added post-review)

`backtest_wf.py` calls `engine.analyze` on point-in-time bar slices for every
Friday over the lookback, applies a 5bps round-trip cost, and reports four
variants: base, sector-neutral quintiles, TR06-filtered, and both. Also
computes Deflated Sharpe (Bailey / López de Prado) and PBO via CSCV over 20
alt weight vectors.

Full 3y × 147 names (net of 5bps, excess vs SPY, 21d horizon):

| Variant | Q5-Q1 | Q5 hit | Sharpe | DSR | PBO |
|---|---|---|---|---|---|
| base | +1.30% | 51.3% | 0.124 | 0.642 | 0.080 |
| **sector-neutral** | **+1.44%** | **52.2%** | **0.229** | **0.817** | 0.170 |
| earnings-filtered | +1.29% | 51.4% | 0.125 | 0.650 | 0.182 |
| both | +1.42% | 52.1% | 0.221 | 0.794 | 0.104 |

**Reads:**
- The old simplified backtest (§4.1 first draft, now §4c) reported Q5 +3.94%
  gross; the shipped scorer nets +1.30%. The **true edge is roughly half of
  what was previously claimed** — this is the finding the harness was built
  for.
- Q1→Q5 is monotonic at every horizon (1/3/5/10/21d) and **PBO 0.08–0.18**
  across variants means the composite is not just weight-noise. It is a real
  but modest edge.
- **Sector-neutral quintiles nearly double Sharpe** (0.12 → 0.23) and push DSR
  toward the 0.95 confidence bar. A meaningful share of Q5's return in the
  base cell was sector-beta noise. Ranking within-sector then aggregating
  captures cleaner alpha. This is the actionable production finding.
- Earnings-blackout filter barely moves anything because yfinance only
  publishes ~4–8 past quarters per name → the historical blackout window we
  can measure covers only ~1% of PIT rows. **Not evidence the filter doesn't
  work** — the forward-looking `emap` production uses is complete.
- Decay curve is monotonic through 21d, so the current hold horizon isn't
  leaving obvious money on the table by holding too long.

## 4d. Phase B validation (added post-review)

Phase B added a 2-per-sector cap on the DESK ranking and a HY OAS + VIX
risk-off gate that halves (HALF_SIZE) or zeroes (NO_ADDS) sizing on every
card. `backtest_phaseB.py` validates both against 3y of top-8 equal-weight
portfolios (21d hold, 5bps cost, non-overlapping monthly Sharpe):

| Variant | Total | Mean/mo | Sharpe | MaxDD | Hit% |
|---|---|---|---|---|---|
| base | +112.71% | +2.18% | 1.05 | −16.54% | 56.4 |
| sector_capped | +65.34% | +1.41% | 1.01 | −14.06% | 61.5 |
| **regime_gated** | **+128.51%** | +2.36% | 1.16 | −11.66% | 56.4 |
| **both** | +81.44% | +1.64% | **1.24** | **−9.04%** | **61.5** |

**Regime gate is a clear win.** It fires 12/153 Fridays (7.8% of the time)
and adds ~16% to total return by halving/zeroing exposure on those baskets
— meaning risk-off Fridays averaged strongly negative excess return over
21d, exactly as the theory says. The four firing clusters map to real
events: Oct 2023 (rate/regional-bank stress), Aug 2024 (yen-carry unwind),
Mar-Apr 2025 (tariff panic — six Fridays, one at NO_ADDS), Mar 2026
(recent VIX 26-31). HY OAS drives 8/12 triggers, VIX 4/12, confirming HY
OAS is the more sensitive early-warning signal.

**Sector cap is a discipline trade-off, not a return win.** Same Sharpe as
base (1.01 vs 1.05) with lower vol and lower return, better MaxDD (−14%
vs −16.5%) and hit rate (61.5% vs 56.4%). Base concentrates hard into the
leading sector; sector cap forces diversification, trading upside for a
smoother distribution.

**Combined wins on every risk-adjusted metric.** Sharpe 1.24, MaxDD −9%,
vol 4.58%, hit rate 61.5% — best of all four in each. Total return +81%
lags base's +113% but the ride is 45% shallower on the worst drawdown.

Caveat: 39 monthly obs → wide Sharpe error bars. Survivorship bias same
as Phase A. The gate rules aren't tuned to the test window, but the
choice of HY OAS as the signal is informed by prior knowledge that it
works.

## 4e. Phase C: signal quality (added post-review)

### Regime multiplier (`phaseC_regime_builder.py`, engine.py `current_regime_edge`)

Pre-built lookup mapping the current regime cell (VIX bucket × HY-OAS
pctile bucket × SPX vs 200d) to the historical Q5 top-quintile edge from
Phase A walk-forward records. Full 3×3×2 = 18 cells, marginal fallback
when a cell has N<20. Displayed as a chip on the DESK tab; MARKET ENV
shows the underlying panel.

Baseline: +2.31% mean fwd 21d Q5, hit 51.3%. Notable cells:

| Cell | Mean fwd 21d | Hit | N | Edge× |
|---|---|---|---|---|
| MID VIX · CALM HY · UP SPX | **+8.77%** | 70.2% | 114 | **3.79×** |
| HI VIX · WIDE HY · DOWN SPX | **-2.96%** | 31.0% | 87 | **-1.28×** |
| LOW VIX · CALM HY · UP SPX | +2.57% | 51.5% | 1956 | 1.11× |
| MID VIX · WIDE HY · DOWN SPX | +0.23% | 45.5% | 202 | 0.10× |

The regime chip is now the first thing a trader sees on DESK: it says
"the model works best in MID VIX + CALM HY + UP SPX" and "in HI VIX +
WIDE HY + DOWN SPX, top-quintile *loses money* — stand aside". Today's
cell (LOW/NORMAL/UP) reads 0.94× baseline — neutral, normal size.

### PEAD sub-scorer: DROPPED after backtest failure (`backtest_pead.py`)

Design intent: separate score for POST_EVENT_DRIFT names with sweet-spot
sizing (3-8% moves, days 4-15). Backtested against composite on the same
2,048-row PEAD subset:

| Ranking | Q5-Q1 spread | Top-3 mean fwd 21d |
|---|---|---|
| by composite | **+3.06%** (monotonic) | **+6.46%** |
| by pead score | **-5.13%** (INVERSE) | +2.89% |

The composite ordered PEAD names correctly; the hand-rolled PEAD score
did the *opposite*. Diagnosis: the sweet-spot assumptions (3-8% moves,
days 4-15) were wrong — the big/fresh shocks the design penalized were
the ones that continue. **Removed from engine.py** with a comment
explaining the failure so future contributors don't re-add it. The
**PEAD Setups section** was kept as UI grouping (trade rules differ:
shorter horizon, tighter stops, gap-fill is invalidation) but ranked
**by composite**.

Devops read: backtest caught a bad design before it shipped. This is
exactly what §4.1 (the walk-forward harness) was built for.

## 4f. Pre-production tech-lead review (2026-08-05)

Second-pass audit after all three phases landed. Reviewer scope: correctness
under load, external-failure handling, cross-entry-point consistency, and
anything that could mis-size a live trade.

### CRITICAL (fixed)

**C1. `serve.lookup()` bypassed the risk-off halved sizing.**
Trader searches "NVDA" during a HALF_SIZE market → card showed full sizing
while pre-scanned top-3 showed halved. Direct 2× oversizing bug.
**Fix:** extracted the halving into `engine.apply_risk_off_to_result(r, risk_off)`;
called from both `cmd_scan` and `serve.lookup`. Verified: injected HALF_SIZE
into `report.json`, ran `/api/lookup?t=AAPL`, confirmed shares halved
(34 → 17, 10.5% → 5.2%, `size_mult=0.5` populated).

### HIGH (fixed)

**H1. `serve.lookup()` bypassed the sector cap.** Searched name didn't get
`sector_capped` flagged even if 3rd+ in a sector already in the ranked list.
**Fix:** `serve.lookup` counts un-capped names in the same sector from the
persisted report and marks the searched name accordingly.

**H2. Backtest PIT lookahead on FRED HY OAS.** `hy_series.loc[:pit]` was
inclusive of pit; FRED publishes T's value on T+1, so backtest overstated
gate timeliness by 1 trading day.
**Fix:** switched to `hy_series.loc[hy_series.index < pit]` (and same for
VIX) in `backtest_phaseB.pit_hy_oas_state` and `pit_risk_off`.

**H3. `serve._state["earnings"]` cached at boot forever.** A server running
for >24h uses last-Monday's earnings calendar → TR06 gate could greenlight
a name that has since had an earnings date confirmed.
**Fix:** added 6h TTL on `_state["earnings"]` and 12h on `_state["rotation"]`;
refresh on each lookup if expired.

### MEDIUM (fixed opportunistically)

**M1. `sim_portfolio` empty-day rows had schema mismatch** → NaN in Sharpe
calc, silently overstated Sharpe. Fixed to always emit the same keys.

**M3. Sector-cap loop created an empty `risk={}` dict** for names without one
(defensive `or {}`), when it should skip. Fixed to check `rk is not None`
before mutating.

### CRITICAL — wide-audit pre-existing code (fixed)

**P1. `size_capped` was a flag, not an enforcement.** [engine.py analyze()]

The comment beside `MAX_POSITION_PCT = 25.0` calls it a cap; the code only
set `size_capped: True` when it triggered but left `shares_at_risk_budget`
un-capped. Unit-tested reproducer: a $10 stock with a 3¢ stop shows
**25,000 shares / $250,000 / 250% of account** with a warning next to it.
A trader taking the ticket sizes 10× beyond the account.

Discovered during the second-pass wide audit, NOT during my Phase A/B/C
work — this bug predates every phase we shipped.

**Fix:** actually cap shares to `MAX_POSITION_PCT × ACCOUNT_SIZE / entry`
when the risk-budget shares would blow through it; store `shares_uncapped`
alongside so the trader can see what the un-capped position would have been.

### HIGH — wide-audit (fixed)

**P3. Non-atomic write to `report.json` in `cmd_scan`.**
`json.dump(report, open(path, "w"))` leaves a partial file on disk during
the fsync. `serve.py._load_report()` reading mid-write would hit unparseable
JSON → 502 for the rest of the day (or until the next scan overwrote it).
serve.py already used tmp+`os.replace` on its own writes; the batch scan did
not. Fixed to match.

**P5. `cmd_lookup` (CLI `python engine.py lookup XYZ`) skipped halved sizing.**
Same class of bug as CRIT-C1 but on the CLI path rather than the HTTP path.
Same fix: read `risk_off` from the existing report, call
`apply_risk_off_to_result(r, risk_off)` on each ad-hoc analyze result.

### Day-2 re-audit findings (2026-08-05, fixed)

Fresh-eyes audit on a new day, specifically hunting for regressions and
bugs introduced BY yesterday's fixes:

**P3b HIGH — cmd_lookup missed atomic-write fix.** Yesterday I fixed the
atomic write in `cmd_scan` but the identical pattern in `cmd_lookup`
(line 2298 → `json.dump(report, open(p, "w"))`) was still non-atomic.
Same race: `python engine.py lookup XYZ` while `serve.py` is up →
serve reads partial JSON. Fixed with tmp+os.replace like cmd_scan.

**Schema breakage HIGH — backtest_pead.py crashed on removed columns.**
The day-1 PEAD-scorer rollback removed `pead`, `shock_move_pct`, and
`shock_sessions_ago` from `backtest_wf.walk_forward()`'s row schema.
`backtest_pead.py` still referenced them (`df["pead"].notna()`,
`df["shock_move_pct"].describe()`), producing KeyError then TypeError
on `nlargest`. Since the tool is kept as a validator for future PEAD
candidates (per HOW-TO-RUN), fixed to handle missing columns gracefully
and still print the composite-only stats on the PEAD subset. Verified
runs clean with "pead: too thin" reported honestly.

**UI dupe LOW — top3 name renders twice when it's also a PEAD name.**
Today's scan: FTNT was in `top3` (composite 82.8) AND `pead_ranked`
(also filtered from `ranked`), so its big-card would render in both
sections. Deduped `pead_ranked` against `top3` set. Verified: today
top3 = [BAC, FTNT, GE]; pead_ranked = [SNOW, XOM, GOOG, AMZN, FCX, CRWD]
— no overlap.

**Verified as still working (no regression):**
- `apply_risk_off_to_result` idempotency (won't double-halve on repeated call)
- P1 MAX_POSITION_PCT cap present in serve.py responses (`shares_uncapped` field)
- C1 fix: NVDA searched under injected HALF_SIZE → 9.2%→4.6%, 43→21 shares
- Full scan on new day: HY OAS pulled, regime edge computed, PEAD section populated, no exceptions
- `backtest_wf.py`: Sharpe 0.116 (base) → 0.218 (sector_neutral); PBO 0.07–0.19; findings unchanged vs yesterday
- `backtest_phaseB.py`: gate still fires only on real stress dates; regime_gated still gives best MaxDD (-11.66%). Sector_capped shifted more (Sharpe 1.01→0.86) — traced to universe churn (screener refresh moved a few names in/out), not a code bug

### MEDIUM (documented, not auto-fixed)

**P2. `thesis_comment` break_lvl is `min(base_low, sma50)` — deeper than the
actual stop** (`max(structural, atr)` in the risk block). The narrative
tells the trader "close below $X invalidates" using a level BELOW the
actual stop. Trader's real stop is fine; only the displayed sentence is
misleading.

**P6. `_hist_earnings_moves` defaults to BMO when yfinance returns midnight
timestamps.** For prints that were actually AMC but returned with `hour=0`,
we'd measure the wrong session (same-day instead of next). yfinance
usually returns tz-aware timestamps in ET so this mostly works, but a
degraded response could silently show wrong historical move stats in the
earnings preview.

### MEDIUM / LOW (flagged for team decision, not auto-fixed)

**M2. `compute_risk_off` — VIX_PANIC alone (VIX > 30, HY OAS calm) only
triggers HALF_SIZE.** A pure equity vol shock without credit stress
(retail-flash-crash style) would still take full trades. Recommend
adding VIX > 35 → NO_ADDS regardless of HY OAS. Design choice, not a bug.

**M4. Regime lookup has no staleness check.** A lookup built months ago
silently drives the DESK chip. Recommend showing a "stale N days" warning
if `built` date > 60d old; refuse to display if > 180d.

**L4. `regime_lookup.json` has no CI on bucket means.** A "3.79× baseline"
cell with N=114 is well within noise. Recommend adding σ or a
"small-sample caveat" flag when N < 100.

---

## 5. Recommended additions, ranked by value

1. ~~**Walk-forward harness on the real scorer.**~~ **DONE** — `backtest_wf.py`.
2. ~~**Propagate sector-neutral ranking to the DESK tab.**~~ **DONE** as a
   2-per-sector cap in Phase B — see §4d.
3. ~~**Position-level portfolio heat cap (spec RK09).**~~ **PARTIALLY DONE** —
   Phase B added a sector cap and a portfolio-wide risk-off halving; a true
   heat cap that tracks total open risk across the book still doesn't exist.
4. ~~**Credit spreads (MK07, FRED HY-OAS).**~~ **DONE** in Phase B, now
   drives the risk-off gate — see §4d.
5. **Confirm earnings dates via IBKR** rather than the Nasdaq calendar alone,
   since TR06 is a safety gate and the calendar carries unconfirmed dates.

## 6. What I would remove
Nothing. The redundancy that exists (RSI alongside MACD alongside checklist
items) is disclosed in the spec's own redundancy map, and the confluence strip
now prevents the real risk — correlated signals masquerading as independent
confirmation.
