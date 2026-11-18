"""State source that replays the fight-state rows of a logged session (logs/session-*.jsonl) in real time.

Lets you run and test the harness's decisions with no REK running:

    set REK_REPLAY=logs\\session-YYYYMMDD-HHMMSS.jsonl
    set REK_REPLAY_FROM=21:00:00
    py rek_combo.py --state examples.replay_state:ReplayState --backend examples.move_backend:MoveBackend

It is also the reference for writing your own state source: read() returns the dict that read() below builds, or raises
rek_combo.Unavailable while there is no fight. rek_mem.FightReader is the live one.
"""
import json
import os
import time
from datetime import datetime

from rek_combo import Unavailable


def fighter(row, at):
    x, z, heading, move, elapsed, tilt = row
    return {"at": at, "x": x, "y": 0.0, "z": z, "heading": heading, "tilt": tilt, "move": move, "elapsed": elapsed}


class ReplayState:
    def __init__(self, game=None, path=None, start=None, speed=None):
        path = path or os.environ.get("REK_REPLAY")
        if not path:
            raise SystemExit("ReplayState needs a session log: set REK_REPLAY=logs\\session-....jsonl")
        start = start or os.environ.get("REK_REPLAY_FROM", "")
        self.speed = float(speed or os.environ.get("REK_REPLAY_SPEED", "1"))
        self.rows = []
        for line in open(path, encoding="utf8"):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("ev") == "state" and r.get("me") and r["t"][11:19] >= start:
                self.rows.append((datetime.fromisoformat(r["t"]).timestamp(), r))
        if not self.rows:
            raise SystemExit(f"no state rows in {path} from {start or 'the start'}")
        self.t0 = self.rows[0][0]
        self.began = None
        self.index = 0
        self.round, self.last_total = 1, 0

    def read(self):
        now = time.perf_counter()
        if self.began is None:
            self.began = now
        target = self.t0 + (now - self.began) * self.speed
        while self.index + 1 < len(self.rows) and self.rows[self.index + 1][0] <= target:
            self.index += 1
        if self.index + 1 >= len(self.rows) and target > self.rows[-1][0] + 1.0:
            raise Unavailable("replay finished")
        t, r = self.rows[self.index]
        hits = r.get("hits")
        total = sum(hits) if hits else 0
        if total < self.last_total:  # the score reset: a new round
            self.round += 1
        self.last_total = total
        at = t - self.t0
        return {"phase": r["phase"], "local": 0, "human": None, "me": fighter(r["me"], at), "them": fighter(r["them"], at),
                "distance": r["dist"], "bearing": r["bearing"], "round": self.round, "hits": hits, "falls": None,
                "health": r.get("health"), "referee": {"seq": 0, "call": 0, "faller": 0, "points": 0}}
