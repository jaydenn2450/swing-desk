"""
backtest_pead.py -- does the PEAD sub-scorer order POST_EVENT_DRIFT names
better than the base composite?

Method:
  - Reuse backtest_wf.walk_forward to get per-row records (composite AND pead
    both recorded per row when applicable)
  - Filter to setup=="POST_EVENT_DRIFT" and pead not None
  - On that subset, quintile-forward-return by (a) composite and (b) pead
  - Also compare top-3-by-each: what's the mean fwd 21d if you took the
    top-3 PEAD names each week by pead-score vs composite-score?

If pead-quintile spread beats composite-quintile spread on the same subset,
the sub-scorer earns its keep. If it doesn't, we ship the display anyway
because it's cleaner UI, but stop believing it adds alpha over composite.
"""

import json, os, sys, warnings
from datetime import date
import numpy as np
import pandas as pd

import backtest_wf

warnings.filterwarnings("ignore")

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
OUT_PATH = os.path.join(DATA, "backtest_pead.json")


def quintile_stats(sub, score_col, ret_col="x21"):
    if len(sub) < 25:
        return None
    if score_col not in sub.columns or sub[score_col].notna().sum() < 25:
        return None
    def _q(s):
        try:
            return pd.qcut(s.rank(method="first"), 5, labels=False, duplicates="drop")
        except ValueError:
            return pd.Series([np.nan] * len(s), index=s.index)
    d = sub.copy()
    d["q"] = _q(d[score_col])
    g = d.dropna(subset=[ret_col, "q"]).groupby("q")[ret_col]
    return {
        f"Q{int(i)+1}": {
            "mean_pct": round(float(x.mean() * 100), 2),
            "hit_pct": round(float((x > 0).mean() * 100), 1),
            "n": int(len(x)),
        }
        for i, x in g
    }


def top_n_per_date(sub, score_col, n=3, ret_col="x21"):
    """For each date, take top-n rows by score_col, take mean x21. Then
    average across dates. Simple top-N portfolio equivalent."""
    if score_col not in sub.columns or sub[score_col].notna().sum() < n:
        return None
    # Drop rows with NaN in score_col so nlargest can order numerically
    # (all-None column would raise TypeError on nlargest, day-2 audit fix).
    sub = sub.dropna(subset=[score_col])
    per_date = []
    for d, day in sub.groupby("date"):
        picks = day.nlargest(n, score_col)
        if len(picks):
            per_date.append({"date": d, "n": len(picks),
                             "mean_x21": float(picks[ret_col].mean())})
    if not per_date:
        return None
    df = pd.DataFrame(per_date)
    return {
        "n_dates": int(len(df)),
        "mean_pct": round(float(df["mean_x21"].mean() * 100), 3),
        "median_pct": round(float(df["mean_x21"].median() * 100), 3),
        "hit_pct": round(float((df["mean_x21"] > 0).mean() * 100), 1),
    }


def main():
    fast = "--fast" in sys.argv
    tickers, sector_map = backtest_wf.load_universe()
    years = 1 if fast else backtest_wf.LOOKBACK_YEARS
    if fast:
        tickers = tickers[:40]
        print(f"[--fast] 40 names, {years}y")

    print(f"Downloading bars ({years + 1}y)...")
    all_bars = backtest_wf.bulk_bars(tickers, years + 1)
    if "SPY" not in all_bars:
        print("FATAL: no SPY"); sys.exit(1)
    usable = [t for t in tickers if t in all_bars]

    print(f"\nWalk-forward on {len(usable)} names ...")
    df = backtest_wf.walk_forward(all_bars, sector_map, usable)
    if df.empty:
        print("FATAL: no records"); sys.exit(1)

    # After the day-1 PEAD-scorer rollback, walk_forward no longer records a
    # 'pead' column. This validator is kept for future candidate scorers:
    # to use it, restore sc["pead"] computation in engine.analyze() and add
    # 'pead' back to the row schema in backtest_wf.walk_forward(). Until then
    # this script only reports composite-based stats on the PEAD subset.
    if "pead" not in df.columns:
        df = df.copy()
        df["pead"] = None
        print("\n[note] 'pead' column not present in walk-forward output — the "
              "sub-scorer is currently rolled back. Reporting composite stats "
              "on the POST_EVENT_DRIFT subset only.")
    if "shock_move_pct" not in df.columns:
        df["shock_move_pct"] = None
    pead_rows = df[df["setup"] == "POST_EVENT_DRIFT"].copy()
    print(f"\nTotal rows: {len(df):,}")
    print(f"POST_EVENT_DRIFT rows: {len(pead_rows):,} "
          f"({len(pead_rows) / len(df) * 100:.1f}% of universe)")
    print(f"Dates with any PEAD setup: {pead_rows['date'].nunique()}")

    if len(pead_rows) < 100:
        print("Not enough PEAD observations to test rigorously. "
              "Reporting anyway with wide error bars.")

    # ---------- quintile spreads
    print("\n=== Quintile-forward-return on POST_EVENT_DRIFT subset ===")
    for score in ("composite", "pead"):
        stats = quintile_stats(pead_rows, score)
        if not stats:
            print(f"  {score}: too thin"); continue
        q1 = stats.get("Q1", {}).get("mean_pct", 0)
        q5 = stats.get("Q5", {}).get("mean_pct", 0)
        spread = q5 - q1
        print(f"\n  By {score.upper()}:")
        for qn, row in stats.items():
            print(f"    {qn}: mean {row['mean_pct']:+6.2f}%  hit {row['hit_pct']:5.1f}%  n={row['n']}")
        print(f"    Q5-Q1 spread: {spread:+.2f}%")

    # ---------- top-3 comparison
    print("\n=== Top-3-per-date portfolio (equal-weight, 21d) ===")
    for score in ("composite", "pead"):
        t = top_n_per_date(pead_rows, score, n=3)
        if not t:
            print(f"  {score}: too thin"); continue
        print(f"  By {score.upper():<9}: mean {t['mean_pct']:+7.3f}% · "
              f"median {t['median_pct']:+7.3f}% · "
              f"hit {t['hit_pct']:5.1f}% · dates {t['n_dates']}")

    # ---------- shock-size sanity (only meaningful when the column is populated)
    if pead_rows["shock_move_pct"].notna().any():
        print("\n=== Sanity: shock-size distribution of PEAD rows ===")
        ms = pead_rows["shock_move_pct"].dropna().describe()
        print(f"  count {int(ms['count'])} mean {ms['mean']:+.2f}% "
              f"median {ms['50%']:+.2f}% min {ms['min']:+.2f}% max {ms['max']:+.2f}%")
    else:
        print("\n=== Shock-size distribution unavailable (column empty) ===")
        ms = pead_rows["shock_move_pct"].describe()   # all-NaN describe returns count=0

    # ---------- save
    out = {
        "run_date": date.today().isoformat(),
        "years": years,
        "records_total": int(len(df)),
        "records_pead_subset": int(len(pead_rows)),
        "quintile_composite": quintile_stats(pead_rows, "composite"),
        "quintile_pead": quintile_stats(pead_rows, "pead"),
        "top3_composite": top_n_per_date(pead_rows, "composite", n=3),
        "top3_pead": top_n_per_date(pead_rows, "pead", n=3),
        "shock_move_stats": {k: (float(v) if pd.notna(v) else None)
                              for k, v in ms.to_dict().items()},
    }
    os.makedirs(DATA, exist_ok=True)
    json.dump(out, open(OUT_PATH, "w", encoding="utf-8"), indent=2, default=str)
    print(f"\nWrote {OUT_PATH}")


if __name__ == "__main__":
    main()
