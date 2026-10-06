# Going live: checklist

Live mode sends REAL orders from your Robinhood account. It is off until you set `LIVE_TRADING=true`.
Nothing here has been tested against Robinhood's real order system (only against a pretend one),
so treat the first day as a trial and watch it.

## What it does in live mode
- **Size:** 1 contract per alert. Lotto = his red double-exclamation (or the word "lotto"); lottos are only copied if priced under 2.50.
- **Buy:** limit order at the ask plus one price step, never above his price +10%. Waits up to 15 s; if it doesn't fill, cancels and checks the final state. No position is recorded unless it actually filled.
- **Sell:** when he sells (half or more of his position -> we sell our 1 contract; ALL OUT -> sell). Limit order at the bid; if not filled in 8 s it is cancelled and retried one price step lower, again and again, until it is out.
- **Late entry, by expiry:** an alert is still copied if the bot sees it late (it was asleep or offline), as long as the ask is still within his price +10%: up to **10 minutes** for 0DTE (`LATE_ENTRY_MIN`), **2 minutes** for options 1-7 days out (`LATE_ENTRY_MIN_SHORT`), and **60 minutes** for swings 8+ days out (`LATE_ENTRY_MIN_SWING`). A late swing is only bought if he hasn't sold any of it: the bot looks again 15 s later (any sell he posted in the meantime arrives first and cancels it). Alerts the bot only sees after a restart are never bought.
- **Wide spread:** if the spread is wider than 15% when he posts, the bot re-checks for **60 seconds** (`SPREAD_RECHECK_SEC`) instead of skipping, and buys if it narrows and every other rule still passes. If it narrows but the ask is above the cap, it becomes a normal watch (below).
- **One position per stock:** while it holds one of his contracts on a stock, it skips his other contracts on that same stock (`ONE_POSITION_PER_STOCK=true`).
- **Watch-and-buy:** if the ask is above the cap when he posts, and the option expires **more than 3 days** out, the bot keeps checking every 10 s for **60 minutes** and buys if the ask comes back within the cap. It stops watching the moment he posts any sell on that contract, when the hour is up, or at the close. All the usual rules (price limits, pot, max positions, STOP_TRADING) are checked again at the moment of buying. Watches survive a restart. `WATCH_MIN=0` turns it off; `WATCH_MIN_DTE` sets the 3-day rule. The heartbeat shows `watching N`.
- **Take profit:** as soon as the bid is more than 40% above what we paid, it sells (it does not wait for him). `TAKE_PROFIT_PCT=0` turns this off.
- **Emergency stop:** if the mid price falls 50% below what we paid, it sells (he often posts no exit on losers).
- **The $5,000 project pot:** the project starts with `PROJECT_CAPITAL=5000`. Only real (live) trades count: each closed trade's profit or loss is added to or taken from the pot. A buy is skipped if its cost is more than the *free* pot (pot minus what is already tied up in open positions), so you can never have more than the pot at risk. You get a Telegram message when 50%, 75% and 90% of the pot is lost.
- **End of project:** when the pot reaches $0 the bot prints and sends **PROJECT ENDED** and never buys again. Open positions are still managed and sold. The pot survives restarts (it lives in `data/state.json`, don't delete it). To run a new project, raise `PROJECT_CAPITAL` in `.env` and restart.
- Fees are not counted in the pot, so the real figure can be a few dollars worse than the bot's.
- **Expiry day:** anything still open at 3:30 PM on its expiry date is sold.
- **Never touches** positions it didn't buy itself.
- Every order is logged in `runs/<date>/orders_log.csv`; every decision in `runs/<date>/trades_log.csv`.

## Before the first live day (tonight)
1. In the Robinhood app, check Account -> options trading is enabled, and look at how day trading is handled on your account type (0DTE alerts mean same-day round trips; a cash account also has settlement limits). If Robinhood blocks an exit, the bot will keep retrying and tell you.
2. Put enough buying power in the account. One contract is usually $100-$1,000.
3. Save the new `sniper_shadow.py` over the old one. Add the new lines from `.env.example` to your `.env` (keep your own keys), including `TAKE_PROFIT_PCT=40`, `PROJECT_CAPITAL=5000`, `MAX_DEPLOYED=5000` and `EXIT_CHECK_SEC=10`. Set `LIVE_TRADING=true` only when you are ready to run live.
4. Optional but recommended, for phone push alerts (Saved Messages does not buzz your phone):
   - In Telegram, message **@BotFather**, send `/newbot`, follow the prompts, copy the token into `TG_BOT_TOKEN`.
   - Open your new bot and send it any message.
   - In a browser open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy the number after `"chat":{"id":` into `TG_NOTIFY_CHAT_ID`.
5. Run `python .\sniper-bot\sniper_shadow.py --selftest` and check it ends with "all live-flow checks passed".

## Each live morning
1. About 9:15, start the bot as usual. It asks you to type `LIVE`. It will say **NOT ARMED**.
2. After 9:30 AM, open a second terminal and run the test with any contract he is in that has a bid of 0.50 or more:
   ```
   python .\sniper-bot\sniper_shadow.py --test-order "SPCX 172.5C 10/30"
   ```
   It places a 1-contract buy at half the bid (it can't fill), reads it back, cancels it, and confirms the cancel. All PASS -> live orders are armed for today. Until then, buys are skipped.
3. Watch the first trades. Keep the Robinhood app open.

## Controls
- **Stop new buys now:** create an empty file named `STOP_TRADING` in the bot folder. Exits keep working. Delete the file to resume.
- **Stop everything:** Ctrl+C. Anything already open stays open in Robinhood.
- **Back to paper mode:** set `LIVE_TRADING=false` and restart.

## If you see
- `!! NOT SOLD YET` - the exit hasn't filled; it keeps retrying lower. If it repeats, sell by hand in Robinhood.
- `!! ... STATE UNKNOWN` - the bot could not confirm an order's final state. New buys are halted. Open Robinhood and check or cancel the order, then restart the bot.
- `Start-up check: ...` - the bot compared its books with Robinhood and found a difference. It never touches positions it didn't buy.
