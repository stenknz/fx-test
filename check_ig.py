#!/usr/bin/env python3
"""IG demo connection check (read-only, stdlib only).

Reads credentials from env (IG_API_KEY/IG_USERNAME/IG_PASSWORD[/IG_ACCOUNT_ID])
or ~/.hermes/ig_demo.json (mode 0600). Prints masked account info, demo
balance, open positions, and live bid/ask for the 5 bot pairs.

Usage:
    python3 check_ig.py
"""
import os
import sys

from ig_client import IGError, IGClient, load_credentials

PAIRS = ["EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDJPY"]


def mask(s):
    s = str(s or "")
    return s[:2] + "***" + s[-2:] if len(s) > 5 else "***"


def main():
    try:
        key, user, pwd, acct = load_credentials()
    except IGError as e:
        print(f"no credentials: {e}")
        return 2
    c = IGClient(key, user, pwd, acct)
    try:
        login = c.login()
    except IGError as e:
        print(f"login FAILED: {e}")
        print("Check: API key enabled for DEMO (not live), username/password correct,")
        print("and IG demo account unlocked (log into the IG web platform once).")
        return 1
    print(f"login OK | account {mask(login['account_id'])} (demo)")
    bal = c.account_balance()
    print(f"balance {bal.get('balance')} {bal.get('currency')} | "
          f"available {bal.get('available')}")
    pos = c.positions()
    print(f"open positions: {len(pos)}")
    for p in pos:
        d = p.get("position", {})
        print(f"  {d.get('epic')} {d.get('direction')} size={d.get('size')} "
              f"open={d.get('openLevel')} deal={mask(d.get('dealId'))}")
    print("\nFX snapshots (IG demo dealing prices):")
    for pair in PAIRS:
        try:
            epic, name, expiry = c.resolve_epic(pair)
            bid, offer = c.snapshot(epic)
            dec = 3 if "JPY" in pair else 5
            print(f"  {pair:<8} bid={bid:.{dec}f} ask={offer:.{dec}f} "
                  f"| {epic} [{name} | {expiry}]")
        except IGError as e:
            print(f"  {pair:<8} ERROR: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
