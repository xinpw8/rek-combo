#!/usr/bin/env python3
"""One row per strike since the kick patch: the setup when it started, whether it landed, and what it cost.

Used by shot_model.py. Also prints how much the enemy's state changes the outcome.

  python shot_data.py [YYYY-MM-DDTHH:MM:SS]    only rows after that moment
"""
import bisect
import glob
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median

import chain_mine as cm

HERE = Path(__file__).resolve().parent
UPDATE = "2026-11-06T22:40:00"  # the first fight after build 26496505, which made kicks uncuttable
BAKED = cm.SET_JSON.parent
GAME = json.loads(cm.SET_JSON.read_text(encoding="utf8"))
PUNCHES = {"LeadStr", "LJab", "RJab", "LHook", "StepHook", "LRPunch", "MuayThai", "ArmSeq"}
NOT_STRIKES = ("Recovery",) + cm.IGNORED
# How long each clip runs when left alone, measured; kicks can't be cut, so for the enemy this is how long it stays locked.
CLIP_S = {"LLK": 1.88, "RLFK": 1.22, "HRSK": 1.32, "RFK": 1.32, "RRFK": 1.5, "BigKick": 3.74, "FlySide": 2.96, "SpinBack": 2.34,
          "StepRH": 3.16, "SevenKick": 3.4, "JumpSpin": 2.5, "SpinJump": 2.5, "LJab": 1.0, "RJab": 2.16, "LHook": 1.31, "LRPunch": 1.56,
          "LeadStr": 1.48, "StepHook": 1.89, "MuayThai": 2.5, "ArmSeq": 3.06, "Recovery": 0.5}


def strike_ends():
    """When each move's last strike window closes, from the game's files: past that, a clip can't hit any more."""
    ends = {}
    for index, name in enumerate(GAME["moves"]):
        path = BAKED / f"{name}.json"
        if path.exists():
            clip = json.loads(path.read_text(encoding="utf8"))
            windows = clip.get("strike_windows") or clip.get("bake", {}).get("strike_windows") or []
            if windows:
                ends[cm.name_of(index + 1)] = (windows[0].get("start_s", 0.0), max(w.get("stop_s", 0.0) for w in windows))
    return ends


STRIKE_ENDS = strike_ends()


def enemy_state(move, elapsed):
    """What the enemy is doing, in terms of what it means for me."""
    if not move:
        return "free"
    name = cm.name_of(move)
    if name == "Recovery":
        return "recovering"
    if name.startswith(cm.IGNORED):
        return "other"
    window = STRIKE_ENDS.get(name)
    if name in PUNCHES:
        return "punch"
    if window and elapsed < window[1]:
        return "kick_active"
    return "kick_spent"  # locked: can't cut it, and it can't hit any more


def load(since=UPDATE):
    rows = cm.load(since)
    pressed = defaultdict(list)
    for path in glob.glob(str(HERE / "logs" / "session-*.jsonl")):
        with open(path, encoding="utf8") as log:
            for line in log:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row["ev"] == "press" and row["t"] >= since:
                    pressed[row["move"].replace("shanghai_", "")].append(datetime.fromisoformat(row["t"]).timestamp())
    for values in pressed.values():
        values.sort()
    states = [r for r in rows if r["ev"] == "state" and r["phase"] == 1 and len(r["me"]) >= 6]
    times = [s["_t"] for s in states]
    ends = sorted((r["_t"], r["mode"]) for r in rows if r["ev"] == "fight_end")
    end_times = [e[0] for e in ends]
    my_hits = sorted(r["_t"] for r in rows if r["ev"] == "hit" and r["mine"] in (1, 4, 5))
    their_points = sorted((r["_t"], r["theirs"]) for r in rows if r["ev"] == "hit" and r["theirs"] in (1, 4, 5))
    their_times = [p[0] for p in their_points]

    out = []
    current = None
    idle_since = None
    previous_t = None
    index = 0
    for i, s in enumerate(states):
        t = s["_t"]
        if previous_t is None or t - previous_t > 3:
            current, idle_since = None, None
        previous_t = t
        move, elapsed = s["me"][3], s["me"][4]
        if not move:
            current = None
            if idle_since is None:
                idle_since = t
            continue
        if current is None or current["move"] != move or elapsed < current["ran"] - 0.05:
            name = cm.name_of(move)
            current = {"move": move, "name": name, "t": t, "ran": elapsed, "end": t, "idle": (t - idle_since) if idle_since else 0.0}
            idle_since = None
            if not name.startswith(NOT_STRIKES):
                j = bisect.bisect_left(times, t - 0.4, 0, i + 1)
                before = states[j]
                span = t - before["_t"]
                closing = (before["dist"] - s["dist"]) / span if 0.15 < span < 0.7 else 0.0
                them_speed = 0.0
                them_toward = 0.0
                if 0.15 < span < 0.7:
                    dx, dz = s["them"][0] - before["them"][0], s["them"][1] - before["them"][1]
                    them_speed = (dx * dx + dz * dz) ** 0.5 / span
                    to_me_x, to_me_z = s["me"][0] - s["them"][0], s["me"][1] - s["them"][1]
                    gap = (to_me_x ** 2 + to_me_z ** 2) ** 0.5 or 1.0
                    them_toward = (dx * to_me_x + dz * to_me_z) / gap / span
                them_name = cm.name_of(s["them"][3]) if s["them"][3] else ""
                row = {"name": name, "t": t, "aim": s["aim"], "dist": s["dist"], "closing": closing, "idle": current["idle"], "them_toward": them_toward, "them_speed": them_speed,
                       "them_state": enemy_state(s["them"][3], s["them"][4]), "them_name": them_name, "them_elapsed": s["them"][4], "them_left": max(0.0, CLIP_S.get(them_name, 0.0) - s["them"][4]) if them_name else 0.0,
                       "them_tilt": s["them"][5], "my_tilt": s["me"][5], "macro": False, "hit": False, "against": 0, "against_miss": 0, "ran": 0.0}
                raw = cm.RAW.get(move, "")
                presses = pressed.get(raw, [])
                k = bisect.bisect_left(presses, t - 0.7)
                row["macro"] = k < len(presses) and presses[k] <= t
                e = bisect.bisect_left(end_times, t)
                row["mode"] = ends[e][1] if e < len(ends) else "running"
                row["human"] = row["mode"].endswith("_mp") or row["mode"] == "mp"
                row["_clip"] = current
                out.append(row)
        current["ran"], current["end"] = elapsed, t
    for row in out:
        clip = row.pop("_clip")
        row["ran"] = clip["ran"]
        row["hit"] = bisect.bisect_left(my_hits, row["t"] - 0.05) < len(my_hits) and my_hits[bisect.bisect_left(my_hits, row["t"] - 0.05)] <= clip["end"] + 0.25
        low, high = bisect.bisect_left(their_times, row["t"]), bisect.bisect_right(their_times, clip["end"] + 0.25)
        row["against"] = sum(p[1] for p in their_points[low:high])
        row["span"] = clip["end"] - row["t"] + 0.1
    return out


