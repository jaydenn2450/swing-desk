"""
swing-desk engine v0.2 — daily long-only swing scanner.

New in v0.2:
  - Broad-market universe via yfinance screener (mcap/volume filters), with
    sector tagging; universe.txt becomes manual ADDS on top of the auto feed
  - Sector rotation module (RRG math ported from rotation_tool.py) — daily +
    weekly quadrants for sector/industry ETFs, confluence per ticker
  - TR06 earnings blackout gate (Nasdaq calendar, next 3 weeks)
  - Measured-move targets + pivot-based support/resistance shelves (fixes the
    flat 1.14R fallback)
  - Fibonacci retracements, ATR bands
  - Options levels via yfinance chains: call/put walls, naive GEX profile,
    gamma-flip estimate, P/C ratios (flow-switch flag)
  - 12-point checklist score + status tag (Momentum building / Watch / ...)
  - Signal events with recency (golden cross, 8/21 & 13/50 EMA crosses,
    RSI extremes, 52w highs/lows)
  - Rule-based thesis comments + trade plan per name
  - `lookup TICKER` command for ad-hoc names; search handled in dashboard

Usage:
  python engine.py scan            # full daily scan -> data/report.json + dashboard.html
  python engine.py html            # re-render dashboard only
  python engine.py lookup NVDA ... # add ad-hoc tickers to today's report
  python engine.py export-holdings # watchlist -> holdings.csv for portfolio_stress_test.py

Not investment advice; internal research tooling.
"""

import json, math, os, sys, time, datetime, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) swing-desk/0.2",
      "Accept": "application/json"}

# ------------------------------------------------------------------- config
ACCOUNT_SIZE = 100_000
RISK_BUDGET_PCT = 0.75
MIN_DOLLAR_VOL = 20_000_000     # TR01
MIN_PRICE = 5.0                 # TR03
MIN_BARS = 220                  # TR11
EARNINGS_BLACKOUT_DAYS = 3      # TR06: no entry within N calendar days of print
EARNINGS_LOOKAHEAD_DAYS = 21    # how far the calendar is scanned (defines "none upcoming")
EARNINGS_ALERT_DAYS = 14        # alert when a report lands within N days
EARNINGS_LOOKBACK_DAYS = 7      # alert (with actuals) when a report landed within N days
MIN_REWARD_RISK = 2.0           # RK04 spec floor; below this a ticket is flagged
MAX_POSITION_PCT = 25.0         # cap any single position at this % of account
AUTO_UNIVERSE = True            # pull broad universe from screener
AUTO_UNIVERSE_SIZE = 150        # top N by avg dollar volume
AUTO_MIN_MCAP = 10_000_000_000
OPTIONS_DEPTH = 14              # how many names get options-level analysis

SECTOR_ETF = {
    "Technology": "XLK", "Financial Services": "XLF", "Healthcare": "XLV",
    "Energy": "XLE", "Industrials": "XLI", "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP", "Utilities": "XLU", "Basic Materials": "XLB",
    "Real Estate": "XLRE", "Communication Services": "XLC",
}
ROTATION_ETFS = {
    "XLK": "Technology", "XLF": "Financials", "XLV": "Health Care",
    "XLE": "Energy", "XLI": "Industrials", "XLY": "Discretionary",
    "XLP": "Staples", "XLU": "Utilities", "XLB": "Materials",
    "XLRE": "Real Estate", "XLC": "Comm Services",
    "SMH": "Semis", "IGV": "Software", "CIBR": "Cyber", "XBI": "Biotech",
    "KRE": "Reg Banks", "XOP": "Oil & Gas", "GDX": "Gold Miners",
    "XME": "Metals", "IYT": "Transports", "XRT": "Retail", "URA": "Uranium",
}
SHORT_ETFS = {"SH": "S&P -1x", "SDS": "S&P -2x", "SPXU": "S&P -3x", "PSQ": "NQ -1x",
              "SQQQ": "NQ -3x", "SOXS": "Semis -3x", "UVXY": "VIX 1.5x", "VXX": "VIX ETN"}
MARKET_EXTRA = ["^VIX9D", "^VIX3M", "CL=F", "USO", "OIH"]
CHART_BARS = 504          # daily bars embedded per name for the chart tab (2y, enables weekly view)

# FRED credit-spread pull (spec MK07). Series BAMLH0A0HYM2 = ICE BofA US High
# Yield OAS -- the best non-price veto per the spec. Cache daily, treat as the
# stress indicator that outranks VIX (VIX is options positioning; HY OAS is
# credit demanding compensation for actual default risk).
FRED_KEY_FILE = "data/fred_api_key.txt"
HY_OAS_SERIES = "BAMLH0A0HYM2"
HY_OAS_CACHE = "data/hy_oas_cache.json"
SECTOR_MAX_POSITIONS = 2  # after ranking, demote the 3rd+ name per sector

# Phase C regime multiplier: bucket the current regime, look up historical
# top-quintile edge from data/regime_lookup.json (built offline by
# phaseC_regime_builder.py). Keep bucket defs in sync with that builder.
REGIME_LOOKUP_FILE = "data/regime_lookup.json"
VIX_BUCKETS = [("LOW", 0, 20), ("MID", 20, 27), ("HI", 27, 999)]
HY_PCTILE_BUCKETS = [("CALM", 0, 30), ("NORMAL", 30, 70), ("WIDE", 70, 101)]

# ---------------------------------------------------------------- data layer

def _get(url, timeout=25, tries=2):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode()
        except Exception:
            if i == tries - 1: raise
            time.sleep(1.0)

def fetch_history(sym, rng="2y"):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
           f"?range={rng}&interval=1d&events=div%2Csplit")
    j = json.loads(_get(url))
    res = j["chart"]["result"][0]
    ts = res["timestamp"]
    q = res["indicators"]["quote"][0]
    adj = res["indicators"].get("adjclose", [{}])[0].get("adjclose") or q["close"]
    bars = []
    for i in range(len(ts)):
        c = q["close"][i]
        if c is None or q["high"][i] is None:
            continue
        k = (adj[i] / c) if (adj[i] and c) else 1.0
        bars.append({
            "d": datetime.datetime.fromtimestamp(ts[i], datetime.UTC).strftime("%Y-%m-%d"),
            "o": q["open"][i] * k, "h": q["high"][i] * k,
            "l": q["low"][i] * k, "c": adj[i] if adj[i] else c,
            "v": q["volume"][i] or 0,
        })
    return bars

def _read_fred_key():
    p = os.path.join(ROOT, FRED_KEY_FILE)
    if not os.path.exists(p):
        return None
    k = open(p, encoding="utf-8").read().strip()
    return k or None


def fetch_hy_oas():
    """FRED BAMLH0A0HYM2 (US HY OAS, daily bps). Cached daily to disk.
    Returns dict with current level, 60d high, 20d change, 1y percentile,
    spark, and a 'read' string, or None if the key is missing / fetch fails.
    HY OAS is the spec's MK07 risk gate: credit-market compensation demanded
    for default risk, which historically leads equity drawdowns."""
    cache_p = os.path.join(ROOT, HY_OAS_CACHE)
    today = datetime.date.today().isoformat()
    if os.path.exists(cache_p):
        try:
            c = json.load(open(cache_p, encoding="utf-8"))
            if c.get("as_of") == today:
                return c
        except Exception:
            pass
    key = _read_fred_key()
    if not key:
        return None
    url = (f"https://api.stlouisfed.org/fred/series/observations?series_id={HY_OAS_SERIES}"
           f"&api_key={key}&file_type=json&observation_start=2023-01-01&sort_order=asc")
    try:
        j = json.loads(_get(url))
    except Exception as e:
        print(f"! FRED fetch failed: {str(e)[:120]}")
        return None
    obs = j.get("observations", [])
    if not obs:
        return None
    series = [(o["date"], float(o["value"])) for o in obs if o["value"] not in (".", "", None)]
    if len(series) < 60:
        return None
    dates = [d for d, _ in series]
    vals = [v for _, v in series]
    lvl = vals[-1]
    prev = vals[-21] if len(vals) >= 21 else vals[0]
    hi60 = max(vals[-60:])
    yr = vals[-252:] if len(vals) >= 252 else vals
    pctile = round(100 * sum(1 for x in yr if x <= lvl) / len(yr))
    hi_1y = max(yr)
    delta_20d_bps = round((lvl - prev) * 100)
    # Simple regime read from level + trend + percentile.
    if lvl >= hi_1y * 0.995:
        read = "STRESS — HY OAS at a 1y high; credit markets are demanding real risk compensation."
    elif lvl > hi60:
        read = "WIDENING — HY OAS broke its 60d high; equity longs should tighten stops."
    elif delta_20d_bps >= 40:
        read = "WIDENING — HY OAS +{}bps in 20d; risk-off building even if levels aren't extreme.".format(delta_20d_bps)
    elif pctile <= 15:
        read = "COMPLACENT — HY OAS in the bottom 15% of 1y range; credit is calm, longs OK."
    else:
        read = f"NORMAL — HY OAS at the {pctile}th %ile of 1y; no credit-market veto."
    out = {
        "as_of": today, "series": HY_OAS_SERIES, "last_obs": dates[-1],
        "level_bps": round(lvl * 100), "level": round(lvl, 2),
        "hi_60d_bps": round(hi60 * 100), "hi_1y_bps": round(hi_1y * 100),
        "delta_20d_bps": delta_20d_bps, "pctile_1y": pctile,
        "spark": [round(x, 2) for x in vals[-90:]],
        "above_60d_hi": lvl > hi60,
        "at_1y_hi": lvl >= hi_1y * 0.995,
        "read": read,
    }
    os.makedirs(os.path.dirname(cache_p), exist_ok=True)
    json.dump(out, open(cache_p, "w", encoding="utf-8"), indent=2)
    return out


def apply_risk_off_to_result(r, risk_off):
    """Halve (HALF_SIZE) or zero (NO_ADDS) the sizing on a single analyze()
    result. Idempotent: skips if size_mult already applied. Must be called
    from BOTH cmd_scan (batch scan) and serve.lookup (search) so the desk's
    sizing rules apply consistently regardless of how the trader lands on a
    card. Bug fix for pre-prod review CRIT-C1."""
    if not risk_off or risk_off.get("size_mult", 1.0) >= 1.0:
        return
    rk = r.get("risk")
    if not rk:
        return
    if rk.get("size_mult") is not None:      # already applied (e.g., html rebuild)
        return
    mult = risk_off["size_mult"]
    rk["size_mult"] = mult
    rk["size_orig_pct"] = rk.get("pct_of_account")
    rk["size_orig_shares"] = rk.get("shares_at_risk_budget")
    if rk.get("shares_at_risk_budget") is not None:
        rk["shares_at_risk_budget"] = int((rk["shares_at_risk_budget"] or 0) * mult)
    if rk.get("notional") is not None:
        rk["notional"] = round((rk["notional"] or 0) * mult)
    if rk.get("pct_of_account") is not None:
        rk["pct_of_account"] = round((rk["pct_of_account"] or 0) * mult, 1)
    rk["risk_off_tier"] = risk_off.get("tier")


def compute_risk_off(vix_last, hy_oas):
    """Combine VIX + HY OAS into a portfolio-level risk-off tier.
    Rules deliberately conservative — the goal is to catch a regime break
    early, not to smooth every wobble:
      NO_ADDS   : HY OAS at 1y high AND VIX > 30 (or either at true extreme)
      HALF_SIZE : HY OAS above 60d high, or VIX 25-30, or HY OAS 20d +>=40bps
      NORMAL    : otherwise
    Returns dict with tier, size_mult, banner text, and the specific triggers."""
    triggers = []
    if hy_oas:
        if hy_oas["at_1y_hi"]:
            triggers.append(("HY_OAS_1Y_HIGH", f"HY OAS at 1y high ({hy_oas['level_bps']}bps)"))
        elif hy_oas["above_60d_hi"]:
            triggers.append(("HY_OAS_60D_HIGH", f"HY OAS above 60d high ({hy_oas['level_bps']}bps > {hy_oas['hi_60d_bps']})"))
        elif hy_oas["delta_20d_bps"] >= 40:
            triggers.append(("HY_OAS_WIDENING", f"HY OAS +{hy_oas['delta_20d_bps']}bps in 20d"))
    if vix_last is not None:
        if vix_last > 30:
            triggers.append(("VIX_PANIC", f"VIX {round(vix_last,1)} > 30"))
        elif vix_last > 25:
            triggers.append(("VIX_STRESS", f"VIX {round(vix_last,1)} > 25"))
    tags = {t for t, _ in triggers}
    if ("HY_OAS_1Y_HIGH" in tags and "VIX_PANIC" in tags) or \
       ("HY_OAS_1Y_HIGH" in tags and "VIX_STRESS" in tags):
        tier, mult = "NO_ADDS", 0.0
    elif ("HY_OAS_60D_HIGH" in tags or "HY_OAS_1Y_HIGH" in tags or
          "HY_OAS_WIDENING" in tags or "VIX_STRESS" in tags or "VIX_PANIC" in tags):
        tier, mult = "HALF_SIZE", 0.5
    else:
        tier, mult = "NORMAL", 1.0
    if tier == "NO_ADDS":
        banner = "RISK-OFF · NO NEW ADDS — " + " · ".join(t for _, t in triggers)
    elif tier == "HALF_SIZE":
        banner = "RISK-OFF · HALF SIZE — " + " · ".join(t for _, t in triggers)
    else:
        banner = None
    return {"tier": tier, "size_mult": mult, "triggers": [t for _, t in triggers],
            "banner": banner}


def _bucket_name(value, buckets, default):
    if value is None:
        return default
    for name, lo, hi in buckets:
        if lo <= value < hi:
            return name
    return buckets[-1][0]


def load_regime_lookup():
    p = os.path.join(ROOT, REGIME_LOOKUP_FILE)
    if not os.path.exists(p):
        return None
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return None


def current_regime_edge(vix_last, hy_oas, spy_above_200d, spy_200d_rising):
    """Look up the historical top-quintile edge for the current regime cell.
    Falls back to marginal (single-dim) averages when the 3-D cell is thin.
    Returns dict with mean_x21_pct, hit_pct, n, edge_mult, source, bucket_label
    -- or None if the lookup file doesn't exist yet."""
    lut = load_regime_lookup()
    if not lut:
        return None
    vb = _bucket_name(vix_last, VIX_BUCKETS, "MID")
    hb = _bucket_name(hy_oas["pctile_1y"] if hy_oas else None, HY_PCTILE_BUCKETS, "NORMAL")
    sb = "UP" if (spy_above_200d and spy_200d_rising) else "DOWN"
    key = f"{vb}|{hb}|{sb}"
    baseline = lut.get("baseline", {})
    baseline_mean = baseline.get("mean_x21_pct", 0)
    min_n = lut.get("min_bucket_n", 20)
    cell = (lut.get("buckets") or {}).get(key)
    source = "3d_bucket"
    if not cell or cell.get("n", 0) < min_n:
        # Marginal fallback: blend the three single-dim edges by weighted mean.
        marginals = lut.get("marginals") or {}
        parts = []
        for dim, name in (("vix", vb), ("hy", hb), ("spx", sb)):
            m = marginals.get(f"{dim}:{name}")
            if m:
                parts.append(m)
        if not parts:
            return None
        mean_x = sum(p["mean_x21_pct"] * p["n"] for p in parts) / sum(p["n"] for p in parts)
        hit = sum(p["hit_pct"] * p["n"] for p in parts) / sum(p["n"] for p in parts)
        cell = {"mean_x21_pct": round(mean_x, 3), "hit_pct": round(hit, 1),
                "n": min(p["n"] for p in parts),
                "edge_mult": round(mean_x / baseline_mean, 2) if baseline_mean else None}
        source = "marginal_blend"
    return {
        "bucket_label": f"{vb} VIX · {hb} HY · {sb} SPX",
        "vix_bucket": vb, "hy_bucket": hb, "spx_bucket": sb,
        "mean_x21_pct": cell["mean_x21_pct"],
        "hit_pct": cell["hit_pct"],
        "n": cell["n"],
        "edge_mult": cell.get("edge_mult"),
        "baseline_mean_x21_pct": baseline_mean,
        "baseline_hit_pct": baseline.get("hit_pct"),
        "source": source,
        "read": _regime_read(cell.get("edge_mult"), cell.get("mean_x21_pct")),
    }


