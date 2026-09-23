"""Indicators and the EMA-crossover signal logic.

Signal rule: when EMA(fast) crosses above EMA(slow) -> long (+1),
crosses below -> short (-1). Direction persists until the next cross.
Stops/targets are ATR-based (volatility), not fixed pip counts.
"""


def ema(values, period):
    """Exponential moving average; None until it has `period` samples."""
    n = len(values)
    out = [None] * n
    if n < period:
        return out
    k = 2.0 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, n):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def atr(bars, period):
    """Average True Range with Wilder smoothing; None until warmed up."""
    n = len(bars)
    out = [None] * n
    if n <= period:
        return out
    tr = [0.0] * n
    for i in range(1, n):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        tr[i] = max(h - l, abs(h - pc), abs(l - pc))
    prev = sum(tr[1:period + 1]) / period
    out[period] = prev
    for i in range(period + 1, n):
        prev = (prev * (period - 1) + tr[i]) / period
        out[i] = prev
    return out


def direction_series(fast, slow):
    """Desired direction per bar from the EMA relationship: +1 / -1 / 0."""
    out = []
    d = 0
    for f, s in zip(fast, slow):
        if f is None or s is None:
            out.append(0)
            continue
        if f > s:
            d = 1
        elif f < s:
            d = -1
        out.append(d)
    return out
