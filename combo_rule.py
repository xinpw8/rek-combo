#!/usr/bin/env python3
"""Works out what really earns a combo, from the score jumps in the session logs.

The live score rises by 1 for a clean hit, 3 when the other robot slips, 4 for COMBO X2 and 5 for a
knockdown (checked against the on-screen referee cards). This lists what came before every combo,
tests candidate rules against hits that did not combo, and shows where each side's points came from.

  python combo_rule.py [YYYY-MM-DDTHH:MM:SS]
"""
import bisect
import glob
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import chain_mine as cm

HERE = Path(__file__).resolve().parent
KIND = {1: "hit", 3: "slip", 4: "COMBO", 5: "KNOCKDOWN"}


def load(since):
    rows = []
    for path in sorted(glob.glob(str(HERE / "logs" / "session-*.jsonl"))):
        with open(path, encoding="utf8") as log:
            for line in log:
                row = json.loads(line)
                if row["ev"] in ("hit", "state") and row["t"] >= since:
                    row["_t"] = datetime.fromisoformat(row["t"]).timestamp()
                    rows.append(row)
    rows.sort(key=lambda r: r["_t"])
    return rows


def events_for(hits, side):
    """Score events for one side: (time, jump, move playing, round, distance, aim, row)."""
    index = 3
    out = []
    for row in hits:
        jump = row["mine"] if side == "mine" else row["theirs"]
        if jump in KIND:
            robot = row["me"] if side == "mine" else row["them"]
            out.append({"t": row["_t"], "jump": jump, "move": cm.name_of(robot[index]) if robot[index] else "none",
                        "round": row["round"], "dist": row["dist"], "aim": row["aim"], "stamp": row["t"][5:19]})
    return out


def starts(states, side):
    """When each move started for one side, from the state rows."""
    out = []
    previous = 0
    for row in states:
        robot = row["me"] if side == "mine" else row["them"]
        if robot[3] != previous:
            if robot[3]:
                out.append((row["_t"], cm.name_of(robot[3])))
            previous = robot[3]
    return out


def report(side, events, moves):
    combos = [e for e in events if e["jump"] == 4]
    print(f"\n===== {side}: {len(combos)} combos =====")
    move_times = [t for t, _ in moves]
    prior_counts = Counter()
    span, last_gap = [], []
    finishers, sets = Counter(), Counter()
    for combo in combos:
        before = [e for e in events if combo["t"] - 8.0 <= e["t"] < combo["t"] and e["round"] == combo["round"] and e["jump"] in (1, 4, 5)]
        in_window = [e for e in before if combo["t"] - e["t"] <= 4.0]
        prior_counts[len(in_window)] += 1
        finishers[combo["move"]] += 1
        if in_window:
            span.append(combo["t"] - in_window[0]["t"])
            last_gap.append(combo["t"] - in_window[-1]["t"])
        sets[" + ".join(sorted({e["move"] for e in in_window} | {combo["move"]}))] += 1
        played = moves[bisect.bisect_left(move_times, combo["t"] - 5.0):bisect.bisect_right(move_times, combo["t"])]
        text = " ".join(f"{e['t'] - combo['t']:+.1f}:{e['move']}" + (f"(+{e['jump']})" if e["jump"] != 1 else "") for e in before)
        played_text = " ".join(f"{t - combo['t']:+.1f}:{name}" for t, name in played)
        print(f"  {combo['stamp']} d{combo['dist']} a{combo['aim']:+.0f} [{combo['move']}] scored before: {text or 'none'}\n      moves started: {played_text}")
    print("\nhow many scoring hits came in the 4 s before the combo:", sorted(prior_counts.items()))
    if span:
        print(f"first of those to the combo: median {statistics.median(span):.2f} s, longest {max(span):.2f} s; last one to the combo: median {statistics.median(last_gap):.2f} s, longest {max(last_gap):.2f} s")
    print("move playing when the combo scored:", finishers.most_common(8))
    print("sets of moves involved:", sets.most_common(10))
    return combos


