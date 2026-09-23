#!/usr/bin/env python3
"""IG-demo strategy runner: EMA-cross entries + ATR stops/targets on IG hourly bars.

Account currency: NZD (matches the IG demo account). Internal P&L math is in
USD (trading.py) and converted at the live NZDUSD rate each cycle.

Modes:
    python3 bot_ig.py --once            one DRY-RUN cycle (default: no orders)
    python3 bot_ig.py                   loop dry-run until Ctrl+C
    python3 bot_ig.py --live --once     place REAL DEMO orders (demo money, real fills)

Live mode uses IG's minimum deal size per market with ATR stop/limit levels
attached natively, and closes positions on stop/target/signal-flip.
Credentials: env IG_API_KEY/IG_USERNAME/IG_PASSWORD or ~/.hermes/ig_demo.json.
"""

import argparse
import csv
import datetime as dt
import json
import math
import os
import sys
import time

from ig_client import IGClient, IGError, load_credentials
from strategy import atr, direction_series, ema
from trading import floating, pair_kind, pnl, size_units

DATA_DIR = os.environ.get("DATA_DIR", ".")
TRADES_CSV = os.path.join(DATA_DIR, "trades_ig.csv")
EQUITY_CSV = os.path.join(DATA_DIR, "equity_ig.csv")


def utc_now():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_price(symbol, p):
    return f"{p:.3f}" if "JPY" in symbol.upper() else f"{p:.5f}"


def yahoo_to_ig6(sym):
    return sym[:-2] if sym.endswith("=X") else sym


def ensure_csv(path, header):
    new = not os.path.exists(path) or os.path.getsize(path) == 0
    fh = open(path, "a", newline="")
    writer = csv.writer(fh)
    if new:
        writer.writerow(header)
    return fh, writer


def resolve_all(client, pairs):
    """Map Yahoo symbols to (epic, name, min_size, contract_size)."""
    out = {}
    for sym in pairs:
        pair6 = yahoo_to_ig6(sym)
        epic, name, expiry = client.resolve_epic(pair6)
        spec = {"epic": epic, "name": name, "min_size": None, "contract": None}
        try:
            body, _ = client._req(
                "GET", f"/markets/{epic}", version="2")
            inst = (body.get("instrument") or {})
            deal = (body.get("dealingRules") or {})
            spec["min_size"] = (deal.get("minDealSize") or {}).get("value")
            spec["contract"] = inst.get("contractSize")
        except IGError:
            pass
        out[sym] = spec
    return out


def record_close(state, tw, sym, pos, xp, reason, kind, nzdusd):
    """Book a paper close: balance, counters, CSV row. Returns pnl_nzd."""
    p_nzd = pnl(kind, pos["entry"], xp, pos["d"], pos["units"]) / nzdusd
    state["balance"] += p_nzd
    state["closed"] += 1
    state["closed_pnl"] += p_nzd
    tw.writerow([utc_now(), sym, "close",
                 "long" if pos["d"] > 0 else "short",
                 fmt_price(sym, xp), f"{pos['units']:.2f}",
                 f"{p_nzd:.2f}", f"{state['balance']:.2f}", reason])
    return p_nzd


