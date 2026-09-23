#!/usr/bin/env python3
"""Simple FX bot dashboard (stdlib only, read-only).

Serves a live page showing IG demo account, equity curve, paper positions
(reconstructed from trades_ig.csv), and recent trades. Auto-refreshes.

Usage:
    python3 dashboard.py [--port 8082]
    then open http://localhost:8082 in a browser.
"""
import argparse
import csv
import html
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from ig_client import IGClient, IGError, load_credentials
    HAVE_IG = True
except ImportError:
    HAVE_IG = False

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", HERE)
TRADES_CSV = os.path.join(DATA_DIR, "trades_ig.csv")
EQUITY_CSV = os.path.join(DATA_DIR, "equity_ig.csv")

_client = None
_client_lock = threading.Lock()


def get_client():
    global _client
    with _client_lock:
        if _client is None and HAVE_IG:
            try:
                key, user, pwd, acct = load_credentials()
                _client = IGClient(key, user, pwd, acct)
                _client.login()
            except IGError:
                _client = None
        return _client


def ig_call(fn, *a):
    """Call IG fn; re-login once on auth failure."""
    global _client
    c = get_client()
    if c is None:
        raise IGError("no IG credentials/client")
    try:
        return fn(c, *a)
    except IGError as e:
        if "401" in str(e) or "403" in str(e):
            with _client_lock:
                _client = None
            c = get_client()
            if c is None:
                raise
            return fn(c, *a)
        raise


def read_csv_rows(path, limit=500):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    return rows[-limit:]


def open_positions():
    """Reconstruct open paper positions from the trade log."""
    pos = {}
    for r in read_csv_rows(TRADES_CSV, limit=5000):
        sym = r.get("symbol", "")
        if r.get("action") == "open":
            pos[sym] = {"side": r.get("side"), "entry": r.get("price"),
                        "units": r.get("units"), "time": r.get("time"),
                        "reason": r.get("reason", "")}
        elif r.get("action") == "close":
            pos.pop(sym, None)
    return pos


_EPICS = {}
_SNAP = {"t": 0.0, "mids": {}}
_BAL = {"t": 0.0, "data": None}
SNAP_TTL = 120


def get_epics(symbols):
    """Resolve IG epics once per process, not on every dashboard refresh."""
    missing = [s for s in symbols if s not in _EPICS]
    if missing:
        found = ig_call(lambda c: {
            s: c.resolve_epic(s[:-2] if s.endswith("=X") else s)[0]
            for s in missing})
        _EPICS.update(found)
    return {s: _EPICS[s] for s in symbols if s in _EPICS}


def snapshot_all(symbols):
    """Live mid prices via IG snapshots, cached SNAP_TTL seconds."""
    now = time.time()
    if now - _SNAP["t"] < SNAP_TTL and all(s in _SNAP["mids"] for s in symbols):
        return dict(_SNAP["mids"])
    out = {}
    try:
        epics = get_epics(symbols)
        for sym in symbols:
            if sym not in epics:
                continue
            try:
                bid, offer = ig_call(lambda c, e: c.snapshot(e), epics[sym])
                out[sym] = (bid + offer) / 2
            except IGError:
                pass
    except IGError:
        pass
    if out:
        _SNAP.update({"t": now, "mids": out})
    return out


def cached_balance():
    now = time.time()
    if _BAL["data"] is not None and now - _BAL["t"] < SNAP_TTL:
        return _BAL["data"]
    bal = ig_call(lambda c: c.account_balance())
    _BAL.update({"t": now, "data": bal})
    return bal


