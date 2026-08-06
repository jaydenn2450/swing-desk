"""
backtest_phaseB.py -- validate the Phase B additions historically.

Phase A proved the shipped scorer has real ordinal information (PBO 0.08-0.18)
and that a sector-neutral quintile ranking nearly doubles Sharpe. Phase B
turned those findings into production plumbing: a 2-per-sector cap on the
DESK ranking, and a HY OAS + VIX risk-off gate that halves (or zeroes) sizing
on every card. This harness answers: do those two additions actually earn
their keep, or are they cargo cult?

Method:
  - Reuse backtest_wf.walk_forward() to get the per-row (date, ticker) DataFrame
  - Fetch historical HY OAS from FRED going back to 2020 (needs 1y warmup for
    the 1y-percentile logic to be honest at the start of the test window)
  - Fetch historical ^VIX from yfinance
  - For each Friday, compute PIT risk_off tier using the SAME rules production
    uses (compute_risk_off in engine.py), just applied to prior-only data
  - Simulate four top-8 equal-weight portfolios, each held 21 trading days:
      base            : as-is
      sector_capped   : after ranking, keep at most 2 per sector, back-fill
                        from the next candidates
      regime_gated    : when PIT risk_off != NORMAL, halve (HALF_SIZE) or zero
                        (NO_ADDS) the sizing for that Friday's basket
      both            : sector cap + regime gate
  - Report: Sharpe, max drawdown, mean/median monthly return, hit rate, and
    a firing-log of which Fridays the gate would have triggered on

Costs: 5 bps round-trip per position. Same 21d hold, non-overlapping monthly
sampling for the Sharpe series so we don't inflate T with overlaps.

Usage:  python backtest_phaseB.py [--fast]
"""

import json, math, os, sys, time, warnings
from datetime import date
import numpy as np
import pandas as pd
import yfinance as yf

import engine
import backtest_wf

warnings.filterwarnings("ignore")

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")

# ---- config
TOP_N = 8                       # ranked names taken per Friday
HORIZON = 21                    # trading days held
COST_FRAC = backtest_wf.COST_FRAC
ACTIONABLE = {"BASE_READY", "BREAKOUT", "MOMENTUM", "BASE_BUILDING",
              "PULLBACK", "POST_EVENT_DRIFT"}
SECTOR_CAP = engine.SECTOR_MAX_POSITIONS
HY_OAS_START = "2020-01-01"     # need >=1y prior to first PIT date for pctile


# --------------------------------------------------- historical data pulls

def fetch_hy_oas_history(start=HY_OAS_START):
    """Bulk FRED pull of the full HY OAS series. Returns pd.Series indexed by
    date. Uses the engine.py key file so we share credentials with production."""
    key = engine._read_fred_key()
    if not key:
        print("! no FRED key found -- risk-off gate can't be tested. Aborting.")
        sys.exit(1)
    url = (f"https://api.stlouisfed.org/fred/series/observations"
           f"?series_id={engine.HY_OAS_SERIES}&api_key={key}"
           f"&file_type=json&observation_start={start}&sort_order=asc")
    j = json.loads(engine._get(url))
    rows = [(o["date"], float(o["value"])) for o in j.get("observations", [])
            if o["value"] not in (".", "", None)]
    s = pd.Series({pd.Timestamp(d): v for d, v in rows}).sort_index()
    print(f"HY OAS: {len(s)} daily obs from {s.index[0].date()} to {s.index[-1].date()}")
    return s


def fetch_vix_history(years):
    """Daily ^VIX closes as pd.Series indexed by date."""
    df = yf.download("^VIX", period=f"{years + 2}y", interval="1d",
                     auto_adjust=True, progress=False)
    s = df["Close"]
    # yf can hand back a single-column DataFrame or a Series depending on version.
    if isinstance(s, pd.DataFrame):
        s = s.iloc[:, 0]
    s = s.dropna()
    s.index = pd.to_datetime(s.index)
    print(f"^VIX: {len(s)} daily obs from {s.index[0].date()} to {s.index[-1].date()}")
    return s


# ---------------------------------------------- PIT risk-off computation

