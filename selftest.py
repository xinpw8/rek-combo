#!/usr/bin/env python3
"""Checks the macro engine without touching the game or the focused window.

A low-level keyboard hook records every key event the harness injects and then
swallows it, so real SendInput traffic can be measured while nothing reaches
whatever window has focus. Physical key presses pass through untouched.
"""
import ctypes
import json
import sys
import tempfile
import threading
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rek_combo as rc  # noqa: E402

# A separate tag, so the capture hook below only ever swallows this test's own
# keys and a harness playing live at the same time is left alone.
rc.INJECT_TAG = 0x52454B54

WH_KEYBOARD_LL = rc.WH_KEYBOARD_LL
WM_QUIT = rc.WM_QUIT
KBDLLHOOKSTRUCT = rc.KBDLLHOOKSTRUCT
HOOKPROC = rc.HOOKPROC


class Capture:
    """Records and swallows only the events carrying the harness's tag."""

    def __init__(self):
        self.events = []
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread_id = None

    def __enter__(self):
        self.thread.start()
        assert self.ready.wait(5), "hook did not install"
        return self

    def __exit__(self, *_):
        ctypes.windll.user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)
        self.thread.join(2)

    def _run(self):
        self.thread_id = ctypes.windll.kernel32.GetCurrentThreadId()

        def callback(code, wparam, lparam):
            info = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            if code == 0 and info.dwExtraInfo == rc.INJECT_TAG:
                self.events.append((time.perf_counter(), info.scanCode, not (info.flags & 0x80)))
                return 1
            return rc.user32.CallNextHookEx(None, code, wparam, lparam)

        self.proc = HOOKPROC(callback)
        hook = rc.user32.SetWindowsHookExW(WH_KEYBOARD_LL, self.proc, None, 0)
        assert hook, f"SetWindowsHookEx failed: {ctypes.get_last_error()}"
        message = wintypes.MSG()
        ctypes.windll.user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)  # creates the queue
        self.ready.set()
        while ctypes.windll.user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            pass
        rc.user32.UnhookWindowsHookEx(hook)


class NullSession:
    def event(self, *_a, **_k):
        pass


failures = []


def check(name, ok, detail=""):
    print(f"{'ok  ' if ok else 'FAIL'} {name}{'  ' + detail if detail else ''}", flush=True)
    if not ok:
        failures.append(name)


def expect_rejected(name, game, raw):
    try:
        rc.Config(raw, game)
    except ValueError as error:
        check(name, True, str(error))
    else:
        check(name, False, "config was accepted")


