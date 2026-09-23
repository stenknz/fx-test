"""Nowick (No Wick) compensation play — pure signal logic, no I/O.

Rules (1-minute intraday, trend-following):
  1. Trend from market structure on candle BODIES: a close beyond the
     highest/lowest close of the prior `lookback` bars = BOS in that
     direction. Trend persists until the opposite break.
  2. Signal = no-wick candle with the trend: bullish candle (close > open)
     whose upper wick is <= `max_wick_frac` of the full range (bearish
     mirror). Range must be >= `min_range` to filter out dead-bar noise.
  3. Compensation entry: a limit at the signal candle's ORIGIN (its open).
     Touched within `validity` bars -> enter at the origin price.
     A newer same-direction signal replaces the pending one; after
     `validity` bars untouched it expires.
  4. Stop = recent market-structure extreme +/- 3-pip buffer, computed by
     the caller and passed in (longs: swing low minus buffer; shorts:
     swing high plus buffer).
     Target = entry +/- 1R (1:1 risk-to-reward).
  5. No active management after entry: SL or TP only.

Session window and spread live in the caller, not here.
"""


def is_nowick(bar, direction, max_wick_frac=0.10, min_range=0.0):
    """True if `bar` is a no-wick candle in `direction` (+1/-1)."""
    o, h, l, c = bar["o"], bar["h"], bar["l"], bar["c"]
    rng = h - l
    if rng <= 0 or rng < min_range:
        return False
    if direction > 0:
        return c > o and (h - c) <= max_wick_frac * rng
    if direction < 0:
        return c < o and (c - l) <= max_wick_frac * rng
    return False


def trend_series(closes, lookback=20):
    """Trend state per bar from body breakouts: +1 / -1, persisting until
    the opposite break (0 only while undecided at the start)."""
    out = []
    d = 0
    for i, c in enumerate(closes):
        if i >= lookback:
            prev = closes[i - lookback:i]
            if c > max(prev):
                d = 1
            elif c < min(prev):
                d = -1
        out.append(d)
    return out


class PendingBoard:
    """Tracks one pending compensation entry per symbol.

    update(sym, idx, bar, trend) -> registers/replaces a pending signal.
    check(sym, idx, bar) -> 'fill' if this bar touches the pending level,
      'expired' if past validity (pending dropped), else None.
    fetch(sym) -> pop and return the pending dict or None.
    """

    def __init__(self, validity=9):
        self.validity = validity
        self._p = {}

    def update(self, sym, idx, bar, trend, stop_level):
        if trend > 0 and is_nowick(bar, 1):
            self._p[sym] = {"d": 1, "level": bar["o"], "stop": stop_level,
                            "sig_idx": idx}
        elif trend < 0 and is_nowick(bar, -1):
            self._p[sym] = {"d": -1, "level": bar["o"], "stop": stop_level,
                            "sig_idx": idx}

    def check(self, sym, idx, bar):
        p = self._p.get(sym)
        if not p:
            return None
        if idx - p["sig_idx"] > self.validity:
            del self._p[sym]
            return "expired"
        touched = bar["l"] <= p["level"] if p["d"] > 0 else bar["h"] >= p["level"]
        return "fill" if touched else None

    def fetch(self, sym):
        return self._p.pop(sym, None)

    def due(self, sym, idx):
        """True if a pending signal from an EARLIER bar awaits a fill."""
        p = self._p.get(sym)
        return p is not None and p["sig_idx"] < idx
