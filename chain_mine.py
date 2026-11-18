#!/usr/bin/env python3
"""Finds the move sequences that really land several hits, from the logged moves and clean hits.

For each pair of consecutive moves it reports how often the second landed, split by whether the
first landed, and by the gap between them. It also lists actual chains of clean hits.

  python chain_mine.py [YYYY-MM-DDTHH:MM:SS]    only rows after that moment
"""
import glob
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
import rek_paths  # noqa: E402  (finds the REK install)
SET_JSON = rek_paths.set_json()
RAW = {i + 1: name.replace("shanghai_", "") for i, name in enumerate(json.loads(SET_JSON.read_text(encoding="utf8"))["moves"])}
SHORT = {"leg2": "LLK", "leg16": "RFK", "leg19": "HRSK", "leg29": "RLFK", "leg30": "RRFK", "leg32": "StepRH", "leg31": "JumpSpin",
         "leg18": "SpinJump", "arm3": "LRPunch", "arm11": "MuayThai", "arm34": "ArmSeq", "arm33": "RJab", "arm32": "LJab",
         "arm31": "LeadStr", "arm20": "LHook", "arm27": "StepHook", "flying_side_kick": "FlySide",
         "seven_kick_combo": "SevenKick", "spinning_back_kick": "SpinBack", "big_kick": "BigKick", "auto_recovery": "Recovery"}
IGNORED = ("dance", "liyudating", "tiaoxin", "b121", "b122", "stance")


def name_of(move):
    raw = RAW.get(move, str(move))
    return SHORT.get(raw, raw)


def load(since):
    rows = []
    for path in glob.glob(str(HERE / "logs" / "session-*.jsonl")):
        with open(path, encoding="utf8") as log:
            for line in log:
                row = json.loads(line)
                if row["ev"] in ("state", "hit", "fight_end") and row["t"] >= since:
                    row["_t"] = datetime.fromisoformat(row["t"]).timestamp()
                    rows.append(row)
    rows.sort(key=lambda r: r["_t"])
    return rows


def plays(rows):
    """One record per play of a move, in order, with a break marker across fights and long gaps."""
    out = []
    current = None
    for row in rows:
        if row["ev"] == "fight_end":
            current = None
            out.append(None)
            continue
        if row["ev"] == "hit":
            if current is not None and row["mine"] in (1, 4, 5):  # +3 is the other robot slipping, not a hit
                current["hits"].append(row["_t"])
            continue
        if row["phase"] != 1:
            continue
        move, elapsed = row["me"][3], row["me"][4]
        if current is None or move != current["move"] or elapsed < current["last"] - 0.05:
            current = {"move": move, "t": row["_t"], "dist": row["dist"], "aim": row["aim"], "last": elapsed,
                       "end": row["_t"], "hits": [], "them_move": row["them"][3]}
            out.append(current)
        current["last"] = elapsed
        current["end"] = row["_t"]
    return out