def cycle(cfg, client, epics, state, tf, tw, ef, ew, warned, live, fixed_size):
    P, R = cfg["strategy"], cfg["risk"]
    dur = 3600  # hourly bars
    now = time.time()
    lines = []

    # NZDUSD mid for USD<->NZD conversion (state keeps prior rate as fallback).
    try:
        nzd_bid, nzd_offer = client.snapshot(epics["NZDUSD=X"]["epic"])
        state["nzdusd"] = (nzd_bid + nzd_offer) / 2
    except IGError as e:
        if not state.get("nzdusd"):
            lines.append(f"NZDUSD snapshot failed and no prior rate: {e}")
            return lines
        lines.append(f"NZDUSD snapshot failed, reusing {state['nzdusd']:.5f}")
    nzdusd = state["nzdusd"]

    # Live IG positions keyed by epic (for managed closes in live mode).
    ig_pos = {}
    if live:
        try:
            for p in client.positions():
                d = p.get("position", {})
                ig_pos[d.get("epic")] = d
        except IGError as e:
            lines.append(f"positions fetch failed: {e}")
        # Reconcile: a paper position whose native IG stop/limit already
        # filled disappears from IG's book — mirror that close in paper.
        for sym, p in list(state["positions"].items()):
            if p.get("deal_id") and epics[sym]["epic"] not in ig_pos:
                d = p["d"]
                hit_stop = ((d > 0 and p["mark"] <= p["stop"])
                            or (d < 0 and p["mark"] >= p["stop"]))
                xp = p["stop"] if hit_stop else p["target"]
                p_nzd = pnl(pair_kind(sym)[1], p["entry"], xp, d,
                            p["units"]) / nzdusd
                state["balance"] += p_nzd
                state["closed"] += 1
                state["closed_pnl"] += p_nzd
                tw.writerow([utc_now(), sym, "close",
                             "long" if d > 0 else "short",
                             fmt_price(sym, xp), f"{p['units']:.2f}",
                             f"{p_nzd:.2f}", f"{state['balance']:.2f}",
                             "stop" if hit_stop else "target"])
                del state["positions"][sym]
                lines.append(f"{sym:<10} reconciled IG native fill "
                             f"({('stop' if hit_stop else 'target').upper()})")

    for sym in cfg["pairs"]:
        base, kind = pair_kind(sym)
        if kind == "cross":
            if sym not in warned:
                lines.append(f"{sym:<10} skipped: cross pair (no USD leg)")
                warned.add(sym)
            continue
        epic = epics[sym]["epic"]
        try:
            bid, offer = client.snapshot(epic)
            price = (bid + offer) / 2
        except IGError as e:
            lines.append(f"{sym:<10} snapshot failed: {e}")
            continue
        # Hourly bars only change when a new hour closes: serve from cache
        # and refetch at most once per hour. IG caps historical-data calls
        # tightly; refetching 60 bars every 10 min burns the weekly allowance
        # in about a day. Snapshots (above) are unaffected by that cap.
        hour_id = int(now // 3600)
        bcache = state.setdefault("bars", {})
        centry = bcache.get(sym)
        if centry and centry["hour"] == hour_id:
            bars = centry["bars"]
        else:
            try:
                bars = client.prices(epic, "HOUR", 30)
            except IGError as e:
                if centry:
                    bars = centry["bars"]
                    lines.append(f"{sym:<10} bars failed, using cache")
                else:
                    # No bars: no signals possible, but stop/target exits
                    # only need the snapshot price — keep managing these.
                    hpos = state["positions"].get(sym)
                    if hpos and hpos.get("stop") is not None:
                        d = hpos["d"]
                        pxs = fmt_price(sym, price)
                        hit_stop = (price <= hpos["stop"] if d > 0
                                    else price >= hpos["stop"])
                        hit_tp = (price >= hpos["target"] if d > 0
                                  else price <= hpos["target"])
                        if hit_stop or hit_tp:
                            xp = hpos["stop"] if hit_stop else hpos["target"]
                            reason = "stop" if hit_stop else "target"
                            p_nzd = record_close(state, tw, sym, hpos, xp,
                                                 reason, kind, nzdusd)
                            state["block"][sym] = d
                            del state["positions"][sym]
                            lines.append(f"{sym:<10} {pxs} CLOSED "
                                         f"{reason.upper()} (snapshot) "
                                         f"pnl={p_nzd:+.2f}")
                        else:
                            fl = floating(kind, hpos["entry"], price, d,
                                          hpos["units"]) / nzdusd
                            lines.append(f"{sym:<10} {pxs} holding "
                                         f"(no bars) pnl={fl:+.2f}")
                    else:
                        lines.append(f"{sym:<10} IG bars failed: {e}")
                    continue
            bcache[sym] = {"hour": hour_id, "bars": bars}

        px = fmt_price(sym, price)
        pos = state["positions"].get(sym)
        if pos:
            pos["mark"] = price

        closed = [b for b in bars if b["t"] + dur <= now]
        if len(closed) < P["ema_slow"] + 2:
            lines.append(f"{sym:<10} {px} waiting for closed bars "
                         f"({len(closed)}/{P['ema_slow'] + 2})")
            continue
        closes = [b["c"] for b in closed]
        dirs = direction_series(ema(closes, P["ema_fast"]),
                                ema(closes, P["ema_slow"]))
        atrs = atr(closed, P["atr_period"])
        d_sig, a = dirs[-1], atrs[-1]
        block = state["block"].get(sym)
        just_reported = False

        # ---- 1) exits ----
        if pos:
            d = pos["d"]
            xp = reason = None
            has_levels = (pos.get("stop") is not None
                          and pos.get("target") is not None)
            if d > 0:
                if has_levels and price <= pos["stop"]:
                    xp, reason = pos["stop"], "stop"
                elif has_levels and price >= pos["target"]:
                    xp, reason = pos["target"], "target"
            else:
                if has_levels and price >= pos["stop"]:
                    xp, reason = pos["stop"], "stop"
                elif has_levels and price <= pos["target"]:
                    xp, reason = pos["target"], "target"
            if xp is None and d_sig != 0 and d_sig != d:
                xp, reason = price, "signal_flip"
            if xp is not None:
                p_nzd = record_close(state, tw, sym, pos, xp, reason,
                                     kind, nzdusd)
                if live and pos.get("deal_id"):
                    try:
                        direction = "SELL" if d > 0 else "BUY"
                        conf = client.close_position(pos["deal_id"], direction,
                                                     pos.get("ig_size"))
                        reason += f" ig:{conf.get('dealStatus')}"
                    except IGError as e:
                        reason += f" ig_close_failed:{e}"
                if reason.startswith("stop") or reason.startswith("target"):
                    state["block"][sym] = d
                else:
                    state["block"].pop(sym, None)
                del state["positions"][sym]
                pos = None
                lines.append(f"{sym:<10} {px} CLOSED {reason.upper()} "
                             f"pnl={p_nzd:+.2f} bal={state['balance']:.2f}")
                just_reported = True

        # ---- 2) entry while flat ----
        if (pos is None and d_sig != 0 and a
                and d_sig != block
                and len(state["positions"]) < R["max_open_positions"]):
            entry = offer if d_sig > 0 else bid  # real fill side, spread included
            stop_dist = a * P["atr_stop_mult"]
            stop = entry - d_sig * stop_dist
            target = entry + d_sig * a * P["atr_target_mult"]
            risk_nzd = state["balance"] * R["risk_percent"] / 100.0
            # NZD -> USD: NZDUSD is USD per NZD, so multiply.
            units = size_units(kind, risk_nzd * nzdusd, stop_dist, stop,
                               entry, R["max_leverage"],
                               state["balance"] * nzdusd)
            if units > 0:
                new_pos = {"d": d_sig, "entry": entry, "stop": stop,
                           "target": target, "units": units, "mark": price}
                ig_note = ""
                if live:
                    if fixed_size:
                        size = fixed_size
                    else:
                        contract = epics[sym].get("contract")
                        if not contract:
                            lines.append(f"{sym:<10} live order SKIPPED: "
                                         f"unknown IG contract size")
                            continue
                        size = math.floor(units / contract * 100) / 100
                        min_size = epics[sym].get("min_size") or 0.01
                        if size < min_size:
                            lines.append(f"{sym:<10} live order SKIPPED: "
                                         f"computed size {size} < IG minimum "
                                         f"{min_size} (risk too small)")
                            continue
                    try:
                        conf = client.open_otc(
                            epic, "BUY" if d_sig > 0 else "SELL", size,
                            stop_level=round(stop, 5 if "JPY" not in sym else 3),
                            limit_level=round(target, 5 if "JPY" not in sym else 3))
                        new_pos["deal_id"] = (conf.get("affectedDeals") or [{}])[0].get("dealId")
                        new_pos["ig_size"] = size
                        ig_note = f" ig:{conf.get('dealStatus')} deal={new_pos['deal_id']}"
                    except IGError as e:
                        lines.append(f"{sym:<10} {px} IG ORDER FAILED: {e}")
                        continue
                state["positions"][sym] = new_pos
                state["block"].pop(sym, None)
                tw.writerow([utc_now(), sym, "open",
                             "long" if d_sig > 0 else "short",
                             fmt_price(sym, entry), f"{units:.2f}", "",
                             f"{state['balance']:.2f}",
                             f"EMA9/21 cross {'LONG' if d_sig > 0 else 'SHORT'} | "
                             f"stop={fmt_price(sym, stop)} "
                             f"tp={fmt_price(sym, target)} | "
                             f"atr={a:.5f}{ig_note}"])
                side = "LONG " if d_sig > 0 else "SHORT"
                lines.append(
                    f"{sym:<10} {px} OPEN {side} units={units:,.0f} "
                    f"entry={fmt_price(sym, entry)} "
                    f"stop={fmt_price(sym, stop)} tp={fmt_price(sym, target)}"
                    f"{ig_note}")
                just_reported = True

        # ---- 3) status ----
        if not just_reported:
            arrow = f"dir={d_sig:+d}"
            if pos:
                fl_nzd = floating(kind, pos["entry"], price, pos["d"],
                                  pos["units"]) / nzdusd
                side = "LONG " if pos["d"] > 0 else "SHORT"
                lines.append(f"{sym:<10} {px} {side} "
                             f"units={pos['units']:,.0f} pnl={fl_nzd:+.2f} "
                             f"{arrow}")
            else:
                note = f" (blocked {block:+d})" if block else ""
                lines.append(f"{sym:<10} {px} flat {arrow}{note}")

    equity = state["balance"]
    for sym, p in state["positions"].items():
        _, kind = pair_kind(sym)
        equity += floating(kind, p["entry"], p["mark"], p["d"], p["units"]) / nzdusd
    state["equity"] = equity
    ew.writerow([utc_now(), f"{state['balance']:.2f}",
                 f"{equity:.2f}", len(state["positions"])])
    tf.flush()
    ef.flush()
    return lines


def seed_from_log(state):
    """Resume paper positions/cash from trades_ig.csv so restarts continue
    cleanly instead of starting flat (which would duplicate paper entries)."""
    import re as _re
    if not os.path.exists(TRADES_CSV):
        return 0
    n = 0
    with open(TRADES_CSV, newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        sym = r.get("symbol", "")
        if r.get("action") == "close":
            state["positions"].pop(sym, None)
            state["closed"] += 1
            try:
                state["closed_pnl"] += float(r.get("pnl_nzd") or 0)
            except ValueError:
                pass
        elif r.get("action") == "open":
            try:
                entry = float(r.get("price") or 0)
                units = float(r.get("units") or 0)
            except ValueError:
                continue
            if entry <= 0 or units <= 0:
                continue
            m = _re.search(r"stop=([\d.]+)\s+tp=([\d.]+)", r.get("reason") or "")
            if m:
                stop, target = float(m.group(1)), float(m.group(2))
            else:
                stop, target = None, None  # hold: exits only on signal flip
            state["positions"][sym] = {
                "d": 1 if r.get("side") == "long" else -1,
                "entry": entry, "stop": stop, "target": target,
                "units": units, "mark": entry}
            n += 1
    if rows:
        try:
            state["balance"] = float(rows[-1].get("balance_nzd") or state["balance"])
        except ValueError:
            pass
    return len(state["positions"])


def main():
    ap = argparse.ArgumentParser(description="FX strategy on IG demo (NZD). "
                                 "Dry-run unless --live.")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--poll", type=int, default=None)
    ap.add_argument("--live", action="store_true",
                    help="place REAL DEMO orders on IG (demo money, real fills)")
    ap.add_argument("--size", type=float, default=None,
                    help="IG deal size for live orders (default: market minimum)")
    args = ap.parse_args()
    if os.environ.get("LIVE", "") == "1":
        args.live = True  # container-friendly switch (Portainer env var)

    with open(args.config) as f:
        cfg = json.load(f)

    try:
        key, user, pwd, acct = load_credentials()
    except IGError as e:
        print(f"no credentials: {e}")
        sys.exit(2)
    client = IGClient(key, user, pwd, acct)
    try:
        login = client.login()
    except IGError as e:
        print(f"IG login FAILED: {e}")
        sys.exit(1)
    bal = client.account_balance()
    if bal.get("balance"):
        start_bal = float(bal["balance"])
    else:
        start_bal = float(cfg["account"]["starting_balance"])

    mode = "LIVE DEMO ORDERS" if args.live else "DRY-RUN (no orders)"
    print("=" * 62)
    print(f" IG FX BOT [{mode}] — account {start_bal:,.0f} "
          f"{bal.get('currency', cfg['account']['currency'])}")
    print(f" strategy: EMA {cfg['strategy']['ema_fast']}/"
          f"{cfg['strategy']['ema_slow']} cross, "
          f"ATR({cfg['strategy']['atr_period']}) "
          f"{cfg['strategy']['atr_stop_mult']}x stop / "
          f"{cfg['strategy']['atr_target_mult']}x target")
    print("=" * 62)

    epics = resolve_all(client, cfg["pairs"])
    print("IG markets:")
    for sym, s in epics.items():
        print(f"  {sym:<10} {s['epic']} min_size={s['min_size']} "
              f"contract={s['contract']}")

    state = {"balance": start_bal, "equity": start_bal, "positions": {},
             "block": {}, "closed": 0, "closed_pnl": 0.0, "nzdusd": None}
    resumed = seed_from_log(state)
    if resumed:
        print(f"resumed {resumed} paper position(s) from {TRADES_CSV}")
    warned = set()
    poll = args.poll or cfg["live"].get("poll_seconds", 600)

    tf, tw = ensure_csv(TRADES_CSV,
                        ["time", "symbol", "action", "side", "price",
                         "units", "pnl_nzd", "balance_nzd", "reason"])
    ef, ew = ensure_csv(EQUITY_CSV,
                        ["time", "balance_nzd", "equity_nzd", "open"])
    fails = 0
    try:
        while True:
            try:
                lines = cycle(cfg, client, epics, state, tf, tw, ef, ew,
                              warned, args.live, args.size)
            except IGError as e:
                fails += 1
                print(f"cycle failed ({fails}x): {e}")
                if fails >= 5:
                    print("too many consecutive failures, exiting")
                    break
                time.sleep(60 * fails)  # back off, don't hammer the API
                continue
            except Exception as e:
                print(f"unexpected cycle error: {type(e).__name__}: {e}")
                break
            fails = 0
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
        print(f"\nrun summary: {state['closed']} closed, "
              f"realised {state['closed_pnl']:+.2f} NZD, "
              f"balance {state['balance']:.2f}, equity {state['equity']:.2f}")
        tf.close()
        ef.close()


if __name__ == "__main__":
    main()
