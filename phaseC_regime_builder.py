"""
phaseC_regime_builder.py -- build the (VIX × HY-OAS × SPX-trend) regime lookup.

Each production card should tell the trader: "the last time this regime cell
looked like today, the top-quintile of the scanner returned X% forward 21d
with Y% hit rate." This module builds the lookup once from historical data;
engine.py reads it at scan time and attaches a regime_edge to every card.

Method:
  - Reuse backtest_wf.walk_forward for per-(date, ticker) records + fwd rets
  - Same PIT VIX + HY OAS pull as backtest_phaseB
  - Enrich each row with PIT (vix_bucket, hy_oas_bucket, spx_trend_bucket)
  - Bucketize by top-quintile membership (Q5 of composite)
  - Aggregate mean forward 21d excess return + hit rate + N per bucket
  - Fall back to marginal (single-dim) averages for bucket cells with N<20

Buckets deliberately coarse (3×3×2 = 18) to keep per-bucket N usable at 3y.

Output: data/regime_lookup.json  -- consumed by engine.py at scan time.
"""

import json, os, sys, warnings, math
from datetime import date
import numpy as np
import pandas as pd
import yfinance as yf

import engine
import backtest_wf
import backtest_phaseB

warnings.filterwarnings("ignore")

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
LOOKUP_PATH = os.path.join(DATA, "regime_lookup.json")

# Bucket definitions -- also referenced by engine.current_regime_bucket() so
# any change here MUST be mirrored there (or moved to a shared constants file).
VIX_BUCKETS = [("LOW", 0, 20), ("MID", 20, 27), ("HI", 27, 999)]
HY_PCTILE_BUCKETS = [("CALM", 0, 30), ("NORMAL", 30, 70), ("WIDE", 70, 101)]
# SPX_TREND has just 2 states: UP (above 200d and slope rising) or DOWN.
MIN_BUCKET_N = 20                 # below this, fall back to marginal averages


def vix_bucket(v):
    if v is None: return "MID"
    for name, lo, hi in VIX_BUCKETS:
        if lo <= v < hi: return name
    return "HI"


def hy_bucket(pctile):
    if pctile is None: return "NORMAL"
    for name, lo, hi in HY_PCTILE_BUCKETS:
        if lo <= pctile < hi: return name
    return "WIDE"


def spx_bucket(above_200d, rising):
    return "UP" if (above_200d and rising) else "DOWN"


# ---------------------------- SPX 200d state per PIT date

def pit_spx_state(spy_closes_list, spy_dates_list):
    """Return dict date_str -> (above_200d bool, 200d_rising bool)."""
    s = pd.Series(spy_closes_list, index=pd.to_datetime(spy_dates_list))
    sma200 = s.rolling(200).mean()
    sma200_21d_ago = sma200.shift(21)
    out = {}
    for d, close in s.items():
        m = sma200.get(d)
        m_prev = sma200_21d_ago.get(d)
        if pd.isna(m) or pd.isna(m_prev):
            continue
        out[d.strftime("%Y-%m-%d")] = {
            "above_200d": bool(close > m),
            "rising_200d": bool(m > m_prev),
        }
    return out


# ---------------------------------------------------------- main

