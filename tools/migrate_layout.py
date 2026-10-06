"""One-time move from the old flat layout to the new one.

    python tools/migrate_layout.py            (shows what it would do, changes nothing)
    python tools/migrate_layout.py --apply    (does it)

CLOSE THE BOT FIRST. Nothing is deleted:
  * the bot's memory and logins move into data/  (paper_positions.json becomes data/state.json)
  * each CSV is split by date into runs/<date>/ and the original goes to legacy/
"""
import csv
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APPLY = "--apply" in sys.argv

# old name -> (new folder, new name)
MOVES = {
    "paper_positions.json": ("data", "state.json"),
    "robinhood.pickle": ("data", "robinhood.pickle"),
    "sniper_session.session": ("data", "sniper_session.session"),
    "sniper_session.session-journal": ("data", "sniper_session.session-journal"),
    "live_armed.txt": ("data", "live_armed.txt"),
}
# daily CSVs and the column that holds each row's date
SPLITS = {
    "trades_log.csv": "logged_at",
    "price_paths.csv": "ts",
    "orders_log.csv": "ts",
    "stalls.csv": "froze_from",
}


def say(msg):
    print(("  " if APPLY else "  [dry run] ") + msg)


def main():
    p = lambda *a: os.path.join(ROOT, *a)
    if not os.path.exists(p("sniper_shadow.py")):
        sys.exit("Run this from the bot's folder: python tools/migrate_layout.py")
    print(("APPLYING" if APPLY else "DRY RUN (nothing will change; add --apply to do it)") + f" in {ROOT}\n")
    print("Make sure the bot is CLOSED (Ctrl+C in its window) before applying.\n")

    # 1. memory and logins -> data/
    for old, (folder, new) in MOVES.items():
        src, dst = p(old), p(folder, new)
        if not os.path.exists(src):
            continue
        if os.path.exists(dst):
            sys.exit(f"STOP: {folder}/{new} already exists, and so does {old}. Check them by hand first.")
        say(f"move {old}  ->  {folder}/{new}")
        if APPLY:
            os.makedirs(p(folder), exist_ok=True)
            shutil.move(src, dst)

    # 2. daily CSVs -> runs/<date>/
    for name, col in SPLITS.items():
        src = p(name)
        if not os.path.exists(src):
            continue
        with open(src, encoding="utf-8-sig", newline="") as f:
            rd = csv.reader(f)
            header = next(rd, None)
            if not header or col not in header:
                say(f"skip {name}: columns not recognised (left in place; check it by hand)")
                continue
            i = header.index(col)
            days = {}
            for row in rd:
                if len(row) > i and row[i][:10].count("-") == 2:
                    days.setdefault(row[i][:10], []).append(row)
        total = sum(len(v) for v in days.values())
        say(f"split {name} ({total} rows) into {len(days)} day folder(s): " + ", ".join(sorted(days)))
        if APPLY:
            for day, rows in days.items():
                dst = p("runs", day, name)
                if os.path.exists(dst):
                    sys.exit(f"STOP: {dst} already exists. Check runs/{day} by hand first.")
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with open(dst, "w", newline="", encoding="utf-8-sig") as g:
                    w = csv.writer(g)
                    w.writerow(header)
                    w.writerows(rows)
            os.makedirs(p("legacy"), exist_ok=True)
            shutil.move(src, p("legacy", name))
            say(f"original kept as legacy/{name}")

    # 3. leftovers from older versions
    for fn in sorted(os.listdir(ROOT)):
        if fn.startswith("trades_log_old_") and fn.endswith(".csv"):
            say(f"move {fn}  ->  legacy/{fn}")
            if APPLY:
                os.makedirs(p("legacy"), exist_ok=True)
                shutil.move(p(fn), p("legacy", fn))

    print("\nDone." if APPLY else "\nLooks fine? Run it again with --apply.")
    if APPLY:
        print("Start the bot as usual. Your pot, positions and logins are in data/; today's files are in runs/.")


if __name__ == "__main__":
    main()
