#!/usr/bin/env python3
"""Backtest the EMA-crossover strategy on historical FX data.

Paper only. Conservative fills: spread charged on entry, and when a bar
touches both stop and target the stop is assumed to fill first.

Usage:
    python3 backtest.py                     # all pairs in config.json
    python3 backtest.py --pair EURUSD=X     # one pair
    python3 backtest.py --interval 15m --range 3mo
"""

import argparse
import csv
import json
import sys

from fxdata import fetch_bars
from strategy import atr, direction_series, ema
from trading import floating, pair_kind, pip_size, pnl, size_units


def run_pair(symbol, cfg, interval, range_):
    bars = fetch_bars(symbol, interval, range_)
    # NZD book: each bar converted at that bar's NZDUSD rate (fallback: last).
    nz = {b["t"]: b["c"] for b in fetch_bars("NZDUSD=X", interval, range_)}
    if len(bars) < 60:
        return None, [], [], f"{symbol}: only {len(bars)} bars returned"
    base, kind = pair_kind(symbol)
    if kind == "cross":
        return None, [], [], f"{symbol}: cross pair (no USD leg) not supported"

    P, R, A = cfg["strategy"], cfg["risk"], cfg["account"]
    spread = cfg["execution"]["spread_pips"] * pip_size(symbol)
    start_bal = float(A["starting_balance"])

    closes = [b["c"] for b in bars]
    fast = ema(closes, P["ema_fast"])
    slow = ema(closes, P["ema_slow"])
    atrs = atr(bars, P["atr_period"])
    dirs = direction_series(fast, slow)
    start = max(P["ema_fast"], P["ema_slow"], P["atr_period"]) + 1

    balance = start_bal
    pos = None
    pending = None     # direction to enter at next bar's open
    block = None       # direction we were stopped out of -> no re-entry until signal changes
    trades = []
    curve = []
    peak = start_bal
    max_dd = 0.0

    last_nz = nz.get(bars[0]["t"], 0.57) if bars else 0.57
    for i in range(start, len(bars)):
        b = bars[i]
        nzdusd = nz.get(b["t"], last_nz)
        last_nz = nzdusd

        # --- entry at this bar's open (signal was set on previous close) ---
        if pending is not None and pos is None:
            d = pending
            pending = None
            a = atrs[i - 1]
            if a:
                entry = b["o"] + (spread if d > 0 else -spread)
                stop_dist = a * P["atr_stop_mult"]
                stop = entry - d * stop_dist
                target = entry + d * a * P["atr_target_mult"]
                # NZD -> USD: NZDUSD is USD per NZD, so multiply.
                risk_nzd = balance * R["risk_percent"] / 100.0
                units = size_units(
                    kind, risk_nzd * nzdusd,
                    stop_dist, stop, entry, R["max_leverage"],
                    balance * nzdusd)
                if units > 0:
                    pos = {"d": d, "entry": entry, "stop": stop,
                           "target": target, "units": units,
                           "t_entry": b["t"], "i_entry": i}

        # --- manage open position against this bar ---
        if pos:
            d = pos["d"]
            xp = reason = None
            if d > 0:
                if b["l"] <= pos["stop"]:
                    xp, reason = pos["stop"], "stop"
                elif b["h"] >= pos["target"]:
                    xp, reason = pos["target"], "target"
            else:
                if b["h"] >= pos["stop"]:
                    xp, reason = pos["stop"], "stop"
                elif b["l"] <= pos["target"]:
                    xp, reason = pos["target"], "target"
            if xp is None and dirs[i] != d and dirs[i] != 0:
                xp, reason = b["c"], "signal_flip"
            if xp is not None:
                p = pnl(kind, pos["entry"], xp, d, pos["units"]) / nzdusd
                balance += p
                trades.append({
                    "pair": symbol, "dir": "long" if d > 0 else "short",
                    "t_entry": pos["t_entry"], "t_exit": b["t"],
                    "entry": pos["entry"], "exit": xp,
                    "units": pos["units"], "pnl": p,
                    "reason": reason, "balance": balance,
                })
                pos = None
                if reason in ("stop", "target"):
                    block = d          # halted; wait for the signal to change
                else:
                    block = None

        # --- fresh signal while flat -> enter next open ---
        if pos is None and dirs[i] != 0 and (block is None or dirs[i] != block):
            pending = dirs[i]
            block = None

        # --- mark-to-market equity ---
        mark = balance
        if pos:
            mark += floating(kind, pos["entry"], b["c"], pos["d"],
                             pos["units"]) / nzdusd
        curve.append((b["t"], mark))
        if mark > peak:
            peak = mark
        dd = (peak - mark) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    # close any still-open position at the last close for fair accounting
    if pos:
        b = bars[-1]
        p = pnl(kind, pos["entry"], b["c"], pos["d"], pos["units"]) / last_nz
        balance += p
        trades.append({
            "pair": symbol, "dir": "long" if pos["d"] > 0 else "short",
            "t_entry": pos["t_entry"], "t_exit": b["t"],
            "entry": pos["entry"], "exit": b["c"],
            "units": pos["units"], "pnl": p,
            "reason": "end_of_data", "balance": balance,
        })

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in losses)
    stats = {
        "pair": symbol, "bars": len(bars), "trades": len(trades),
        "wins": len(wins),
        "win_rate": 100.0 * len(wins) / len(trades) if trades else 0.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
        "pnl": balance - start_bal,
        "return_pct": 100.0 * (balance - start_bal) / start_bal,
        "max_dd_pct": 100.0 * max_dd,
        "avg_trade": (balance - start_bal) / len(trades) if trades else 0.0,
    }
    return stats, trades, curve, None


