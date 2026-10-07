# sniper-bot

Copies a trader's **long option** alerts (calls and puts) from a Telegram group into a Robinhood account:
buy the contract when he buys, sell it when he sells. One contract per alert, with hard limits on price,
size and total money at risk. It ignores shares, cash-secured puts, covered calls and spreads.

> **Unofficial and risky.** It uses `robin_stocks`, an unofficial Robinhood client that may break or breach
> Robinhood's terms. Live mode places real orders with real money. Nothing here is financial advice.

## What it does
- Reads alerts live from Telegram (Telethon) and understands his formats, typos and corrections.
- Checks the real quote on Robinhood before every buy; skips wide spreads (after a 60 s re-check), stale alerts,
  prices far from his, and a second contract on a stock it already holds.
- Late entry by expiry (0DTE 10 min, 1-7 days 2 min, swings 60 min if he hasn't sold), watch-and-buy for options
  more than 3 days out (up to 60 min), take-profit at +40%, emergency stop at -50%, expiry-day close at 3:30 PM.
- A hard **project pot** (default $5,000): when it is lost, buying stops for good.
- Always runs a **paper mode** (logs only) unless `LIVE_TRADING=true`.
- Records the price path of every contract he buys, for later analysis.

## Folder layout
```
sniper_shadow.py        the bot (one file)
requirements.txt
.env.example            copy to .env and fill in (never commit .env)
docs/SETUP.md           first-time setup
docs/LIVE.md            going live: checklist, controls, what the alerts mean
tools/migrate_layout.py one-time move from the old flat layout
data/                   the bot's memory and logins (git-ignored, never share)
  state.json            positions, the pot, price watches (state.json.bak is its spare copy)
  robinhood.pickle      saved Robinhood login
  sniper_session.session  saved Telegram login
  live_armed.txt        written by --test-order; allows live orders for that day
runs/<YYYY-MM-DD>/      one folder per trading day (git-ignored)
  trades_log.csv        every alert and what the bot decided
  price_paths.csv       price of every contract he bought, over time
  orders_log.csv        every real order sent (live mode)
  stalls.csv            only if the bot froze for over a minute
STOP_TRADING            create this empty file to stop new buys (exits keep working)
```

## Run it
```
python -m venv venv                      (once, outside or inside the repo)
venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env                   (then fill in)
python sniper_shadow.py --selftest       (checks the logic, no logins)
python sniper_shadow.py                  (start the bot, about 9:15 AM)
python sniper_shadow.py --test-order "SPCX 172.5C 10/30"   (arms live orders for today)
```
Details: `docs/SETUP.md` and `docs/LIVE.md`.

## Never commit
`.env`, `data/` (logins), `*.session`, `*.pickle`. `.gitignore` already excludes them; check `git status`
before every push.
