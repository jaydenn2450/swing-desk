# How to run Swing Desk

Requires Python 3.11+ with `yfinance`, `pandas`, `numpy`, `lxml`, `requests`.
Install: `pip install yfinance pandas numpy lxml requests`.

---

## The normal morning routine (2 commands)

**1. Run the scan** — screens the universe, computes every signal, writes the pages.
Takes ~20 seconds.

```bash
python engine.py all
```

**2. Start the desk** — leave this window open all session.

```bash
python serve.py
```

Then open **http://localhost:8741/dashboard.html**

Stop the server with `Ctrl-C` in that window when you're done for the day.

> The server is what makes ticker search work. Without it the dashboard still
> opens, but searching a new name can't pull its options/fundamentals.

---

## Running it in VS Code (no typing)

Open the folder once:

```bash
code .
```

Install the recommended **Python** extension if VS Code prompts you (it will —
`.vscode/extensions.json` asks for it).

### The one shortcut to remember

**`Ctrl+Shift+B`** → runs **☀ Morning Run**: scans, then starts the desk server.
Two terminal panels open; leave the server one running. Then open
http://localhost:8741/dashboard.html

### All tasks
`Ctrl+Shift+P` → **Tasks: Run Task** → pick from:

| Task | Does |
|---|---|
| ☀ Morning Run (scan + start desk) | The daily one — `Ctrl+Shift+B` |
| 1. Scan (engine.py all) | Scan only |
| 2. Start desk server | Server only |
| Rebuild pages only | Instant page rebuild from last scan |
| Look up a ticker | Prompts for a symbol |
| Watchlist: add / remove ticker | Prompts for a symbol |
| Run backtest | Score validation (~1 min) |
| Export holdings for stress test | Writes `holdings.csv` |
| 🛑 Stop all Python | Kills stuck servers, frees port 8741 |

### Debugging (breakpoints)
`F5` → pick a configuration: **Scan**, **Desk server**, **Lookup a ticker**
(prompts), or **Backtest**. Set breakpoints by clicking the gutter — useful if
you ever want to inspect why a name scored the way it did.

### Editing the app
- Signals, gates, scoring → `engine.py`
- Dashboard layout → `dashboard_template.html`, then run **Rebuild pages only**
- Chart screen → `chart_template.html`, then **Rebuild pages only**

Never edit `dashboard.html` / `chart.html` directly — they're generated and get
overwritten on every rebuild.

---

## Earnings alerts

Every card — screened, top pick, or searched — shows a compact alert when
earnings are near. Nothing to configure; it runs on every scan and lookup.

| Alert | When | Shows |
|---|---|---|
| 📅 **REPORTS IN Nd** | Report due within **14 days** | Date, plus `BLACKOUT — no new entries` inside 3 days |
| 📊 **REPORTED Nd AGO** | Reported in the **last 7 days** | BEAT/MISS %, actual EPS vs estimate, fiscal quarter |

The last report also appears as a line in the FUNDAMENTALS block.

### Earnings preview (reports within 14 days)
Names reporting soon also get a full **EARNINGS PREVIEW** block:

- **Street numbers** — consensus EPS with the analyst range and count, revenue
  consensus, and YoY growth
- **Estimate revisions (30d)** — UP means the bar has been raised, so an in-line
  print can still disappoint
- **Options-implied move** — from the ATM straddle of the first post-earnings
  expiry, expressed as a price band
- **History** — average absolute move over past prints, how often it trades up,
  and the beat rate (BMO vs AMC timing handled correctly)
- **Implied vs realized** — RICH means options price a bigger move than this
  name usually delivers
- **Stop test** — the key one: does an average implied move breach your stop?
- **A call**: `AVOID THROUGH PRINT` / `HALF SIZE AT MOST` / `ACCEPTABLE — still
  binary`, with the ✓ and ✗ factors behind it

Validated by `python backtest_earnings.py` — see REVIEW.md §4b. Short version:
holding through prints earns a slightly *higher* average but with ~50% more
variance and a much fatter left tail, so waiting is the better default for a
stop-based book without being free.

