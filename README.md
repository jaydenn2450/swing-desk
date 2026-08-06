# Swing Desk — internal daily swing scanner (v1.2, production)
<img width="2048" height="953" alt="Screenshot 2026-08-04 190123" src="https://github.com/user-attachments/assets/0feed741-be36-43b6-b470-0b5c9a629cf4" />
<img width="2017" height="930" alt="image" src="https://github.com/user-attachments/assets/fdb7f72b-a02e-4489-98d4-b61c7102f2c4" />
> Daily desk view: top-3 focus ideas with confluence strip, ranked scan, and sector rotation. Live options intel & GEX ladders on the FLOW tab.

> **Read [REVIEW.md](REVIEW.md) before trading from this.** It documents the
> pre-production adversarial review, what was fixed, and the limitations you
> must trade around (option-flow side-inference is inferred not observed;
> feeds can disagree by a full session). The walk-forward gap flagged in the
> original review is now closed — see `backtest_wf.py` and REVIEW.md §4c.

**v1.2 — hardening pass after adversarial review**
- **Event-bar detection.** Any gap ≥3% or move ≥5% in the last 10 sessions is
  detected, tagged `POST_EVENT_DRIFT`, shown as an orange EVENT BAR banner, and
  named in the thesis with its gap-fill level. Previously a name that gapped
  +7% on earnings could be presented as a quiet base.
- **Trend is a gate, not a vote.** When the technical thesis is BROKEN the
  confluence strip reads TREND GATE FAILED regardless of options/FA.
- **R:R integrity.** Sub-1R tickets are demoted out of the top ranks; cards flag
  `POOR` (red) and `BELOW_SPEC` (amber) against the spec's 2:1 floor.
- **Earnings clarity** (`none 21d` instead of an ambiguous `—`), **ETF/index
  cards** state that fundamentals don't apply, **server robustness** (bad
  ticker → 404, `BRK.B` → `BRK-B`, overlong → 400), and **GEX "pin" language
  suppressed** when the king strike is >7% from spot.

**v1.1 — live lookup server (every ticker gets the full treatment)**
- `serve.py` serves the dashboard *and* exposes `/api/lookup?t=TICKER`.
  Searching any ticker now fetches price history, options chains and
  fundamentals on demand (~5s) and renders a card **identical in every section
  to a top pick** — checklist, confluence strip, thesis, trade plan, options
  intel (walls/GEX/skew/flow), fundamentals. This fixes the old behaviour where
  a searched name showed "not fetched today".
- Auto-enrichment also covers: ranked-table names outside the top 8, chart-tab
  tickers, compare-mode adds, and watchlist adds.
- Results merge into `data/report.json`, so they survive reloads and rebuilds.
- HTML is served `no-store` and chart links are build-stamped, so a stale
  browser cache can't show you yesterday's page.

**v1.0 — FA layer + three-way confluence**
- **FUNDAMENTALS block** on every card — deliberately thin, only what argues
  for or against a long: revenue growth YoY **with acceleration/deceleration**
  (latest quarter vs the full-year trend), EPS growth, gross/operating/net
  margins with the YoY operating-margin change, forward P/E · PEG · P/S,
  net cash · FCF · ROE, and analyst consensus with target upside.
- Each block renders **SUPPORTS LONG / NEUTRAL / REJECTS LONG** with the ✓ pro
  and ✗ con reasons that drove it — same grammar as the options verdict.
- **CONFLUENCE strip** at the top of every card: TECHNICAL · OPTIONS ·
  FUNDAMENTAL verdicts side by side, plus a one-line read
  ("ALL ALIGNED — highest conviction", "CONFLICTED — size down or wait",
  "MULTIPLE REJECTS — stand aside"). Three independent votes on one trade.
- FA also appears in the watchlist cards, the chart tab's side panel, and the
  compare table (rev growth, fwd P/E · PEG, FA verdict) so side-by-side
  candidate selection includes fundamentals.
- Coverage: top 8 ranked + watchlist each scan, cached daily in
  `data/fa_cache.json`; any other name via `python engine.py lookup T`.

**v0.4.1 — OPTIONS INTEL on every card**
- The thin "options map" grew into a full **OPTIONS INTEL** block shown on
  focus cards, search cards, and lookups: call/put walls, gamma flip, net
  gamma, P/C volume & OI, **IV skew** (90%-moneyness put IV minus 110% call
  IV — steep = downside hedging bid, flat = no fear), ATM IV with IBKR 52w IV
  percentile, **option momentum** (chain vol/OI plus IBKR volume-vs-average),
  EOD premium flow tilt, and the top 3 flow prints inline.
- Each block renders a **SUPPORTS LONG / NEUTRAL / REJECTS LONG** verdict with
  its reasons — options as an explicit validation/rejection input to the
  thesis, not a side stat.
- Options coverage widened: top 8 ranked + watchlist + USO every scan; any
  other name gets the same block via `python engine.py lookup T`.
- IBKR enrichment fields (volume vs avg, P/C, IV percentile, HV30) merge
  directly into the intel block and its verdict.

