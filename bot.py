#!/usr/bin/env python3
"""Live PAPER-trading loop: EMA-crossover entries, ATR stops/targets.

No broker connection, no real money. Polls Yahoo for spot prices, takes
signals only from CLOSED bars, and manages open paper positions against
the live price. Ctrl+C stops cleanly.

Usage:
    python3 bot.py            # loop until Ctrl+C
    python3 bot.py --once     # single cycle (smoke test)
    python3 bot.py --poll 30  # override poll interval (seconds)
"""

import argparse
import csv
import datetime as dt
import json
import os
import time

import fxdata
from strategy import atr, direction_series, ema
from trading import floating, pair_kind, pip_size, pnl, size_units

TRADES_CSV = "trades_live.csv"
EQUITY_CSV = "equity_live.csv"


def fmt_price(symbol, p):
    return f"{p:.3f}" if "JPY" in symbol.upper() else f"{p:.5f}"


def utc_now():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_csv(path, header):
    new = not os.path.exists(path) or os.path.getsize(path) == 0
    fh = open(path, "a", newline="")
    writer = csv.writer(fh)
    if new:
        writer.writerow(header)
    return fh, writer


def cycle(cfg, state, tf, tw, ef, ew, warned):
    """One poll: fetch each pair, manage exits, take entries, log, print."""
    P, R = cfg["strategy"], cfg["risk"]
    L = cfg["live"]
    interval = L.get("interval", "1h")
    dur = fxdata.INTERVAL_SEC.get(interval, 3600)
    now = time.time()
    lines = []

    for sym in cfg["pairs"]:
        base, kind = pair_kind(sym)
        if kind == "cross":
            if sym not in warned:
                lines.append(f"{sym:<10} skipped: cross pair (no USD leg)")
                warned.add(sym)
            continue
        try:
            bars, price = fxdata.fetch(sym, interval, L.get("range", "5d"))
        except Exception as exc:
            lines.append(f"{sym:<10} fetch failed: {exc}")
            continue
        time.sleep(0.2)  # be polite to the API

        px = fmt_price(sym, price)
        pos = state["positions"].get(sym)
        if pos:
            pos["mark"] = price

        # Signals only from bars that have fully closed.
        closed = [b for b in bars if b["t"] + dur <= now]
        if len(closed) < P["ema_slow"] + 2:
            lines.append(f"{sym:<10} {px} waiting for closed bars "
                         f"({len(closed)}/{P['ema_slow'] + 2})")
            continue
        closes = [b["c"] for b in closed]
        dirs = direction_series(ema(closes, P["ema_fast"]),
                                ema(closes, P["ema_slow"]))
        atrs = atr(closed, P["atr_period"])
        d_sig = dirs[-1]
        a = atrs[-1]
        block = state["block"].get(sym)
        just_reported = False

        # ---- 1) exits against the live price ----
        if pos:
            d = pos["d"]
            xp = reason = None
            if d > 0:
                if price <= pos["stop"]:
                    xp, reason = pos["stop"], "stop"
                elif price >= pos["target"]:
                    xp, reason = pos["target"], "target"
            else:
                if price >= pos["stop"]:
                    xp, reason = pos["stop"], "stop"
                elif price <= pos["target"]:
                    xp, reason = pos["target"], "target"
            if xp is None and d_sig != 0 and d_sig != d:
                xp, reason = price, "signal_flip"
            if xp is not None:
                p = pnl(kind, pos["entry"], xp, d, pos["units"])
                state["balance"] += p
                state["closed"] += 1
                state["closed_pnl"] += p
                tw.writerow([utc_now(), sym, "close",
                             "long" if d > 0 else "short",
                             fmt_price(sym, xp), f"{pos['units']:.2f}",
                             f"{p:.2f}", f"{state['balance']:.2f}", reason])
                if reason in ("stop", "target"):
                    state["block"][sym] = d  # wait for signal to change
                else:
                    state["block"].pop(sym, None)
                del state["positions"][sym]
                pos = None
                lines.append(f"{sym:<10} {px} CLOSED {reason.upper()} "
                             f"pnl={p:+.2f} bal={state['balance']:.2f}")
                just_reported = True

        # ---- 2) entry while flat ----
        if (pos is None and d_sig != 0 and a
                and d_sig != block
                and len(state["positions"]) < R["max_open_positions"]):
            spread = cfg["execution"]["spread_pips"] * pip_size(sym)
            entry = price + (spread if d_sig > 0 else -spread)
            stop_dist = a * P["atr_stop_mult"]
            stop = entry - d_sig * stop_dist
            target = entry + d_sig * a * P["atr_target_mult"]
            units = size_units(
                kind, state["balance"] * R["risk_percent"] / 100.0,
                stop_dist, stop, entry, R["max_leverage"], state["balance"])
            if units > 0:
                pos = {"d": d_sig, "entry": entry, "stop": stop,
                       "target": target, "units": units, "mark": price}
                state["positions"][sym] = pos
                state["block"].pop(sym, None)
                tw.writerow([utc_now(), sym, "open",
                             "long" if d_sig > 0 else "short",
                             fmt_price(sym, entry), f"{units:.2f}", "",
                             f"{state['balance']:.2f}", f"atr={a:.5f}"])
                side = "LONG " if d_sig > 0 else "SHORT"
                lines.append(
                    f"{sym:<10} {px} OPEN {side} units={units:,.0f} "
                    f"entry={fmt_price(sym, entry)} "
                    f"stop={fmt_price(sym, stop)} tp={fmt_price(sym, target)}")
                just_reported = True

        # ---- 3) plain status line ----
        if not just_reported:
            arrow = f"dir={d_sig:+d}"
            if pos:
                fl = floating(kind, pos["entry"], price, pos["d"],
                              pos["units"])
                side = "LONG " if pos["d"] > 0 else "SHORT"
                lines.append(f"{sym:<10} {px} {side} "
                             f"units={pos['units']:,.0f} pnl={fl:+.2f} "
                             f"{arrow}")
            else:
                note = f" (blocked {block:+d})" if block else ""
                lines.append(f"{sym:<10} {px} flat {arrow}{note}")

    # ---- equity mark & log ----
    equity = state["balance"]
    for sym, p in state["positions"].items():
        _, kind = pair_kind(sym)
        equity += floating(kind, p["entry"], p["mark"], p["d"], p["units"])
    state["equity"] = equity
    ew.writerow([utc_now(), f"{state['balance']:.2f}",
                 f"{equity:.2f}", len(state["positions"])])
    tf.flush()
    ef.flush()
    return lines


