#!/usr/bin/env python3
"""Who hits whom, per second, in each situation: what wins exchanges and what loses them.

  python exchange_report.py [HH:MM:SS]    only fights after that time today
"""
import glob
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
import rek_paths  # noqa: E402  (finds the REK install)
SET_JSON = rek_paths.set_json()
NAMES = {i + 1: name.replace("shanghai_", "") for i, name in enumerate(json.loads(SET_JSON.read_text(encoding="utf8"))["moves"])}
STATE_S = 0.1  # state rows are logged ten times a second


def load(since):
    rows = []
    for path in glob.glob(str(HERE / "logs" / "session-*.jsonl")):
        with open(path, encoding="utf8") as log:
            for line in log:
                row = json.loads(line)
                if row["t"][11:19] >= since:
                    row["_t"] = datetime.fromisoformat(row["t"]).timestamp()
                    rows.append(row)
    rows.sort(key=lambda r: r["_t"])
    return rows


def situation(row):
    move = NAMES.get(row["me"][3], str(row["me"][3])) if row["me"][3] else None
    if move is None:
        return "free, facing them" if abs(row["aim"]) <= 35 else "free, turned away"
    if move == "auto_recovery":
        return "recovering"
    return "in a move, facing them" if abs(row["aim"]) <= 35 else "in a move, turned away"


def distance_band(distance):
    if distance < 0.7:
        return "under 0.7 m"
    if distance < 1.1:
        return "0.7-1.1 m"
    if distance < 1.6:
        return "1.1-1.6 m"
    return "over 1.6 m"


def table(title, seconds, mine, theirs):
    print(title)
    print(f"  {'':26}{'seconds':>8}{'my hits':>9}{'their hits':>12}{'my/s':>7}{'their/s':>9}")
    for key in sorted(seconds, key=lambda k: -seconds[k]):
        s = seconds[key]
        print(f"  {key:26}{s:8.0f}{mine[key]:9d}{theirs[key]:12d}{mine[key] / s if s else 0:7.2f}{theirs[key] / s if s else 0:9.2f}")
    print()


def main():
    since = sys.argv[1] if len(sys.argv) > 1 else "01:56:40"
    rows = load(since)
    state_rows = [r for r in rows if r["ev"] == "state" and r["phase"] == 1]
    hit_rows = [r for r in rows if r["ev"] == "hit"]
    seconds, by_distance = Counter(), Counter()
    for row in state_rows:
        seconds[situation(row)] += STATE_S
        by_distance[distance_band(row["dist"])] += STATE_S
    mine, theirs, mine_d, theirs_d = Counter(), Counter(), Counter(), Counter()
    my_moves, their_moves = Counter(), Counter()
    for hit in hit_rows:
        key, band = situation(hit), distance_band(hit["dist"])
        if hit["mine"] > 0:
            mine[key] += hit["mine"]
            mine_d[band] += hit["mine"]
            my_moves[NAMES.get(hit["me"][3], "none")] += hit["mine"]
        if hit["theirs"] > 0:
            theirs[key] += hit["theirs"]
            theirs_d[band] += hit["theirs"]
            their_moves[NAMES.get(hit["them"][3], "none")] += hit["theirs"]
    total = sum(seconds.values())
    print(f"since {since}: {total:.0f} s of rounds, my clean hits {sum(mine.values())}, theirs {sum(theirs.values())}\n")
    table("By what my robot was doing:", seconds, mine, theirs)
    table("By distance:", by_distance, mine_d, theirs_d)
    print("Move playing when I scored:   ", my_moves.most_common(8))
    print("Move playing when they scored:", their_moves.most_common(8))


if __name__ == "__main__":
    main()
