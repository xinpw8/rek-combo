#!/usr/bin/env python3
"""Land rate of each move by the angle and distance it started from idle. Also prints the rates when cutting another clip.

Writes land_table.json, which the macro uses to choose the next move. Rates are shrunk toward the
move's overall rate, so a bin with three plays does not look certain.

  python land_table.py [YYYY-MM-DDTHH:MM:SS]    only rows after that moment
"""
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import chain_mine as cm

HERE = Path(__file__).resolve().parent
AIM_BINS = [(-181, -90), (-90, -40), (-40, -20), (-20, 20), (20, 40), (40, 90), (90, 181)]
DIST_BINS = [(0.0, 0.7), (0.7, 1.0), (1.0, 1.3), (1.3, 1.7), (1.7, 9.0)]
PRIOR_PLAYS = 6.0
IDLE_S = 0.25  # a strike after at least this long with no clip playing counts as started from idle
STARTS = ("idle", "cut")
RAW_NAME = {cm.SHORT[raw]: raw for raw in cm.SHORT}


def label(bin_range):
    return f"{bin_range[0]}..{bin_range[1]}"


def find(value, bins):
    return next(b for b in bins if b[0] <= value < b[1])


def main():
    since = sys.argv[1] if len(sys.argv) > 1 else "2026-11-05T01:56:40"
    all_plays = cm.plays(cm.load(since))
    for before, play in zip([None] + all_plays, all_plays):  # idle spells are plays of move 0, so this is how long it idled first
        if play:
            play["idle"] = play["t"] - before["t"] if before and not before["move"] else 0.0
    sequence = [p for p in all_plays if p is None or (p["move"] and not cm.name_of(p["move"]).startswith(cm.IGNORED))]

    overall = defaultdict(lambda: [0, 0])
    by_start, by_aim, by_dist = defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0])
    joint = defaultdict(lambda: [0, 0])
    for play in sequence:
        if play is None:
            continue
        name = cm.name_of(play["move"])
        if name == "Recovery":
            continue
        landed = bool(play["hits"])
        aim, dist = find(play["aim"], AIM_BINS), find(play["dist"], DIST_BINS)
        start = "idle" if play["idle"] >= IDLE_S else "cut"
        overall[name][0] += 1
        overall[name][1] += landed
        for tally, key in ((by_start, (start, name)), (by_aim, (start, name, aim)), (by_dist, (start, name, dist)), (joint, (start, name, aim, dist))):
            tally[key][0] += 1
            tally[key][1] += landed

    names = [n for n, (plays_n, _) in sorted(overall.items(), key=lambda kv: -kv[1][1]) if plays_n >= 15]
    for start in STARTS:
        print(f"Started {'from idle' if start == 'idle' else 'cutting a clip'}, by aim, negative = opponent to the left (landed/plays):")
        print(f"{'':11}{''.join(f'{label(a):>12}' for a in AIM_BINS)}")
        for name in names:
            cells = "".join(f"{by_aim[(start, name, a)][1]:>7}/{by_aim[(start, name, a)][0]:<4}" for a in AIM_BINS)
            print(f"{name:11}{cells}")
        print("\nAnd by distance:")
        print(f"{'':11}{''.join(f'{label(d):>12}' for d in DIST_BINS)}")
        for name in names:
            cells = "".join(f"{by_dist[(start, name, d)][1]:>7}/{by_dist[(start, name, d)][0]:<4}" for d in DIST_BINS)
            print(f"{name:11}{cells}")
        print()

    # How long after a press a hit from this move has shown up, so a chain knows when to stop waiting.
    hit_times = defaultdict(list)
    for play in sequence:
        if play and play["hits"]:
            hit_times[cm.name_of(play["move"])].append(play["hits"][0] - play["t"])

    # How soon the same move has really started again after itself.
    repeat_gaps = defaultdict(list)
    last_start = {}
    for play in sequence:
        if play is None:
            last_start = {}
            continue
        name = cm.name_of(play["move"])
        if name in last_start and play["t"] - last_start[name] < 8.0:
            repeat_gaps[name].append(play["t"] - last_start[name])
        last_start[name] = play["t"]
    print("\nSoonest the same move restarted after itself (smallest, 5th percentile, n):")
    for name in names[:8]:
        gaps = sorted(repeat_gaps[name])
        if gaps:
            print(f"  {name:11} {gaps[0]:.2f} {gaps[len(gaps) // 20]:.2f}  n={len(gaps)}")

    # How long each clip runs when nothing cuts it short, so a chain knows when Recovery isn't worth trying.
    natural_ends = defaultdict(list)
    for play, following in zip(all_plays, all_plays[1:]):
        if play and following and play["move"] and not following["move"]:
            natural_ends[cm.name_of(play["move"])].append(play["last"])

    def cells_for(start, name, whole_move):
        plays_n, hits = by_start[(start, name)]
        base = (hits + PRIOR_PLAYS * whole_move) / (plays_n + PRIOR_PLAYS)
        cells = {}
        for aim in AIM_BINS:
            for dist in DIST_BINS:
                plays_n, hits = joint[(start, name, aim, dist)]
                a_plays, a_hits = by_aim[(start, name, aim)]
                d_plays, d_hits = by_dist[(start, name, dist)]
                # Back off from the exact cell to the separate aim and distance rates, then the whole move.
                aim_rate = (a_hits + PRIOR_PLAYS * base) / (a_plays + PRIOR_PLAYS)
                dist_rate = (d_hits + PRIOR_PLAYS * base) / (d_plays + PRIOR_PLAYS)
                blended = min(1.0, aim_rate * dist_rate / base) if base else 0.0
                cells[f"{label(aim)}|{label(dist)}"] = round((hits + PRIOR_PLAYS * blended) / (plays_n + PRIOR_PLAYS), 3)
        return cells

    table = {}
    for name in names:
        base = overall[name][1] / overall[name][0]
        times = sorted(hit_times[name])
        hit_by = times[min(len(times) - 1, int(len(times) * 0.9))] if len(times) >= 5 else 0.75
        # A clip can also stop early when the robot is hit or falls, so take the long end of these, not the middle.
        ended = sorted(natural_ends[name])
        ends = ended[int(len(ended) * 0.9)] if len(ended) >= 3 else None
        table[RAW_NAME.get(name, name)] = {"overall": round(base, 3), "plays": overall[name][0], "cells": cells_for("idle", name, base),
                                           "hit_by_s": round(hit_by, 2), "ends_s": ends}
    (HERE / "land_table.json").write_text(json.dumps({"aim_bins": AIM_BINS, "dist_bins": DIST_BINS, "moves": table}, indent=0), encoding="utf8")
    print(f"\nwrote land_table.json with {len(table)} moves")

    print("\nThree different moves landing within 4 s:")
    landed = [p for p in sequence if p and p["hits"] and cm.name_of(p["move"]) != "Recovery"]
    triples = Counter()
    for i, first in enumerate(landed):
        chain = [first]
        for later in landed[i + 1:]:
            if later["hits"][0] - first["hits"][0] > 4.0:
                break
            if cm.name_of(later["move"]) not in [cm.name_of(c["move"]) for c in chain]:
                chain.append(later)
        if len(chain) >= 3:
            gaps = [round(chain[j + 1]["t"] - chain[j]["t"], 2) for j in range(2)]
            aims = [round(chain[j]["aim"]) for j in range(3)]
            triples[(" > ".join(cm.name_of(c["move"]) for c in chain[:3]), str(gaps), str(aims))] += 1
    for (moves, gaps, aims), count in triples.most_common(12):
        print(f"  {count}x {moves:30} gaps {gaps:14} aim at each start {aims}")


if __name__ == "__main__":
    main()