def rate(rows):
    n = len(rows)
    return f"{sum(r['hit'] for r in rows)}/{n} {100 * sum(r['hit'] for r in rows) / n:.0f}%" if n else "-"


def main():
    since = sys.argv[1] if len(sys.argv) > 1 else UPDATE
    data = load(since)
    kicks = [r for r in data if r["name"] not in PUNCHES]
    print(f"{len(data)} strikes since {since}, {sum(r['hit'] for r in data)} landed; humans {sum(r['human'] for r in data)}; macro {sum(r['macro'] for r in data)}")
    print("\nWhat the enemy was doing when my strike started. Kicks only:")
    print(f"{'':14}{'n':>5}{'all':>12}{'lined up':>12}{'macro':>12}{'human':>12}{'points against per kick':>26}")
    for state in ("free", "punch", "kick_active", "kick_spent", "recovering", "other"):
        rows = [r for r in kicks if r["them_state"] == state]
        lined = [r for r in rows if -15 <= r["aim"] <= 35 and r["dist"] <= 1.7]
        points = sum(r["against"] for r in rows) / len(rows) if rows else 0.0
        print(f"{state:14}{len(rows):>5}{rate(rows):>12}{rate(lined):>12}{rate([r for r in rows if r['macro']]):>12}{rate([r for r in rows if r['human']]):>12}{points:>26.2f}")

    print("\nEnemy locked in a kick, by how long it has left, lined up and in reach:")
    for low, high in ((0, 0.4), (0.4, 0.8), (0.8, 1.5), (1.5, 9)):
        rows = [r for r in kicks if r["them_state"] in ("kick_active", "kick_spent") and low <= r["them_left"] < high and -15 <= r["aim"] <= 35 and r["dist"] <= 1.7]
        print(f"  {low}-{high} s left: {rate(rows):>12}, points against per kick {sum(r['against'] for r in rows) / len(rows) if rows else 0:.2f}")

    print("\nEnemy movement, lined up and in reach, enemy not in a clip:")
    for low, high in ((-9, -0.3), (-0.3, -0.1), (-0.1, 0.1), (0.1, 0.3), (0.3, 0.6), (0.6, 9)):
        rows = [r for r in kicks if r["them_state"] == "free" and low <= r["them_toward"] < high and -15 <= r["aim"] <= 35 and r["dist"] <= 1.7]
        print(f"  coming toward me {low}..{high} m/s: {rate(rows):>12}")

    print("\nThe cost of a kick: points against during its clip, by whether it landed, and points per second of lock.")
    print(f"{'':11}{'n':>5}{'land':>6}{'against if it hit':>19}{'against if it missed':>22}{'median lock':>12}{'net per throw':>15}")
    for name in ("LLK", "HRSK", "RLFK", "RFK", "RRFK", "FlySide", "BigKick", "SevenKick", "SpinBack", "StepRH", "SpinJump", "JumpSpin"):
        rows = [r for r in data if r["name"] == name]
        if len(rows) < 8:
            continue
        hit, miss = [r for r in rows if r["hit"]], [r for r in rows if not r["hit"]]
        net = (sum(r["hit"] for r in rows) - sum(r["against"] for r in rows)) / len(rows)
        print(f"{name:11}{len(rows):>5}{100 * len(hit) / len(rows):>5.0f}%{sum(r['against'] for r in hit) / len(hit) if hit else 0:>19.2f}{sum(r['against'] for r in miss) / len(miss) if miss else 0:>22.2f}{median(r['ran'] for r in rows):>11.2f}s{net:>+15.2f}")
    punches = [r for r in data if r["name"] in PUNCHES]
    if punches:
        print(f"{'punches':11}{len(punches):>5}{100 * sum(r['hit'] for r in punches) / len(punches):>5.0f}%{'':19}{sum(r['against'] for r in punches if not r['hit']) / max(1, sum(not r['hit'] for r in punches)):>22.2f}{median(r['ran'] for r in punches):>11.2f}s{(sum(r['hit'] for r in punches) - sum(r['against'] for r in punches)) / len(punches):>+15.2f}")


if __name__ == "__main__":
    main()
