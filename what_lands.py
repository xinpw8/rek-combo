#!/usr/bin/env python3
"""What separates a landed kick from a whiff, beyond angle and distance.

Compares plays of a move on: closing speed before it started, whether the opponent was mid-move and
facing, how far the opponent was facing away, and whether the pilot or a macro pressed it.

  python what_lands.py [move name] [YYYY-MM-DDTHH:MM:SS]
"""
import bisect
import glob
import json
import math
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import chain_mine as cm

HERE = Path(__file__).resolve().parent


def wrap(degrees):
    return (degrees + 180.0) % 360.0 - 180.0


def load(since):
    rows = []
    for path in glob.glob(str(HERE / "logs" / "session-*.jsonl")):
        with open(path, encoding="utf8") as log:
            for line in log:
                row = json.loads(line)
                if row["ev"] in ("state", "hit", "fight_end", "press") and row["t"] >= since:
                    row["_t"] = datetime.fromisoformat(row["t"]).timestamp()
                    rows.append(row)
    rows.sort(key=lambda r: r["_t"])
    return rows


def show(title, groups):
    print(title)
    for key in groups:
        n, hits = groups[key]
        if n:
            print(f"   {key:30} {hits:4d}/{n:<4d} {100 * hits / n:3.0f}%")


def main():
    wanted = sys.argv[1] if len(sys.argv) > 1 else "LLK"
    since = sys.argv[2] if len(sys.argv) > 2 else "2026-11-05T01:56:40"
    rows = load(since)
    states = [r for r in rows if r["ev"] == "state" and r["phase"] == 1]
    times = [s["_t"] for s in states]
    presses = [r for r in rows if r["ev"] == "press"]
    press_times = [p["_t"] for p in presses]
    use = [p for p in cm.plays([r for r in rows if r["ev"] != "press"]) if p and p["move"] and cm.name_of(p["move"]) == wanted]

    keys = ("closing", "opp", "facing", "source", "zone_closing", "zone_source", "zone_opp")
    tallies = {k: defaultdict(lambda: [0, 0]) for k in keys}

    def add(table, key, landed):
        tallies[table][key][0] += 1
        tallies[table][key][1] += landed

    usable = 0
    for play in use:
        i = bisect.bisect_left(times, play["t"])
        if i < 4 or i >= len(states):
            continue
        now, before = states[i], states[i - 3]
        dt = now["_t"] - before["_t"]
        if not 0.2 < dt < 0.5:
            continue
        usable += 1
        closing = (before["dist"] - now["dist"]) / dt
        me_x, me_z, them_x, them_z = now["me"][0], now["me"][1], now["them"][0], now["them"][1]
        toward_me = math.degrees(math.atan2(me_x - them_x, me_z - them_z))
        their_aim = abs(wrap(toward_me - now["them"][2] - 45.0))
        landed = bool(play["hits"])
        band = "under 0" if closing < 0 else "0-0.3 m/s" if closing < 0.3 else "0.3-0.6 m/s" if closing < 0.6 else "0.6-1.0 m/s" if closing < 1.0 else "over 1 m/s"
        them_move = cm.name_of(now["them"][3]) if now["them"][3] else "idle/walking"
        opp = "opponent idle/walking" if them_move == "idle/walking" else "opponent recovering" if them_move == "Recovery" else "opponent mid-move"
        facing = "opponent facing me (<45)" if their_aim < 45 else "opponent side-on (45-90)" if their_aim < 90 else "opponent turned away (>90)"
        j = bisect.bisect_right(press_times, play["t"]) - 1
        source = "macro" if j >= 0 and play["t"] - press_times[j] <= 0.35 and presses[j]["move"].replace("shanghai_", "") in cm.SHORT and cm.SHORT[presses[j]["move"].replace("shanghai_", "")] == wanted else "pilot on pad"
        in_zone = abs(play["aim"]) <= 20 and 0.7 <= play["dist"] <= 1.3
        add("closing", band, landed)
        add("opp", opp, landed)
        add("facing", facing, landed)
        add("source", source, landed)
        if in_zone:
            add("zone_closing", band, landed)
            add("zone_source", source, landed)
            add("zone_opp", opp, landed)
    print(f"{wanted}: {usable} usable plays since {since}\n")
    order = ["under 0", "0-0.3 m/s", "0.3-0.6 m/s", "0.6-1.0 m/s", "over 1 m/s"]
    show("All plays, by closing speed before the kick:", {k: tallies["closing"][k] for k in order})
    show("Inside the angle and distance zone, by closing speed:", {k: tallies["zone_closing"][k] for k in order})
    show("All plays, by who pressed it:", tallies["source"])
    show("Inside the zone, by who pressed it:", tallies["zone_source"])
    show("All plays, by what the opponent was doing:", tallies["opp"])
    show("Inside the zone, by what the opponent was doing:", tallies["zone_opp"])
    show("All plays, by which way the opponent faced:", tallies["facing"])

    press_started = [p for p in presses if p["t"] >= since and cm.SHORT.get(p["move"].replace("shanghai_", ""), "") == wanted and p.get("step", 0) == 0]
    starts = 0
    for press in press_started:
        i = bisect.bisect_left(times, press["_t"])
        window = [s for s in states[i:i + 6] if s["_t"] - press["_t"] <= 0.5]
        if any(cm.name_of(s["me"][3]) == wanted and s["me"][4] < 0.5 for s in window if s["me"][3]):
            starts += 1
    print(f"\nmacro opening presses of {wanted}: {len(press_started)}, of which the game started the move {starts}")


if __name__ == "__main__":
    main()
