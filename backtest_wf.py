"""
backtest_wf.py -- walk-forward harness that runs the SHIPPED scorer.

This replaces the vectorized approximation in backtest.py. For each historical
Friday it slices each name's bars to point-in-time, calls engine.analyze
unchanged, and measures forward returns at horizons 1/3/5/10/21 trading days.
Because we call the real scorer, the Q5-Q1 spread reported here is on the same
model that ships in the dashboard -- closing the gap flagged in REVIEW.md.

What it produces (data/backtest_wf.json + console):
  - Base results: per-quintile mean/median/hit at each horizon, decay curve,
    regime-gate PASS/FAIL, Deflated Sharpe, PBO
  - Four-variant comparison at 21d: base, sector-neutral, TR06-filtered, both
      * sector-neutral quintiles within (date, sector) then aggregated —
        removes the "Q5 is just whatever sector rallied" beta noise
      * TR06-filtered drops rows within EARNINGS_BLACKOUT_DAYS of a print,
        using yfinance historical earnings_dates (cached in data/)

Caveats -- printed at the end of the run too:
  1. Universe = today's auto-screener list. Names that got delisted between
     the lookback start and today are invisible. Read absolute returns
     skeptically; relative Q5-Q1 spread is what matters.
  2. Historical earnings dates come from yfinance Ticker.earnings_dates,
     which typically only returns ~4-8 past quarters per name. Older PIT
     dates therefore have thin coverage -- the TR06-filtered variant is
     a lower bound on the earnings-blackout effect (some prints we don't
     see are still there).
  3. Sector map is today's mapping. Sector reassignments are rare enough to
     ignore at swing horizons.
  4. Returns use split/dividend-adjusted closes (yfinance auto_adjust=True)
     which matches what engine.fetch_history produces.

Usage:  python backtest_wf.py [--fast]     # --fast: 40-name / 1y smoke test
"""

import json, math, os, sys, time, warnings, itertools, random
from datetime import date
import numpy as np
import pandas as pd
import yfinance as yf

# Import the real scorer. This is the whole point of the harness.
import engine

warnings.filterwarnings("ignore")

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")

# ------------------------------------------------------------------- config
LOOKBACK_YEARS = 3
BAR_BUFFER_DAYS = 260              # need >= MIN_BARS (220) before first PIT date
HORIZONS = (1, 3, 5, 10, 21)
COST_BPS_ROUNDTRIP = 5             # 5 bps applied once per trade, all horizons
COST_FRAC = COST_BPS_ROUNDTRIP / 1e4
MIN_BARS = engine.MIN_BARS         # 220 as of engine v0.2

# Deflated Sharpe / PBO
DSR_TRIALS = 20                    # assumed number of scorer variants a human might have tried
PBO_N_CONFIGS = 20                 # alt weight vectors sampled for PBO
PBO_S = 16                         # CSCV submatrices (even; C(16,8)=12870 splits)
PBO_SEED = 42

# The six bucket names, in the order/weights of the shipped composite.
BUCKETS = ("trend", "relative_strength", "participation",
           "structure", "momentum", "volatility")
SHIPPED_WEIGHTS = np.array([0.25, 0.25, 0.20, 0.15, 0.10, 0.05])

# TR06 blackout: same window engine.py uses in production (calendar days).
EARNINGS_BLACKOUT_DAYS = engine.EARNINGS_BLACKOUT_DAYS
EARNINGS_CACHE_TTL_DAYS = 14
MIN_SECTOR_NAMES = 5               # sector needs >= this many names on a date to quintile


# --------------------------------------------------------- universe & bars

def load_universe():
    p = os.path.join(DATA, "universe_auto.json")
    if os.path.exists(p):
        j = json.load(open(p, encoding="utf-8"))
        return j["tickers"], j.get("sectors", {})
    tickers = [l.split()[0].upper()
               for l in open(os.path.join(ROOT, "universe.txt"), encoding="utf-8")
               if l.strip() and not l.startswith("#")]
    return tickers, {}