def _regime_read(mult, mean_x):
    if mult is None or mean_x is None:
        return "regime lookup unavailable"
    if mean_x < 0:
        return "REGIME UNFAVORABLE — top-quintile historically LOSES here; stand aside or trade tiny."
    if mult >= 2.0:
        return f"REGIME FAVORABLE — top-quintile historically {mult}× baseline. Model works best here."
    if mult >= 1.3:
        return f"REGIME SUPPORTIVE — top-quintile {mult}× baseline. Reasonable to press."
    if mult >= 0.7:
        return f"REGIME NEUTRAL — top-quintile {mult}× baseline. Normal size."
    return f"REGIME WEAK — top-quintile only {mult}× baseline. Size down."


def build_universe():
    """Broad-market feed: yfinance screener, mcap>AUTO_MIN_MCAP, tagged by sector.
    Cached per day. Returns (tickers, sector_map)."""
    cache = os.path.join(DATA, "universe_auto.json")
    today = datetime.date.today().isoformat()
    if os.path.exists(cache):
        c = json.load(open(cache, encoding="utf-8"))
        if c.get("date") == today:
            return c["tickers"], c["sectors"]
    import yfinance as yf
    from yfinance import EquityQuery
    sector_map, rows_all = {}, {}
    print(f"Building universe from screener ({len(SECTOR_ETF)} sector queries)...")
    for sector in SECTOR_ETF:
        try:
            q = EquityQuery("and", [
                EquityQuery("is-in", ["exchange", "NMS", "NYQ"]),
                EquityQuery("eq", ["sector", sector]),
                EquityQuery("gt", ["intradaymarketcap", AUTO_MIN_MCAP]),
                EquityQuery("gt", ["avgdailyvol3m", 500_000]),
            ])
            r = yf.screen(q, sortField="avgdailyvol3m", sortAsc=False, size=100)
            for x in r.get("quotes", []):
                s = x.get("symbol", "")
                px = x.get("regularMarketPrice") or 0
                adv = (x.get("averageDailyVolume3Month") or 0) * px
                if not s or "." in s or px < MIN_PRICE:
                    continue
                sector_map[s] = sector
                rows_all[s] = adv
        except Exception as e:
            print(f"  ! screener {sector}: {e}")
    tickers = sorted(rows_all, key=rows_all.get, reverse=True)[:AUTO_UNIVERSE_SIZE]
    json.dump({"date": today, "tickers": tickers, "sectors": sector_map},
              open(cache, "w", encoding="utf-8"))
    print(f"  universe: {len(tickers)} names (of {len(rows_all)} screened)")
    return tickers, sector_map

def _num_or_none(s):
    """Parse Nasdaq money strings. Losses use accounting parentheses: ($0.39) = -0.39."""
    if s is None:
        return None
    t = str(s).strip().replace("$", "").replace(",", "").replace("%", "")
    neg = t.startswith("(") and t.endswith(")")
    if neg:
        t = t[1:-1]
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v

def fetch_earnings_map(days_ahead=21, days_back=EARNINGS_LOOKBACK_DAYS):
    """Earnings calendar both directions. Returns (upcoming, week, recent).
      upcoming : {ticker: iso-date} for the next N days   -> TR06 gate + alerts
      week     : {date: [rows]} for the dashboard's week view
      recent   : {ticker: {...actuals...}} for the last N days -> post-report alert
    Nasdaq publishes actual EPS / forecast / surprise% once a name has reported.
    Cached per day."""
    cache = os.path.join(DATA, "earnings_cache.json")
    today = datetime.date.today()
    tstr = today.isoformat()
    if os.path.exists(cache):
        try:
            c = json.load(open(cache, encoding="utf-8"))
            if c.get("date") == tstr and "recent" in c:   # "recent" = new cache format
                return c["map"], c["week"], c["recent"]
        except Exception:
            pass
    emap, week, recent = {}, {}, {}

    def _fetch(day):
        return json.loads(_get(
            f"https://api.nasdaq.com/api/calendar/earnings?date={day.isoformat()}", timeout=15))

    # ---- forward: scheduled reports
    for i in range(days_ahead + 1):
        day = today + datetime.timedelta(days=i)
        if day.weekday() >= 5:
            continue
        try:
            rows = (_fetch(day).get("data") or {}).get("rows") or []
            keep = []
            for r in rows:
                sym = r.get("symbol")
                if not sym: continue
                emap.setdefault(sym, day.isoformat())
                capv = _num_or_none(r.get("marketCap")) or 0
                if capv >= 5_000_000_000 and i <= 7:
                    keep.append({"ticker": sym, "name": r.get("name"), "time": r.get("time"),
                                 "epsForecast": r.get("epsForecast"),
                                 "marketCap": r.get("marketCap"), "cap": capv})
            if keep and i <= 7:
                keep.sort(key=lambda x: -x["cap"])
                week[day.isoformat()] = [{k: v for k, v in x.items() if k != "cap"} for x in keep[:15]]
        except Exception:
            pass

    # ---- backward: reports already out, with the actual numbers
    for i in range(1, days_back + 1):
        day = today - datetime.timedelta(days=i)
        if day.weekday() >= 5:
            continue
        try:
            rows = (_fetch(day).get("data") or {}).get("rows") or []
            for r in rows:
                sym = r.get("symbol")
                if not sym or sym in recent:
                    continue
                eps, est = _num_or_none(r.get("eps")), _num_or_none(r.get("epsForecast"))
                surprise = _num_or_none(r.get("surprise"))
                if surprise is None and eps is not None and est not in (None, 0):
                    surprise = round(100 * (eps - est) / abs(est), 1)
                recent[sym] = {
                    "date": day.isoformat(), "days_ago": i,
                    "eps": eps, "eps_forecast": est, "surprise_pct": surprise,
                    "beat": (None if eps is None or est is None else eps >= est),
                    "quarter": r.get("fiscalQuarterEnding"),
                }
        except Exception:
            pass

    json.dump({"date": tstr, "map": emap, "week": week, "recent": recent},
              open(cache, "w", encoding="utf-8"))
    return emap, week, recent

def fetch_options_levels(sym, spot):
    """Call/put walls, naive GEX profile + gamma flip, P/C ratios.
    Uses the expiry 20-60 DTE (falls back to nearest). Naive dealer assumption:
    dealers long calls (+gamma), short puts (-gamma). Approximation only."""
    try:
        import yfinance as yf
        t = yf.Ticker(sym)
        exps = t.options
        if not exps: return None
        today = datetime.date.today()
        def dte(e): return (datetime.date.fromisoformat(e) - today).days
        monthly = [e for e in exps if 20 <= dte(e) <= 60] or list(exps[:3])
        exp = monthly[0]
        oc = t.option_chain(exp)
        T = max(dte(exp), 1) / 365.0
        lo, hi = spot * 0.75, spot * 1.25
        def rows(df, sign):
            out = []
            def num(x):
                try:
                    v = float(x)
                    return v if v == v else 0.0  # NaN -> 0
                except (TypeError, ValueError):
                    return 0.0
            for _, r in df.iterrows():
                k, oi, iv = num(r["strike"]), num(r.get("openInterest")), num(r.get("impliedVolatility"))
                vol = num(r.get("volume"))
                if not (lo <= k <= hi) or oi <= 0: continue
                if iv <= 0.01 or iv > 5: iv = 0.5
                d1 = (math.log(spot / k) + 0.5 * iv * iv * T) / (iv * math.sqrt(T))
                gamma = math.exp(-d1 * d1 / 2) / math.sqrt(2 * math.pi) / (spot * iv * math.sqrt(T))
                out.append({"k": k, "oi": oi, "vol": vol,
                            "gex": sign * gamma * oi * 100 * spot})
            return out
        calls, puts = rows(oc.calls, +1), rows(oc.puts, -1)
        if not calls or not puts: return None
        call_wall = max(calls, key=lambda x: x["oi"])["k"]
        put_wall = max(puts, key=lambda x: x["oi"])["k"]
        strikes = sorted({x["k"] for x in calls + puts})
        net = {k: 0.0 for k in strikes}
        for x in calls + puts: net[x["k"]] += x["gex"]
        # gamma flip: highest strike below which cumulative net GEX is negative
        flip = None; cum = 0.0
        for k in strikes:
            cum += net[k]
            if cum > 0: flip = k; break
        if flip is not None and abs(flip - spot) / spot > 0.15:
            flip = None  # too far from spot to be a usable level
        total_gex = sum(net.values())
        cvol, pvol = sum(x["vol"] for x in calls), sum(x["vol"] for x in puts)
        coi, poi = sum(x["oi"] for x in calls), sum(x["oi"] for x in puts)
        pc_vol = round(pvol / cvol, 2) if cvol else None
        pc_oi = round(poi / coi, 2) if coi else None
        flow_switch = (pc_vol is not None and pc_oi is not None
                       and ((pc_vol > 1.2 and pc_oi < 0.9) or (pc_vol < 0.6 and pc_oi > 1.1)))
        top_oi = sorted(calls + puts, key=lambda x: -x["oi"])[:5]
        return {"expiry": exp, "dte": dte(exp),
                "call_wall": call_wall, "put_wall": put_wall,
                "gamma_flip": flip, "net_gex_sign": "LONG" if total_gex > 0 else "SHORT",
                "pc_vol": pc_vol, "pc_oi": pc_oi, "flow_switch": flow_switch,
                "top_oi_strikes": [{"k": x["k"], "oi": int(x["oi"]),
                                    "type": "C" if x["gex"] >= 0 else "P"} for x in top_oi]}
    except Exception as e:
        return {"error": str(e)[:120]}

# ------------------------------------------- options: ladder, flow, snapshots

def _num(x):
    try:
        v = float(x)
        return v if v == v else 0.0
    except (TypeError, ValueError):
        return 0.0

def _opt_rows(df, sign, spot, T, lo_f=0.75, hi_f=1.25):
    out = []
    for _, r in df.iterrows():
        k = _num(r["strike"])
        if not (spot * lo_f <= k <= spot * hi_f): continue
        oi, iv, vol = _num(r.get("openInterest")), _num(r.get("impliedVolatility")), _num(r.get("volume"))
        row = {"k": k, "oi": oi, "vol": vol, "sign": sign,
               "iv": iv if 0.01 < iv <= 5 else None,
               "last": _num(r.get("lastPrice")), "bid": _num(r.get("bid")), "ask": _num(r.get("ask"))}
        if oi > 0:
            ivv = iv if 0.01 < iv <= 5 else 0.5
            d1 = (math.log(spot / k) + 0.5 * ivv * ivv * T) / (ivv * math.sqrt(T))
            row["gex"] = sign * math.exp(-d1 * d1 / 2) / math.sqrt(2 * math.pi) / (spot * ivv * math.sqrt(T)) * oi * 100 * spot
        else:
            row["gex"] = 0.0
        out.append(row)
    return out

def _gex_snapshot_delta(sym, oi_map, basis="oi"):
    """Persist today's per-strike weight; return {strike: %change vs prior day}.
    `basis` tags whether the weights are open interest or volume — we only
    compare like-for-like, so a day that fell back to volume doesn't produce
    fake "OI dropped 90%" deltas against a prior OI-basis day."""
    d = os.path.join(DATA, "gex_snapshots")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"{sym.replace('=','_')}.json")
    today = datetime.date.today().isoformat()
    prev = None
    if os.path.exists(p):
        try:
            old = json.load(open(p, encoding="utf-8"))
            prev = old.get("prev") if old.get("date") == today else {"date": old["date"], "oi": old["oi"], "basis": old.get("basis", "oi")}
        except Exception:
            prev = None
    json.dump({"date": today, "basis": basis, "oi": {str(k): v for k, v in oi_map.items()}, "prev": prev},
              open(p, "w", encoding="utf-8"))
    if not prev or prev.get("basis", "oi") != basis: return {}
    out = {}
    for k, v in oi_map.items():
        pv = (prev.get("oi") or {}).get(str(k))
        if pv and (pv[0] + pv[1]) > 20:
            out[k] = round(100 * ((v[0] + v[1]) / (pv[0] + pv[1]) - 1))
    return out

def _gex_commentary(ladder, spot, call_wall, put_wall, flip, net_sign):
    if not ladder: return ""
    pos = [r for r in ladder if r["gex"] > 0]
    king = max(pos, key=lambda r: r["gex"]) if pos else None
    lines = []
    if king:
        dist = abs(king["k"] / spot - 1)
        rel = ("AT price" if dist < 0.005 else
               f"{round(dist*100,1)}% {'above' if king['k']>spot else 'below'} price")
        grow = f", OI {'+' if king.get('doi',0)>0 else ''}{king['doi']}% d/d" if king.get("doi") is not None else ""
        # only claim "pin" when the strike is actually near spot; otherwise it is
        # just the biggest strike and pin language over-claims.
        tail = ("— price tends to pin toward it into expiry." if dist <= 0.07
                else "— too far from spot to act as a pin; treat as a magnet only if price gets there.")
        lines.append(f"THE KING: ${king['k']:g} is the biggest positive-gamma strike ({rel}{grow}) {tail}")
    below = sorted((r for r in ladder if r["k"] < spot), key=lambda r: -r["k"])
    first_neg = next((r for r in below if r["gex"] < 0), None)
    if first_neg:
        pos_floor = [r for r in below if r["gex"] > 0 and r["k"] > first_neg["k"]]
        if pos_floor:
            lines.append(f"Positive-gamma floor holds down to ${min(r['k'] for r in pos_floor):g}; "
                         f"first negative strike below is ${first_neg['k']:g} — under it, dealer hedging AMPLIFIES the downside.")
        else:
            lines.append(f"No positive-gamma cushion below — ${first_neg['k']:g} and lower are amplifier strikes; moves down there get fast.")
    if net_sign == "SHORT":
        lines.append("Net SHORT gamma overall: expect exaggerated moves in both directions; size down, widen stops.")
    else:
        lines.append("Net LONG gamma overall: dealers dampen moves — grinding tape, favors patient adds near support.")
    eroding = [r for r in ladder if r.get("doi") is not None and r["doi"] <= -15 and r["gex"] > 0]
    building = [r for r in ladder if r.get("doi") is not None and r["doi"] >= 15]
    if eroding: lines.append("Eroding support: OI dropping ≥15% at " + ", ".join(f"${r['k']:g}" for r in eroding[:3]) + ".")
    if building: lines.append("Building: OI up ≥15% at " + ", ".join(f"${r['k']:g}" for r in building[:3]) + ".")
    return " ".join(lines)