**New in v0.4**
- **chart.html — the chart is its own browser tab** (`chart.html#TICKER`), so
  you can keep the desk and charts side by side. Interactive: mouse-wheel zoom,
  drag to pan, double-click reset, DAILY/WEEKLY toggle (2y of data), levels
  overlay on/off, ✎ NOTE (click the chart to drop a note — auto-saved in the
  browser, survives re-runs; click a note dot to delete), 💾 SAVE PNG.
- **Compare mode** — add up to 3 tickers in the chart tab (⇄ COMPARE):
  rebased performance overlay (3M/6M/1Y window) + a side-by-side stats table
  (setup, checklist, composite, RS, ATR, R:R, thesis, sector rotation,
  earnings) to pick the best of the candidates.
- **Search = full signal card** — Enter on any ticker shows the complete
  intelligence card (thesis, 12-pt checklist, scores, trade plan, options map),
  identical to the focus cards. The chart is an optional 📈 CHART ↗ button.
- **👁 WATCHLIST tab** — add/remove tickers in the UI; saves automatically in
  the browser (localStorage) and never changes unless you change it. Every
  re-run refreshes each name's signals (setup, checklist, composite, RVOL,
  accumulation/distribution, thesis verdict). Names from `watchlist.txt`
  (managed via `python engine.py watch add/remove T`) additionally get the
  daily health verdict (HOLDING/WARNING/BROKEN) and options/GEX coverage —
  promote a browser-added name to the file when you want full coverage.


**New in v0.3** — three-tab dashboard:
- **DESK** — everything from v0.2 (top 3, signal scans, ranked table, watchlist, earnings).
- **MARKET ENV** — VIX basket (level/9D/3M term structure, zone, 1y percentile,
  auto-read), oil complex (WTI trend + energy rotation quadrants + USO flow
  pointer), short/inverse-ETF basket (SH/SDS/SPXU/PSQ/SQQQ/SOXS/UVXY/VXX
  dollar-volume hedging gauge with LOW/NORMAL/ELEVATED/PANIC zones).
- **FLOW & GEX** — per focus name + USO: notable option flow rows (strike,
  C/P, expiry/DTE, size @ price, premium, Vol/OI flag, BOUGHT@ASK / SOLD@BID
  side inference) with a bull/bear premium tilt, and a strike-by-strike GEX
  ladder (teal = absorbs, purple = amplifies, gold ★ = king/pin strike, white
  row = spot) with day-over-day OI change chips (appear after 2+ daily scans;
  snapshots persist in `data/gex_snapshots/`) and auto-commentary about pin
  structure, floors, and amplifier zones.
- **Interactive chart screen** — click any ticker anywhere (or search + Enter):
  candlestick chart (260d) with SMA 20/50/200, volume, and every critical
  level drawn & labeled (entry, stop, T1/T2, shelves, fibs, OI walls, gamma
  flip, 52w high, 200d-break), plus the thesis comment and trade plan beside
  it. Esc to close. Names outside the report → `python engine.py lookup XYZ`.

*Flow/GEX honesty note:* rows are EOD chain approximations (volume vs OI,
last-trade vs closing bid/ask), not live tick tape — a real-time flow feed
(e.g. via IBKR subscriptions) is the upgrade path if we want intraday.


Long-only equity swing-idea desk tool for a 2-person shop. Scans the broad
market daily for the top 3 actionable swing setups, tracks whether existing
ideas are holding or breaking down, maps sector rotation, options positioning
(walls/GEX), and flags the week's earnings + macro events.

**Not investment advice — mechanical screens + rule-based commentary for
internal research only.**

---

## Daily workflow

```
python engine.py all        # ~20s: screener universe + scan + dashboard/chart html
python serve.py             # start the desk (leave it running all session)
```
Then open **http://localhost:8741/dashboard.html**.

`serve.py` is what makes lookups automatic: **every ticker you search — in the
scan or not — is analyzed live** (technicals, options intel, fundamentals) in
about 5 seconds, and merged into `data/report.json` so it persists. No manual
commands. Same for the chart tab and watchlist adds.

`python engine.py lookup XYZ` still exists as a CLI equivalent if the server
isn't running.
Then ask Claude:
1. **"Enrich today's top 3 from IBKR"** — pulls live option volume vs average,
   P/C, IV + 52w percentile via IBKR MCP → `data/ibkr_enrich.json` → rerun
   `python engine.py html`. Volume ≥2× average gets flagged UNUSUAL.
2. **"Refresh macro.txt"** weekly — CPI/FOMC/NFP dates via web search.
3. Weekly: `python engine.py export-holdings` then run
   `python ../../Portfolio-risk/portfolio_stress_test.py --holdings holdings.csv`
   for the portfolio-level VaR / crisis-replay view (kept standalone on purpose —
   it measures the book, not the ideas).

Files you edit: `universe.txt` (manual adds on top of the auto screener),
`watchlist.txt` (open ideas, optional entry price), `macro.txt` (event dates).
Config knobs at the top of `engine.py`.

