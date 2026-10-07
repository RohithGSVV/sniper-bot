"""
Sniper alerts bot - copies his long option alerts into Robinhood
================================================================

What it does
  1. Listens to the Sniper Trades Telegram group, live.
  2. Reads each alert (BOUGHT / SOLD 1/2 / ALL OUT ...).
  3. Pulls the real Robinhood bid/ask for that option at that moment.
  4. Decides what to do (buy? skip? how many? sell how many?) using the guardrails in your .env file.
  5. LIVE_TRADING=true: places REAL limit orders (1 contract per alert, or 2 with a runner) and runs the
     take-profit, runner and emergency stops itself. LIVE_TRADING=false (shadow mode): only logs what it
     WOULD do and never sends an order.
  6. Logs every decision to runs/<date>/trades_log.csv, posts a short note to your Telegram
     "Saved Messages", and records the price path of every contract he buys.

What it never does
  It never touches positions it didn't buy, and it never buys once the project pot is gone.
  See README.md and docs/LIVE.md.

Run:
  python sniper_shadow.py --list-chats   # first time: find the group id
  python sniper_shadow.py                # run during market hours (type LIVE when asked, in live mode)
  python sniper_shadow.py --selftest     # check the logic with pretend quotes and orders, no logins
"""

import asyncio
import csv
import functools
import json
import math
import os
import re
import shutil
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field, asdict, replace
from datetime import datetime, date, time as dtime, timedelta, tzinfo
from fractions import Fraction


class _USEastern(tzinfo):
    """New York time without needing the tzdata package (Windows has no
    built-in time zone database). US rule: daylight time from 2:00 AM on the
    2nd Sunday of March to 2:00 AM on the 1st Sunday of November."""

    @staticmethod
    def _nth_sunday(year, month, n):
        d = date(year, month, 1)
        d += timedelta(days=(6 - d.weekday()) % 7)      # first Sunday
        return d + timedelta(weeks=n - 1)

    def _dst_bounds_local(self, year):
        start = datetime.combine(self._nth_sunday(year, 3, 2), dtime(2))
        end = datetime.combine(self._nth_sunday(year, 11, 1), dtime(1))   # 1:00 standard
        return start, end

    def utcoffset(self, dt):
        return timedelta(hours=-5) + self.dst(dt)

    def dst(self, dt):
        if dt is None:
            return timedelta(0)
        start, end = self._dst_bounds_local(dt.year)
        naive = dt.replace(tzinfo=None, fold=0)
        if end <= naive < end + timedelta(hours=1):     # the repeated 1 AM hour in November
            return timedelta(0) if dt.fold else timedelta(hours=1)
        return timedelta(hours=1) if start <= naive < end else timedelta(0)

    def tzname(self, dt):
        return "EDT" if self.dst(dt) else "EST"

    def fromutc(self, dt):
        naive = dt.replace(tzinfo=None)
        start, end = self._dst_bounds_local(naive.year)
        start_utc, end_utc = start + timedelta(hours=5), end + timedelta(hours=5)
        is_dst = start_utc <= naive < end_utc
        local = naive + (timedelta(hours=-4) if is_dst else timedelta(hours=-5))
        repeated = end <= local < end + timedelta(hours=1)
        return local.replace(tzinfo=self, fold=1 if (repeated and not is_dst) else 0)


try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:          # no tzdata (typical on Windows) -> use the built-in rule above
    ET = _USEastern()
HERE = os.path.dirname(os.path.abspath(__file__))
# Folder layout (see README.md):
#   data/   the bot's memory and logins (never share, never commit): state.json, robinhood.pickle,
#           sniper_session.session
#   runs/   one folder per trading day: trades_log.csv, price_paths.csv, orders_log.csv, stalls.csv
DATA_DIR = os.path.join(HERE, "data")
RUNS_DIR = os.path.join(HERE, "runs")
os.makedirs(DATA_DIR, exist_ok=True)


def run_file(name):
    """Today's copy of a daily file, e.g. runs/2026-10-06/trades_log.csv (the folder is made on demand).
    Evaluated every time it is used, so a bot left running past midnight starts a new day's folder."""
    day = datetime.now(ET).strftime("%Y-%m-%d")
    folder = os.path.join(RUNS_DIR, day)
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, name)


class _DynPath:
    """A file path that may be given as text or as a function returning text (for the daily files)."""
    def __set_name__(self, owner, name):
        self.attr = "_" + name

    def __get__(self, obj, typ=None):
        v = getattr(obj, self.attr)
        return v() if callable(v) else v

    def __set__(self, obj, v):
        setattr(obj, self.attr, v)


LOG_CSV = lambda: run_file("trades_log.csv")            # every alert + what the bot decided (daily)
PATHS_CSV = lambda: run_file("price_paths.csv")         # price of every contract he buys, over time (daily)
ORDERS_CSV = lambda: run_file("orders_log.csv")         # every REAL order sent, live mode only (daily)
STALLS_CSV = lambda: run_file("stalls.csv")             # any freeze longer than a minute (daily)
STATE_JSON = os.path.join(DATA_DIR, "state.json")       # memory between restarts (positions, pot, watches)
STOP_FILE = os.path.join(HERE, "STOP_TRADING")          # create this file = no new buys (exits still work)
OLD_LAYOUT_FILES = ["paper_positions.json", "robinhood.pickle", "sniper_session.session",
                    "trades_log.csv", "price_paths.csv", "orders_log.csv", "stalls.csv"]

_CSV_WAITING: dict[str, list] = {}      # rows that could not be written yet, by file
_CSV_WARNED: dict[str, float] = {}


def append_csv(path, cols, row):
    """Add one row (a dict or a list) to a CSV file, writing the header first if the file is new.
    A log that is open in Excel is locked and can't be written. That must never stop an order or the
    timer, so the row is kept in memory and saved with the next row that gets through.
    Returns True when everything is on disk."""
    rows = _CSV_WAITING.pop(path, []) + [dict(row) if isinstance(row, dict) else list(row)]
    try:
        new = not os.path.exists(path)
        # UTF-8 so emoji in alerts can be saved (Windows default can't);
        # the "-sig" marker on a new file makes Excel open it correctly
        with open(path, "a", newline="", encoding="utf-8-sig" if new else "utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(cols)
            for r in rows:
                w.writerow([r.get(c, "") for c in cols] if isinstance(r, dict) else r)
        return True
    except OSError as e:
        _CSV_WAITING[path] = rows[-5000:]
        if time.time() - _CSV_WARNED.get(path, 0) > 60:
            _CSV_WARNED[path] = time.time()
            print(f"!! Can't write to {os.path.basename(path)} ({type(e).__name__}) - is it open in Excel? Close it. "
                  f"{len(rows)} line(s) are waiting and will be saved then.")
        return False


def refuse_old_layout():
    """Files from the old flat layout next to the script mean the new data/ and runs/ folders would start
    empty: the pot, positions and logins would silently reset. Stop and say how to move them."""
    left = [n for n in OLD_LAYOUT_FILES if os.path.exists(os.path.join(HERE, n))]
    if left:
        print("\nOld file layout found next to the script: " + ", ".join(left))
        print("The bot now keeps its memory in data/ and its daily files in runs/<date>/.")
        print("Close the bot, then run once:  python tools/migrate_layout.py --apply")
        sys.exit(1)


# ----------------------------------------------------------------------------
# Settings (read from .env; defaults shown here)
# ----------------------------------------------------------------------------
def _env(name, default):
    return os.environ.get(name, str(default)).strip()


class Settings:
    def __init__(self):
        self.budget_per_trade = float(_env("BUDGET_PER_TRADE", 500))
        self.max_single_contract_cost = float(_env("MAX_SINGLE_CONTRACT_COST", 1000))
        self.entry_cap_pct = float(_env("ENTRY_CAP_PCT", 10))
        self.entry_floor_pct = float(_env("ENTRY_FLOOR_PCT", 30))
        self.allow_puts = _env("ALLOW_PUTS", "true").lower() == "true"
        self.max_spread_pct = float(_env("MAX_SPREAD_PCT", 15))
        self.max_alert_age_sec = float(_env("MAX_ALERT_AGE_SEC", 60))
        self.open_buffer_min = float(_env("OPEN_BUFFER_MIN", 1))
        self.max_open_positions = int(_env("MAX_OPEN_POSITIONS", 4))
        self.max_deployed = float(_env("MAX_DEPLOYED", 5000))
        # THE PROJECT'S MONEY: we start with this much. Live realized P&L is tracked against it,
        # we never put more than what's left to work, and new buys stop for good when it is gone.
        self.project_capital = float(_env("PROJECT_CAPITAL", 5000))
        self.exit_check_sec = float(_env("EXIT_CHECK_SEC", 10))
        self.daily_loss_limit = float(_env("DAILY_LOSS_LIMIT", 750))
        # how to split his partial sells over our contracts: down | up | nearest
        # nearest (default): with 1 contract we sell once he has sold half or more
        self.partial_rounding = _env("PARTIAL_ROUNDING", "nearest").lower()
        self.contracts_per_trade = int(_env("CONTRACTS_PER_TRADE", 1))
        # runner: buy 2 contracts when both fit within MAX_SINGLE_CONTRACT_COST (lottos: always 1). The first is
        # sold at his first trim (or by the take-profit); the second, the runner, only when he is fully out - or
        # if its mid price falls back to what we paid
        self.keep_runner = _env("KEEP_RUNNER", "true").lower() == "true"
        # his red double-exclamation (or the word "lotto") marks a lotto; we only copy lottos
        # whose price is under this many dollars per share (2.50 = $250 per contract)
        self.lotto_max_price = float(_env("LOTTO_MAX_PRICE", 2.50))
        # emergency exit if a position loses this % of what we paid (0 = off). Measured on the
        # mid price, since he often posts no exit on losers (they ran to zero in the observation week)
        self.disaster_stop_pct = float(_env("DISASTER_STOP_PCT", 50))
        self.max_buys_per_day = int(_env("MAX_BUYS_PER_DAY", 6))
        # sell as soon as the bid is more than this % above what we paid (0 = off)
        self.take_profit_pct = float(_env("TAKE_PROFIT_PCT", 40))
        # late entry: a buy alert is still acted on up to this many minutes after he posts it, by expiry:
        # 0DTE uses LATE_ENTRY_MIN; short-dated (1-7 days) LATE_ENTRY_MIN_SHORT; swing (8+ days)
        # LATE_ENTRY_MIN_SWING, and a late swing is only bought while he hasn't sold any of it
        self.late_entry_min = float(_env("LATE_ENTRY_MIN", 10))
        self.late_entry_min_short = float(_env("LATE_ENTRY_MIN_SHORT", 2))
        self.late_entry_min_swing = float(_env("LATE_ENTRY_MIN_SWING", 60))
        # a spread wider than MAX_SPREAD_PCT when he posts is re-checked for this many seconds before
        # the buy is skipped (it is often a one-quote blip) (0 = off)
        self.spread_recheck_sec = float(_env("SPREAD_RECHECK_SEC", 60))
        # never hold two of his contracts on the same stock at once (e.g. MU 1100C and MU 1130C)
        self.one_position_per_stock = _env("ONE_POSITION_PER_STOCK", "true").lower() == "true"
        # watch-and-buy: when the ask is above our cap at the alert (his price +ENTRY_CAP_PCT) and the
        # option expires MORE than WATCH_MIN_DTE days out, keep checking for WATCH_MIN minutes and buy if
        # the ask comes back within the cap - unless he sells first (0 = off)
        self.watch_min = float(_env("WATCH_MIN", 60))
        self.watch_min_dte = int(_env("WATCH_MIN_DTE", 3))
        self.watch_max_tries = int(_env("WATCH_MAX_TRIES", 3))
        # ---- LIVE TRADING (everything below is ignored unless LIVE_TRADING=true) ----
        self.live_trading = _env("LIVE_TRADING", "false").lower() == "true"
        self.buy_timeout_sec = float(_env("BUY_TIMEOUT_SEC", 15))
        self.buy_cushion_ticks = int(_env("BUY_CUSHION_TICKS", 1))
        self.sell_attempt_sec = float(_env("SELL_ATTEMPT_SEC", 8))
        self.min_buying_power_buffer_pct = float(_env("MIN_BUYING_POWER_BUFFER_PCT", 10))
        self.tg_bot_token = _env("TG_BOT_TOKEN", "")          # optional: phone push alerts
        self.tg_notify_chat = _env("TG_NOTIFY_CHAT_ID", "")
        hh, mm = _env("EXPIRY_CLOSE_TIME", "15:30").split(":")
        self.expiry_close_time = dtime(int(hh), int(mm))
        self.no_new_buys_after = dtime(15, 30)
        self.add_to_watchlist = _env("ADD_TO_WATCHLIST", "true").lower() == "true"
        self.watchlist_name = _env("WATCHLIST_NAME", "Sniper Test")
        self.notify_saved_messages = _env("NOTIFY_SAVED_MESSAGES", "true").lower() == "true"
        # observation / price tracking
        self.track_interval_sec = float(_env("TRACK_INTERVAL_SEC", 30))
        self.post_exit_track_min = float(_env("POST_EXIT_TRACK_MIN", 30))
        self.backfill_days = float(_env("BACKFILL_DAYS", 7))
        self.heartbeat_min = float(_env("HEARTBEAT_MIN", 5))
        # typo'd exit alerts: how close his price must be to the live bid/ask, and how
        # fresh the alert must be, before we trust "closest contract" over the literal text
        self.typo_price_tol_pct = float(_env("TYPO_PRICE_TOL_PCT", 8))
        self.typo_max_age_sec = float(_env("TYPO_MAX_AGE_SEC", 120))
        # log any time the program freezes for longer than this (PC sleep, paused window)
        self.stall_warn_sec = float(_env("STALL_WARN_SEC", 60))


def expiry_bucket(exp: date, today: date):
    """0DTE = expires today, short = 1-7 days, swing = 8+ days."""
    dte = (exp - today).days
    return ("0DTE" if dte <= 0 else "short" if dte <= 7 else "swing"), dte


# ----------------------------------------------------------------------------
# 1) Reading the alert text
# ----------------------------------------------------------------------------
@dataclass
class Alert:
    action: str               # BUY | SELL | ALL_OUT
    ticker: str
    strike: float
    cp: str                   # C or P
    exp: date
    price: float
    fraction: Fraction | None  # e.g. 1/2 for "SOLD 1/2", None if not given
    note: str
    raw: str

    @property
    def contract(self):
        return f"{self.ticker} {self.strike:g}{self.cp} {self.exp:%m/%d}"


ALERT_RE = re.compile(
    r"""^\s*
    (?P<action>BOUGHT|BOT|BUY|SOLD|SELL|TRIMMED|TRIM|ALL[\s-]*OUT)\s+
    (?:(?P<num>\d+)\s*/\s*(?P<den>\d+)\s+)?            # optional 1/2, 1/4 ...
    (?P<ticker>[A-Z]{1,6})\s+
    \$?(?P<strike>\d+(?:\.\d+)?)\s*(?P<cp>CALLS?|PUTS?|C|P)\s+
    (?P<month>\d{1,2})/(?P<day>\d{1,2})(?:/(?P<year>\d{2,4}))?\s+
    (?:@\s*)?\$?(?P<price>\d*\.?\d+)
    (?P<rest>.*)$""",
    re.X,
)


def clean_line(line: str) -> str:
    line = line.replace("@everyone", " ").replace("@here", " ")
    line = line.encode("ascii", "ignore").decode()   # drops emoji like the red !!
    return re.sub(r"\s+", " ", line).strip().upper()


def infer_expiry(month: int, day: int, year: str | None, today: date) -> date:
    if year:
        y = int(year)
        return date(y + 2000 if y < 100 else y, month, day)
    d = date(today.year, month, day)
    if d < today - timedelta(days=7):      # "1/16" seen in December = next year
        d = date(today.year + 1, month, day)
    return d


# We only copy LONG options: buy a call/put, later sell that same call/put.
# Everything below is ignored on purpose.
SHARES_RE = re.compile(r"\bSHARES?\b|\bSTOCKS?\b")
SHORT_OPEN_RE = re.compile(r"CASH[\s-]*SECURED|\bCSPS?\b|COVERED|SELL(?:ING)? TO OPEN|SOLD TO OPEN|\bSTO\b|\bNAKED\b|\bWROTE\b|\bWRITING\b")
SHORT_CLOSE_RE = re.compile(r"BUY(?:ING)? TO CLOSE|BOUGHT TO CLOSE|\bBTC\b|BOUGHT BACK|BUY(?:ING)? BACK")
MULTI_LEG_RE = re.compile(r"SPREAD|\bROLL(?:ED|ING)?\b|IRON|CONDOR|BUTTERFLY|STRADDLE|STRANGLE|CALENDAR|"
                          r"\d+(?:\.\d+)?[CP]\s*/\s*\d+(?:\.\d+)?[CP]\b")
# His follow-up corrections: "2.45*", "*NBIS", "typo - 2.45", "meant 1100C".
# For now these are LOGGED ONLY - the bot never trades on them.
CORRECTION_RE = re.compile(r"\*|\bTYPO\b|\bCORRECTION\b|\bCORRECTED\b|\bMEANT\b|\bSHOULD BE\b")
# Something that looks like an option contract: "1100C 10/2"
LOOKS_LIKE_OPTION_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:C|P|CALLS?|PUTS?)\b.*\d{1,2}/\d{1,2}")
CONTRACT_RE = re.compile(
    r"(?P<ticker>[A-Z]{1,6})\s+\$?(?P<strike>\d+(?:\.\d+)?)\s*(?P<cp>CALLS?|PUTS?|C|P)\s+(?P<month>\d{1,2})/(?P<day>\d{1,2})")


def contract_key(ticker, strike, cp, exp: date):
    return f"{ticker}|{strike:g}|{cp[0]}|{exp.isoformat()}"


def find_contract_key(text: str, today: date):
    """Pull 'ASML 1800P 10/2' out of any line (used for short trades we ignore)."""
    m = CONTRACT_RE.search(text)
    if not m:
        return None
    try:
        exp = infer_expiry(int(m["month"]), int(m["day"]), None, today)
    except ValueError:
        return None
    return contract_key(m["ticker"], float(m["strike"]), m["cp"], exp)


def parse_line(line: str, today: date):
    """Returns (Alert, None) if it is a long-option alert we can copy,
    or (None, reason) if the line should be ignored.
    Reasons starting with 'SHORT' or 'UNREADABLE' get special handling."""
    text = clean_line(line)
    if not text:
        return None, "empty"
    # normalise common spellings so they read like his usual format
    text = re.sub(r"^(SOLD|SELL) TO CLOSE\b|^STC\b", "SOLD", text)
    text = re.sub(r"^(BOUGHT|BUY) TO OPEN\b|^BTO\b", "BOUGHT", text)

    if CORRECTION_RE.search(text):
        return None, "CORRECTION: follow-up fix - logged only, no trade"
    if SHARES_RE.search(text):
        return None, "stock trade (shares) - ignored"
    if SHORT_OPEN_RE.search(text):
        return None, "SHORT OPEN: CSP / covered call / short option - ignored"
    if SHORT_CLOSE_RE.search(text):
        return None, "SHORT CLOSE: buying back a short option - ignored"
    if MULTI_LEG_RE.search(text):
        return None, "multi-leg trade (spread/roll) - ignored"
    m = ALERT_RE.match(text)
    if not m:
        if LOOKS_LIKE_OPTION_RE.search(text):
            return None, "UNREADABLE: looks like an option alert but the format is off"
        return None, "not an alert (info text)"

    word = m["action"]
    if word in ("BOUGHT", "BOT", "BUY"):
        action = "BUY"
    elif word.startswith("ALL"):
        action = "ALL_OUT"
    else:
        action = "SELL"

    fraction = None
    if m["num"]:
        num, den = int(m["num"]), int(m["den"])
        if den == 0 or num > den:
            return None, f"odd fraction {num}/{den} - ignored"
        fraction = Fraction(num, den)
    if action == "BUY" and fraction:
        return None, "buy with a fraction - unclear, ignored"
    if word.startswith("TRIM") and fraction is None:
        return None, "TRIM without a size - unclear, ignored"

    try:
        exp = infer_expiry(int(m["month"]), int(m["day"]), m["year"], today)
    except ValueError:
        return None, "bad expiry date"

    return Alert(
        action=action,
        ticker=m["ticker"],
        strike=float(m["strike"]),
        cp=m["cp"][0],
        exp=exp,
        price=float(m["price"]),
        fraction=fraction,
        note=m["rest"].strip(" -"),
        raw=line.strip(),
    ), None


def parse_message(text: str, today: date):
    """One Telegram/Discord message can hold several alert lines."""
    results = []
    for line in text.splitlines():
        if line.strip():
            results.append((line.strip(),) + parse_line(line, today))
    return results


def edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


# ----------------------------------------------------------------------------
# 2) Paper positions
# ----------------------------------------------------------------------------
@dataclass
class Position:
    ticker: str
    strike: float
    cp: str
    exp: str                  # ISO date
    qty_orig: int
    qty_open: int
    entry_price: float        # what WE would have paid (the ask)
    his_entry: float          # his alert price
    opened_at: str
    sold_fraction: str = "0"  # cumulative fraction he has sold, as text e.g. "3/4"
    realized_pnl: float = 0.0
    his_pnl_same_qty: float = 0.0
    real: bool = False        # True = a real Robinhood position, False = paper
    lotto: bool = False
    exit_wanted: int = 0      # contracts we still need to sell (live: retried until done)
    exit_attempts: int = 0
    exit_refused: int = 0     # sell orders in a row that Robinhood refused outright
    exit_why: str = ""
    runner: bool = False      # 2 contracts: 1 sold at his first trim, 1 (the runner) kept until he is fully out

    @property
    def contract(self):
        return f"{self.ticker} {self.strike:g}{self.cp} {date.fromisoformat(self.exp):%m/%d}"

    @property
    def runner_left(self):
        """Only the runner is left: the first of the 2 contracts has been sold."""
        return self.runner and 0 < self.qty_open < self.qty_orig


LOTTO_RE = re.compile("‼|❗|!!|\\bLOTTOS?\\b", re.I)    # double red !, heavy !, "!!", the word lotto


def is_lotto(raw: str) -> bool:
    """His red double exclamation mark marks a lotto (the word 'lotto' counts too)."""
    return bool(LOTTO_RE.search(raw or ""))


def round_to_tick(price: float, tick: float, mode: str) -> float:
    n = price / tick
    n = math.ceil(n - 1e-9) if mode == "up" else math.floor(n + 1e-9) if mode == "down" else round(n)
    return round(n * tick, 2)


def tick_for(min_ticks, price: float) -> float:
    """Robinhood's price step for this contract at this price. If we can't read it, use the
    coarsest common step (a multiple of 0.05 / 0.10 is valid on every contract)."""
    try:
        above, below, cutoff = (float(min_ticks[k]) for k in ("above_tick", "below_tick", "cutoff_price"))
    except Exception:
        above, below, cutoff = 0.10, 0.05, 3.00
    return above if price >= cutoff else below