def fetch_options_full(sym, spot):
    """Everything options for one name: walls/flip levels + GEX ladder with
    day-over-day OI change + notable flow rows (EOD side-inference).
    All of it is an EOD approximation, not live tape."""
    try:
        import yfinance as yf
        t = yf.Ticker(sym)
        exps = list(t.options)
        if not exps: return {"error": "no listed options for this symbol"}
        today = datetime.date.today()
        dte = lambda e: (datetime.date.fromisoformat(e) - today).days
        # GEX candidates: widen the window and rank by distance from the 30d
        # sweet spot, so a stale/empty expiry doesn't kill the whole read when
        # a healthier one sits right next to it.
        gex_cands = sorted([e for e in exps if 10 <= dte(e) <= 90],
                           key=lambda e: abs(dte(e) - 30))
        if not gex_cands: gex_cands = exps[:3]
        flow_exps = list(dict.fromkeys(
            [e for e in exps if 0 <= dte(e) <= 45][:3] + gex_cands[:4]))
        chains = {}
        for e in flow_exps:
            try: chains[e] = t.option_chain(e)
            except Exception: pass
        # Try tight strike window with OI first, then widen. If OI is missing
        # everywhere (yfinance/Yahoo currently drops openInterest on a lot of
        # near-dated chains), fall back to volume as a positioning proxy so the
        # user still gets walls, IV and flow instead of an empty panel.
        gex_exp, calls, puts, T, weight_key = None, None, None, None, "oi"
        for wkey in ("oi", "vol"):
            for lo_f, hi_f in ((0.75, 1.25), (0.55, 1.45)):
                for cand in gex_cands:
                    if cand not in chains: continue
                    Tc = max(dte(cand), 1) / 365.0
                    c = [x for x in _opt_rows(chains[cand].calls, +1, spot, Tc, lo_f, hi_f) if x[wkey] > 0]
                    p = [x for x in _opt_rows(chains[cand].puts, -1, spot, Tc, lo_f, hi_f) if x[wkey] > 0]
                    if len(c) >= 3 and len(p) >= 3:
                        gex_exp, calls, puts, T, weight_key = cand, c, p, Tc, wkey
                        break
                if gex_exp is not None: break
            if gex_exp is not None: break
        if gex_exp is None:
            return {"error": f"no options activity at strikes near ${spot:.0f} in nearby expiries"}
        oc = chains[gex_exp]
        # when falling back to volume, treat today's volume as the positioning
        # weight for walls/GEX. _opt_rows only computed gex on rows that had
        # oi>0, so we need to (a) recompute gex using vol as size, and
        # (b) alias oi = vol so the downstream wall/PC math is unchanged.
        if weight_key == "vol":
            for x in calls + puts:
                iv = x.get("iv") or 0.5
                d1 = (math.log(spot / x["k"]) + 0.5 * iv * iv * T) / (iv * math.sqrt(T))
                x["gex"] = (x["sign"] * math.exp(-d1 * d1 / 2) / math.sqrt(2 * math.pi)
                            / (spot * iv * math.sqrt(T)) * x["vol"] * 100 * spot)
                x["oi"] = x["vol"]
        call_wall = max(calls, key=lambda x: x["oi"])["k"]
        put_wall = max(puts, key=lambda x: x["oi"])["k"]
        strikes = sorted({x["k"] for x in calls + puts})
        net, oi_map = {k: 0.0 for k in strikes}, {}
        for x in calls + puts:
            net[x["k"]] += x["gex"]
            o = oi_map.setdefault(x["k"], [0, 0])
            o[0 if x["sign"] > 0 else 1] += int(x["oi"])
        flip, cum = None, 0.0
        for k in strikes:
            cum += net[k]
            if cum > 0: flip = k; break
        if flip is not None and abs(flip - spot) / spot > 0.15: flip = None
        total_gex = sum(net.values())
        cvol, pvol = sum(x["vol"] for x in calls), sum(x["vol"] for x in puts)
        coi_t, poi_t = sum(x["oi"] for x in calls), sum(x["oi"] for x in puts)
        pc_vol = round(pvol / cvol, 2) if cvol else None
        pc_oi = round(poi_t / coi_t, 2) if coi_t else None
        flow_switch = (pc_vol is not None and pc_oi is not None
                       and ((pc_vol > 1.2 and pc_oi < 0.9) or (pc_vol < 0.6 and pc_oi > 1.1)))
        doi = _gex_snapshot_delta(sym, oi_map, basis=weight_key)
        ladder = [{"k": k, "gex": round(net[k]), "doi": doi.get(k)}
                  for k in strikes if abs(k - spot) / spot <= 0.13]
        ladder.sort(key=lambda r: -r["k"])
        net_sign = "LONG" if total_gex > 0 else "SHORT"
        # ---- IV skew (gex expiry): 90%-moneyness put IV vs 110% call IV
        def _iv_near(rows_l, target):
            cand = [x for x in rows_l if x.get("iv")]
            return min(cand, key=lambda x: abs(x["k"] - target))["iv"] if cand else None
        atm_ivs = [v for v in (_iv_near(calls, spot), _iv_near(puts, spot)) if v]
        atm_iv = round(sum(atm_ivs) / len(atm_ivs) * 100, 1) if atm_ivs else None
        p90, c110 = _iv_near(puts, spot * 0.90), _iv_near(calls, spot * 1.10)
        skew_pts = round((p90 - c110) * 100, 1) if (p90 and c110) else None
        skew_read = None
        if skew_pts is not None:
            skew_read = ("STEEP — heavy downside hedging" if skew_pts > 8 else
                         "normal" if skew_pts >= 3 else
                         "FLAT/CALL-BID — upside chase, little fear")
        # ---- notable flow rows (all fetched expiries) + chain activity
        flow = []
        tot_vol = tot_oi = 0.0
        for e, oc2 in chains.items():
            Tf = max(dte(e), 1) / 365.0
            for df, cp in ((oc2.calls, "C"), (oc2.puts, "P")):
                rows2 = _opt_rows(df, 1 if cp == "C" else -1, spot, Tf, 0.82, 1.18)
                for x in rows2:
                    tot_vol += x["vol"]; tot_oi += x["oi"]
                for x in rows2:
                    if x["vol"] < 200: continue
                    prem = x["vol"] * x["last"] * 100
                    voloi = round(x["vol"] / x["oi"], 1) if x["oi"] >= 10 else None
                    if prem < 25_000: continue
                    if (voloi is None or voloi < 2) and prem < 250_000: continue
                    side = ("ASK" if x["ask"] and x["last"] >= x["ask"] * 0.98 else
                            "BID" if x["bid"] and x["last"] <= x["bid"] * 1.02 else "MID")
                    flow.append({"cp": cp, "k": x["k"], "exp": e, "dte": dte(e),
                                 "vol": int(x["vol"]), "oi": int(x["oi"]), "voloi": voloi,
                                 "last": round(x["last"], 2), "prem": int(prem), "side": side,
                                 "bullish": (cp == "C" and side == "ASK") or (cp == "P" and side == "BID"),
                                 "bearish": (cp == "P" and side == "ASK") or (cp == "C" and side == "BID")})
        flow.sort(key=lambda x: -x["prem"])
        bull_prem = sum(f["prem"] for f in flow if f["bullish"])
        bear_prem = sum(f["prem"] for f in flow if f["bearish"])
        return {"expiry": gex_exp, "dte": dte(gex_exp), "spot": round(spot, 2),
                "weight_basis": weight_key,  # "oi" or "vol" (fallback when Yahoo drops OI)
                "call_wall": call_wall, "put_wall": put_wall, "gamma_flip": flip,
                "net_gex_sign": net_sign, "pc_vol": pc_vol, "pc_oi": pc_oi,
                "flow_switch": flow_switch,
                "atm_iv": atm_iv, "skew_pts": skew_pts, "skew_read": skew_read,
                "activity_ratio": round(tot_vol / tot_oi, 2) if tot_oi else None,
                "ladder": ladder,
                "commentary": _gex_commentary(ladder, spot, call_wall, put_wall, flip, net_sign),
                "flow": flow[:12],
                "flow_tilt": {"bull_prem": bull_prem, "bear_prem": bear_prem,
                              "read": ("BULLISH" if bull_prem > bear_prem * 1.5 else
                                       "BEARISH" if bear_prem > bull_prem * 1.5 else "MIXED")}}
    except Exception as e:
        return {"error": str(e)[:120]}

# ------------------------------------------------------------- indicator lib

def sma(xs, n, i=None):
    i = len(xs) - 1 if i is None else i
    if i + 1 < n: return None
    return sum(xs[i - n + 1:i + 1]) / n

def ema_series(xs, n):
    k, out, e = 2 / (n + 1), [], None
    for x in xs:
        e = x if e is None else x * k + e * (1 - k)
        out.append(e)
    return out

def sma_series(xs, n):
    out, s = [None] * len(xs), 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= n: s -= xs[i - n]
        if i >= n - 1: out[i] = s / n
    return out

def rsi(closes, n=14):
    if len(closes) < n + 1: return None
    g = l = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        g += max(d, 0); l += max(-d, 0)
    ag, al = g / n, l / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    if al == 0: return 100.0
    return 100 - 100 / (1 + ag / al)

def rsi_series(closes, n=14):
    out = [None] * len(closes)
    if len(closes) < n + 1: return out
    g = l = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        g += max(d, 0); l += max(-d, 0)
    ag, al = g / n, l / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out

def atr(bars, n=14):
    if len(bars) < n + 1: return None
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    a = sum(trs[:n]) / n
    for t in trs[n:]:
        a = (a * (n - 1) + t) / n
    return a

def adx(bars, n=14):
    if len(bars) < 2 * n + 1: return None
    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(bars)):
        up = bars[i]["h"] - bars[i - 1]["h"]
        dn = bars[i - 1]["l"] - bars[i]["l"]
        plus_dm.append(up if (up > dn and up > 0) else 0.0)
        minus_dm.append(dn if (dn > up and dn > 0) else 0.0)
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    def wilder(xs):
        s = sum(xs[:n]); out = [s]
        for x in xs[n:]:
            s = s - s / n + x; out.append(s)
        return out
    trn, pn, mn = wilder(trs), wilder(plus_dm), wilder(minus_dm)
    dxs, pdi_last, mdi_last = [], 0.0, 0.0
    for i in range(len(trn)):
        if trn[i] == 0: continue
        pdi = 100 * pn[i] / trn[i]; mdi = 100 * mn[i] / trn[i]
        pdi_last, mdi_last = pdi, mdi
        if pdi + mdi > 0:
            dxs.append(100 * abs(pdi - mdi) / (pdi + mdi))
    if len(dxs) < n: return None
    a = sum(dxs[:n]) / n
    for d in dxs[n:]:
        a = (a * (n - 1) + d) / n
    return {"adx": a, "pdi": pdi_last, "mdi": mdi_last}

def macd_hist(closes):
    if len(closes) < 35: return None
    e12, e26 = ema_series(closes, 12), ema_series(closes, 26)
    line = [a - b for a, b in zip(e12, e26)]
    sig = ema_series(line, 9)
    h = [a - b for a, b in zip(line, sig)]
    return {"line": line[-1], "hist": h[-1], "hist_prev": h[-4], "above_zero": line[-1] > 0}

def bb_width_pct_rank(closes, n=20, lookback=126):
    if len(closes) < n + lookback // 2: return None
    widths = []
    for i in range(n - 1, len(closes)):
        w = closes[i - n + 1:i + 1]
        m = sum(w) / n
        sd = math.sqrt(sum((x - m) ** 2 for x in w) / n)
        widths.append((4 * sd) / m if m else 0)
    hist = widths[-lookback:]
    rank = sum(1 for x in hist if x <= widths[-1]) / len(hist)
    return {"width": widths[-1], "pct_rank": rank * 100}

def linreg_slope_r2(ys):
    nn = len(ys)
    if nn < 10: return None
    xs = list(range(nn))
    mx, my = sum(xs) / nn, sum(ys) / nn
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0: return None
    b = sxy / sxx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (my + b * (x - mx))) ** 2 for x, y in zip(xs, ys))
    return {"slope": b, "r2": 1 - ss_res / ss_tot if ss_tot else 0}

def pivots(bars, left=5, right=5, lookback=252):
    """Swing highs/lows with N-bar strength. Returns (highs, lows) as [(idx, price)]."""
    n = len(bars)
    start = max(left, n - lookback)
    highs, lows = [], []
    for i in range(start, n - right):
        h, l = bars[i]["h"], bars[i]["l"]
        if all(bars[j]["h"] <= h for j in range(i - left, i + right + 1) if j != i):
            highs.append((i, h))
        if all(bars[j]["l"] >= l for j in range(i - left, i + right + 1) if j != i):
            lows.append((i, l))
    return highs, lows

def cluster_levels(levels, tol=0.015):
    """Merge nearby price levels; return [(price, touches)] sorted by price."""
    out = []
    for p in sorted(levels):
        if out and abs(p - out[-1][0]) / out[-1][0] <= tol:
            prev_p, cnt = out[-1]
            out[-1] = ((prev_p * cnt + p) / (cnt + 1), cnt + 1)
        else:
            out.append((p, 1))
    return out

def obv_series(closes, vols):
    out, o = [], 0.0
    for i in range(len(closes)):
        if i and closes[i] > closes[i - 1]: o += vols[i]
        elif i and closes[i] < closes[i - 1]: o -= vols[i]
        out.append(o)
    return out

def cross_days_ago(fast, slow, direction, max_ago=15):
    """Bars since fast crossed above (bull) / below (bear) slow. None if not in window."""
    n = min(len(fast), len(slow))
    for ago in range(0, max_ago):
        i = n - 1 - ago
        if i < 1 or fast[i] is None or slow[i] is None or fast[i-1] is None or slow[i-1] is None:
            break
        if direction == "bull" and fast[i] > slow[i] and fast[i-1] <= slow[i-1]: return ago
        if direction == "bear" and fast[i] < slow[i] and fast[i-1] >= slow[i-1]: return ago
    return None

# ----------------------------------------------------------------- rotation

def rrg_point_series(closes, bench, window, mom_period):
    """JdK-style RS-Ratio / RS-Momentum (z-score approximation), stdlib port
    of rotation_tool.py. Returns list of (ratio, mom) for the last bars."""
    m = min(len(closes), len(bench))
    rs = [100.0 * closes[-m + i] / bench[-m + i] for i in range(m)]
    def zser(xs, w):
        out = [None] * len(xs)
        for i in range(w - 1, len(xs)):
            win = xs[i - w + 1:i + 1]
            mu = sum(win) / w
            sd = math.sqrt(sum((x - mu) ** 2 for x in win) / w)
            out[i] = 100.0 + (xs[i] - mu) / sd if sd else 100.0
        return out
    ratio = zser(rs, window)
    roc = [None] * len(ratio)
    for i in range(mom_period, len(ratio)):
        if ratio[i] is not None and ratio[i - mom_period] is not None:
            roc[i] = ratio[i] - ratio[i - mom_period]
    valid = [x for x in roc if x is not None]
    mom = [None] * len(roc)
    for i in range(len(roc)):
        if roc[i] is None: continue
        j0 = max(0, i - window + 1)
        win = [x for x in roc[j0:i + 1] if x is not None]
        if len(win) < window // 2: continue
        mu = sum(win) / len(win)
        sd = math.sqrt(sum((x - mu) ** 2 for x in win) / len(win))
        mom[i] = 100.0 + (roc[i] - mu) / sd if sd else 100.0
    return ratio, mom

def quadrant(r, m):
    if r >= 100 and m >= 100: return "leading"
    if r >= 100: return "weakening"
    if m >= 100: return "improving"
    return "lagging"

def weekly_closes(bars):
    """Last close of each ISO week."""
    out, cur_key = [], None
    for b in bars:
        d = datetime.date.fromisoformat(b["d"])
        key = d.isocalendar()[:2]
        if key != cur_key:
            out.append(b["c"]); cur_key = key
        else:
            out[-1] = b["c"]
    return out

def build_rotation(hist):
    spy_d = [b["c"] for b in hist["SPY"]]
    spy_w = weekly_closes(hist["SPY"])
    out = {"daily": [], "weekly": []}
    for tf, bench, window, momp in (("daily", spy_d, 50, 10), ("weekly", spy_w, 14, 4)):
        rows = []
        for etf, name in ROTATION_ETFS.items():
            if etf not in hist: continue
            closes = [b["c"] for b in hist[etf]] if tf == "daily" else weekly_closes(hist[etf])
            if len(closes) < window * 2: continue
            ratio, mom = rrg_point_series(closes, bench, window, momp)
            if ratio[-1] is None or mom[-1] is None: continue
            score = 0.6 * (ratio[-1] - 100) + 0.4 * (mom[-1] - 100)
            rows.append({"etf": etf, "name": name,
                         "ratio": round(ratio[-1], 2), "mom": round(mom[-1], 2),
                         "score": round(score, 3), "quad": quadrant(ratio[-1], mom[-1])})
        rows.sort(key=lambda r: -r["score"])
        for i, r in enumerate(rows): r["rank"] = i + 1
        out[tf] = rows
    return out

