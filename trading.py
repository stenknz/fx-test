"""Position sizing and P&L math for USD-account FX pairs.

Two quote conventions (crosses like EURJPY without a USD leg are refused):

  quote_usd  (EURUSD): price = USD per base unit.
      P&L(USD) = delta_price * units, so risk per unit = stop distance.
      units = risk / stop_dist

  base_usd   (USDJPY): price = quote per USD.
      P&L(USD) = delta_price * units / exit_price, so at the stop:
      units = risk * stop_price / stop_dist  (exact at the stop price)
"""


def pair_kind(symbol):
    base = symbol[:-2] if symbol.endswith("=X") else symbol
    if len(base) != 6 or not base.isalpha():
        return base, "cross"
    c1, c2 = base[:3].upper(), base[3:].upper()
    if c2 == "USD":
        return base, "quote_usd"
    if c1 == "USD":
        return base, "base_usd"
    return base, "cross"


def pip_size(symbol):
    return 0.01 if "JPY" in symbol.upper() else 0.0001


def size_units(kind, risk_usd, stop_dist, stop_price, entry_price,
               max_leverage, equity):
    """Units of base (quote_usd) or USD notional (base_usd) risking risk_usd
    at the stop. Capped by max_leverage. Returns 0.0 if unsupported."""
    if stop_dist <= 0 or risk_usd <= 0:
        return 0.0
    if kind == "quote_usd":
        units = risk_usd / stop_dist
        notional = units * entry_price
    elif kind == "base_usd":
        units = risk_usd * stop_price / stop_dist
        notional = units
    else:
        return 0.0
    max_notional = equity * max_leverage
    if max_notional > 0 and notional > max_notional:
        units *= max_notional / notional
    return units


def pnl(kind, entry, exit_price, direction, units):
    """Realised P&L in USD for a closed position."""
    delta = (exit_price - entry) * direction
    if kind == "quote_usd":
        return delta * units
    if kind == "base_usd":
        if exit_price == 0:
            return 0.0
        return delta * units / exit_price
    return 0.0


def floating(kind, entry, mark, direction, units):
    """Unrealised P&L in USD at the given market mark."""
    return pnl(kind, entry, mark, direction, units)