def pit_hy_oas_state(hy_series, pit_date):
    """Compute the same fields engine.fetch_hy_oas puts in its cache, but
    using ONLY observations strictly BEFORE pit_date. FRED publishes each
    day's HY OAS value the next business day, so a Friday scan can only see
    Thursday's value at best. Using pit_date's own observation would leak
    ~1 day of lookahead into the backtest (pre-prod review HIGH-H2).
    Returns dict or None if too little history."""
    pit = pd.Timestamp(pit_date)
    prior = hy_series.loc[hy_series.index < pit]
    if len(prior) < 60:
        return None
    lvl = float(prior.iloc[-1])
    prev = float(prior.iloc[-21]) if len(prior) >= 21 else float(prior.iloc[0])
    hi60 = float(prior.iloc[-60:].max())
    yr = prior.iloc[-252:] if len(prior) >= 252 else prior
    hi_1y = float(yr.max())
    pctile = int(round(100 * (yr <= lvl).mean()))
    delta_20d_bps = int(round((lvl - prev) * 100))
    return {
        "level_bps": int(round(lvl * 100)),
        "hi_60d_bps": int(round(hi60 * 100)),
        "hi_1y_bps": int(round(hi_1y * 100)),
        "delta_20d_bps": delta_20d_bps,
        "pctile_1y": pctile,
        "above_60d_hi": lvl > hi60,
        # match production's 0.5% tolerance for "at 1y high"
        "at_1y_hi": lvl >= hi_1y * 0.995,
    }


def pit_risk_off(pit_date, hy_series, vix_series):
    """Combine PIT HY OAS state + PIT VIX into the same risk_off tier
    production produces. Uses engine.compute_risk_off unchanged so any tuning
    of the rules stays consistent between backtest and production.
    Strict "< pit" boundary on both series so a Friday-close scan doesn't
    peek at Friday's HY OAS value (which FRED only publishes Saturday)."""
    hy_state = pit_hy_oas_state(hy_series, pit_date)
    pit = pd.Timestamp(pit_date)
    vix_pit = vix_series.loc[vix_series.index < pit]
    vix_last = float(vix_pit.iloc[-1]) if len(vix_pit) else None
    return engine.compute_risk_off(vix_last, hy_state), hy_state, vix_last


# --------------------------------------------------- portfolio simulator

def _pick_top_n(day_rows, top_n=TOP_N, sector_cap=False):
    """Given rows for one PIT date, return the top-N tickets applying the
    same filters production does: gate_regime==True, actionable setup, ranked
    by composite. If sector_cap, back-fill past the cap so we always end up
    with top_n names when the universe has enough alternatives."""
    eligible = day_rows[
        day_rows["gate_regime"] &
        day_rows["setup"].isin(ACTIONABLE)
    ].sort_values("composite", ascending=False)
    if not sector_cap:
        return eligible.head(top_n)
    picks = []
    sec_counts = {}
    for _, r in eligible.iterrows():
        sec = r["sector"] or "Unknown"
        if sec_counts.get(sec, 0) >= SECTOR_CAP:
            continue
        picks.append(r)
        sec_counts[sec] = sec_counts.get(sec, 0) + 1
        if len(picks) >= top_n:
            break
    return pd.DataFrame(picks) if picks else eligible.head(0)


def sim_portfolio(df, risk_off_by_date, sector_cap=False, regime_gate=False):
    """Walk each Friday, pick top-N per rules, compute equal-weight 21d gross
    excess-vs-SPY return, apply cost, apply size multiplier (halved / zeroed)
    when regime_gate. Returns per-date return series + audit rows."""
    out_rows = []
    for d, day_rows in df.groupby("date"):
        picks = _pick_top_n(day_rows, sector_cap=sector_cap)
        n = len(picks)
        if n == 0:
            # Consistent schema with populated rows so summarize() counts
            # empty Fridays as 0% return (pre-prod review MED-M1). Previously
            # emitted "gross"/"net" keys that became NaN and got skipped,
            # slightly overstating Sharpe.
            out_rows.append({"date": d, "n": 0,
                             "gross_excess": 0.0, "sized_return": 0.0,
                             "tier": "EMPTY", "mult": 0.0})
            continue
        # Equal-weight excess return over horizon (already net of cost + SPY in df["x21"]).
        gross_x = picks["x21"].mean()      # excess vs SPY, already 5bps-net per Phase A
        tier, mult = "NORMAL", 1.0
        if regime_gate:
            ro = risk_off_by_date.get(d)
            if ro is not None:
                tier = ro["tier"]
                mult = ro["size_mult"]
        out_rows.append({"date": d, "n": n,
                         "gross_excess": float(gross_x),
                         "sized_return": float(gross_x * mult),
                         "tier": tier, "mult": mult,
                         "picks": ",".join(picks["sym"].tolist()),
                         "sectors": ",".join((picks["sector"].fillna("?")).tolist())})
    return pd.DataFrame(out_rows).sort_values("date").reset_index(drop=True)