# ----------------------------------------------------------------------------
# 2b) Price tracker: follows EVERY contract he buys (even ones we'd skip)
#     from his alert until 30 min after he is fully out, or expiry.
#     This is the raw data for designing stops / second-chance buys later.
# ----------------------------------------------------------------------------
@dataclass
class Tracked:
    key: str
    ticker: str
    strike: float
    cp: str
    exp: str
    bucket: str
    his_entry: float
    his_alert_at: str          # when he posted the buy (ISO, Eastern)
    source: str                # live | catch-up
    his_sold: str = "0"        # cumulative fraction he has sold
    his_exit_at: str = ""
    his_exit_price: float = 0.0
    stop_after: str = ""       # stop sampling after this time
    samples: int = 0
    first_mark: float = 0.0
    hi_mark: float = 0.0
    lo_mark: float = 0.0
    last_mark: float = 0.0
    misses: int = 0            # samples where Robinhood returned no price
    done: bool = False
    end_reason: str = ""

    @property
    def contract(self):
        return f"{self.ticker} {self.strike:g}{self.cp} {date.fromisoformat(self.exp):%m/%d}"


PATH_COLS = ["ts", "contract", "bucket", "dte", "event", "his_entry", "bid", "ask", "mark",
             "spread_pct", "mark_vs_his_pct", "ask_vs_his_pct", "underlying", "iv", "delta",
             "volume", "min_since_his_buy", "his_sold", "note"]


class Tracker:
    path_csv = _DynPath()

    def __init__(self, settings, quote_many, underlying_many, now, path_csv, save, notify):
        self.s = settings
        self.quote_many = quote_many          # fn(list[Tracked]) -> {key: quote dict}
        self.underlying_many = underlying_many  # fn(list[str]) -> {ticker: price}
        self.now = now
        self.path_csv = path_csv
        self.save = save
        self.notify = notify
        self.items: dict[str, Tracked] = {}
        self.last_sample = None
        self.fail_streak = 0

    # ---- persistence (stored inside the engine's state file) ----------------
    def to_json(self):
        return [asdict(t) for t in self.items.values()]

    def load(self, data):
        self.items = {d["key"]: Tracked(**d) for d in data or []}

    def active(self):
        return [t for t in self.items.values() if not t.done]

    # ---- events from alerts --------------------------------------------------
    def start(self, a: "Alert", posted_at: datetime, source: str):
        today = self.now().date()
        if a.exp < today:
            return
        key = contract_key(a.ticker, a.strike, a.cp, a.exp)
        t = self.items.get(key)
        late = "" if source == "live" else f" (posted {posted_at:%m/%d %H:%M}, seen late)"
        if t and not t.done:
            self.event(t, f"HIS_BUY_AGAIN @ {a.price}{late}", snapshot=(source == "live"))
            return
        bucket, _ = expiry_bucket(a.exp, posted_at.date())
        t = Tracked(key=key, ticker=a.ticker, strike=a.strike, cp=a.cp, exp=a.exp.isoformat(),
                    bucket=bucket, his_entry=a.price, his_alert_at=posted_at.isoformat(timespec="seconds"),
                    source=source)
        self.items[key] = t
        self.event(t, f"HIS_BUY @ {a.price}{late}", snapshot=(source == "live"))
        self.save()

    def find(self, a: "Alert"):
        live = self.active()
        exact = [t for t in live if (t.ticker, t.strike, t.cp, t.exp) ==
                 (a.ticker, a.strike, a.cp, a.exp.isoformat())]
        if exact:
            return exact[0]
        close = [t for t in live if (t.strike, t.cp, t.exp) == (a.strike, a.cp, a.exp.isoformat())
                 and edit_distance(t.ticker, a.ticker) <= 1]
        return close[0] if len(close) == 1 else None

    def open_contracts(self):
        """Contracts he is still in (bought, not yet fully sold)."""
        return [t for t in self.active() if not t.his_exit_at]

    def possible_exit(self, t: Tracked, a: "Alert", posted_at: datetime, why: str):
        """An exit alert that probably belongs to this contract but could not be
        confirmed. Marked on the price timeline only - the contract keeps being tracked."""
        late = "" if self.now() - posted_at < timedelta(minutes=5) else f" (posted {posted_at:%m/%d %H:%M})"
        self._write(t, {}, None, f"POSSIBLE_EXIT? alert said {a.contract} @ {a.price}{late} - {why}")

    def his_sell(self, a: "Alert", posted_at: datetime, source: str, typo_note: str = ""):
        t = self.find(a)
        if not t:
            return
        if a.action == "ALL_OUT" or a.fraction is None:
            sold = Fraction(1)
        else:
            sold = min(Fraction(1), Fraction(t.his_sold) + a.fraction)
        t.his_sold = str(sold)
        label = "HIS_ALL_OUT" if sold == 1 else f"HIS_SELL {a.fraction}"
        late = "" if source == "live" else f" (posted {posted_at:%m/%d %H:%M}, seen late)"
        fixed = f" [typo fixed: {typo_note}]" if typo_note else ""
        self.event(t, f"{label} @ {a.price}{late}{fixed}", snapshot=(source == "live"))
        if sold == 1 and not t.his_exit_at:
            t.his_exit_at = posted_at.isoformat(timespec="seconds")
            t.his_exit_price = a.price
            t.stop_after = (posted_at + timedelta(minutes=self.s.post_exit_track_min)).isoformat(timespec="seconds")
        self.save()

    # ---- sampling --------------------------------------------------------------
    def due(self):
        now = self.now()
        if not market_is_open(now):
            return False
        return self.last_sample is None or (now - self.last_sample).total_seconds() >= self.s.track_interval_sec

    def sweep(self):
        """Stop following contracts that expired or that he left 30+ min ago.
        Runs all the time, not just in market hours, so they end on the right day."""
        now = self.now()
        changed = False
        for t in self.active():
            exp = date.fromisoformat(t.exp)
            if exp < now.date() or (exp == now.date() and now.time() >= dtime(16, 0)):
                self.finish(t, "expired")
                changed = True
            elif t.stop_after and now >= datetime.fromisoformat(t.stop_after):
                self.finish(t, f"{self.s.post_exit_track_min:g} min after his exit")
                changed = True
        if changed:
            self.save()

    def sample(self):
        now = self.now()
        self.last_sample = now
        self.sweep()
        live = self.active()
        if not live:
            return 0
        quotes = self.quote_many(live) or {}
        unders = {}
        try:
            unders = self.underlying_many(sorted({t.ticker for t in live})) or {}
        except Exception as e:
            print(f"underlying quote error: {e}")
        got = 0
        for t in live:
            q = quotes.get(t.key)
            if not q or q.get("mark") is None:
                t.misses += 1
                if t.misses >= 10 and not t.samples:
                    self.finish(t, "Robinhood has no price for this contract (typo?)")
                continue
            got += 1
            self._write(t, q, unders.get(t.ticker), "")
        if got == 0:
            self.fail_streak += 1
            if self.fail_streak == 3:
                self.notify("!! Robinhood quotes failing 3 times in a row - session may have expired. "
                            "Restart the bot.")
        else:
            self.fail_streak = 0
        self.save()
        return got

    def event(self, t: Tracked, label: str, snapshot: bool):
        q, u = None, None
        if snapshot and market_is_open(self.now()):
            q = (self.quote_many([t]) or {}).get(t.key)
            try:
                u = (self.underlying_many([t.ticker]) or {}).get(t.ticker)
            except Exception:
                u = None
        self._write(t, q or {}, u, label)

    def finish(self, t: Tracked, reason: str):
        t.done = True
        t.end_reason = reason
        self._write(t, {}, None, f"END ({reason})")

    def _write(self, t: Tracked, q: dict, under, event: str):
        now = self.now()
        mark, bid, ask = q.get("mark"), q.get("bid"), q.get("ask")
        if mark is not None and not event:
            t.samples += 1
            t.first_mark = t.first_mark or mark
            t.hi_mark = max(t.hi_mark, mark) if t.hi_mark else mark
            t.lo_mark = min(t.lo_mark, mark) if t.lo_mark else mark
            t.last_mark = mark
        pct = lambda x: f"{(x / t.his_entry - 1) * 100:.1f}" if x and t.his_entry else ""
        spread = f"{(ask - bid) / ((ask + bid) / 2) * 100:.1f}" if bid and ask else ""
        mins = (now - datetime.fromisoformat(t.his_alert_at)).total_seconds() / 60
        row = {"ts": now.strftime("%Y-%m-%d %H:%M:%S"), "contract": t.contract, "bucket": t.bucket,
               "dte": (date.fromisoformat(t.exp) - now.date()).days, "event": event,
               "his_entry": t.his_entry, "bid": bid if bid is not None else "",
               "ask": ask if ask is not None else "", "mark": mark if mark is not None else "",
               "spread_pct": spread, "mark_vs_his_pct": pct(mark), "ask_vs_his_pct": pct(ask),
               "underlying": under if under is not None else "", "iv": q.get("iv", ""),
               "delta": q.get("delta", ""), "volume": q.get("volume", ""),
               "min_since_his_buy": f"{mins:.1f}", "his_sold": t.his_sold, "note": ""}
        append_csv(self.path_csv, PATH_COLS, row)

    def summary_lines(self):
        today = self.now().date().isoformat()
        out = []
        for t in self.items.values():
            if not t.samples:
                continue
            last_day = t.his_exit_at[:10] if t.his_exit_at else today
            if not t.done or last_day == today or t.his_alert_at[:10] == today:
                hi = (t.hi_mark / t.his_entry - 1) * 100
                lo = (t.lo_mark / t.his_entry - 1) * 100
                if t.his_exit_at:
                    status = f"his exit {t.his_exit_price} ({(t.his_exit_price / t.his_entry - 1) * 100:+.0f}%)"
                elif t.exp < today:
                    status = "EXPIRED (he never posted an exit)"
                else:
                    status = "he is still in"
                out.append(f"  {t.contract} [{t.bucket}] his buy {t.his_entry}: high {hi:+.0f}%, "
                           f"low {lo:+.0f}%, {status}; {t.samples} samples")
        return out


def _usd(x):
    return f"{'+' if x >= 0 else '-'}${abs(x):,.0f}"


def _ago(sec):
    return f"{sec:.0f} s" if sec < 120 else f"{sec / 60:.0f} min"


def buy_message(verb, qty, contract, our_price, his_price, lotto=False, delay=None, watched=False, runner=False):
    """A small card for Telegram: what we paid next to what he paid. First line is the title;
    the rest is a table meant for a monospaced font."""
    diff = (our_price / his_price - 1) * 100 if his_price else 0.0
    rule = " " + "-" * 40
    lines = [f"🟢 {verb}  {contract}" + ("  [LOTTO]" if lotto else ""),
             rule,
             f" {'':<10}{'Price':>8}{'vs him':>10}",
             f" {'We paid':<10}{our_price:>8.2f}{diff:>+9.1f}%",
             f" {'He paid':<10}{his_price:>8.2f}",
             f" {'Cost':<10}{'$' + format(qty * our_price * 100, ',.0f'):>8}   ({qty} contract{'s' if qty != 1 else ''})"]
    if runner:
        lines.append(f" {'Plan':<10}1 sold at his first trim, 1 runner kept until he is fully out")
    if watched:
        lines.append(f" {'Timing':<10}bought after watching the price ({_ago(delay)} after his alert)")
    elif delay is not None and delay > 60:
        lines.append(f" {'Timing':<10}late entry ({_ago(delay)} after his alert)")
    return "\n".join(lines)


def sell_message(verb, qty, contract, our_price, our_entry, his_price, his_entry, pnl, his_pnl, why, left):
    """A small card for Telegram: our sell next to his, with both profits."""
    our_pct = (our_price / our_entry - 1) * 100 if our_entry else 0.0
    rule = " " + "-" * 44
    lines = [f"🔴 {verb}  {contract}",
             rule,
             f" {'':<6}{'Bought':>8}{'Sold':>8}   Profit",
             f" {'Us':<6}{our_entry:>8.2f}{our_price:>8.2f}   {_usd(pnl)} ({our_pct:+.0f}%)"]
    if his_price:
        his_pct = (his_price / his_entry - 1) * 100 if his_entry else 0.0
        gap = (our_price / his_price - 1) * 100
        lines.append(f" {'Him':<6}{his_entry:>8.2f}{his_price:>8.2f}   {_usd(his_pnl)} ({his_pct:+.0f}%)")
        lines.append(rule)
        lines.append(f" We sold {abs(gap):.1f}% {'above' if gap >= 0 else 'below'} his price. {left} left.")
    else:
        lines.append(f" {'Him':<6}{his_entry:>8.2f}{'--':>8}   has not sold")
        lines.append(rule)
        lines.append(f" Our own exit: {why.strip() or 'bot rule'}. {left} left.")
    return "\n".join(lines)


def market_is_open(t: datetime):
    return t.weekday() < 5 and dtime(9, 30) <= t.time() < dtime(16, 0)


