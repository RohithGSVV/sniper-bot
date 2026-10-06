# Sniper alerts bot: shadow-mode setup

Shadow mode reads the Telegram alerts live and checks real Robinhood prices. It logs what it *would* do, and it never places an order. Doing steps 1–6 tonight leaves only step 7 for the morning.

## 1. Install Python
- **Windows:** install Python 3.12 from python.org. On the first installer screen, tick **"Add python.exe to PATH"**.
- **Mac:** install Python 3.12 from python.org, or run `brew install python`.

## 2. Install the bot
Unzip the `sniper-bot` folder into Documents, then open a terminal in that folder:
- **Windows:** right-click the folder and choose "Open in Terminal".
- **Mac:** open Terminal and type `cd ~/Documents/sniper-bot`.

Then run:

```
python -m venv venv
venv\Scripts\activate          (Windows)
source venv/bin/activate       (Mac)
pip install -r requirements.txt
```

On a Mac, use `python3` if `python` isn't found. You'll repeat the `activate` line each time you open a new terminal.

## 3. Get your Telegram API keys
1. Go to **my.telegram.org** and log in with your phone number. The code arrives inside the Telegram app.
2. Open **API development tools** and fill in the form:
   - **App title:** `sniper reader`
   - **Short name:** `sniperreader`
   - **Platform:** Desktop
3. Copy **api_id** and **api_hash**.
4. Copy `.env.example` to a new file named `.env` and paste both values in.

## 4. Create the test watchlist (optional)
In the Robinhood app, create a list named exactly **Sniper Test**. The bot adds each alerted ticker (MU, NBIS…) there so you can see it working.

## 5. Check the alert reader (no logins)
```
python sniper_shadow.py --selftest
```
This replays Oct 1's alerts with made-up prices. You should see WOULD BUY / SKIP / HOLD / WOULD SELL lines and a summary.

## 6. Connect Telegram and find the group
```
python sniper_shadow.py --list-chats
```
- The first time, it asks for your phone number (+1…), then the code Telegram sends you, then your Telegram password if you use two-step verification.
- It then prints your groups. Copy the number next to **Sniper Trades** into `TG_CHAT_ID` in `.env`.
- Telegram will list the bot as a new device under Settings → Devices. That's expected.

## 7. Each trading day (observation week: through Wed Oct 7)
Start it by about **9:15 AM**, from `D:\WorkSpace\sniper-bot` with the venv active:
```
python .\sniper-bot\sniper_shadow.py
```
- **Robinhood login:** it reuses the saved session. It only asks for your email and password if the session has expired (about once a day), and you may need to approve in the Robinhood app.
- **Catch-up:** on startup it reads every message posted while it was off, then starts following his open contracts. The first run reads the last 7 days.
- **Leave it running until after 4:05 PM.** A summary posts to Saved Messages, then press Ctrl+C. Restart it the next morning.
- **Keep the PC awake:** keep it plugged in with the lid open. In Settings → System → Power & battery, set both "turn off my screen" and "put my device to sleep" to Never when plugged in, and in Control Panel → Power Options → "Choose what closing the lid does", pick "Do nothing" when plugged in. The bot keeps the screen on while it runs (on laptops with Modern Standby, the screen timing out is what puts the PC to sleep), but closing the lid or a flat battery still sleeps it, and nothing protects an open position while it sleeps.
- **Heartbeat:** every 5 minutes it prints an `alive` line showing whether Telegram is connected and how many contracts it's tracking.

## What gets recorded
- **runs/<date>/trades_log.csv:** every alert, with the time he posted it, the delay before the bot saw it, the expiry group (0DTE / short / swing), the real bid/ask, and what the bot would have done.
- **runs/<date>/price_paths.csv:** for **every** contract he buys, including ones the bot would skip, the bid, ask and mark every 30 seconds in market hours. It also records the stock price, implied volatility and delta, and his buy/sell events marked on the timeline. Recording runs from his buy until 30 minutes after he's fully out, or until expiry.
- **Mistyped exits:** if an exit alert names a contract he isn't in, the bot looks for the one open contract that differs by a single field (ticker letter, strike, call/put or date) and accepts it only if his price fits that contract's live bid/ask. Otherwise it logs a CHECK and marks `POSSIBLE_EXIT?` on the price timeline. Alerts that arrive late (catch-up) are always CHECK, never guessed.
- **runs/<date>/stalls.csv:** appears only if the program froze for over a minute (PC sleep, paused window). It lists when it froze and resumed, so those gaps can be ignored in the analysis.
- **data/state.json:** the bot's memory between restarts (positions, the $5,000 pot, price watches). Don't delete it.
- **runs/<date>/orders_log.csv:** every real order, live mode only.
- **Telegram "Saved Messages":** one line per decision plus the daily summary, with each tracked contract's high and low versus his buy price.

## Wednesday after the close
Stop the bot and send the day folders under `runs/` (`trades_log.csv` and `price_paths.csv` in each) and `data/state.json`.
**Never** send `.env` or anything in `data/` except `state.json`.

## Keep private
`.env`, `data/sniper_session.session` and `data/robinhood.pickle` each give access to your accounts. Don't share or upload them.
