"""
backtest_earnings.py — validate the earnings-preview recommendation.

The preview's default advice is: DON'T hold a swing long through the print;
wait, then trade the reaction (our POST_EVENT_DRIFT setup). This tests whether
that advice actually holds up, rather than just sounding prudent.

Three strategies, measured over the same events:
  A  HOLD THROUGH : buy the close before the print, exit 10 sessions later
  B  DRIFT (up)   : skip the print; buy the close of the reaction day only if it
                    closed UP, exit 10 sessions later
  C  DRIFT (any)  : buy the reaction-day close regardless of direction

Reported per strategy: mean/median return, hit rate, worst case, and the
standard deviation of outcomes (the binary-risk cost of A shows up here).

Usage:  python backtest_earnings.py
"""
import sys, os, statistics as st
import warnings
warnings.filterwarnings("ignore")
import yfinance as yf

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import engine

HOLD_DAYS = 10
NAMES = ["AAPL", "MSFT", "NVDA", "AMD", "AVGO", "ANET", "CRM", "ADBE", "NOW", "PANW",
         "CAT", "DE", "GE", "HON", "RTX", "UNP", "JPM", "BAC", "GS", "V",
         "JNJ", "LLY", "MRK", "PG", "KO", "MCD", "COST", "WMT", "XOM", "CVX"]


def events_for(sym):
    """(reaction_day_index, pre_close, reaction_close) for each past print."""
    try:
        t = yf.Ticker(sym)
        ed = t.earnings_dates
        bars = engine.fetch_history(sym, "5y")
    except Exception:
        return []
    if ed is None or ed.empty or not bars:
        return []
    days = [b["d"] for b in bars]
    closes = [b["c"] for b in bars]
    idx = {d: i for i, d in enumerate(days)}
    import datetime
    out = []
    for ts, _row in ed.iterrows():
        try:
            d = ts.date()
            amc = ts.hour >= 12
        except Exception:
            continue
        if d >= datetime.date.today():
            continue
        ds = d.isoformat()
        react = (next((x for x in days if x > ds), None) if amc
                 else (ds if ds in idx else next((x for x in days if x >= ds), None)))
        if not react or react not in idx:
            continue
        i = idx[react]
        if i == 0 or i + HOLD_DAYS >= len(closes):
            continue
        out.append((i, closes[i - 1], closes[i]))
    return out


def main():
    A, B, C = [], [], []
    print(f"Collecting earnings events for {len(NAMES)} names (5y) ...")
    for n, sym in enumerate(NAMES, 1):
        evs = events_for(sym)
        for i, pre, react in evs:
            bars = None
            # forward return from each entry
            pass
        # need closes again; refetch once per symbol
        try:
            bars = engine.fetch_history(sym, "5y")
        except Exception:
            continue
        closes = [b["c"] for b in bars]
        for i, pre, react in evs:
            fwd = closes[i + HOLD_DAYS]
            A.append(100 * (fwd / pre - 1))                 # held through the print
            C.append(100 * (fwd / react - 1))               # bought the reaction
            if react > pre:                                  # reaction closed up
                B.append(100 * (fwd / react - 1))
        if n % 10 == 0:
            print(f"  ... {n}/{len(NAMES)}")

    def rep(name, xs):
        if not xs:
            print(f"{name:22s} no data"); return
        xs_s = sorted(xs)
        print(f"{name:22s} n={len(xs):4d}  mean {st.mean(xs):+6.2f}%  median {st.median(xs):+6.2f}%  "
              f"hit {100*sum(1 for x in xs if x>0)/len(xs):4.1f}%  "
              f"sd {st.pstdev(xs):5.2f}  worst {xs_s[0]:+7.2f}%  p05 {xs_s[max(0,len(xs)//20)]:+6.2f}%")

    print(f"\n=== Forward {HOLD_DAYS}-session return by strategy ===")
    rep("A HOLD THROUGH", A)
    rep("B DRIFT (up only)", B)
    rep("C DRIFT (any)", C)

    if A and B:
        print(f"\nRisk-adjusted (mean/sd):  A {st.mean(A)/st.pstdev(A):.3f}   "
              f"B {st.mean(B)/st.pstdev(B):.3f}   C {st.mean(C)/st.pstdev(C):.3f}")
        print(f"Downside tail (5th pct):  A {sorted(A)[len(A)//20]:+.2f}%   "
              f"B {sorted(B)[len(B)//20]:+.2f}%   C {sorted(C)[len(C)//20]:+.2f}%")
        print("\nRead: if B beats A on mean/sd and has a shallower tail, 'wait for the print,")
        print("then trade the reaction' is the better default for a long-only swing book.")
    print("\nCaveats: survivorship-biased name list, no costs/slippage, fixed 10-day exit,")
    print("no stop-loss modelled (A's real-world tail is worse than shown).")


if __name__ == "__main__":
    main()