# ----------------------------------------------------------------------------
# 3) The decision engine (no network code in here, so it can be tested)
# ----------------------------------------------------------------------------
class Engine:
    log_path = _DynPath()
    LOG_COLS = ["logged_at", "alert_time", "delay_sec", "msg_id", "source", "raw", "action",
                "contract", "bucket", "his_price", "bid", "ask", "spread_pct", "decision", "reason",
                "qty", "our_price", "our_pnl", "his_pnl_same_qty"]

    def __init__(self, settings, quotes, notify, now=lambda: datetime.now(ET),
                 log_path=LOG_CSV, state_path=STATE_JSON, watchlist=None, tracker=None,
                 broker=None, stop_file=STOP_FILE):
        self.broker = broker          # Broker (real orders) or None (paper only)
        self.stop_file = stop_file
        self.halted = ""              # non-empty = no new buys (e.g. an order we could not confirm)
        self.lock = threading.RLock()  # alerts and the timer run in different threads
        self._stop_checked = {}
        self._exit_tried = {}
        self.s = settings
        self.quotes = quotes          # function(ticker, exp_date, strike, cp) -> dict or None
        self._notify = notify         # function(text)
        self.now = now
        self.log_path = log_path
        self.state_path = state_path
        self.watchlist = watchlist    # function(ticker) or None
        self.tracker = tracker        # Tracker or None
        if tracker:
            tracker.save = self.save_state
        self.positions: list[Position] = []
        self.pending_sells: list[dict] = []   # sells that came in while market closed
        self.his_shorts: set[str] = set()     # contracts he SOLD to open (CSPs, covered calls...)
        self.seen_buys: set[str] = set()      # every contract he has posted a BUY for
        self.watching: dict[str, dict] = {}   # buys skipped only for being above the cap, still being watched
        self._last_watch = None
        self.inflight: list[str] = []         # real orders sent but not yet resolved
        self.live_realized = 0.0              # real (live) profit/loss since the project started
        self.project_started = ""
        self.loss_alerts: list[int] = []      # loss levels (%) already announced
        self.last_alert = None                # last real alert, for linking corrections
        self.processed_ids: list[int] = []    # Telegram message ids already handled
        self.last_msg_id = 0
        self.quiet = False                    # True while catching up: log, but don't ping
        self.ctx = {}                         # msg_id / source of the message being handled
        self.load_state()
        self._rotate_old_log()

    def notify(self, text):
        if not self.quiet:
            self._notify(text)

    def _rotate_old_log(self):
        """If trades_log.csv was written by an older version (different columns), keep it
        under another name so the new file starts clean."""
        if not os.path.exists(self.log_path):
            return
        with open(self.log_path, encoding="utf-8-sig", errors="replace") as f:
            header = f.readline().strip().split(",")
        if header != self.LOG_COLS:
            base, ext = os.path.splitext(self.log_path)
            os.replace(self.log_path, f"{base}_old_{self.now():%Y%m%d_%H%M%S}{ext}")

    # ---- persistence ------------------------------------------------------
    def _read_state_file(self):
        """The saved state ({} on a first run). If state.json can't be read (damaged by a power cut, say),
        use the spare copy; if that fails too, stop rather than start with an empty memory."""
        main, spare = self.state_path, self.state_path + ".bak"
        if not os.path.exists(main) and not os.path.exists(spare):
            return {}
        problem = ""
        for path in (main, spare):
            if not os.path.exists(path):
                problem = problem or "missing"
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    raise ValueError("not a state file")
            except (ValueError, OSError) as e:
                problem = problem or type(e).__name__
                continue
            if path == spare:
                self._notify(f"!! {os.path.basename(main)} could not be read ({problem}). Started from the spare copy: "
                             f"check the pot and your open positions against Robinhood.")
            return data
        sys.exit(f"\n{main} is damaged ({problem}) and there is no readable spare copy.\n"
                 f"Stopping here rather than starting with an empty memory: the project pot and open positions "
                 f"would be forgotten.\nTo start fresh, move that file somewhere else, set PROJECT_CAPITAL to what is "
                 f"really left, and check Robinhood for open positions.")

    def load_state(self):
        data = self._read_state_file()
        if data:
            allp = [Position(**p) for p in data.get("positions", [])]
            live_mode = self.broker is not None
            # paper positions are never mixed with real ones: drop the ones from the other mode
            dropped = [p for p in allp if p.real != live_mode]
            self.positions = [p for p in allp if p.real == live_mode]
            self.carried_over = dropped            # kept in the file so nothing is lost
            if dropped:
                kind = "REAL" if not live_mode else "paper"
                print(f"Note: {len(dropped)} {kind} position(s) in the state file belong to the other mode and "
                      f"are not managed in this run: " + ", ".join(p.contract for p in dropped))
            self.pending_sells = data.get("pending_sells", [])
            self.inflight = data.get("inflight", [])
            led = data.get("ledger", {})
            self.live_realized = float(led.get("live_realized", 0.0))
            self.project_started = led.get("started", "")
            self.loss_alerts = list(led.get("loss_alerts", []))
            self.his_shorts = set(data.get("his_shorts", []))
            self.seen_buys = set(data.get("seen_buys", []))
            self.watching = dict(data.get("watching", {}))
            self.processed_ids = data.get("processed_ids", [])
            self.last_msg_id = data.get("last_msg_id", 0)
            if self.tracker:
                self.tracker.load(data.get("tracked", []))

    def save_state(self):
        data = {"positions": [asdict(p) for p in self.positions + list(getattr(self, "carried_over", []))],
                "pending_sells": self.pending_sells,
                "inflight": self.inflight,
                "ledger": {"live_realized": round(self.live_realized, 2), "started": self.project_started,
                           "loss_alerts": self.loss_alerts},
                "his_shorts": sorted(self.his_shorts),
                "seen_buys": sorted(self.seen_buys),
                "watching": self.watching,
                "last_msg_id": self.last_msg_id,
                "processed_ids": self.processed_ids[-2000:],
                "tracked": self.tracker.to_json() if self.tracker else []}
        # Write a new file and swap it in, so a crash or power cut mid-write can't leave half a file.
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        for _ in range(5):
            try:
                os.replace(tmp, self.state_path)
                break
            except PermissionError:          # Windows: another program has the file open for a moment
                time.sleep(0.05)
        else:
            with open(self.state_path, "w", encoding="utf-8") as f:     # last resort: write in place
                json.dump(data, f, indent=2)
        # a spare copy of the same state, in case the main file is ever unreadable
        try:
            shutil.copyfile(self.state_path, self.state_path + ".bak")
        except OSError:
            pass

    def log(self, row: dict):
        row.setdefault("msg_id", self.ctx.get("msg_id", ""))
        row.setdefault("source", self.ctx.get("source", ""))
        row.setdefault("logged_at", self.now().strftime("%Y-%m-%d %H:%M:%S"))
        append_csv(self.log_path, self.LOG_COLS, row)

    def _rows_today(self):
        """Today's rows of the decision log, including any still waiting to be written (file open in Excel).
        Raises OSError if the file can't be read."""
        today = self.now().strftime("%Y-%m-%d")
        rows = []
        if os.path.exists(self.log_path):
            with open(self.log_path, encoding="utf-8-sig", errors="replace") as f:
                rows = list(csv.DictReader(f))
        rows += [{c: "" if r.get(c) is None else str(r[c]) for c in self.LOG_COLS}
                 for r in _CSV_WAITING.get(self.log_path, [])]
        return [r for r in rows if (r.get("logged_at") or "").startswith(today)]

    # ---- market clock -----------------------------------------------------
    def market_open(self, t=None):
        return market_is_open(t or self.now())

    # ---- helpers ----------------------------------------------------------
    def deployed(self):
        return sum(p.entry_price * 100 * p.qty_open for p in self.positions)

    def realized_today(self):
        return sum(float(r["our_pnl"]) for r in self._rows_today() if r.get("our_pnl"))

    def find_position(self, a: Alert):
        """Match a sell alert to what we hold.
        - exact match on ticker + strike + C/P + expiry -> sell
        - ticker off by one letter (NVIS vs NBIS), everything else exact,
          and only one such position -> sell, with a note
        Anything looser (wrong strike, wrong date) is NOT sold automatically."""
        live = [p for p in self.positions if p.qty_open > 0]
        same = [p for p in live if p.strike == a.strike and p.cp == a.cp and p.exp == a.exp.isoformat()]
        exact = [p for p in same if p.ticker == a.ticker]
        if exact:
            return exact[0], ""
        close = [p for p in same if edit_distance(p.ticker, a.ticker) <= 1]
        if len(close) == 1:
            return close[0], f"ticker typo? alert says {a.ticker}, matched your {close[0].ticker}"
        return None, ""

    # ---- closest match for mistyped exit alerts -----------------------------------
    def his_open(self):
        """Every contract he is in right now: what the tracker follows (all his buys)
        plus anything we hold. Each as (ticker, strike, cp, expiry-ISO)."""
        out = []
        if self.tracker:
            out += [(t.ticker, t.strike, t.cp, t.exp) for t in self.tracker.open_contracts()]
        out += [(p.ticker, p.strike, p.cp, p.exp) for p in self.positions if p.qty_open > 0]
        return list(dict.fromkeys(out))

    def _price_check(self, price, quote):
        """(True/False/None, text). Is his exit price consistent with the contract's live quote?"""
        if not quote:
            return None, "no live quote to check his price against"
        bid, ask, mark = quote.get("bid"), quote.get("ask"), quote.get("mark")
        if bid is None or ask is None:
            if mark is None:
                return None, "no live quote to check his price against"
            bid = ask = mark
        tol = self.s.typo_price_tol_pct / 100
        lo, hi = bid - max(0.05, tol * bid), ask + max(0.05, tol * ask)
        ok = lo <= price <= hi
        return ok, f"his price {price:g} {'fits' if ok else 'does not fit'} live bid/ask {bid:g}/{ask:g}"

    def resolve_exit(self, a: Alert, alert_time: datetime, source: str):
        """A sell/all-out alert that names a contract he is not in is usually a typo.
        Look for the ONE contract he IS in that differs from the text in exactly one
        field (ticker by a letter, strike, call/put, or expiry), and accept it only if
        his price agrees with that contract's live bid/ask.
        Returns (status, alert, note, candidates):
          exact  - the text matches a contract he is in
          fixed  - matched the closest contract; alert is rewritten to it
          check  - looks like a typo but can't be confirmed: logged, nothing applied
          none   - nothing close (e.g. a contract we never saw him buy)"""
        mine = self.his_open()
        iso = a.exp.isoformat()
        if (a.ticker, a.strike, a.cp, iso) in mine:
            return "exact", a, "", []
        key = contract_key(a.ticker, a.strike, a.cp, a.exp)
        if key in self.his_shorts or key in self.seen_buys:
            return "none", a, "", []        # a real contract he already dealt with - not a typo
        cands = []
        for tk, k, cp, ex in mine:
            d_tk = edit_distance(tk, a.ticker)
            if d_tk > 1:
                continue
            diffs = [n for n, bad in (("ticker", d_tk > 0), ("strike", k != a.strike),
                                      ("C/P", cp != a.cp), ("date", ex != iso)) if bad]
            if len(diffs) == 1:
                cands.append(((tk, k, cp, ex), diffs[0]))
        if not cands:
            return "none", a, "", []

        fresh = source == "live" and (self.now() - alert_time).total_seconds() <= self.s.typo_max_age_sec
        verdicts = []                       # (candidate, field, ok, text)
        for c, field_ in cands:
            tk, k, cp, ex = c
            if field_ == "ticker":
                ok, txt = True, "only the ticker letters differ"
            elif not fresh:
                ok, txt = None, "alert is old, so his price can't be checked against the live quote"
            else:
                ok, txt = self._price_check(a.price, self.quotes(tk, date.fromisoformat(ex), k, cp))
            verdicts.append((c, field_, ok, txt))
        names = [f"{tk} {k:g}{cp} {date.fromisoformat(ex):%m/%d}" for (tk, k, cp, ex), _, _, _ in verdicts]
        good = [v for v in verdicts if v[2]]
        if len(good) == 1:
            (tk, k, cp, ex), field_, _, txt = good[0]
            fixed = replace(a, ticker=tk, strike=k, cp=cp, exp=date.fromisoformat(ex))
            return "fixed", fixed, f"alert said {a.contract}, matched his open {fixed.contract} ({field_}; {txt})", \
                [good[0][0]]
        if len(good) > 1:
            return "check", a, "more than one of his open contracts fits", [v[0] for v in good]
        why = "; ".join(f"{n} ({f}: {t})" for n, (_, f, _, t) in zip(names, verdicts))
        return "check", a, why, [v[0] for v in verdicts]

    def on_exit_check(self, a: Alert, alert_time: datetime, cands, why: str):
        names = ", ".join(f"{tk} {k:g}{cp} {date.fromisoformat(ex):%m/%d}" for tk, k, cp, ex in cands)
        row = {"alert_time": f"{alert_time:%m/%d %H:%M:%S}",
               "delay_sec": f"{(self.now() - alert_time).total_seconds():.1f}",
               "raw": a.raw, "action": a.action, "contract": a.contract, "his_price": a.price,
               "bucket": expiry_bucket(a.exp, alert_time.date())[0],
               "decision": "CHECK", "reason": f"possible typo of his open {names} - {why}; NOT applied"}
        self.log(row)
        self.notify(f"!! CHECK: exit alert '{a.raw}' doesn't match anything he holds, but may mean {names}. "
                    f"{why}. Not applied.")
        if self.tracker:
            for tk, k, cp, ex in cands:
                t = self.tracker.items.get(contract_key(tk, k, cp, date.fromisoformat(ex)))
                if t:
                    self.tracker.possible_exit(t, a, alert_time, why)
        return ("CHECK", a.contract, names)

    def near_misses(self, a: Alert):
        """Positions that are probably what a mistyped sell alert meant
        (same/similar ticker, but strike, C/P or date doesn't line up)."""
        return [p for p in self.positions if p.qty_open > 0
                and edit_distance(p.ticker, a.ticker) <= 1
                and (p.strike, p.cp, p.exp) != (a.strike, a.cp, a.exp.isoformat())]

    # ---- main entry -------------------------------------------------------
    def handle_message(self, text: str, alert_time: datetime, msg_id=None, source="live"):
        with self.lock:                 # one thing at a time: alerts and the timer share positions
            return self._handle_message(text, alert_time, msg_id, source)

    def _handle_message(self, text: str, alert_time: datetime, msg_id=None, source="live"):
        """source = 'live' (arrived just now) or 'catch-up' (posted while the bot was off)."""
        if msg_id is not None:
            if msg_id in self.processed_ids:
                return []            # already handled (Telegram can deliver twice after a reconnect)
            self.processed_ids.append(msg_id)
            self.last_msg_id = max(self.last_msg_id, msg_id)
        self.ctx = {"msg_id": msg_id if msg_id is not None else "", "source": source}
        out = []
        today = self.now().date()
        for raw, alert, reason in parse_message(text, alert_time.date()):
            if alert is None:
                if reason.startswith("SHORT"):
                    key = find_contract_key(clean_line(raw), alert_time.date())
                    if key and reason.startswith("SHORT OPEN"):
                        self.his_shorts.add(key)
                if reason.startswith("CORRECTION"):
                    out.append(self.on_correction(raw, alert_time))
                    continue
                if reason.startswith("UNREADABLE"):
                    self.notify(f"!! Couldn't read this alert - check it yourself: {raw}")
                if reason != "not an alert (info text)":
                    self.log({"alert_time": f"{alert_time:%m/%d %H:%M:%S}", "raw": raw,
                              "decision": "IGNORE", "reason": reason})
                out.append(("IGNORE", raw, reason))
                continue
            if alert.action == "BUY":
                if self.tracker and contract_key(alert.ticker, alert.strike, alert.cp, alert.exp) not in self.his_shorts:
                    self.tracker.start(alert, alert_time, source)
                result = self.on_buy(alert, alert_time, source)
            else:
                status, alert, fix_note, cands = self.resolve_exit(alert, alert_time, source)
                if status == "check":
                    result = self.on_exit_check(alert, alert_time, cands, fix_note)
                else:
                    self._end_watch(contract_key(alert.ticker, alert.strike, alert.cp, alert.exp),
                                    "he sold before the price came back into range")
                    if self.tracker:
                        self.tracker.his_sell(alert, alert_time, source, typo_note=fix_note)
                    result = self.on_sell(alert, alert_time, fix_note=fix_note)
            out.append(result)
            # remember the last real alert, so a correction can point back to it
            self.last_alert = {"raw": raw, "time": f"{alert_time:%H:%M:%S}",
                               "outcome": " - ".join(str(x) for x in result if x is not None)}
        self.save_state()
        return out

    # ---- his corrections and edits: log only, no trading -------------------
    def on_correction(self, raw, alert_time):
        prev = getattr(self, "last_alert", None)
        about = (f"probably fixes [{prev['time']}] {prev['raw']}  (bot did: {prev['outcome']})"
                 if prev else "no earlier alert this session")
        self.log({"alert_time": f"{alert_time:%H:%M:%S}", "raw": raw, "action": "CORRECTION",
                  "decision": "LOGGED", "reason": f"correction, not acted on; {about}"})
        self.notify(f"Correction posted: '{raw}' - {about}. Logged only, no trade.")
        return ("CORRECTION", raw, about)

    def on_edit(self, text, edited_at):
        with self.lock:                 # the log and self.ctx are shared with alerts and the timer
            self.ctx = {}
            self.log({"alert_time": f"{edited_at:%H:%M:%S}", "raw": text, "action": "EDITED",
                      "decision": "LOGGED", "reason": "he edited an earlier message; not acted on"})
            self.notify(f"Alert was EDITED (logged only, no trade): {text}")

    # ---- buys -------------------------------------------------------------
    def on_buy(self, a: Alert, alert_time: datetime, source="live", from_watch=False):
        now = self.now()
        delay = (now - alert_time).total_seconds()
        row = {"alert_time": f"{alert_time:%m/%d %H:%M:%S}", "delay_sec": f"{delay:.1f}",
               "raw": a.raw, "action": "BUY", "contract": a.contract, "his_price": a.price,
               "bucket": expiry_bucket(a.exp, alert_time.date())[0], "_watched": from_watch}

        def skip(reason, transient=False):
            if from_watch and transient:
                return ("WAIT", a.contract, reason)      # still being watched: stay quiet, try again
            if from_watch:
                reason += " [watch ended]"
                self.watching.pop(key, None)
            row.update(decision="SKIP", reason=reason)
            self.log(row)
            self.notify(f"SKIP buy {a.contract} @ {a.price} - {reason}")
            return ("SKIP", a.contract, reason)

        key = contract_key(a.ticker, a.strike, a.cp, a.exp)
        if key in self.his_shorts:
            return skip("he is buying back his own short (CSP / covered call) - not a new long")
        self.seen_buys.add(key)
        self.save_state()
        if key in self.watching and not from_watch:
            return skip("already watching this contract for a better price")
        if source != "live":
            return skip(f"missed - posted {alert_time:%m/%d %H:%M} while the bot was off")
        if a.cp == "P" and not self.s.allow_puts:
            return skip("puts turned off (ALLOW_PUTS=false)")
        lotto = is_lotto(a.raw)
        if os.path.exists(self.stop_file):
            return skip("STOP_TRADING file present - no new buys")
        if self.broker:
            if self.halted:
                return skip(f"TRADING HALTED: {self.halted}")
            if self.project_over():
                return skip(f"PROJECT ENDED: the ${self.s.project_capital:,.0f} of capital is gone")
        if lotto and a.price >= self.s.lotto_max_price:
            return skip(f"lotto priced {a.price:g} - lottos only under {self.s.lotto_max_price:g}")

        # rules that need no quote
        if not self.market_open(now):
            return skip("market closed")
        open_at = datetime.combine(now.date(), dtime(9, 30), ET)
        if now < open_at + timedelta(minutes=self.s.open_buffer_min):
            return skip(f"first {self.s.open_buffer_min:g} min after open")
        if now.time() >= self.s.no_new_buys_after and a.exp == now.date():
            return skip("too late on expiry day")
        if a.exp < now.date():
            return skip("already expired")
        bucket = row["bucket"]
        age_limit = self._late_limit_sec(bucket)
        if not from_watch and delay > age_limit:
            return skip(f"alert is {delay:.0f}s old (limit {age_limit:.0f}s for {bucket})")
        late_swing = bucket == "swing" and delay > self.s.max_alert_age_sec
        if late_swing and self.tracker:
            t = self.tracker.items.get(key)
            if t and Fraction(t.his_sold) > 0:
                return skip(f"late entry, but he has already sold {t.his_sold} of it")
        if any(p.contract == a.contract and p.qty_open > 0 for p in self.positions):
            return skip("already holding this contract")
        if self.s.one_position_per_stock:
            same = [p for p in self.positions if p.ticker == a.ticker and p.qty_open > 0]
            if same:
                return skip(f"already holding {same[0].contract} - one position per stock", transient=True)
        if len([p for p in self.positions if p.qty_open > 0]) >= self.s.max_open_positions:
            return skip(f"max {self.s.max_open_positions} open positions", transient=True)
        try:
            lost_today, bought_today = self.realized_today(), self.buys_today()
        except OSError as e:
            # can't check the daily limits -> don't buy
            return skip(f"can't read today's log to check the daily limits ({type(e).__name__}) - "
                        f"is it open in another program?", transient=True)
        if lost_today <= -self.s.daily_loss_limit:
            return skip("daily loss limit hit")
        if bought_today >= self.s.max_buys_per_day:
            return skip(f"already made {self.s.max_buys_per_day} buys today (MAX_BUYS_PER_DAY)")
        if late_swing and not from_watch:
            # An alert this late came in a burst (bot asleep or offline), with any sell he posted queued right
            # behind it: look again in a few seconds instead of buying now. His sell ends the watch.
            return self._start_watch(a, alert_time, key, row, f"alert arrived {_ago(delay)} late",
                                     until=alert_time + timedelta(minutes=self.s.late_entry_min_swing),
                                     first_check=now + timedelta(seconds=15))

        q = self.quotes(a.ticker, a.exp, a.strike, a.cp)
        if not q or not q.get("ask") or not q.get("bid"):
            return skip("contract not found on Robinhood or no quote (typo in ticker/strike/date?)", transient=True)
        bid, ask = q["bid"], q["ask"]
        mid = (bid + ask) / 2
        spread_pct = (ask - bid) / mid * 100 if mid else 999
        row.update(bid=bid, ask=ask, spread_pct=f"{spread_pct:.1f}")

        cap = round(a.price * (1 + self.s.entry_cap_pct / 100), 2)
        if spread_pct > self.s.max_spread_pct:
            why = f"spread {spread_pct:.0f}% > {self.s.max_spread_pct:g}%"
            if not from_watch and self.s.spread_recheck_sec > 0:
                return self._start_watch(a, alert_time, key, row, why, kind="spread",
                                         until=now + timedelta(seconds=self.s.spread_recheck_sec))
            return skip(why, transient=True)
        if ask > cap:
            why = f"ask {ask} above cap {cap} (+{(ask / a.price - 1) * 100:.0f}% vs his {a.price})"
            # a new alert, or a spread re-check whose spread has narrowed but whose ask is above the cap
            if self._can_watch(a, now) and (not from_watch or self.watching.get(key, {}).get("kind") == "spread"):
                return self._start_watch(a, alert_time, key, row, why)
            return skip(why, transient=True)
        floor = a.price * (1 - self.s.entry_floor_pct / 100)
        if ask < floor:
            # Far CHEAPER than his price is not a bargain - it usually means a typo
            # (wrong strike/date/price) and we'd be buying a different contract.
            return skip(f"ask {ask} is {(1 - ask / a.price) * 100:.0f}% below his {a.price} - "
                        f"probably a typo / wrong contract", transient=True)

        if lotto and ask >= self.s.lotto_max_price:
            return skip(f"lotto, but the ask {ask:g} is not under {self.s.lotto_max_price:g}", transient=True)

        cost_one = ask * 100
        qty = self.s.contracts_per_trade            # 1 contract per alert, or 2 with a runner (below)
        if cost_one > self.s.max_single_contract_cost:
            return skip(f"1 contract costs ${cost_one:.0f} > ${self.s.max_single_contract_cost:g} limit", transient=True)
        # runner: 2 contracts when both fit within MAX_SINGLE_CONTRACT_COST and the other money limits (never a lotto)
        runner = (self.s.keep_runner and qty == 1 and not lotto and 2 * cost_one <= self.s.max_single_contract_cost
                  and self.deployed() + 2 * cost_one <= self.s.max_deployed
                  and (not self.broker or 2 * cost_one <= self.capital_free()))
        if runner:
            qty = 2
        if self.deployed() + qty * cost_one > self.s.max_deployed:
            return skip(f"would exceed ${self.s.max_deployed:g} deployed", transient=True)

        if self.broker:
            free = self.capital_free()
            if qty * cost_one > free:
                return skip(f"project capital: ${free:,.0f} free (${self.capital_left():,.0f} left, rest is in open "
                            f"positions), this needs ${qty * cost_one:,.0f}", transient=True)
            return self._buy_live(a, row, qty, ask, cap, lotto, skip, runner)

        self.positions.append(Position(
            ticker=a.ticker, strike=a.strike, cp=a.cp, exp=a.exp.isoformat(),
            qty_orig=qty, qty_open=qty, entry_price=ask, his_entry=a.price,
            opened_at=now.isoformat(timespec="seconds"), lotto=lotto, runner=runner))
        self.save_state()
        slip = (ask / a.price - 1) * 100
        row.update(decision="WOULD BUY", qty=qty, our_price=ask,
                   reason=f"limit {cap}; paying {slip:+.1f}% vs his price" + ("; LOTTO" if lotto else "")
                          + ("; RUNNER" if runner else ""))
        self.log(row)
        self.notify(buy_message("WOULD BUY", qty, a.contract, ask, a.price, lotto, delay, from_watch, runner))
        if self.watchlist:
            self.watchlist(a.ticker)
        return ("WOULD BUY", a.contract, qty, ask)

    # ---- watch-and-buy: ask was above our cap, keep looking for a while ----------
    def _can_watch(self, a: Alert, now):
        return self.s.watch_min > 0 and (a.exp - now.date()).days > self.s.watch_min_dte

    def _late_limit_sec(self, bucket):
        """How long after he posts (seconds) a buy alert may still be acted on, by expiry bucket."""
        mins = {"short": self.s.late_entry_min_short, "swing": self.s.late_entry_min_swing}.get(
            bucket, self.s.late_entry_min)
        return max(self.s.max_alert_age_sec, mins * 60)

    def _start_watch(self, a: Alert, alert_time: datetime, key, row, why, kind="price", until=None, first_check=None):
        """kind = price (ask above the cap: watch up to WATCH_MIN) | spread (spread too wide: re-check for
        SPREAD_RECHECK_SEC). Either way every buy rule is checked again before buying, and his sell ends it."""
        until = until or alert_time + timedelta(minutes=self.s.watch_min)
        self.watching[key] = {"raw": a.raw, "alert_time": alert_time.isoformat(timespec="seconds"),
                              "until": until.isoformat(timespec="seconds"), "tries": 0,
                              "msg_id": self.ctx.get("msg_id", ""), "kind": kind,
                              "not_before": first_check.isoformat(timespec="seconds") if first_check else ""}
        self.save_state()
        if kind == "spread":
            reason = (f"{why}. RE-CHECKING for {self.s.spread_recheck_sec:g} s: buys if the spread narrows and "
                      f"every other rule still passes, unless he sells first")
            text = (f"RE-CHECKING {a.contract} (his {a.price}): {why}. Will buy if the spread narrows within "
                    f"{self.s.spread_recheck_sec:g} s, unless he sells first.")
        else:
            mins = (until - alert_time).total_seconds() / 60
            reason = (f"{why}. WATCHING until {until:%H:%M} ({mins:.0f} min after his alert): buys once the ask "
                      f"is within the cap, unless he sells first")
            text = (f"WATCHING {a.contract} (his {a.price}): {why}. Will buy once the ask is within the cap, "
                    f"before {until:%H:%M}, unless he sells first.")
        row.update(decision="WATCH", reason=reason)
        self.log(row)
        self.notify(text)
        return ("WATCH", a.contract, reason)

    def _end_watch(self, key, why, log_row=True):
        rec = self.watching.pop(key, None)
        if rec is None:
            return
        self.save_state()
        alert, _ = parse_line(rec["raw"], datetime.fromisoformat(rec["alert_time"]).date())
        name = alert.contract if alert else key
        if log_row:
            self.log({"alert_time": datetime.fromisoformat(rec["alert_time"]).strftime("%m/%d %H:%M:%S"),
                      "raw": rec["raw"], "action": "BUY", "contract": name, "decision": "WATCH ENDED",
                      "reason": why})
        self.notify(f"Stopped watching {name}: {why}")

    def _poll_watches(self, now):
        if not self.watching:
            return
        if self._last_watch and (now - self._last_watch).total_seconds() < self.s.exit_check_sec:
            return
        self._last_watch = now
        for key, rec in list(self.watching.items()):
            alert_time = datetime.fromisoformat(rec["alert_time"])
            until = datetime.fromisoformat(rec["until"])
            if now >= until:
                if rec.get("kind") == "spread":
                    why = (f"re-checked for {self.s.spread_recheck_sec:g} s and it never qualified "
                           f"(last check: {rec.get('last') or 'spread still too wide'})")
                else:
                    why = (f"{(until - alert_time).total_seconds() / 60:.0f} minutes passed and it never qualified "
                           f"(last check: {rec.get('last') or 'ask above the cap'})")
                self._end_watch(key, why)
                continue
            if not self.market_open(now):
                self._end_watch(key, "market closed")
                continue
            if rec.get("not_before") and now < datetime.fromisoformat(rec["not_before"]):
                continue
            alert, _ = parse_line(rec["raw"], alert_time.date())
            if alert is None:
                self._end_watch(key, "could not re-read the alert")
                continue
            self.ctx = {"msg_id": rec.get("msg_id", ""), "source": "watch"}
            res = self.on_buy(alert, alert_time, "live", from_watch=True)
            if res[0] in ("BOUGHT", "WOULD BUY"):
                self.watching.pop(key, None)
                self.save_state()
            elif res[0] == "WAIT":
                rec["last"] = res[2]
            elif res[0] == "NOT FILLED":
                rec["tries"] = rec.get("tries", 0) + 1
                if rec["tries"] >= self.s.watch_max_tries:
                    self._end_watch(key, f"{rec['tries']} buy orders did not fill")
                else:
                    self.save_state()
            elif res[0] == "ERROR":
                self.watching.pop(key, None)
                self.save_state()

    def _track_order(self, order_id, active):
        """Remember orders that are still open, so a crash can't leave one forgotten."""
        if active and order_id not in self.inflight:
            self.inflight.append(order_id)
        if not active and order_id in self.inflight:
            self.inflight.remove(order_id)
        self.save_state()

    def _buy_live(self, a: Alert, row: dict, qty: int, ask: float, cap: float, lotto: bool, skip, runner=False):
        """Place a REAL limit buy: ask plus a small cushion, never above our cap."""
        b = self.broker
        tick = b.tick_size(a.ticker, a.exp, a.strike, a.cp, ask)
        top = round_to_tick(cap, tick, "down")                       # highest valid price within the cap
        limit = round(min(round_to_tick(ask, tick, "up") + self.s.buy_cushion_ticks * tick, top), 2)
        if limit < ask - 1e-9:
            return skip(f"ask {ask} is above the cap {cap} once rounded to valid price steps", transient=True)
        bp = b.buying_power()
        cushion = 1 + self.s.min_buying_power_buffer_pct / 100
        two = 2 * limit * 100
        if runner and (two > self.s.max_single_contract_cost or two > self.capital_free()
                       or (bp is not None and bp < two * cushion)):
            qty, runner = 1, False          # at the limit price (a step above the ask) 2 no longer fit: buy 1
        need = limit * 100 * qty
        if need > self.capital_free():
            return skip(f"project capital: ${self.capital_free():,.0f} free, the order needs ${need:,.0f}", transient=True)
        if bp is not None and bp < need * cushion:
            return skip(f"not enough buying power (${bp:,.0f} available, ${need:,.0f} needed)", transient=True)
        res = b.buy(a.ticker, a.exp, a.strike, a.cp, qty, limit, on_order=self._track_order)
        if res.state == "unknown":
            self.halted = f"could not confirm the state of buy order {res.order_id} on {a.contract}"
            row.update(decision="ERROR", reason=f"{res.note}; NEW BUYS HALTED")
            self.log(row)
            self.notify(f"!! BUY ORDER STATE UNKNOWN for {a.contract} (order {res.order_id}). "
                        f"New buys are halted. Open Robinhood now and check/cancel the order by hand.")
            return ("ERROR", a.contract, res.note)
        if res.qty <= 0:
            row.update(decision="NOT FILLED", reason=f"limit {limit} - {res.note or res.state}")
            self.log(row)
            self.notify(f"NOT FILLED: buy {a.contract} limit {limit} ({res.note or res.state}). No position taken.")
            return ("NOT FILLED", a.contract, res.state)
        fill = res.price or limit
        runner = runner and res.qty == 2            # only 1 of the 2 filled: an ordinary 1-contract position
        self.positions.append(Position(
            ticker=a.ticker, strike=a.strike, cp=a.cp, exp=a.exp.isoformat(),
            qty_orig=res.qty, qty_open=res.qty, entry_price=fill, his_entry=a.price,
            opened_at=self.now().isoformat(timespec="seconds"), real=True, lotto=lotto, runner=runner))
        self.save_state()
        slip = (fill / a.price - 1) * 100
        row.update(decision="BOUGHT", qty=res.qty, our_price=fill,
                   reason=f"LIVE order {res.order_id}: limit {limit}, filled {fill}; {slip:+.1f}% vs his price"
                          + ("; LOTTO" if lotto else "") + ("; RUNNER" if runner else "")
                          + (f"; only {res.qty} of {qty} filled" if res.qty < qty else ""))
        self.log(row)
        self.notify(buy_message("BOUGHT", res.qty, a.contract, fill, a.price, lotto,
                                float(row.get("delay_sec") or 0), bool(row.get("_watched")), runner))
        if self.watchlist:
            self.watchlist(a.ticker)
        return ("BOUGHT", a.contract, res.qty, fill)

    def startup_live(self):
        """Run once when live mode starts: clean up orders left open by a crash, then make the
        bot's books match Robinhood's. Returns a list of things to tell you."""
        msgs = []
        for oid in list(self.inflight):
            info = self.broker.order_info(oid)
            st = info.get("state")
            if st == "filled":
                msgs.append(f"order {oid} filled while the bot was not tracking it - check Robinhood")
            elif st and st not in Broker.BAD:
                try:
                    self.broker.api.cancel(oid)
                    msgs.append(f"cancelled leftover order {oid} (was {st})")
                except Exception as e:
                    msgs.append(f"could not cancel leftover order {oid} ({e}) - check Robinhood")
            self.inflight.remove(oid)
        if not self.project_started:
            self.project_started = self.now().isoformat(timespec="seconds")
        rec, held = self.broker.reconcile(self.positions)
        msgs += rec
        if held is not None:
            have = {(t, e, k, c): q for t, e, k, c, q in held}
            for p in list(self.positions):
                if p.real:
                    n = have.get((p.ticker, p.exp, p.strike, p.cp), 0)
                    if n <= 0:
                        self.positions.remove(p)
                    elif n < p.qty_open:
                        p.qty_open = n
        self.save_state()
        return msgs

    # ---- the project's money: PROJECT_CAPITAL at the start, live P&L tracked against it ----
    def capital_left(self):
        """Project money not yet lost: starting capital plus real profit/loss so far."""
        return self.s.project_capital + self.live_realized

    def capital_free(self):
        """...and not currently tied up in open real positions (counted at what we paid)."""
        return self.capital_left() - sum(p.entry_price * 100 * p.qty_open for p in self.positions if p.real)

    def project_over(self):
        return self.capital_left() <= 0

    def _after_live_pnl(self, pnl):
        self.live_realized += pnl
        left = self.capital_left()
        if self.project_over():
            self._notify(f"!! PROJECT ENDED: the ${self.s.project_capital:,.0f} is gone "
                         f"(live P&L ${self.live_realized:+,.0f}). No more buys. Open positions will still be exited.")
        for level in (50, 75, 90):
            lost = (1 - left / self.s.project_capital) * 100
            if lost >= level and level not in self.loss_alerts and left > 0:
                self.loss_alerts.append(level)
                self._notify(f"!! Project capital is down {level}%+: ${left:,.0f} left of ${self.s.project_capital:,.0f}.")
        self.save_state()

    def capital_line(self):
        return (f"Project capital: ${self.capital_left():,.0f} left of ${self.s.project_capital:,.0f} "
                f"(live P&L ${self.live_realized:+,.0f})")

    def buys_today(self):
        return sum(1 for r in self._rows_today() if r.get("decision") in ("WOULD BUY", "BOUGHT"))

    # ---- sells ------------------------------------------------------------
    def on_sell(self, a: Alert, alert_time: datetime, from_queue=False, fix_note=""):
        now = self.now()
        row = {"alert_time": f"{alert_time:%m/%d %H:%M:%S}",
               "delay_sec": f"{(now - alert_time).total_seconds():.1f}",
               "raw": a.raw, "action": a.action, "contract": a.contract, "his_price": a.price,
               "bucket": expiry_bucket(a.exp, alert_time.date())[0]}

        pos, typo_note = self.find_position(a)
        if fix_note:
            typo_note = f"{typo_note}; {fix_note}".strip("; ") if typo_note else fix_note
        if pos is None:
            misses = self.near_misses(a)
            if misses:
                held = ", ".join(f"{p.qty_open}x {p.contract}" for p in misses)
                row.update(decision="CHECK", reason=f"possible typo - you hold {held}; not sold automatically")
                self.log(row)
                self.notify(f"!! CHECK: he sold {a.contract} but you hold {held}. "
                            f"Typo? Not sold automatically - decide by hand.")
                return ("CHECK", a.contract, held)
            key = contract_key(a.ticker, a.strike, a.cp, a.exp)
            if key not in self.seen_buys and a.action == "SELL" and a.fraction is None:
                # He's selling something he never posted a buy for: most likely he is
                # opening a short (CSP/covered call). Remember it, so his later
                # "BOUGHT" of this same contract (the buy-back) is not copied.
                self.his_shorts.add(key)
                self.save_state()
                reason = "not holding; no earlier BUY seen - treated as his short, buy-back will be ignored"
            else:
                reason = "not holding this contract"
            if fix_note:
                reason += f" [{fix_note}]"
            row.update(decision="IGNORE", reason=reason)
            self.log(row)
            return ("IGNORE", a.contract, "not holding")

        if not self.market_open(now):
            self.pending_sells.append({"text": a.raw, "alert_time": alert_time.isoformat()})
            self.save_state()
            row.update(decision="QUEUED", reason="market closed - will sell at the open")
            self.log(row)
            self.notify(f"QUEUED sell {pos.contract} - market closed")
            return ("QUEUED", pos.contract)

        # how much he has sold so far, as a fraction of his original position
        if a.action == "ALL_OUT" or a.fraction is None:
            target = Fraction(1)
        else:
            target = min(Fraction(1), Fraction(pos.sold_fraction) + a.fraction)
        pos.sold_fraction = str(target)
        exact = target * pos.qty_orig
        if target == 1:
            want_sold = pos.qty_orig
        elif pos.runner:
            want_sold = 1          # the first contract goes at his first trim; the runner waits until he is fully out
        elif self.s.partial_rounding == "up":
            want_sold = math.ceil(exact)
        elif self.s.partial_rounding == "nearest":
            want_sold = math.floor(exact + Fraction(1, 2))      # 1 contract: sell once he has sold half or more
        else:
            want_sold = math.floor(exact)
        already_sold = pos.qty_orig - pos.qty_open
        to_sell = min(pos.qty_open, want_sold - already_sold)
        if pos.exit_wanted:                  # an exit is already being worked: don't start a second one
            pos.exit_wanted = max(pos.exit_wanted, to_sell)
            self.save_state()
            row.update(decision="PENDING", reason="an exit order for this contract is already being retried")
            self.log(row)
            return ("PENDING", pos.contract)

        if to_sell <= 0:
            self.save_state()
            if pos.runner_left:
                row.update(decision="HOLD", reason=f"he has sold {target} - holding the runner until he is fully out"
                           + (f"; {typo_note}" if typo_note else ""))
                self.log(row)
                self.notify(f"HOLD runner {pos.contract}: he sold {a.fraction} more. The runner is sold when he is "
                            f"fully out, or if its mid falls back to your buy price {pos.entry_price:g}.")
                return ("HOLD", pos.contract)
            row.update(decision="HOLD", reason=f"{target} of {pos.qty_orig} rounds to 0 more - holding"
                       + (f"; {typo_note}" if typo_note else ""))
            self.log(row)
            self.notify(f"HOLD {pos.contract}: he sold {a.fraction}, too small to split your {pos.qty_open}")
            return ("HOLD", pos.contract)

        why = ("sold at the open (queued)" if from_queue else "") + (" " + typo_note if typo_note else "")
        if pos.runner and to_sell < pos.qty_open:
            why = f"his first trim: 1 of 2 sold, runner kept {why}"
        return self._close(pos, to_sell, row, his_price=a.price, why=why)

    def _runner_kept(self, pos):
        """Say so once the first of a runner's 2 contracts has been sold."""
        if any(p is pos for p in self.positions) and pos.runner_left and not pos.exit_wanted:
            self._notify(f"RUNNER kept: 1x {pos.contract}. It is sold when he is fully out, or if its mid falls back "
                         f"to your buy price {pos.entry_price:g}.")

    def _close_live(self, pos, qty, row, his_price, why, q):
        """One attempt to sell for real. If it doesn't fill, the position stays flagged
        (exit_wanted) and the timer tries again at a lower price until it is out."""
        res = self.broker.sell_once(pos.ticker, date.fromisoformat(pos.exp), pos.strike, pos.cp, qty, q,
                                    pos.exit_attempts, on_order=self._track_order)
        # messages about real orders go out even while catching up on missed alerts (self._notify)
        if res.state == "unknown":
            self.halted = f"could not confirm the state of sell order {res.order_id} on {pos.contract}"
            self._notify(f"!! SELL ORDER STATE UNKNOWN for {pos.contract} (order {res.order_id}). "
                         f"New buys are halted. Check Robinhood now.")
        if res.qty > 0:
            price = res.price or (q or {}).get("bid") or 0.0
            pnl = (price - pos.entry_price) * 100 * res.qty
            his = (his_price - pos.his_entry) * 100 * res.qty if his_price else 0.0
            pos.qty_open -= res.qty
            pos.realized_pnl += pnl
            pos.his_pnl_same_qty += his
            pos.exit_wanted = max(0, qty - res.qty)
            if pos.qty_open <= 0:
                self.positions.remove(pos)
            elif pos.exit_wanted == 0:
                pos.exit_attempts = 0
            self._after_live_pnl(pnl)
            row.update(bid=(q or {}).get("bid"), ask=(q or {}).get("ask"), decision="SOLD", qty=res.qty,
                       our_price=price, our_pnl=f"{pnl:.2f}", his_pnl_same_qty=f"{his:.2f}",
                       reason=f"LIVE order {res.order_id}: {why.strip()}".strip(": "))
            self.log(row)
            self._notify(sell_message("SOLD", res.qty, pos.contract, price, pos.entry_price, his_price,
                                      pos.his_entry, pnl, his, why, pos.qty_open))
            self._runner_kept(pos)
            return ("SOLD", pos.contract, res.qty, price, round(pnl, 2))
        # not sold this time
        first = pos.exit_wanted == 0
        pos.exit_wanted = qty
        pos.exit_why = why.strip() or pos.exit_why
        if res.state == "failed":
            # Robinhood refused the order, so it was never open. See whether we still hold the contract
            # (it may have been sold by hand); if we do, retry more slowly and without lowering the price.
            # Only drop it when two refusals in a row both find it gone: one reading can be incomplete.
            have = self.broker.still_held(pos.ticker, pos.exp, pos.strike, pos.cp)
            if have == 0 and pos.exit_refused >= 1:
                return self._drop(pos, row, f"Robinhood refused the sell twice ({res.note or res.state}) and no "
                                            f"longer shows this position")
            if have and have < pos.qty_open:
                pos.qty_open = have
                pos.exit_wanted = min(qty, have)
            pos.exit_refused += 1
        else:
            pos.exit_refused = 0
            if res.state != "no_quote":
                pos.exit_attempts += 1
        self.save_state()
        n = pos.exit_attempts + pos.exit_refused
        if first or n in (3, 6) or n % 10 == 0:
            row.update(decision="SELL PENDING", qty=qty,
                       reason=f"attempt {n}: {res.note or res.state}; will keep retrying")
            self.log(row)
            self._notify(f"!! NOT SOLD YET: {pos.contract} (attempt {n}: {res.note or res.state}). "
                         f"Retrying. If this repeats, sell it by hand in Robinhood.")
        return ("SELL PENDING", pos.contract, n)

    def _drop(self, pos, row, why):
        """Take a real position off the bot's books without a sale of ours (sold by hand, say)."""
        self.positions.remove(pos)
        self.save_state()
        row.update(decision="DROPPED", qty=pos.qty_open,
                   reason=f"{why}; removed from the bot's books, P&L not counted in the pot")
        self.log(row)
        self._notify(f"!! {pos.contract}: {why}. Sold by hand? It is removed from the bot's books, and its profit "
                     f"or loss is NOT counted in the project pot.")
        return ("DROPPED", pos.contract)

    def retry_exits(self, now):
        if not self.broker or not self.market_open(now):
            return
        for pos in list(self.positions):
            if pos.real and pos.exit_wanted > 0 and pos.qty_open > 0:
                last = self._exit_tried.get(pos.contract)
                # normally every 3 s; after refused orders 10 s, 20 s, 40 s, then once a minute
                wait = min(60, 5 * 2 ** min(pos.exit_refused, 6)) if pos.exit_refused else 3
                if last and (now - last).total_seconds() < wait:
                    continue
                self._exit_tried[pos.contract] = now
                row = {"alert_time": "", "raw": "(retrying exit)", "action": "SELL_RETRY", "contract": pos.contract}
                self._close(pos, min(pos.exit_wanted, pos.qty_open), row, his_price=None, why=pos.exit_why)

    def check_stops(self, now):
        """Automatic exits, checked every ~10 s on each open position:
        - take profit: the BID is more than TAKE_PROFIT_PCT above what we paid (a price we can really sell at).
          With a runner it sells only the first of the 2 contracts; the runner itself has no take-profit;
        - runner stop: once only the runner is left, sell it if the MID falls back to what we paid;
        - disaster stop: the MID has fallen DISASTER_STOP_PCT below what we paid. He often posts no
          exit on losers, so without this a loser can ride to zero."""
        stop_pct, tp_pct = self.s.disaster_stop_pct, self.s.take_profit_pct
        if not self.market_open(now):
            return
        for pos in list(self.positions):
            if pos.qty_open <= 0 or pos.exit_wanted:
                continue
            runner_left = pos.runner_left
            if stop_pct <= 0 and tp_pct <= 0 and not runner_left:
                continue
            last = self._stop_checked.get(pos.contract)
            if last and (now - last).total_seconds() < self.s.exit_check_sec:
                continue
            self._stop_checked[pos.contract] = now
            q = self.quotes(pos.ticker, date.fromisoformat(pos.exp), pos.strike, pos.cp)
            if not q:
                continue
            bid = q.get("bid")
            if tp_pct > 0 and not runner_left and bid and bid > pos.entry_price * (1 + tp_pct / 100):
                row = {"alert_time": "", "raw": "(take profit)", "action": "TAKE_PROFIT", "contract": pos.contract}
                why = f"take profit: bid {bid:g} is {(bid / pos.entry_price - 1) * 100:+.0f}% over our {pos.entry_price:g}"
                self._close(pos, 1 if pos.runner else pos.qty_open, row, his_price=None,
                            why=why + ("; 1 of 2 sold, runner kept" if pos.runner else ""))
                continue
            ref = q.get("mark")
            if ref is None and bid is not None and q.get("ask") is not None:
                ref = (bid + q["ask"]) / 2
            if runner_left and ref is not None and ref <= pos.entry_price:
                row = {"alert_time": "", "raw": "(runner stop)", "action": "RUNNER_STOP", "contract": pos.contract}
                self._close(pos, pos.qty_open, row, his_price=None,
                            why=f"runner stop: mid {ref:g} is back to our buy price {pos.entry_price:g}")
                continue
            if stop_pct > 0 and ref is not None and ref <= pos.entry_price * (1 - stop_pct / 100):
                row = {"alert_time": "", "raw": "(disaster stop)", "action": "STOP", "contract": pos.contract}
                self._close(pos, pos.qty_open, row, his_price=None,
                            why=f"disaster stop: mid {ref:g} is {(1 - ref / pos.entry_price) * 100:.0f}% below our {pos.entry_price:g}")

    def _close(self, pos, qty, row, his_price, why=""):
        q = self.quotes(pos.ticker, date.fromisoformat(pos.exp), pos.strike, pos.cp)
        if self.broker and pos.real:
            return self._close_live(pos, qty, row, his_price, why, q)
        bid = q.get("bid") if q else None
        if bid is None:
            row.update(decision="ERROR", reason="no quote - in live mode YOU must sell by hand")
            self.log(row)
            self.notify(f"!! No quote for {pos.contract}. Live mode would need a manual sell.")
            return ("ERROR", pos.contract)
        pnl = (bid - pos.entry_price) * 100 * qty
        his = (his_price - pos.his_entry) * 100 * qty if his_price else 0.0
        pos.qty_open -= qty
        pos.realized_pnl += pnl
        pos.his_pnl_same_qty += his
        if pos.qty_open == 0:
            self.positions.remove(pos)
        self.save_state()
        row.update(bid=bid, ask=(q or {}).get("ask"), decision="WOULD SELL", qty=qty, our_price=bid,
                   our_pnl=f"{pnl:.2f}", his_pnl_same_qty=f"{his:.2f}", reason=why.strip())
        self.log(row)
        self.notify(sell_message("WOULD SELL", qty, pos.contract, bid, pos.entry_price, his_price,
                                 pos.his_entry, pnl, his, why, pos.qty_open))
        self._runner_kept(pos)
        return ("WOULD SELL", pos.contract, qty, bid, round(pnl, 2))

    # ---- things that run on a timer ----------------------------------------
    def tick(self):
        with self.lock:
            self._tick()

    def _tick(self):
        now = self.now()
        self.retry_exits(now)
        self.check_stops(now)
        self._poll_watches(now)
        if self.market_open(now) and self.pending_sells:
            queued, self.pending_sells = self.pending_sells, []
            self.save_state()
            for item in queued:
                alert, _ = parse_line(item["text"], now.date())
                if alert:
                    self.on_sell(alert, datetime.fromisoformat(item["alert_time"]), from_queue=True)
        # safety close on expiry day
        if self.market_open(now) and now.time() >= self.s.expiry_close_time:
            for pos in list(self.positions):
                if pos.exp == now.date().isoformat() and pos.qty_open > 0 and not pos.exit_wanted:
                    row = {"alert_time": "", "raw": "(safety close - expiry day)",
                           "action": "SAFETY_CLOSE", "contract": pos.contract}
                    self._close(pos, pos.qty_open, row, his_price=None,
                                why=f"expiry day {self.s.expiry_close_time:%H:%M} safety close")

    def summary(self):
        today = self.now().strftime("%Y-%m-%d")
        try:
            rows = self._rows_today()
        except OSError as e:
            rows = []
            print(f"summary: can't read today's log ({type(e).__name__}) - is it open in another program?")
        buys = [r for r in rows if r["decision"] in ("WOULD BUY", "BOUGHT")]
        skips = [r for r in rows if r["decision"] == "SKIP"]
        sells = [r for r in rows if r["decision"] in ("WOULD SELL", "SOLD")]
        ours = sum(float(r["our_pnl"] or 0) for r in sells)
        his = sum(float(r["his_pnl_same_qty"] or 0) for r in sells)
        slips = [float(r["our_price"]) / float(r["his_price"]) - 1 for r in buys if r["his_price"]]
        live = self.broker is not None
        lines = [f"{'LIVE' if live else 'Shadow'} summary {today}",
                 f"{'Buys' if live else 'Would-buys'}: {len(buys)}  Skips: {len(skips)}  Sells: {len(sells)}",
                 f"Avg entry vs his price: {sum(slips) / len(slips) * 100:+.1f}%" if slips else "Avg entry: n/a",
                 f"Closed P&L - ours: ${ours:+,.0f}   his (same size): ${his:+,.0f}",
                 *([self.capital_line()] if live else []),
                 f"Still open: {', '.join(f'{p.qty_open}x {p.contract}' for p in self.positions) or 'none'}"]
        for r in skips:
            lines.append(f"  skip {r['contract']}: {r['reason']}")
        if self.tracker:
            tl = self.tracker.summary_lines()
            if tl:
                lines.append("Price paths (vs his buy price):")
                lines += tl
        attention = [r for r in rows if r["decision"] == "CHECK" or r["reason"].startswith("UNREADABLE")
                     or r["action"] in ("CORRECTION", "EDITED")]
        if attention:
            lines.append("Needs your attention:")
            lines += [f"  {r['raw']}  ->  {r['reason']}" for r in attention]
        return "\n".join(lines)


