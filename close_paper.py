#!/usr/bin/env python3
"""Close all open PAPER positions at live IG mid prices (dry-run bookkeeping).

Appends `close` rows with reason `manual_close` to trades_ig.csv. Touches
nothing on IG — the demo account is unaffected. Run while the bot loop is
stopped to avoid it appending interleaved rows.
"""

import csv
import datetime as dt
import os
import sys

sys.path.insert(0, ".")
from ig_client import IGClient, load_credentials
from trading import floating, pair_kind

DATA_DIR = os.environ.get("DATA_DIR", ".")
TRADES_CSV = os.path.join(DATA_DIR, "trades_ig.csv")


def utc_now():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def open_positions():
    pos = {}
    if not os.path.exists(TRADES_CSV):
        return pos, 0.0
    with open(TRADES_CSV, newline="") as f:
        for r in csv.DictReader(f):
            sym = r.get("symbol", "")
            if r.get("action") == "open":
                pos[sym] = {"side": r.get("side"), "entry": r.get("price"),
                            "units": r.get("units")}
            elif r.get("action") == "close":
                pos.pop(sym, None)
    bal = 0.0
    with open(TRADES_CSV, newline="") as f:
        rows = list(csv.DictReader(f))
        if rows:
            try:
                bal = float(rows[-1].get("balance_nzd") or 0)
            except ValueError:
                pass
    return pos, bal


def main():
    pos, bal = open_positions()
    if not pos:
        print("no open paper positions.")
        return
    key, user, pwd, acct = load_credentials()
    client = IGClient(key, user, pwd, acct)
    client.login()
    syms = list(pos.keys())
    epics = {s: client.resolve_epic(s[:-2] if s.endswith("=X") else s)[0]
             for s in syms}
    nzbid, nzask = client.snapshot(epics.get("NZDUSD=X") or
                                   client.resolve_epic("NZDUSD")[0])
    nzdusd = (nzbid + nzask) / 2
    total = 0.0
    with open(TRADES_CSV, "a", newline="") as f:
        w = csv.writer(f)
        for sym, p in pos.items():
            bid, ask = client.snapshot(epics[sym])
            mark = (bid + ask) / 2
            _, kind = pair_kind(sym)
            d = 1 if p["side"] == "long" else -1
            entry, units = float(p["entry"]), float(p["units"])
            p_nzd = floating(kind, entry, mark, d, units) / nzdusd
            bal += p_nzd
            total += p_nzd
            dec = 3 if "JPY" in sym else 5
            w.writerow([utc_now(), sym, "close", p["side"], f"{mark:.{dec}f}",
                        f"{units:.2f}", f"{p_nzd:.2f}", f"{bal:.2f}",
                        "manual_close"])
            print(f"{sym:<10} closed {p['side']} {units:,.0f} @ {mark:.{dec}f} "
                  f"pnl={p_nzd:+.2f} NZD")
    print(f"closed {len(pos)}, total {total:+.2f} NZD, balance {bal:.2f} NZD")


if __name__ == "__main__":
    main()