def bulk_bars(tickers, years):
    """Download years of daily bars for `tickers` + SPY. Returns
       (name -> list[bar-dict]) in the exact shape engine.analyze expects."""
    print(f"Downloading {years}y daily for {len(tickers)} names + SPY ...")
    t0 = time.time()
    df = yf.download(tickers + ["SPY"], period=f"{years}y", interval="1d",
                     auto_adjust=True, progress=False, group_by="ticker",
                     threads=True)
    print(f"  fetched in {time.time() - t0:.1f}s")
    out = {}
    for sym in tickers + ["SPY"]:
        try:
            sub = df[sym] if sym in df.columns.get_level_values(0) else None
        except Exception:
            sub = None
        if sub is None or sub.empty:
            continue
        sub = sub.dropna(subset=["Close"])
        bars = []
        for dt, row in sub.iterrows():
            v = row.get("Volume", 0)
            bars.append({
                "d": dt.strftime("%Y-%m-%d"),
                "o": float(row["Open"]) if not pd.isna(row["Open"]) else float(row["Close"]),
                "h": float(row["High"]) if not pd.isna(row["High"]) else float(row["Close"]),
                "l": float(row["Low"]) if not pd.isna(row["Low"]) else float(row["Close"]),
                "c": float(row["Close"]),
                "v": int(v) if not pd.isna(v) else 0,
            })
        if len(bars) >= MIN_BARS + max(HORIZONS) + 5:
            out[sym] = bars
    print(f"  usable: {len(out) - (1 if 'SPY' in out else 0)} names "
          f"(+ SPY: {'ok' if 'SPY' in out else 'MISSING'})")
    return out


# ------------------------------------------------------- earnings history