# ----------------------------------------------------------------------------
# 4) Robinhood (read-only quotes + test watchlist)
# ----------------------------------------------------------------------------
class Robinhood:
    def __init__(self, settings):
        import robin_stocks.robinhood as rh
        self.rh = rh
        # robin_stocks sends its GET requests (quotes, order status, positions) with no timeout, so one
        # stuck connection would freeze alerts, stops and exits together. Give every request a default;
        # calls that pass their own (order posts use 16 s) keep theirs.
        sess = rh.helper.SESSION
        sess.request = functools.partial(sess.request, timeout=10)
        self.s = settings
        self._ids = {}
        self.watchlist_ok = False

    def login(self):
        # Re-uses the saved session (robinhood.pickle) if it is still valid; only asks for
        # email / password when it has expired. Approve in the Robinhood app if asked.
        user = os.environ.get("RH_USERNAME") or None
        pw = os.environ.get("RH_PASSWORD") or None
        print("Logging in to Robinhood (asks for email/password only if the saved session expired)...")
        res = self.rh.login(user, pw, store_session=True, pickle_path=DATA_DIR)
        if not res:
            sys.exit("Robinhood login failed.")
        print("Robinhood: logged in.")
        if self.s.add_to_watchlist:
            try:
                lists = self.rh.account.get_all_watchlists() or {}
                names = [w.get("display_name") for w in lists.get("results", [])]
                self.watchlist_ok = self.s.watchlist_name in names
                if not self.watchlist_ok:
                    print(f"Watchlist '{self.s.watchlist_name}' not found (you have: {names}). "
                          f"Create it in the Robinhood app; watchlist adds are off for now.")
            except Exception as e:
                print(f"Could not read watchlists ({e}); watchlist adds are off.")

    def quote(self, ticker, exp, strike, cp):
        key = (ticker, exp, strike, cp)
        try:
            if key not in self._ids:
                self._ids[key] = self.rh.helper.id_for_option(
                    ticker, exp.isoformat(), f"{strike:g}", "call" if cp == "C" else "put")
            oid = self._ids[key]
            if not oid:
                return None
            data = self.rh.options.get_option_market_data_by_id(oid)
            d = data[0] if isinstance(data, list) and data else data
            if not d:
                return None
            return self._parse(d)
        except Exception as e:
            print(f"quote error {ticker} {strike}{cp} {exp}: {e}")
            return None

    def _instrument_url(self, ticker, exp, strike, cp):
        key = (ticker, exp, strike, cp)
        if key not in self._ids:
            self._ids[key] = self.rh.helper.id_for_option(
                ticker, exp.isoformat(), f"{strike:g}", "call" if cp == "C" else "put")
        oid = self._ids[key]
        return f"https://api.robinhood.com/options/instruments/{oid}/" if oid else None

    @staticmethod
    def _parse(d):
        f = lambda k: float(d[k]) if d.get(k) not in (None, "") else None
        return {"bid": f("bid_price"), "ask": f("ask_price"), "mark": f("mark_price"),
                "iv": f("implied_volatility"), "delta": f("delta"), "volume": f("volume")}

    def quote_many(self, items):
        """One request for up to 10 contracts at a time; falls back to one-by-one."""
        out, urls = {}, {}
        for t in items:
            try:
                url = self._instrument_url(t.ticker, date.fromisoformat(t.exp), t.strike, t.cp)
                if url:
                    urls[url] = t.key
            except Exception as e:
                print(f"instrument lookup failed {t.contract}: {e}")
        url_list = list(urls)
        for i in range(0, len(url_list), 10):
            chunk = url_list[i:i + 10]
            try:
                res = self.rh.helper.request_get(self.rh.urls.marketdata_options_url(), "results",
                                                 {"instruments": ",".join(chunk)}) or []
                for d in res:
                    if not d:
                        continue
                    inst = d.get("instrument") or ""
                    if not inst and d.get("instrument_id"):
                        inst = f"https://api.robinhood.com/options/instruments/{d['instrument_id']}/"
                    if inst in urls:
                        out[urls[inst]] = self._parse(d)
            except Exception as e:
                print(f"batch quote failed ({e}); trying one by one")
        for t in items:                      # anything the batch call missed
            if t.key not in out:
                q = self.quote(t.ticker, date.fromisoformat(t.exp), t.strike, t.cp)
                if q:
                    out[t.key] = q
        return out

    def underlying_many(self, tickers):
        res = self.rh.stocks.get_quotes(list(tickers)) or []
        return {d["symbol"]: float(d["last_trade_price"]) for d in res
                if d and d.get("symbol") and d.get("last_trade_price")}

    def add_to_watchlist(self, ticker):
        if not self.watchlist_ok:
            return
        try:
            self.rh.account.post_symbols_to_watchlist(ticker, name=self.s.watchlist_name)
        except Exception as e:
            print(f"watchlist add failed for {ticker}: {e}")

    # ---- real orders (only called in live mode) ---------------------------------
    def option_id(self, ticker, exp, strike, cp):
        key = (ticker, exp, strike, cp)
        if key not in self._ids:
            self._ids[key] = self.rh.helper.id_for_option(
                ticker, exp.isoformat(), f"{strike:g}", "call" if cp == "C" else "put")
        return self._ids[key]

    def min_ticks(self, oid):
        if not hasattr(self, "_ticks"):
            self._ticks = {}
        if oid not in self._ticks:
            d = self.rh.options.get_option_instrument_data_by_id(oid) or {}
            self._ticks[oid] = d.get("min_ticks")
        return self._ticks[oid]

    def place(self, side, ticker, exp, strike, cp, qty, price):
        """Send one limit order, good for the day only. side = buy (to open) | sell (to close)."""
        kind = "call" if cp == "C" else "put"
        fn = self.rh.orders.order_buy_option_limit if side == "buy" else self.rh.orders.order_sell_option_limit
        effect, direction = ("open", "debit") if side == "buy" else ("close", "credit")
        return fn(effect, direction, round(price, 2), ticker, qty, exp.isoformat(), f"{strike:g}", kind,
                  timeInForce="gfd")

    def order_info(self, order_id):
        return self.rh.orders.get_option_order_info(order_id) or {}

    def cancel(self, order_id):
        return self.rh.orders.cancel_option_order(order_id)

    def open_orders(self):
        return self.rh.orders.get_all_open_option_orders() or []

    def buying_power(self):
        prof = self.rh.profiles.load_account_profile() or {}
        return float(prof["buying_power"]) if prof.get("buying_power") not in (None, "") else None

    def holdings(self):
        """Long option positions on the account: [(ticker, exp_iso, strike, 'C'/'P', qty)]."""
        out = []
        for p in self.rh.options.get_open_option_positions() or []:
            qty = float(p.get("quantity") or 0)
            if qty <= 0 or p.get("type") not in (None, "long"):
                continue
            oid = p.get("option_id") or (p.get("option") or "").rstrip("/").split("/")[-1]
            inst = self.rh.options.get_option_instrument_data_by_id(oid) or {}
            out.append((inst.get("chain_symbol") or p.get("chain_symbol"), inst.get("expiration_date"),
                        float(inst.get("strike_price")), "C" if inst.get("type") == "call" else "P", int(qty)))
        return out


