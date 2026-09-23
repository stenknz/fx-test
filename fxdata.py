"""Yahoo Finance FX data access (stdlib only).

Yahoo's chart endpoint gives both historical OHLC bars and the current
spot price in one request, which keeps the live bot's request rate low.
Symbols are Yahoo-style: EURUSD=X, USDJPY=X, ...
"""

import json
import time
import urllib.parse
import urllib.request

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}

INTERVAL_SEC = {
    "1m": 60, "2m": 120, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "1d": 86400,
}


def _get(url, tries=3):
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode())
        except Exception as exc:  # network hiccups -> retry with backoff
            last = exc
            time.sleep(1 + attempt * 2)
    raise RuntimeError(f"fetch failed after {tries} tries: {url} ({last})")


def fetch(symbol, interval="1h", range_="5d"):
    """Return (bars, price). bars: list of dicts t/o/h/l/c. price: current spot."""
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{urllib.parse.quote(symbol)}?interval={interval}&range={range_}"
    )
    data = _get(url)
    result = data["chart"]["result"][0]
    meta = result.get("meta", {}) or {}
    if "regularMarketPrice" not in meta:
        raise RuntimeError(f"unexpected Yahoo schema for {symbol}")
    price = float(meta["regularMarketPrice"])
    ts = result.get("timestamp") or []
    q = result["indicators"]["quote"][0]
    bars = []
    for i, t in enumerate(ts):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c):
            continue
        bars.append({"t": t, "o": o, "h": h, "l": l, "c": c})
    return bars, price


def fetch_bars(symbol, interval="1h", range_="5d"):
    bars, _ = fetch(symbol, interval, range_)
    return bars