def main():
    ap = argparse.ArgumentParser(description="FX paper-trading bot "
                                             "(no real money)")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--once", action="store_true", help="one cycle then exit")
    ap.add_argument("--poll", type=int, default=None, help="poll seconds")
    args = ap.parse_args()

    with open(args.config) as f:
        cfg = json.load(f)

    print("=" * 62)
    print(" PAPER FX BOT — no broker, no real money. Ctrl+C to stop.")
    print(f" account: {cfg['account']['starting_balance']:,.0f} "
          f"{cfg['account']['currency']} | "
          f"risk {cfg['risk']['risk_percent']}%/trade | "
          f"max {cfg['risk']['max_open_positions']} open | "
          f"max lev {cfg['risk']['max_leverage']}x")
    print(f" strategy: EMA {cfg['strategy']['ema_fast']}/"
          f"{cfg['strategy']['ema_slow']} cross, "
          f"ATR({cfg['strategy']['atr_period']}) "
          f"{cfg['strategy']['atr_stop_mult']}x stop / "
          f"{cfg['strategy']['atr_target_mult']}x target, "
          f"spread {cfg['execution']['spread_pips']} pip")
    print(f" pairs: {', '.join(p.replace('=X', '') for p in cfg['pairs'])}")
    print("=" * 62)

    state = {
        "balance": float(cfg["account"]["starting_balance"]),
        "equity": float(cfg["account"]["starting_balance"]),
        "positions": {},  # symbol -> {d, entry, stop, target, units, mark}
        "block": {},      # symbol -> direction halted at (no re-entry until flip)
        "closed": 0, "closed_pnl": 0.0,
    }
    warned = set()
    poll = args.poll or cfg["live"].get("poll_seconds", 120)

    tf, tw = ensure_csv(TRADES_CSV,
                        ["time", "symbol", "action", "side", "price",
                         "units", "pnl", "balance", "reason"])
    ef, ew = ensure_csv(EQUITY_CSV,
                        ["time", "balance", "equity", "open"])

    try:
        while True:
            lines = cycle(cfg, state, tf, tw, ef, ew, warned)
            print(f"\n--- {utc_now()} | balance {state['balance']:.2f} | "
                  f"equity {state['equity']:.2f} | open "
                  f"{len(state['positions'])}/"
                  f"{cfg['risk']['max_open_positions']} ---")
            for ln in lines:
                print(ln)
            if args.once:
                break
            time.sleep(poll)
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        open_pos = len(state["positions"])
        print(f"\nrun summary: {state['closed']} trade(s) closed, "
              f"realised {state['closed_pnl']:+.2f}, "
              f"balance {state['balance']:.2f}, "
              f"equity {state['equity']:.2f}, "
              f"{open_pos} position(s) still open (paper)")
        print(f"logs: {os.path.abspath(TRADES_CSV)}, "
              f"{os.path.abspath(EQUITY_CSV)}")
        tf.close()
        ef.close()


if __name__ == "__main__":
    main()