def main():
    rc.winmm.timeBeginPeriod(1)
    game = rc.GameData()
    config = rc.load_config(Path(__file__).resolve().parent / "combos.json", game)

    # The table read from the build holds the moves the harness relies on.
    for alias, key in (("LLK", "Y"), ("RFK", "I"), ("RRFK", "O"), ("RLFK", "U"), ("HRSK", "K")):
        check(f"{alias} is Shift+{key} in this build", game.chords[rc.ALIASES[alias]] == ["LeftShift", key])
    every_key = {k for chord in game.chords.values() for k in chord}
    check("every bound key has a scan code", every_key <= set(rc.SCAN), str(sorted(every_key - set(rc.SCAN))))
    check("e-stop key is not used by any macro",
          not any("Backspace" in s.chord for c in config.combos for s in c.steps))

    # Every shipped combo meets the combo rule and the game's cooldown.
    for combo in config.combos:
        text = rc.describe(combo, config, game)
        fatal = any(word in text for word in ("cooldown", "over the"))
        check(f"timing is playable: {combo.name}", not fatal, text if fatal else "")

    # A press inside the cooldown and a cancelled strike are reported.
    bad = rc.Combo("bad", None, None, [rc.Step("shanghai_leg2", 0.0, game.chords["shanghai_leg2"]),
                                       rc.Step("shanghai_leg16", 0.3, game.chords["shanghai_leg16"]),
                                       rc.Step("shanghai_leg2", 0.3, game.chords["shanghai_leg2"])])
    text = rc.describe(bad, config, game)
    check("flags a gap inside the cooldown", "cooldown" in text)
    check("flags a cancelled strike", "cancels this move" in text)
    expect_rejected("rejects an unbound move", game,
                    {"combos": [{"name": "x", "steps": [{"move": "nope"}]}]})
    expect_rejected("rejects a duplicate trigger", game,
                    {"combos": [{"name": "a", "key": "f6", "steps": [{"move": "LLK"}]},
                                {"name": "b", "key": "f6", "steps": [{"move": "RFK"}]}]})

    # The key file changes constantly, so the engine is tested on fixed examples of its own.
    config = rc.Config({"combos": [
        {"name": "fixture a", "key": "f6", "steps": [{"move": "LLK"}, {"move": "RFK", "after_ms": 950},
                                                     {"move": "shanghai_arm3", "after_ms": 1200}]},
        {"name": "fixture b", "key": "f7", "steps": [{"move": "RLFK"}, {"move": "RFK", "after_ms": 900}]}]}, game)
    combo = config.combos[0]
    keyboard = rc.Keyboard()

    # Real SendInput traffic: order, timing, and nothing left held.
    with Capture() as capture:
        player = rc.Player(keyboard, NullSession(), require_focus=False)
        rc.say = lambda _text: None
        player.fire(combo, config, "selftest")
        player.thread.join(10)
        time.sleep(0.05)
    shift = rc.SCAN["LeftShift"]
    events = capture.events
    wanted = []
    for step in combo.steps:
        mods, key = step.chord[:-1], step.chord[-1]
        wanted += [(rc.SCAN[m], True) for m in mods] + [(rc.SCAN[key], True), (rc.SCAN[key], False)]
        wanted += [(rc.SCAN[m], False) for m in reversed(mods)]
    check("chords in order: modifier down, key down, key up, modifier up",
          [(scan, down) for _, scan, down in events] == wanted, f"{len(events)} events")
    letters = [(t, scan) for t, scan, down in events if down and scan != shift]
    expected = [rc.SCAN[s.chord[-1]] for s in combo.steps]
    check("moves arrive in order", [scan for _, scan in letters] == expected)
    for i, step in enumerate(combo.steps[1:], 1):
        gap_ms = (letters[i][0] - letters[i - 1][0]) * 1000
        check(f"gap {i} within 3 ms of {step.after_s * 1000:.0f} ms", abs(gap_ms - step.after_s * 1000) < 3,
              f"measured {gap_ms:.1f} ms")
    for number, (pressed_at, scan) in enumerate(letters, 1):
        released_at = next(t for t, s, down in events if s == scan and not down and t > pressed_at)
        hold_ms = (released_at - pressed_at) * 1000
        check(f"move {number}: key held at least 2.5 frames", hold_ms >= 42, f"{hold_ms:.0f} ms")
    check("nothing left held", keyboard.held == [])

    # One press plays the whole combo: another key cannot cut it short.
    with Capture() as capture:
        player = rc.Player(keyboard, NullSession(), require_focus=False)
        player.fire(combo, config, "selftest")
        time.sleep(0.2)
        refused = not player.fire(config.combos[1], config, "selftest")
        player.thread.join(10)
        time.sleep(0.05)
    played = [scan for _, scan, down in capture.events if down and scan != shift]
    check("a second key during a combo is ignored", refused and played == expected)

    # An interrupt key cuts a playing combo off at once.
    cutter = rc.Config({"combos": [{"name": "cut", "key": "f7", "interrupt": True, "steps": [{"move": "auto_recovery"}]}]}, game)
    with Capture() as capture:
        player = rc.Player(keyboard, NullSession(), require_focus=False)
        player.fire(combo, config, "selftest")
        time.sleep(0.2)
        taken_over = player.fire(cutter.combos[0], cutter, "selftest")
        player.thread.join(5)
        time.sleep(0.05)
    played = [scan for _, scan, down in capture.events if down and scan != shift]
    check("an interrupt key cuts the combo and plays at once",
          taken_over and played == [expected[0], rc.SCAN["R"]] and keyboard.held == [])

    # The last move gets to finish its strikes before another combo can start.
    punch = rc.Config({"combos": [{"name": "punch", "key": "f6", "steps": [{"move": "shanghai_arm3"}]}]}, game)
    with Capture() as capture:
        player = rc.Player(keyboard, NullSession(), require_focus=False)
        player.fire(punch.combos[0], punch, "selftest")
        time.sleep(0.8)
        check("still playing 0.8 s after a punch combo's press", player.busy)
        player.thread.join(5)
        check("free again once its strikes are done", not player.busy)

    # Live aim is faked here, so the turn logic can be tested without a fight.
    class FakeAim:
        def __init__(self, values, degrees_per_second=0.0, move=(0, 0.0)):
            self.values = values
            self.speed = degrees_per_second
            self.move = move
            self.began = time.perf_counter()
            self.rate = 0.0
            self.chain = rc.ComboChain(recovery_id=33)
            self.score = 0
            self.chain.update(self.began, 1, 0, 0)
            self.dropped = set()
            self.script = []  # (seconds after creation, score jump, move playing)
            self.aim_at = None  # aim by seconds after creation, instead of the list of values
            self.move_at = None  # (move, elapsed) by seconds after creation, instead of move
            self.drop_presses = 0  # how many presses the game ignores
            self.idle_at = None  # seconds idle by seconds after creation, when not simply idle for a long time

        def tick(self):
            elapsed = time.perf_counter() - self.began
            while self.script and self.script[0][0] <= elapsed:
                _, jump, move = self.script.pop(0)
                self.score += jump
                self.chain.update(time.perf_counter(), 1, self.score, move)

        def error(self):
            self.tick()
            if self.aim_at:
                return self.aim_at(time.perf_counter() - self.began)
            return self.values.pop(0) if len(self.values) > 1 else self.values[0]

        lead_shift = 0.0  # how far the predicted aim is ahead of the current one

        def predicted(self, _lead):
            value = self.error()
            return None if value is None else value - self.lead_shift

        def in_a_move(self):
            return bool(self.move[0])

        def current_move(self):
            return self.move_at(time.perf_counter() - self.began) if self.move_at else self.move

        closing_speed = 0.0
        enemy = (0, 0.0)
        is_human = False

        def closing(self):
            return self.closing_speed

        own_speed = 0.0

        def own_approach(self):
            return self.own_speed

        up_for = float("inf")  # seconds since the other robot got up; 0 while it is down

        def enemy_up_for(self):
            return self.up_for

        them_speed = 0.0  # how fast the other robot walks toward mine

        def enemy_approach(self):
            return self.them_speed

        def shot_inputs(self):
            return {"closing": self.closing_speed or 0.0, "them_toward": 0.0, "them_move": self.enemy[0], "them_elapsed": self.enemy[1], "human": self.is_human, "idle": self.idle_for()}

        def idle_for(self):
            if self.current_move()[0]:
                return 0.0
            return self.idle_at(time.perf_counter() - self.began) if self.idle_at else 9.0

        def heading(self):
            return ((time.perf_counter() - self.began) * self.speed + 180.0) % 360.0 - 180.0

        tilt_deg = 0.0

        def tilt(self):
            return self.tilt_deg

        me_xz = (0.0, 0.0)
        them_xz = None
        facing_deg = 0.0

        def positions(self):
            them = self.them_xz or (self.me_xz[0], self.me_xz[1] + self.distance())
            return {"me": self.me_xz, "them": them, "facing": self.facing_deg, "distance": self.distance()}

        range_m = 1.0
        range_at = None  # distance by seconds after creation, when it changes
        dropped = frozenset()
        hit_times = ()  # seconds after creation at which the clean-hit counter rises
        blocked = ()

        def distance(self):
            return self.range_at(time.perf_counter() - self.began) if self.range_at else self.range_m

        def hits(self):
            self.tick()
            elapsed = time.perf_counter() - self.began
            return self.chain.landed + sum(t <= elapsed for t in self.hit_times)

        def since_landed(self):
            landed = self.chain.last_landed
            return float("inf") if landed is None else time.perf_counter() - landed

        def started_since(self, move_id, when):
            if self.drop_presses and when not in self.dropped:
                self.dropped.add(when)
                self.drop_presses -= 1
            return when not in self.dropped

        def since_started(self, move_id):
            return 0.0 if move_id in self.blocked else float("inf")

    gated = rc.Config({"combos": [{"name": "gated", "key": "f6", "steps": [
        {"move": "LLK"}, {"move": "RFK", "min_ms": 600, "max_ms": 1200}]}]}, game)
    second_key = rc.SCAN[gated.combos[0].steps[1].chord[-1]]

    def gap_with(aim):
        with Capture() as capture:
            player = rc.Player(keyboard, NullSession(), require_focus=False, aim=aim)
            player.fire(gated.combos[0], gated, "selftest")
            player.thread.join(5)
            time.sleep(0.05)
        downs = [t for t, scan, down in capture.events if down and scan != shift]
        check("gated combo plays both moves", len(downs) == 2 and
              [scan for _, scan, down in capture.events if down and scan != shift][1] == second_key)
        return downs[1] - downs[0]

    gap = gap_with(FakeAim([5.0]))
    check("aligned: fires at the earliest allowed time", 0.58 < gap < 0.68, f"{gap:.2f} s")
    gap = gap_with(FakeAim([90.0]))
    check("never aligned: fires at the time limit anyway", 1.15 < gap < 1.25, f"{gap:.2f} s")
    gap = gap_with(FakeAim([None]))
    check("no live aim: fires midway through the range", 0.85 < gap < 0.95, f"{gap:.2f} s")

    # The turn table picks the move that reaches a turn soonest.
    table = rc.TurnTable({"spinning_back_kick": [(0.5, 64.0), (0.6, 94.0), (0.7, 134.0)],
                          "shanghai_leg16": [(0.5, -49.0), (0.6, -57.0)],
                          "shanghai_leg19": [(0.5, -102.0), (0.6, -148.0)]})
    check("table: right 90 is Spinning Back Kick at 0.6 s", table.best(90.0)[:2] == ("spinning_back_kick", 0.6))
    check("table: left 150 is High Right Side Kick at 0.6 s", table.best(-150.0)[:2] == ("shanghai_leg19", 0.6))
    check("table: left 50 is Right Front Kick", table.best(-50.0)[0] == "shanghai_leg16")
    check("table: nothing available gives None", rc.TurnTable({}).best(90.0) is None)
    real = rc.TurnTable.load(game.chords)
    check("the measured turn table loads", bool(real.curves), f"{len(real.curves)} moves")
    check("the measured table has no emotes or recovery", not any(
        m.startswith("dance") or m == "auto_recovery" for m in real.curves))

    def action_config(action):
        built = rc.Config({"combos": [{"name": "action", "key": "f6", "interrupt": True, "action": action}]}, game)
        built.table = table
        return built

    def run_action(action, aim):
        built = action_config(action)
        with Capture() as capture:
            player = rc.Player(keyboard, NullSession(), require_focus=False, aim=aim)
            player.fire(built.combos[0], built, "selftest")
            player.thread.join(8)
            time.sleep(0.05)
        return [(t, scan) for t, scan, down in capture.events if down and scan != shift]

    key_of = lambda move: rc.SCAN[game.chords[move][-1]]

    presses = run_action({"kind": "turn", "degrees": 90.0}, FakeAim([0.0], degrees_per_second=150.0))
    check("set turn: plays the move, then cancels with Recovery",
          [scan for _, scan in presses] == [key_of("spinning_back_kick"), rc.SCAN["R"]], str(len(presses)))
    if len(presses) == 2:
        gap = presses[1][0] - presses[0][0]
        check("set turn: the cancel respects the cooldown and comes when turned", 0.54 < gap < 0.8, f"{gap:.2f} s")
    check("set turn: nothing left held", keyboard.held == [])
    presses = run_action({"kind": "turn", "degrees": 90.0, "stop": "LLK"}, FakeAim([0.0], degrees_per_second=150.0))
    check("set turn: a kick can't be cut, so only Recovery can try to stop it",
          [scan for _, scan in presses] == [key_of("spinning_back_kick"), rc.SCAN["R"]], str(presses))
    kick_table = table
    table = rc.TurnTable({"shanghai_arm3": [(0.6, -50.0), (0.7, -60.0)]})
    presses = run_action({"kind": "turn", "degrees": -60.0, "stop": "LLK"}, FakeAim([0.0], degrees_per_second=-90.0))
    table = kick_table
    check("set turn: a stop move cuts a punch turn",
          [scan for _, scan in presses] == [key_of("shanghai_arm3"), key_of("shanghai_leg2")], str(presses))
    if len(presses) == 2:
        gap = presses[1][0] - presses[0][0]
        check("set turn: the stop move respects the cooldown and comes when turned", 0.54 < gap < 0.8, f"{gap:.2f} s")
    expect_rejected("a stop move with no keyboard binding", game, {"combos": [{"name": "x", "key": "f6", "action": {"kind": "turn", "degrees": 90, "stop": "nothing"}}]})
    presses = run_action({"kind": "face", "stop": "LLK"}, FakeAim([-150.0], degrees_per_second=-250.0))
    check("face: a kick turn can only be stopped by Recovery",
          [scan for _, scan in presses] == [key_of("shanghai_leg19"), rc.SCAN["R"]])

    presses = run_action({"kind": "face"}, FakeAim([-150.0], degrees_per_second=-250.0))
    check("face: far off target uses a turning move, then cancels",
          [scan for _, scan in presses] == [key_of("shanghai_leg19"), rc.SCAN["R"]])
    presses = run_action({"kind": "face"}, FakeAim([5.0]))
    check("face: aligned and free presses nothing", presses == [])
    presses = run_action({"kind": "face"}, FakeAim([5.0], move=(6, 0.7)))
    check("face: aligned in a clip just frees the robot", [scan for _, scan in presses] == [rc.SCAN["R"]])
    presses = run_action({"kind": "face"}, FakeAim([None]))
    check("face: presses nothing without a live round", presses == [])

    begun = time.perf_counter()
    presses = run_action({"kind": "free"}, FakeAim([0.0], move=(13, 0.2)))
    check("free: waits out the cooldown, then taps Recovery once",
          [scan for _, scan in presses] == [rc.SCAN["R"]] and presses[0][0] - begun > 0.3)
    presses = run_action({"kind": "free"}, FakeAim([0.0]))
    check("free: nothing playing presses nothing", presses == [])

    # The combo rule as the game scores it: different moves, each hit within 4 s of the last.
    llk, hrsk, big, recovery = (game.move_ids[m] for m in ("shanghai_leg2", "shanghai_leg19", "big_kick", "auto_recovery"))
    rule = rc.ComboChain(recovery_id=recovery)
    rule.update(0.0, 1, 0, 0)
    rule.update(1.0, 1, 1, llk)
    check("rule: a clean hit starts a chain", rule.moves == (llk,) and rule.landed == 1)
    rule.update(1.5, 1, 2, llk)
    check("rule: the same move again adds nothing", rule.moves == (llk,) and rule.landed == 2)
    rule.update(4.0, 1, 3, hrsk)
    check("rule: a different move adds to it", rule.moves == (llk, hrsk))
    rule.update(7.5, 1, 7, big)
    check("rule: COMBO X2 counts and clears the chain", rule.combos == 1 and rule.moves == () and rule.landed == 4)
    rule.update(8.0, 1, 10, 0)
    check("rule: the other robot slipping is not my hit", rule.landed == 4 and rule.moves == ())
    rule.update(8.5, 1, 11, llk)
    rule.update(9.0, 1, 16, hrsk)
    check("rule: a knockdown lands but does not add to the chain", rule.landed == 6 and rule.moves == (llk,))
    rule.update(12.4, 1, 16, 0)
    check("rule: still alive just inside 4 s", rule.moves == (llk,))
    rule.update(12.6, 1, 16, 0)
    check("rule: more than 4 s without a hit clears it", rule.moves == ())
    rule.update(13.0, 1, 16, hrsk)
    rule.update(13.3, 1, 17, 0)
    check("rule: a hit reported just after its clip ended is credited to it", rule.moves == (hrsk,))
    rule.update(13.4, 1, 17, recovery)
    rule.update(13.5, 1, 18, recovery)
    check("rule: Recovery never earns credit for itself", rule.moves == (hrsk,) and recovery not in rule.moves)
    rule.update(14.0, 2, 0, 0)
    check("rule: a new round clears it", rule.moves == ())
    hit_rule = rc.ComboChain(recovery_id=recovery)
    hit_rule.update(0.0, 1, 0, 0, theirs=0)
    hit_rule.update(1.0, 1, 1, llk, theirs=0)
    hit_rule.update(1.5, 1, 1, 0, theirs=1)
    check("rule: a clean hit on me breaks the chain", hit_rule.moves == (), str(hit_rule.moves))
    hit_rule.update(2.0, 1, 2, hrsk, theirs=1)
    check("rule: after it, my next hit starts a new chain", hit_rule.moves == (hrsk,), str(hit_rule.moves))
    hit_rule.update(2.5, 1, 2, 0, theirs=4)
    check("rule: their slip points (+3) don't break it", hit_rule.moves == (hrsk,), str(hit_rule.moves))

    # The chain macro: one press plays all three, waiting for shots and surviving misses.
    jab_id = game.move_ids["shanghai_arm32"]
    cut_rule = rc.ComboChain(recovery_id=recovery)
    cut_rule.update(0.0, 1, 0, 0)
    cut_rule.update(1.0, 1, 0, jab_id)
    cut_rule.update(1.5, 1, 0, jab_id)
    cut_rule.update(1.55, 1, 0, llk)
    cut_rule.update(1.6, 1, 1, llk)
    check("rule: a hit showing just after a clip was cut belongs to that clip", cut_rule.moves == (jab_id,), str(cut_rule.moves))
    cut_rule.update(1.9, 1, 2, llk)
    check("rule: the next hit belongs to the clip that cut it", cut_rule.moves == (jab_id, llk), str(cut_rule.moves))

    good = {"-20..20|0.7..1.0": 0.6}
    chain_land = rc.LandTable({
        "aim_bins": [[-181, -20], [-20, 20], [20, 181]], "dist_bins": [[0.0, 0.7], [0.7, 1.0], [1.7, 9.0]],
        "moves": {
            "leg2": {"overall": 0.1, "cells": good, "hit_by_s": 0.3, "ends_s": 1.9},
            "leg19": {"overall": 0.1, "cells": good, "hit_by_s": 0.3},
            "big_kick": {"overall": 0.1, "cells": good, "hit_by_s": 0.3},
            "flying_side_kick": {"overall": 0.05, "cells": {"-20..20|1.7..9.0": 0.5}, "hit_by_s": 1.0}}})
    check("land table: rate by angle and distance", chain_land.rate("shanghai_leg2", 5.0, 0.8) == 0.6)
    check("land table: how long a clip runs", chain_land.ends("shanghai_leg2") == 1.9 and chain_land.ends("big_kick") == float("inf"))
    near, far, close = "-20..20|0.7..1.0", "-20..20|1.7..9.0", "-20..20|0.0..0.7"
    idle_land = rc.LandTable({
        "aim_bins": chain_land.aim_bins, "dist_bins": chain_land.dist_bins,
        "moves": {
            "leg2": {"overall": 0.01, "cells": {near: 0.6, far: 0.38, close: 0.36}, "hit_by_s": 0.3, "ends_s": 1.9},
            "leg19": {"overall": 0.01, "cells": {near: 0.4}, "hit_by_s": 0.3},
            "big_kick": {"overall": 0.01, "cells": {near: 0.9, far: 0.55}, "hit_by_s": 0.3, "ends_s": 3.8},
            "flying_side_kick": {"overall": 0.01, "cells": {close: 0.4}, "hit_by_s": 1.0}}})
    check("the game's kicks can't be cut, its punches can",
          {"shanghai_leg2", "shanghai_leg19", "big_kick", "flying_side_kick"} <= game.uninterruptible and "shanghai_arm32" not in game.uninterruptible)
    combo_action = {"kind": "chain", "openers": ["LLK", "flying_side_kick"], "moves": ["LLK", "HRSK", "big_kick", "flying_side_kick"],
                    "open_min": 0.35, "open_wait_ms": 500, "next_min": 0.3, "last_chance_s": 1.0, "last_min": 0.15, "no_cancel": ["HRSK"]}

    def run_combo(aim, action=combo_action, join_s=9.0, player_setup=None, land=chain_land, shots=None):
        built = action_config(action)
        built.land = land
        if shots:
            built.shots = shots
        with Capture() as capture:
            player = rc.Player(keyboard, NullSession(), require_focus=False, aim=aim)
            if player_setup:
                player_setup(player)
            begun = time.perf_counter()
            player.fire(built.combos[0], built, "selftest")
            player.thread.join(join_s)
            finished = time.perf_counter() - begun
            player.cancel.set()  # it must stop while the capture hook is still catching its keys
            player.thread.join(2)
            time.sleep(0.05)
        downs = [(t - begun, scan) for t, scan, down in capture.events if down and scan != shift]
        return [scan for _, scan in downs], [t for t, _ in downs], finished

    aim = FakeAim([5.0])
    aim.range_m = 0.8
    aim.script = [(0.25, 1, llk), (1.0, 1, hrsk), (1.55, 4, big)]
    scans, times, _ = run_combo(aim)
    check("chain: one press lands three different moves", scans == [key_of("shanghai_leg2"), key_of("shanghai_leg19"), key_of("big_kick")], str(scans))
    check("chain: every gap clears the cooldown", all(b - a >= 0.54 for a, b in zip(times, times[1:])))
    check("chain: the combo scoring ends it", aim.chain.combos == 1)

    aim = FakeAim([5.0])
    aim.range_m = 0.8
    aim.script = [(0.25, 1, llk)]
    aim.aim_at = lambda t: 5.0 if t < 0.3 or t >= 2.5 else 70.0
    scans, times, _ = run_combo(aim, join_s=3.2)
    check("chain: waits past 2 s for the next shot, then throws it",
          len(scans) >= 2 and scans[0] == key_of("shanghai_leg2") and scans[1] != scans[0] and 2.45 < times[1] < 2.7,
          f"{[round(t, 2) for t in times]}")

    aim = FakeAim([5.0])
    aim.range_m = 0.8
    aim.script = [(0.25, 1, llk)]
    aim.aim_at = lambda t: 5.0 if t < 0.3 else 70.0
    scans, times, finished = run_combo(aim)
    check("chain: gives up once 4 s pass with no shot", scans == [key_of("shanghai_leg2")] and 4.1 < finished < 4.7, f"{finished:.2f} s")

    aim = FakeAim([5.0])
    aim.range_m = 0.8
    aim.script = [(0.25, 1, llk), (2.6, 4, big)]
    scans, times, _ = run_combo(aim)
    check("chain: a miss doesn't end it while time remains", len(scans) >= 3 and scans[0] == key_of("shanghai_leg2"), str(len(scans)))

    aim = FakeAim([5.0])
    aim.range_m = 0.8
    scans, times, finished = run_combo(aim)
    check("chain: a missed opener ends it", scans == [key_of("shanghai_leg2")] and finished < 1.2, f"{finished:.2f} s")

    aim = FakeAim([70.0])
    aim.range_m = 0.8
    scans, _, finished = run_combo(aim)
    check("chain: never in position holds fire", scans == [] and 0.45 < finished < 0.7, f"{finished:.2f} s")

    aim = FakeAim([5.0])
    aim.range_m = 0.8
    aim.aim_at = lambda t: 70.0 if t < 0.3 else 5.0
    scans, times, _ = run_combo(aim)
    check("chain: fires as soon as the robot lines up", scans == [key_of("shanghai_leg2")] and 0.28 < times[0] < 0.42, f"{times}")

    aim = FakeAim([5.0])
    aim.range_m = 0.8
    aim.chain.moves, aim.chain.last_hit = (llk,), aim.began - 3.8  # that chain runs out 0.2 s from now, before a shot opens
    aim.aim_at = lambda t: 70.0 if t < 0.3 else 5.0
    aim.script = [(0.25, 0, 0)]
    scans, times, _ = run_combo(aim)
    check("chain: pressed as an old chain runs out, it opens a new one",
          scans == [key_of("shanghai_leg2")] and 0.28 < times[0] < 0.42, f"{scans} {times}")

    aim = FakeAim([5.0])
    aim.range_m = 2.0
    scans, _, _ = run_combo(aim)
    check("chain: from long range opens with Flying Side Kick", scans == [key_of("flying_side_kick")])

    aim = FakeAim([5.0])
    aim.range_m = 0.8
    aim.chain.update(time.perf_counter(), 1, 1, llk)
    aim.score = 1
    aim.script = [(0.7, 4, hrsk)]
    scans, _, _ = run_combo(aim)
    check("chain: a hit from the pad counts, so it moves on to a different move",
          scans and scans[0] != key_of("shanghai_leg2"), str(scans))

    single = dict(combo_action, max_strikes=1)
    aim = FakeAim([5.0], move=(0, 0.0))
    aim.range_m = 0.8
    scans, _, finished = run_combo(aim, action=single)
    check("one strike: throws once and stops", scans == [key_of("shanghai_leg2")] and finished < 1.2, f"{finished:.2f} s")

    # Off balance, nothing goes until the robot steadies.
    steady = dict(single, max_tilt_deg=15)
    aim = FakeAim([5.0])
    aim.range_m = 0.8
    aim.tilt_deg = 30.0
    scans, _, finished = run_combo(aim, action=steady)
    check("tilt: holds fire while the robot leans", scans == [] and finished < 1.0, f"{scans} {finished:.2f} s")
    aim = FakeAim([5.0])
    aim.range_m = 0.8
    aim.tilt_deg = 5.0
    scans, _, _ = run_combo(aim, action=steady)
    check("tilt: upright, it strikes", scans == [key_of("shanghai_leg2")], str(scans))

    # Pressed while the robot is still in another clip: the wait starts once it's free, up to a cap.
    busy = dict(single, max_wait_ms=3000)
    aim = FakeAim([5.0])
    aim.range_m = 0.8
    aim.move_at = lambda t: (hrsk, 0.2 + t) if t < 1.0 else (0, 0.0)
    scans, times, _ = run_combo(aim, action=busy, land=idle_land)
    check("busy: waits out a clip longer than its window, then strikes",
          scans == [key_of("shanghai_leg2")] and 0.95 < times[0] < 1.4, f"{scans} {[round(t, 2) for t in times]}")
    aim = FakeAim([5.0])
    aim.range_m = 0.8
    aim.move_at = lambda t: (hrsk, 0.2 + t)
    scans, _, finished = run_combo(aim, action=busy, land=idle_land, join_s=4.0)
    check("busy: never free, it gives up at the cap", [s for s in scans if s != rc.SCAN["R"]] == [] and 2.9 < finished < 3.6, f"{finished:.2f} s")
    aim = FakeAim([5.0])
    aim.range_m = 0.8
    aim.move_at = lambda t: (hrsk, 0.2 + t) if t < 1.0 else (0, 0.0)
    scans, _, finished = run_combo(aim, action=single, land=idle_land)
    check("busy: without the cap set, the old window still applies", [s for s in scans if s != rc.SCAN["R"]] == [] and finished < 0.9, f"{finished:.2f} s")

    # Poke or combo: once nothing left can land inside the combo's 4 s, it walks out of reach instead of standing there.
    poke = dict(combo_action, openers=["LLK"], moves=["LLK", "big_kick"], move_min_used={"big_kick": 2}, opens=1,
                backoff_ms=300, backoff_until_m=1.8)
    back, right, left_key = rc.SCAN["S"], rc.SCAN["D"], rc.SCAN["A"]
    aim = FakeAim([5.0])
    aim.range_m = 0.8
    aim.script = [(0.25, 1, llk)]
    scans, _, finished = run_combo(aim, action=poke)
    check("poke: a hit with no combo to follow backs off, straight back",
          scans[:1] == [key_of("shanghai_leg2")] and back in scans and right not in scans and finished < 1.6, f"{scans} {finished:.2f} s")
    aim = FakeAim([5.0])
    aim.range_m = 0.8
    scans, _, _ = run_combo(aim, action=poke)
    check("poke: a missed opener backs off too", scans[:1] == [key_of("shanghai_leg2")] and back in scans, str(scans))
    aim = FakeAim([5.0])
    aim.range_m = 0.8
    aim.script = [(0.25, 1, llk)]
    scans, _, _ = run_combo(aim, action=poke, player_setup=lambda p: setattr(p, "stick_pushed", True))
    check("poke: the pilot's stick wins, no back-off keys", back not in scans and right not in scans, str(scans))
    aim = FakeAim([5.0])
    aim.range_m = 0.8
    aim.me_xz, aim.them_xz, aim.facing_deg = (0.0, -2.3), (0.0, -1.5), 0.0
    aim.script = [(0.25, 1, llk)]
    scans, _, _ = run_combo(aim, action=poke)
    check("poke: with a wall behind it steps sideways, not back", back not in scans and (right in scans or left_key in scans), str(scans))
    aim = FakeAim([5.0])
    aim.range_m = 2.4
    aim.script = [(0.25, 1, llk)]
    scans, _, _ = run_combo(aim, action=dict(poke, move_max_dist={}))
    check("poke: already out of reach, it stays", back not in scans and right not in scans, str(scans))
    aim = FakeAim([5.0])
    aim.range_m = 0.8
    aim.chain.moves, aim.chain.last_hit = (llk,), aim.began - 1.0
    scans, _, _ = run_combo(aim, action=dict(poke, max_strikes=1))
    check("poke: a combo that can't go on doesn't block a fresh kick", scans[:1] == [key_of("shanghai_leg2")], str(scans))
    one = dict(poke, moves=["LLK", "big_kick", "HRSK"], max_strikes=1)
    aim = FakeAim([5.0])
    aim.range_m = 0.8
    aim.script = [(0.25, 1, llk)]
    scans, _, _ = run_combo(aim, action=one)
    check("poke: one strike that opened a combo stays put for the next press", back not in scans, str(scans))
    aim = FakeAim([5.0])
    aim.range_m = 0.8
    aim.script = [(0.25, 1, llk), (1.0, 1, hrsk), (1.55, 4, big)]
    scans, _, _ = run_combo(aim, action=dict(poke, moves=["LLK", "HRSK", "big_kick"]))
    check("combo: with an opening it keeps going, three moves", scans[:3] == [key_of("shanghai_leg2"), key_of("shanghai_leg19"), key_of("big_kick")], str(scans))

    # A poke has to be a good shot; the hit that sets up a combo may go on longer odds.
    class FixedShots:
        def __init__(self, chances):
            self.chances = chances
            self.moves = {m: m for m in chances}
            self.time_value = 0.0

        def chance(self, move, aim, distance, setup):
            return self.chances.get(move, 0.0)

        def lock(self, move):
            return 1.0

    graded = dict(combo_action, openers=["LLK"], moves=["LLK", "HRSK"], use_model=True, min_chance=0.40, next_chance=0.30, max_strikes=1)
    shots = FixedShots({"shanghai_leg2": 0.35, "shanghai_leg19": 0.35})
    aim = FakeAim([5.0])
    aim.range_m = 0.8
    scans, _, _ = run_combo(aim, action=graded, shots=shots)
    check("poke bar: a 35% poke doesn't go", scans == [], str(scans))
    aim = FakeAim([5.0])
    aim.range_m = 0.8
    aim.chain.update(time.perf_counter(), 1, 1, llk)
    aim.score = 1
    scans, _, _ = run_combo(aim, action=graded, shots=shots)
    check("combo bar: a 35% second hit goes", scans[:1] == [key_of("shanghai_leg19")], str(scans))

    # A move the model underrates can have its own, lower bar.
    low_hrsk = FixedShots({"shanghai_leg2": 0.25, "shanghai_leg19": 0.25})
    aim = FakeAim([5.0])
    aim.range_m = 0.8
    aim.chain.update(time.perf_counter(), 1, 1, llk)
    aim.score = 1
    strict = dict(graded, last_min=0.4)  # so the combo's last second doesn't lower the bar either
    scans, _, _ = run_combo(aim, action=strict, shots=low_hrsk)
    check("move bar: without its own bar, a 25% follow-up waits", scans == [], str(scans))
    aim = FakeAim([5.0])
    aim.range_m = 0.8
    aim.chain.update(time.perf_counter(), 1, 1, llk)
    aim.score = 1
    scans, _, _ = run_combo(aim, action=dict(strict, move_min_chance={"HRSK": 0.2}), shots=low_hrsk)
    check("move bar: with its own 20% bar, High Right Side Kick goes", scans[:1] == [key_of("shanghai_leg19")], str(scans))

    # In the face of a punching enemy, Big Kick goes first even though it's normally only a finisher.
    brawler = dict(combo_action, openers=["LLK"], moves=["LLK", "big_kick"], move_min_used={"big_kick": 2}, use_model=True,
                   min_chance=0.35, max_strikes=1, brawl={"moves": ["big_kick"], "max_dist": 0.9})
    both_ok = FixedShots({"shanghai_leg2": 0.5, "big_kick": 0.5})
    ljab_id = game.move_ids["shanghai_arm32"]
    aim = FakeAim([5.0])
    aim.range_m = 0.7
    aim.enemy = (ljab_id, 0.3)
    scans, _, _ = run_combo(aim, action=brawler, shots=both_ok)
    check("brawl: close and punching, it opens with Big Kick", scans[:1] == [key_of("big_kick")], str(scans))
    aim = FakeAim([5.0])
    aim.range_m = 0.7
    scans, _, _ = run_combo(aim, action=brawler, shots=both_ok)
    check("brawl: close but not punching, the usual poke", scans[:1] == [key_of("shanghai_leg2")], str(scans))
    aim = FakeAim([5.0])
    aim.range_m = 1.2
    aim.enemy = (ljab_id, 0.3)
    scans, _, _ = run_combo(aim, action=brawler, shots=both_ok)
    check("brawl: punching but out past 0.9 m, the usual poke", scans[:1] == [key_of("shanghai_leg2")], str(scans))

    # The walk-in never runs while the robot is down or getting up.
    walker = dict(single, walk_to_m=0.9)
    w_key = rc.SCAN["W"]
    aim = FakeAim([70.0])  # never lined up, so the only thing it can do is walk
    aim.range_m = 2.0
    aim.tilt_deg = 60.0
    scans, _, _ = run_combo(aim, action=walker)
    check("walk-in: never while the robot is down", w_key not in scans, str(scans))
    aim = FakeAim([70.0])
    aim.range_m = 2.0
    aim.move = (33, 0.5)  # getting up (Recovery)
    scans, _, _ = run_combo(aim, action=walker)
    check("walk-in: never while it's getting up", w_key not in scans, str(scans))
    aim = FakeAim([70.0])
    aim.range_m = 2.0
    scans, _, _ = run_combo(aim, action=walker)
    check("walk-in: upright and too far, it walks in", w_key in scans, str(scans))

    # Never kick into theirs: wait until their kick can't hit any more, then go.
    waiting = dict(single, wait_out_kicks=True)
    aim = FakeAim([5.0])
    aim.range_m = 0.8
    aim.enemy = (llk, 0.3)  # their Left Leg Kick hits from 0.16 to 0.56 s
    scans, _, _ = run_combo(aim, action=waiting)
    check("wait out kicks: none thrown into theirs while it can still hit", scans == [], str(scans))
    aim = FakeAim([5.0])
    aim.range_m = 0.8
    aim.enemy = (llk, 0.9)
    scans, _, _ = run_combo(aim, action=waiting)
    check("wait out kicks: once theirs is spent, it kicks", scans[:1] == [key_of("shanghai_leg2")], str(scans))

    # A press during a back-off means attack now: it cuts the back-off short instead of being ignored.
    built = action_config(dict(poke, backoff_ms=1500))
    built.land = chain_land
    aim = FakeAim([5.0])
    aim.range_m = 0.8
    aim.script = [(0.25, 1, llk)]
    with Capture():
        player = rc.Player(keyboard, NullSession(), require_focus=False, aim=aim)
        player.fire(built.combos[0], built, "selftest")
        deadline = time.perf_counter() + 3.0
        while not player.backing_off and time.perf_counter() < deadline:
            time.sleep(0.01)
        was_backing = player.backing_off
        refired = player.fire(built.combos[0], built, "selftest")
        time.sleep(0.3)
        player.cancel.set()
        player.thread.join(2)
        time.sleep(0.05)
    check("back-off: a new press cuts it short and acts", was_backing and refired, f"backing {was_backing} refired {refired}")
    check("back-off: nothing left held after the cut", not keyboard.held, str(keyboard.held))

    # Strikes wait for the clip to end, because that's when they land.
    aim = FakeAim([5.0])
    aim.range_m = 0.8
    aim.move_at = lambda t: (llk, 0.7 + t) if t < 0.4 else (0, 0.0)
    aim.idle_at = lambda t: t - 0.4
    scans, times, _ = run_combo(aim, action=dict(single, open_wait_ms=1500), land=idle_land)
    kick = key_of("shanghai_leg2")
    check("idle first: lets the clip end and the robot settle, then strikes",
          kick in scans and 0.65 < times[scans.index(kick)] < 0.85, f"{scans} {[round(t, 2) for t in times]}")

    aim = FakeAim([5.0])
    aim.range_m = 0.8
    aim.idle_at = lambda t: 0.1 + t
    scans, times, _ = run_combo(aim, action=single, land=idle_land)
    check("idle first: a clip that only just ended still waits to settle", scans == [kick] and 0.17 < times[0] < 0.3, f"{times}")

    chained = dict(combo_action, moves=["LLK", "HRSK", "big_kick"])
    only_big = dict(combo_action, moves=["big_kick"], max_strikes=1)
    ljab = game.move_ids["shanghai_arm32"]

    # A kick can't be cut: the game ignores a press during one, so none goes, whatever the clock says.
    aim = FakeAim([5.0], move=(llk, 1.0))
    aim.range_m = 0.8
    aim.chain.moves, aim.chain.last_hit = (hrsk,), aim.began
    scans, _, _ = run_combo(aim, action=dict(chained, max_strikes=1), join_s=0.3, land=idle_land)
    check("kicks can't be cut: no strike during one", [s for s in scans if s != rc.SCAN["R"]] == [], str(scans))
    aim = FakeAim([5.0], move=(llk, 1.0))
    aim.range_m = 0.8
    aim.chain.moves, aim.chain.last_hit = (hrsk,), aim.began - 3.4
    scans, _, _ = run_combo(aim, action=only_big, join_s=0.3, land=idle_land)
    check("kicks can't be cut: not even with the 4 s running out", [s for s in scans if s != rc.SCAN["R"]] == [], str(scans))
    aim = FakeAim([5.0], move=(ljab, 0.9))
    aim.range_m = 0.8
    aim.chain.moves, aim.chain.last_hit = (hrsk,), aim.began
    scans, _, _ = run_combo(aim, action=dict(chained, max_strikes=1), join_s=0.3, land=idle_land)
    check("a kick cuts a punch at once, without waiting to settle", scans[:1] == [kick], str(scans))

    aim = FakeAim([5.0])
    aim.range_m = 0.8
    aim.idle_at = lambda t: 0.01
    aim.chain.moves, aim.chain.last_hit = (llk,), aim.began - 3.4
    scans, _, _ = run_combo(aim, action=only_big, join_s=0.3, land=idle_land)
    check("idle first: with the 4 s running out, it doesn't wait to settle", key_of("big_kick") in scans, str(scans))

    patient = dict(combo_action, openers=["big_kick"], open_wait_ms=1500, max_strikes=1, move_min_idle_ms={"big_kick": 600})
    aim = FakeAim([5.0])
    aim.range_m = 0.8
    aim.idle_at = lambda t: t
    scans, times, _ = run_combo(aim, action=patient)
    check("move idle: a move that needs longer waits for it", scans == [key_of("big_kick")] and 0.55 < times[0] < 0.75, f"{times}")
    aim = FakeAim([5.0])
    aim.range_m = 0.8
    aim.idle_at = lambda t: t
    scans, times, _ = run_combo(aim, action=dict(patient, openers=["big_kick", "LLK"]))
    check("move idle: meanwhile another move can go", scans == [key_of("shanghai_leg2")] and 0.27 < times[0] < 0.45, f"{scans} {times}")
    aim = FakeAim([5.0])
    aim.range_m = 0.8
    aim.idle_at = lambda t: 0.01
    aim.chain.moves, aim.chain.last_hit = (llk,), aim.began - 3.4
    scans, _, _ = run_combo(aim, action=dict(only_big, move_min_idle_ms={"big_kick": 600}), join_s=0.3, land=idle_land)
    check("move idle: with the 4 s running out, it doesn't wait", key_of("big_kick") in scans, str(scans))

    # Moves are listed shortest clip first, and the first good enough goes.
    both = dict(combo_action, openers=["LLK", "big_kick"], max_strikes=1)
    aim = FakeAim([5.0])
    aim.range_m = 0.8
    scans, _, _ = run_combo(aim, action=both, land=idle_land)
    check("pick: the first move good enough goes, even when a later one rates higher", scans == [kick], str(scans))
    aim = FakeAim([5.0])
    aim.range_m = 2.0
    scans, _, _ = run_combo(aim, action=both, land=idle_land)
    check("pick: a first move that isn't good enough is passed over", scans == [key_of("big_kick")], str(scans))
    aim = FakeAim([5.0])
    aim.range_m = 0.5
    scans, _, _ = run_combo(aim, action=dict(combo_action, max_strikes=1), land=idle_land)
    check("pick: when none is good enough, the best of them goes", scans == [key_of("flying_side_kick")], str(scans))

    capped = dict(both, move_max_dist={"big_kick": 1.7})
    aim = FakeAim([5.0])
    aim.range_m = 2.0
    scans, _, _ = run_combo(aim, action=capped, land=idle_land)
    check("pick: a move past its reach is passed over", scans == [kick], str(scans))

    # Each kick lands from its own side of the line, so it waits for that side.
    sided = dict(single, open_min=0.05, open_wait_ms=800, move_aim={"LLK": [0, 35]}, openers=["LLK"])
    aim = FakeAim([-10.0])
    aim.range_m = 0.8
    scans, _, _ = run_combo(aim, action=sided)
    check("aim window: from the wrong side the kick holds", scans == [], str(scans))
    aim = FakeAim([-10.0])
    aim.range_m = 0.8
    aim.aim_at = lambda t: -10.0 if t < 0.2 else 20.0
    scans, times, _ = run_combo(aim, action=sided)
    check("aim window: it goes once the robot comes round", scans == [kick] and 0.18 < times[0] < 0.35, f"{scans} {times}")
    aim = FakeAim([40.0])
    aim.range_m = 0.8
    scans, _, _ = run_combo(aim, action=dict(sided, max_aim_deg=60))
    check("aim window: past its far edge too", scans == [], str(scans))
    aim = FakeAim([5.0])
    aim.range_m = 0.5
    scans, _, _ = run_combo(aim, action=dict(single, openers=["flying_side_kick"], open_wait_ms=300, move_min_dist={"flying_side_kick": 1.3}), land=idle_land)
    check("min reach: a long-range move holds when too close", scans == [], str(scans))

    # A slow long-range kick doesn't go while the gap is opening.
    no_chase = dict(single, openers=["flying_side_kick"], open_wait_ms=300, move_min_closing={"flying_side_kick": -0.2})
    aim = FakeAim([5.0])
    aim.range_m = 2.0
    aim.closing_speed = -0.5
    scans, _, _ = run_combo(aim, action=no_chase)
    check("closing: a long-range kick holds while the gap opens", scans == [], str(scans))
    aim = FakeAim([5.0])
    aim.range_m = 2.0
    aim.closing_speed = 0.1
    scans, _, _ = run_combo(aim, action=no_chase)
    check("closing: it goes once the gap holds or closes", scans == [key_of("flying_side_kick")], str(scans))
    aim = FakeAim([5.0])
    aim.range_m = 2.0
    aim.closing_speed = None
    scans, _, _ = run_combo(aim, action=no_chase)
    check("closing: without the data it holds", scans == [], str(scans))
    live = rc.Aim()
    now = time.perf_counter()
    live.state, live.stamp = {"distance": 1.0}, now
    live.gaps.extend([(now - 0.8, 2.0), (now - 0.4, 1.4), (now - 0.1, 1.05)])
    check("closing: measured over about 0.4 s", 0.9 < live.closing() < 1.1, str(live.closing()))
    live.gaps.clear()
    live.gaps.extend([(now - 0.1, 1.05)])
    check("closing: too short a span gives nothing", live.closing() is None)

    # The chain walks back in, and leaves that to the pilot when the stick is pushed.
    walker = dict(single, walk_to_m=0.9, openers=["LLK"], open_wait_ms=300)
    w_key = rc.SCAN["W"]
    aim = FakeAim([70.0])
    aim.range_m = 1.5
    scans, _, _ = run_combo(aim, action=walker)
    check("walk: out of range it holds W", w_key in scans, str(scans))
    check("walk: W is let go when the chain ends", "W" not in keyboard.held and keyboard.held == [])
    aim = FakeAim([70.0])
    aim.range_m = 0.8
    scans, _, _ = run_combo(aim, action=walker)
    check("walk: in range it doesn't", w_key not in scans, str(scans))
    aim = FakeAim([70.0])
    aim.range_m = 1.5
    scans, _, _ = run_combo(aim, action=walker, player_setup=lambda p: setattr(p, "stick_pushed", True))
    check("walk: not while the pilot steers", w_key not in scans, str(scans))

    # Left Leg Kick first. Walking in stretches its reach; close in it goes from further left.
    sure = FixedShots({"shanghai_leg2": 0.5})
    reach = dict(single, use_model=True, openers=["LLK"], moves=["LLK"], open_wait_ms=300, min_chance=0.35,
                 move_max_dist={"LLK": 1.25}, move_max_dist_moving={"LLK": 1.55}, moving_min_mps=0.45)
    aim = FakeAim([5.0])
    aim.range_m = 1.45
    scans, _, _ = run_combo(aim, action=reach, shots=sure)
    check("reach: standing at 1.45 m the kick holds", scans == [], str(scans))
    aim = FakeAim([5.0])
    aim.range_m = 1.45
    aim.own_speed = 0.8
    scans, _, _ = run_combo(aim, action=reach, shots=sure)
    check("reach: walking in at 1.45 m it goes", scans == [kick], str(scans))
    aim = FakeAim([5.0])
    aim.range_m = 1.7
    aim.own_speed = 0.8
    scans, _, _ = run_combo(aim, action=reach, shots=sure)
    check("reach: walking in at 1.7 m it still holds", scans == [], str(scans))
    sided_near = dict(reach, move_aim={"LLK": [-10, 25]}, move_aim_near={"LLK": [-20, 30]}, near_m=1.15)
    aim = FakeAim([-15.0])
    aim.range_m = 0.9
    scans, _, _ = run_combo(aim, action=sided_near, shots=sure)
    check("near window: close in, -15 deg goes", scans == [kick], str(scans))
    aim = FakeAim([-15.0])
    aim.range_m = 1.2
    scans, _, _ = run_combo(aim, action=sided_near, shots=sure)
    check("near window: further out, -15 deg holds", scans == [], str(scans))

    # Positioning: never a sitting duck in their reach without a shot.
    s_key, d_key, a_key = rc.SCAN["S"], rc.SCAN["D"], rc.SCAN["A"]
    walking = {w_key, s_key, d_key, a_key}
    placed = dict(reach, max_aim_deg=60, wait_out_kicks=True, move_aim={"LLK": [-10, 25]},
                  position={"close_m": 0.9, "hover_m": 1.65, "kick_reach_m": 1.6, "approach_aim": [-20, 30]})
    aim = FakeAim([5.0])
    aim.range_m = 2.0
    scans, _, _ = run_combo(aim, action=placed, shots=sure)
    check("position: lined up and out of reach, it steps in", w_key in scans and s_key not in scans, str(scans))
    check("position: the walk is let go when the chain ends", not any(k in keyboard.held for k in "WASD"), str(keyboard.held))
    aim = FakeAim([50.0])
    aim.range_m = 1.0
    scans, _, _ = run_combo(aim, action=placed, shots=sure)
    check("position: no shot in their reach, it backs out straight back", s_key in scans and d_key not in scans and w_key not in scans, str(scans))
    aim = FakeAim([50.0])
    aim.range_m = 2.0
    scans, _, _ = run_combo(aim, action=placed, shots=sure)
    check("position: no shot but out of their reach, it stands and lets Auto Turning line up", not walking & set(scans), str(scans))
    aim = FakeAim([5.0])
    aim.range_m = 1.0
    aim.enemy = (llk, 0.3)
    scans, _, _ = run_combo(aim, action=placed, shots=sure)
    check("position: their kick live in reach, it gets out and doesn't kick into it", s_key in scans and kick not in scans, str(scans))
    aim = FakeAim([5.0])
    aim.range_m = 2.0
    aim.enemy = (llk, 1.2)
    scans, _, _ = run_combo(aim, action=placed, shots=sure)
    check("position: their kick spent and lined up, it walks in for the counter", w_key in scans and s_key not in scans, str(scans))
    aim = FakeAim([50.0])
    aim.range_m = 2.0
    aim.enemy = (llk, 1.2)
    scans, _, _ = run_combo(aim, action=placed, shots=sure)
    check("position: their kick spent but not lined up, it doesn't walk in", w_key not in scans, str(scans))
    aim = FakeAim([5.0])
    aim.range_m = 1.1
    scans, _, _ = run_combo(aim, action=dict(placed, position=dict(placed["position"], close_m=1.25)), shots=FixedShots({"shanghai_leg2": 0.1}))
    check("position: lined up inside 1.25 m with no shot, it backs out instead of walking deeper", s_key in scans and w_key not in scans, str(scans))

    # Finisher hunt: with two different hits in, it walks in to Big Kick's range when lined up; with no shot there it backs
    # out like after any hit (never a sitting duck: a clean hit on me wipes the combo).
    hunter = dict(placed, moves=["LLK", "big_kick"], move_min_used={"big_kick": 2}, move_aim={"LLK": [-10, 25], "big_kick": [-15, 45]},
                  position=dict(placed["position"], close_m=1.25), finish_chance=0.3, finish_hunt={"close_m": 1.1})
    no_finish = FixedShots({"shanghai_leg2": 0.1, "big_kick": 0.1})

    def one_from_combo(range_m, left_s, enemy=None):
        aim = FakeAim([5.0])
        aim.range_m = range_m
        if enemy:
            aim.enemy = enemy
        aim.chain.moves, aim.chain.last_hit = (hrsk, llk), aim.began - (rc.ComboChain.GAP_S - left_s)
        return aim

    scans, _, _ = run_combo(one_from_combo(1.2, 3.0), action=hunter, shots=no_finish, join_s=1.2)
    check("finish hunt: one hit from a combo at 1.2 m, it walks in to Big Kick range", w_key in scans and s_key not in scans, str(scans))
    scans, _, _ = run_combo(one_from_combo(1.0, 3.0), action=hunter, shots=no_finish, join_s=1.2)
    check("finish hunt: in Big Kick range with no shot yet, it backs out instead of standing there", s_key in scans, str(scans))
    scans, _, _ = run_combo(one_from_combo(1.0, 3.0), action=dict(hunter, finish_hunt=None), shots=no_finish, join_s=1.2)
    check("finish hunt: switched off, the same spot backs out as before", s_key in scans, str(scans))
    scans, _, _ = run_combo(one_from_combo(1.0, 3.0, enemy=(llk, 0.3)), action=hunter, shots=no_finish, join_s=1.2)
    check("finish hunt: their kick live in reach, it still dodges and doesn't kick into it", s_key in scans and key_of("big_kick") not in scans, str(scans))
    scans, _, _ = run_combo(one_from_combo(1.0, 0.9), action=dict(hunter, last_min=0.35), shots=FixedShots({"big_kick": 0.32}), join_s=1.0)
    check("finish bar: the last second never raises the finisher's bar", scans[:1] == [key_of("big_kick")], str(scans))
    scans, _, _ = run_combo(one_from_combo(1.0, 3.0), action=dict(hunter, finish_chance=0.4), shots=FixedShots({"big_kick": 0.32}), join_s=1.0)
    check("finish bar: under the finisher's bar it holds", key_of("big_kick") not in scans, str(scans))

    class RecSession:
        def __init__(self):
            self.events = []

        def event(self, kind, **fields):
            self.events.append(kind)

    built = action_config(dict(placed, backoff_ms=300, backoff_until_m=1.8))
    built.land = chain_land
    built.shots = FixedShots({"shanghai_leg2": 0.1})
    aim = FakeAim([5.0])
    aim.range_m = 1.0
    rec = RecSession()
    with Capture():
        player = rc.Player(keyboard, rec, require_focus=False, aim=aim)
        player.fire(built.combos[0], built, "selftest")
        player.thread.join(3)
        player.cancel.set()
        player.thread.join(2)
        time.sleep(0.05)
    check("position: a press that finds no shot backs out after it", "held" in rec.events and "backoff" in rec.events
          and rec.events.index("backoff") > rec.events.index("held"), str(rec.events))
    check("position: nothing held after that back-off", not keyboard.held, str(keyboard.held))

    # Fire on where it faces now as well as where the turn should take it.
    aim = FakeAim([-25.0])
    aim.range_m = 0.9
    aim.lead_shift = -20.0  # facing -25 now; predicted -5 once the turn carries on
    scans, _, _ = run_combo(aim, action=placed, shots=sure)
    check("aim now: without the check, it fires on the predicted turn", scans[:1] == [kick], str(scans))
    aim = FakeAim([-25.0])
    aim.range_m = 0.9
    aim.lead_shift = -20.0
    scans, _, _ = run_combo(aim, action=dict(placed, aim_now_too=True), shots=sure)
    check("aim now: with it, it waits until it really faces them", kick not in scans, str(scans))
    aim = FakeAim([-5.0])
    aim.range_m = 0.9
    scans, _, _ = run_combo(aim, action=dict(placed, aim_now_too=True), shots=sure)
    check("aim now: lined up now and later, it fires", scans[:1] == [kick], str(scans))

    # A robot walking in gets kicked first: Left Leg Kick from further out, Big Kick close in and from wider angles.
    rusher = dict(placed, rush={"min_approach_mps": 0.4, "max_dist": 1.5, "close_m": 1.1, "moves": ["LLK", "big_kick"],
                                "close_moves": ["big_kick", "LLK"],
                                "limits": {"LLK": {"aim": [-12, 30], "max_dist": 1.5}, "big_kick": {"aim": [-20, 60], "max_dist": 1.3}}})
    aim = FakeAim([5.0])
    aim.range_m = 1.4
    scans, _, _ = run_combo(aim, action=rusher, shots=sure)
    check("rush: standing still at 1.4 m, no kick (out of standing reach)", kick not in scans, str(scans))
    aim = FakeAim([5.0])
    aim.range_m = 1.4
    aim.them_speed = 0.7
    scans, _, _ = run_combo(aim, action=rusher, shots=sure)
    check("rush: it walks in at 1.4 m, Left Leg Kick goes first", scans[:1] == [kick], str(scans))
    aim = FakeAim([45.0])
    aim.range_m = 0.95
    aim.them_speed = 0.7
    scans, _, _ = run_combo(aim, action=rusher, shots=sure)
    check("rush: close and off to the right, Big Kick", scans[:1] == [key_of("big_kick")], str(scans))
    aim = FakeAim([45.0])
    aim.range_m = 1.4
    aim.them_speed = 0.7
    scans, _, _ = run_combo(aim, action=rusher, shots=sure)
    check("rush: Big Kick not past 1.3 m, Left Leg Kick not past 30 deg", key_of("big_kick") not in scans and kick not in scans, str(scans))
    aim = FakeAim([5.0])
    aim.range_m = 1.0
    aim.them_speed = 0.7
    aim.up_for = 0.0
    scans, _, _ = run_combo(aim, action=dict(rusher, foul_guard_ms=350), shots=sure)
    check("rush: never on a robot that is down", kick not in scans and key_of("big_kick") not in scans, str(scans))

    # A fast charger is met further out with Left Leg Kick; Big Kick's reach stays the same.
    fast_rusher = dict(rusher, rush=dict(rusher["rush"], fast_mps=0.6, fast_max_dist=1.7,
                                         limits=dict(rusher["rush"]["limits"], LLK={"aim": [-12, 30], "max_dist": 1.5, "fast_max_dist": 1.7})))
    aim = FakeAim([5.0])
    aim.range_m = 1.65
    aim.them_speed = 0.8
    scans, _, _ = run_combo(aim, action=fast_rusher, shots=sure)
    check("early rush: a fast charger at 1.65 m gets Left Leg Kick first", scans[:1] == [kick], str(scans))
    aim = FakeAim([5.0])
    aim.range_m = 1.65
    aim.them_speed = 0.5
    scans, _, _ = run_combo(aim, action=fast_rusher, shots=sure)
    check("early rush: a slower walk-in at 1.65 m still waits", kick not in scans, str(scans))
    aim = FakeAim([5.0])
    aim.range_m = 1.65
    aim.them_speed = 0.8
    scans, _, _ = run_combo(aim, action=rusher, shots=sure)
    check("early rush: switched off, 1.65 m waits as before", kick not in scans, str(scans))
    aim = FakeAim([45.0])
    aim.range_m = 1.4
    aim.them_speed = 0.8
    scans, _, _ = run_combo(aim, action=fast_rusher, shots=sure)
    check("early rush: Big Kick's reach doesn't grow", key_of("big_kick") not in scans and kick not in scans, str(scans))

    # Back off from a charger too: standing in their reach gets the robot hit and wipes the combo. An old stand_vs_charge
    # key left in combos.json changes nothing.
    charge = dict(placed, position=dict(placed["position"], close_m=1.25), stand_vs_charge={"min_approach_mps": 0.4})
    no_llk = FixedShots({"shanghai_leg2": 0.1})
    aim = FakeAim([5.0])
    aim.range_m = 1.0
    aim.them_speed = 0.7
    scans, _, _ = run_combo(aim, action=charge, shots=no_llk, join_s=1.0)
    check("back off: a charger at 1.0 m and no shot, it backs out", s_key in scans, str(scans))
    aim = FakeAim([5.0])
    aim.range_m = 1.0
    scans, _, _ = run_combo(aim, action=charge, shots=no_llk, join_s=1.0)
    check("back off: one standing still at 1.0 m gets backed away from", s_key in scans, str(scans))
    aim = FakeAim([5.0])
    aim.range_m = 1.0
    aim.them_speed = 0.7
    aim.enemy = (llk, 0.3)
    scans, _, _ = run_combo(aim, action=charge, shots=no_llk, join_s=1.0)
    check("back off: their kick live as they charge, it dodges and doesn't kick into it", s_key in scans and kick not in scans, str(scans))
    built = action_config(dict(placed, backoff_ms=300, backoff_until_m=1.8, stand_vs_charge={"min_approach_mps": 0.4}))
    built.land = chain_land
    built.shots = FixedShots({"shanghai_leg2": 0.1})
    aim = FakeAim([5.0])
    aim.range_m = 1.0
    aim.them_speed = 0.7
    rec = RecSession()
    with Capture() as capture:
        player = rc.Player(keyboard, rec, require_focus=False, aim=aim)
        player.fire(built.combos[0], built, "selftest")
        player.thread.join(3)
        player.cancel.set()
        player.thread.join(2)
        time.sleep(0.05)
    scans = [scan for _, scan, down in capture.events if down and scan != shift]
    check("back off: a press with no shot backs out after it while they charge as well",
          "backoff" in rec.events and "backoff_skipped" not in rec.events and s_key in scans, f"{rec.events} {scans}")

    # LLK first: in its spot (angle, and the distance it will meet them at) it goes whatever the model says.
    first = dict(placed, llk_first={"move": "LLK", "aim": [-10, 20], "contact_m": [0.6, 1.2], "lead_s": 0.45, "chance": 0.62})
    aim = FakeAim([5.0])
    aim.range_m = 1.1
    scans, _, _ = run_combo(aim, action=first, shots=no_llk)
    check("llk first: lined up at 1.1 m and the model says 10%, it kicks anyway", scans[:1] == [kick], str(scans))
    aim = FakeAim([5.0])
    aim.range_m = 1.1
    scans, _, _ = run_combo(aim, action=placed, shots=no_llk, join_s=1.0)
    check("llk first: switched off, the same spot waits on the model", kick not in scans, str(scans))
    aim = FakeAim([5.0])
    aim.range_m = 1.5
    scans, _, _ = run_combo(aim, action=first, shots=no_llk, join_s=1.0)
    check("llk first: standing at 1.5 m it would meet them too far out, no kick", kick not in scans, str(scans))
    aim = FakeAim([5.0])
    aim.range_m = 1.5
    aim.closing_speed = 1.0
    scans, _, _ = run_combo(aim, action=first, shots=no_llk)
    check("llk first: 1.5 m with the gap closing 1 m/s meets them at 1.05 m, it kicks", scans[:1] == [kick], str(scans))
    aim = FakeAim([30.0])
    aim.range_m = 1.1
    scans, _, _ = run_combo(aim, action=first, shots=no_llk, join_s=1.0)
    check("llk first: 30 deg off is the wrong angle, no kick", kick not in scans, str(scans))
    aim = FakeAim([5.0])
    aim.range_m = 1.1
    aim.enemy = (llk, 0.3)
    scans, _, _ = run_combo(aim, action=first, shots=no_llk, join_s=1.0)
    check("llk first: their Left Leg Kick already out, it dodges instead (second LLK lands 1 in 10)", s_key in scans and kick not in scans, str(scans))
    aim = FakeAim([5.0])
    aim.range_m = 1.1
    aim.enemy = (big, 0.3)
    scans, _, _ = run_combo(aim, action=first, shots=no_llk)
    check("llk first: their slower kick on its way, LLK in its spot still goes", scans[:1] == [kick], str(scans))
    # Line up: LLK ready, in its reach, a few degrees outside its window: stand still for Auto Turning, then back off.
    liner = dict(first, open_wait_ms=2000, llk_first=dict(first["llk_first"], line_up_deg=20, line_up_m=1.3, line_up_s=0.5))
    aim = FakeAim([-20.0])
    aim.range_m = 0.9
    scans, _, _ = run_combo(aim, action=liner, shots=no_llk, join_s=0.35)
    check("line up: 10 deg outside the window at 0.9 m, it stands still instead of backing out", not walking & set(scans) and kick not in scans, str(scans))
    aim = FakeAim([-20.0])
    aim.range_m = 0.9
    scans, _, _ = run_combo(aim, action=liner, shots=no_llk, join_s=1.2)
    check("line up: still not lined up after line_up_s, it backs out", s_key in scans and kick not in scans, str(scans))
    aim = FakeAim([-60.0])
    aim.range_m = 0.9
    scans, _, _ = run_combo(aim, action=liner, shots=no_llk, join_s=0.35)
    check("line up: far off the window, it backs out at once", s_key in scans, str(scans))
    # Their Left Leg Kick counts as live until their_live_s, past its hit window (mine into it: 0/4 at 0.5-0.9 s, 4/5 from 1.1 s).
    built = rc.Config({"settings": {"their_live_s": {"LLK": 1.0}}, "combos": []}, game)
    check("their live: the setting reads by move", built.their_live_s == {"shanghai_leg2": 1.0}, str(built.their_live_s))
    probe = rc.Player(keyboard, NullSession(), require_focus=False, aim=FakeAim([5.0]))
    probe.aim.enemy = (llk, 0.7)
    check("their live: their LLK 0.7 s in is still live", probe._shot_setup(built)["them_state"] == "kick_active", str(probe._shot_setup(built)))
    probe.aim.enemy = (llk, 1.2)
    check("their live: 1.2 s in it's spent", probe._shot_setup(built)["them_state"] == "kick_spent", str(probe._shot_setup(built)))
    aim = FakeAim([30.0])
    aim.range_m = 1.1
    aim.enemy = (llk, 0.3)
    scans, _, _ = run_combo(aim, action=first, shots=no_llk, join_s=1.0)
    check("llk first: their kick on its way and LLK out of its spot, it dodges and doesn't kick", s_key in scans and kick not in scans, str(scans))

    # Hold to arm: while the pad button that fired it stays held, the chain stays armed; letting go ends it.
    def run_held(action, aim, hold_s, shots, hold=True):
        built = rc.Config({"combos": [{"name": "held", "pad": "l1", "interrupt": True, "action": action}]}, game)
        built.table = table
        built.land = chain_land
        built.shots = shots
        combo = built.combos[0]
        with Capture() as capture:
            player = rc.Player(keyboard, NullSession(), require_focus=False, aim=aim)
            player.pad_now = combo.pad_mask if hold else 0
            begun = time.perf_counter()
            player.fire(combo, built, combo.pad)
            time.sleep(hold_s)
            alive_held = player.thread.is_alive()
            player.pad_now = 0
            player.thread.join(1.5)
            alive_after = player.thread.is_alive()
            player.cancel.set()
            player.thread.join(2)
            time.sleep(0.05)
        scans = [scan for t, scan, down in capture.events if down and scan != shift]
        return scans, alive_held, alive_after

    holder = dict(placed, open_wait_ms=300, opens=1, hold_to_arm=True)
    aim = FakeAim([50.0])
    aim.range_m = 2.0
    _, alive_held, alive_after = run_held(holder, aim, 1.0, FixedShots({"shanghai_leg2": 0.1}))
    check("hold to arm: held 1 s with no shot (open wait 0.3 s), still armed", alive_held, str(alive_held))
    check("hold to arm: let go, it ends", not alive_after, str(alive_after))
    aim = FakeAim([50.0])
    aim.range_m = 2.0
    _, alive_held, _ = run_held(dict(holder, hold_to_arm=False), aim, 1.0, FixedShots({"shanghai_leg2": 0.1}))
    check("hold to arm: switched off, a held button doesn't keep it armed", not alive_held, str(alive_held))
    aim = FakeAim([5.0])
    aim.range_m = 1.0
    scans, _, _ = run_held(holder, aim, 1.8, sure)
    check("hold to arm: a missed opener doesn't end it while held, it kicks again", scans.count(kick) >= 2, str(scans))
    aim = FakeAim([5.0])
    aim.range_m = 1.0
    scans, _, _ = run_held(holder, aim, 1.8, sure, hold=False)
    check("hold to arm: a tap still throws just the one opener", scans.count(kick) == 1, str(scans))

    # Stuck in Recovery: the rescue presses its steps in order and stops once the robot stands.
    three, one, b_key, m_key = rc.SCAN["Digit3"], rc.SCAN["Digit1"], rc.SCAN["B"], rc.SCAN["M"]
    check("rescue: the build has Passive Mode, Straighten, Get Up and lie down",
          {"passive", "straighten", "get_up", "lie_down"} <= set(game.rescue_chords), str(game.rescue_chords))
    expect_rejected("rescue: an unknown step is refused", game,
                    {"combos": [{"name": "r", "key": "f10", "action": {"kind": "rescue", "steps": [["fly", 1.0]]}}]})
    stuck = {"kind": "rescue", "steps": [["straighten", 0.3], ["get_up", 0.3]]}
    aim = FakeAim([5.0], move=(33, 0.0))
    aim.down_ids = frozenset({33})
    aim.tilt_deg = 80.0
    scans, _, _ = run_combo(aim, action=stuck)
    check("rescue: still down, it sends Straighten then Get Up", scans == [three, one], str(scans))
    aim = FakeAim([5.0])
    aim.down_ids = frozenset({33})
    aim.tilt_deg = 10.0
    aim.move_at = lambda t: (33, 0.0) if t < 0.15 else (0, 0.0)
    scans, _, finished = run_combo(aim, action=stuck)
    check("rescue: once it stands, the rest is skipped", scans == [three] and finished < 0.5, f"{scans} {finished:.2f} s")
    aim = FakeAim([5.0], move=(33, 0.0))
    aim.down_ids = frozenset({33})
    aim.tilt_deg = 80.0
    scans, _, _ = run_combo(aim, action={"kind": "rescue", "steps": [["passive", 0.1], ["passive", 0.1]]})
    check("rescue: Passive Mode is Shift+B, twice to cycle it", scans == [b_key, b_key], str(scans))
    aim = FakeAim([5.0], move=(33, 0.0))
    aim.down_ids = frozenset({33})
    aim.tilt_deg = 80.0
    scans, _, _ = run_combo(aim, action={"kind": "rescue", "lie_down_first": True, "settle_s": 0.1, "steps": [["get_up", 0.1]]})
    check("rescue test: lies down (Ctrl+M) first, then the steps", [s for s in scans if s != rc.SCAN["LeftCtrl"]] == [m_key, one]
          and rc.SCAN["LeftCtrl"] in scans, str(scans))
    check("rescue: nothing left held", not keyboard.held, str(keyboard.held))
    built = rc.Config({"settings": {"rescue": {"auto": True, "stuck_s": 6.5, "steps": [["get_up", 1.0]]}}, "combos": []}, game)
    check("rescue: settings build the automatic rescue", built.rescue_auto and built.rescue_stuck_s == 6.5 and built.rescue_combo
          and built.rescue_combo.interrupt and built.rescue_combo.action["kind"] == "rescue")
    check("rescue: on unless switched off (never off: the pilot's call, 11-10)", rc.Config({"combos": []}, game).rescue_auto is True)

    # Get Up spam: from the moment the robot starts to go over, Get Up again and again until it stands.
    built = rc.Config({"settings": {"rescue": {"spam": {"tilt_deg": 50, "every_s": 0.1, "max_s": 4.0}}}, "combos": []}, game)
    check("get-up spam: settings build it", bool(built.rescue_spam) and built.rescue_spam.interrupt and built.rescue_tilt_deg == 50
          and built.rescue_spam.action["spam"] == "get_up" and built.rescue_spam.action["every_s"] == 0.1)
    expect_rejected("get-up spam: an unknown chord is refused", game,
                    {"settings": {"rescue": {"spam": {"chord": "fly"}}}, "combos": []})
    spam = {"kind": "rescue", "spam": "get_up", "every_s": 0.05, "max_s": 0.6}
    aim = FakeAim([5.0], move=(33, 0.0))
    aim.down_ids = frozenset({33})
    aim.tilt_deg = 80.0
    scans, _, _ = run_combo(aim, action=spam)
    check("get-up spam: going over, it presses Get Up again and again", len(scans) >= 3 and set(scans) == {one}, str(scans))
    aim = FakeAim([5.0])
    aim.down_ids = frozenset({33})
    aim.tilt_deg = 10.0
    aim.move_at = lambda t: (33, 0.0) if t < 0.15 else (0, 0.0)
    scans, _, finished = run_combo(aim, action=spam)
    check("get-up spam: it stops once the robot stands", 1 <= len(scans) <= 3 and finished < 0.5, f"{scans} {finished:.2f} s")
    getup = game.move_ids["liyudating_getup"]
    aim = FakeAim([5.0], move=(getup, 0.3))
    aim.down_ids = frozenset({33, getup})
    aim.tilt_deg = 80.0
    scans, _, _ = run_combo(aim, action=dict(spam, max_s=0.3))
    check("get-up spam: a get-up already playing is left to finish", scans == [], str(scans))
    check("get-up spam: nothing left held", not keyboard.held, str(keyboard.held))

    # Hitting a robot that is down or getting up gives it 3 points: no strike until it has stood 0.35 s, and no backing off meanwhile.
    guarded = dict(placed, foul_guard_ms=350)
    aim = FakeAim([5.0])
    aim.range_m = 0.9
    aim.up_for = 0.0
    scans, _, _ = run_combo(aim, action=guarded, shots=sure)
    check("foul guard: no strike while it's down", kick not in scans, str(scans))
    check("foul guard: and no backing off from a robot on the floor", s_key not in scans, str(scans))
    aim = FakeAim([5.0])
    aim.range_m = 0.9
    aim.up_for = 0.1
    scans, _, _ = run_combo(aim, action=guarded, shots=sure)
    check("foul guard: none just after it stands", kick not in scans, str(scans))
    aim = FakeAim([5.0])
    aim.range_m = 0.9
    aim.up_for = 0.6
    scans, _, _ = run_combo(aim, action=guarded, shots=sure)
    check("foul guard: once it has stood 0.35 s, it strikes", scans[:1] == [kick], str(scans))
    live = rc.Aim()
    live.down_ids = frozenset({33})
    check("foul guard: the live reader starts with no fall seen", live.enemy_up_for() == float("inf"))
    live.enemy_down = True
    check("foul guard: down reads as 0", live.enemy_up_for() == 0.0)
    live.enemy_down, live.enemy_up_at = False, time.perf_counter() - 0.2
    check("foul guard: up for about 0.2 s", 0.15 < live.enemy_up_for() < 0.3, str(live.enemy_up_for()))
    live.strike_ids = frozenset({15})
    strike_ids = rc.strike_move_ids(game)
    check("foul guard: their jump or spin kick leaning past 45 deg is not a fall", not live._enemy_is_down({"move": 15, "tilt": 60.0}))
    check("foul guard: leaning past 45 deg outside a strike is down", live._enemy_is_down({"move": 0, "tilt": 60.0}))
    check("foul guard: a Recovery move is down whatever the lean", live._enemy_is_down({"move": 33, "tilt": 10.0}))
    check("foul guard: the build's strike ids exclude Recovery and get-ups",
          bool(strike_ids) and game.move_ids["auto_recovery"] not in strike_ids and game.move_ids["shanghai_leg2"] in strike_ids, str(sorted(strike_ids)))
    aim = FakeAim([5.0])
    aim.range_m = 2.0
    scans, _, _ = run_combo(aim, action=placed, shots=sure, player_setup=lambda p: setattr(p, "stick_pushed", True))
    check("position: the pilot's stick wins, no walking keys", not walking & set(scans), str(scans))
    aim = FakeAim([5.0], move=(llk, 0.5))
    aim.range_m = 1.0
    scans, _, _ = run_combo(aim, action=placed, shots=sure)
    check("position: nothing while its own kick plays", not walking & set(scans), str(scans))
    aim = FakeAim([5.0])
    aim.range_m = 2.0
    aim.tilt_deg = 50.0
    scans, _, _ = run_combo(aim, action=placed, shots=sure)
    check("position: nothing while it's down", not walking & set(scans), str(scans))
    aim = FakeAim([180.0])
    aim.range_m = 1.0
    aim.facing_deg = 180.0  # turned right round: the other robot is behind it
    scans, _, _ = run_combo(aim, action=placed, shots=sure)
    check("position: turned away, backing out walks forward, away from them", w_key in scans and s_key not in scans, str(scans))
    aim = FakeAim([5.0])
    aim.range_at = lambda t: max(1.0, 2.0 - 0.9 * t)  # walking in at 0.9 m/s from 2 m
    aim.own_speed = 0.8
    scans, times, _ = run_combo(aim, action=dict(placed, open_wait_ms=1500), shots=sure)
    kicked_at = times[scans.index(kick)] if kick in scans else None
    check("position: the step-in kicks the moment it's in reach (W first, then the kick at about 1.55 m)",
          scans[:1] == [w_key] and kicked_at is not None and 0.42 < kicked_at < 0.65, f"{scans} {[round(t, 2) for t in times]}")
    aim = FakeAim([5.0])
    aim.range_m = 0.8
    scans, _, _ = run_combo(aim, action=dict(single, openers=["LLK", "big_kick"], move_min_used={"big_kick": 2}, open_min=0.05, move_aim={"LLK": [40, 50]}))
    check("finisher: Big Kick doesn't open", key_of("big_kick") not in scans, str(scans))
    aim = FakeAim([5.0])
    aim.range_m = 0.8
    aim.chain.moves, aim.chain.last_hit = (llk, hrsk), aim.began
    scans, _, _ = run_combo(aim, action=dict(single, moves=["big_kick"], move_min_used={"big_kick": 2}))
    check("finisher: Big Kick finishes", scans == [key_of("big_kick")], str(scans))
    aim = FakeAim([5.0])
    aim.range_m = 0.8
    aim.chain.moves, aim.chain.last_hit = (llk,), aim.began
    scans, _, _ = run_combo(aim, action=dict(single, moves=["flying_side_kick"], move_min_dist={"flying_side_kick": 1.3}, last_min=0.01, next_min=0.01))
    check("min reach: applies to an opener, not mid-chain", scans == [key_of("flying_side_kick")], str(scans))
    aim = FakeAim([5.0])
    aim.range_m = 0.8
    aim.chain.moves, aim.chain.last_hit = (llk, hrsk), aim.began - 3.2
    slow = dict(single, moves=["flying_side_kick"], last_min=0.01, next_min=0.01)
    scans, _, _ = run_combo(aim, action=dict(slow, move_hit_s={"flying_side_kick": 0.3}), join_s=0.3)
    check("hit time: a shorter known time lets a late strike go", scans == [key_of("flying_side_kick")], str(scans))

    # The shot model decides what to throw, and whether to throw at all.
    model_data = {"columns": ["LLK", "LLK|a", "LLK|a2", "LLK|d", "LLK|d2", "BigKick", "FlySide", "closing", "them_toward", "them_kick_active", "human", "idle"],
                  "weights": [0.0, 0.0, -1.0, 0.0, -1.0, 1.0, 1.0, 0.7, 0.0, -2.0, -0.5, 0.0],
                  "moves": {"shanghai_leg2": "LLK", "big_kick": "BigKick", "flying_side_kick": "FlySide"}, "lock_s": {"LLK": 1.88, "BigKick": 3.74, "FlySide": 2.96}, "time_value": 0.09}
    shots = rc.ShotModel(model_data)
    base = {"closing": 0.0, "them_toward": 0.0, "them_state": "free", "human": False, "idle": 1.0}
    centre = shots.chance("shanghai_leg2", 0.0, 1.1, base)
    check("shot model: 50-50 where its weights say", abs(centre - 0.5) < 1e-9, str(centre))
    check("shot model: off the line and out of reach are worse", shots.chance("shanghai_leg2", 40.0, 1.1, base) < centre and shots.chance("shanghai_leg2", 0.0, 1.8, base) < centre)
    check("shot model: a closing gap is better", shots.chance("shanghai_leg2", 0.0, 1.1, dict(base, closing=1.0)) > centre)
    check("shot model: an enemy mid-kick is worse", shots.chance("shanghai_leg2", 0.0, 1.1, dict(base, them_state="kick_active")) < centre)
    check("shot model: a real player is worse", shots.chance("shanghai_leg2", 0.0, 1.1, dict(base, human=True)) < centre)
    check("shot model: it knows nothing of a move it wasn't fit on", shots.chance("shanghai_arm33", 0.0, 1.1, base) == 0.0)
    check("shot model: a kick's lock comes from the file", shots.lock("big_kick") == 3.74 and shots.lock("shanghai_arm33") == 2.0)
    real = rc.ShotModel.load()
    real_chance = real.chance("shanghai_leg2", 10.0, 0.8, base)
    check("shot model: the fitted file loads and is sensible", 0.3 < real_chance < 0.9 and real.chance("shanghai_leg2", -60.0, 2.5, base) < 0.15, f"{real_chance:.2f}")
    modelled = dict(single, use_model=True, use_recovery=False, openers=["LLK"], moves=["LLK", "big_kick"], open_wait_ms=300, min_chance=0.35, finish_chance=0.2, combo_value=4.0)
    aim = FakeAim([0.0])
    aim.range_m = 1.1
    scans, _, _ = run_combo(aim, action=modelled, shots=shots)
    check("model pick: a good shot goes", scans == [kick], str(scans))
    aim = FakeAim([50.0])
    aim.range_m = 1.1
    scans, _, finished = run_combo(aim, action=dict(modelled, max_aim_deg=60), shots=shots)
    check("model pick: a poor shot holds fire", scans == [], str(scans))
    aim = FakeAim([0.0])
    aim.range_m = 1.1
    aim.enemy = (llk, 0.3)  # an enemy mid-kick
    scans, _, _ = run_combo(aim, action=modelled, shots=shots)
    check("model pick: it sees the enemy's kick and holds", scans == [], str(scans))
    aim = FakeAim([0.0])
    aim.range_m = 1.1
    aim.enemy = (game.move_ids["shanghai_arm32"], 0.3)  # a punch isn't a kick
    scans, _, _ = run_combo(aim, action=modelled, shots=shots)
    check("model pick: an enemy's punch doesn't stop it", scans == [kick], str(scans))
    aim = FakeAim([0.0])
    aim.range_m = 1.1
    aim.chain.moves, aim.chain.last_hit = (hrsk,), aim.began
    even = rc.ShotModel(dict(model_data, weights=[0.0, 0.0, -1.0, 0.0, -1.0, 0.0, 0.0, 0.7, 0.0, -2.0, -0.5, 0.0]))
    scans, _, _ = run_combo(aim, action=dict(modelled, moves=["big_kick", "LLK"]), shots=even)
    check("model pick: at equal odds, the shorter lock", scans[:1] == [kick], str(scans))
    aim = FakeAim([0.0])
    aim.range_m = 1.1
    aim.chain.moves, aim.chain.last_hit = (hrsk, llk), aim.began
    longshot = rc.ShotModel(dict(model_data, weights=[0.0, 0.0, -1.0, 0.0, -1.0, -1.1, 1.0, 0.7, 0.0, -2.0, -0.5, 0.0]))
    scans, _, _ = run_combo(aim, action=dict(modelled, moves=["big_kick"]), shots=longshot)
    check("model pick: a combo's third hit goes on long odds", scans == [key_of("big_kick")], str(scans))
    aim = FakeAim([0.0])
    aim.range_m = 1.1
    aim.chain.moves, aim.chain.last_hit = (hrsk,), aim.began
    scans, _, _ = run_combo(aim, action=dict(modelled, moves=["big_kick"], min_chance=0.45), shots=longshot, join_s=0.4)
    check("model pick: the same odds mid-chain hold", key_of("big_kick") not in scans, str(scans))
    aim = FakeAim([70.0], move=(big, 0.7))
    aim.range_m = 0.8
    scans, _, _ = run_combo(aim, action=modelled, shots=shots, join_s=0.3)
    check("no recovery: the chain doesn't tap R when told not to", scans == [], str(scans))
    live = rc.Aim()
    now = time.perf_counter()
    live.state = {"me": {"x": 0.0, "z": 0.0, "move": 0}, "them": {"x": 1.0, "z": 0.0, "move": 0, "elapsed": 0.0}, "human": True, "distance": 1.0}
    live.stamp = now
    live.enemy_track.extend([(now - 0.8, 2.0, 0.0), (now - 0.4, 1.4, 0.0)])
    check("enemy approach: measured toward me", 0.8 < live.enemy_approach() < 1.2, str(live.enemy_approach()))
    inputs = live.shot_inputs()
    check("shot inputs: carry the enemy and who they are", inputs["human"] is True and inputs["them_move"] == 0 and "closing" in inputs and "idle" in inputs)

    # A setup punch in reach, then the kick cuts it.
    jab = key_of("shanghai_arm32")
    setup = {"move": "LJab", "max_dist": 1.05, "max_aim_deg": 25}
    jab_then_kick = dict(combo_action, setup=setup, max_strikes=1)
    aim = FakeAim([5.0])
    aim.range_m = 0.8
    scans, times, _ = run_combo(aim, action=jab_then_kick)
    check("setup: in reach the jab goes first, then a kick cuts it as soon as the game allows",
          scans == [jab, kick] and 0.54 < times[1] - times[0] < 0.7, f"{scans} {[round(t, 2) for t in times]}")
    aim = FakeAim([5.0])
    aim.range_m = 2.0
    scans, _, _ = run_combo(aim, action=jab_then_kick)
    check("setup: out of its reach there's no jab", scans == [key_of("flying_side_kick")], str(scans))
    aim = FakeAim([28.0])
    aim.range_m = 0.8
    scans, _, _ = run_combo(aim, action=dict(jab_then_kick, open_min=0.05))
    check("setup: off its line there's no jab", scans == [kick], str(scans))
    left_only = dict(jab_then_kick, setup=dict(setup, min_aim_deg=-25, max_aim_deg=0), openers=["LLK"], open_min=0.05)
    aim = FakeAim([5.0])
    aim.range_m = 0.8
    scans, _, _ = run_combo(aim, action=left_only)
    check("setup: right of its line there's no jab", scans == [kick], str(scans))
    aim = FakeAim([-10.0])
    aim.range_m = 0.8
    scans, _, _ = run_combo(aim, action=left_only)
    check("setup: left of center the jab goes first", scans == [jab, kick], str(scans))
    aim = FakeAim([-28.0])
    aim.range_m = 0.8
    scans, _, _ = run_combo(aim, action=left_only)
    check("setup: too far left there's no jab", scans == [kick], str(scans))
    aim = FakeAim([5.0])
    aim.range_m = 0.8
    aim.chain.moves, aim.chain.last_hit = (hrsk,), aim.began - 3.4
    scans, _, _ = run_combo(aim, action=jab_then_kick, join_s=0.4)
    check("setup: with the 4 s running out it's straight to the kick", scans[:1] == [kick], str(scans))
    aim = FakeAim([5.0])
    aim.range_m = 0.8
    aim.chain.moves, aim.chain.last_hit = (ljab,), aim.began
    scans, _, _ = run_combo(aim, action=jab_then_kick)
    check("setup: a jab already in the chain isn't repeated", scans == [kick], str(scans))
    aim = FakeAim([5.0])
    aim.range_m = 0.8
    aim.script = [(0.4, 1, ljab), (0.9, 1, llk), (1.5, 4, hrsk)]
    scans, _, _ = run_combo(aim, action=dict(combo_action, setup=setup))
    check("setup: jab, kick, then a different kick makes the combo",
          scans == [jab, kick, key_of("shanghai_leg19")] and aim.chain.combos == 1, str(scans))

    # A press the game ignores isn't a strike.
    aim = FakeAim([5.0])
    aim.range_m = 0.8
    aim.drop_presses = 1
    scans, times, _ = run_combo(aim, action=single)
    check("dropped: a press the game ignores goes again, and isn't a miss",
          scans == [kick, kick] and 0.28 < times[1] - times[0] < 0.45, f"{scans} {[round(t, 2) for t in times]}")

    # A press keeps going after a missed opener, up to a limit.
    retries = dict(combo_action, opens=2)
    aim = FakeAim([5.0])
    aim.range_m = 0.8
    scans, _, finished = run_combo(aim, action=retries)
    check("opens: a missed opener gets another try, then stops", scans == [kick, kick] and finished < 2.5, f"{scans} {finished:.2f} s")
    aim = FakeAim([5.0])
    aim.range_m = 0.8
    aim.aim_at = lambda t: 5.0 if t < 0.3 else 70.0
    scans, _, finished = run_combo(aim, action=retries)
    check("opens: another try waits for its own shot, then gives up", scans == [kick] and 0.9 < finished < 1.6, f"{scans} {finished:.2f} s")

    # The real clock on the robot being idle.
    live = rc.Aim()
    live.state = {"me": {"move": 0}}
    live.stamp = time.perf_counter()
    live.idle_since = live.stamp - 0.5
    check("idle clock: counts from the end of the last clip", 0.45 < live.idle_for() < 1.0)
    live.state = {"me": {"move": 9}}
    check("idle clock: zero in a clip", live.idle_for() == 0.0)
    live.state, live.stamp = {"me": {"move": 0}}, time.perf_counter() - 1.0
    check("idle clock: zero without fresh data", live.idle_for() == 0.0)

    aim = FakeAim([70.0], move=(big, 0.7))  # the long clips are the ones worth ending
    aim.range_m = 0.8
    scans, _, _ = run_combo(aim, join_s=0.3)
    check("chain: a long spent clip with no shot gets Recovery, so the robot can turn", scans[:1] == [rc.SCAN["R"]], str(scans))
    aim = FakeAim([70.0], move=(hrsk, 0.9))
    aim.range_m = 0.8
    scans, _, _ = run_combo(aim, join_s=0.3)
    check("chain: never cancels High Right Side Kick", scans == [], str(scans))
    getup = game.move_ids["liyudating_getup"]
    aim = FakeAim([5.0], move=(getup, 0.9))
    aim.range_m = 0.8
    scans, _, _ = run_combo(aim, join_s=0.3)
    check("chain: leaves a get-up alone", scans == [], str(scans))

    # The game ignores most R taps, so they repeat, and they must never cost a strike.
    aim = FakeAim([70.0], move=(big, 0.6))
    aim.range_m = 0.8
    scans, _, _ = run_combo(aim, join_s=0.45)
    check("recovery: an ignored tap repeats", scans.count(rc.SCAN["R"]) >= 3, str(scans))
    aim = FakeAim([70.0])
    aim.range_m = 0.8
    aim.move_at = lambda t: (big, 0.6 + t) if t < 0.3 else (0, 0.0)
    aim.aim_at = lambda t: 70.0 if t < 0.2 else 5.0
    scans, times, _ = run_combo(aim, action=dict(combo_action, open_wait_ms=1000), join_s=1.0)
    check("recovery: ignored taps don't hold up the strike once the clip ends",
          kick in scans and 0.25 < times[scans.index(kick)] < 0.6, f"{scans} {[round(t, 2) for t in times]}")
    aim = FakeAim([5.0], move=(ljab, 0.5))
    aim.range_m = 0.8
    scans, _, _ = run_combo(aim, join_s=0.3)
    check("recovery: no tap while a shot is open and only the cooldown is in the way", scans == [], str(scans))
    aim = FakeAim([70.0], move=(llk, 0.7))
    aim.range_m = 0.8
    scans, _, _ = run_combo(aim, join_s=0.3)
    check("recovery: no tap on a short clip, which ends about as soon as Recovery would let the next move start", scans == [], str(scans))
    aim = FakeAim([70.0], move=(big, 0.3))
    aim.range_m = 0.8
    scans, _, _ = run_combo(aim, join_s=0.3)
    check("recovery: no tap inside the switch cooldown", scans == [], str(scans))
    tester = rc.Player(keyboard, NullSession(), require_focus=False, aim=FakeAim([5.0]))
    built = action_config(combo_action)
    built.land = chain_land
    tester.aim.blocked = (recovery,)
    check("recovery: a strike waits out a Recovery that took", not tester._can_start(built, "shanghai_leg2"))
    tester.aim.blocked = ()
    tester.last_press = time.perf_counter()
    check("recovery: a strike waits to see whether a tap took", not tester._can_start(built, "shanghai_leg2"))
    tester.last_press = time.perf_counter() - 0.3
    check("recovery: an ignored tap starts no cooldown", tester._can_start(built, "shanghai_leg2"))
    loose = dict(combo_action, open_min=0.05)
    aim = FakeAim([40.0])
    aim.range_m = 0.8
    scans, _, _ = run_combo(aim, action=loose)
    check("aim limit: never throws from over 30 deg off", scans == [], str(scans))
    aim = FakeAim([25.0])
    aim.range_m = 0.8
    scans, _, _ = run_combo(aim, action=loose)
    check("aim limit: inside 30 deg the land table decides", scans[:1] == [kick], str(scans))
    aim = FakeAim([40.0])
    aim.range_m = 0.8
    aim.chain.moves, aim.chain.last_hit = (hrsk,), aim.began - 3.4
    scans, _, _ = run_combo(aim, action=dict(loose, last_min=0.05, max_strikes=1), join_s=0.3)
    check("aim limit: wider with the 4 s running out", scans[:1] == [kick], str(scans))
    aim = FakeAim([50.0])
    aim.range_m = 0.8
    aim.chain.moves, aim.chain.last_hit = (hrsk,), aim.began - 3.4
    scans, _, _ = run_combo(aim, action=dict(loose, last_min=0.05, max_strikes=1), join_s=0.3)
    check("aim limit: even then, not past 45 deg", scans == [], str(scans))

    tester = rc.Player(keyboard, NullSession(), require_focus=False, aim=FakeAim([5.0]))
    built = action_config(combo_action)
    built.land = chain_land
    tester.pilot_at = time.perf_counter()
    check("chain: waits while the pilot's own press may be starting", not tester._can_start(built, "shanghai_leg2"))
    tester.pilot_at = time.perf_counter() - 1.0
    check("chain: free to press after that", tester._can_start(built, "shanghai_leg2"))
    tester.aim = FakeAim([5.0], move=(ljab, 0.7))
    check("chain: doesn't cut off a punch that may still land", not tester._can_start(built, "shanghai_leg2"))
    tester.aim.script = [(0.0, 1, ljab)]
    tester.aim.tick()
    tester.aim.move = (ljab, 0.7)
    check("chain: can move on once that punch has landed", tester._can_start(built, "shanghai_leg2"))
    fly = game.move_ids["flying_side_kick"]
    tester.aim = FakeAim([5.0], move=(fly, 2.0))
    tester.aim.script = [(0.0, 1, fly)]
    tester.aim.tick()
    check("chain: a kick can't be cut, even once it has landed", not tester._can_start(built, "shanghai_leg2"))
    check("chain: nothing left held", keyboard.held == [])

    # Aborting mid-combo stops the remaining moves and leaves no key down.
    with Capture() as capture:
        player = rc.Player(keyboard, NullSession(), require_focus=False)
        player.fire(combo, config, "selftest")
        time.sleep(0.4)
        player.stop("selftest abort")
        time.sleep(0.05)
    presses = [e for e in capture.events if e[2] and e[1] != shift]
    check("abort drops the remaining moves", len(presses) == 1, f"{len(presses)} press(es)")
    downs = sum(1 if e[2] else -1 for e in capture.events)
    check("abort leaves no key down", downs == 0 and keyboard.held == [])

    # Without REK focused, a macro must not type into whatever window has focus.
    if rc.foreground_is_game():
        print("skip focus test: REK is the focused window right now", flush=True)
    else:
        with Capture() as capture:
            player = rc.Player(keyboard, NullSession(), require_focus=True)
            refused = not player.fire(combo, config, "selftest")
            time.sleep(0.2)
        check("refuses to fire when REK is not focused", refused and not capture.events)

    # Combo keys are taken while REK is focused and left alone everywhere else.
    # Capture sits behind the trigger hook and swallows whatever gets past it, so
    # anything it records proves the hook let the key through, and no key leaks.
    focused = {"value": True}
    key_4 = rc.vk_from_name("4")

    def tap(name):
        keyboard.down(name)
        keyboard.up(name)
        time.sleep(0.05)

    def drain(triggers):
        seen = []
        while not triggers.pressed.empty():
            seen.append(triggers.pressed.get())
        return seen

    with Capture() as capture:
        triggers = rc.KeyTriggers(is_focused=lambda: focused["value"], accept_injected=True)
        triggers.watched = frozenset({key_4})
        triggers.start()
        check("keyboard hook installs", triggers.ready.wait(5))
        tap("Digit4")
        triggers.observed = frozenset({rc.vk_from_name("7")})
        check("combo key fires once", drain(triggers) == [key_4])
        check("combo key never reaches the game", capture.events == [])
        tap("Digit2")
        check("other keys pass through", len(capture.events) == 2 and drain(triggers) == [])
        capture.events.clear()
        tap("Digit7")
        check("an observed key is noted and still reaches the game",
              len(capture.events) == 2 and triggers.other_keys.get_nowait() == rc.vk_from_name("7"))
        capture.events.clear()
        focused["value"] = False
        tap("Digit4")
        check("combo key types normally outside REK", len(capture.events) == 2 and drain(triggers) == [])
        capture.events.clear()
        focused["value"] = True
        keyboard.down("Digit4")
        keyboard.down("Digit4")  # what holding a key looks like
        focused["value"] = False
        keyboard.up("Digit4")
        time.sleep(0.05)
        check("holding a key fires once", drain(triggers) == [key_4])
        check("release of a taken key is taken too", capture.events == [] and not triggers.held)
        triggers.stop()
    real = rc.KeyTriggers(is_focused=lambda: True)
    real.watched = frozenset({key_4})
    check("synthetic keys cannot trigger a combo", real.decide(key_4, True, injected=True) is False)

    # REK out of focus: the macro's keys can't reach it, so a pad press mid-round puts it back, and failing is never silent.
    pad_config = rc.Config({"combos": [{"name": "pad", "pad": "r1", "key": "f6", "action": {"kind": "free"}}]}, game)
    pad_combo = pad_config.combos[0]
    saved_focus = rc.foreground_is_game, rc.focus_game, rc.warn_beep
    calls = []
    rc.foreground_is_game = lambda: False
    rc.focus_game = lambda: (calls.append("focus"), False)[1]
    rc.warn_beep = lambda: calls.append("beep")
    try:
        tester = rc.Player(keyboard, NullSession(), require_focus=True, aim=FakeAim([5.0]))
        fired = tester.fire(pad_combo, pad_config, pad_combo.pad)
        check("focus: a pad press in a live round tries to put REK back", calls[:1] == ["focus"] and fired is False, str(calls))
        check("focus: if that fails the pilot hears it", "beep" in calls, str(calls))
        calls.clear()
        fired = tester.fire(pad_combo, pad_config, pad_combo.key)
        check("focus: a keyboard trigger never moves the focus", "focus" not in calls and fired is False, str(calls))
        calls.clear()
        tester.aim = FakeAim([None])
        fired = tester.fire(pad_combo, pad_config, pad_combo.pad)
        check("focus: outside a live round it leaves the window alone and stays quiet", calls == [] and fired is False, str(calls))
        front = {"yes": False}
        rc.foreground_is_game = lambda: front["yes"]
        rc.focus_game = lambda: front.update(yes=True) or True
        tester.aim = FakeAim([5.0])
        with Capture():
            fired = tester.fire(pad_combo, pad_config, pad_combo.pad)
            tester.thread.join(3)
            tester.cancel.set()
        check("focus: once REK is back the macro runs", fired is True and front["yes"])
    finally:
        rc.foreground_is_game, rc.focus_game, rc.warn_beep = saved_focus

    # combos.json parsing: a saved typo keeps the previous config working.
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "c.json"
        path.write_text("{ not json", encoding="utf8")
        try:
            rc.load_config(path, game)
            check("rejects a broken combos.json", False)
        except json.JSONDecodeError:
            check("rejects a broken combos.json", True)

    # REK launching while the harness runs: a reader connecting mid-load keeps retrying instead of dying.
    import rek_mem
    opened = []

    class FakeProcess:
        base = 0x1000

        def __init__(self, pid):
            self.closed = False
            opened.append(self)

        def close(self):
            self.closed = True

        def ptr(self, address):
            return 0

    class LoadingIl2cpp:
        def __init__(self, process):
            pass

        def find_class_rva(self, name):
            raise rek_mem.Unavailable("class not found")

    calls = {"n": 0}

    class BadThenGood:
        def read(self):
            calls["n"] += 1
            if calls["n"] == 1:
                raise AttributeError("boom")
            if calls["n"] > 3:
                raise SystemExit  # ends the test's thread quietly
            return {"me": {"at": 1.0, "heading": 0.0, "move": 0}, "phase": 1}

    saved = rek_mem.find_pid, rek_mem.Process, rek_mem.Il2cpp, rek_mem.FightReader
    rek_mem.find_pid, rek_mem.Process, rek_mem.Il2cpp = (lambda: 77), FakeProcess, LoadingIl2cpp
    try:
        reader = rek_mem.FightReader()
        outcomes = []
        for _ in range(2):
            try:
                reader.read()
                outcomes.append("read")
            except rek_mem.Unavailable:
                outcomes.append("unavailable")
            except Exception as error:
                outcomes.append(repr(error))
        check("reader: a game still loading is only 'not ready', every time", outcomes == ["unavailable"] * 2, str(outcomes))
        check("reader: a failed connect closes its handle", len(opened) == 2 and all(p.closed for p in opened))
        rek_mem.FightReader = BadThenGood
        aim = rc.Aim()
        aim.start()
        aim.join(3.0)
        check("live aim: an unexpected read error doesn't end the thread", calls["n"] > 3 and not aim.is_alive(), str(calls))
    finally:
        rek_mem.find_pid, rek_mem.Process, rek_mem.Il2cpp, rek_mem.FightReader = saved

    pad = rc.Pad()
    pad.buttons()
    check("controller is readable", pad.id is not None, f"joystick {pad.id}")

    print(f"\n{len(failures)} failure(s)" if failures else "\nall checks passed", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