def main():
    ap = argparse.ArgumentParser(description="Backtest the FX bot (paper only)")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--pair", action="append",
                    help="override pairs (repeatable), e.g. --pair EURUSD=X")
    ap.add_argument("--interval", help="bar interval, e.g. 1h, 15m, 1d")
    ap.add_argument("--range", dest="range_", help="history range, e.g. 1y, 6mo, 3mo")
    args = ap.parse_args()

    with open(args.config) as f:
        cfg = json.load(f)
    pairs = args.pair or cfg["pairs"]
    interval = args.interval or cfg["backtest"]["interval"]
    range_ = args.range_ or cfg["backtest"]["range"]

    print(f"Backtesting {len(pairs)} pair(s): interval={interval} range={range_}")
    print(f"strategy: EMA {cfg['strategy']['ema_fast']}/{cfg['strategy']['ema_slow']} cross, "
          f"ATR({cfg['strategy']['atr_period']}) "
          f"{cfg['strategy']['atr_stop_mult']}x stop / {cfg['strategy']['atr_target_mult']}x target | "
          f"risk {cfg['risk']['risk_percent']}%/trade | spread "
          f"{cfg['execution']['spread_pips']} pip\n")

    all_trades, all_curves, results = [], [], []
    for sym in pairs:
        try:
            stats, trades, curve, err = run_pair(sym, cfg, interval, range_)
        except Exception as exc:
            print(f"{sym}: FAILED ({exc})")
            continue
        if err:
            print(err)
            continue
        results.append(stats)
        all_trades.extend(trades)
        for t, eq in curve:
            all_curves.append((sym, t, eq))
        pf = f"{stats['profit_factor']:.2f}" if stats["profit_factor"] else "inf"
        print(f"{stats['pair']:<10} bars={stats['bars']:<5} "
              f"trades={stats['trades']:<3} win={stats['win_rate']:5.1f}%  "
              f"PF={pf:<5} pnl={stats['pnl']:+9.2f} "
              f"ret={stats['return_pct']:+6.2f}%  maxDD={stats['max_dd_pct']:5.2f}%")

    if not results:
        print("\nNo results.")
        sys.exit(1)

    print("\n--- aggregate ---")
    n_tr = sum(r["trades"] for r in results)
    n_w = sum(r["wins"] for r in results)
    total_pnl = sum(r["pnl"] for r in results)
    mean_ret = sum(r["return_pct"] for r in results) / len(results)
    worst_dd = max(r["max_dd_pct"] for r in results)
    print(f"pairs={len(results)}  trades={n_tr}  "
          f"pooled win rate={100.0 * n_w / n_tr if n_tr else 0:.1f}%  "
          f"sum pnl={total_pnl:+.2f}  mean return={mean_ret:+.2f}%  "
          f"worst maxDD={worst_dd:.2f}%")
    print("(each pair simulated with its own "
          f"{cfg['account']['starting_balance']:,.0f} {cfg['account']['currency']} book)")

    with open("backtest_trades.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(all_trades[0].keys()) if all_trades
                           else ["pair"])
        w.writeheader()
        w.writerows(all_trades)
    with open("backtest_equity.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["pair", "t", "equity"])
        w.writerows(all_curves)
    print("\nwrote backtest_trades.csv, backtest_equity.csv")
    print("Paper results only — past performance does not predict the future.")


if __name__ == "__main__":
    main()