# ----------------------------------------------------------------- analysis

def clamp(x, lo=0, hi=100): return max(lo, min(hi, x))

def fib_levels(bars):
    """Retracements of the dominant 52w upswing (low -> subsequent high)."""
    closes = [b["c"] for b in bars[-252:]]
    if len(closes) < 60: return None
    ilo = min(range(len(closes)), key=lambda i: closes[i])
    seg = closes[ilo:]
    ihi = max(range(len(seg)), key=lambda i: seg[i])
    lo, hi = closes[ilo], seg[ihi]
    if hi <= lo * 1.05 or ihi < 5: return None
    rng = hi - lo
    levels = {"0% (swing high)": hi, "23.6%": hi - .236 * rng, "38.2%": hi - .382 * rng,
              "50%": hi - .5 * rng, "61.8%": hi - .618 * rng, "78.6%": hi - .786 * rng,
              "100% (swing low)": lo}
    return {k: round(v, 2) for k, v in levels.items()}

def analyze(sym, bars, spy_closes, sector=None, earnings_date=None, recent_report=None):
    closes = [b["c"] for b in bars]
    vols = [b["v"] for b in bars]
    n = len(bars)
    out = {"ticker": sym, "bars": n, "close": round(closes[-1], 2), "date": bars[-1]["d"],
           "sector": sector, "gates": {}, "scores": {}}

    # ---- Stage 0 gates
    dv = sorted(c * v for c, v in zip(closes[-50:], vols[-50:]))
    med_dv = dv[len(dv) // 2] if dv else 0
    out["median_dollar_vol_m"] = round(med_dv / 1e6, 1)
    g = out["gates"]
    g["TR01_liquidity"] = med_dv >= MIN_DOLLAR_VOL
    g["TR03_price_floor"] = closes[-1] >= MIN_PRICE
    g["TR11_history"] = n >= MIN_BARS
    days_to_earn = None
    if earnings_date:
        days_to_earn = (datetime.date.fromisoformat(earnings_date) - datetime.date.today()).days
    # "no date" must not be ambiguous between "none upcoming" and "we don't know"
    out["earnings"] = {"date": earnings_date, "days": days_to_earn,
                       "checked_days": EARNINGS_LOOKAHEAD_DAYS,
                       "status": ("SCHEDULED" if earnings_date else "NONE_WITHIN_WINDOW"),
                       "alert_upcoming": bool(days_to_earn is not None
                                              and 0 <= days_to_earn <= EARNINGS_ALERT_DAYS),
                       "recent": recent_report,
                       "alert_recent": bool(recent_report)}
    g["TR06_earnings_clear"] = days_to_earn is None or days_to_earn > EARNINGS_BLACKOUT_DAYS
    if not g["TR11_history"]:
        out["gates_passed"] = False
        out["status"] = "INSUFFICIENT_HISTORY"
        return out

    # ---- Stage 2 regime
    s200 = sma(closes, 200); s200_prev = sma(closes, 200, n - 21)
    s50, s20, s150 = sma(closes, 50), sma(closes, 20), sma(closes, 150)
    g["RG01_above_200d"] = closes[-1] > s200
    g["RG02_200d_rising"] = s200 > s200_prev
    ax = adx(bars)
    g["RG11_trending"] = (ax["adx"] >= 15) if ax else False
    stack = s20 > s50 > s200
    wk = weekly_closes(bars)
    wk30 = sma(wk, 30)
    weekly_ok = wk30 is not None and wk[-1] > wk30
    out["ma"] = {"sma20": round(s20, 2), "sma50": round(s50, 2), "sma150": round(s150, 2) if s150 else None,
                 "sma200": round(s200, 2), "stack_aligned": stack,
                 "weekly_above_30w": weekly_ok,
                 "adx": round(ax["adx"], 1) if ax else None,
                 "dmi_bull": (ax["pdi"] > ax["mdi"]) if ax else None}

    # ---- Stage 3 relative strength
    m = min(len(closes), len(spy_closes))
    ratio = [closes[-m + i] / spy_closes[-m + i] for i in range(m)]
    rs_ratio_pos = ratio[-1] / max(ratio[-60:])
    ret_12m = closes[-1] / closes[-min(n, 252)] - 1
    ret_12_1 = (closes[-22] / closes[-min(n, 252)] - 1) if n >= 252 else ret_12m
    ret_3m = closes[-1] / closes[-63] - 1 if n >= 63 else 0
    ret_1m = closes[-1] / closes[-21] - 1 if n >= 21 else 0
    spy_3m = spy_closes[-1] / spy_closes[-63] - 1 if len(spy_closes) >= 63 else 0
    out["rs"] = {"ratio_vs_60d_high": round(rs_ratio_pos, 4),
                 "ret_12m": round(ret_12m * 100, 1), "ret_12_1": round(ret_12_1 * 100, 1),
                 "ret_3m": round(ret_3m * 100, 1), "ret_1m": round(ret_1m * 100, 1),
                 "spy_outperform_3m": ret_3m > spy_3m}

    # ---- Stage 4 structure
    hi52 = max(closes[-252:]); lo52 = min(closes[-252:])
    off_high = (hi52 - closes[-1]) / hi52
    base_win = bars[-30:-1]
    bh = max(b["h"] for b in base_win); bl = min(b["l"] for b in base_win)
    base_depth = (bh - bl) / bh
    rng10 = [(b["h"] - b["l"]) / b["c"] for b in bars[-10:]]
    rng30 = [(b["h"] - b["l"]) / b["c"] for b in bars[-30:]]
    tightening = (sum(rng10) / 10) < (sum(rng30) / 30)
    lr = linreg_slope_r2([math.log(c) for c in closes[-120:]])
    ph, pl = pivots(bars)
    res_shelves = [round(p, 2) for p, cnt in cluster_levels([x[1] for x in ph]) if p > closes[-1] * 1.005]
    sup_shelves = [round(p, 2) for p, cnt in cluster_levels([x[1] for x in pl]) if p < closes[-1] * 0.995]
    hh = len(ph) >= 2 and ph[-1][1] > ph[-2][1]
    hl = len(pl) >= 2 and pl[-1][1] > pl[-2][1]
    # spring: undercut of prior 20d low in last 5 bars, closed back above it
    prior_low = min(b["l"] for b in bars[-25:-5])
    sprung = any(b["l"] < prior_low for b in bars[-5:]) and closes[-1] > prior_low
    tr_today = bars[-1]["h"] - bars[-1]["l"]
    nr7 = all(tr_today <= (bars[i]["h"] - bars[i]["l"]) for i in range(-7, -1))
    # shock bars: overnight gap >=3% or single-day move >=5% in the last 10 sessions.
    # A name that just gapped is a post-event trade, NOT a quiet base — the setup
    # classifier cannot see this and would otherwise mislabel it.
    shocks = []
    for i in range(max(1, n - 10), n):
        gp = 100 * (bars[i]["o"] / bars[i - 1]["c"] - 1)
        mv = 100 * (bars[i]["c"] / bars[i - 1]["c"] - 1)
        if abs(gp) >= 3 or abs(mv) >= 5:
            shocks.append({"date": bars[i]["d"], "gap_pct": round(gp, 1),
                           "move_pct": round(mv, 1), "sessions_ago": n - 1 - i})
    last_shock = shocks[-1] if shocks else None
    out["structure"] = {
        "hi52": round(hi52, 2), "lo52": round(lo52, 2),
        "off_52w_high_pct": round(off_high * 100, 1),
        "base_high": round(bh, 2), "base_low": round(bl, 2),
        "base_depth_pct": round(base_depth * 100, 1), "tightening": tightening,
        "trend_r2_120d": round(lr["r2"], 2) if lr else None,
        "resistance": res_shelves[:3], "support": sup_shelves[-3:][::-1],
        "hh_hl": hh and hl, "nr7": nr7, "spring": sprung,
        "fib": fib_levels(bars),
        "shock": last_shock, "shock_count_10d": len(shocks),
    }

    # ---- Stage 5 participation
    v50 = sum(vols[-50:]) / 50
    vol_ratio_today = vols[-1] / v50 if v50 else 0
    base_vol_ratio = (sum(b["v"] for b in base_win) / len(base_win)) / v50 if v50 else 1
    upv = sum(v for c0, c1, v in zip(closes[-51:-1], closes[-50:], vols[-50:]) if c1 > c0)
    dnv = sum(v for c0, c1, v in zip(closes[-51:-1], closes[-50:], vols[-50:]) if c1 < c0)
    ud_ratio = upv / dnv if dnv else 2.0
    accum = distrib = 0
    for i in range(n - 25, n):
        if vols[i] > v50 * 1.2:
            if closes[i] > closes[i - 1]: accum += 1
            elif closes[i] < closes[i - 1]: distrib += 1
    obv = obv_series(closes, vols)
    obv_lr = linreg_slope_r2(obv[-20:])
    obv_up = obv_lr and obv_lr["slope"] > 0
    out["volume"] = {"rvol": round(vol_ratio_today, 2), "base_dryup": round(base_vol_ratio, 2),
                     "updown_ratio_50d": round(ud_ratio, 2),
                     "accum_days_25d": accum, "distrib_days_25d": distrib,
                     "obv_rising": bool(obv_up)}

    # ---- Stage 6 momentum + volatility
    r = rsi(closes); mh = macd_hist(closes); bw = bb_width_pct_rank(closes)
    a = atr(bars)
    ext_atr = (closes[-1] - s20) / a if a else 0
    out["momentum"] = {"rsi14": round(r, 1) if r else None,
                       "macd_above_zero": mh["above_zero"] if mh else None,
                       "macd_hist_rising": (mh["hist"] > mh["hist_prev"]) if mh else None}
    out["volatility"] = {"atr": round(a, 2) if a else None,
                         "atr_pct": round(a / closes[-1] * 100, 2) if a else None,
                         "ext_atr_vs_20d": round(ext_atr, 2),
                         "bb_width_pct_rank": round(bw["pct_rank"], 0) if bw else None,
                         "squeeze": (bw["pct_rank"] < 25) if bw else False}
    g["VO03_not_extended"] = ext_atr <= 4.0
    out["gates_passed"] = all(g.values())

    # ---- signal events (recency, in trading days)
    e8, e21, e13d, e50e = ema_series(closes, 8), ema_series(closes, 21), ema_series(closes, 13), ema_series(closes, 50)
    s50s, s200s = sma_series(closes, 50), sma_series(closes, 200)
    rs_ser = rsi_series(closes)
    ev = {}
    for name, f, s, d in (("golden_cross", s50s, s200s, "bull"), ("death_cross", s50s, s200s, "bear"),
                          ("ema_8_21_bull", e8, e21, "bull"), ("ema_8_21_bear", e8, e21, "bear"),
                          ("ema_13_50_bull", e13d, e50e, "bull"), ("ema_13_50_bear", e13d, e50e, "bear")):
        ago = cross_days_ago(f, s, d)
        if ago is not None: ev[name] = ago
    if r is not None and r >= 70: ev["rsi_overbought"] = 0
    if r is not None and r <= 30: ev["rsi_oversold"] = 0
    for ago in range(0, 15):
        i = n - 1 - ago
        if i < 252: break
        if closes[i] >= max(closes[i - 251:i + 1]):
            ev["new_52w_high"] = ago; break
    out["events"] = ev

    # ---- 12-point checklist (screenshot-style)
    chk = {
        "MA FAN": stack, "200d RISING": g["RG02_200d_rising"], "HH/HL": hh and hl,
        "52W HI": off_high <= 0.08, "CLOSE HI": closes[-1] >= max(closes[-20:]) * 0.98,
        "U/D VOL": ud_ratio > 1.1, "OBV+": bool(obv_up), "VDU": base_vol_ratio < 0.85,
        "ATR SQZ": (bw["pct_rank"] < 25) if bw else False, "NR7": nr7, "SPRING": sprung,
        "W.EMA": weekly_ok,
    }
    chk_score = sum(chk.values())
    out["checklist"] = {"items": chk, "score": chk_score,
                        "tag": ("MOMENTUM BUILDING" if chk_score >= 8 else
                                "WATCH" if chk_score >= 6 else
                                "TRANSITION" if chk_score >= 4 else "WEAK")}

    # ---- setup classification
    prior_25_high = max(b["h"] for b in bars[-26:-1])
    setup = "NONE"
    fresh_shock = last_shock and last_shock["sessions_ago"] <= 5 and last_shock["move_pct"] > 0
    if not g["RG01_above_200d"] or not g["RG02_200d_rising"]:
        setup = "DOWNTREND"
    elif fresh_shock and closes[-1] > s20:
        # post-event continuation (PEAD-style): a real, well-supported setup — but
        # it must be named honestly, not dressed up as a quiet base.
        setup = "POST_EVENT_DRIFT"
    elif closes[-1] > prior_25_high and vol_ratio_today >= 1.4:
        setup = "BREAKOUT"
    elif off_high <= 0.08 and base_depth <= 0.15 and (base_vol_ratio < 0.85 or tightening):
        setup = "BASE_READY"
    elif off_high <= 0.15 and base_depth <= 0.20:
        setup = "BASE_BUILDING"
    elif ext_atr > 3.5:
        setup = "EXTENDED"
    elif stack and (r or 0) >= 55 and ext_atr <= 3.0:
        setup = "MOMENTUM"
    elif stack and abs(closes[-1] - s20) <= (a or 1) and 40 <= (r or 50) <= 55:
        setup = "PULLBACK"
    elif off_high <= 0.25:
        setup = "CONSOLIDATING"
    out["setup"] = setup

    # ---- scores
    sc = {}
    sc["trend"] = clamp((25 if g["RG01_above_200d"] else 0) + (25 if g["RG02_200d_rising"] else 0)
                        + (20 if stack else 0) + (15 if closes[-1] > s50 else 0)
                        + (15 if (ax and ax["adx"] >= 20 and ax["pdi"] > ax["mdi"]) else 0))
    sc["relative_strength"] = clamp(50 * rs_ratio_pos ** 4 + clamp(ret_12_1 * 100, 0, 30)
                                    + clamp(ret_3m * 150, 0, 20))
    sc["participation"] = clamp(clamp((ud_ratio - 0.8) * 50, 0, 40) + accum * 8 - distrib * 6
                                + (10 if obv_up else 0) + (10 if base_vol_ratio < 0.85 else 0))
    sc["structure"] = clamp(clamp((1 - off_high / 0.25) * 40, 0, 40)
                            + clamp((0.20 - base_depth) * 200, 0, 30)
                            + (15 if tightening else 0)
                            + (15 if (lr and lr["r2"] > 0.6 and lr["slope"] > 0) else 0))
    mo = 0
    if r: mo += 30 if 50 <= r <= 75 else (15 if 40 <= r < 50 or 75 < r <= 80 else 0)
    if mh: mo += (20 if mh["above_zero"] else 0) + (25 if mh["hist"] > mh["hist_prev"] else 0)
    if ax and ax["pdi"] > ax["mdi"]: mo += 25
    sc["momentum"] = clamp(mo)
    vo = 50
    if bw: vo += (25 if bw["pct_rank"] < 25 else 0) - (15 if bw["pct_rank"] > 85 else 0)
    vo -= clamp((ext_atr - 2.5) * 20, 0, 35)
    sc["volatility"] = clamp(vo)
    sc["composite"] = round(sc["trend"] * .25 + sc["relative_strength"] * .25
                            + sc["participation"] * .20 + sc["structure"] * .15
                            + sc["momentum"] * .10 + sc["volatility"] * .05, 1)
    # Note: an early Phase C draft added a hand-tuned PEAD sub-score for
    # POST_EVENT_DRIFT names, but backtest_pead.py showed it was ANTI-predictive
    # (Q5-Q1 = -5.13%) — the sweet-spot assumptions (3-8% moves, days 4-15)
    # were wrong; the big/fresh shocks the design penalized were the ones
    # that actually continue. The base composite ranks PEAD names monotonically
    # (Q5-Q1 = +3.06% on the same subset), so we use composite for PEAD too
    # and group them into their own UI section for trader awareness.
    out["scores"] = {k: round(v, 1) for k, v in sc.items()}

    # ---- risk outputs: measured-move target + shelf-aware stop
    if a:
        entry = round(bh * 1.002, 2) if setup in ("BASE_READY", "BASE_BUILDING") else round(closes[-1], 2)
        stop_struct = bl - 0.5 * a
        stop_atr = entry - 2.2 * a
        stop = round(max(stop_struct, stop_atr), 2)
        if stop >= entry * 0.995:
            stop = round(entry - 2.2 * a, 2)
        rps = entry - stop
        # target ladder: next resistance shelf -> 52w high -> measured move
        mm = bh + (bh - bl)                      # measured move from the base
        shelf_above = next((sh for sh in res_shelves if sh >= entry + rps), None)
        candidates = [x for x in (shelf_above, hi52 if hi52 > entry * 1.03 else None, mm) if x]
        target = round(min(candidates) if candidates else entry + 2.5 * a, 2)
        t2 = round(mm, 2) if mm > target * 1.01 else (round(max(candidates), 2) if candidates else None)
        if t2 is not None and t2 <= target * 1.01: t2 = None
        rr = round((target - entry) / rps, 2) if rps > 0 else None
        shares_risk = int((ACCOUNT_SIZE * RISK_BUDGET_PCT / 100) / rps) if rps > 0 else 0
        notional_risk = shares_risk * entry
        # RK04: the spec requires >= 2:1. Flag rather than silently present sub-1R ideas.
        rr_flag = None
        if rr is not None:
            if rr < 1.0: rr_flag = "POOR"
            elif rr < MIN_REWARD_RISK: rr_flag = "BELOW_SPEC"
        # Actually enforce MAX_POSITION_PCT (pre-prod wide-audit HIGH-P1).
        # Previously size_capped was a flag only; the shares number displayed
        # to the trader could still represent > MAX_POSITION_PCT of account
        # for low-priced names with tight stops. Now we cap shares to the
        # position ceiling and keep the un-capped original for context.
        max_notional = ACCOUNT_SIZE * MAX_POSITION_PCT / 100
        capped = notional_risk > max_notional
        if capped and entry > 0:
            shares = int(max_notional / entry)
            notional = shares * entry
        else:
            shares = shares_risk
            notional = notional_risk
        out["risk"] = {"entry": entry, "stop": stop, "risk_per_share": round(rps, 2),
                       "target": target, "target2": t2, "reward_risk": rr,
                       "rr_flag": rr_flag, "rr_to_t2": round((t2 - entry) / rps, 2) if (t2 and rps > 0) else None,
                       "notional": round(notional), "pct_of_account": round(100 * notional / ACCOUNT_SIZE, 1),
                       "size_capped": capped,
                       "shares_at_risk_budget": shares,
                       "shares_uncapped": shares_risk if capped else None,
                       "measured_move": round(mm, 2),
                       "atr_bands": {"+1": round(entry + a, 2), "+2": round(entry + 2*a, 2),
                                     "-1": round(entry - a, 2), "-2": round(entry - 2*a, 2)}}
    return out

# --------------------------------------------------- comments & trade plans

def thesis_comment(r, rotation=None, opts=None):
    """Rule-based long-thesis assessment: holds / weakens where / breaks where."""
    if r.get("status") == "INSUFFICIENT_HISTORY":
        return {"verdict": "NO_DATA", "text": "Not enough price history to assess.", "levels": {}}
    c, ma, st, vol, mom, vola = r["close"], r["ma"], r["structure"], r["volume"], r["momentum"], r["volatility"]
    sup = st.get("support") or []
    hold_lvl = max([x for x in (ma["sma50"], st["base_low"]) if x] + sup[:1] or [ma["sma200"]])
    hold_lvl = round(min(hold_lvl, c * 0.999), 2)
    break_lvl = round(min(st["base_low"], ma["sma50"] or st["base_low"]) , 2)
    dead_lvl = round(ma["sma200"], 2)
    intact = c > (ma["sma50"] or 0) and r["gates"].get("RG01_above_200d")
    weak = c < (ma["sma50"] or 0) or vol["distrib_days_25d"] >= 4
    broken = c < ma["sma200"] or not r["gates"].get("RG02_200d_rising", True)
    verdict = "BROKEN" if broken else ("WEAKENING" if weak else ("INTACT" if intact else "NEUTRAL"))
    parts = []
    if verdict == "INTACT":
        parts.append(f"Long thesis INTACT at ${c} — holds while price defends ${hold_lvl} "
                     f"(first support: {'50d MA' if hold_lvl==ma['sma50'] else 'base low / shelf'}).")
    elif verdict == "WEAKENING":
        parts.append(f"Thesis WEAKENING at ${c} — trading below the 50d (${ma['sma50']})"
                     + (f" with {vol['distrib_days_25d']} distribution days" if vol["distrib_days_25d"] >= 4 else "")
                     + f". Needs to reclaim ${ma['sma50']} to repair.")
    elif verdict == "BROKEN":
        parts.append(f"Thesis BROKEN — price ${c} vs 200d ${ma['sma200']}"
                     + ("" if r["gates"].get("RG02_200d_rising") else " and the 200d is falling")
                     + ". Long case needs a full rebase; stand aside.")
    else:
        parts.append(f"Neutral at ${c}.")
    parts.append(f"A daily close below ${break_lvl} invalidates the swing setup; "
                 f"below ${dead_lvl} (200d) the trend itself is broken — stop and reconsider the whole long case.")
    if mom["rsi14"] is not None:
        if mom["rsi14"] > 75: parts.append(f"RSI {mom['rsi14']} hot — chase risk; prefer pullback entries.")
        elif mom["rsi14"] < 40: parts.append(f"RSI {mom['rsi14']} — momentum washed out, wait for a higher low.")
    if vola["ext_atr_vs_20d"] > 3: parts.append(f"Extended {vola['ext_atr_vs_20d']} ATR above the 20d — poor R:R here.")
    if rotation:
        q = rotation.get("quad"); nm = rotation.get("name"); etf = rotation.get("etf")
        if q == "leading": parts.append(f"Sector confluence: {nm} ({etf}) is LEADING the rotation — market is behind this long.")
        elif q == "improving": parts.append(f"Sector confluence: {nm} ({etf}) is IMPROVING — early-cycle tailwind.")
        elif q == "weakening": parts.append(f"Sector caution: {nm} ({etf}) is WEAKENING on rotation — trim conviction, tighter stop.")
        elif q == "lagging": parts.append(f"Sector headwind: {nm} ({etf}) is LAGGING — this long fights the rotation; demand extra confirmation.")
    if opts and not opts.get("error"):
        oparts = []
        if opts.get("pc_vol") is not None:
            tilt = "call-heavy" if opts["pc_vol"] < 0.8 else ("put-heavy" if opts["pc_vol"] > 1.2 else "balanced")
            oparts.append(f"P/C vol {opts['pc_vol']} ({tilt})")
        if opts.get("flow_switch"): oparts.append("FLOW SWITCH vs positioning — options tone changed, re-check the tape")
        if opts.get("call_wall"): oparts.append(f"call wall ${opts['call_wall']} (magnet/resistance)")
        if opts.get("put_wall"): oparts.append(f"put wall ${opts['put_wall']} (support)")
        if opts.get("gamma_flip"): oparts.append(f"gamma flip ≈ ${opts['gamma_flip']} — below it dealers are short gamma and moves accelerate")
        if opts.get("skew_pts") is not None and opts["skew_pts"] > 8:
            oparts.append(f"put skew steep (+{opts['skew_pts']}pts) — someone is paying up for downside protection")
        elif opts.get("skew_pts") is not None and opts["skew_pts"] < 3:
            oparts.append(f"skew flat ({opts['skew_pts']}pts) — options market shows little downside fear")
        if oparts: parts.append("Options: " + "; ".join(oparts) + ".")
    sh = (r.get("structure") or {}).get("shock")
    if sh:
        when = "today" if sh["sessions_ago"] == 0 else f"{sh['sessions_ago']} session(s) ago"
        parts.append(f"EVENT BAR {when} ({sh['date']}): gap {sh['gap_pct']:+}%, close {sh['move_pct']:+}% — "
                     f"this is a post-event trade, not a quiet base. The catalyst is spent; expect "
                     f"gap-fill risk toward ${round(r['close'] * (1 - abs(sh['gap_pct']) / 100), 2)} and IV crush if it was earnings.")
    ed = r.get("earnings", {})
    if ed.get("days") is not None and 0 <= ed["days"] <= EARNINGS_LOOKAHEAD_DAYS:
        blk = " — INSIDE the blackout window, no new entries" if ed["days"] <= EARNINGS_BLACKOUT_DAYS else ""
        parts.append(f"Earnings {ed['date']} ({ed['days']}d){blk}.")
    elif ed.get("status") == "NONE_WITHIN_WINDOW":
        parts.append(f"No earnings scheduled within {ed.get('checked_days', 21)}d (calendar checked).")
    rec = ed.get("recent")
    if rec:
        s = rec.get("surprise_pct")
        verdict = ("beat" if (s or 0) > 0 else "missed" if s is not None else "reported")
        nums = (f"EPS ${rec['eps']} vs ${rec['eps_forecast']} est"
                if rec.get("eps") is not None and rec.get("eps_forecast") is not None else "")
        parts.append(f"Reported {rec['days_ago']}d ago ({rec['date']}): {verdict}"
                     + (f" by {abs(s)}%" if s is not None else "")
                     + (f" — {nums}." if nums else "."))
    k = r.get("risk") or {}
    if k.get("rr_flag") == "POOR":
        parts.append(f"⚠ Reward:risk to T1 is only {k['reward_risk']}R — below 1:1. "
                     f"Do not take this as shown; wait for a lower entry or a nearer stop.")
    elif k.get("rr_flag") == "BELOW_SPEC":
        parts.append(f"Reward:risk to T1 is {k['reward_risk']}R, under our {MIN_REWARD_RISK}:1 standard"
                     + (f" (T2 would be {k['rr_to_t2']}R)." if k.get("rr_to_t2") else "."))
    return {"verdict": verdict,
            "levels": {"hold": hold_lvl, "invalidate": break_lvl, "trend_break": dead_lvl},
            "text": " ".join(parts)}

def trade_plan(r, opts=None):
    """'If we enter at today's close' — levels, ranges and events to watch."""
    if "risk" not in r: return None
    k, st, vola = r["risk"], r["structure"], r["volatility"]
    above, below = [], []
    for sh in (st.get("resistance") or [])[:2]: above.append({"px": sh, "label": "resistance shelf"})
    if st["hi52"] > r["close"] * 1.005: above.append({"px": st["hi52"], "label": "52w high"})
    if opts and opts.get("call_wall") and opts["call_wall"] > r["close"]:
        above.append({"px": opts["call_wall"], "label": "call wall (OI magnet)"})
    above.append({"px": k["atr_bands"]["+2"], "label": "+2 ATR (day-range extreme)"})
    for sh in (st.get("support") or [])[:2]: below.append({"px": sh, "label": "support shelf"})
    if opts and opts.get("put_wall") and opts["put_wall"] < r["close"]:
        below.append({"px": opts["put_wall"], "label": "put wall (OI support)"})
    if opts and opts.get("gamma_flip") and opts["gamma_flip"] < r["close"]:
        below.append({"px": opts["gamma_flip"], "label": "gamma flip — short-gamma zone below"})
    fib = st.get("fib") or {}
    for name in ("38.2%", "50%"):
        if name in fib and fib[name] < r["close"]:
            below.append({"px": fib[name], "label": f"fib {name}"})
    below.append({"px": k["stop"], "label": "STOP (thesis invalid)"})
    above = sorted({(x['px'], x['label']) for x in above})[:5]
    below = sorted({(x['px'], x['label']) for x in below}, reverse=True)[:6]
    watch = []
    ed = r.get("earnings", {})
    if ed.get("date"): watch.append(f"Earnings {ed['date']} ({ed['days']}d)")
    if vola["squeeze"]: watch.append("Vol squeeze — expansion imminent, direction decides")
    if r["events"].get("ema_8_21_bear") is not None: watch.append("8/21 EMA just crossed bearish — short-term momentum lost")
    if opts and opts.get("net_gex_sign") == "SHORT": watch.append("Net short-gamma tape — expect exaggerated moves both ways")
    watch.append("Macro: see macro.txt calendar (CPI/FOMC/NFP)")
    return {"entry": r["close"], "stop": k["stop"], "t1": k["target"], "t2": k.get("target2"),
            "rr": k["reward_risk"], "expected_day_range": f"±{vola['atr']} (1 ATR)",
            "above": [{"px": p, "label": l} for p, l in above],
            "below": [{"px": p, "label": l} for p, l in below],
            "watch": watch}

def watchlist_health(sym, res, entry_px=None):
    if res.get("status") == "INSUFFICIENT_HISTORY":
        return {"ticker": sym, "verdict": "NO_DATA", "reasons": []}
    ma, vol, mom = res["ma"], res["volume"], res["momentum"]
    reasons, score = [], 0
    if res["close"] < ma["sma50"]: score += 2; reasons.append("below 50d MA")
    if res["close"] < ma["sma200"]: score += 3; reasons.append("below 200d MA")
    if not res["gates"].get("RG02_200d_rising", True): score += 2; reasons.append("200d MA falling")
    if vol["distrib_days_25d"] >= 4: score += 2; reasons.append(f"{vol['distrib_days_25d']} distribution days/25d")
    if vol["updown_ratio_50d"] < 0.9: score += 1; reasons.append("down-volume dominating")
    if (mom["rsi14"] or 50) < 40: score += 1; reasons.append(f"RSI {mom['rsi14']}")
    if mom["macd_above_zero"] is False and mom["macd_hist_rising"] is False:
        score += 1; reasons.append("MACD negative and falling")
    if entry_px and res["close"] < entry_px * 0.93:
        score += 3; reasons.append(f"-{round((1 - res['close'] / entry_px) * 100, 1)}% from entry")
    verdict = "HOLDING" if score <= 1 else ("WARNING" if score <= 4 else "BROKEN")
    positives = []
    if res["close"] > ma["sma20"]: positives.append("above 20d")
    if ma["stack_aligned"]: positives.append("MAs aligned")
    if vol["accum_days_25d"] > vol["distrib_days_25d"]: positives.append("net accumulation")
    return {"ticker": sym, "verdict": verdict, "risk_score": score, "reasons": reasons,
            "positives": positives, "close": res["close"], "setup": res.get("setup"),
            "scores": res.get("scores", {})}

# -------------------------------------------------------- earnings preview

def _hist_earnings_moves(t, bars, max_events=8):
    """Realized next-session move on each past earnings date.
    This is the honest counterweight to the options-implied move: what the
    market ACTUALLY did, versus what it is currently charging for."""
    try:
        ed = t.earnings_dates
        if ed is None or ed.empty:
            return None
    except Exception:
        return None
    closes = {b["d"]: b["c"] for b in bars}
    days = [b["d"] for b in bars]
    idx = {d: i for i, d in enumerate(days)}
    out, beats = [], []
    for ts, row in ed.iterrows():
        try:
            d = ts.date()
        except Exception:
            continue
        if d >= datetime.date.today():
            continue
        rep, est = row.get("Reported EPS"), row.get("EPS Estimate")
        if rep == rep and est == est and est not in (None, 0):
            beats.append(bool(rep >= est))
        # Timing decides which session carries the reaction:
        #   BMO (before ~12:00) -> the move happens THAT SAME day
        #   AMC (>= 12:00)      -> the move happens the NEXT session
        # Getting this wrong measures the day AFTER the move, which reads as
        # near-zero volatility and makes options look absurdly overpriced.
        try:
            amc = ts.hour >= 12
        except Exception:
            amc = True
        if amc:
            react = next((x for x in days if x > d.isoformat()), None)
        else:
            react = d.isoformat() if d.isoformat() in idx else \
                next((x for x in days if x >= d.isoformat()), None)
        if not react or react not in idx or idx[react] == 0:
            continue
        i = idx[react]
        prev = closes[days[i - 1]]
        if not prev:
            continue
        mv = 100 * (closes[react] / prev - 1)
        out.append({"date": d.isoformat(), "move_pct": round(mv, 1),
                    "session": "AMC" if amc else "BMO"})
        if len(out) >= max_events:
            break
    if not out:
        return None
    moves = [x["move_pct"] for x in out]
    ups = sum(1 for m in moves if m > 0)
    return {
        "n": len(out),
        "avg_abs_move": round(sum(abs(m) for m in moves) / len(moves), 1),
        "max_abs_move": round(max(abs(m) for m in moves), 1),
        "up_rate": round(100 * ups / len(moves)),
        "beat_rate": round(100 * sum(beats) / len(beats)) if beats else None,
        "recent": out[:4],
    }

def fetch_earnings_preview(sym, spot, earnings_date, bars, risk=None, opts=None):
    """Street numbers + options-implied move + what to watch + a mechanical
    call on holding a long through the print. Only run for names reporting soon."""
    try:
        import yfinance as yf
        t = yf.Ticker(sym)
        pv = {"date": earnings_date}
        d_earn = datetime.date.fromisoformat(earnings_date)
        pv["days"] = (d_earn - datetime.date.today()).days

        # ---- street estimates (consensus + dispersion)
        try:
            ee = t.earnings_estimate
            if ee is not None and "0q" in ee.index:
                r = ee.loc["0q"]
                pv["street"] = {
                    "eps_avg": round(float(r["avg"]), 2),
                    "eps_low": round(float(r["low"]), 2),
                    "eps_high": round(float(r["high"]), 2),
                    "n_analysts": int(r["numberOfAnalysts"]) if r["numberOfAnalysts"] == r["numberOfAnalysts"] else None,
                    "yoy_growth_pct": round(float(r["growth"]) * 100, 1) if r["growth"] == r["growth"] else None,
                    "year_ago_eps": round(float(r["yearAgoEps"]), 2) if r["yearAgoEps"] == r["yearAgoEps"] else None,
                }
        except Exception:
            pass
        try:
            re_ = t.revenue_estimate
            if re_ is not None and "0q" in re_.index:
                r = re_.loc["0q"]
                pv["revenue"] = {
                    "avg_b": round(float(r["avg"]) / 1e9, 2),
                    "growth_pct": round(float(r["growth"]) * 100, 1) if r["growth"] == r["growth"] else None,
                }
        except Exception:
            pass
        # ---- estimate revisions: are analysts marking up or down into the print?
        try:
            tr = t.eps_trend
            if tr is not None and "0q" in tr.index:
                r = tr.loc["0q"]
                now, ago = float(r["current"]), float(r["30daysAgo"])
                if ago:
                    chg = 100 * (now - ago) / abs(ago)
                    pv["revisions"] = {
                        "eps_now": round(now, 3), "eps_30d_ago": round(ago, 3),
                        "chg_pct": round(chg, 1),
                        "direction": "UP" if chg > 0.5 else ("DOWN" if chg < -0.5 else "FLAT"),
                    }
        except Exception:
            pass

        # ---- options-implied move from the ATM straddle of the first post-earnings expiry
        try:
            exps = list(t.options)
            after = [e for e in exps if datetime.date.fromisoformat(e) >= d_earn]
            if after and spot:
                exp = after[0]
                oc = t.option_chain(exp)
                def _pick(df):
                    k = min(df["strike"], key=lambda x: abs(x - spot))
                    return df[df["strike"] == k].iloc[0], k
                c, ck = _pick(oc.calls)
                p, _ = _pick(oc.puts)
                def _mid(row):
                    b, a = _num(row.get("bid")), _num(row.get("ask"))
                    return (b + a) / 2 if (b > 0 and a > 0) else _num(row.get("lastPrice"))
                strad = _mid(c) + _mid(p)
                if strad > 0:
                    im = 100 * strad / spot
                    pv["implied"] = {
                        "move_pct": round(im, 1), "expiry": exp, "atm_strike": float(ck),
                        "up_to": round(spot * (1 + im / 100), 2),
                        "down_to": round(spot * (1 - im / 100), 2),
                    }
        except Exception:
            pass

        # ---- what the market actually did on past prints
        hm = _hist_earnings_moves(t, bars)
        if hm:
            pv["history"] = hm
            if pv.get("implied"):
                ratio = pv["implied"]["move_pct"] / hm["avg_abs_move"] if hm["avg_abs_move"] else None
                if ratio:
                    pv["implied_vs_realized"] = {
                        "ratio": round(ratio, 2),
                        "read": ("RICH — options pricing a bigger move than this name usually delivers"
                                 if ratio >= 1.25 else
                                 "CHEAP — options pricing less than this name usually delivers"
                                 if ratio <= 0.8 else
                                 "FAIR — implied roughly matches history"),
                    }

        # ---- does the position survive an average print?
        if risk and pv.get("implied") and risk.get("stop") and spot:
            stop_dist = 100 * (spot - risk["stop"]) / spot
            pv["stop_test"] = {
                "stop_distance_pct": round(stop_dist, 1),
                "implied_move_pct": pv["implied"]["move_pct"],
                "survives": pv["implied"]["move_pct"] < stop_dist,
                "note": (f"An average implied down-move takes price to "
                         f"${pv['implied']['down_to']} vs your stop ${risk['stop']} — "
                         + ("the stop holds." if pv["implied"]["move_pct"] < stop_dist
                            else "the stop is BREACHED by a typical print.")),
            }

        pv["watch"] = _preview_watchlist(pv, opts)
        pv["call"] = _preview_call(pv, opts)
        return pv
    except Exception as e:
        return {"error": str(e)[:120]}

def _preview_watchlist(pv, opts):
    """The specific things worth reading in the print, derived from the data."""
    w = []
    st, rv, rev = pv.get("street"), pv.get("revenue"), pv.get("revisions")
    if st:
        line = f"Consensus EPS ${st['eps_avg']}"
        if st.get("eps_low") is not None:
            line += f" (range ${st['eps_low']}–${st['eps_high']}"
            line += f", {st['n_analysts']} analysts)" if st.get("n_analysts") else ")"
        if st.get("yoy_growth_pct") is not None:
            line += f" — {st['yoy_growth_pct']:+}% YoY vs ${st.get('year_ago_eps')} a year ago"
        w.append(line + ".")
    if rv:
        w.append(f"Revenue consensus ${rv['avg_b']}B"
                 + (f" ({rv['growth_pct']:+}% YoY)." if rv.get("growth_pct") is not None else "."))
    if rev:
        if rev["direction"] == "UP":
            w.append(f"Estimates REVISED UP {rev['chg_pct']:+}% in 30d — the bar has been raised; "
                     f"an in-line print may disappoint.")
        elif rev["direction"] == "DOWN":
            w.append(f"Estimates CUT {rev['chg_pct']}% in 30d — a lowered bar is easier to clear, "
                     f"but the cut itself is the warning.")
        else:
            w.append("Estimates flat over 30d — no analyst repositioning into the print.")
    h = pv.get("history")
    if h:
        s = f"Past {h['n']} prints: average move ±{h['avg_abs_move']}% (max ±{h['max_abs_move']}%), higher {h['up_rate']}% of the time"
        if h.get("beat_rate") is not None:
            s += f", beat EPS {h['beat_rate']}% of the time"
        w.append(s + ".")
        if h.get("beat_rate") and h["beat_rate"] >= 75 and h["up_rate"] <= 50:
            w.append(f"Note: this name beats EPS {h['beat_rate']}% of the time yet still trades DOWN "
                     f"after {100 - h['up_rate']}% of prints — the number is not the trade; "
                     f"guidance and positioning are.")
    im = pv.get("implied")
    if im:
        w.append(f"Options price a ±{im['move_pct']}% move → roughly ${im['down_to']} to ${im['up_to']}.")
    ivr = pv.get("implied_vs_realized")
    if ivr:
        w.append(f"Implied/realized {ivr['ratio']}× — {ivr['read']}.")
    if opts and not opts.get("error"):
        if opts.get("skew_pts") is not None:
            w.append(f"Skew {opts['skew_pts']:+}pts into the print — "
                     + ("downside protection is bid." if opts["skew_pts"] > 8 else
                        "little fear priced." if opts["skew_pts"] < 3 else "normal."))
        if opts.get("call_wall"):
            w.append(f"Post-print magnets: call wall ${opts['call_wall']}, put wall ${opts['put_wall']}.")
    st_t = pv.get("stop_test")
    if st_t:
        w.append(st_t["note"])
    return w

def _preview_call(pv, opts):
    """Mechanical call on carrying a swing long THROUGH the print.
    Bias is deliberately conservative: our trend/RS edge says nothing about a
    binary event, so 'wait and trade the reaction' is the default."""
    score, pro, con = 0, [], []
    st_t = pv.get("stop_test")
    if st_t:
        if not st_t["survives"]:
            score -= 3
            con.append(f"an average print (±{st_t['implied_move_pct']}%) breaches your "
                       f"{st_t['stop_distance_pct']}% stop")
        else:
            score += 1
            pro.append(f"stop sits outside the implied move ({st_t['stop_distance_pct']}% vs "
                       f"±{st_t['implied_move_pct']}%)")
    rev = pv.get("revisions")
    if rev:
        if rev["direction"] == "UP": score += 1; pro.append(f"estimates revised up {rev['chg_pct']:+}% in 30d")
        elif rev["direction"] == "DOWN": score -= 1; con.append(f"estimates cut {rev['chg_pct']}% in 30d")
    h = pv.get("history")
    if h and h.get("up_rate") is not None:
        # For a long, how the stock TRADES matters more than whether it beats —
        # plenty of names beat and still sell off on guidance.
        if h["up_rate"] >= 65:
            score += 1
            pro.append(f"trades up after {h['up_rate']}% of the last {h['n']} prints"
                       + (f" (beats {h['beat_rate']}%)" if h.get("beat_rate") is not None else ""))
        elif h["up_rate"] <= 35:
            score -= 1
            con.append(f"trades down after {100-h['up_rate']}% of the last {h['n']} prints")
    ivr = pv.get("implied_vs_realized")
    if ivr and ivr["ratio"] >= 1.25:
        score -= 1; con.append(f"options rich at {ivr['ratio']}× typical move (premium crush after)")
    if opts and not opts.get("error"):
        ft = (opts.get("flow_tilt") or {}).get("read")
        if ft == "BULLISH": score += 1; pro.append("premium flow leaning bullish")
        elif ft == "BEARISH": score -= 1; con.append("premium flow leaning bearish")
        if opts.get("skew_pts") is not None and opts["skew_pts"] > 8:
            score -= 1; con.append("steep put skew — hedging bid into the print")
    if h and h.get("n", 0) < 5:
        con.append(f"only {h['n']} past prints in the sample — history is indicative, not reliable")
    verdict = ("AVOID THROUGH PRINT" if score <= -2 else
               "HALF SIZE AT MOST" if score <= 0 else
               "ACCEPTABLE — still binary")
    # Language is grounded in backtest_earnings.py (585 prints, 30 large caps, 5y):
    # holding through earned a HIGHER mean (+1.66% vs +1.15% over 10 sessions) but with
    # ~50% more variance and a far worse tail (5th pct -11.6% vs -7.1%). So the case for
    # waiting is risk-adjusted, not return-maximising — and must be stated that way.
    action = ("Wait for the print, then trade the reaction. Across 585 past prints, waiting cut "
              "the 5th-percentile loss from -11.6% to -7.1% — you give up some average return "
              "for a materially smaller tail, and a gap through a stop is unmanageable."
              if score <= -2 else
              "If you want exposure, take a partial position now and add after the print confirms — "
              "that keeps some upside while halving the gap risk."
              if score <= 0 else
              "Conditions are as favourable as they get, but a print is still binary and our trend "
              "and fundamental edge does not predict it. Historically, holding through earned a "
              "slightly higher average with roughly 50% more variance — size for a gap through "
              "your stop, not for the average outcome.")
    return {"verdict": verdict, "score": score, "pro": pro, "con": con, "action": action}

# ------------------------------------------------------------ fundamentals

def _pctf(x, d=1):
    return None if x is None else round(float(x) * 100, d)

def fetch_fa(sym):
    """Essential fundamentals for a long thesis: growth (+acceleration), margins,
    valuation vs growth, balance sheet, analyst view. Cached per day.
    Deliberately thin — this supports or rejects the trade, it isn't a model."""
    try:
        import yfinance as yf
        t = yf.Ticker(sym)
        i = t.info or {}
        fa = {
            "name": i.get("shortName"), "industry": i.get("industry"),
            "mcap_b": round((i.get("marketCap") or 0) / 1e9, 1) or None,
            "rev_growth_yoy": _pctf(i.get("revenueGrowth")),
            "eps_growth_yoy": _pctf(i.get("earningsGrowth") if i.get("earningsGrowth") is not None
                                    else i.get("earningsQuarterlyGrowth")),
            "gross_margin": _pctf(i.get("grossMargins")),
            "op_margin": _pctf(i.get("operatingMargins")),
            "net_margin": _pctf(i.get("profitMargins")),
            "roe": _pctf(i.get("returnOnEquity")),
            "fwd_pe": round(i.get("forwardPE"), 1) if i.get("forwardPE") else None,
            "trail_pe": round(i.get("trailingPE"), 1) if i.get("trailingPE") else None,
            "peg": round(i.get("pegRatio"), 2) if i.get("pegRatio") else None,
            "ps": round(i.get("priceToSalesTrailing12Months"), 1) if i.get("priceToSalesTrailing12Months") else None,
            "fcf_b": round((i.get("freeCashflow") or 0) / 1e9, 2) or None,
            "analysts": i.get("numberOfAnalystOpinions"),
            "rating": i.get("recommendationKey"),
            "target": round(i.get("targetMeanPrice"), 2) if i.get("targetMeanPrice") else None,
        }
        cash, debt = i.get("totalCash") or 0, i.get("totalDebt") or 0
        fa["net_cash_b"] = round((cash - debt) / 1e9, 2)
        fa["de"] = round(i.get("debtToEquity"), 0) if i.get("debtToEquity") else None
        # revenue acceleration: latest YoY vs prior-quarter YoY (needs 5+ quarters)
        try:
            q = t.quarterly_income_stmt
            rev = [float(x) for x in q.loc["Total Revenue"].tolist() if x == x]
            if len(rev) >= 5:
                yoy_now = 100 * (rev[0] / rev[4] - 1)
                fa["rev_yoy_q"] = round(yoy_now, 1)
                if len(rev) >= 6 and rev[5] == rev[5]:
                    yoy_prev = 100 * (rev[1] / rev[5] - 1)
                    fa["rev_accel_pts"] = round(yoy_now - yoy_prev, 1)
                    fa["accel_basis"] = "vs prior quarter's YoY"
                else:
                    # only 5 quarters available: benchmark the latest quarter's
                    # YoY against the most recent full-year growth rate
                    try:
                        ar = [float(x) for x in t.income_stmt.loc["Total Revenue"].tolist() if x == x]
                        if len(ar) >= 2 and ar[1]:
                            fy = 100 * (ar[0] / ar[1] - 1)
                            fa["rev_fy_growth"] = round(fy, 1)
                            fa["rev_accel_pts"] = round(yoy_now - fy, 1)
                            fa["accel_basis"] = f"latest quarter vs FY trend ({round(fy,1)}%)"
                    except Exception:
                        pass
            if "Operating Income" in q.index and len(rev) >= 5:
                oi = [float(x) for x in q.loc["Operating Income"].tolist() if x == x]
                if len(oi) >= 5:
                    fa["op_margin_now"] = round(100 * oi[0] / rev[0], 1)
                    fa["op_margin_yoy_pts"] = round(100 * oi[0] / rev[0] - 100 * oi[4] / rev[4], 1)
        except Exception:
            pass
        return fa
    except Exception as e:
        return {"error": str(e)[:100]}

def fa_verdict(fa, close=None):
    """SUPPORTS / NEUTRAL / REJECTS the long, with the reasons that drove it."""
    if not fa or fa.get("error"):
        return None
    s, pro, con = 0, [], []
    g = fa.get("rev_yoy_q") if fa.get("rev_yoy_q") is not None else fa.get("rev_growth_yoy")
    if g is not None:
        if g >= 20: s += 2; pro.append(f"revenue +{g}% YoY")
        elif g >= 8: s += 1; pro.append(f"revenue +{g}% YoY")
        elif g < 0: s -= 2; con.append(f"revenue {g}% YoY — shrinking")
        else: con.append(f"revenue only +{g}% YoY")
    a, basis = fa.get("rev_accel_pts"), fa.get("accel_basis", "")
    if a is not None:
        if a >= 2: s += 2; pro.append(f"growth ACCELERATING (+{a}pts, {basis})")
        elif a <= -3: s -= 2; con.append(f"growth DECELERATING ({a}pts, {basis})")
    e = fa.get("eps_growth_yoy")
    if e is not None:
        if e >= 20: s += 1; pro.append(f"EPS +{e}% YoY")
        elif e < -10: s -= 1; con.append(f"EPS {e}% YoY")
    om = fa.get("op_margin_yoy_pts")
    if om is not None:
        if om >= 1: s += 1; pro.append(f"operating margin expanding (+{om}pts YoY)")
        elif om <= -2: s -= 1; con.append(f"operating margin compressing ({om}pts YoY)")
    elif fa.get("op_margin") is not None and fa["op_margin"] < 0:
        s -= 1; con.append("unprofitable at the operating line")
    peg, fpe = fa.get("peg"), fa.get("fwd_pe")
    if peg is not None:
        if peg <= 1.5: s += 1; pro.append(f"PEG {peg} — growth reasonably priced")
        elif peg >= 3: s -= 1; con.append(f"PEG {peg} — paying up for the growth")
    elif fpe is not None and g is not None:
        if fpe > 60 and g < 20: s -= 1; con.append(f"forward P/E {fpe} on only {g}% growth")
    if fa.get("net_cash_b") is not None:
        if fa["net_cash_b"] > 0: s += 1; pro.append(f"net cash ${fa['net_cash_b']}B")
        elif fa.get("de") and fa["de"] > 200: s -= 1; con.append(f"debt/equity {fa['de']}")
    if fa.get("fcf_b") is not None and fa["fcf_b"] < 0:
        s -= 1; con.append("negative free cash flow")
    if close and fa.get("target"):
        up = round(100 * (fa["target"] / close - 1), 1)
        fa["target_upside_pct"] = up
        if up <= -5: s -= 1; con.append(f"trading {abs(up)}% ABOVE consensus target ${fa['target']}")
        elif up >= 15: s += 1; pro.append(f"{up}% below consensus target ${fa['target']}")
    v = "SUPPORTS LONG" if s >= 3 else ("REJECTS LONG" if s <= -2 else "NEUTRAL")
    return {"verdict": v, "score": s, "pro": pro, "con": con}

# --------------------------------------------------------- market environment

def market_env(hist, rotation, hy_oas=None):
    """VIX basket, oil complex, HY OAS (credit veto MK07), short-ETF gauge."""
    def closes(s): return [b["c"] for b in hist[s]] if s in hist and hist[s] else None
    env = {}
    if hy_oas:
        env["hy_oas"] = hy_oas
    vix, v9, v3 = closes("^VIX"), closes("^VIX9D"), closes("^VIX3M")
    if vix:
        lvl = vix[-1]
        zone = ("COMPLACENT" if lvl < 14 else "NORMAL" if lvl < 20 else
                "CAUTION" if lvl < 25 else "STRESS" if lvl < 30 else "CRISIS")
        r3 = round(lvl / v3[-1], 3) if v3 else None
        r9 = round(v9[-1] / lvl, 3) if v9 else None
        yr = vix[-252:]
        pctile = round(100 * sum(1 for x in yr if x <= lvl) / len(yr))
        parts = [f"VIX {round(lvl,1)} — {zone} ({pctile}th %ile of the last year)."]
        if r3 is not None:
            parts.append("Term structure in CONTANGO (VIX below VIX3M) — normal, longs OK." if r3 < 0.95 else
                         "Term structure FLAT — market starting to pay up for near protection." if r3 <= 1.0 else
                         "BACKWARDATION (VIX above VIX3M) — acute risk-off; halt new longs until it normalizes.")
        if r9 is not None and r9 > 1.02:
            parts.append("VIX9D above VIX: an event premium is priced into THIS week — check the macro calendar.")
        if zone == "COMPLACENT":
            parts.append("Very low vol cuts both ways: great trend tape, but shocks land harder — keep stops honest.")
        elif zone in ("STRESS", "CRISIS"):
            parts.append("MK05 veto: reduce gross, no breakout chasing; only A+ setups with tight risk.")
        env["vix"] = {"level": round(lvl, 1), "vix9d": round(v9[-1], 1) if v9 else None,
                      "vix3m": round(v3[-1], 1) if v3 else None, "ratio_3m": r3, "ratio_9d": r9,
                      "zone": zone, "pctile_1y": pctile,
                      "spark": [round(x, 1) for x in vix[-60:]], "comment": " ".join(parts)}
    cl = closes("CL=F")
    if cl:
        wti = cl[-1]
        ret_1m = round((wti / cl[-21] - 1) * 100, 1) if len(cl) >= 21 else None
        s50 = sma(cl, 50)
        quads = {r["etf"]: r["quad"] for r in rotation["daily"] if r["etf"] in ("XLE", "XOP", "GDX", "XME")}
        parts = [f"WTI ${round(wti,2)} ({'+' if ret_1m and ret_1m>0 else ''}{ret_1m}% 1m, "
                 f"{'above' if s50 and wti>s50 else 'below'} its 50d)."]
        if quads:
            lead = [k for k, v in quads.items() if v in ("leading", "improving")]
            parts.append(f"Energy rotation: {', '.join(k+'='+v for k,v in quads.items())}.")
            if ret_1m is not None and ret_1m > 8 and lead:
                parts.append("Oil momentum + energy leadership = inflation-pressure tape; watch rate-sensitive longs and CPI dates.")
            elif ret_1m is not None and ret_1m < -8:
                parts.append("Oil breaking down — disinflation tailwind for growth longs, headwind for our energy names.")
        env["oil"] = {"wti": round(wti, 2), "ret_1m_pct": ret_1m,
                      "above_50d": bool(s50 and wti > s50),
                      "spark": [round(x, 2) for x in cl[-60:]],
                      "energy_quads": quads, "comment": " ".join(parts),
                      "flow_note": "USO option flow: see FLOW tab (scanned daily with the focus names)."}
    rows = []
    gauge_vals = []
    for s, name in SHORT_ETFS.items():
        if s not in hist or len(hist[s]) < 60: continue
        b = hist[s]
        c = [x["c"] for x in b]; v = [x["v"] for x in b]
        v50 = sum(v[-50:]) / 50
        rvol = round(v[-1] / v50, 2) if v50 else None
        dv5 = sum(x["c"] * x["v"] for x in b[-5:]) / 5
        dv50 = sum(x["c"] * x["v"] for x in b[-50:]) / 50
        surge = round(dv5 / dv50, 2) if dv50 else None
        s20v = sma(c, 20)
        rows.append({"etf": s, "name": name, "close": round(c[-1], 2),
                     "chg_1d_pct": round((c[-1] / c[-2] - 1) * 100, 2),
                     "rvol": rvol, "dollar_vol_surge_5d": surge,
                     "above_20d": bool(s20v and c[-1] > s20v)})
        if s not in ("UVXY", "VXX") and surge:
            gauge_vals.append(surge)
    if rows:
        gauge = round(sum(gauge_vals) / len(gauge_vals), 2) if gauge_vals else None
        zone = (None if gauge is None else
                "LOW" if gauge < 0.9 else "NORMAL" if gauge < 1.3 else
                "ELEVATED" if gauge < 2.0 else "PANIC")
        cmt = {"LOW": "Little hedging demand — nobody is paying for protection; fine for longs, but complacency builds tops.",
               "NORMAL": "Hedging demand normal — no signal either way.",
               "ELEVATED": "Inverse-ETF dollar volume running hot — institutions are hedging. Distribution risk: tighten stops on extended longs.",
               "PANIC": "Hedging at panic levels — historically closer to a low than a top (contrarian), but wait for the reversal day, don't guess."}.get(zone, "")
        pos_above = sum(1 for r in rows if r["above_20d"] and r["etf"] not in ("UVXY", "VXX"))
        if pos_above >= 4:
            cmt += f" {pos_above}/6 index inverses are above their own 20d — the hedges are WORKING, i.e. the market is weakening."
        env["short_etfs"] = {"rows": rows, "gauge_5d_dollar_vol": gauge, "zone": zone, "comment": cmt}
    return env

def chart_pack(bars, n=CHART_BARS):
    return [[b["d"], round(b["o"], 2), round(b["h"], 2), round(b["l"], 2),
             round(b["c"], 2), int(b["v"])] for b in bars[-n:]]

# ------------------------------------------------------------------- runner

def read_list(fn):
    out = []
    p = os.path.join(ROOT, fn)
    if not os.path.exists(p): return out
    for line in open(p, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#"):
            parts = line.split()
            out.append((parts[0].upper(), float(parts[1]) if len(parts) > 1 else None))
    return out

def read_macro():
    out = []
    p = os.path.join(ROOT, "macro.txt")
    if not os.path.exists(p): return out
    for line in open(p, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "|" in line:
            d, label = line.split("|", 1)
            out.append({"date": d.strip(), "label": label.strip()})
    return out

def fetch_all_history(symbols):
    hist, errors = {}, {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(fetch_history, s): s for s in symbols}
        for f in as_completed(futs):
            s = futs[f]
            try: hist[s] = f.result()
            except Exception as e: errors[s] = str(e)[:100]
    return hist, errors

def rotation_for_sector(rotation, sector):
    if not sector: return None
    etf = SECTOR_ETF.get(sector)
    if not etf: return None
    row = next((r for r in rotation["daily"] if r["etf"] == etf), None)
    if not row: return None
    wrow = next((r for r in rotation["weekly"] if r["etf"] == etf), None)
    return {"etf": etf, "name": row["name"], "quad": row["quad"], "rank": row["rank"],
            "score": row["score"], "weekly_quad": wrow["quad"] if wrow else None}

def cmd_scan():
    os.makedirs(DATA, exist_ok=True)
    t0 = time.time()
    manual = [t for t, _ in read_list("universe.txt")]
    watch = read_list("watchlist.txt")
    sector_map = {}
    auto = []
    if AUTO_UNIVERSE:
        try:
            auto, sector_map = build_universe()
        except Exception as e:
            print(f"! screener failed ({e}); falling back to universe.txt only")
    universe = sorted(set(auto) | set(manual) | {t for t, _ in watch})
    allsyms = sorted(set(universe) | set(ROTATION_ETFS) | set(SHORT_ETFS)
                     | set(MARKET_EXTRA) | {"SPY", "QQQ", "^VIX"})
    print(f"Universe {len(universe)} names (auto {len(auto)} + manual {len(manual)}); fetching {len(allsyms)} histories...")
    hist, errors = fetch_all_history(allsyms)
    print(f"  ok={len(hist)} failed={len(errors)}")
    spy = hist.get("SPY")
    if not spy: print("FATAL: no SPY"); sys.exit(1)
    spy_closes = [b["c"] for b in spy]

    print("Earnings calendar (forward + recent actuals)...")
    emap, week, recent_map = fetch_earnings_map()
    print("Rotation (RRG daily + weekly)...")
    rotation = build_rotation(hist)

    # market context
    spy_s200 = sma(spy_closes, 200); spy_s200p = sma(spy_closes, 200, len(spy_closes) - 21)
    vix = hist.get("^VIX"); vix_last = vix[-1]["c"] if vix else None
    results = {}
    for s in universe:
        if s not in hist: continue
        try:
            results[s] = analyze(s, hist[s], spy_closes, sector_map.get(s), emap.get(s),
                                 recent_map.get(s))
        except Exception as e:
            errors[s] = f"analyze: {e}"
    scanned = [r for r in results.values() if r.get("status") != "INSUFFICIENT_HISTORY"]
    above200 = [r for r in scanned if r["gates"].get("RG01_above_200d")]
    breadth = round(len(above200) / len(scanned) * 100, 1) if scanned else 0
    regime_on = spy_closes[-1] > spy_s200 and spy_s200 > spy_s200p
    qqq = hist.get("QQQ"); qqq_closes = [b["c"] for b in qqq] if qqq else []
    print("HY OAS (FRED credit-spread veto)...")
    hy_oas = fetch_hy_oas()
    if hy_oas is None:
        print("  (no FRED key or fetch failed — risk-off tier will use VIX only)")
    risk_off = compute_risk_off(vix_last, hy_oas)
    if risk_off["tier"] != "NORMAL":
        print(f"  {risk_off['banner']}")
    regime_edge = current_regime_edge(
        vix_last, hy_oas, spy_closes[-1] > spy_s200, spy_s200 > spy_s200p)
    if regime_edge:
        print(f"Regime edge: {regime_edge['bucket_label']} -> "
              f"{regime_edge['edge_mult']}x baseline "
              f"({regime_edge['mean_x21_pct']:+.2f}% mean fwd 21d, "
              f"n={regime_edge['n']}, source={regime_edge['source']})")
    else:
        print("Regime lookup unavailable — run phaseC_regime_builder.py first.")
    market = {
        "spy_close": round(spy_closes[-1], 2),
        "spy_vs_200d_pct": round((spy_closes[-1] / spy_s200 - 1) * 100, 1),
        "spy_200d_rising": spy_s200 > spy_s200p,
        "qqq_vs_200d_pct": round((qqq_closes[-1] / sma(qqq_closes, 200) - 1) * 100, 1) if len(qqq_closes) >= 200 else None,
        "vix": round(vix_last, 1) if vix_last else None, "vix_elevated": (vix_last or 0) > 25,
        "universe_breadth_above_200d_pct": breadth,
        "regime": "RISK_ON" if (regime_on and (vix_last or 0) < 28 and breadth > 40) else
                  ("CAUTION" if regime_on else "RISK_OFF"),
        "risk_off": risk_off,
        "hy_oas": hy_oas,
        "regime_edge": regime_edge,
    }

    # RS percentile within today's universe (RS02)
    rets = sorted(r["rs"]["ret_12_1"] for r in scanned if "rs" in r)
    for r in scanned:
        if "rs" in r:
            r["rs"]["pctile"] = round(100 * sum(1 for x in rets if x <= r["rs"]["ret_12_1"]) / len(rets))
            r["checklist"]["items"]["TOP RS"] = r["rs"]["pctile"] >= 70
            r["checklist"]["score"] = sum(r["checklist"]["items"].values())
            cs = r["checklist"]["score"]
            r["checklist"]["tag"] = ("MOMENTUM BUILDING" if cs >= 8 else "WATCH" if cs >= 6 else
                                     "TRANSITION" if cs >= 4 else "WEAK")

    actionable = ("BASE_READY", "BREAKOUT", "MOMENTUM", "BASE_BUILDING", "PULLBACK", "POST_EVENT_DRIFT")
    # Rank on quality, but never let an untradable ticket lead the desk: a name
    # whose reward:risk at today's price is under 1R is demoted below everything
    # takeable, regardless of how good the chart looks.
    def _rr_tier(r):
        f = (r.get("risk") or {}).get("rr_flag")
        return 2 if f == "POOR" else (1 if f == "BELOW_SPEC" else 0)
    prelim = sorted((r for r in scanned if r.get("gates_passed") and r.get("setup") in actionable),
                    key=lambda r: (_rr_tier(r), -r["scores"]["composite"], actionable.index(r["setup"])))
    # Sector cap: from Phase A walk-forward, Q5 concentrates into whichever
    # sector rallied that quarter (sector-neutral variant gave ~2x Sharpe).
    # Mark the 3rd+ name per sector as sector_capped and re-sort so capped
    # names fall below un-capped equivalents. Keeps the primary ranking
    # unchanged; top-N stops being "top 5 semis" by accident.
    sec_counts = {}
    for r in prelim:
        sec = r.get("sector") or "Unknown"
        sec_counts[sec] = sec_counts.get(sec, 0) + 1
        rk = r.get("risk")
        if rk is not None:                          # don't fabricate a risk dict
            rk["sector_capped"] = sec_counts[sec] > SECTOR_MAX_POSITIONS
    # Names without a risk block sort as sector_capped=False (they can't be
    # sized anyway); real ranking uses the flag when present.
    ranked = sorted(prelim,
                    key=lambda r: ((r.get("risk") or {}).get("sector_capped", False),
                                   _rr_tier(r), -r["scores"]["composite"],
                                   actionable.index(r["setup"])))
    top3 = [r["ticker"] for r in ranked[:3]]
    # PEAD names (post-earnings-announcement drift setups) get their own UI
    # section — trade rules differ (short horizon, tight stops, no chasing).
    # Ranked by composite because the hand-rolled PEAD sub-scorer failed its
    # backtest (see backtest_pead.py) — composite orders these names better.
    # Dedupe against top3 so a PEAD name already in the focus row doesn't
    # render twice (day-2 audit UI-nit).
    _top3_set = set(top3)
    pead_ranked = [r["ticker"] for r in ranked
                   if r.get("setup") == "POST_EVENT_DRIFT" and r["ticker"] not in _top3_set][:6]

    # Portfolio-level risk-off: halve/zero every result's sizing via the
    # shared helper. serve.lookup uses the same helper so searched names get
    # the same treatment (pre-prod review CRIT-C1).
    if risk_off["size_mult"] < 1.0:
        for r in results.values():
            apply_risk_off_to_result(r, risk_off)

    # options full (walls, GEX ladder, flow) for top ranked + watchlist + USO
    focus = list(dict.fromkeys([r["ticker"] for r in ranked[:8]] + [t for t, _ in watch]))[:OPTIONS_DEPTH]
    print(f"Options (walls/GEX ladder/flow) for {focus} + USO...")
    optmap = {}
    for s in focus:
        if s in results and "risk" in results[s]:
            optmap[s] = fetch_options_full(s, results[s]["close"])
    if "USO" in hist:
        optmap["USO"] = fetch_options_full("USO", hist["USO"][-1]["c"])
    print("Market environment (VIX/oil/HY-OAS/short-ETFs)...")
    env = market_env(hist, rotation, hy_oas=hy_oas)
    print(f"Fundamentals for {len(focus)} focus names...")
    fa_cache_p = os.path.join(DATA, "fa_cache.json")
    today_iso = datetime.date.today().isoformat()
    fa_cache = {}
    if os.path.exists(fa_cache_p):
        try:
            c = json.load(open(fa_cache_p, encoding="utf-8"))
            if c.get("date") == today_iso: fa_cache = c.get("fa", {})
        except Exception: pass
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(fetch_fa, s): s for s in focus if s not in fa_cache}
        for f in as_completed(futs):
            try: fa_cache[futs[f]] = f.result()
            except Exception as e: fa_cache[futs[f]] = {"error": str(e)[:80]}
    json.dump({"date": today_iso, "fa": fa_cache}, open(fa_cache_p, "w", encoding="utf-8"))
    for s, fa in fa_cache.items():
        if s in results:
            results[s]["fa"] = fa
            results[s]["fa_verdict"] = fa_verdict(fa, results[s].get("close"))

    # ---- earnings preview for focus names reporting inside the alert window
    pv_names = [s for s in focus
                if s in results and (results[s].get("earnings") or {}).get("alert_upcoming")]
    if pv_names:
        print(f"Earnings previews (street numbers + implied move) for {pv_names}...")
        for s in pv_names:
            r = results[s]
            r["earnings"]["preview"] = fetch_earnings_preview(
                s, r["close"], r["earnings"]["date"], hist[s], r.get("risk"), optmap.get(s))
    # thesis comments + trade plans for everyone (cheap, rule-based)
    for s, r in results.items():
        rot = rotation_for_sector(rotation, r.get("sector"))
        r["rotation"] = rot
        r["thesis"] = thesis_comment(r, rot, optmap.get(s))
        r["plan"] = trade_plan(r, optmap.get(s))
    watch_report = []
    for t, px in watch:
        if t in results:
            wr = watchlist_health(t, results[t], px)
            wr["thesis"] = results[t].get("thesis")
        else:
            wr = {"ticker": t, "verdict": "NO_DATA", "reasons": [errors.get(t, "fetch failed")]}
        watch_report.append(wr)

    # cross-universe signal event groups
    groups = {}
    for r in scanned:
        for evname, ago in (r.get("events") or {}).items():
            groups.setdefault(evname, []).append({"t": r["ticker"], "ago": ago})
    for v in groups.values(): v.sort(key=lambda x: x["ago"])

    # --- data freshness: is the last bar a completed session or still forming?
    # Timezone-independent heuristic: a live intraday bar carries only part of a
    # normal day's volume. Matters because RVOL, breakout confirmation and the
    # volume checklist items are all unreliable on a partial bar.
    _sv = sorted(b["v"] for b in spy[-21:-1])
    _med_v = _sv[len(_sv) // 2] if _sv else 0
    _last = spy[-1]
    _today = datetime.date.today().isoformat()
    _is_today = _last["d"] == _today
    _vol_pct = round(100 * _last["v"] / _med_v) if _med_v else None
    _partial = bool(_is_today and _vol_pct is not None and _vol_pct < 70)
    _lag_days = (datetime.date.today() - datetime.date.fromisoformat(_last["d"])).days
    freshness = {
        "last_bar": _last["d"], "is_today": _is_today, "lag_days": _lag_days,
        "bar_volume_pct_of_median": _vol_pct, "partial_bar": _partial,
        "state": ("LIVE_PARTIAL" if _partial else "TODAY_COMPLETE" if _is_today else "PRIOR_CLOSE"),
        "note": ("Today's bar is still forming — price is live (Yahoo delays ~15min) but volume is "
                 "incomplete, so RVOL, breakout confirmation and volume checklist items understate. "
                 "Re-run after the close for the authoritative daily read."
                 if _partial else
                 "Today's session is complete — this is a final daily bar." if _is_today else
                 f"Latest completed session: {_last['d']}. Markets closed since then."),
    }
    charts = {s: chart_pack(hist[s]) for s in universe if s in hist}
    charts["SPY"] = chart_pack(hist["SPY"])

    # --- diff vs previous scan: what changed between yesterday's ranking and today's?
    # Written to report["changes"] so the dashboard can render a "SINCE LAST SCAN" strip.
    changes = None
    prev_p = os.path.join(DATA, "report.json")
    if os.path.exists(prev_p):
        try:
            prev = json.load(open(prev_p, encoding="utf-8"))
            prev_ranked = prev.get("ranked") or []
            prev_top3 = prev.get("top3") or []
            prev_res = prev.get("results") or {}
            new_ranked = [r["ticker"] for r in ranked[:25]]
            new_set, prev_set = set(new_ranked), set(prev_ranked)
            new_top3_set, prev_top3_set = set(top3), set(prev_top3)
            setup_flips, rr_flips, gate_breaks = [], [], []
            for t in new_set & prev_set:
                pr, nr = prev_res.get(t) or {}, results.get(t) or {}
                ps, ns = pr.get("setup"), nr.get("setup")
                if ps and ns and ps != ns:
                    setup_flips.append({"t": t, "prev": ps, "now": ns})
                pf = (pr.get("risk") or {}).get("rr_flag")
                nf = (nr.get("risk") or {}).get("rr_flag")
                if pf != nf and (pf == "OK" or nf == "OK" or nf == "POOR" or pf == "POOR"):
                    rr_flips.append({"t": t, "prev": pf or "—", "now": nf or "—"})
            # names that were ranked before but dropped out — flag if they now fail a gate
            for t in prev_set - new_set:
                nr = results.get(t) or {}
                if nr and not nr.get("gates_passed"):
                    gate_breaks.append({"t": t, "setup": nr.get("setup")})
            changes = {
                "prev_as_of": prev.get("as_of"),
                "top3_added": sorted(new_top3_set - prev_top3_set),
                "top3_removed": sorted(prev_top3_set - new_top3_set),
                "ranked_added": sorted(new_set - prev_set),
                "ranked_removed": sorted(prev_set - new_set),
                "setup_flips": setup_flips,
                "rr_flips": rr_flips,
                "gate_breaks": gate_breaks,
            }
        except Exception as e:
            changes = {"error": str(e)[:120]}

    report = {
        "as_of": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "market": market, "rotation": rotation, "market_env": env, "freshness": freshness,
        "top3": top3, "ranked": [r["ticker"] for r in ranked[:25]],
        "pead_ranked": pead_ranked,
        "changes": changes,
        "results": results, "options": optmap, "charts": charts,
        "watchlist": watch_report, "earnings_week": week,
        "signal_groups": groups, "macro": read_macro(),
        "universe_size": len(universe), "auto_universe": bool(auto),
        "errors": {k: v for k, v in errors.items() if k in universe},
        "params": {"account": ACCOUNT_SIZE, "risk_budget_pct": RISK_BUDGET_PCT,
                   "earnings_blackout_days": EARNINGS_BLACKOUT_DAYS},
    }
    # Atomic write (pre-prod wide-audit HIGH-P3): a serve.py lookup happening
    # mid-write would previously read a partially-flushed report.json and
    # crash. Write to .tmp then os.replace so readers always see a complete
    # file. serve.py's persist path already uses this pattern.
    _rp = os.path.join(DATA, "report.json")
    _rp_tmp = _rp + ".tmp"
    with open(_rp_tmp, "w", encoding="utf-8") as _f:
        json.dump(report, _f, indent=None)
    os.replace(_rp_tmp, _rp)
    print(f"Data: last bar {freshness['last_bar']} [{freshness['state']}]"
          + (f" — volume only {freshness['bar_volume_pct_of_median']}% of normal, bar still forming"
             if freshness["partial_bar"] else ""))
    print(f"Top 3: {top3} | regime={market['regime']} breadth={breadth}% | {round(time.time()-t0)}s")
    print("Wrote data/report.json")

def cmd_lookup(tickers):
    """Ad-hoc lookup: analyze arbitrary tickers and merge into today's report."""
    p = os.path.join(DATA, "report.json")
    report = json.load(open(p, encoding="utf-8")) if os.path.exists(p) else None
    if not report:
        print("Run `python engine.py scan` first."); sys.exit(1)
    hist, errors = fetch_all_history(list(tickers) + ["SPY"])
    spy_closes = [b["c"] for b in hist["SPY"]]
    emap, _, recent_map = fetch_earnings_map()
    # Same risk-off state cmd_scan wrote; applied to each looked-up name so
    # CLI lookups halve consistently with the batch scan (pre-prod wide-audit
    # HIGH-P5; parallel to the serve.lookup fix for CRIT-C1).
    risk_off = (report.get("market") or {}).get("risk_off")
    for s in tickers:
        s = s.upper()
        if s not in hist:
            print(f"! no data for {s}"); continue
        r = analyze(s, hist[s], spy_closes, None, emap.get(s), recent_map.get(s))
        opts = fetch_options_full(s, r["close"]) if "risk" in r else None
        report.setdefault("charts", {})[s] = chart_pack(hist[s])
        rot = rotation_for_sector(report["rotation"], r.get("sector"))
        r["rotation"] = rot
        if risk_off:
            apply_risk_off_to_result(r, risk_off)
        r["thesis"] = thesis_comment(r, rot, opts)
        r["plan"] = trade_plan(r, opts)
        fa = fetch_fa(s)
        r["fa"] = fa
        r["fa_verdict"] = fa_verdict(fa, r.get("close"))
        if (r.get("earnings") or {}).get("alert_upcoming"):
            r["earnings"]["preview"] = fetch_earnings_preview(
                s, r["close"], r["earnings"]["date"], hist[s], r.get("risk"), opts)
        r["adhoc"] = True
        report["results"][s] = r
        if opts: report["options"][s] = opts
        print(f"{s}: {r.get('setup')} | composite {r.get('scores',{}).get('composite')} | {r['thesis']['verdict']}")
    # Atomic write (pre-prod re-audit HIGH-P3b — same class as P3 in cmd_scan,
    # missed on the first pass). serve.py concurrent read can't see a
    # partially-flushed report.
    _tmp = p + ".tmp"
    with open(_tmp, "w", encoding="utf-8") as _f:
        json.dump(report, _f)
    os.replace(_tmp, p)
    cmd_html()

def cmd_html():
    report = json.load(open(os.path.join(DATA, "report.json"), encoding="utf-8"))
    enrich_p = os.path.join(DATA, "ibkr_enrich.json")
    enrich = json.load(open(enrich_p, encoding="utf-8")) if os.path.exists(enrich_p) else {}
    charts = report.get("charts") or {}
    slim = {k: v for k, v in report.items() if k != "charts"}
    tpl = open(os.path.join(ROOT, "dashboard_template.html"), encoding="utf-8").read()
    html = tpl.replace("/*__REPORT__*/null", json.dumps(slim)) \
              .replace("/*__ENRICH__*/null", json.dumps(enrich))
    out = os.path.join(ROOT, "dashboard.html")
    open(out, "w", encoding="utf-8").write(html)
    print(f"Wrote {out}")
    ctpl_p = os.path.join(ROOT, "chart_template.html")
    if os.path.exists(ctpl_p):
        ctpl = open(ctpl_p, encoding="utf-8").read()
        chtml = ctpl.replace("/*__REPORT__*/null", json.dumps(slim)) \
                    .replace("/*__CHARTS__*/null", json.dumps(charts))
        cout = os.path.join(ROOT, "chart.html")
        open(cout, "w", encoding="utf-8").write(chtml)
        print(f"Wrote {cout}")

def cmd_watch(args):
    """watch add TICKER [entry] | watch remove TICKER — edits watchlist.txt."""
    p = os.path.join(ROOT, "watchlist.txt")
    lines = open(p, encoding="utf-8").read().splitlines() if os.path.exists(p) else []
    if not args or args[0] not in ("add", "remove"):
        print("usage: engine.py watch add TICKER [entry] | watch remove TICKER"); return
    action = args[0]
    sym = args[1].upper() if len(args) > 1 else None
    if not sym: print("ticker required"); return
    existing = {l.split()[0].upper(): l for l in lines if l.strip() and not l.startswith("#")}
    if action == "add":
        if sym in existing: print(f"{sym} already on the watchlist"); return
        entry = f" {args[2]}" if len(args) > 2 else ""
        lines.append(f"{sym}{entry}")
        print(f"Added {sym}. Re-run `python engine.py all` to include it in health checks + options.")
    else:
        if sym not in existing: print(f"{sym} not on the watchlist"); return
        lines = [l for l in lines if l.strip() != existing[sym].strip()]
        print(f"Removed {sym}.")
    open(p, "w", encoding="utf-8").write("\n".join(lines) + "\n")

def cmd_export_holdings():
    watch = read_list("watchlist.txt")
    p = os.path.join(ROOT, "holdings.csv")
    with open(p, "w", encoding="utf-8") as f:
        f.write("ticker,weight\n")
        w = round(1.0 / max(len(watch), 1), 4)
        for t, _ in watch:
            f.write(f"{t},{w}\n")
    print(f"Wrote {p} (equal-weight). Run: python ../Portfolio-risk/portfolio_stress_test.py --holdings {p}")

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd == "lookup": cmd_lookup(sys.argv[2:])
    elif cmd == "watch": cmd_watch(sys.argv[2:])
    elif cmd == "export-holdings": cmd_export_holdings()
    else:
        if cmd in ("scan", "all"): cmd_scan()
        if cmd in ("html", "all"): cmd_html()
