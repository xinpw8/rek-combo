#!/usr/bin/env python3
"""Tests the rule 'N different moves, each hit within G seconds of the previous one' against every hit."""
import sys
from collections import Counter

import combo_rule as cr


def score(events, opponent_times, gap, need, distinct, reset_on_taken, count_knockdown):
    """Walks one side's scoring events and compares the rule's prediction with what the game awarded."""
    hit = false_alarm = missed = 0
    chain, last_t, last_round = [], None, None
    kinds = (1, 4, 5) if count_knockdown else (1, 4)
    examples = []
    for event in events:
        if event["jump"] not in kinds:
            continue
        if event["round"] != last_round or (last_t is not None and event["t"] - last_t > gap):
            chain = []
        if reset_on_taken and last_t is not None and any(last_t < t < event["t"] for t in opponent_times):
            chain = []
        others = {m for m in chain if m != event["move"]} if distinct else chain
        predicted = len(others) >= need - 1
        if predicted and event["jump"] == 4:
            hit += 1
        elif predicted:
            false_alarm += 1
            examples.append(("predicted, no combo", event["stamp"], list(chain), event["move"]))
        elif event["jump"] == 4:
            missed += 1
            examples.append(("combo, not predicted", event["stamp"], list(chain), event["move"]))
        if event["jump"] == 4:
            chain = []
        else:
            chain.append(event["move"])
        last_t, last_round = event["t"], event["round"]
    return hit, false_alarm, missed, examples


def main():
    since = sys.argv[1] if len(sys.argv) > 1 else "2026-11-05T01:56:40"
    rows = cr.load(since)
    hits = [r for r in rows if r["ev"] == "hit"]
    for side, other in (("mine", "theirs"), ("theirs", "mine")):
        events = cr.events_for(hits, side)
        opponent_times = [e["t"] for e in cr.events_for(hits, other) if e["jump"] in (1, 4, 5)]
        results = []
        for gap in (2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 6.0):
            for need in (2, 3):
                for distinct in (True, False):
                    for reset in (False, True):
                        for knockdown in (False, True):
                            h, fa, m, examples = score(events, opponent_times, gap, need, distinct, reset, knockdown)
                            f1 = 2 * h / max(2 * h + fa + m, 1)
                            results.append((f1, h, fa, m, gap, need, distinct, reset, knockdown, examples))
        results.sort(key=lambda r: -r[0])
        print(f"\n{side}: best rules (combos explained / predicted wrongly / combos missed):")
        for f1, h, fa, m, gap, need, distinct, reset, knockdown, _ in results[:8]:
            print(f"  {f1:.2f}  explains {h:3d}, wrong {fa:3d}, misses {m:3d} | gap {gap} s, {need} moves, "
                  f"{'different' if distinct else 'any'} moves{', reset when hit' if reset else ''}{', knockdowns count' if knockdown else ''}")
        print("  the rule as the patch notes read, all within 4 s, is tested in combo_rule.py")
        best = results[0]
        print(f"  where the best rule was wrong ({len(best[9])}):")
        for kind, stamp, chain, move in best[9][:14]:
            print(f"    {kind:22} {stamp} chain {chain} then {move}")


if __name__ == "__main__":
    main()
