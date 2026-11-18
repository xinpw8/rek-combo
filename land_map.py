#!/usr/bin/env python3
"""Shows where each move really lands, from the clean-hit counter logged during fights.

Each play of a move is one row: where the fighters were when it started, and whether a clean hit
was scored during it. Hits are credited to whatever move the robot was playing when the counter
rose, so a hit that registers just after a cancel can be credited to the next move.

  python land_map.py [HH:MM:SS]    only fights after that time today
"""
import glob
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
import rek_paths  # noqa: E402  (finds the REK install)
SET_JSON = rek_paths.set_json()
NAMES = {i + 1: name.replace("shanghai_", "") for i, name in enumerate(json.loads(SET_JSON.read_text(encoding="utf8"))["moves"])}
SKIP = {"auto_recovery"}
DIST_BANDS = [(0.0, 0.7), (0.7, 0.9), (0.9, 1.1), (1.1, 1.3), (1.3, 1.6), (1.6, 9.0)]
AIM_BANDS = [(0, 20), (20, 40), (40, 60), (60, 90), (90, 181)]


def load(since):
    rows = []
    for path in glob.glob(str(HERE / "logs" / "session-*.jsonl")):
        with open(path, encoding="utf8") as log:
            for line in log:
                row = json.loads(line)
                if row["ev"] in ("state", "hit") and row["t"][11:19] >= since:
                    row["_t"] = datetime.fromisoformat(row["t"]).timestamp()
                    rows.append(row)
    rows.sort(key=lambda r: r["_t"])
    return rows


def plays(rows):
    current = None
    for row in rows:
        if row["ev"] == "hit":
            if current and row["mine"] > 0:
                current["hits"].append((row["_t"] - current["t"], row["dist"], row["aim"]))
            continue
        move, elapsed = row["me"][3], row["me"][4]
        if current is None or move != current["move"] or elapsed < current["last"] - 0.05:
            if current and current["move"]:
                yield current
            current = {"move": move, "t": row["_t"], "dist": row["dist"], "aim": row["aim"], "hits": [], "last": elapsed}
        current["last"] = elapsed
    if current and current["move"]:
        yield current


def band(value, bands):
    return next((b for b in bands if b[0] <= value < b[1]), None)


def main():
    since = sys.argv[1] if len(sys.argv) > 1 else "01:56:40"
    table = defaultdict(list)
    for play in plays(load(since)):
        name = NAMES.get(play["move"], str(play["move"]))
        if name not in SKIP and not name.startswith(("dance", "liyudating")):
            table[name].append(play)
    total_plays = sum(len(v) for v in table.values())
    total_hits = sum(1 for v in table.values() for p in v if p["hits"])
    print(f"since {since}: {total_plays} plays, {total_hits} landed at least one clean hit\n")
    for name, group in sorted(table.items(), key=lambda kv: -len(kv[1])):
        landed = [p for p in group if p["hits"]]
        print(f"{name}: {len(landed)}/{len(group)} landed ({100 * len(landed) / len(group):.0f}%)")
        if landed:
            times = sorted(p["hits"][0][0] for p in landed)
            print(f"   first hit about {times[len(times) // 2]:.2f} s into the move (range {times[0]:.2f}-{times[-1]:.2f})")
        cells = []
        for low, high in DIST_BANDS:
            inside = [p for p in group if low <= p["dist"] < high]
            if inside:
                cells.append(f"{low:.1f}-{high if high < 9 else 'up'}m {sum(1 for p in inside if p['hits'])}/{len(inside)}")
        print("   by distance at start: " + "  ".join(cells))
        cells = []
        for low, high in AIM_BANDS:
            inside = [p for p in group if low <= abs(p["aim"]) < high]
            if inside:
                cells.append(f"{low}-{high if high < 181 else 180}deg {sum(1 for p in inside if p['hits'])}/{len(inside)}")
        print("   by aim at start:      " + "  ".join(cells))


if __name__ == "__main__":
    main()