def test_rules(events):
    """How well does each candidate rule separate hits that combo from hits that don't?"""
    print("\nCandidate rules, tested on every scoring hit:")
    scoring = [e for e in events if e["jump"] in (1, 4)]
    def rule_counts(window, need, distinct):
        fired_combo = fired_plain = missed_combo = 0
        for i, event in enumerate(scoring):
            before = [e for e in scoring[max(0, i - 12):i] if event["t"] - e["t"] <= window and e["round"] == event["round"]]
            # A combo resets the count, so hits from before the last combo don't carry over.
            last_combo = max((j for j, e in enumerate(before) if e["jump"] == 4), default=-1)
            before = before[last_combo + 1:]
            if distinct:
                count = len({e["move"] for e in before} - {event["move"]})
            else:
                count = len(before)
            predicted = count >= need
            if predicted and event["jump"] == 4:
                fired_combo += 1
            elif predicted:
                fired_plain += 1
            elif event["jump"] == 4:
                missed_combo += 1
        return fired_combo, fired_plain, missed_combo
    for window in (2.0, 3.0, 4.0, 5.0, 6.0):
        for need, distinct, label in ((1, False, "1 earlier hit"), (2, False, "2 earlier hits"), (1, True, "1 other move"), (2, True, "2 other moves")):
            hit, false_alarm, missed = rule_counts(window, need, distinct)
            print(f"  within {window:.0f} s, {label:14}: explains {hit:3d} combos, misses {missed:3d}, and predicts {false_alarm:3d} that never happened")


def points_table(hits):
    print("\nWhere the points came from:")
    tally = {"mine": Counter(), "theirs": Counter()}
    other = {"mine": Counter(), "theirs": Counter()}
    for row in hits:
        for side in ("mine", "theirs"):
            jump = row[side]
            if jump in KIND:
                tally[side][KIND[jump]] += 1
            elif jump > 0:
                other[side][jump] += 1
    for side in ("mine", "theirs"):
        total = sum(count * jump for jump, name in KIND.items() for count in [tally[side][name]])
        parts = ", ".join(f"{name} {tally[side][name]} x{jump} = {tally[side][name] * jump}" for jump, name in KIND.items())
        print(f"  {side:7} {total:5d} points: {parts}" + (f" | unexplained jumps {dict(other[side])}" if other[side] else ""))


def slips_and_falls(hits, states):
    print("\nMy move when I gave up points by slipping or being knocked down:")
    times = [s["_t"] for s in states]
    for jump, label in ((3, "slip"), (5, "knockdown")):
        current = Counter()
        recent = Counter()
        for row in hits:
            if row["theirs"] != jump:
                continue
            current[cm.name_of(row["me"][3]) if row["me"][3] else "none"] += 1
            i = bisect.bisect_left(times, row["_t"])
            names = []
            for s in states[max(0, i - 25):i]:
                name = cm.name_of(s["me"][3]) if s["me"][3] else None
                if name and name != "Recovery" and (not names or names[-1] != name):
                    names.append(name)
            recent[names[-1] if names else "none"] += 1
        print(f"  {label:10} playing {current.most_common(6)}\n             last strike in the 2.5 s before {recent.most_common(8)}")


def main():
    since = sys.argv[1] if len(sys.argv) > 1 else "2026-11-05T01:56:40"
    rows = load(since)
    hits = [r for r in rows if r["ev"] == "hit"]
    states = [r for r in rows if r["ev"] == "state" and r["phase"] == 1]
    mine = events_for(hits, "mine")
    theirs = events_for(hits, "theirs")
    report("mine", mine, starts(states, "mine"))
    test_rules(mine)
    report("theirs", theirs, starts(states, "theirs"))
    points_table(hits)
    slips_and_falls(hits, states)


if __name__ == "__main__":
    main()
