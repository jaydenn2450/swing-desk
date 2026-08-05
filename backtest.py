"""
backtest.py — quick walk-forward sanity check of the swing-desk composite score.

Question answered: do names our score ranks highly actually outperform SPY over
the next month? This is a *sanity check*, not a full simulation: no slippage,
no position management, weekly sampling, and the universe is TODAY'S screener
list (survivorship bias inflates everything — read results as relative decile
spread, not absolute returns).

Method:
  - 3y of daily data for the current auto-universe (+ SPY)
  - every Friday: compute a vectorized approximation of the composite score
    per name (trend / RS / participation / structure / momentum buckets,
    spec weights 25/25/20/15/10/5)
  - rank into quintiles; measure forward 21-trading-day excess return vs SPY
  - report per-quintile mean/median excess return + hit rate, plus the same
    for names passing the regime gate (RG01+RG02) vs failing

Usage:  python backtest.py
"""

import json, os, warnings
import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")
ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
FWD = 21  # forward horizon, trading days

def load_universe():
    p = os.path.join(DATA, "universe_auto.json")
    if os.path.exists(p):
        return json.load(open(p, encoding="utf-8"))["tickers"]
    return [l.split()[0].upper() for l in open(os.path.join(ROOT, "universe.txt"), encoding="utf-8")
            if l.strip() and not l.startswith("#")]

def main():
    tickers = load_universe()
    print(f"Downloading 3y daily for {len(tickers)} names + SPY ...")
    px = yf.download(tickers + ["SPY"], period="3y", interval="1d",
                     auto_adjust=True, progress=False)
    close, vol = px["Close"].ffill(), px["Volume"]
    spy = close["SPY"]
    close = close.drop(columns=["SPY"]).dropna(axis=1, thresh=int(len(close) * 0.9))
    vol = vol[close.columns]
    print(f"  usable names: {close.shape[1]}, bars: {len(close)}")

    s20, s50, s200 = close.rolling(20).mean(), close.rolling(50).mean(), close.rolling(200).mean()
    trend = (0.25 * (close > s200) + 0.25 * (s200 > s200.shift(21))
             + 0.20 * ((s20 > s50) & (s50 > s200)) + 0.30 * (close > s50)).astype(float) * 100

    r12_1 = close.shift(21) / close.shift(252) - 1
    rs = r12_1.rank(axis=1, pct=True) * 100

    up = vol.where(close.diff() > 0, 0).rolling(50).sum()
    dn = vol.where(close.diff() < 0, 0).rolling(50).sum()
    ud = (up / dn).clip(0, 3)
    part = ((ud - 0.8) * 50).clip(0, 100)

    off_hi = 1 - close / close.rolling(252).max()
    struct = ((1 - off_hi / 0.25) * 100).clip(0, 100)

    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/14).mean()
    rsi = 100 - 100 / (1 + gain / loss)
    mom = ((rsi >= 50) & (rsi <= 75)).astype(float) * 70 + ((rsi > 40) & (rsi < 50)).astype(float) * 35

    composite = (0.25 * trend + 0.25 * rs + 0.20 * part + 0.15 * struct + 0.10 * mom) / 0.95
    gate = (close > s200) & (s200 > s200.shift(21))

    fwd = close.shift(-FWD) / close - 1
    fwd_spy = spy.shift(-FWD) / spy - 1
    excess = fwd.sub(fwd_spy, axis=0)

    fridays = [d for d in close.index[252:-FWD] if d.weekday() == 4]
    rows = []
    for d in fridays:
        c = composite.loc[d].dropna()
        e = excess.loc[d].reindex(c.index)
        g = gate.loc[d].reindex(c.index)
        q = pd.qcut(c.rank(method="first"), 5, labels=False)
        for name, mask in [("Q1 (worst)", q == 0), ("Q2", q == 1), ("Q3", q == 2),
                           ("Q4", q == 3), ("Q5 (best)", q == 4),
                           ("gate PASS", g == True), ("gate FAIL", g == False)]:
            ee = e[mask].dropna()
            if len(ee):
                rows.append({"bucket": name, "date": d, "mean": ee.mean(),
                             "hit": (ee > 0).mean(), "n": len(ee)})
    df = pd.DataFrame(rows)
    agg = df.groupby("bucket").agg(
        mean_excess_21d=("mean", "mean"), median_weekly=("mean", "median"),
        hit_rate=("hit", "mean"), avg_names=("n", "mean"), weeks=("date", "nunique"))
    agg["mean_excess_21d"] = (agg["mean_excess_21d"] * 100).round(2)
    agg["median_weekly"] = (agg["median_weekly"] * 100).round(2)
    agg["hit_rate"] = (agg["hit_rate"] * 100).round(1)
    agg["avg_names"] = agg["avg_names"].round(0)
    order = ["Q1 (worst)", "Q2", "Q3", "Q4", "Q5 (best)", "gate PASS", "gate FAIL"]
    agg = agg.reindex(order)
    print("\n=== Composite-score quintiles -> forward 21d excess return vs SPY ===")
    print(agg.to_string())
    spread = agg.loc['Q5 (best)', 'mean_excess_21d'] - agg.loc['Q1 (worst)', 'mean_excess_21d']
    print(f"\nQ5-Q1 spread: {round(spread, 2)}% per 21d "
          f"| gate PASS-FAIL: {round(agg.loc['gate PASS','mean_excess_21d'] - agg.loc['gate FAIL','mean_excess_21d'], 2)}%")
    print("CAVEATS: survivorship-biased universe (today's list), no costs, "
          "overlapping windows, weekly sampling. Use for relative ranking only.")
    os.makedirs(DATA, exist_ok=True)
    agg.reset_index().to_json(os.path.join(DATA, "backtest_results.json"), orient="records")
    print("Wrote data/backtest_results.json")

if __name__ == "__main__":
    main()