Why both directions matter: a report *ahead* is un-priced risk you're carrying
into a gap; a report *behind* means the catalyst is spent and the move you're
looking at is already the market's verdict. Loss-making quarters are handled
correctly (Nasdaq reports them in accounting parentheses).

---

## When to run it (data freshness)

Every run fetches fresh data — but *when* you run changes what you get. The
header tells you which state you're in:

| Header badge | Means | Trust it for |
|---|---|---|
| **● PRIOR CLOSE** (grey) | Market closed; last completed session | Everything. Planning tomorrow's trades. |
| **● TODAY'S CLOSE** (green) | Today's session finished | Everything. The authoritative daily read. |
| **● LIVE · BAR STILL FORMING** (amber) | Running mid-session | Price and trend only — see below |

### Running during market hours
You get today's *forming* bar: price is live (Yahoo delays ~15 min) but **volume
is only partial**. That degrades anything volume-based:

- RVOL reads low, so breakout confirmation (needs ≥1.4× volume) won't trigger
- `U/D VOL`, `OBV+`, `VDU`, accumulation/distribution counts are provisional
- Entry/stop/target levels shift as the bar moves

The dashboard shows an amber warning banner in this state, so you can't mistake
it for a final read.

### Recommended rhythm
- **After the close (~4:15pm ET or later)** — the real daily scan. Plan here.
- **Intraday, optional** — a "how is it developing" check. Treat volume signals
  as provisional and don't judge a breakout until the close.
- **Pre-market / weekend** — shows the prior close; perfectly good for planning.

Options and fundamentals are fetched live whenever you search, so those are
current regardless of when you run.

---

## Command reference

| Command | What it does |
|---|---|
| `python engine.py all` | Full scan + rebuild both pages (the normal one) |
| `python engine.py scan` | Scan only — refresh data, don't rebuild pages |
| `python engine.py html` | Rebuild pages from the last scan (instant; use after editing a template or dropping in IBKR data) |
| `python serve.py` | Start the desk on port 8741 |
| `python serve.py --port 9000` | Start on a different port if 8741 is busy |
| `python engine.py lookup NVDA` | Analyze a ticker from the CLI (the server does this automatically, so you rarely need it) |
| `python engine.py watch add NVDA` | Add to the file watchlist — full daily health + options coverage |
| `python engine.py watch add NVDA 178.50` | Same, recording your entry price so drawdown-from-entry is tracked |
| `python engine.py watch remove NVDA` | Remove from the file watchlist |
| `python engine.py export-holdings` | Write `holdings.csv` from the watchlist for the portfolio stress test |
| `python backtest.py` | Legacy backtest — validates a *simplified* composite (kept for parity with older reports) |
| `python backtest_wf.py` | Walk-forward on the **real** `engine.analyze` scorer + Deflated Sharpe + PBO + sector-neutral / TR06-filtered variants (~90s after cache warmup) |
| `python backtest_phaseB.py` | Portfolio-level validation of Phase B additions: top-8 equal-weight, 21d hold, four variants (base / sector-capped / regime-gated / both) with Sharpe / MaxDD / hit-rate + a risk-off firing log against known stress dates (~100s) |
| `python phaseC_regime_builder.py` | Rebuilds `data/regime_lookup.json` — historical (VIX × HY-OAS × SPX) → Q5 forward-21d edge lookup used by the DESK regime chip. Re-run occasionally (monthly?) so the lookup reflects the most recent 3y of walk-forward data (~100s) |
| `python backtest_pead.py` | Validates any candidate PEAD sub-scorer against the base composite on the POST_EVENT_DRIFT subset. If a new scorer's Q5-Q1 spread beats composite's +3.06%, it earns replacement; otherwise composite stays as the PEAD ranker |

---

## Using the app

**Four tabs:** DESK (top 3, signals, ranked scan, earnings) · WATCHLIST ·
MARKET ENV (VIX / oil / short-ETF hedging) · FLOW & GEX.

- **Search any ticker + Enter** → full signal card. Works for names outside the
  scan too; it fetches them live in ~5 seconds.
- **Click a ticker or 📈 CHART** → interactive chart in its own browser tab
  (zoom with the wheel, drag to pan, DAILY/WEEKLY toggle, ✎ NOTE to annotate,
  💾 SAVE PNG, ⇄ COMPARE up to 3 names side by side).