# ----------------------------------------------------------------------------
# 4b) The broker: turns decisions into REAL orders. Only built when LIVE_TRADING=true.
#     Every order is a LIMIT order that is good for the day only. We wait for the fill,
#     cancel what doesn't fill, and confirm the final state before moving on.
# ----------------------------------------------------------------------------
@dataclass
class OrderResult:
    state: str = ""          # filled | cancelled | failed | unknown | no_quote
    qty: int = 0             # contracts actually filled
    price: float = 0.0       # average fill price per share (0 = unknown)
    order_id: str = ""
    note: str = ""


class Broker:
    orders_csv = _DynPath()
    BAD = ("cancelled", "canceled", "rejected", "failed")

    def __init__(self, api, settings, notify, orders_csv=ORDERS_CSV, sleep=time.sleep,
                 now=lambda: datetime.now(ET)):
        self.api = api
        self.s = settings
        self.notify = notify
        self.orders_csv = orders_csv
        self.sleep = sleep
        self.now = now

    # ---- small helpers ----------------------------------------------------------
    def _log(self, action, contract, qty, limit, order_id, state, fill, note=""):
        cols = ["ts", "action", "contract", "qty", "limit", "order_id", "state", "fill_price", "note"]
        append_csv(self.orders_csv, cols,
                   [f"{self.now():%Y-%m-%d %H:%M:%S}", action, contract, qty, limit, order_id, state, fill, note])

    def tick_size(self, ticker, exp, strike, cp, price):
        try:
            return tick_for(self.api.min_ticks(self.api.option_id(ticker, exp, strike, cp)), price)
        except Exception:
            return tick_for(None, price)

    def buying_power(self):
        try:
            return self.api.buying_power()
        except Exception as e:
            print(f"could not read buying power: {e}")
            return None

    def order_info(self, order_id):
        try:
            return self.api.order_info(order_id) or {}
        except Exception as e:
            print(f"order lookup failed ({order_id}): {e}")
            return {}

    @staticmethod
    def fill_of(info):
        """(contracts filled, average price per share) from an order record."""
        try:
            qty = int(float(info.get("processed_quantity") or 0))
        except Exception:
            qty = 0
        price = 0.0
        try:
            ex = [e for leg in info.get("legs", []) for e in leg.get("executions", [])]
            tot = sum(float(e["quantity"]) for e in ex)
            if tot:
                price = sum(float(e["price"]) * float(e["quantity"]) for e in ex) / tot
                qty = qty or int(tot)
        except Exception:
            pass
        if not price and qty:
            try:
                price = float(info.get("processed_premium")) / (qty * 100)
            except Exception:
                pass
        return qty, round(price, 4)

    def _wait(self, order_id, seconds, poll=1.0):
        info = {}
        for _ in range(int(seconds / poll) + 1):
            info = self.order_info(order_id)
            st = info.get("state")
            if st == "filled" or st in self.BAD:
                return info
            self.sleep(poll)
        return info

    def _settle(self, order_id):
        """Cancel an order that didn't fill, then make sure we know how it ended
        (it can still fill in the instant before the cancel lands)."""
        try:
            self.api.cancel(order_id)
        except Exception as e:
            print(f"cancel call failed ({order_id}): {e}")
        return self._wait(order_id, 8)

    def _send_and_wait(self, side, ticker, exp, strike, cp, qty, price, wait_sec, on_order):
        label = f"{ticker} {strike:g}{cp} {exp:%m/%d}"
        try:
            order = self.api.place(side, ticker, exp, strike, cp, qty, price)
        except Exception as e:
            self._log(side.upper(), label, qty, price, "", "failed", "", f"call raised: {e}")
            return OrderResult("failed", note=f"order call raised: {e}")
        oid = (order or {}).get("id")
        if not oid:
            note = str((order or {}).get("detail") or order)[:200]
            self._log(side.upper(), label, qty, price, "", "failed", "", note)
            return OrderResult("failed", note=f"Robinhood refused the order: {note}")
        if on_order:
            on_order(oid, True)
        info = self._wait(oid, wait_sec)
        state = info.get("state")
        if state != "filled" and state not in self.BAD:
            info = self._settle(oid)
            state = info.get("state")
        got, avg = self.fill_of(info)
        self._log(side.upper(), label, qty, price, oid, state, avg or "", "")
        if state == "filled" or got > 0:
            if on_order:
                on_order(oid, False)
            return OrderResult("filled", got or qty, avg, oid)
        if state in self.BAD:
            if on_order:
                on_order(oid, False)
            return OrderResult("cancelled", 0, 0.0, oid, note=f"not filled in {wait_sec:g}s ({state})")
        return OrderResult("unknown", 0, 0.0, oid, note=f"order state after cancel: {state or 'no answer'}")

    # ---- the two actions --------------------------------------------------------
    def buy(self, ticker, exp, strike, cp, qty, limit, on_order=None):
        return self._send_and_wait("buy", ticker, exp, strike, cp, qty, limit, self.s.buy_timeout_sec, on_order)

    def sell_once(self, ticker, exp, strike, cp, qty, quote, attempt, on_order=None):
        """One sell attempt. Attempt 0 is at the bid; every later attempt goes one price step lower."""
        if quote is None:
            return OrderResult("no_quote", note="no quote available to price the sell")
        floor_price = self.tick_size(ticker, exp, strike, cp, 0.01)
        bid = quote.get("bid")
        if bid is None or bid <= 0:
            price = floor_price                      # nobody is bidding: offer it at the lowest price
        else:
            tick = self.tick_size(ticker, exp, strike, cp, bid)
            price = max(round_to_tick(bid, tick, "down") - attempt * tick, floor_price)
        return self._send_and_wait("sell", ticker, exp, strike, cp, qty, round(price, 2),
                                   self.s.sell_attempt_sec, on_order)

    def still_held(self, ticker, exp_iso, strike, cp):
        """How many of this contract Robinhood shows on the account right now (None = could not read)."""
        try:
            return sum(q for t, e, k, c, q in self.api.holdings() if (t, e, k, c) == (ticker, exp_iso, strike, cp))
        except Exception as e:
            print(f"could not read your Robinhood positions: {e}")
            return None

    # ---- start-up checks --------------------------------------------------------
    def reconcile(self, positions):
        """Compare what the bot believes it holds with what Robinhood says.
        Returns (messages, held) - held is None if Robinhood could not be read."""
        try:
            held = self.api.holdings()
        except Exception as e:
            return [f"could not read your Robinhood positions ({e})"], None
        msgs = []
        theirs = {(t, e, k, c): q for t, e, k, c, q in held}
        mine = {(p.ticker, p.exp, p.strike, p.cp): p for p in positions if p.real and p.qty_open > 0}
        for key, p in mine.items():
            have = theirs.get(key, 0)
            if have < p.qty_open:
                msgs.append(f"bot thought it held {p.qty_open}x {p.contract} but Robinhood shows {have} - "
                            f"position dropped from the bot's books")
        for key, q in theirs.items():
            if key not in mine:
                t, e, k, c = key
                msgs.append(f"Robinhood shows {q}x {t} {k:g}{c} {e} that the bot did not buy - the bot will NOT touch it")
        try:
            oo = self.api.open_orders()
            if oo:
                msgs.append(f"{len(oo)} option order(s) are open on your account right now")
        except Exception:
            pass
        return msgs, held


# ----------------------------------------------------------------------------
# 5) Telegram listener
# ----------------------------------------------------------------------------
def make_client():
    from telethon import TelegramClient
    api_id = os.environ.get("TG_API_ID")
    api_hash = os.environ.get("TG_API_HASH")
    if not api_id or not api_hash:
        sys.exit("Set TG_API_ID and TG_API_HASH in your .env file first.")
    return TelegramClient(os.path.join(DATA_DIR, "sniper_session"), int(api_id), api_hash)


async def list_chats():
    client = make_client()
    await client.start()
    print("\nYour Telegram chats (copy the number next to Sniper Trades into TG_CHAT_ID):\n")
    async for d in client.iter_dialogs():
        if d.is_group or d.is_channel:
            print(f"{d.id:>16}  {d.name}")
    await client.disconnect()


def protect_from_freezing():
    """Windows only. Two things freeze this program silently:
      1. the PC going to sleep -> ask Windows to keep the PC and the screen awake while we run
         (on laptops with Modern Standby the screen timing out is what puts the PC to sleep,
         so keeping only the system awake is not enough). Closing the lid still sleeps it;
      2. clicking/selecting text in an old-style console window, which pauses the
         program until a key is pressed -> switch that 'QuickEdit' behaviour off.
    Never raises; returns a short text saying what it did."""
    if os.name != "nt":
        return "freeze protection: skipped (not Windows)"
    done = []
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED, ES_DISPLAY_REQUIRED = 0x80000000, 0x00000001, 0x00000002
        if k32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED):
            done.append("PC and screen kept awake while the bot runs (closing the lid still sleeps it)")
        handle = k32.GetStdHandle(-10)                 # standard input
        mode = ctypes.c_uint32()
        if k32.GetConsoleMode(handle, ctypes.byref(mode)):
            ENABLE_QUICK_EDIT, ENABLE_EXTENDED = 0x0040, 0x0080
            if k32.SetConsoleMode(handle, (mode.value & ~ENABLE_QUICK_EDIT) | ENABLE_EXTENDED):
                done.append("click-to-pause turned off in this window")
    except Exception as e:
        done.append(f"could not fully apply ({e})")
    return "freeze protection: " + ("; ".join(done) or "nothing applied")




def log_stall(start: datetime, end: datetime):
    append_csv(STALLS_CSV(), ["froze_from", "resumed_at", "minutes"],
               [f"{start:%Y-%m-%d %H:%M:%S}", f"{end:%Y-%m-%d %H:%M:%S}",
                f"{(end - start).total_seconds() / 60:.1f}"])


def send_phone(s, text, pre=False):
    """Optional push alert through your own Telegram bot (Saved Messages never buzzes the phone)."""
    if not (s.tg_bot_token and s.tg_notify_chat):
        return

    def _post():
        try:
            import urllib.parse
            import urllib.request
            fields = {"chat_id": s.tg_notify_chat, "text": text[:3500]}
            if pre:                       # monospaced block, so a card's columns line up
                import html
                fields = {"chat_id": s.tg_notify_chat, "parse_mode": "HTML",
                          "text": "<pre>" + html.escape(text[:3500]) + "</pre>"}
            data = urllib.parse.urlencode(fields).encode()
            urllib.request.urlopen(f"https://api.telegram.org/bot{s.tg_bot_token}/sendMessage", data, timeout=10).read()
        except Exception as e:
            print(f"phone alert failed: {e}")
    threading.Thread(target=_post, daemon=True).start()


async def run():
    from telethon import events
    s = Settings()
    print(protect_from_freezing())
    if s.live_trading:
        print("\n" + "=" * 70 + "\n LIVE TRADING IS ON: this run places REAL orders with your Robinhood money."
              f"\n 1 contract per alert"
              + (f" (2 with a runner when both cost up to ${s.max_single_contract_cost:,.0f}; lottos always 1)"
                 if s.keep_runner else "")
              + f"; lottos only under {s.lotto_max_price:g}; emergency stop at "
              f"-{s.disaster_stop_pct:g}%.\n Kill switch: create a file named STOP_TRADING in this folder.\n" + "=" * 70)
        if input("Type LIVE and press Enter to continue (anything else exits): ").strip() != "LIVE":
            sys.exit("Not confirmed - exiting.")
    chat_id = os.environ.get("TG_CHAT_ID")
    if not chat_id:
        sys.exit("Set TG_CHAT_ID in .env (run with --list-chats to find it).")
    chat_id = int(chat_id)

    client = make_client()
    await client.start()
    print("Telegram: logged in.")

    rh = Robinhood(s)
    rh.login()

    loop = asyncio.get_running_loop()
    rh_lock = asyncio.Lock()
    tag = "LIVE" if s.live_trading else "shadow"

    def notify(text):
        print(f"[{datetime.now(ET):%H:%M:%S}] {text}")
        if "\n" in text and text.startswith(("🟢", "🔴")):
            # a buy/sell card: put the mode tag on its first line and send it in a monospaced
            # block so the columns line up
            head, body = text.split("\n", 1)
            card = f"[{tag}] {head}\n{body}"
            if s.notify_saved_messages:
                asyncio.run_coroutine_threadsafe(client.send_message("me", f"```\n{card}\n```"), loop)
            send_phone(s, card, pre=True)
            return
        if s.notify_saved_messages:
            asyncio.run_coroutine_threadsafe(client.send_message("me", f"[{tag}] {text}"), loop)
        if not text.startswith(("SKIP", "HOLD")):
            send_phone(s, f"[{tag}] {text}")

    tracker = Tracker(s, rh.quote_many, rh.underlying_many, now=lambda: datetime.now(ET),
                      path_csv=PATHS_CSV, save=lambda: None, notify=notify)
    broker = Broker(rh, s, notify) if s.live_trading else None
    engine = Engine(s, rh.quote, notify, watchlist=rh.add_to_watchlist, tracker=tracker, broker=broker)
    if broker:
        for msg in await asyncio.to_thread(engine.startup_live):
            notify(f"!! Start-up check: {msg}")
        bp = broker.buying_power()
        notify(f"LIVE mode started. {engine.capital_line()}. Buying power: {'unknown' if bp is None else f'${bp:,.0f}'}.")
    status = {"last_alert": None, "down_since": None, "warned": False}
    errors = {"n": 0, "told_at": {}}

    def report_error(where, e):
        """An unexpected error must be seen but must not stop the bot: the timer loop runs the stops,
        exits and watches. Printed every time; messaged at most once every 5 minutes per place."""
        errors["n"] += 1
        now = datetime.now(ET)
        print(f"[{now:%H:%M:%S}] !! unexpected error in {where}: {type(e).__name__}: {e}")
        traceback.print_exception(type(e), e, e.__traceback__)
        last = errors["told_at"].get(where)
        if last is None or (now - last).total_seconds() >= 300:
            errors["told_at"][where] = now
            notify(f"!! Unexpected error in {where}: {type(e).__name__}: {e}. The bot keeps running "
                   f"({errors['n']} error(s) so far). Check Robinhood, and restart the bot if this repeats.")

    @client.on(events.NewMessage(chats=chat_id))
    async def on_new(event):
        text = event.raw_text or ""
        alert_time = event.message.date.astimezone(ET)
        status["last_alert"] = datetime.now(ET)
        print(f"\n[{datetime.now(ET):%H:%M:%S}] ALERT ({alert_time:%H:%M:%S}): {text}")
        async with rh_lock:
            try:
                await asyncio.to_thread(engine.handle_message, text, alert_time, event.message.id, "live")
            except Exception as e:
                report_error(f"the alert '{text[:80]}' (handle this one by hand)", e)

    @client.on(events.MessageEdited(chats=chat_id))
    async def on_edit(event):
        edited_at = (event.message.edit_date or event.message.date).astimezone(ET)
        try:
            await asyncio.to_thread(engine.on_edit, event.raw_text or "", edited_at)
        except Exception as e:
            report_error("an edited message", e)

    # ---- catch up on anything posted while the bot was off ----------------
    async def catch_up():
        try:
            try:
                entity = await client.get_entity(chat_id)
            except Exception:
                await client.get_dialogs()
                entity = await client.get_entity(chat_id)
            kw = {"reverse": True, "limit": 1000}
            if engine.last_msg_id:
                kw["min_id"] = engine.last_msg_id
                what = "since the bot last ran"
            else:
                kw["offset_date"] = datetime.now(ET) - timedelta(days=s.backfill_days)
                what = f"from the last {s.backfill_days:g} days (first run)"
            msgs = [m async for m in client.iter_messages(entity, **kw) if m.raw_text]
        except Exception as e:
            notify(f"!! Couldn't read missed messages ({e}). Live alerts still work.")
            return
        if not msgs:
            print("Catch-up: nothing missed.")
            return
        print(f"Catch-up: reading {len(msgs)} messages {what}...")
        async with rh_lock:
            engine.quiet = True
            try:
                for m in msgs:
                    try:
                        await asyncio.to_thread(engine.handle_message, m.raw_text,
                                                m.date.astimezone(ET), m.id, "catch-up")
                    except Exception as e:
                        report_error(f"the missed message '{m.raw_text[:80]}'", e)
            finally:
                engine.quiet = False
        live = tracker.active()
        notify(f"Caught up on {len(msgs)} messages posted while off. Now tracking {len(live)} of his "
               f"open contracts: " + (", ".join(t.contract for t in live) or "none"))

    async def timer():
        summary_sent_for = None
        last_beat = datetime.now(ET)
        prev_start = datetime.now(ET)
        while True:
            # Each lap normally takes ~5-10 s. A much longer lap means the whole program
            # was frozen (PC asleep, window paused, Robinhood call stuck): record it.
            lap_start = datetime.now(ET)
            if (lap_start - prev_start).total_seconds() > s.stall_warn_sec:
                mins = (lap_start - prev_start).total_seconds() / 60
                log_stall(prev_start, lap_start)
                notify(f"!! The bot was frozen for {mins:.0f} min ({prev_start:%H:%M:%S} to {lap_start:%H:%M:%S}). "
                       f"Price samples in that window are missing and alerts posted then arrived late. "
                       f"Likely cause: PC sleep or a paused window.")
            prev_start = lap_start
            await asyncio.sleep(5)
            now = datetime.now(ET)
            # Each part is guarded on its own: an error in one must not end this loop or skip the others.
            try:
                async with rh_lock:
                    await asyncio.to_thread(engine.tick)
            except Exception as e:
                report_error("the timer (stops, exits and watches)", e)
            try:
                async with rh_lock:
                    if tracker.due():
                        await asyncio.to_thread(tracker.sample)
                    else:
                        tracker.sweep()
            except Exception as e:
                report_error("the price tracker", e)
            try:
                # connection watch
                if not client.is_connected():
                    status["down_since"] = status["down_since"] or now
                    if (now - status["down_since"]).total_seconds() > 60 and not status["warned"]:
                        status["warned"] = True
                        print(f"[{now:%H:%M:%S}] !! Telegram disconnected for over a minute - alerts may be late")
                elif status["down_since"]:
                    if status["warned"]:
                        notify(f"Telegram reconnected after {(now - status['down_since']).total_seconds() / 60:.0f} min "
                               f"- alerts in that gap will arrive late and buys will be skipped as old.")
                    status.update(down_since=None, warned=False)
                # heartbeat line so you can see it's alive
                if (now - last_beat).total_seconds() >= s.heartbeat_min * 60:
                    last_beat = now
                    la = status["last_alert"]
                    mode = "LIVE" if broker else "shadow"
                    held = len([p for p in engine.positions if p.qty_open > 0])
                    cap = f" | capital ${engine.capital_left():,.0f}" if broker else ""
                    print(f"[{now:%H:%M:%S}] alive | {mode} | holding {held}{cap} | Telegram "
                          f"{'connected' if client.is_connected() else 'DISCONNECTED'}"
                          f" | tracking {len(tracker.active())} contracts"
                          f"{f' | watching {len(engine.watching)}' if engine.watching else ''}"
                          f" | last alert {f'{(now - la).total_seconds() / 60:.0f} min ago' if la else 'none yet'}")
                if now.weekday() < 5 and now.time() >= dtime(16, 5) and summary_sent_for != now.date():
                    summary_sent_for = now.date()
                    notify(engine.summary())
            except Exception as e:
                report_error("the status report", e)

    mode_txt = "LIVE MODE running - real orders" if s.live_trading else "SHADOW MODE running - no real orders"
    print(f"\n{mode_txt}. Listening to chat {chat_id}. Ctrl+C to stop.\n")
    await catch_up()
    asyncio.create_task(timer())
    try:
        await client.run_until_disconnected()
    finally:
        print("\n" + engine.summary())


