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

**HIGH-4 — Backtest does not validate the shipped scorer. (DOCUMENTED, NOT FIXED — see §4.)**

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

**1. The backtest does not validate the shipped scorer.**
`backtest.py` recomputes a *simplified* composite in pandas — no structure or
participation as actually built, no gates, no earnings blackout. It validates
the *idea* (score ordering predicts forward excess return: Q5 +3.94% vs Q1
+0.72% per 21 days, monotonic across quintiles) but not the product. The
universe is also today's screener list run backwards, i.e. survivorship-biased —
the tell is that *every* bucket is positive, including the worst.
**Treat the composite as a sensible ranking heuristic, not a validated edge.**
The single highest-value next project is a walk-forward harness that calls
`engine.analyze` itself, with costs and a point-in-time universe.

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

## 5. Recommended additions, ranked by value

1. **Walk-forward harness on the real scorer** (§4.1). Everything else is
   polish; this is the only item that changes what we *know*.
2. **Position-level portfolio heat cap (spec RK09).** Sizing is per-trade only;
   nothing stops six correlated longs each risking 0.75%. Cards now show
   notional and a size-cap flag, but book-level heat is unenforced.
3. **Credit spreads (MK07, FRED HY-OAS).** The spec calls it the best non-price
   veto and it is a cheap add to the MARKET ENV tab.
4. **Confirm earnings dates via IBKR** rather than the Nasdaq calendar alone,
   since TR06 is a safety gate and the calendar carries unconfirmed dates.

## 6. What I would remove
Nothing. The redundancy that exists (RSI alongside MACD alongside checklist
items) is disclosed in the spec's own redundancy map, and the confluence strip
now prevents the real risk — correlated signals masquerading as independent
confirmation.