# --------------------------------------------------------------- metrics

def sharpe_annual(monthly_returns, periods_per_year=12):
    x = np.asarray(monthly_returns, dtype=float)
    x = x[~np.isnan(x)]
    if len(x) < 3 or x.std(ddof=1) == 0:
        return float("nan")
    return float(x.mean() / x.std(ddof=1) * math.sqrt(periods_per_year))


def max_drawdown(monthly_returns):
    """MaxDD of a compounded equity curve built from the monthly return series."""
    x = np.asarray(monthly_returns, dtype=float)
    x = np.nan_to_num(x, nan=0.0)
    equity = np.cumprod(1 + x)
    peaks = np.maximum.accumulate(equity)
    dd = equity / peaks - 1
    return float(dd.min())


def summarize(sim_df):
    """Take a per-Friday sim_df, resample to non-overlapping monthly (every 4th
    Friday) so Sharpe isn't inflated by 21d-overlapping windows, then compute
    standard metrics."""
    ret_col = "sized_return" if "sized_return" in sim_df.columns else "gross_excess"
    weekly = sim_df.set_index(pd.to_datetime(sim_df["date"]))[ret_col].sort_index()
    monthly = weekly.iloc[::4]
    return {
        "n_fridays": int(len(weekly)),
        "n_monthly_obs": int(len(monthly)),
        "mean_monthly_pct": round(float(monthly.mean() * 100), 3),
        "median_monthly_pct": round(float(monthly.median() * 100), 3),
        "std_monthly_pct": round(float(monthly.std(ddof=1) * 100), 3),
        "sharpe_annual": round(sharpe_annual(monthly), 3),
        "hit_rate_pct": round(float((monthly > 0).mean() * 100), 1),
        "max_dd_pct": round(max_drawdown(monthly) * 100, 2),
        "total_return_pct": round(float((np.cumprod(1 + monthly.fillna(0)) - 1).iloc[-1] * 100), 2),
    }


# ------------------------------------------------------- gate firing audit

def audit_firing_log(risk_off_by_date):
    """List each Friday where the gate would have fired, with the trigger
    detail. Sanity check: did it fire during known stress moments, and did it
    NOT fire during long calm stretches?"""
    fired = []
    for d in sorted(risk_off_by_date):
        ro = risk_off_by_date[d]
        if ro["tier"] != "NORMAL":
            fired.append({"date": str(pd.Timestamp(d).date()),
                          "tier": ro["tier"],
                          "triggers": " · ".join(ro["triggers"])})
    return fired


# ------------------------------------------------------------------- main