# ----------------------------------------------------------------------------
# 6) Self-test: today's real alerts through the engine with made-up quotes
# ----------------------------------------------------------------------------
def selftest():
    import tempfile
    tmp = tempfile.mkdtemp()
    clock = {"t": datetime(2026, 10, 1, 9, 32, 5, tzinfo=ET)}
    book = {}   # (ticker, strike) -> (bid, ask)

    def quotes(ticker, exp, strike, cp):
        b = book.get((ticker, strike))
        return {"bid": b[0], "ask": b[1], "mark": sum(b) / 2} if b else None

    s = Settings()

    s.keep_runner = False          # 1 contract per alert here (runner: selftest_runner)
    eng = Engine(s, quotes, notify=lambda t: print("   ->", t), now=lambda: clock["t"],
                 log_path=os.path.join(tmp, "log.csv"), state_path=os.path.join(tmp, "s.json"))

    def feed(hhmm, text, bid_ask=None, key=None):
        h, m = map(int, hhmm.split(":"))
        clock["t"] = datetime(2026, 10, 1, h, m, 5, tzinfo=ET)
        if bid_ask:
            book[key] = bid_ask
        print(f"\n{hhmm}  {text!r}")
        return eng.handle_message(text, clock["t"] - timedelta(seconds=3))

    feed("08:28", "ALL OUT STX shares 925")
    feed("09:32", "BOUGHT MU 1100C 10/2 6.55", (6.50, 6.75), ("MU", 1100.0))
    feed("09:45", "ALL OUT ASML 1900C 10/2 1.85‼️", (1.80, 1.90), ("ASML", 1900.0))
    feed("10:49", "BOUGHT META 750C 10/2 1.15‼️", (1.30, 1.40), ("META", 750.0))
    feed("13:27", "BOUGHT NBIS 240C 10/2 2.57 - lotto @everyone", (2.55, 2.65), ("NBIS", 240.0))
    feed("13:29", "SOLD 1/2 NVIS 240C 10/2 3.6 @everyone\n3.65 executed", (3.55, 3.70), ("NBIS", 240.0))
    feed("13:31", "SOLD 1/4 NBIS 240C 10/2 4.25 - runners left", (4.20, 4.35), ("NBIS", 240.0))
    feed("13:45", "SOLD 1/2 MU 1100C 10/2 9.9 - hold 5 more.", (9.80, 10.0), ("MU", 1100.0))
    feed("13:48", "ALL OUT MU 1100C 10/2 12.4", (12.30, 12.50), ("MU", 1100.0))
    feed("13:48", "BOUGHT MU 1130C 10/2 4.28 - small sall lotto for tomorrow.", (4.25, 4.40), ("MU", 1130.0))
    feed("14:01", "BOUGHT shares VST 138.59")
    feed("14:05", "SOLD cash secured puts ASML 1800P 10/2 15.5")
    print("\n" + eng.summary())

    print("\n\n========== Typo and non-long-option checks (made-up alerts) ==========")
    book[("ASML", 1800.0)] = (5.10, 5.30)
    feed("14:20", "BOUGHT ASML 1800P 10/2 5.2", key=None)            # buy-back of his CSP
    feed("14:21", "SOLD covered calls STX 1000C 10/9 4.5")
    feed("14:22", "SOLD STX 1050C 10/9 3.2")                          # short with no label
    book[("STX", 1050.0)] = (1.60, 1.70)
    feed("14:23", "BOUGHT STX 1050C 10/9 1.65")                       # ...its buy-back
    feed("14:24", "ALL OUT STX shares 925")
    feed("14:25", "Rolled AMD 200C 10/2 to 10/9 210C")
    feed("14:26", "SOLD TO CLOSE MU 1130C 10/2 4.9", (4.85, 4.95), ("MU", 1130.0))  # normal exit, other wording
    book[("AMD", 200.0)] = (1.80, 1.90)
    feed("14:28", "BOUGHT AMD 200C 10/9 18.5")                        # price typo (meant 1.85)
    feed("14:29", "1.85*")                                            # his follow-up fix
    feed("14:29", "BOUGHT AMD 200C 10/9 1.85*")                       # full re-post with * : also log only
    feed("14:29", "typo - meant 200C not 220C")
    book[("TSLA", 300.0)] = (2.40, 2.50)
    feed("14:31", "BOUGHT TSLA 300C 10/9 2.45")
    feed("14:35", "SOLD 1/2 TSLA 310C 10/9 3.1")                      # strike typo on a sell
    feed("14:36", "BOUHGT AMZN 220C 10/9 1.8")                        # misspelt action
    feed("14:37", "BOUGHT TSAL 300C 10/9 2.45")                       # ticker typo on a buy
    feed("14:40", "ALL-OUT TSLA 300C 10/9 3.3", (3.25, 3.35), ("TSLA", 300.0))
    print("\n" + eng.summary())
    selftest_tracker()
    selftest_exit_typos()
    selftest_live()
    selftest_watch()
    selftest_entry_rules()
    selftest_failures()
    selftest_runner()


class _FakeAPI:
    """Stands in for Robinhood's order system. Each placed order follows the next scripted behaviour:
    fill | nofill (stays open until cancelled) | fill_on_cancel (fills just as we cancel) |
    fill_one (1 contract fills, the rest is cancelled) | stuck (cancel never lands) |
    refuse (Robinhood rejects the request)."""

    def __init__(self):
        self.orders, self.script, self.placed, self.sizes, self.n = {}, [], [], [], 0
        self.held, self.bp, self.open = [], 5000.0, []
        self.ticks = {"above_tick": "0.10", "below_tick": "0.05", "cutoff_price": "3.00"}

    def option_id(self, *a):
        return "oid"

    def min_ticks(self, oid):
        return self.ticks

    def place(self, side, ticker, exp, strike, cp, qty, price):
        beh = self.script.pop(0) if self.script else "fill"
        if beh == "refuse":
            return {"detail": "Not enough buying power."}
        self.n += 1
        oid = f"o{self.n}"
        self.orders[oid] = {"id": oid, "state": "queued", "beh": beh, "price": price, "qty": qty,
                            "processed_quantity": "0", "legs": [{"executions": []}]}
        self.placed.append((side, price))
        self.sizes.append((side, qty))
        if beh == "fill":
            self._fill(oid)
        elif beh == "fill_one":                  # only 1 contract fills; the rest stays open until cancelled
            self._fill(oid, 1)
            self.orders[oid]["state"] = "partially_filled"
        return {"id": oid, "state": "unconfirmed"}

    def _fill(self, oid, n=None):
        o = self.orders[oid]
        n = o["qty"] if n is None else n
        o.update(state="filled", processed_quantity=str(n))
        o["legs"][0]["executions"] = [{"price": str(o["price"]), "quantity": str(n)}]

    def order_info(self, oid):
        return dict(self.orders[oid])

    def cancel(self, oid):
        o = self.orders[oid]
        if o["beh"] == "fill_on_cancel":
            self._fill(oid)
        elif o["beh"] != "stuck":
            o["state"] = "cancelled"

    def buying_power(self):
        return self.bp

    def holdings(self):
        return self.held

    def open_orders(self):
        return self.open


def selftest_watch():
    """Late entry (alerts up to 10 min old) and watch-and-buy (ask above the cap at the alert, option
    expiring more than 3 days out: keep watching up to 60 min, buy when the ask is back within the cap,
    stop if he sells)."""
    import tempfile
    print("\n\n========== late entry + watch-and-buy ==========")
    tmp = tempfile.mkdtemp()
    clock = {"t": datetime(2026, 10, 6, 10, 0, 0, tzinfo=ET)}
    book, notes = {}, []
    s = Settings()
    s.keep_runner = False          # 1 contract per alert here (runner: selftest_runner)
    s.max_deployed, s.max_open_positions, s.max_buys_per_day = 5000, 10, 20
    s.max_single_contract_cost, s.project_capital = 1000, 5000

    def quotes(ticker, exp, strike, cp):
        b = book.get(ticker)
        return {"bid": b[0], "ask": b[1], "mark": round((b[0] + b[1]) / 2, 3)} if b else None

    api = _FakeAPI()
    stopf = os.path.join(tmp, "STOP_TRADING")

    def build():
        brk = Broker(api, s, lambda x: None, orders_csv=os.path.join(tmp, "orders.csv"), sleep=lambda x: None,
                     now=lambda: clock["t"])
        return Engine(s, quotes, lambda x: notes.append(x), now=lambda: clock["t"],
                      log_path=os.path.join(tmp, "log.csv"), state_path=os.path.join(tmp, "s.json"),
                      broker=brk, stop_file=stopf)

    eng = build()

    def feed(text, age=2, advance=3):
        clock["t"] += timedelta(seconds=advance)
        notes.clear()
        return eng.handle_message(text, clock["t"] - timedelta(seconds=age))

    def tick(sec=11):
        clock["t"] += timedelta(seconds=sec)
        notes.clear()
        eng.tick()

    def check(label, cond, detail=""):
        print(f"  {'PASS' if cond else 'FAIL'}  {label}  {detail}")
        assert cond, label

    def last_log():
        with open(eng.log_path, encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))[-1]

    def has(ticker):
        return any(p.ticker == ticker and p.qty_open > 0 for p in eng.positions)

    # --- late entry, short-dated: up to 2 minutes after his alert (more in selftest_entry_rules) ---
    book["LAA"] = (2.0, 2.05)
    r = feed("BOUGHT LAA 100C 10/9 2.0", age=90)
    check("short-dated alert 90 s old is still bought", r[0][0] == "BOUGHT", str(r[0]))
    book["LAB"] = (2.0, 2.05)
    r = feed("BOUGHT LAB 100C 10/9 2.0", age=3 * 60)
    check("short-dated alert 3 min old is skipped", r[0][0] == "SKIP" and "old" in r[0][2], str(r[0]))
    check("...and not watched (too old to start with)", not eng.watching)

    # --- watch-and-buy -------------------------------------------------------------
    n_orders = len(api.placed)
    book["WWA"] = (2.90, 3.00)                       # his price 2.5 -> cap 2.75, ask 3.00 is +20%
    r = feed("BOUGHT WWA 100C 10/16 2.5")
    check("ask above cap, expiry 10 days out -> WATCH", r[0][0] == "WATCH", str(r[0]))
    check("watch logged as WATCH", last_log()["decision"] == "WATCH", last_log()["reason"])
    check("watch is saved to the state file", len(eng.watching) == 1)
    check("no order sent while watching", len(api.placed) == n_orders)
    tick()
    check("still above cap -> nothing happens, no noise", not has("WWA") and not notes, str(notes))
    r = feed("BOUGHT WWA 100C 10/16 2.5")
    check("same alert again is not a second watch", r[0][0] == "SKIP" and "already watching" in r[0][2], str(r[0]))
    book["WWA"] = (2.60, 2.65)
    tick()
    check("ask back within cap -> BOUGHT", has("WWA") and not eng.watching, str(notes))
    msg = notes[0] if notes else ""
    check("buy card shows what we paid and what he paid", "We paid" in msg and "2.70" in msg and "He paid" in msg
          and "2.50" in msg, msg)
    check("buy card says it came from watching", "after watching" in msg, msg)
    check("buy card starts with the title line", msg.startswith("🟢 BOUGHT  WWA 100C 10/16"), msg)
    check("limit never above the cap (2.75)", api.placed[-1][1] <= 2.75 + 1e-9, str(api.placed[-1]))
    check("bought row logged", last_log()["decision"] == "BOUGHT", last_log()["reason"])
    tick()
    check("bought once only", sum(1 for p in eng.positions if p.ticker == "WWA") == 1)

    # --- sell messages: ours next to his ---------------------------------------------------
    book["WWA"] = (3.40, 3.50)
    clock["t"] += timedelta(seconds=1)
    notes.clear()
    eng.handle_message("ALL OUT WWA 100C 10/16 3.5", clock["t"] - timedelta(seconds=2))
    msg = " ".join(notes)
    check("sell card has an Us row and a Him row", "Us " in msg and "3.40" in msg and "Him" in msg and "3.50" in msg, msg)
    check("...with both profits", "+$70" in msg and "+$100" in msg, msg)
    check("...and how far from his price", "below his price" in msg, msg)
    book["LAA"] = (3.40, 3.50)                       # +62% over our 2.1 -> take profit (our own exit)
    tick()
    msg = " ".join(notes)
    check("our own exit says he has not sold", "has not sold" in msg and "3.40" in msg and "Our own exit" in msg, msg)
    book["LAA"] = (2.0, 2.05)

    # --- he sells before the price comes back: watch ends -----------------------------
    book["BRX"] = (2.90, 3.00)
    feed("BOUGHT BRX 100C 10/16 2.5")
    check("second watch started", len(eng.watching) == 1)
    r = feed("SOLD 1/2 BRX 100C 10/16 3.1")
    check("his sell ends the watch", not eng.watching and any("Stopped watching" in n for n in notes), str(notes))
    book["BRX"] = (2.55, 2.60)
    tick()
    check("price back in range afterwards: NOT bought", not has("BRX"))

    # --- time out after 60 minutes ---------------------------------------------------
    book["CLM"] = (2.90, 3.00)
    feed("BOUGHT CLM 100C 10/16 2.5")
    tick(59 * 60)
    check("still watching at 59 min", len(eng.watching) == 1 and not has("CLM"))
    tick(2 * 60)
    check("60 min passed -> watch ended, not bought", not eng.watching and not has("CLM"))
    check("ending is logged", last_log()["decision"] == "WATCH ENDED", last_log()["reason"])
    book["CLM"] = (2.55, 2.60)
    tick()
    check("a late price drop after the watch ended does nothing", not has("CLM"))

    # --- expiry 3 days or less: no watching ----------------------------------------------
    book["SHA"] = (2.90, 3.00)
    r = feed("BOUGHT SHA 100C 10/8 2.5")             # 2 days out
    check("expiry 2 days out: plain SKIP, no watch", r[0][0] == "SKIP" and not eng.watching, str(r[0]))
    book["SHB"] = (2.90, 3.00)
    r = feed("BOUGHT SHB 100C 10/9 2.5")             # 3 days out: not MORE than 3
    check("expiry exactly 3 days out: no watch", r[0][0] == "SKIP" and not eng.watching, str(r[0]))
    book["SHC"] = (2.90, 3.00)
    r = feed("BOUGHT SHC 100C 10/10 2.5")            # 4 days out
    check("expiry 4 days out: watch", r[0][0] == "WATCH", str(r[0]))
    eng._end_watch(contract_key("SHC", 100.0, "C", date(2026, 10, 10)), "test cleanup")

    # --- the other rules still apply at the moment of buying ---------------------------
    book["DNP"] = (2.90, 3.00)
    feed("BOUGHT DNP 100C 10/16 2.5")
    open(stopf, "w").close()
    book["DNP"] = (2.55, 2.60)
    tick()
    check("STOP_TRADING file blocks the watched buy and ends the watch",
          not has("DNP") and not eng.watching and last_log()["decision"] == "SKIP", last_log()["reason"])
    os.remove(stopf)

    book["EQS"] = (2.90, 3.00)
    feed("BOUGHT EQS 100C 10/16 2.5")
    book["EQS"] = (2.0, 2.9)                         # price in range but spread far too wide
    tick()
    check("spread too wide: keeps watching quietly", len(eng.watching) == 1 and not has("EQS") and not notes, str(notes))
    book["EQS"] = (2.60, 2.65)
    tick()
    check("then spread normal and ask within cap: bought", has("EQS") and not eng.watching)

    # --- a watch survives a restart ------------------------------------------------------
    book["FTV"] = (2.90, 3.00)
    feed("BOUGHT FTV 100C 10/16 2.5")
    eng2 = build()
    check("watch is still there after a restart", len(eng2.watching) == 1, str(eng2.watching))
    eng = eng2
    book["FTV"] = (2.60, 2.65)
    tick()
    check("...and buys after the restart", has("FTV") and not eng.watching)

    # --- market closes: watch ends -------------------------------------------------------
    book["GHW"] = (2.90, 3.00)
    feed("BOUGHT GHW 100C 10/16 2.5")
    clock["t"] = datetime(2026, 10, 6, 16, 1, tzinfo=ET)
    notes.clear()
    eng.tick()
    check("market close ends the watch", not eng.watching and not has("GHW"))

    # --- WATCH_MIN=0 turns it off --------------------------------------------------------
    s.watch_min = 0
    clock["t"] = datetime(2026, 10, 7, 10, 0, tzinfo=ET)
    book["HJK"] = (2.90, 3.00)
    r = feed("BOUGHT HJK 100C 10/16 2.5")
    check("WATCH_MIN=0: plain SKIP", r[0][0] == "SKIP" and not eng.watching, str(r[0]))
    s.watch_min = 60

    # --- the summary labels contracts that already expired -------------------------------
    tr = Tracker(s, lambda items: {}, lambda tk: {}, now=lambda: clock["t"], path_csv=os.path.join(tmp, "p.csv"),
                 save=lambda: None, notify=lambda x: None)
    eng3 = Engine(s, quotes, lambda x: None, now=lambda: clock["t"], log_path=os.path.join(tmp, "l3.csv"),
                  state_path=os.path.join(tmp, "s3.json"), tracker=tr)
    eng3.quiet = True
    clock["t"] = datetime(2026, 10, 5, 10, 0, tzinfo=ET)          # the day MU still had time left
    eng3.handle_message("BOUGHT MU 1075C 10/5 2.0", datetime(2026, 10, 5, 9, 40, tzinfo=ET), None, "catch-up")
    clock["t"] = datetime(2026, 10, 7, 10, 0, tzinfo=ET)          # two days later it is long expired
    eng3.handle_message("BOUGHT ORCL 155C 10/16 0.98", datetime(2026, 10, 7, 9, 40, tzinfo=ET), None, "catch-up")
    for t in tr.items.values():
        t.samples, t.hi_mark, t.lo_mark = 5, t.his_entry * 1.2, t.his_entry * 0.5
    lines = tr.summary_lines()
    check("expired contract is labelled EXPIRED", any("MU 1075C" in l and "EXPIRED" in l for l in lines), str(lines))
    check("a live contract still says he is still in", any("ORCL" in l and "still in" in l for l in lines), str(lines))
    print("\n  all late-entry / watch checks passed")