def fetch_earnings_history_bulk(tickers, cache_path):
    """Per-ticker yfinance Ticker.earnings_dates, cached to disk. Returns
       {sym: sorted list of ISO earnings dates (past + upcoming yfinance knows)}.

    yfinance typically returns 4-8 past quarters -- older PIT dates will have
    thin coverage, which the caller must interpret."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    today = date.today().isoformat()
    cache = {"as_of": today, "tickers": {}}
    if os.path.exists(cache_path):
        try:
            cache = json.load(open(cache_path, encoding="utf-8"))
        except Exception:
            pass
    cached_age = (date.today() - date.fromisoformat(cache.get("as_of", "2000-01-01"))).days
    fresh = cached_age <= EARNINGS_CACHE_TTL_DAYS
    have = set(cache.get("tickers", {})) if fresh else set()
    todo = [t for t in tickers if t not in have]
    if not todo:
        print(f"Earnings-history cache hit for all {len(tickers)} names (age {cached_age}d)")
        return cache["tickers"]

    print(f"Fetching earnings history for {len(todo)} names (cache miss) ...")
    def _one(sym):
        try:
            ed = yf.Ticker(sym).earnings_dates
        except Exception:
            return sym, []
        if ed is None or len(ed) == 0:
            return sym, []
        dates = []
        for ts in ed.index:
            try:
                t = pd.Timestamp(ts)
                if t.tzinfo is not None:
                    t = t.tz_convert("UTC").tz_localize(None)
                dates.append(t.strftime("%Y-%m-%d"))
            except Exception:
                continue
        return sym, sorted(set(dates))

    tickers_map = cache.get("tickers", {}) if fresh else {}
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(_one, s): s for s in todo}
        done = 0
        for f in as_completed(futs):
            sym, dts = f.result()
            tickers_map[sym] = dts
            done += 1
            if done % 25 == 0 or done == len(todo):
                print(f"  [{done}/{len(todo)}] {time.time() - t0:.0f}s elapsed")
    cache = {"as_of": today, "tickers": tickers_map}
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    json.dump(cache, open(cache_path, "w", encoding="utf-8"), indent=2)
    return tickers_map


def add_near_earnings_flag(df, ehist, blackout_days=EARNINGS_BLACKOUT_DAYS):
    """Mark rows whose PIT date is within `blackout_days` calendar days
    BEFORE a known earnings print. Mirrors engine.analyze's TR06 semantics:
    reject if days_to_next_earn <= blackout_days."""
    # Pre-parse dates once per ticker.
    parsed = {s: sorted(pd.Timestamp(d) for d in dts) for s, dts in ehist.items()}
    def _near(row):
        dts = parsed.get(row["sym"], [])
        if not dts:
            return False
        pit = pd.Timestamp(row["date"])
        # Find first earnings date >= pit; check whether it's within blackout window.
        # Cheap linear scan; ~4-8 dates per name.
        for d in dts:
            delta = (d - pit).days
            if 0 <= delta <= blackout_days:
                return True
            if delta > blackout_days:
                break
        return False
    df["near_earnings"] = df.apply(_near, axis=1)
    # Coverage: fraction of (name, date) rows where we have any earnings date
    # within the past 400 days -- proxy for "did we actually have data to check".
    def _covered(row):
        dts = parsed.get(row["sym"], [])
        if not dts:
            return False
        pit = pd.Timestamp(row["date"])
        return any(-400 <= (d - pit).days <= 400 for d in dts)
    df["earnings_covered"] = df.apply(_covered, axis=1)
    return df


# ------------------------------------------------------------- walk-forward

def date_index(bars):
    """date_str -> position, for O(1) PIT lookup."""
    return {b["d"]: i for i, b in enumerate(bars)}


def walk_forward(all_bars, sector_map, universe):
    """Loop historical Fridays, call engine.analyze at each, record everything.
    Returns a DataFrame with one row per (date, ticker) and columns for every
    bucket score + forward gross returns at every horizon."""
    spy_bars = all_bars["SPY"]
    spy_closes_all = [b["c"] for b in spy_bars]
    spy_idx = date_index(spy_bars)

    # PIT candidate dates: SPY's Fridays after MIN_BARS warmup and before
    # end - max_horizon. SPY is the calendar anchor; if a name is missing a
    # particular Friday (e.g. exchange holiday it lists on isn't NYSE) we skip
    # it individually.
    first = MIN_BARS
    last = len(spy_bars) - max(HORIZONS) - 1
    fri_positions = [i for i in range(first, last + 1)
                     if pd.Timestamp(spy_bars[i]["d"]).weekday() == 4]
    print(f"Walk-forward: {len(fri_positions)} Fridays "
          f"({spy_bars[fri_positions[0]]['d']} -> {spy_bars[fri_positions[-1]]['d']})")

    name_bars = {s: all_bars[s] for s in universe if s in all_bars}
    name_idx = {s: date_index(b) for s, b in name_bars.items()}
    print(f"  {len(name_bars)} names have usable history")

    records = []
    t0 = time.time()
    for k, spi in enumerate(fri_positions):
        d = spy_bars[spi]["d"]
        pit_spy_closes = spy_closes_all[:spi + 1]
        spy_now = pit_spy_closes[-1]
        spy_h = {h: spy_closes_all[spi + h] for h in HORIZONS}
        for sym in name_bars:
            bi = name_idx[sym].get(d)
            if bi is None or bi < MIN_BARS:
                continue
            if bi + max(HORIZONS) >= len(name_bars[sym]):
                continue
            bars_pit = name_bars[sym][:bi + 1]
            try:
                r = engine.analyze(sym, bars_pit, pit_spy_closes,
                                   sector_map.get(sym), None, None)
            except Exception:
                continue
            if r.get("status") == "INSUFFICIENT_HISTORY":
                continue
            scores = r.get("scores") or {}
            if "composite" not in scores:
                continue
            gates = r.get("gates") or {}
            entry_c = r["close"]
            row = {
                "date": d, "sym": sym,
                "sector": sector_map.get(sym),
                "setup": r.get("setup"),
                "composite": scores["composite"],
                "gate_regime": bool(gates.get("RG01_above_200d")
                                    and gates.get("RG02_200d_rising")),
            }
            for b in BUCKETS:
                row[b] = scores.get(b)
            for h in HORIZONS:
                fwd_c = name_bars[sym][bi + h]["c"]
                gross = fwd_c / entry_c - 1
                spy_ret = spy_h[h] / spy_now - 1
                # Excess return net of round-trip cost.
                row[f"g{h}"] = gross
                row[f"n{h}"] = gross - COST_FRAC
                row[f"x{h}"] = (gross - COST_FRAC) - spy_ret
            records.append(row)
        if (k + 1) % 10 == 0 or k == len(fri_positions) - 1:
            elapsed = time.time() - t0
            print(f"  [{k + 1}/{len(fri_positions)}] {d} | "
                  f"{len(records)} rows | {elapsed:.0f}s elapsed")
    return pd.DataFrame(records)


# -------------------------------------------------------- stats & reports

def _apply_filters(df, sector_neutral=False, filter_earnings=False):
    """Return a working copy with a 'q' column holding quintile assignments.
    sector_neutral quintiles within (date, sector), else within date only.
    filter_earnings drops rows tagged near_earnings before quintiling."""
    d = df
    if filter_earnings and "near_earnings" in d.columns:
        d = d[~d["near_earnings"]]
    d = d.copy()
    def _q(s):
        if len(s.dropna()) < MIN_SECTOR_NAMES:
            return pd.Series([np.nan] * len(s), index=s.index)
        try:
            return pd.qcut(s.rank(method="first"), 5, labels=False, duplicates="drop")
        except ValueError:
            return pd.Series([np.nan] * len(s), index=s.index)
    if sector_neutral:
        # Rows without a sector fall back to date-only bucketing so they're
        # not silently dropped.
        has_sec = d["sector"].notna()
        d.loc[has_sec, "q"] = (d[has_sec].groupby(["date", "sector"])["composite"]
                                          .transform(_q))
        d.loc[~has_sec, "q"] = (d[~has_sec].groupby("date")["composite"]
                                            .transform(_q))
    else:
        d["q"] = d.groupby("date")["composite"].transform(_q)
    return d


def per_horizon_quintile_stats(df, sector_neutral=False, filter_earnings=False):
    """For each horizon: quintile the composite score, average forward excess
    returns per quintile across all dates. Also gate PASS/FAIL at 21d."""
    d = _apply_filters(df, sector_neutral, filter_earnings)
    out = {}
    for h in HORIZONS:
        col = f"x{h}"
        g = d.dropna(subset=[col, "q"]).groupby("q")[col]
        stats = pd.DataFrame({
            "mean_pct": (g.mean() * 100).round(2),
            "median_pct": (g.median() * 100).round(2),
            "hit_rate_pct": (g.apply(lambda s: (s > 0).mean()) * 100).round(1),
            "n": g.size(),
        })
        stats.index = [f"Q{int(i) + 1}" for i in stats.index]
        if "Q5" in stats.index and "Q1" in stats.index:
            spread = float(stats.loc["Q5", "mean_pct"] - stats.loc["Q1", "mean_pct"])
        else:
            spread = None
        out[f"h{h}"] = {"per_quintile": stats.to_dict(orient="index"),
                        "q5_minus_q1_pct": round(spread, 2) if spread is not None else None}
    # Regime gate bucket at 21d — computed on the same filter (earnings) but
    # not sector-neutral (the gate itself is name-level).
    dg = df[~df["near_earnings"]] if (filter_earnings and "near_earnings" in df.columns) else df
    col = "x21"
    gpass = dg[dg["gate_regime"]][col].dropna()
    gfail = dg[~dg["gate_regime"]][col].dropna()
    out["gate"] = {
        "pass_mean_pct": round(gpass.mean() * 100, 2) if len(gpass) else None,
        "fail_mean_pct": round(gfail.mean() * 100, 2) if len(gfail) else None,
        "pass_hit_pct": round((gpass > 0).mean() * 100, 1) if len(gpass) else None,
        "fail_hit_pct": round((gfail > 0).mean() * 100, 1) if len(gfail) else None,
        "pass_n": int(len(gpass)), "fail_n": int(len(gfail)),
    }
    return out


def decay_curve(df, sector_neutral=False, filter_earnings=False):
    """Mean Q5 excess return by day-of-hold."""
    d = _apply_filters(df, sector_neutral, filter_earnings)
    q5 = d[d["q"] == 4]
    out = {}
    for h in HORIZONS:
        s = q5[f"x{h}"].dropna()
        out[f"day{h}"] = {"mean_pct": round(s.mean() * 100, 2) if len(s) else None,
                          "hit_pct": round((s > 0).mean() * 100, 1) if len(s) else None,
                          "n": int(len(s))}
    return out


# ------------------------------------- Deflated Sharpe + PBO on alt weights

def q5_q1_series(df, horizon=21, sector_neutral=False, filter_earnings=False):
    """Non-overlapping monthly Q5-Q1 return series (take every 4th Friday) so
    the Sharpe stat isn't inflated by 21-day-overlapping windows."""
    d = _apply_filters(df, sector_neutral, filter_earnings)
    per_date = (d.dropna(subset=[f"x{horizon}", "q"])
                 .groupby(["date", "q"])[f"x{horizon}"].mean()
                 .unstack("q"))
    if 0 not in per_date.columns or 4 not in per_date.columns:
        return pd.Series(dtype=float)
    per_date = per_date.dropna(subset=[0, 4])
    strat = per_date[4] - per_date[0]
    return strat.iloc[::4]


