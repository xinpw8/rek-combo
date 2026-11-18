#!/usr/bin/env python3
"""Learns the chance each kick lands from the setup it starts in, and tests that on later fights it never saw.

Logistic regression, by hand since only numpy is here. Writes shot_model.json for the macro, with the chance model and each
move's measured cost in points against.

  python shot_model.py          fit on the earlier 70% of strikes, test on the rest, then fit on all and write the file
"""
import json
import math
import sys
from pathlib import Path

import numpy as np

import shot_data as sd

HERE = Path(__file__).resolve().parent
MOVES = ["LLK", "HRSK", "RLFK", "RFK", "RRFK", "FlySide", "BigKick", "SevenKick", "SpinJump"]
SHARED = ["closing", "them_toward", "them_punch", "them_kick_active", "them_kick_spent", "human", "idle"]
L2 = 1.0


def clip(value, low, high):
    return max(low, min(high, value))


def features(row):
    """The chance depends on the move, on the angle and distance for that move, and on what's shared by all of them."""
    a, d = row["aim"] / 30.0, clip(row["dist"], 0.2, 3.5) - 1.1
    a = clip(a, -3.0, 3.0)
    values = {}
    name = row["name"]
    if name in MOVES:
        values[f"{name}"] = 1.0
        values[f"{name}|a"] = a
        values[f"{name}|a2"] = a * a
        values[f"{name}|d"] = d
        values[f"{name}|d2"] = d * d
    values["closing"] = clip(row["closing"], -1.5, 2.0)
    values["them_toward"] = clip(row["them_toward"], -1.0, 1.5)
    state = row["them_state"]
    values["them_punch"] = float(state == "punch")
    values["them_kick_active"] = float(state == "kick_active")
    values["them_kick_spent"] = float(state == "kick_spent")
    values["human"] = float(bool(row.get("human")))
    values["idle"] = float(row["idle"] >= 0.25)
    return values


def names():
    out = []
    for move in MOVES:
        out += [move, f"{move}|a", f"{move}|a2", f"{move}|d", f"{move}|d2"]
    return out + SHARED


def matrix(rows, columns):
    index = {c: i for i, c in enumerate(columns)}
    x = np.zeros((len(rows), len(columns)))
    for r, row in enumerate(rows):
        for key, value in features(row).items():
            x[r, index[key]] = value
    return x


def fit(x, y, steps=60):
    w = np.zeros(x.shape[1])
    penalty = np.full(x.shape[1], L2)
    for _ in range(steps):  # Newton steps
        p = 1.0 / (1.0 + np.exp(-x @ w))
        grad = x.T @ (p - y) + penalty * w
        hess = (x.T * (p * (1 - p) + 1e-6)) @ x + np.diag(penalty)
        step = np.linalg.solve(hess, grad)
        w -= step
        if np.abs(step).max() < 1e-6:
            break
    return w


def logloss(p, y):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return float(-(y * np.log(p) + (1 - y) * np.log(1 - p)).mean())


def auc(p, y):
    order = np.argsort(p)
    ranks = np.empty(len(p))
    ranks[order] = np.arange(1, len(p) + 1)
    pos = y.sum()
    neg = len(y) - pos
    return float((ranks[y == 1].sum() - pos * (pos + 1) / 2) / (pos * neg)) if pos and neg else float("nan")