def selftest_entry_rules():
    """Late entry by expiry (0DTE: LATE_ENTRY_MIN, short-dated: 2 min, swing: 60 min while he hasn't sold any),
    re-checking a wide spread for 60 s instead of skipping, and one position per stock."""
    import tempfile
    print("\n\n========== entry rules: late entry by expiry, spread re-check, one per stock ==========")
    tmp = tempfile.mkdtemp()
    clock = {"t": datetime(2026, 10, 6, 10, 0, 0, tzinfo=ET)}
    book, notes = {}, []
    s = Settings()
    s.keep_runner = False          # 1 contract per alert here (runner: selftest_runner)
    s.max_deployed, s.max_open_positions, s.max_buys_per_day = 5000, 20, 50
    s.max_single_contract_cost, s.project_capital = 1000, 5000

    def quotes(ticker, exp, strike, cp):
        b = book.get(ticker)
        return {"bid": b[0], "ask": b[1], "mark": round((b[0] + b[1]) / 2, 3)} if b else None

    api = _FakeAPI()
    brk = Broker(api, s, lambda x: None, orders_csv=os.path.join(tmp, "orders.csv"), sleep=lambda x: None,
                 now=lambda: clock["t"])
    tr = Tracker(s, lambda items: {}, lambda tickers: {}, now=lambda: clock["t"],
                 path_csv=os.path.join(tmp, "paths.csv"), save=lambda: None, notify=lambda x: None)
    eng = Engine(s, quotes, lambda x: notes.append(x), now=lambda: clock["t"],
                 log_path=os.path.join(tmp, "log.csv"), state_path=os.path.join(tmp, "s.json"),
                 tracker=tr, broker=brk, stop_file=os.path.join(tmp, "STOP_TRADING"))

    def feed(text, age=2, source="live"):
        clock["t"] += timedelta(seconds=3)
        notes.clear()
        return eng.handle_message(text, clock["t"] - timedelta(seconds=age), None, source)

    def tick(sec=11):
        clock["t"] += timedelta(seconds=sec)
        notes.clear()
        eng.tick()

    def check(label, cond, detail=""):
        print(f"  {'PASS' if cond else 'FAIL'}  {label}  {detail}")
        assert cond, label

    def has(ticker):
        return any(p.ticker == ticker and p.qty_open > 0 for p in eng.positions)

    # --- late entry by expiry -------------------------------------------------------------
    book["ZDA"] = (2.0, 2.05)
    r = feed("BOUGHT ZDA 100C 10/6 2.0", age=9 * 60)
    check("0DTE alert 9 min old is still bought (LATE_ENTRY_MIN)", r[0][0] == "BOUGHT", str(r[0]))
    book["ZDB"] = (2.0, 2.05)
    r = feed("BOUGHT ZDB 100C 10/6 2.0", age=11 * 60)
    check("0DTE alert 11 min old is skipped", r[0][0] == "SKIP" and "old" in r[0][2], str(r[0]))
    book["SDA"] = (2.0, 2.05)
    r = feed("BOUGHT SDA 100C 10/13 2.0", age=110)           # 7 days out: still short-dated
    check("short-dated (7 days) alert 110 s old is bought", r[0][0] == "BOUGHT", str(r[0]))
    book["SDB"] = (2.0, 2.05)
    r = feed("BOUGHT SDB 100C 10/13 2.0", age=130)
    check("short-dated alert 130 s old is skipped", r[0][0] == "SKIP" and "old" in r[0][2], str(r[0]))

    n = len(api.placed)
    book["SWA"] = (2.0, 2.05)
    r = feed("BOUGHT SWA 100C 10/14 2.0", age=30 * 60)       # 8 days out: swing
    check("swing alert 30 min old: re-checked first, not bought straight away",
          r[0][0] == "WATCH" and not has("SWA") and len(api.placed) == n, str(r[0]))
    tick(5)
    check("...not before 15 s", not has("SWA"))
    tick(11)
    check("...then bought while the ask is within the cap", has("SWA") and not eng.watching, str(notes))
    book["SWB"] = (2.0, 2.05)
    r = feed("BOUGHT SWB 100C 10/14 2.0", age=61 * 60)
    check("swing alert 61 min old is skipped", r[0][0] == "SKIP" and "old" in r[0][2], str(r[0]))

    book["SWC"] = (2.0, 2.05)
    feed("BOUGHT SWC 100C 10/14 2.0", age=20 * 60)
    feed("SOLD 1/2 SWC 100C 10/14 2.6", age=5 * 60)          # his sell, queued right behind it
    check("his sell arriving right behind a late swing ends the re-check", not eng.watching and not has("SWC"))
    tick(30)
    check("...and nothing is bought later", not has("SWC"))

    book["SWD"] = (2.0, 2.05)
    feed("BOUGHT SWD 100C 10/14 2.0", age=3 * 60 * 60, source="catch-up")   # seen late: tracked, not bought
    feed("SOLD 1/2 SWD 100C 10/14 2.6")
    r = feed("BOUGHT SWD 100C 10/14 2.0", age=10 * 60)
    check("late swing he has already sold some of is skipped", r[0][0] == "SKIP" and "already sold" in r[0][2],
          str(r[0]))
    r = feed("BOUGHT SWD 100C 10/14 2.0")
    check("...but the same alert on time is bought as usual", r[0][0] == "BOUGHT", str(r[0]))

    # --- a wide spread is re-checked for 60 s ------------------------------------------------
    n = len(api.placed)
    book["SPA"] = (1.50, 2.05)                                 # his 2.0: spread 31%
    r = feed("BOUGHT SPA 100C 10/9 2.0")
    check("wide spread: re-checking instead of skipping",
          r[0][0] == "WATCH" and "RE-CHECKING" in r[0][2] and len(api.placed) == n, str(r[0]))
    tick()
    check("still wide: keeps re-checking quietly", not has("SPA") and len(eng.watching) == 1 and not notes, str(notes))
    book["SPA"] = (1.95, 2.05)
    tick()
    check("spread narrows -> bought", has("SPA") and not eng.watching, str(notes))

    book["SPB"] = (1.50, 2.05)
    feed("BOUGHT SPB 100C 10/9 2.0")
    for _ in range(5):
        tick()
    check("still re-checking at 55 s", len(eng.watching) == 1 and not has("SPB"))
    tick(10)
    check("60 s of wide spread -> skipped, not bought", not eng.watching and not has("SPB"))
    check("...and it says why", "never qualified" in " ".join(notes) and "spread" in " ".join(notes), str(notes))

    book["SPC"] = (3.70, 5.10)                                 # his 3.90: spread 32% (the COHR case)
    feed("BOUGHT SPC 100C 10/14 3.9")
    book["SPC"] = (3.90, 4.30)                                 # spread fine now, but ask above the 4.29 cap
    tick()
    rec = eng.watching.get(contract_key("SPC", 100.0, "C", date(2026, 10, 14)), {})
    check("narrowed but above the cap -> becomes a normal price watch", rec.get("kind") == "price"
          and not has("SPC"), str(rec))
    book["SPC"] = (3.80, 3.90)
    tick()
    check("...and buys when the ask is back within the cap", has("SPC") and not eng.watching, str(notes))

    s.spread_recheck_sec = 0
    book["SPD"] = (1.50, 2.05)
    r = feed("BOUGHT SPD 100C 10/9 2.0")
    check("SPREAD_RECHECK_SEC=0: plain SKIP", r[0][0] == "SKIP" and "spread" in r[0][2] and not eng.watching,
          str(r[0]))
    s.spread_recheck_sec = 60

    # --- one position per stock -----------------------------------------------------------
    book["ONE"] = (2.0, 2.05)
    r = feed("BOUGHT ONE 100C 10/9 2.0")
    check("first ONE contract bought", r[0][0] == "BOUGHT", str(r[0]))
    r = feed("BOUGHT ONE 110C 10/9 2.0")
    check("second ONE contract (other strike) skipped", r[0][0] == "SKIP" and "one position per stock" in r[0][2],
          str(r[0]))
    feed("ALL OUT ONE 100C 10/9 2.4")
    r = feed("BOUGHT ONE 110C 10/9 2.0")
    check("after the first is sold, another ONE contract can be bought", r[0][0] == "BOUGHT", str(r[0]))
    s.one_position_per_stock = False
    book["TWO"] = (2.0, 2.05)
    feed("BOUGHT TWO 100C 10/9 2.0")
    r = feed("BOUGHT TWO 110C 10/9 2.0")
    check("ONE_POSITION_PER_STOCK=false: both bought", r[0][0] == "BOUGHT", str(r[0]))
    s.one_position_per_stock = True
    print("\n  all entry-rule checks passed")


