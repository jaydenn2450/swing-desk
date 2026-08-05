"""
serve.py — the desk server. Serves the dashboard AND enriches any ticker on demand.

Why this exists: dashboard.html is a static file, so searching a ticker could
only ever show what the last scan happened to fetch. This server adds a live
lookup endpoint, so EVERY ticker you search gets the same full treatment as a
top pick — technicals, options intel (walls/GEX/skew/flow), and fundamentals —
with no manual commands.

Usage:
    python serve.py            # then open http://localhost:8741/dashboard.html
    python serve.py --port 9000

Endpoints:
    /api/lookup?t=AAPL         -> {result, options, chart} fully analyzed
    /api/health                -> readiness + what's cached

Looked-up names are merged into data/report.json, so they persist across
`engine.py html` rebuilds and page reloads.
"""

import json, os, sys, threading, urllib.parse, datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import engine

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
REPORT = os.path.join(DATA, "report.json")

_lock = threading.Lock()
_state = {"spy": None, "earnings": None, "recent_earnings": None,
          "rotation": None, "sectors": None}


def _load_report():
    with open(REPORT, encoding="utf-8") as f:
        return json.load(f)


def _boot():
    """Warm the shared context a lookup needs (SPY, earnings, rotation)."""
    if _state["spy"] is None:
        print("  warming: SPY history ...")
        _state["spy"] = [b["c"] for b in engine.fetch_history("SPY")]
    if _state["earnings"] is None:
        print("  warming: earnings calendar ...")
        try:
            up, _wk, rec = engine.fetch_earnings_map()
            _state["earnings"], _state["recent_earnings"] = up, rec
        except Exception:
            _state["earnings"], _state["recent_earnings"] = {}, {}
    if _state["rotation"] is None:
        try:
            rep = _load_report()
            _state["rotation"] = rep.get("rotation")
            _state["sectors"] = {t: (r or {}).get("sector")
                                 for t, r in (rep.get("results") or {}).items()}
        except Exception:
            _state["rotation"], _state["sectors"] = None, {}


def _sector_for(sym):
    """Sector from the cached screener universe, else from yfinance info."""
    cache = os.path.join(DATA, "universe_auto.json")
    if os.path.exists(cache):
        try:
            s = (json.load(open(cache, encoding="utf-8")).get("sectors") or {}).get(sym)
            if s: return s
        except Exception:
            pass
    return None


def _normalize(sym):
    """BRK.B / BRK B -> BRK-B (Yahoo's class-share convention)."""
    s = sym.upper().strip().replace(" ", "-")
    if "." in s and not s.startswith("^"):
        s = s.replace(".", "-")
    return s


def lookup(sym):
    """Full analysis for one ticker: TA + options intel + fundamentals."""
    sym = _normalize(sym)
    _boot()
    try:
        bars = engine.fetch_history(sym)
    except Exception as e:
        # unknown symbols surface as an upstream 404 — that's a clean "not found",
        # not a server fault, and must not read as a crash to the user.
        msg = str(e)
        if "404" in msg or "Not Found" in msg:
            return {"error": f"'{sym}' not found — check the symbol", "notfound": True}
        return {"error": f"data provider error for {sym}: {msg[:90]}"}
    if not bars:
        return {"error": f"no price data for {sym}", "notfound": True}
    sector = _sector_for(sym)
    r = engine.analyze(sym, bars, _state["spy"], sector,
                       (_state["earnings"] or {}).get(sym),
                       (_state.get("recent_earnings") or {}).get(sym))
    opts = None
    if "risk" in r:
        opts = engine.fetch_options_full(sym, r["close"])
    rot = engine.rotation_for_sector(_state["rotation"], r.get("sector")) if _state["rotation"] else None
    r["rotation"] = rot
    r["thesis"] = engine.thesis_comment(r, rot, opts)
    r["plan"] = engine.trade_plan(r, opts)
    fa = engine.fetch_fa(sym)
    r["fa"] = fa
    r["fa_verdict"] = engine.fa_verdict(fa, r.get("close"))
    if (r.get("earnings") or {}).get("alert_upcoming"):
        r["earnings"]["preview"] = engine.fetch_earnings_preview(
            sym, r["close"], r["earnings"]["date"], bars, r.get("risk"), opts)
    r["adhoc"] = True
    chart = engine.chart_pack(bars)

    # persist into report.json so reloads and html rebuilds keep it
    with _lock:
        try:
            rep = _load_report()
            rep.setdefault("results", {})[sym] = r
            if opts: rep.setdefault("options", {})[sym] = opts
            rep.setdefault("charts", {})[sym] = chart
            tmp = REPORT + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(rep, f)
            os.replace(tmp, REPORT)
        except Exception as e:
            print(f"  ! could not persist {sym}: {e}")
    return {"result": r, "options": opts, "chart": chart}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        # dashboards are regenerated every scan — never let the browser cache them
        if self.path.endswith(".html") or self.path == "/":
            self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/health":
            return self._json({"ok": True, "warm": _state["spy"] is not None})
        if parsed.path == "/api/lookup":
            q = urllib.parse.parse_qs(parsed.query)
            t = (q.get("t") or [""])[0].upper().strip()
            if (not t or len(t) > 12
                    or not t.replace(".", "").replace("-", "").replace("^", "").isalnum()):
                return self._json({"error": "bad ticker"}, 400)
            print(f"[lookup] {t} ...")
            try:
                out = lookup(t)
                if out.get("error"):
                    return self._json(out, 404 if out.get("notfound") else 502)
                r = out["result"]
                print(f"[lookup] {t} -> {r.get('setup')} | {(r.get('thesis') or {}).get('verdict')}"
                      f" | opts={'yes' if out.get('options') else 'no'}"
                      f" | fa={(r.get('fa_verdict') or {}).get('verdict', 'n/a')}")
                return self._json(out)
            except Exception as e:
                print(f"[lookup] {t} FAILED: {e}")
                return self._json({"error": str(e)[:200]}, 500)
        return super().do_GET()

    def log_message(self, fmt, *args):
        pass  # keep the console clean; lookups log themselves


if __name__ == "__main__":
    port = 8741
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    if not os.path.exists(REPORT):
        sys.exit("No data/report.json — run `python engine.py all` first.")
    print(f"Swing Desk server — warming up ...")
    _boot()
    print(f"\n  Dashboard : http://localhost:{port}/dashboard.html")
    print(f"  Charts    : http://localhost:{port}/chart.html#TICKER")
    print(f"  Any ticker you search is now analyzed live (TA + options + FA).")
    print(f"  Ctrl-C to stop.\n")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