def alt_composites_returns_matrix(df, horizon=21, n_configs=PBO_N_CONFIGS,
                                  seed=PBO_SEED, sector_neutral=False,
                                  filter_earnings=False):
    """Reconstruct alt-weight strategy returns from stored per-bucket scores
    without re-running the walk-forward. Same filter flags as q5_q1_series."""
    rng = np.random.default_rng(seed)
    configs = [("shipped", SHIPPED_WEIGHTS)]
    for i in range(n_configs - 1):
        w = SHIPPED_WEIGHTS + rng.uniform(-0.08, 0.08, size=6)
        w = np.clip(w, 0.01, None)
        w = w / w.sum()
        configs.append((f"alt{i + 1:02d}", w))

    d = df
    if filter_earnings and "near_earnings" in d.columns:
        d = d[~d["near_earnings"]]

    B = d[list(BUCKETS)].to_numpy(dtype=float)
    x = d[f"x{horizon}"].to_numpy(dtype=float)
    dates = d["date"].to_numpy()
    sectors = d["sector"].to_numpy() if sector_neutral else None

    def _qcut(s):
        if len(s.dropna()) < MIN_SECTOR_NAMES:
            return pd.Series([np.nan] * len(s), index=s.index)
        try:
            return pd.qcut(s.rank(method="first"), 5, labels=False, duplicates="drop")
        except ValueError:
            return pd.Series([np.nan] * len(s), index=s.index)

    rets_per_config = {}
    for name, w in configs:
        comp = B @ w
        tmp = pd.DataFrame({"date": dates, "comp": comp, "x": x})
        if sector_neutral:
            tmp["sector"] = sectors
            has = tmp["sector"].notna()
            tmp.loc[has, "q"] = (tmp[has].groupby(["date", "sector"])["comp"]
                                          .transform(_qcut))
            tmp.loc[~has, "q"] = (tmp[~has].groupby("date")["comp"]
                                            .transform(_qcut))
        else:
            tmp["q"] = tmp.groupby("date")["comp"].transform(_qcut)
        tmp = tmp.dropna(subset=["q", "x"])
        per_date = tmp.groupby(["date", "q"])["x"].mean().unstack("q")
        if 0 not in per_date.columns or 4 not in per_date.columns:
            rets_per_config[name] = pd.Series(dtype=float)
            continue
        per_date = per_date.dropna(subset=[0, 4])
        strat = per_date[4] - per_date[0]
        rets_per_config[name] = strat.iloc[::4]
    return pd.DataFrame(rets_per_config), configs


