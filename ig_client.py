"""IG Markets demo REST client (stdlib only, no pip dependencies).

Demo base URL: https://demo-api.ig.com/gateway/deal
Auth (session v2): POST /session with X-IG-API-KEY + identifier/password
  -> response headers CST + X-SECURITY-TOKEN, reused on every call.

Credentials are NEVER pasted in chat. They load from (in order):
  env vars IG_API_KEY / IG_USERNAME / IG_PASSWORD / IG_ACCOUNT_ID (optional)
  or JSON file ~/.hermes/ig_demo.json  {"api_key":..,"username":..,"password":..,"account_id":..}
"""

import calendar
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

DEMO_BASE = "https://demo-api.ig.com/gateway/deal"
LIVE_BASE = "https://api.ig.com/gateway/deal"  # unused: demo only


class IGError(RuntimeError):
    pass


def load_credentials(path=None):
    """Return (api_key, username, password, account_id|None). Raises if missing."""
    api_key = os.environ.get("IG_API_KEY")
    username = os.environ.get("IG_USERNAME")
    password = os.environ.get("IG_PASSWORD")
    account_id = os.environ.get("IG_ACCOUNT_ID")
    if api_key and username and password:
        return api_key, username, password, account_id
    cfg_path = path or os.path.expanduser("~/.hermes/ig_demo.json")
    if os.path.exists(cfg_path):
        mode = os.stat(cfg_path).st_mode & 0o777
        if mode & 0o077:
            print(f"warning: {cfg_path} is group/other-readable "
                  f"(mode {oct(mode)}); run: chmod 600 {cfg_path}")
        with open(cfg_path) as f:
            cfg = json.load(f)
        api_key = api_key or cfg.get("api_key")
        username = username or cfg.get("username")
        password = password or cfg.get("password")
        account_id = account_id or cfg.get("account_id")
    if not (api_key and username and password):
        raise IGError(
            "IG credentials not found. Set env IG_API_KEY/IG_USERNAME/IG_PASSWORD "
            f"or create {cfg_path} (mode 0600) with "
            '{"api_key":..,"username":..,"password":..}. Never paste secrets in chat.'
        )
    return api_key, username, password, account_id