def status_payload():
    eq_rows = read_csv_rows(EQUITY_CSV, 1)
    last = eq_rows[0] if eq_rows else {}
    trades = read_csv_rows(TRADES_CSV, limit=5000)
    closes = [t for t in trades if t.get("action") == "close"]
    realized = 0.0
    for t in closes:
        try:
            realized += float(t.get("pnl_nzd") or 0)
        except (TypeError, ValueError):
            continue  # torn line mid-append: skip
    def fnum(v):
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0  # torn line mid-append
    payload = {
        "balance": fnum(last.get("balance_nzd")),
        "equity": fnum(last.get("equity_nzd")),
        "open_count": int(last.get("open") or 0),
        "closed": len(closes),
        "realized_nzd": round(realized, 2),
        "updated": last.get("time", "-"),
        "ig_balance": None, "ig_available": None, "ig_ccy": None,
        "ig_error": None,
    }
    try:
        bal = cached_balance()
        payload["ig_balance"] = bal.get("balance")
        payload["ig_available"] = bal.get("available")
        payload["ig_ccy"] = bal.get("currency")
    except IGError as e:
        payload["ig_error"] = str(e)[:120]
    return payload


PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FX Bot Dashboard</title>
<style>
:root{color-scheme:dark}
body{font-family:system-ui,sans-serif;background:#111;color:#eee;margin:0;padding:16px;max-width:900px}
h1{font-size:20px;margin:0 0 12px}
.cards{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px}
.card{background:#1c1c1c;border:1px solid #333;border-radius:8px;padding:10px 14px;min-width:130px}
.card .k{font-size:11px;color:#999;text-transform:uppercase}
.card .v{font-size:20px;font-weight:600}
.neg{color:#f66}.pos{color:#6d6}
canvas{width:100%;height:180px;background:#1c1c1c;border:1px solid #333;border-radius:8px}
table{width:100%;border-collapse:collapse;margin-top:12px;font-size:13px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #2a2a2a}
th{color:#999;font-weight:500}
h2{font-size:15px;margin:18px 0 4px;color:#bbb}
#err{color:#f66;font-size:13px}
small{color:#777}
</style></head><body>
<h1>FX Bot <small id="mode"></small></h1>
<div class="cards" id="cards"></div>
<div id="err"></div>
<h2>Equity (NZD)</h2>
<canvas id="eq" width="860" height="180"></canvas>
<h2>Open paper positions</h2>
<table><thead><tr><th>Pair</th><th>Side</th><th>Entry</th><th>Stop</th><th>Limit (TP)</th><th>Units</th><th>Live</th><th>Floating NZD</th></tr></thead>
<tbody id="pos"></tbody></table>
<h2>Recent trades</h2>
<table><thead><tr><th>Time</th><th>Pair</th><th>Action</th><th>Side</th><th>Price</th><th>P&amp;L</th><th>Reason</th></tr></thead>
<tbody id="tr"></tbody></table>
<script>
async function j(p){const r=await fetch(p);return r.json();}
function esc(s){return String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
async function tick(){
  try{
    const [s,eq,tr,po]=await Promise.all([j('/api/status'),j('/api/equity'),j('/api/trades'),j('/api/positions')]);
    document.getElementById('mode').textContent='· updated '+esc(s.updated);
    const cls=v=>v<0?'neg':'pos';
    document.getElementById('cards').innerHTML=
      card('Balance',s.balance.toFixed(2)+' NZD')+
      card('Equity',s.equity.toFixed(2)+' NZD')+
      card('Open',s.open_count)+
      card('Closed',s.closed)+
      card('Realised',s.realized_nzd.toFixed(2),cls(s.realized_nzd))+
      (s.ig_balance!=null?card('IG demo',s.ig_balance+' '+esc(s.ig_ccy||'')):'');
    document.getElementById('err').textContent=s.ig_error?('IG live data unavailable: '+s.ig_error):'';
    drawEq(eq);
    document.getElementById('pos').innerHTML=po.length?po.map(p=>
      `<tr><td>${esc(p.symbol)}</td><td>${esc(p.side)}</td><td>${esc(p.entry)}</td><td>${esc(p.stop??'-')}</td><td>${esc(p.target??'-')}</td><td>${esc(p.units)}</td><td>${esc(p.live??'-')}</td><td class="${p.floating<0?'neg':'pos'}" title="${esc(p.reason??'')}">${p.floating!=null?p.floating.toFixed(2):'-'}</td></tr>`).join('')
      :'<tr><td colspan="8">flat — no open positions</td></tr>';
    document.getElementById('tr').innerHTML=tr.map(t=>
      `<tr><td>${esc(t.time)}</td><td>${esc(t.symbol)}</td><td>${esc(t.action)}</td><td>${esc(t.side)}</td><td>${esc(t.price)}</td><td class="${(parseFloat(t.pnl_nzd)||0)<0?'neg':'pos'}">${esc(t.pnl_nzd||'')}</td><td>${esc(t.reason||'')}</td></tr>`).join('');
  }catch(e){document.getElementById('err').textContent='dashboard error: '+e;}
}
function card(k,v,c){return `<div class="card"><div class="k">${k}</div><div class="v ${c||''}">${v}</div></div>`;}
function drawEq(pts){
  const cv=document.getElementById('eq'),x=cv.getContext('2d');
  x.clearRect(0,0,cv.width,cv.height);
  if(pts.length<2){x.fillStyle='#777';x.fillText('not enough equity history yet',20,90);return;}
  const vs=pts.map(p=>p[1]),mn=Math.min(...vs),mx=Math.max(...vs),rg=(mx-mn)||1;
  x.strokeStyle='#4af';x.lineWidth=2;x.beginPath();
  pts.forEach((p,i)=>{const px=10+i*(cv.width-20)/(pts.length-1),py=cv.height-14-((p[1]-mn)/rg)*(cv.height-28);i?x.lineTo(px,py):x.moveTo(px,py);});
  x.stroke();
  x.fillStyle='#999';x.fillText(mx.toFixed(2),8,12);x.fillText(mn.toFixed(2),8,cv.height-4);
}
tick();setInterval(tick,30000);
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            self._send(PAGE, "text/html; charset=utf-8")
        elif self.path == "/api/status":
            self._send(json.dumps(status_payload()))
        elif self.path == "/api/equity":
            rows = read_csv_rows(EQUITY_CSV, limit=5000)
            pts = []
            for r in rows:
                try:
                    pts.append([r.get("time"), float(r.get("equity_nzd", 0))])
                except (TypeError, ValueError):
                    continue  # torn line mid-append: skip
            self._send(json.dumps(pts))
        elif self.path == "/api/trades":
            rows = read_csv_rows(TRADES_CSV, 25)
            rows.reverse()
            self._send(json.dumps(rows))
        elif self.path == "/api/positions":
            pos = open_positions()
            mids = snapshot_all(list(pos) + ["NZDUSD=X"]) if pos else {}
            try:
                nz = mids.get("NZDUSD=X") or 0.57
            except Exception:
                nz = 0.57
            out = []
            for sym, p in pos.items():
                try:
                    entry, units = float(p["entry"]), float(p["units"])
                    live = mids.get(sym)
                    fl = None
                    if live:
                        d = 1 if p["side"] == "long" else -1
                        kind = "quote" if not sym.startswith("USD") else "base"
                        usd = (live - entry) * d * units
                        if kind == "base" and live:
                            usd /= live
                        fl = usd / nz
                    m = re.search(r"stop=([\d.]+)\s+tp=([\d.]+)", p.get("reason") or "")
                    out.append({"symbol": sym, "side": p["side"], "entry": p["entry"],
                                "units": p["units"],
                                "stop": m.group(1) if m else None,
                                "target": m.group(2) if m else None,
                                "reason": p.get("reason", ""),
                                "live": f"{live:.5f}" if live else None,
                                "floating": fl})
                except (ValueError, TypeError):
                    pass
            self._send(json.dumps(out))
        else:
            self.send_response(404)
            self.end_headers()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8082)
    ap.add_argument("--bind", default="127.0.0.1",
                    help="bind address (use 0.0.0.0 inside Docker)")
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f"dashboard: http://localhost:{args.port}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
