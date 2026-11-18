#!/usr/bin/env python3
"""Measures how far each move really turns and travels, from recorded fights.

Reads the state rows in logs/session-*.jsonl and writes turn_table.json: for each move,
the median turn (degrees, positive to the right) and travel (metres, forward and right of
the heading the move started from) at each tenth of a second into the move.
"""
import glob
import json
import math
import os
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
import rek_paths  # noqa: E402  (finds the REK install)
SET_JSON = rek_paths.set_json()
MOVES = json.loads(SET_JSON.read_text(encoding="utf8"))["moves"]
NAMES = {i + 1: name for i, name in enumerate(MOVES)}
BIN_S = 0.1
MIN_SAMPLES = 4


def clip_start_s(name):
    """The feed's clock counts from the clip's first authored frame, not from where the move starts playing."""
    path = SET_JSON.with_name(name + ".json")
    return json.loads(path.read_text(encoding="utf8")).get("start_frame", 0) / 100.0 if path.exists() else 0.0


START_S = {move: clip_start_s(name) for move, name in NAMES.items()}


def wrap(degrees):
    return (degrees + 180.0) % 360.0 - 180.0


def load_states():
    rows = []
    for path in glob.glob(str(HERE / "logs" / "session-*.jsonl")):
        with open(path, encoding="utf8") as log:
            for line in log:
                row = json.loads(line)
                if row["ev"] == "state" and row["phase"] == 1:
                    row["_t"] = datetime.fromisoformat(row["t"]).timestamp()
                    rows.append(row)
    rows.sort(key=lambda r: r["_t"])
    return rows


def instances(rows):
    """Splits the log into single plays of a move, each one seen from close to its start."""
    current = None
    for row in rows:
        move, elapsed = row["me"][3], row["me"][4]
        fresh = current is None or move != current["move"] or elapsed < current["last"] - 0.05 \
            or row["_t"] - current["last_t"] > 0.4
        if fresh:
            if current and current["move"]:
                yield current
            current = {"move": move, "samples": [], "last": elapsed, "last_t": row["_t"],
                       "x": row["me"][0], "z": row["me"][1], "heading": row["me"][2],
                       "first_elapsed": elapsed, "turned": 0.0, "previous": row["me"][2]}
        current["turned"] += wrap(row["me"][2] - current["previous"])
        current["previous"] = row["me"][2]
        current["last"], current["last_t"] = elapsed, row["_t"]
        current["samples"].append((elapsed, current["turned"], row["me"][0] - current["x"], row["me"][1] - current["z"]))
    if current and current["move"]:
        yield current


def build():
    turned, forward, right = defaultdict(list), defaultdict(list), defaultdict(list)
    plays = defaultdict(int)
    for play in instances(load_states()):
        offset = START_S.get(play["move"], 0.0)
        if play["first_elapsed"] - offset > 0.15:  # the start of this play was not seen, so its turn is unknown
            continue
        plays[play["move"]] += 1
        h = math.radians(play["heading"])
        for elapsed, turn, dx, dz in play["samples"]:
            key = (play["move"], round(round((elapsed - offset) / BIN_S) * BIN_S, 1))
            turned[key].append(turn)
            forward[key].append(dx * math.sin(h) + dz * math.cos(h))
            right[key].append(dx * math.cos(h) - dz * math.sin(h))
    table = {}
    for (move, at), values in turned.items():
        if len(values) >= MIN_SAMPLES:
            entry = table.setdefault(NAMES.get(move, str(move)), {"plays": plays[move], "curve": {}})
            entry["curve"][f"{at:.1f}"] = [round(statistics.median(values), 1),
                                           round(statistics.median(forward[(move, at)]), 2),
                                           round(statistics.median(right[(move, at)]), 2), len(values)]
    return table


def main():
    table = build()
    (HERE / "turn_table.json").write_text(json.dumps(table, indent=1), encoding="utf8")
    print("turn at time into the move (deg, + right), then forward metres at that time")
    for name, entry in sorted(table.items(), key=lambda kv: -kv[1]["plays"]):
        cells = [f"{at}:{row[0]:+.0f}/{row[1]:+.2f}" for at, row in sorted(entry["curve"].items(), key=lambda kv: float(kv[0]))
                 if 0.4 <= float(at) <= 1.5]
        print(f"{name.replace('shanghai_', ''):20} plays {entry['plays']:3d} | " + " ".join(cells))


if __name__ == "__main__":
    main()