- **Watchlist tab** → type a ticker, `+ ADD`. Saves instantly in the browser and
  survives re-runs. For full daily health + options coverage, promote it to the
  file with `python engine.py watch add TICKER`.

### Two kinds of watchlist entry
| | Where it lives | Coverage |
|---|---|---|
| **MY ADD** (typed in the browser) | that browser's storage | signals refresh each scan |
| **FILE** (`engine.py watch add`) | `watchlist.txt` | + health verdict, options intel, GEX, fundamentals every scan |

To clear browser adds: open the Watchlist tab and click ✕ on each, or run
`localStorage.removeItem('sd_watchlist')` in the browser console.

---

## Files you edit

| File | Purpose |
|---|---|
| `watchlist.txt` | Names you're tracking (or use the `watch` commands) |
| `universe.txt` | Manual additions on top of the ~150-name auto screener |
| `macro.txt` | CPI/FOMC/NFP dates shown on the dashboard — **ask Claude to refresh weekly**; the dates in there now are placeholders |

Config knobs are at the top of `engine.py`: `ACCOUNT_SIZE`, `RISK_BUDGET_PCT`,
`MIN_DOLLAR_VOL`, `EARNINGS_BLACKOUT_DAYS`, `MIN_REWARD_RISK`, `MAX_POSITION_PCT`,
`SECTOR_MAX_POSITIONS` (max ranked names per sector before demotion),
`FRED_KEY_FILE` (path to your FRED API key for the HY-OAS credit-spread veto).

### Portfolio-level risk-off gate (Phase B)
`data/fred_api_key.txt` holds your FRED API key (get one free at
fredaccount.stlouisfed.org/apikeys). Engine pulls HY OAS (BAMLH0A0HYM2) each
scan and combines with VIX into a risk-off tier: **NORMAL** (nothing) /
**HALF_SIZE** (banner + every card's sizing halved) / **NO_ADDS** (banner +
sizing zeroed). Triggers: HY OAS above 60d high or +40bps in 20d, or VIX > 25,
promote to HALF_SIZE; HY OAS at 1y high **and** VIX in stress zone promote to
NO_ADDS. The HY CREDIT SPREAD panel on the MARKET ENV tab shows the raw level
and trigger detail.

### Sector cap
`SECTOR_MAX_POSITIONS = 2` — after the primary composite ranking, the 3rd+
name in any sector is marked `sector_capped` and demoted below un-capped
alternatives. Rank rows show a `↓SC` badge; big cards show `↓ SEC CAP` on the
Size line. This bakes in the Phase A walk-forward finding that sector-neutral
ranking gives ~2× the Sharpe.

---

## Weekly / occasional

- **IBKR live enrichment** — ask Claude *"enrich today's focus names from IBKR"*.
  It pulls option volume vs average, put/call and IV percentile into
  `data/ibkr_enrich.json`, then run `python engine.py html`.
- **Refresh macro dates** — ask Claude *"refresh macro.txt"*.
- **Portfolio stress test** — `python engine.py export-holdings`, then
  `python "..\..\Portfolio-risk\portfolio_stress_test.py" --holdings holdings.csv`

---

## If something goes wrong

| Symptom | Fix |
|---|---|
| "OPTIONS/FA UNAVAILABLE (SERVER NOT RUNNING)" but the server *is* running | You opened `dashboard.html` as a file (double-clicked it). Use the address **http://localhost:8741/dashboard.html** — never open the file directly. The page now warns you when this happens. |
| `Address already in use` / page won't load | An old server is still running. `Get-Process python \| Stop-Process -Force`, then start `serve.py` again |
| Search says "needs the desk server" | `serve.py` isn't running — start it |
| Page looks like yesterday's | Browser cache. Add `?v=2` to the URL or hard-refresh (Ctrl-Shift-R) |
| `No data/report.json` on server start | Run `python engine.py all` first |
| A ticker returns "not found" | Check the symbol; class shares use a dash (`BRK-B`, though `BRK.B` is auto-corrected) |

---

Before trading from any of this, read **REVIEW.md** — especially the three
limitations in §4. Mechanical screens and rule-based commentary, not investment
advice.