def selftest_failures():
    """Things going wrong around the bot: a log open in Excel, a damaged state file, a position sold by hand,
    a real sale while catching up. None of them may stop the bot or go unreported."""
    import stat
    import tempfile
    print("\n\n========== when things go wrong: locked log, damaged state file, sold by hand ==========")
    clock = {"t": datetime(2026, 10, 6, 10, 0, 0, tzinfo=ET)}
    book, notes = {}, []
    s = Settings()
    s.keep_runner = False          # 1 contract per alert here (runner: selftest_runner)
    s.max_deployed, s.max_open_positions, s.max_buys_per_day = 5000, 20, 50
    s.max_single_contract_cost, s.project_capital = 1000, 5000

    def quotes(ticker, exp, strike, cp):
        b = book.get(ticker)
        return {"bid": b[0], "ask": b[1], "mark": round((b[0] + b[1]) / 2, 3)} if b else None

    def build(tmp, api):
        brk = Broker(api, s, lambda x: None, orders_csv=os.path.join(tmp, "orders.csv"), sleep=lambda x: None,
                     now=lambda: clock["t"])
        return Engine(s, quotes, lambda x: notes.append(x), now=lambda: clock["t"],
                      log_path=os.path.join(tmp, "log.csv"), state_path=os.path.join(tmp, "s.json"),
                      broker=brk, stop_file=os.path.join(tmp, "STOP_TRADING"))

    def feed(eng, text, age=2, source="live"):
        clock["t"] += timedelta(seconds=3)
        notes.clear()
        return eng.handle_message(text, clock["t"] - timedelta(seconds=age), None, source)

    def check(label, cond, detail=""):
        print(f"  {'PASS' if cond else 'FAIL'}  {label}  {detail}")
        assert cond, label

    def has(eng, ticker):
        return any(p.ticker == ticker and p.qty_open > 0 for p in eng.positions)

    # --- the decision log can't be written (read-only: what a file open in Excel looks like to the bot) ---
    tmp, api = tempfile.mkdtemp(), _FakeAPI()
    eng = build(tmp, api)
    book["LKA"] = (2.0, 2.05)
    feed(eng, "BOUGHT LKA 100C 10/9 2.0")
    os.chmod(eng.log_path, stat.S_IREAD)
    book["LKA"] = (2.40, 2.50)
    r = feed(eng, "ALL OUT LKA 100C 10/9 2.45")
    check("log can't be written: the sale still goes through", r[0][0] == "SOLD" and not has(eng, "LKA"), str(r[0]))
    check("...and you are still told", any("SOLD" in n for n in notes), str(notes))
    check("...and today's profit/loss still counts it", abs(eng.realized_today() - 30.0) < 0.01, str(eng.realized_today()))
    os.chmod(eng.log_path, stat.S_IREAD | stat.S_IWRITE)
    feed(eng, "BOUGHT shares VST 138.59")                    # any next log line
    with open(eng.log_path, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    check("once it can be written again, the waiting lines are saved in order",
          [x["decision"] for x in rows[-2:]] == ["SOLD", "IGNORE"], str([x["decision"] for x in rows]))

    # --- the state file is damaged (power cut mid-write) ------------------------------------------
    with open(eng.state_path, encoding="utf-8") as f:
        whole = f.read()
    check("state file is complete after a save, nothing half-written left behind",
          bool(json.loads(whole)) and not os.path.exists(eng.state_path + ".tmp"))
    with open(eng.state_path, "w", encoding="utf-8") as f:
        f.write(whole[: len(whole) // 2])
    notes.clear()
    eng2 = build(tmp, api)
    check("damaged state file: starts from the spare copy with the pot intact",
          abs(eng2.live_realized - 30.0) < 0.01, str(eng2.live_realized))
    check("...and says so", any("spare copy" in n for n in notes), str(notes))
    for path in (eng.state_path, eng.state_path + ".bak"):
        with open(path, "w", encoding="utf-8") as f:
            f.write("{ not a state file")
    try:
        build(tmp, api)
        stopped = False
    except SystemExit:
        stopped = True
    check("state file and spare copy both damaged: refuses to start with an empty memory", stopped)

    # --- Robinhood refuses a sell ----------------------------------------------------------------
    tmp, api = tempfile.mkdtemp(), _FakeAPI()
    eng = build(tmp, api)

    def tick(sec=5):
        clock["t"] += timedelta(seconds=sec)
        notes.clear()
        eng.tick()

    book["HND"] = (2.0, 2.05)
    feed(eng, "BOUGHT HND 100C 10/9 2.0")
    api.held = []                                            # sold by hand in the app
    api.script = ["refuse"] * 50
    pot = eng.live_realized
    r = feed(eng, "ALL OUT HND 100C 10/9 2.4")
    check("sell refused, Robinhood shows no position: not dropped on one reading alone",
          r[0][0] == "SELL PENDING" and has(eng, "HND"), str(r[0]))
    said = []
    for _ in range(24):
        tick()
        said += notes
    check("refused again and still not shown: dropped from the books", not has(eng, "HND"))
    check("...you are told, and the pot is left alone",
          any("removed from the bot's books" in n for n in said) and eng.live_realized == pot, str(said))
    check("...after two sell orders, and no more", len(api.script) == 48, str(50 - len(api.script)))

    def not_answering():
        raise RuntimeError("Robinhood is not answering")

    book["RFS"] = (2.0, 2.05)
    api.script = []
    feed(eng, "BOUGHT RFS 100C 10/9 2.0")
    api.held = []
    api.holdings = not_answering                             # positions can't be read at all
    api.script = ["refuse"] * 500
    feed(eng, "ALL OUT RFS 100C 10/9 2.4")
    for _ in range(60):                                      # five minutes of timer laps
        tick()
    check("sell refused and positions unreadable: never dropped", has(eng, "RFS"))
    del api.holdings
    api.held = [("RFS", "2026-10-09", 100.0, "C", 1)]        # readable again: still held, sells still refused
    for _ in range(60):                                      # five more minutes
        tick()
    tries = 500 - len(api.script)
    check("sell refused but still held: keeps trying, about once a minute instead of every lap",
          has(eng, "RFS") and 8 <= tries <= 20, f"{tries} orders in 10 min")
    api.script = []
    for _ in range(15):
        tick()
    check("...and sells at the bid once Robinhood accepts the order (price not walked down meanwhile)",
          not has(eng, "RFS") and api.placed[-1] == ("sell", 2.0), str(api.placed[-1]))

    # --- a real sale while catching up on missed alerts ---------------------------------------------
    book["QTS"] = (2.0, 2.05)
    feed(eng, "BOUGHT QTS 100C 10/9 2.0")
    book["QTS"] = (2.40, 2.50)
    eng.quiet = True
    r = feed(eng, "ALL OUT QTS 100C 10/9 2.45", age=600, source="catch-up")
    eng.quiet = False
    check("a real sale while catching up is still announced",
          r[0][0] == "SOLD" and any("SOLD" in n for n in notes), str(notes))
    print("\n  all failure-handling checks passed")


def selftest_runner():
    """The runner: 2 contracts when both fit within MAX_SINGLE_CONTRACT_COST. The first is sold at his first
    trim (or by the take-profit); the runner only when he is fully out, or if it falls back to our buy price."""
    import tempfile
    print("\n\n========== runner: 2 contracts, 1 sold at his first trim, 1 kept until he is out ==========")
    tmp = tempfile.mkdtemp()
    clock = {"t": datetime(2026, 10, 6, 10, 0, 0, tzinfo=ET)}
    book, notes = {}, []
    s = Settings()
    s.keep_runner, s.max_single_contract_cost, s.max_deployed, s.project_capital = True, 500, 5000, 5000
    s.max_open_positions, s.max_buys_per_day, s.take_profit_pct, s.disaster_stop_pct = 20, 50, 40, 50

    def quotes(ticker, exp, strike, cp):
        b = book.get(ticker)
        return {"bid": b[0], "ask": b[1], "mark": round((b[0] + b[1]) / 2, 3)} if b else None

    api = _FakeAPI()

    def build():
        brk = Broker(api, s, lambda x: None, orders_csv=os.path.join(tmp, "orders.csv"), sleep=lambda x: None,
                     now=lambda: clock["t"])
        return Engine(s, quotes, lambda x: notes.append(x), now=lambda: clock["t"],
                      log_path=os.path.join(tmp, "log.csv"), state_path=os.path.join(tmp, "s.json"),
                      broker=brk, stop_file=os.path.join(tmp, "STOP_TRADING"))

    eng = build()

    def feed(text, e=None):
        clock["t"] += timedelta(seconds=3)
        notes.clear()
        return (e or eng).handle_message(text, clock["t"] - timedelta(seconds=2))

    def tick(sec=11):
        clock["t"] += timedelta(seconds=sec)
        notes.clear()
        eng.tick()

    def check(label, cond, detail=""):
        print(f"  {'PASS' if cond else 'FAIL'}  {label}  {detail}")
        assert cond, label

    def pos(ticker, e=None):
        return next((p for p in (e or eng).positions if p.ticker == ticker and p.qty_open > 0), None)

    # --- how many to buy ------------------------------------------------------------------------
    book["RNA"] = (1.95, 2.00)
    r = feed("BOUGHT RNA 100C 10/16 2.0")
    check("2 fit within $500 -> buys 2, as a runner",
          r[0][0] == "BOUGHT" and r[0][2] == 2 and pos("RNA").runner and api.sizes[-1] == ("buy", 2), str(r[0]))
    check("...and the buy card says so", any("1 runner kept" in n for n in notes), str(notes))
    book["RNB"] = (2.40, 2.47)
    r = feed("BOUGHT RNB 100C 10/16 2.47")
    check("2 fit at the ask (2 x $247) but not at the limit price (2 x $255) -> buys 1",
          r[0][0] == "BOUGHT" and r[0][2] == 1 and not pos("RNB").runner, str(r[0]))
    book["RNC"] = (2.95, 3.00)
    r = feed("BOUGHT RNC 100C 10/16 3.0")
    check("2 would cost more than $500 -> buys 1, as before", r[0][2] == 1 and not pos("RNC").runner, str(r[0]))
    book["RNM"] = (1.95, 2.00)
    r = feed("BOUGHT RNM 100C 10/16 2.0‼️")
    check("a lotto that 2 would fit: still just 1 contract", r[0][0] == "BOUGHT" and r[0][2] == 1
          and not pos("RNM").runner and pos("RNM").lotto, str(r[0]))
    book["RNN"] = (1.95, 2.00)
    r = feed("BOUGHT RNN 100C 10/16 2.0 - small lotto")
    check("...the same when he writes 'lotto'", r[0][2] == 1 and not pos("RNN").runner, str(r[0]))

    # --- his trims and his full exit ------------------------------------------------------------
    pot = eng.live_realized
    book["RNA"] = (2.55, 2.60)
    r = feed("SOLD 1/4 RNA 100C 10/16 2.6")
    check("his first trim (even a 1/4) sells 1 of the 2",
          r[0][0] == "SOLD" and r[0][2] == 1 and pos("RNA").qty_open == 1, str(r[0]))
    check("...and says the runner is kept", any("RUNNER kept" in n for n in notes), str(notes))
    r = feed("SOLD 1/4 RNA 100C 10/16 2.8")
    check("his next trim: the runner is held", r[0][0] == "HOLD" and pos("RNA").qty_open == 1, str(r[0]))
    book["RNA"] = (3.40, 3.50)
    tick()
    check("the runner has no take-profit (up 66%, still held)", pos("RNA") is not None)
    r = feed("ALL OUT RNA 100C 10/16 3.45")
    check("his ALL OUT sells the runner", r[0][0] == "SOLD" and r[0][2] == 1 and pos("RNA") is None, str(r[0]))
    check("both sales count in the pot (+$50 and +$135)", abs(eng.live_realized - pot - 185) < 0.01,
          str(eng.live_realized - pot))

    book["RNG"] = (1.95, 2.00)
    feed("BOUGHT RNG 100C 10/16 2.0")
    book["RNG"] = (2.55, 2.60)
    feed("SOLD 1/2 RNG 100C 10/16 2.6")
    r = feed("SOLD 1/2 RNG 100C 10/16 2.8")
    check("his sells adding up to all of it also sell the runner", r[0][0] == "SOLD" and pos("RNG") is None, str(r[0]))
    book["RNH"] = (1.95, 2.00)
    feed("BOUGHT RNH 100C 10/16 2.0")
    r = feed("SOLD RNH 100C 10/16 2.1")
    check("a sell with no size (he is out) sells both at once",
          r[0][0] == "SOLD" and r[0][2] == 2 and pos("RNH") is None, str(r[0]))

    # --- the bot's own exits ------------------------------------------------------------------------
    book["RND"] = (1.95, 2.00)
    feed("BOUGHT RND 100C 10/16 2.0")
    book["RND"] = (2.55, 2.60)
    feed("SOLD 1/2 RND 100C 10/16 2.6")
    book["RND"] = (2.00, 2.10)                       # mid 2.05: back to our buy price
    tick()
    check("runner falls back to our buy price -> sold by the runner stop",
          pos("RND") is None and any("runner stop" in n for n in notes), str(notes))

    book["RNE"] = (1.95, 2.00)
    feed("BOUGHT RNE 100C 10/16 2.0")
    book["RNE"] = (2.95, 3.00)                       # bid 44% over our 2.05
    tick()
    check("take-profit sells 1 of the 2 and keeps the runner", pos("RNE") and pos("RNE").qty_open == 1, str(notes))
    tick()
    check("...and does not fire again on the runner", pos("RNE") and pos("RNE").qty_open == 1)
    r = feed("SOLD 1/2 RNE 100C 10/16 3.0")
    check("his first trim after that: the runner is held", r[0][0] == "HOLD", str(r[0]))
    r = feed("ALL OUT RNE 100C 10/16 3.5")
    check("...until his ALL OUT", r[0][0] == "SOLD" and pos("RNE") is None, str(r[0]))

    book["RNF"] = (1.95, 2.00)
    feed("BOUGHT RNF 100C 10/16 2.0")
    book["RNF"] = (0.90, 1.00)                       # mid 0.95: down 54%
    tick()
    check("disaster stop before any trim sells both", pos("RNF") is None and api.sizes[-1] == ("sell", 2),
          str(api.sizes[-1]))

    # --- limits and odd cases ---------------------------------------------------------------------
    book["RNI"] = (1.95, 2.00)
    api.script = ["fill_one"]
    r = feed("BOUGHT RNI 100C 10/16 2.0")
    check("only 1 of the 2 filled -> an ordinary 1-contract position",
          r[0][2] == 1 and pos("RNI").qty_orig == 1 and not pos("RNI").runner, str(r[0]))
    r = feed("SOLD 1/2 RNI 100C 10/16 2.6")
    check("...which sells at his half, as before", r[0][0] == "SOLD" and pos("RNI") is None, str(r[0]))

    s.project_capital += 300 - eng.capital_free()     # leave $300 of the pot free
    book["RNJ"] = (1.95, 2.00)
    r = feed("BOUGHT RNJ 100C 10/16 2.0")
    check("only $300 of the pot free: buys 1, not 2", r[0][0] == "BOUGHT" and r[0][2] == 1, str(r[0]))
    s.project_capital = 5000

    s.keep_runner = False
    book["RNK"] = (1.95, 2.00)
    r = feed("BOUGHT RNK 100C 10/16 2.0")
    check("KEEP_RUNNER=false -> 1 contract", r[0][2] == 1 and not pos("RNK").runner, str(r[0]))
    s.keep_runner = True

    book["RNL"] = (1.95, 2.00)
    feed("BOUGHT RNL 100C 10/16 2.0")
    book["RNL"] = (2.55, 2.60)
    feed("SOLD 1/4 RNL 100C 10/16 2.6")
    eng2 = build()
    check("after a restart the runner is still a runner", pos("RNL", eng2) and pos("RNL", eng2).runner_left)

    paper = Engine(s, quotes, lambda x: notes.append(x), now=lambda: clock["t"],
                   log_path=os.path.join(tmp, "paper_log.csv"), state_path=os.path.join(tmp, "paper.json"),
                   stop_file=os.path.join(tmp, "STOP_TRADING"))
    book["RNP"] = (1.95, 2.00)
    r = feed("BOUGHT RNP 100C 10/16 2.0", paper)
    check("paper mode: WOULD BUY 2", r[0][0] == "WOULD BUY" and r[0][2] == 2, str(r[0]))
    book["RNP"] = (2.55, 2.60)
    r = feed("SOLD 1/2 RNP 100C 10/16 2.6", paper)
    check("paper mode: his first trim -> WOULD SELL 1, runner kept",
          r[0][0] == "WOULD SELL" and r[0][2] == 1 and pos("RNP", paper).runner_left, str(r[0]))
    print("\n  all runner checks passed")


def selftest_live():
    """The live-order flow against a pretend Robinhood: fills, timeouts, a fill that lands during
    the cancel, a cancel that never confirms, refused orders, a sell that needs repricing, the
    disaster stop, the kill switch and the start-up reconcile."""
    import tempfile
    print("\n\n========== LIVE order flow (pretend Robinhood) ==========")
    tmp = tempfile.mkdtemp()
    clock = {"t": datetime(2026, 10, 6, 10, 0, 0, tzinfo=ET)}
    book = {}                                    # ticker -> (bid, ask)
    notes = []
    s = Settings()
    s.keep_runner = False          # 1 contract per alert here (runner: selftest_runner)
    s.max_buys_per_day, s.disaster_stop_pct, s.partial_rounding = 6, 50, "nearest"

    def quotes(ticker, exp, strike, cp):
        b = book.get(ticker)
        return {"bid": b[0], "ask": b[1], "mark": round((b[0] + b[1]) / 2, 3)} if b else None

    api = _FakeAPI()
    stopf = os.path.join(tmp, "STOP_TRADING")

    def build(path="s.json"):
        brk = Broker(api, s, lambda x: None, orders_csv=os.path.join(tmp, "orders.csv"), sleep=lambda x: None,
                     now=lambda: clock["t"])
        return Engine(s, quotes, lambda x: notes.append(x), now=lambda: clock["t"],
                      log_path=os.path.join(tmp, "log.csv"), state_path=os.path.join(tmp, path),
                      broker=brk, stop_file=stopf)

    eng = build()

    def feed(text, advance=3):
        clock["t"] += timedelta(seconds=advance)
        notes.clear()
        return eng.handle_message(text, clock["t"] - timedelta(seconds=2))

    def tick(sec=5):
        clock["t"] += timedelta(seconds=sec)
        notes.clear()
        eng.tick()

    def check(label, cond, detail=""):
        print(f"  {'PASS' if cond else 'FAIL'}  {label}  {detail}")
        assert cond, label

    def decision():
        with open(eng.log_path, encoding="utf-8-sig") as f:
            r = list(csv.DictReader(f))[-1]
        return r["decision"], r["reason"]

    # --- lotto marker -------------------------------------------------------------
    check("red double ! counts as lotto", is_lotto("BOUGHT A 100C 10/9 1.5‼️"))
    check("the word lotto counts", is_lotto("BOUGHT A 100C 10/9 1.5 - small lotto"))
    check("a normal alert is not a lotto", not is_lotto("BOUGHT A 100C 10/9 1.5"))

    # --- buys: sizing and lotto price rule ---------------------------------------
    book["AAA"] = (1.50, 1.55)
    r = feed("BOUGHT AAA 100C 10/9 1.5‼️")
    check("lotto under 2.50 is bought", r[0][0] == "BOUGHT", str(r[0]))
    check("always exactly 1 contract", r[0][2] == 1)
    check("limit = ask + 1 step, capped at his price +10%", api.placed[-1] == ("buy", 1.60), str(api.placed[-1]))
    check("fill price recorded from the order", abs(eng.positions[0].entry_price - 1.60) < 1e-9 and eng.positions[0].real)
    book["BBB"] = (2.5, 2.55)
    r = feed("BOUGHT BBB 100C 10/9 2.5‼️")
    check("lotto at 2.50 is skipped", r[0][0] == "SKIP" and "lotto" in r[0][2], r[0][2])
    book["CCC"] = (6.3, 6.4)
    r = feed("BOUGHT CCC 100C 10/9 6.3")
    check("non-lotto at 6.30 is still 1 contract", r[0][0] == "BOUGHT" and r[0][2] == 1, str(r[0]))
    check("above 3.00 the price step is 0.10", api.placed[-1][1] in (6.5, 6.6, 6.9), str(api.placed[-1]))
    book["DDD"] = (12.0, 12.1)
    r = feed("BOUGHT DDD 100C 10/9 12.0")
    check("one contract over $1,000 is skipped", r[0][0] == "SKIP", r[0][2])
    r = feed("BOUGHT AAA 100C 10/9 1.5")
    check("same contract again is skipped", r[0][0] == "SKIP" and "already holding" in r[0][2])

    # --- order problems -----------------------------------------------------------
    book["EEE"] = (1.0, 1.05)
    api.script = ["nofill"]
    r = feed("BOUGHT EEE 100C 10/9 1.0")
    check("unfilled buy is cancelled, no position", r[0][0] == "NOT FILLED" and not any(
        p.ticker == "EEE" for p in eng.positions), str(r[0]))
    api.script = ["fill_on_cancel"]
    r = feed("BOUGHT EEE 100C 10/9 1.0")
    check("fill that lands during the cancel is kept", r[0][0] == "BOUGHT", str(r[0]))
    api.script = ["refuse"]
    book["FFF"] = (1.0, 1.05)
    r = feed("BOUGHT FFF 100C 10/9 1.0")
    check("order refused by Robinhood is reported", r[0][0] == "NOT FILLED" and "buying power" in decision()[1], decision()[1])

    # --- sells --------------------------------------------------------------------
    book["AAA"] = (1.85, 1.90)
    r = feed("ALL OUT AAA 100C 10/9 1.9")
    check("his ALL OUT sells our contract", r[0][0] == "SOLD", str(r[0]))
    check("sell limit is the bid", api.placed[-1] == ("sell", 1.85), str(api.placed[-1]))
    check("P&L uses the real fill (1.85 vs 1.60)", abs(r[0][4] - 25.0) < 0.01, str(r[0]))
    check("position closed", not any(p.ticker == "AAA" for p in eng.positions))
    book["CCC"] = (6.8, 6.9)
    r = feed("SOLD 1/4 CCC 100C 10/9 6.9")
    check("1 contract, he sold only 1/4 -> hold", r[0][0] == "HOLD", str(r[0]))
    api.script = ["nofill", "nofill", "fill"]
    r = feed("SOLD 1/4 CCC 100C 10/9 7.0")
    check("1 contract, he is now at 1/2 -> sell", r[0][0] == "SELL PENDING", str(r[0]))
    tick(); tick()
    ccc = [p for p in api.placed if p[0] == "sell"][-3:]
    check("not filled -> retried one step lower each time, then sold",
          [p[1] for p in ccc] == [6.8, 6.7, 6.6] and not any(p.ticker == "CCC" for p in eng.positions), str(ccc))

    # --- emergency stop -----------------------------------------------------------
    book["GGG"] = (1.0, 1.05)
    feed("BOUGHT GGG 100C 10/9 1.0")
    check("bought GGG", any(p.ticker == "GGG" for p in eng.positions))
    book["GGG"] = (0.50, 0.58)          # mid 0.54, paid 1.10 -> down 51%
    tick(20)
    check("mid fell 50%+ -> sold by the disaster stop", not any(p.ticker == "GGG" for p in eng.positions)
          and decision()[0] == "SOLD" and "disaster stop" in decision()[1], str(decision()))

    # --- take profit (more than +40% over our fill, measured on the bid) ----------------
    s.take_profit_pct = 40
    book["JJJ"] = (1.0, 1.05)
    feed("BOUGHT JJJ 100C 10/9 1.0")                     # filled at 1.10 -> trigger is a bid above 1.54
    check("bought JJJ at 1.10", any(p.ticker == "JJJ" and abs(p.entry_price - 1.10) < 1e-9 for p in eng.positions))
    book["JJJ"] = (1.50, 1.55)                           # +36%: not yet
    tick(15)
    check("+36% -> keep holding", any(p.ticker == "JJJ" for p in eng.positions))
    book["JJJ"] = (1.54, 1.60)                           # exactly +40%: 'more than' is required
    tick(15)
    check("exactly +40% -> keep holding", any(p.ticker == "JJJ" for p in eng.positions))
    book["JJJ"] = (1.56, 1.62)                           # +42%
    tick(15)
    check("+42% -> sold by take profit", not any(p.ticker == "JJJ" for p in eng.positions)
          and decision()[0] == "SOLD" and "take profit" in decision()[1], str(decision()))
    r = feed("ALL OUT JJJ 100C 10/9 2.0")
    check("his later ALL OUT is ignored (already out)", r[0][0] == "IGNORE", str(r[0]))
    s.take_profit_pct = 0
    book["KKK"] = (1.0, 1.05)
    feed("BOUGHT KKK 100C 10/9 1.0")
    book["KKK"] = (3.0, 3.1)
    tick(15)
    check("take profit off (0) -> holds a +170% winner until he sells",
          any(p.ticker == "KKK" for p in eng.positions))
    s.take_profit_pct = 40
    tick(15)
    check("turned back on -> sells it", not any(p.ticker == "KKK" for p in eng.positions))

    # --- gates --------------------------------------------------------------------
    book["HHH"] = (1.0, 1.05)
    open(stopf, "w").close()
    r = feed("BOUGHT HHH 100C 10/9 1.0")
    check("STOP_TRADING file -> no new buys", r[0][0] == "SKIP" and "STOP_TRADING" in r[0][2], r[0][2])
    os.remove(stopf)
    s.max_buys_per_day = 1
    r = feed("BOUGHT HHH 100C 10/9 1.0")
    check("daily buy limit", r[0][0] == "SKIP" and "buys today" in r[0][2], r[0][2])
    s.max_buys_per_day = 99
    api.bp = 50.0
    r = feed("BOUGHT HHH 100C 10/9 1.0")
    check("not enough buying power -> skip", r[0][0] == "SKIP" and "buying power" in r[0][2], r[0][2])
    api.bp = 5000.0
    api.script = ["stuck"]
    r = feed("BOUGHT HHH 100C 10/9 1.0")
    check("cancel that never confirms -> halt all buying", r[0][0] == "ERROR" and eng.halted, str(r[0]))
    book["III"] = (1.0, 1.05)
    r = feed("BOUGHT III 100C 10/9 1.0")
    check("buying stays halted", r[0][0] == "SKIP" and "HALTED" in r[0][2], r[0][2])
    eng.halted = ""

    # --- restart + reconcile ------------------------------------------------------
    api.script = []
    n_real = len([p for p in eng.positions if p.real])
    eng.positions.append(Position("ZZZ", 50.0, "C", "2026-10-09", 1, 1, 1.0, 1.0, "x", real=True))
    eng.inflight.append("o1")
    api.orders["o99"] = {"id": "o99", "state": "queued", "beh": "nofill", "price": 1, "processed_quantity": "0",
                         "legs": [{"executions": []}]}
    eng.inflight.append("o99")
    api.held = [("YYY", "2026-10-09", 10.0, "C", 1)] + [(p.ticker, p.exp, p.strike, p.cp, p.qty_open)
                                                         for p in eng.positions if p.ticker != "ZZZ"]
    eng.save_state()
    eng2 = build()
    check("real positions survive a restart", len([p for p in eng2.positions if p.real]) == n_real + 1)
    eng = eng2
    msgs = eng.startup_live()
    check("start-up: leftover open order cancelled", any("cancelled leftover order o99" in m for m in msgs), str(msgs))
    check("start-up: position Robinhood doesn't have is dropped", not any(p.ticker == "ZZZ" for p in eng.positions))
    check("start-up: holding the bot didn't buy is flagged, not touched", any("YYY" in m for m in msgs))
    # --- the project's money: $5,000 pot, hard stop when it is gone ----------------------
    eng.positions = [p for p in eng.positions if not p.real]
    eng.live_realized, eng.loss_alerts = 0.0, []
    check("default project capital is $5,000", Settings().project_capital == 5000 and Settings().max_deployed == 5000)
    s.project_capital = 300.0                    # a tiny pot, so the limits are easy to hit
    check("all of the pot is free at the start", eng.capital_free() == 300.0)
    book["LLL"] = (1.50, 1.55)
    r = feed("BOUGHT LLL 100C 10/9 1.5")
    check("a $160 buy fits in the $300 pot", r[0][0] == "BOUGHT", str(r[0]))
    check("money in open positions is not 'free'", abs(eng.capital_free() - 140.0) < 0.01, str(eng.capital_free()))
    book["MMM"] = (1.50, 1.55)
    r = feed("BOUGHT MMM 100C 10/9 1.5")
    check("second $160 buy doesn't fit -> skipped, nothing sent",
          r[0][0] == "SKIP" and "project capital" in r[0][2] and not any(p.ticker == "MMM" for p in eng.positions), r[0][2])
    eng._after_live_pnl(-160.0)                  # pretend the first trade lost $160 (53% of the pot)
    check("a 50% loss is announced once", sum("down 50%" in n for n in notes) == 1, str(notes))
    notes.clear()
    eng._after_live_pnl(-5.0)
    check("...and not announced again", not any("down 50%" in n for n in notes), str(notes))
    check("pot left = 300 - 165 = 135", abs(eng.capital_left() - 135.0) < 0.01, str(eng.capital_left()))
    notes.clear()
    eng._after_live_pnl(-140.0)                  # the rest is gone
    check("pot gone -> PROJECT ENDED announced", any("PROJECT ENDED" in n for n in notes), str(notes))
    r = feed("BOUGHT MMM 100C 10/9 1.5")
    check("project ended -> no more buys", r[0][0] == "SKIP" and "PROJECT ENDED" in r[0][2], r[0][2])
    book["LLL"] = (1.55, 1.60)
    r = feed("ALL OUT LLL 100C 10/9 1.6")
    check("...but the open position is still sold", r[0][0] == "SOLD", str(r[0]))
    led = eng.live_realized
    eng3 = build()
    check("the ledger survives a restart", abs(eng3.live_realized - (led + 0.0)) < 0.01 and eng3.project_over(),
          f"{eng3.live_realized} vs {led}")
    s.project_capital = 5000.0                   # (a new pot would restart it)
    check("raising PROJECT_CAPITAL gives a new pot", not eng3.project_over())
    # --- a paper run never takes over real positions ------------------------------
    paper = Engine(s, quotes, lambda x: None, now=lambda: clock["t"], log_path=os.path.join(tmp, "log.csv"),
                   state_path=os.path.join(tmp, "s.json"))
    check("paper mode ignores real positions (and keeps them in the file)", not paper.positions)
    print("\n  orders_log.csv written:", os.path.exists(os.path.join(tmp, "orders.csv")))
    print("\n  all live-flow checks passed")


def selftest_exit_typos():
    """Closest-match handling of mistyped exit alerts, including two real ones from his
    channel (SPCX '10/9' for 10/30, ORCL '10/1' for 10/16). Made-up quotes."""
    import tempfile
    print("\n\n========== Mistyped exit alerts: closest match ==========")
    tmp = tempfile.mkdtemp()
    clock = {"t": datetime(2026, 10, 5, 10, 4, 47, tzinfo=ET)}
    book = {}        # (ticker, strike, exp_iso) -> (bid, ask)

    def q1(ticker, exp, strike, cp):
        b = book.get((ticker, strike, exp.isoformat()))
        return {"bid": b[0], "ask": b[1], "mark": round(sum(b) / 2, 3)} if b else None

    def qmany(items):
        out = {}
        for t in items:
            q = q1(t.ticker, date.fromisoformat(t.exp), t.strike, t.cp)
            if q:
                out[t.key] = q
        return out

    s = Settings()

    s.keep_runner = False          # 1 contract per alert here (runner: selftest_runner)
    tr = Tracker(s, qmany, lambda tk: {}, now=lambda: clock["t"], path_csv=os.path.join(tmp, "p.csv"),
                 save=lambda: None, notify=lambda x: None)
    eng = Engine(s, q1, lambda x: print("   ->", x), now=lambda: clock["t"],
                 log_path=os.path.join(tmp, "l.csv"), state_path=os.path.join(tmp, "s.json"), tracker=tr)
    old = datetime(2026, 9, 28, 10, 0, tzinfo=ET)
    eng.quiet = True
    for text, when in [("BOUGHT SPCX 172.5C 10/30 2.7", datetime(2026, 10, 2, 9, 54, tzinfo=ET)),
                       ("BOUGHT ORCL 155C 10/16 0.98", old),
                       ("BOUGHT TSLA 300C 10/9 2.45", datetime(2026, 10, 5, 9, 30, tzinfo=ET)),
                       ("BOUGHT MU 1100C 10/9 5.0", datetime(2026, 10, 5, 9, 31, tzinfo=ET)),
                       ("BOUGHT MU 1130C 10/9 5.1", datetime(2026, 10, 5, 9, 32, tzinfo=ET))]:
        eng.handle_message(text, when, None, "catch-up")
    eng.quiet = False
    book[("SPCX", 172.5, "2026-10-30")] = (5.40, 5.50)
    book[("ORCL", 155.0, "2026-10-16")] = (1.60, 1.70)
    book[("TSLA", 300.0, "2026-10-09")] = (2.40, 2.50)
    book[("MU", 1100.0, "2026-10-09")] = (5.00, 5.10)
    book[("MU", 1130.0, "2026-10-09")] = (5.05, 5.15)

    def run_case(label, text, when, source, expect, expect_open=None):
        res = eng.handle_message(text, when, None, source)
        got = res[0][0] if res else "?"
        reason = ""
        with open(eng.log_path, encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        if rows:
            reason = rows[-1]["reason"]
        ok = got == expect
        print(f"  {'PASS' if ok else 'FAIL'}  {label}: {text!r} -> {got}"
              f"{'' if ok else f' (expected {expect})'}\n        {reason[:170]}")
        assert ok, label
        if expect_open is not None:
            names = [t.contract for t in tr.open_contracts()]
            assert (expect_open in names) == (expect != "IGNORE" and expect_open in names), label
        return rows[-1] if rows else None

    now = clock["t"]
    # 1. His real SPCX typo: live, price 5.5 sits inside the 10/30 contract's 5.40/5.50
    run_case("SPCX date typo, price fits", "ALL OUT SPCX 172.5C 10/9 5.5", now, "live", "IGNORE")
    t = tr.items[contract_key("SPCX", 172.5, "C", date(2026, 10, 30))]
    assert t.his_exit_at and t.his_exit_price == 5.5, "SPCX 10/30 should now be marked as exited"
    print("        tracker: SPCX 10/30 marked as his exit @ 5.5 -> PASS")
    # 2. Same alert again: the 10/30 contract is no longer open, so nothing matches
    run_case("repeat of the same exit", "ALL OUT SPCX 172.5C 10/9 5.5", now, "live", "IGNORE")
    # 3. His real ORCL typo, seen days late (can't check the price) -> flagged, not applied
    run_case("ORCL date typo, old alert", "ALL OUT ORCL 155C 10/1 2.75", old + timedelta(minutes=25),
             "catch-up", "CHECK")
    assert not tr.items[contract_key("ORCL", 155, "C", date(2026, 10, 16))].his_exit_at
    print("        tracker: ORCL 10/16 still open (not guessed) -> PASS")
    # 4. Strike typo but his price is far from the live quote -> flagged
    run_case("TSLA strike typo, price does not fit", "SOLD 1/2 TSLA 310C 10/9 9.9", now, "live", "CHECK")
    # 5. Strike typo and the price fits -> accepted
    run_case("TSLA strike typo, price fits", "SOLD 1/2 TSLA 310C 10/9 2.5", now, "live", "IGNORE")
    # 6. Two of his contracts fit equally well -> flagged, never guessed
    run_case("MU strike typo, two fit", "SOLD MU 1115C 10/9 5.05", now, "live", "CHECK")
    # 7. Nothing close: a contract we never saw him buy
    run_case("unknown contract", "ALL OUT ZZZZ 50C 10/9 1.0", now, "live", "IGNORE")
    # 8. No live quote available (e.g. market closed) -> flagged
    book.pop(("MU", 1100.0, "2026-10-09")); book.pop(("MU", 1130.0, "2026-10-09"))
    run_case("MU typo, no quote", "ALL OUT MU 1100P 10/9 5.0", now, "live", "CHECK")
    with open(os.path.join(tmp, "p.csv"), encoding="utf-8-sig") as f:
        marks = [r for r in csv.DictReader(f) if r["event"].startswith(("HIS_ALL_OUT", "HIS_SELL", "POSSIBLE"))]
    print("\n  price_paths.csv markers:")
    for r in marks:
        print(f"    {r['contract']:<18} {r['event'][:140]}")
    print("\n  all exit-typo checks passed")


def selftest_tracker():
    """Price tracking across a restart and two days, with made-up prices."""
    import tempfile
    print("\n\n========== Price tracking (made-up prices) ==========")
    tmp = tempfile.mkdtemp()
    clock = {"t": datetime(2026, 10, 2, 9, 0, tzinfo=ET)}
    book = {}

    def q1(ticker, exp, strike, cp):
        b = book.get((ticker, strike))
        return {"bid": b[0], "ask": b[1], "mark": round(sum(b) / 2, 3)} if b else None

    def qmany(items):
        return {t.key: q1(t.ticker, None, t.strike, t.cp) for t in items if (t.ticker, t.strike) in book}

    under = lambda tickers: {t: 100.0 for t in tickers}

    def build():
        s = Settings()
        s.keep_runner = False          # 1 contract per alert here (runner: selftest_runner)
        tr = Tracker(s, qmany, under, now=lambda: clock["t"], path_csv=os.path.join(tmp, "paths.csv"),
                     save=lambda: None, notify=lambda x: print("   ->", x))
        eng = Engine(s, q1, lambda x: None, now=lambda: clock["t"], log_path=os.path.join(tmp, "log.csv"),
                     state_path=os.path.join(tmp, "s.json"), tracker=tr)
        return eng, tr

    def at(d, h, m):
        clock["t"] = datetime(2026, 10, d, h, m, tzinfo=ET)

    eng, tr = build()
    # catch-up on start: messages posted before the bot was running
    eng.quiet = True
    eng.handle_message("BOUGHT AVGO 400C 10/30 2.4", datetime(2026, 10, 2, 9, 48, tzinfo=ET), 101, "catch-up")
    eng.handle_message("BOUGHT SNDK 1800C 10/2 2.85", datetime(2026, 10, 2, 9, 50, tzinfo=ET), 102, "catch-up")
    eng.handle_message("BOUGHT TSAL 300C 10/9 2.45", datetime(2026, 10, 2, 9, 51, tzinfo=ET), 103, "catch-up")
    eng.quiet = False
    print("tracking after catch-up:", [t.contract for t in tr.active()])
    # same message delivered again live -> ignored
    print("duplicate delivery ignored:", eng.handle_message("BOUGHT AVGO 400C 10/30 2.4",
          datetime(2026, 10, 2, 9, 48, tzinfo=ET), 101, "live") == [])

    path = [(10, 0, 2.30, 2.40, 2.80, 2.90), (11, 0, 2.70, 2.80, 3.40, 3.50), (12, 0, 2.10, 2.20, 2.00, 2.10),
            (15, 59, 2.50, 2.60, 0.40, 0.50)]
    for h, m, ab, aa, sb, sa in path:
        at(2, h, m)
        book[("AVGO", 400.0)] = (ab, aa)
        book[("SNDK", 1800.0)] = (sb, sa)
        if h == 11:
            eng.handle_message("SOLD 1/2 SNDK 1800C 10/2 3.45", clock["t"], 104, "live")
        for k in range(12 if h == 10 else 1):      # 10 extra samples so TSAL gives up
            clock["t"] += timedelta(seconds=31)
            if tr.due():
                tr.sample()
    at(2, 16, 1)
    tr.sweep()
    print("Friday close, still tracking:", [t.contract for t in tr.active()])

    # restart on Monday: state comes back from the file
    eng, tr = build()
    at(5, 9, 31)
    book[("AVGO", 400.0)] = (3.00, 3.10)
    tr.sample()
    eng.handle_message("ALL OUT AVGO 400C 10/30 3.05", clock["t"], 105, "live")
    at(5, 10, 2)
    tr.sample()
    print("Monday 10:02, still tracking:", [t.contract for t in tr.active()])
    print("\n".join(tr.summary_lines()))
    with open(os.path.join(tmp, "paths.csv"), encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    print(f"\nprice_paths.csv: {len(rows)} rows. Events:")
    for r in rows:
        if r["event"]:
            print(f"  {r['ts']}  {r['contract']:<18} {r['bucket']:<6} {r['event']:<55} mark={r['mark']}")


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass
    if "--selftest" in sys.argv:
        # before .env is loaded: the checks expect the default settings, not your own limits
        selftest()
        sys.exit(0)
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(HERE, ".env"))
    except ImportError:
        pass
    refuse_old_layout()
    if "--list-chats" in sys.argv:
        asyncio.run(list_chats())
    else:
        try:
            asyncio.run(run())
        except KeyboardInterrupt:
            pass