def main():
    tickers, sector_map = backtest_wf.load_universe()
    years = backtest_wf.LOOKBACK_YEARS

    print(f"Downloading bars for {len(tickers)} names + SPY ({years + 1}y)...")
    all_bars = backtest_wf.bulk_bars(tickers, years + 1)
    if "SPY" not in all_bars:
        print("FATAL: no SPY"); sys.exit(1)
    usable = [t for t in tickers if t in all_bars]

    print(f"\nWalk-forward on {len(usable)} names (reusing Phase A harness)...")
    df = backtest_wf.walk_forward(all_bars, sector_map, usable)
    if df.empty:
        print("FATAL: no records"); sys.exit(1)

    # PIT VIX + HY OAS (reuse Phase B helpers)
    print("\nFetching PIT VIX + HY OAS...")
    vix_s = backtest_phaseB.fetch_vix_history(years)
    hy_s = backtest_phaseB.fetch_hy_oas_history()

    # PIT SPX trend
    spy_bars = all_bars["SPY"]
    spx_state = pit_spx_state(
        [b["c"] for b in spy_bars],
        [b["d"] for b in spy_bars])

    # Enrich df with bucket columns
    print("Enriching rows with regime buckets...")
    unique_dates = sorted(df["date"].unique())
    date_ctx = {}
    for d in unique_dates:
        pit = pd.Timestamp(d)
        vix_prior = vix_s.loc[:pit]
        vix_val = float(vix_prior.iloc[-1]) if len(vix_prior) else None
        hy_state = backtest_phaseB.pit_hy_oas_state(hy_s, d)
        hy_pct = hy_state["pctile_1y"] if hy_state else None
        spx = spx_state.get(d, {"above_200d": True, "rising_200d": True})
        date_ctx[d] = {
            "vix": vix_val,
            "vix_bucket": vix_bucket(vix_val),
            "hy_pctile": hy_pct,
            "hy_bucket": hy_bucket(hy_pct),
            "spx_bucket": spx_bucket(spx["above_200d"], spx["rising_200d"]),
        }

    df["vix_bucket"] = df["date"].map(lambda d: date_ctx[d]["vix_bucket"])
    df["hy_bucket"] = df["date"].map(lambda d: date_ctx[d]["hy_bucket"])
    df["spx_bucket"] = df["date"].map(lambda d: date_ctx[d]["spx_bucket"])

    # Within-date composite quintile so "Q5" = model's top pick that day
    def _q(s):
        try:
            return pd.qcut(s.rank(method="first"), 5, labels=False, duplicates="drop")
        except ValueError:
            return pd.Series([np.nan] * len(s), index=s.index)
    df["q"] = df.groupby("date")["composite"].transform(_q)

    q5 = df[df["q"] == 4].dropna(subset=["x21"]).copy()
    baseline = {
        "mean_x21_pct": round(float(q5["x21"].mean() * 100), 3),
        "hit_pct": round(float((q5["x21"] > 0).mean() * 100), 1),
        "n": int(len(q5)),
    }
    print(f"\nBaseline Q5 excess-21d over 3y: {baseline['mean_x21_pct']}% "
          f"(hit {baseline['hit_pct']}%, n={baseline['n']})")

    # Aggregate per full 3-D bucket
    print("Aggregating per (vix, hy, spx) bucket...")
    buckets = {}
    for (vb, hb, sb), grp in q5.groupby(["vix_bucket", "hy_bucket", "spx_bucket"]):
        key = f"{vb}|{hb}|{sb}"
        mean_x = float(grp["x21"].mean())
        hit = float((grp["x21"] > 0).mean())
        buckets[key] = {
            "mean_x21_pct": round(mean_x * 100, 3),
            "hit_pct": round(hit * 100, 1),
            "n": int(len(grp)),
            "edge_mult": round(mean_x / (baseline["mean_x21_pct"] / 100), 2)
                         if baseline["mean_x21_pct"] != 0 else None,
        }

    # Marginal fallbacks (single-dim buckets, used when 3-D cell has N<MIN_BUCKET_N)
    marginals = {}
    for dim, col in [("vix", "vix_bucket"), ("hy", "hy_bucket"), ("spx", "spx_bucket")]:
        for name, grp in q5.groupby(col):
            key = f"{dim}:{name}"
            mean_x = float(grp["x21"].mean())
            marginals[key] = {
                "mean_x21_pct": round(mean_x * 100, 3),
                "hit_pct": round(float((grp["x21"] > 0).mean() * 100), 1),
                "n": int(len(grp)),
                "edge_mult": round(mean_x / (baseline["mean_x21_pct"] / 100), 2)
                             if baseline["mean_x21_pct"] != 0 else None,
            }

    # Print a compact matrix
    print("\n" + "=" * 70)
    print(" REGIME LOOKUP -- Q5 forward-21d excess return by bucket")
    print(f" (baseline = {baseline['mean_x21_pct']:+.2f}%, hit {baseline['hit_pct']:.1f}%)")
    print("=" * 70)
    print(f"  {'VIX':<5} {'HY':<7} {'SPX':<5}   {'mean':>8} {'hit':>6} {'n':>5} {'edge×':>6}")
    print("  " + "-" * 55)
    for vb in ["LOW", "MID", "HI"]:
        for hb in ["CALM", "NORMAL", "WIDE"]:
            for sb in ["UP", "DOWN"]:
                key = f"{vb}|{hb}|{sb}"
                b = buckets.get(key)
                if b:
                    mark = "  " if b["n"] >= MIN_BUCKET_N else "*"
                    print(f"  {vb:<5} {hb:<7} {sb:<5} {mark}"
                          f"{b['mean_x21_pct']:>+7.2f}% "
                          f"{b['hit_pct']:>5.1f}% "
                          f"{b['n']:>5} "
                          f"{b['edge_mult']:>6.2f}×" if b['edge_mult'] is not None else '   n/a')
    print("  (rows starred = N<20, engine.py falls back to marginals)")

    print("\nMarginals (single-dim):")
    for k, v in sorted(marginals.items()):
        print(f"  {k:<12} {v['mean_x21_pct']:>+7.2f}%  hit {v['hit_pct']:>5.1f}%  n={v['n']:>5}  {v['edge_mult']:>6.2f}×")

    # Save
    out = {
        "built": date.today().isoformat(),
        "years": years,
        "records": int(len(df)),
        "q5_records": int(len(q5)),
        "baseline": baseline,
        "min_bucket_n": MIN_BUCKET_N,
        "buckets": buckets,
        "marginals": marginals,
        "vix_bucket_defs": [[n, lo, hi] for n, lo, hi in VIX_BUCKETS],
        "hy_pctile_bucket_defs": [[n, lo, hi] for n, lo, hi in HY_PCTILE_BUCKETS],
        "notes": [
            "Built from Phase A walk-forward records (real engine.analyze on PIT slices).",
            "Q5 = within-date top quintile of composite. Bucket cell = "
            "Q5 rows whose PIT regime landed in that cell.",
            "edge_mult = bucket_mean / baseline_mean. >1 = the model works BETTER "
            "in this regime; <1 = alpha is weaker here.",
            "Marginals used for cells with N<{}.".format(MIN_BUCKET_N),
        ],
    }
    json.dump(out, open(LOOKUP_PATH, "w", encoding="utf-8"), indent=2)
    print(f"\nWrote {LOOKUP_PATH}")


if __name__ == "__main__":
    main()
