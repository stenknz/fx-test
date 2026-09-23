#!/usr/bin/env python3
"""Backtest the Nowick compensation play per Bard FX's documented rules.

  Timeframe:       15-minute bars (closed bars only)
  Session window:  06:00-12:00 UTC == 08:00-14:00 Sweden summer time (CEST).
                   Signals AND fills only inside the window. Shift by an hour
                   in winter (CET). Existing positions are managed regardless.
  Signal:          no-wick candle (wick <= 10% of range) with the body-BOS trend
  Entry:           limit at the signal candle's origin, touched within 9 bars;
                   newer same-direction signals replace pendings
  Stop:            recent 20-bar structure extreme +/- 3-pip buffer
  Target:          1R (1:1). No active management: SL or TP only.

Conservative fills: entries only from the bar after the signal; a fill bar
that also breaches the stop scores as a stop-out; stop-first on dual hits.
Spread charged against the entry.

Usage:
    python3 backtest_nowick.py --symbol AUDUSD=X --range 1mo
"""

import argparse
import csv
import datetime as dt
import sys

sys.path.insert(0, ".")
sys.path.insert(0, "strategies")

import fxdata
from nowick import PendingBoard, trend_series
from trading import pair_kind, pip_size, pnl

SESSION_HOURS = (6, 7, 8, 9, 10, 11)  # 06:00-11:59 UTC (Sweden 08-14 summer)
STRUCT_LOOKBACK = 20
BUFFER_PIPS = 3.0
VALIDITY = 9


def structure_stop(bars, i, d, pip):
    """Recent structure extreme +/- 3-pip buffer over the bars before i."""
    window = bars[max(0, i - STRUCT_LOOKBACK):i]
    if not window:
        return None
    buf = BUFFER_PIPS * pip
    if d > 0:
        return min(b["l"] for b in window) - buf
    return max(b["h"] for b in window) + buf


def main():
    ap = argparse.ArgumentParser(description="Nowick backtest (15m bars)")
    ap.add_argument("--symbol", default="AUDUSD=X")
    ap.add_argument("--range", default="1mo")
    ap.add_argument("--risk-nzd", type=float, default=200.0)
    ap.add_argument("--spread-pips", type=float, default=1.0)
    ap.add_argument("--max-hold", type=int, default=40)  # 10h safety net
    args = ap.parse_args()

    base, kind = pair_kind(args.symbol)
    if kind == "cross":
        print("cross pairs unsupported"); sys.exit(2)
    pip = pip_size(args.symbol)
    spread = args.spread_pips * pip

    bars, _ = fxdata.fetch(args.symbol, "15m", args.range)
    nzbars, _ = fxdata.fetch("NZDUSD=X", "15m", args.range)
    nz = {b["t"]: b["c"] for b in nzbars}
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    bars = [b for b in bars if b["t"] + 900 <= now]
    if len(bars) < STRUCT_LOOKBACK + 30:
        print(f"only {len(bars)} closed 15m bars — need more history"); sys.exit(2)

    trends = trend_series([b["c"] for b in bars], 20)
    board = PendingBoard(validity=VALIDITY)
    trades = []
    open_pos = None
    last_nz = nz.get(bars[0]["t"], 0.572)

    for i, bar in enumerate(bars):
        nzdusd = nz.get(bar["t"], last_nz)
        last_nz = nzdusd
        hour = dt.datetime.fromtimestamp(bar["t"], dt.timezone.utc).hour
        in_session = hour in SESSION_HOURS

        # 1) pending fill check (earlier-bar signals only, session only)
        if open_pos is None and in_session and board.due("S", i):
            chk = board.check("S", i, bar)
            if chk == "fill":
                p = board.fetch("S")
                d = p["d"]
                fill = p["level"] + (spread if d > 0 else -spread)
                risk_dist = (fill - p["stop"]) * d
                sane = (p["stop"] < fill) if d > 0 else (p["stop"] > fill)
                if risk_dist > 0 and sane:
                    risk_usd = args.risk_nzd * nzdusd
                    units = (risk_usd / risk_dist if kind == "quote_usd"
                             else risk_usd * p["stop"] / risk_dist)
                    tp = fill + d * risk_dist  # 1R
                    open_pos = {"d": d, "entry": fill, "stop": p["stop"],
                                "tp": tp, "units": units, "i0": i,
                                "dec": 3 if "JPY" in args.symbol else 5}
                    hit_stop = (bar["l"] <= p["stop"] if d > 0
                                else bar["h"] >= p["stop"])
                    hit_tp = (bar["h"] >= tp if d > 0 else bar["l"] <= tp)
                    if hit_stop or hit_tp:
                        xp = p["stop"] if hit_stop else tp
                        trades.append(close_trade(
                            open_pos, xp, bar["t"], nzdusd, kind,
                            "stop" if hit_stop else "target", args.risk_nzd))
                        open_pos = None

        # 2) manage open position — SL or TP only (stop-first)
        if open_pos is not None:
            d = open_pos["d"]
            hit_stop = (bar["l"] <= open_pos["stop"] if d > 0
                        else bar["h"] >= open_pos["stop"])
            hit_tp = (bar["h"] >= open_pos["tp"] if d > 0
                      else bar["l"] <= open_pos["tp"])
            if hit_stop or hit_tp:
                xp = open_pos["stop"] if hit_stop else open_pos["tp"]
                trades.append(close_trade(open_pos, xp, bar["t"], nzdusd,
                                          kind, "stop" if hit_stop else "target",
                                          args.risk_nzd))
                open_pos = None
            elif i - open_pos["i0"] >= args.max_hold:
                trades.append(close_trade(open_pos, bar["c"], bar["t"],
                                          nzdusd, kind, "time_stop",
                                          args.risk_nzd))
                open_pos = None

        # 3) register fresh signals (session only, trend required)
        if in_session and trends[i] != 0:
            stop = structure_stop(bars, i, trends[i], pip)
            if stop is not None:
                board.update("S", i, bar, trends[i], stop)

    if open_pos is not None:
        trades.append(close_trade(open_pos, bars[-1]["c"], bars[-1]["t"],
                                  last_nz, kind, "eod", args.risk_nzd))

    wins = [t for t in trades if t["r"] > 0]
    tot_r = sum(t["r"] for t in trades)
    tot_nzd = sum(t["pnl"] for t in trades)
    print(f"symbol {args.symbol} | {len(bars)} 15m bars | 1R target | "
          f"spread={args.spread_pips}pip | risk={args.risk_nzd:.0f} NZD")
    print(f"trades {len(trades)} | wins {len(wins)} "
          f"({100*len(wins)/len(trades) if trades else 0:.1f}%) | "
          f"total {tot_r:+.2f}R ({tot_nzd:+.2f} NZD) | "
          f"expectancy {tot_r/len(trades) if trades else 0:+.3f}R/trade")
    if trades:
        out = f"backtest_nowick_{base}.csv"
        with open(out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(trades[0].keys()))
            w.writeheader()
            w.writerows(trades)
        print(f"wrote {out}")


def close_trade(pos, xp, t, nzdusd, kind, reason, risk_nzd):
    p_nzd = pnl(kind, pos["entry"], xp, pos["d"], pos["units"]) / nzdusd
    return {"time": dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "side": "long" if pos["d"] > 0 else "short",
            "entry": round(pos["entry"], pos.get("dec", 5)),
            "exit": round(xp, pos.get("dec", 5)),
            "r": round(p_nzd / risk_nzd, 3),
            "pnl": round(p_nzd, 2), "reason": reason}


if __name__ == "__main__":
    main()