def main():
    data = [r for r in sd.load() if r["name"] in MOVES]
    data.sort(key=lambda r: r["t"])
    columns = names()
    y = np.array([float(r["hit"]) for r in data])
    cut = int(len(data) * 0.7)
    train, test = data[:cut], data[cut:]
    x_train, y_train = matrix(train, columns), y[:cut]
    x_test, y_test = matrix(test, columns), y[cut:]
    w = fit(x_train, y_train)
    p = 1.0 / (1.0 + np.exp(-x_test @ w))
    print(f"{len(data)} kicks since the patch. Trained on the first {cut}, tested on the last {len(test)} (from {__import__('datetime').datetime.fromtimestamp(test[0]['t']).strftime('%H:%M:%S')}).")
    print(f"Test: land rate {y_test.mean():.0%}. Log loss, lower is better: model {logloss(p, y_test):.3f}; always the average {logloss(np.full(len(y_test), y_train.mean()), y_test):.3f}; each move's average", end=" ")
    averages = {m: (sum(r["hit"] for r in train if r["name"] == m) + 2 * y_train.mean()) / (sum(r["name"] == m for r in train) + 2) for m in MOVES}
    print(f"{logloss(np.array([averages[r['name']] for r in test]), y_test):.3f}")
    print(f"Ranking (AUC, 0.5 is a coin flip): model {auc(p, y_test):.3f}; each move's average {auc(np.array([averages[r['name']] for r in test]), y_test):.3f}")
    print("\nWhen the model says X, how often did it land on the unseen fights?")
    order = np.argsort(p)
    for low, high in ((0, 0.15), (0.15, 0.25), (0.25, 0.35), (0.35, 0.45), (0.45, 0.6), (0.6, 1.01)):
        mask = (p >= low) & (p < high)
        if mask.sum():
            print(f"  said {low:.2f}-{high:.2f}: n={mask.sum():<4} landed {y_test[mask].mean():.0%}")
    print("\nIf the macro had only thrown the strikes the model liked, on those unseen fights:")
    macro = np.array([r["macro"] for r in test])
    human = np.array([bool(r["human"]) for r in test])
    for label, mask in (("macro strikes", macro), ("pad strikes", ~macro), ("real players", human)):
        if mask.sum() < 5:
            print(f"  {label}: too few ({mask.sum()})")
            continue
        line = f"  {label}: all {y_test[mask].mean():.0%} of {mask.sum()}"
        for threshold in (0.3, 0.4, 0.5, 0.6):
            kept = mask & (p >= threshold)
            line += f" | >={threshold:.1f}: {y_test[kept].mean() if kept.sum() else 0:.0%} of {kept.sum()}"
        print(line)

    # Fit on everything, and say what it learned.
    x_all = matrix(data, columns)
    w = fit(x_all, y)
    coefficient = dict(zip(columns, w.tolist()))
    print("\nWhat it learned, fit on everything. Best angle and distance for each move, and the chance there with a free, standing enemy:")
    table = {}
    for move in MOVES:
        a1, a2, d1, d2 = coefficient[f"{move}|a"], coefficient[f"{move}|a2"], coefficient[f"{move}|d"], coefficient[f"{move}|d2"]
        best_a = clip(-a1 / (2 * a2), -2, 2) if a2 < 0 else (2.0 if a1 > 0 else -2.0)
        best_d = clip(-d1 / (2 * d2), -0.9, 1.4) if d2 < 0 else (1.4 if d1 > 0 else -0.9)
        score = coefficient[move] + a1 * best_a + a2 * best_a ** 2 + d1 * best_d + d2 * best_d ** 2 + coefficient["idle"]
        print(f"  {move:10} best aim {best_a * 30:+4.0f}, best distance {best_d + 1.1:.2f} m, chance there {1 / (1 + math.exp(-score)):.0%}")
    print("Shared effects on the odds (multiplier):", {k: round(math.exp(coefficient[k]), 2) for k in SHARED})

    everything = sd.load()
    cost = {}
    for move in MOVES:
        rows = [r for r in everything if r["name"] == move]
        if rows:
            prior = sum(r["against"] for r in everything) / len(everything)
            cost[move] = round((sum(r["against"] for r in rows) + 10 * prior) / (len(rows) + 10), 3)
    print("Average points against while each move plays:", cost)
    # How dangerous is a lock, against simply standing there? And what is a second of the robot's time worth?
    rows = sd.cm.load(sd.UPDATE)
    states = [r for r in rows if r["ev"] == "state" and r["phase"] == 1]
    fight_ends = sorted((r["_t"], r["mode"]) for r in rows if r["ev"] == "fight_end")

    def is_human(t):
        for end_t, mode in fight_ends:
            if end_t >= t:
                return mode.endswith("_mp") or mode == "mp"
        return False

    seconds = {}
    points = {}
    my_hits = {"all": 0, "human": 0}
    total_seconds = {"all": 0.0, "human": 0.0}
    times = [s["_t"] for s in states]

    def kind_of(state):
        move = state["me"][3]
        if not move:
            return "idle"
        name = sd.cm.name_of(move)
        if name == "Recovery":
            return "recovery"
        return "punch" if name in sd.PUNCHES else ("other" if name.startswith(sd.cm.IGNORED) else "kick")

    for a, b in zip(states, states[1:]):
        gap = b["_t"] - a["_t"]
        if gap < 0.5:
            kind = kind_of(a)
            for scope in ("all", "human") if is_human(a["_t"]) else ("all",):
                seconds[(scope, kind)] = seconds.get((scope, kind), 0.0) + gap
                total_seconds[scope] += gap
    import bisect
    for row in rows:
        if row["ev"] != "hit" or row["_t"] < states[0]["_t"]:
            continue
        i = bisect.bisect_left(times, row["_t"]) - 1
        if i < 0:
            continue
        scopes = ("all", "human") if is_human(row["_t"]) else ("all",)
        for scope in scopes:
            if row["mine"] in (1, 4, 5):
                my_hits[scope] += 1
            if row["theirs"] in (1, 4, 5):
                kind = kind_of(states[i])
                points[(scope, kind)] = points.get((scope, kind), 0) + row["theirs"]
    print("\nPoints against per second, by what my robot was doing:")
    for scope in ("all", "human"):
        cells = [f"{kind} {points.get((scope, kind), 0) / seconds[(scope, kind)]:.3f}/s ({seconds[(scope, kind)]:.0f} s)" for kind in ("idle", "kick", "punch", "recovery") if seconds.get((scope, kind))]
        print(f"  {scope:6} " + ", ".join(cells) + f"; my hits per second {my_hits[scope] / max(1.0, total_seconds[scope]):.3f}")
    time_value = my_hits["all"] / max(1.0, total_seconds["all"])
    lock = {move: sd.CLIP_S[move] for move in MOVES}
    raw_names = {f"shanghai_{raw}" if raw[:3] in ("leg", "arm") else raw: short for raw, short in sd.cm.SHORT.items() if short in MOVES}
    (HERE / "shot_model.json").write_text(json.dumps({"columns": columns, "weights": w.tolist(), "cost": cost, "lock_s": lock, "moves": raw_names, "time_value": round(time_value, 3)}, indent=0), encoding="utf8")
    print(f"\nwrote shot_model.json, a second of the robot's time is worth {time_value:.3f} hits")


if __name__ == "__main__":
    sys.exit(main())