def sharpe(x):
    x = np.asarray(x, dtype=float)
    x = x[~np.isnan(x)]
    if len(x) < 3 or x.std(ddof=1) == 0:
        return float("nan")
    return float(x.mean() / x.std(ddof=1))


def _phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _phi_inv(p):
    """Beasley-Springer-Moro approximation to the standard-normal quantile.
    Good to ~1e-7 across (0,1)."""
    if p <= 0.0 or p >= 1.0:
        return float("nan")
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / \
               ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1)
    if p <= phigh:
        q = p - 0.5
        r = q * q
        return (((((a[0]*r + a[1])*r + a[2])*r + a[3])*r + a[4])*r + a[5]) * q / \
               (((((b[0]*r + b[1])*r + b[2])*r + b[3])*r + b[4])*r + 1)
    q = math.sqrt(-2 * math.log(1 - p))
    return -(((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / \
            ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1)


def deflated_sharpe(strategy_returns, alt_sharpes, assumed_trials=DSR_TRIALS):
    """Bailey / López de Prado 2014 Deflated Sharpe Ratio.
    Returns Prob[true SR > 0] after adjusting for skew, kurtosis, sample size,
    and multiple-testing selection bias. > 0.95 is meaningful.

    strategy_returns : 1-D array of the shipped strategy's non-overlapping returns
    alt_sharpes      : SR of each alt strategy variant (used for Var[SR] across trials)
    assumed_trials   : N used in the expected-max-SR term. More trials -> harder bar.
    """
    x = np.asarray(strategy_returns, dtype=float)
    x = x[~np.isnan(x)]
    T = len(x)
    if T < 8:
        return {"dsr": None, "reason": f"insufficient sample T={T}"}
    sr = sharpe(x)
    if not np.isfinite(sr):
        return {"dsr": None, "reason": "zero variance"}
    mu, sd = x.mean(), x.std(ddof=1)
    g1 = float(((x - mu) ** 3).mean() / sd ** 3)
    g2 = float(((x - mu) ** 4).mean() / sd ** 4)   # raw kurtosis (Normal = 3)

    var_srs = float(np.nanvar(alt_sharpes, ddof=1)) if len(alt_sharpes) > 1 else 0.0
    N = max(int(assumed_trials), len(alt_sharpes))
    eul = 0.5772156649015329
    emax = math.sqrt(var_srs) * (
        (1 - eul) * _phi_inv(1 - 1 / N) + eul * _phi_inv(1 - 1 / (N * math.e)))
    denom_sq = 1 - g1 * sr + (g2 - 1) / 4 * sr ** 2
    if denom_sq <= 0:
        return {"dsr": None, "reason": f"denom<=0 (skew/kurt pathology): {denom_sq:.3g}"}
    z = (sr - emax) * math.sqrt(T - 1) / math.sqrt(denom_sq)
    return {
        "dsr": round(_phi(z), 4),
        "sharpe_obs": round(sr, 3),
        "expected_max_sr_null": round(emax, 3),
        "skew": round(g1, 2), "kurt": round(g2, 2), "T": T, "N_trials": N,
        "note": "DSR > 0.95 = strong evidence the strategy isn't a lucky pick from N trials",
    }


def pbo(returns_matrix, s=PBO_S):
    """Probability of Backtest Overfitting via Combinatorially Symmetric CV.
    returns_matrix: DataFrame [T periods x M configs]
    Returns fraction of splits where the in-sample winner ranks below median OOS.
    < 0.5 = model has some real edge; >= 0.5 = selection is noise-driven."""
    R = returns_matrix.dropna().to_numpy()
    T, M = R.shape
    if T < s or s % 2 != 0:
        return {"pbo": None, "reason": f"need T>={s} and even s; got T={T}"}
    # Trim T so it's cleanly divisible by s.
    T_trim = (T // s) * s
    R = R[:T_trim]
    subs = np.array_split(R, s, axis=0)
    logits = []
    for combo in itertools.combinations(range(s), s // 2):
        train_idx = list(combo)
        test_idx = [i for i in range(s) if i not in combo]
        train = np.concatenate([subs[i] for i in train_idx], axis=0)
        test = np.concatenate([subs[i] for i in test_idx], axis=0)
        tr_sr = np.array([sharpe(train[:, m]) for m in range(M)])
        te_sr = np.array([sharpe(test[:, m]) for m in range(M)])
        if np.all(np.isnan(tr_sr)) or np.all(np.isnan(te_sr)):
            continue
        winner = int(np.nanargmax(tr_sr))
        # Rank of winner in OOS (1 = worst, M = best).
        ranks = pd.Series(te_sr).rank(method="average").to_numpy()
        w_rank = ranks[winner]
        omega = w_rank / (M + 1)
        # Guard against log(0).
        omega = min(max(omega, 1 / (M + 1)), M / (M + 1))
        logits.append(math.log(omega / (1 - omega)))
    logits = np.array(logits)
    return {
        "pbo": round(float((logits < 0).mean()), 4),
        "median_logit": round(float(np.median(logits)), 3),
        "n_splits": int(len(logits)),
        "M_configs": int(M),
        "note": "PBO < 0.5 = signal likely real; PBO ~ 0.5+ = weight tuning was noise-fitting",
    }


# ------------------------------------------------------------------- main

VARIANTS = (
    # (label, sector_neutral, filter_earnings)
    ("base",              False, False),
    ("sector_neutral",    True,  False),
    ("earnings_filtered", False, True),
    ("both",              True,  True),
)


def run_variant(df, label, sector_neutral, filter_earnings):
    """Compute Q5-Q1 series, alt-weights matrix, DSR, PBO for one variant.
    Returns a compact result dict."""
    ret_matrix, _ = alt_composites_returns_matrix(
        df, horizon=21, sector_neutral=sector_neutral,
        filter_earnings=filter_earnings)
    if ret_matrix.empty or "shipped" not in ret_matrix.columns:
        return {"label": label, "error": "no returns produced (thin sample)"}
    shipped = ret_matrix["shipped"].dropna()
    alt_srs = [sharpe(ret_matrix[c]) for c in ret_matrix.columns]
    dsr = deflated_sharpe(shipped.to_numpy(), alt_srs)
    pbo_res = pbo(ret_matrix)
    q_stats = per_horizon_quintile_stats(df, sector_neutral, filter_earnings)
    n_rows = len(df) if not filter_earnings else int((~df["near_earnings"]).sum())
    return {
        "label": label,
        "sector_neutral": sector_neutral,
        "filter_earnings": filter_earnings,
        "n_rows_used": n_rows,
        "q5_minus_q1_21d_pct": q_stats["h21"]["q5_minus_q1_pct"],
        "q5_hit_21d_pct": q_stats["h21"]["per_quintile"].get("Q5", {}).get("hit_rate_pct"),
        "sharpe_shipped": dsr.get("sharpe_obs"),
        "dsr": dsr.get("dsr"),
        "pbo": pbo_res.get("pbo"),
        "monthly_T": dsr.get("T"),
    }


def main():
    fast = "--fast" in sys.argv
    tickers, sector_map = load_universe()
    if fast:
        tickers = tickers[:40]
        years = 1
        print(f"[--fast] 40 names, {years}y")
    else:
        years = LOOKBACK_YEARS

    all_bars = bulk_bars(tickers, years + 1)     # +1y warmup buffer
    if "SPY" not in all_bars:
        print("FATAL: no SPY bars"); sys.exit(1)
    usable = [t for t in tickers if t in all_bars]

    ehist = fetch_earnings_history_bulk(
        usable, os.path.join(DATA, "earnings_history_cache.json"))

    print(f"Running walk-forward on {len(usable)} names ...")
    df = walk_forward(all_bars, sector_map, usable)
    print(f"\nRecords: {len(df)}")
    if df.empty:
        print("No records produced -- check that bulk_bars returned data."); sys.exit(1)

    print("Tagging rows near known earnings prints ...")
    df = add_near_earnings_flag(df, ehist)
    coverage = df["earnings_covered"].mean() * 100
    near = df["near_earnings"].mean() * 100
    print(f"  earnings coverage: {coverage:.1f}% of rows have any print within +/-400d")
    print(f"  near-earnings flag: {near:.1f}% of rows dropped by TR06 filter")

    # -------- base variant: full detail (as v1)
    print("\nComputing base variant: per-horizon stats, decay, DSR, PBO ...")
    q_stats_base = per_horizon_quintile_stats(df, False, False)
    decay_base = decay_curve(df, False, False)
    ret_matrix, configs = alt_composites_returns_matrix(df, horizon=21)
    alt_srs = [sharpe(ret_matrix[c]) for c in ret_matrix.columns]
    dsr_base = deflated_sharpe(ret_matrix["shipped"].to_numpy(), alt_srs)
    pbo_base = pbo(ret_matrix)

    # -------- four-variant comparison
    print("Computing 4-variant comparison ...")
    variant_results = [run_variant(df, *v) for v in VARIANTS]

    # ================================================================ PRINT
    print("\n" + "=" * 74)
    print(" BASE VARIANT (composite-quintile within date, no TR06 filter)")
    print("=" * 74)
    for h in HORIZONS:
        s = q_stats_base[f"h{h}"]
        print(f"\nHorizon {h}d -- Q5-Q1 spread: {s['q5_minus_q1_pct']:+.2f}% "
              f"(after {COST_BPS_ROUNDTRIP}bps cost, excess vs SPY)")
        for qname, row in s["per_quintile"].items():
            print(f"  {qname}: mean {row['mean_pct']:+6.2f}%  "
                  f"med {row['median_pct']:+6.2f}%  hit {row['hit_rate_pct']:5.1f}%  n={row['n']}")
    print("\nRegime gate at 21d:")
    g = q_stats_base["gate"]
    print(f"  PASS: {g['pass_mean_pct']:+.2f}%  hit {g['pass_hit_pct']:.1f}%  n={g['pass_n']}")
    print(f"  FAIL: {g['fail_mean_pct']:+.2f}%  hit {g['fail_hit_pct']:.1f}%  n={g['fail_n']}")
    print("\nQ5 decay curve (mean excess by day-of-hold):")
    for h in HORIZONS:
        r = decay_base[f"day{h}"]
        print(f"  day {h:>2}: {r['mean_pct']:+6.2f}%   hit {r['hit_pct']:5.1f}%   n={r['n']}")

    print("\n" + "=" * 74)
    print(" FOUR-VARIANT COMPARISON (21d horizon)")
    print("=" * 74)
    hdr = f"  {'variant':<20} {'n_rows':>8} {'Q5-Q1':>8} {'Q5 hit':>8} {'Sharpe':>8} {'DSR':>7} {'PBO':>7}"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    def _f(v, w, digits=3, pct=False):
        if v is None:
            return f"{'n/a':>{w}}"
        s = f"{v:>+{w-1}.{digits}f}%" if pct else f"{v:>{w}.{digits}f}"
        return s
    for r in variant_results:
        if "error" in r:
            print(f"  {r['label']:<20} ERROR: {r['error']}")
            continue
        print(f"  {r['label']:<20} {r['n_rows_used']:>8}  "
              f"{_f(r['q5_minus_q1_21d_pct'], 7, 2, pct=True)}  "
              f"{_f(r['q5_hit_21d_pct'], 6, 1)}%  "
              f"{_f(r['sharpe_shipped'], 8, 3)}  "
              f"{_f(r['dsr'], 7, 3)}  "
              f"{_f(r['pbo'], 7, 3)}")

    # -------- save
    os.makedirs(DATA, exist_ok=True)
    out = {
        "run_date": date.today().isoformat(),
        "years": years,
        "universe_size": len(usable),
        "records": len(df),
        "horizons": list(HORIZONS),
        "cost_bps_roundtrip": COST_BPS_ROUNDTRIP,
        "earnings_coverage_pct": round(coverage, 1),
        "near_earnings_pct": round(near, 1),
        "base": {
            "per_horizon": q_stats_base,
            "decay_curve": decay_base,
            "deflated_sharpe": dsr_base,
            "pbo": pbo_base,
        },
        "variants_21d": variant_results,
        "alt_weight_configs": [
            {"name": n, "weights": {b: round(float(v), 3) for b, v in zip(BUCKETS, w)}}
            for n, w in configs
        ],
        "caveats": [
            "Universe = today's screener list -> survivorship-biased.",
            "yfinance earnings_dates typically covers only ~4-8 past quarters, "
            "so the TR06-filtered variant is a lower bound on the earnings "
            "blackout effect (misses older prints).",
            "Sector map = today's mapping.",
            "Costs modeled as 5bps round-trip; slippage on mega-caps only.",
        ],
    }
    outp = os.path.join(DATA, "backtest_wf.json")
    json.dump(out, open(outp, "w", encoding="utf-8"), indent=2, default=str)
    print(f"\nWrote {outp}")

    print("\nCAVEATS:")
    for c in out["caveats"]:
        print(f"  - {c}")


if __name__ == "__main__":
    main()
