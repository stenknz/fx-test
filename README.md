# fx-bot — paper FX trading on IG demo (NZD)

Paper-trading harness for FX strategies against a live **IG demo account**.
Pure Python standard library — nothing to install. Real IG dealing prices,
simulated (dry-run) or demo-money fills. No live/real-money support by design.

> ⚠️ **Educational tool, not financial advice.** Both included strategies
> lose money in backtests more often than not (results below). Never point
> this at a real-money account.

## Quick start

```bash
cd ~/Projects/fx-bot

# 0. Credentials (never paste in chat): env IG_API_KEY/IG_USERNAME/IG_PASSWORD
#    or ~/.hermes/ig_demo.json (mode 0600) {"api_key":..,"username":..,"password":..}
cp ig_demo_template.json ~/.hermes/ig_demo.json && chmod 600 ~/.hermes/ig_demo.json

# 1. Verify the IG demo login + live prices
python3 check_ig.py

# 2. Backtests
python3 backtest.py                      # EMA cross, hourly, all pairs
python3 backtest_nowick.py --symbol AUDUSD=X --range 1mo   # Nowick, 15m

# 3. Strategy loop, dry-run (default: prints signals, places NO orders)
python3 bot_ig.py --once                 # single cycle
python3 bot_ig.py                        # loop until Ctrl+C (10 min poll)

# 4. Dashboard (read-only): http://localhost:8082
python3 dashboard.py

# 5. Close all paper positions at live prices (bookkeeping only)
python3 close_paper.py

# 6. Live DEMO orders (demo money, real fills) — explicit flag required
python3 bot_ig.py --live --once
```

Paper logs: `trades_ig.csv` (every open/close with reason, stop, TP) and
`equity_ig.csv` (balance/equity/open-count each cycle). Set `DATA_DIR` to
point the logs elsewhere (the NAS setup uses `/data`).

## Files

| File | Purpose |
|---|---|
| `config.json` | Pairs, strategy, risk, intervals (account is NZD 20k) |
| `ig_client.py` | IG demo REST client (session auth, prices, orders) |
| `bot_ig.py` | Strategy loop on IG hourly bars (dry-run unless `--live`) |
| `strategy.py` | EMA, ATR (Wilder), crossover direction series |
| `strategies/nowick.py` | Nowick (No-Wick) compensation-play signal logic |
| `trading.py` | Sizing + P&L math (USD legs, converted to NZD at live rate) |
| `dashboard.py` | Read-only web dashboard (equity curve, positions, trades) |
| `check_ig.py` | Read-only IG login/price check |
| `close_paper.py` | Close paper positions at live mids (log only) |
| `backtest.py` / `backtest_nowick.py` | Historical simulators (conservative fills) |
| `Dockerfile`, `docker-compose.yml` | NAS 24/7 deployment (Portainer stack) |

## Strategies

**EMA 9/21 cross (hourly)** — long/short on closed-bar crosses, 1.5×ATR
stop / 2.5×ATR target, 1% risk, max 2 positions, 5× leverage cap, no
re-entry after a stop until the signal flips.

**Nowick compensation play (15m, per Bard FX docs)** — body-breakout trend,
no-wick signal candle, limit entry at the candle origin valid 9 bars,
stop at recent structure ± 3-pip buffer, 1R target, SL-or-TP-only
management, entries 06:00–12:00 UTC (≈08:00–14:00 Sweden summer).

## Honest backtest results

EMA cross, 1y hourly, 1 pip spread: 4 of 5 pairs lost (only USDJPY +22%).
Nowick, 1mo 15m, 1R, 1 pip spread: AUDUSD −2.8R, GBPUSD −8.0R, USDJPY −0.7R
(win rates 36–47% — needs 50%+ at 1:1). Neither strategy has earned trust
yet; the harness exists to test changes honestly.

Known simulation simplifications: backtests fill at the next bar's open
(the live loop enters immediately on the closed-bar signal); exits are
evaluated at mid-price while entries pay the spread side; Yahoo data has
no real spread, slippage, swaps, or execution risk.

The dashboard has no login — it's a read-only LAN page. Don't expose
port 8082 beyond your home network / Tailscale.

## NAS 24/7 (Asustor + Portainer)

1. Copy this folder to `/volume1/Docker/fx-bot` on the NAS.
2. SSH into the NAS and build + start it:
   `cd /volume1/Docker/fx-bot && docker compose up -d --build`
   (Portainer Stacks can't build images, so the command line does this bit;
   Portainer still shows and manages the container afterwards.)
   Set the three `IG_*` env vars (demo values only) before deploying.
3. Dashboard at `http://<nas-ip>:8082`, logs in `./data`.
4. For demo-money orders later: append `--live` to the bot command in
   the `Dockerfile` CMD — no other change needed.