def main():
    since = sys.argv[1] if len(sys.argv) > 1 else "2026-11-05T01:56:40"
    all_plays = plays(load(since))
    moves = [p for p in all_plays if p and p["move"] and not name_of(p["move"]).startswith(IGNORED)]
    print(f"since {since}: {len(moves)} plays, {sum(1 for p in moves if p['hits'])} landed\n")

    solo = defaultdict(lambda: [0, 0])
    for p in moves:
        solo[name_of(p["move"])][0] += 1
        solo[name_of(p["move"])][1] += bool(p["hits"])
    print("Land rate by move:")
    for name, (n, hit) in sorted(solo.items(), key=lambda kv: -kv[1][0])[:14]:
        print(f"  {name:12} {hit:3d}/{n:<4d} {100 * hit / n:3.0f}%")

    # Walk the sequence of real moves, skipping idle time up to the combo window.
    sequence = []
    for p in all_plays:
        if p is None:
            sequence.append(None)
        elif p["move"] and not name_of(p["move"]).startswith(IGNORED):
            sequence.append(p)
    pairs = defaultdict(list)
    for a, b in zip(sequence, sequence[1:]):
        if a is None or b is None or name_of(a["move"]) == "Recovery" or name_of(b["move"]) == "Recovery":
            continue
        gap = b["t"] - a["t"]
        if gap <= 2.5:
            pairs[(name_of(a["move"]), name_of(b["move"]))].append((bool(a["hits"]), bool(b["hits"]), gap, b["dist"], b["aim"]))
    print("\nSecond move lands, after the first landed (pairs with at least 3 such cases):")
    rows = []
    for (a, b), cases in pairs.items():
        after_hit = [c for c in cases if c[0]]
        if len(after_hit) >= 3:
            landed = sum(c[1] for c in after_hit)
            rows.append((landed / len(after_hit), landed, len(after_hit), a, b, statistics.median(c[2] for c in after_hit if c[1]) if landed else None,
                         sum(c[1] for c in cases if not c[0]), len(cases) - len(after_hit)))
    for rate, landed, n, a, b, gap, miss_land, miss_n in sorted(rows, key=lambda r: (-r[1], -r[0])):
        gap_text = f"{gap:.2f}s" if gap else "  -  "
        print(f"  {a:9} > {b:9} {landed:3d}/{n:<3d} {100 * rate:3.0f}%  median gap when it landed {gap_text} | after a miss {miss_land}/{miss_n}")

    print("\nAfter any landed hit, did the next move land, by gap between the moves:")
    bands = [(0, 0.7), (0.7, 0.9), (0.9, 1.1), (1.1, 1.4), (1.4, 2.0), (2.0, 2.5)]
    for low, high in bands:
        cases = [c for v in pairs.values() for c in v if c[0] and low <= c[2] < high]
        if cases:
            print(f"  {low:.1f}-{high:.1f}s  {sum(c[1] for c in cases):3d}/{len(cases):<4d} {100 * sum(c[1] for c in cases) / len(cases):3.0f}%")
    print("\nAfter a landed hit, next move by distance and aim when it started:")
    follow = [c for v in pairs.values() for c in v if c[0]]
    for low, high in ((0, 0.7), (0.7, 0.9), (0.9, 1.1), (1.1, 1.3), (1.3, 1.6), (1.6, 9)):
        cases = [c for c in follow if low <= c[3] < high]
        if cases:
            print(f"  {low:.1f}-{high:.1f} m   {sum(c[1] for c in cases):3d}/{len(cases):<4d} {100 * sum(c[1] for c in cases) / len(cases):3.0f}%")
    for low, high in ((0, 20), (20, 40), (40, 60), (60, 90), (90, 181)):
        cases = [c for c in follow if low <= abs(c[4]) < high]
        if cases:
            print(f"  {low:3d}-{high:<3d} deg  {sum(c[1] for c in cases):3d}/{len(cases):<4d} {100 * sum(c[1] for c in cases) / len(cases):3.0f}%")

    # Real runs of clean hits from different moves inside the 4 second combo window.
    runs = Counter()
    examples = {}
    landed = [p for p in sequence if p and p["hits"] and name_of(p["move"]) != "Recovery"]
    for i, first in enumerate(landed):
        chain = [first]
        for later in landed[i + 1:]:
            if later["hits"][0] - first["hits"][0] > 4.0:
                break
            if name_of(later["move"]) not in [name_of(c["move"]) for c in chain]:
                chain.append(later)
        if len(chain) >= 2:
            key = tuple(name_of(c["move"]) for c in chain[:3])
            runs[key] += 1
            examples.setdefault(key, [round(chain[j + 1]["t"] - chain[j]["t"], 2) for j in range(len(chain[:3]) - 1)])
    print("\nDifferent moves landing within 4 s (two and three hit runs), with an example of the gaps:")
    for key, count in runs.most_common(14):
        print(f"  {count:3d}x  {' > '.join(key):34} gaps {examples[key]}")
    triples = sum(count for key, count in runs.items() if len(key) == 3)
    print(f"\nthree-move runs: {triples}; two-move runs: {sum(runs.values()) - triples}")


if __name__ == "__main__":
    main()