## Architecture

```
yfinance screener (mcap>$10B, 11 sector queries)  ─┐  auto universe (150) + universe.txt
Yahoo v8 chart API (2y adjusted OHLCV, threaded)  ─┤
Nasdaq earnings calendar (21d, cached daily)      ─┼─>  engine.py scan  ─>  data/report.json
yfinance option chains (top3+watchlist)           ─┤        │
rotation: RRG math (JdK approx) on 22 ETFs        ─┘        v
                                    data/ibkr_enrich.json (Claude/IBKR)
                                                            │
                                          engine.py html ───┴──> dashboard.html (static)
```

### What's computed per name (ta-indicator-spec.md stages)

- **Gates:** TR01 $20M dollar-vol · TR03 $5 floor · TR11 220 bars ·
  **TR06 earnings blackout (no entry ≤3d before print)** · RG01/RG02 200d
  regime · RG11 ADX · VO03 extension ≤4 ATR
- **12-pt checklist** (screenshot-style): MA FAN, 200d RISING, HH/HL, 52W HI,
  CLOSE HI, U/D VOL, OBV+, VDU, ATR SQZ, NR7, SPRING, W.EMA (+TOP RS swap-in)
  → tag: ≥8 MOMENTUM BUILDING · 6-7 WATCH · 4-5 TRANSITION · <4 WEAK
- **Structure:** pivot-clustered support/resistance shelves, base geometry,
  fib retracements of the 52w swing, NR7, spring (undercut & reclaim)
- **Risk:** stop = structure (base low − ½ATR) or 2.2 ATR; **targets =
  shelf → 52w high → measured move (base height projected)**; T1/T2; R:R;
  shares at risk budget; ±1/2 ATR day bands
- **Rotation confluence:** ticker's sector ETF quadrant (Leading / Improving /
  Weakening / Lagging, daily + weekly) — prefer longs in Leading/Improving
- **Options map** (top ideas + watchlist): call wall, put wall, naive GEX
  gamma-flip, P/C volume & OI, flow-switch flag (volume tilt fighting OI tilt)
- **Thesis comment** (rule-based): INTACT / WEAKENING / BROKEN + the exact
  levels — hold level, invalidation level, trend-break level (200d)
- **Trade plan:** "if entered today" — stop/T1/T2, expected day range, levels
  above/below (shelves, fibs, walls, gamma flip), events to watch
- **Signal events** across the universe with recency: golden/death cross,
  8/21 & 13/50 EMA crosses, RSI extremes, new 52w highs

### Dashboard

Search bar (any scanned ticker → full card; outside universe → lookup hint),
regime banner, macro chips, rotation quadrant boxes (daily/weekly toggle),
top-3 focus cards, signal-scan browser with recency chips, ranked table
(click row → detail card), watchlist health with thesis text, earnings week.

## Backtest

Two harnesses ship. Prefer `backtest_wf.py` for anything that matters — it
calls the real scorer.

### `backtest_wf.py` — walk-forward on the real `engine.analyze` (data/backtest_wf.json)

3y × 147 names, weekly PIT slicing, 5bps round-trip cost, forward 21d excess
vs SPY:

| Variant | Q5-Q1 | Sharpe | DSR | PBO |
|---|---|---|---|---|
| base | +1.30% | 0.124 | 0.642 | 0.080 |
| **sector-neutral** | **+1.44%** | **0.229** | **0.817** | 0.170 |
| earnings-filtered | +1.29% | 0.125 | 0.650 | 0.182 |
| both | +1.42% | 0.221 | 0.794 | 0.104 |

Q1→Q5 monotonic across all horizons (1/3/5/10/21d), decay curve monotonic
through 21d. PBO < 0.2 across variants means the composite isn't noise-fit.
The sector-neutral lift is the real actionable finding — see REVIEW.md §4c.
Rerun: `python backtest_wf.py` (~90s after earnings-history cache warm-up).

### `backtest.py` — legacy simplified composite (data/backtest_results.json)

Kept for historical comparison; it recomputes a vectorized approximation of
the scorer (missing structure/participation/gates) and previously reported
Q5 +3.94%/21d gross. The walk-forward above shows the real scorer nets
roughly half that after costs, which is the whole reason the walk-forward
harness now exists.

## Known limitations / next up

1. **FA layer not wired yet** (next milestone): EPS/sales growth, margins,
   guidance revisions — IBKR fundamentals or earnings-call summaries via Claude.
2. GEX is a naive approximation (dealers long calls/short puts, one expiry).
   Real dealer positioning needs flow attribution we don't have.
3. Base-pattern classifier is still geometric (no cup-handle/double-bottom
   recognition).
4. Credit spreads (MK07) not wired — add FRED HY-OAS pull.
5. Earnings dates come from Nasdaq's calendar — occasionally missing/late for
   unconfirmed dates; IBKR enrichment should double-check before entries.
6. Yahoo endpoints are unofficial; if they break, swap `fetch_history` to IBKR
   `get_price_history` via Claude — the engine is source-agnostic.