def main():
    fast = "--fast" in sys.argv
    tickers, sector_map = backtest_wf.load_universe()
    if fast:
        tickers = tickers[:40]
        years = 1
        print(f"[--fast] 40 names, {years}y")
    else:
        years = backtest_wf.LOOKBACK_YEARS

    # 1. Bars + walk-forward records (reuse Phase A)
    all_bars = backtest_wf.bulk_bars(tickers, years + 1)
    if "SPY" not in all_bars:
        print("FATAL: no SPY bars"); sys.exit(1)
    usable = [t for t in tickers if t in all_bars]

    print(f"\nRunning walk-forward on {len(usable)} names (reusing Phase A harness)...")
    df = backtest_wf.walk_forward(all_bars, sector_map, usable)
    if df.empty:
        print("FATAL: no records"); sys.exit(1)
    print(f"  {len(df)} rows, dates {df['date'].min()} -> {df['date'].max()}")

    # 2. Historical HY OAS + VIX
    print("\nFetching historical HY OAS + VIX ...")
    hy = fetch_hy_oas_history()
    vix = fetch_vix_history(years)

    # 3. PIT risk-off state for every unique Friday
    print("Computing PIT risk-off state per Friday ...")
    dates = sorted(df["date"].unique())
    risk_off_by_date = {}
    hy_state_by_date = {}
    for d in dates:
        ro, hy_state, vix_last = pit_risk_off(d, hy, vix)
        risk_off_by_date[d] = ro
        hy_state_by_date[d] = {"hy": hy_state, "vix": vix_last}

    # 4. Four portfolio variants
    print("\nSimulating 4 portfolio variants (top-8 equal-weight, 21d hold)...")
    variants = {
        "base":            {"sector_cap": False, "regime_gate": False},
        "sector_capped":   {"sector_cap": True,  "regime_gate": False},
        "regime_gated":    {"sector_cap": False, "regime_gate": True},
        "both":            {"sector_cap": True,  "regime_gate": True},
    }
    sims = {}
    summaries = {}
    for name, kw in variants.items():
        sim = sim_portfolio(df, risk_off_by_date, **kw)
        sims[name] = sim
        summaries[name] = summarize(sim)

    # 5. Firing audit
    firing_log = audit_firing_log(risk_off_by_date)

    # ================================================================ PRINT
    print("\n" + "=" * 78)
    print(" PHASE B BACKTEST -- top-8 portfolio, monthly non-overlapping")
    print("=" * 78)
    hdr = f"  {'variant':<16} {'total':>8} {'mean/mo':>9} {'std/mo':>8} {'Sharpe':>8} {'MaxDD':>8} {'hit%':>6} {'T':>4}"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for name in ("base", "sector_capped", "regime_gated", "both"):
        s = summaries[name]
        print(f"  {name:<16} {s['total_return_pct']:>+7.2f}% "
              f"{s['mean_monthly_pct']:>+7.3f}% "
              f"{s['std_monthly_pct']:>7.3f}% "
              f"{s['sharpe_annual']:>8.2f} "
              f"{s['max_dd_pct']:>+7.2f}% "
              f"{s['hit_rate_pct']:>5.1f}% "
              f"{s['n_monthly_obs']:>4}")

    print(f"\nRisk-off gate firing log ({len(firing_log)} of {len(dates)} Fridays):")
    if not firing_log:
        print("  (never fired -- either the test window was calm, or the gate is too strict)")
    else:
        # Compact: show first 3, count of each tier, and each contiguous cluster
        tier_counts = {}
        for e in firing_log:
            tier_counts[e["tier"]] = tier_counts.get(e["tier"], 0) + 1
        print(f"  tier counts: {tier_counts}")
        # Cluster by consecutive Fridays
        prev_dt = None
        clusters = []
        for e in firing_log:
            dt = pd.Timestamp(e["date"])
            if prev_dt is None or (dt - prev_dt).days > 21:
                clusters.append([e])
            else:
                clusters[-1].append(e)
            prev_dt = dt
        print(f"  {len(clusters)} clusters:")
        for cl in clusters[:10]:
            first, last = cl[0], cl[-1]
            trigs = sorted({t for e in cl for t in e["triggers"].split(" · ")})
            print(f"    {first['date']} -> {last['date']} "
                  f"({len(cl)} Fridays, worst tier={max((e['tier'] for e in cl), key=lambda x:{'NORMAL':0,'HALF_SIZE':1,'NO_ADDS':2}[x])})")
            for t in trigs[:4]:
                print(f"        · {t}")

    # ---------- save
    os.makedirs(DATA, exist_ok=True)
    out = {
        "run_date": date.today().isoformat(),
        "years": years,
        "top_n": TOP_N,
        "horizon": HORIZON,
        "cost_bps_roundtrip": backtest_wf.COST_BPS_ROUNDTRIP,
        "sector_cap": SECTOR_CAP,
        "records": len(df),
        "variants": summaries,
        "gate_firing_days": len(firing_log),
        "gate_firing_log": firing_log,
        "caveats": [
            "Universe = today's screener list -> survivorship-biased.",
            "PIT VIX is a spot value (no term-structure PIT).",
            "Analyze() still runs with earnings_date=None so TR06 gate doesn't fire "
            "historically -- same limitation as Phase A.",
            "Regime gate simulates HALF_SIZE by halving basket return (as if sizing was halved "
            "on each open position), and NO_ADDS by holding cash for that basket (0% return).",
            "'both' compounds sector cap + regime gate on the same top-N picks each Friday.",
        ],
    }
    outp = os.path.join(DATA, "backtest_phaseB.json")
    json.dump(out, open(outp, "w", encoding="utf-8"), indent=2, default=str)
    print(f"\nWrote {outp}")


if __name__ == "__main__":
    main()