class IGClient:
    def __init__(self, api_key, username, password, account_id=None,
                 base=DEMO_BASE, timeout=20):
        self.api_key = api_key
        self.username = username
        self.password = password
        self.account_id = account_id
        self.base = base
        self.timeout = timeout
        self.cst = None
        self.token = None

    # ---- low level ----
    def _req(self, method, path, body=None, version="1"):
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        headers = {
            "Content-Type": "application/json; charset=UTF-8",
            "Accept": "application/json; charset=UTF-8",
            "X-IG-API-KEY": self.api_key,
            "Version": str(version),
        }
        if self.cst:
            headers["CST"] = self.cst
        if self.token:
            headers["X-SECURITY-TOKEN"] = self.token
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode()
                heads = {k.lower(): v for k, v in resp.headers.items()}
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode()[:500]
            except Exception:
                detail = ""
            raise IGError(f"{method} {path} -> HTTP {e.code}: {detail}")
        except Exception as e:  # URLError, timeout, DNS: never leak raw
            raise IGError(f"{method} {path} -> network error "
                          f"({type(e).__name__}: {e})")
        try:
            return (json.loads(raw) if raw else {}), heads
        except Exception as e:
            raise IGError(f"{method} {path} -> bad response: {e}")

    # ---- auth ----
    def login(self):
        body, heads = self._req("POST", "/session", {
            "identifier": self.username,
            "password": self.password,
        }, version="2")
        self.cst = heads.get("cst")
        self.token = heads.get("x-security-token")
        if not (self.cst and self.token):
            raise IGError("login failed: no CST/X-SECURITY-TOKEN returned")
        current = body.get("currentAccountId") or body.get("currentAccount", {}).get("id")
        if self.account_id and self.account_id != current:
            self.set_account(self.account_id)
            current = self.account_id
        else:
            self.account_id = current
        body.pop("ipBanInfo", None)
        return {"account_id": current, "info": body}

    def set_account(self, account_id):
        self._req("PUT", "/session", {"accountId": account_id,
                                      "defaultAccount": True}, version="1")
        self.account_id = account_id

    # ---- read-only ----
    def accounts(self):
        body, _ = self._req("GET", "/accounts", version="1")
        return body.get("accounts", [])

    def account_balance(self):
        for a in self.accounts():
            if a.get("accountId") == self.account_id or not self.account_id:
                bal = a.get("balance", {}) or {}
                return {"account_id": a.get("accountId"),
                        "balance": bal.get("balance"),
                        "available": bal.get("available"),
                        "currency": a.get("currency")}
        return {}

    def resolve_epic(self, pair6):
        """Map 'EURUSD' -> tradeable IG epic via market search. Returns (epic, name, expiry)."""
        q = urllib.parse.quote(pair6)
        body, _ = self._req("GET", f"/markets?searchTerm={q}", version="1")
        mkts = body.get("markets", [])
        cands = [m for m in mkts if m.get("marketStatus") == "TRADEABLE"
                 and pair6 in (m.get("epic", "") + m.get("instrumentName", ""))]
        if not cands:
            raise IGError(f"no tradeable IG market found for {pair6} "
                          f"(searched {len(mkts)} markets)")
        # Prefer spot-style epics (DFB/CFD/MINI) over dated forwards/options.
        def rank(m):
            e = m.get("epic", "")
            return (0 if any(k in e for k in ("DFB", "CFD", "MINI")) else 1,
                    len(e))
        cands.sort(key=rank)
        best = cands[0]
        return best["epic"], best.get("instrumentName"), best.get("expiry")

    def snapshot(self, epic):
        """Return (bid, offer) for an epic."""
        body, _ = self._req("GET", f"/markets/{urllib.parse.quote(epic, safe='')}",
                            version="2")
        snap = body.get("snapshot", {}) or {}
        try:
            return float(snap["bid"]), float(snap["offer"])
        except (KeyError, TypeError, ValueError):
            raise IGError(f"malformed snapshot for {epic}")

    def prices(self, epic, resolution="HOUR", n=40):
        """Return list of bars [{t,o,h,l,c}] as mid-price hourly bars + latest time str."""
        path = (f"/prices/{urllib.parse.quote(epic, safe='')}?resolution={resolution}"
                f"&max={n}&pageSize={n}")
        body, _ = self._req("GET", path, version="3")
        out = []
        for p in body.get("prices", []):
            try:
                st = p.get("snapshotTime", "")
                # "2026/09/22 12:00:00" is UTC for FX; timegm treats it as such
                # on any host timezone (mktime would misread on non-UTC hosts).
                t = calendar.timegm(time.strptime(st, "%Y/%m/%d %H:%M:%S"))
                o = (p["openPrice"]["bid"] + p["openPrice"]["ask"]) / 2
                h = (p["highPrice"]["bid"] + p["highPrice"]["ask"]) / 2
                l = (p["lowPrice"]["bid"] + p["lowPrice"]["ask"]) / 2
                c = (p["closePrice"]["bid"] + p["closePrice"]["ask"]) / 2
                out.append({"t": t, "o": o, "h": h, "l": l, "c": c})
            except (KeyError, TypeError, ValueError):
                continue
        out.sort(key=lambda b: b["t"])  # oldest-first: signal order matters
        return out

    def positions(self):
        body, _ = self._req("GET", "/positions", version="2")
        return body.get("positions", [])

    # ---- orders (demo; gated by callers behind --live) ----
    def open_otc(self, epic, direction, size, stop_level=None,
                 limit_level=None, currency="USD", reference=None):
        ref = reference or f"FXBOT{int(time.time())}"
        body = {"epic": epic, "direction": direction, "size": size,
                "orderType": "MARKET", "currencyCode": currency,
                "guaranteedStop": False, "dealReference": ref}
        if stop_level:
            body["stopLevel"] = stop_level
        if limit_level:
            body["limitLevel"] = limit_level
        resp, _ = self._req("POST", "/positions/otc", body, version="2")
        return self._confirm(resp.get("dealReference", ref))

    def close_position(self, deal_id, direction, size):
        resp, _ = self._req("DELETE", f"/positions/otc/{deal_id}",
                            {"dealId": deal_id, "direction": direction,
                             "size": size, "orderType": "MARKET"}, version="1")
        return self._confirm(resp.get("dealReference", ""))

    def _confirm(self, deal_ref, tries=6):
        last = {}
        for _ in range(tries):
            try:
                body, _ = self._req(
                    "GET", f"/confirms/{urllib.parse.quote(deal_ref, safe='')}",
                    version="1")
            except IGError as e:
                last = {"dealStatus": "PENDING", "reason": str(e)[:120]}
                time.sleep(2)
                continue
            if body.get("dealStatus") in ("ACCEPTED", "REJECTED"):
                return body
            last = body
            time.sleep(2)
        raise IGError(f"confirm timeout for {deal_ref}: {last}")
