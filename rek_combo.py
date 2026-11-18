#!/usr/bin/env python3
"""REK combo macros: one button press plays a timed chain of moves.

The pilot keeps flying with the DualShock 4. Pressing a macro button (a pad
button or a keyboard key) sends the combo's moves to REK as keyboard chords.

Why keyboard chords: REK feeds the keyboard and the gamepad into its input
binder side by side, so the pad's Custom 2 layout stays active while the
keyboard answers on the built-in T800 table. That table and the strike windows
are read from the game's own set.json at startup, so a patch that rebinds a
move is picked up automatically.

combos.json is reloaded whenever it is saved, so timings can be changed while
the game runs. Only stdlib + Win32 calls; nothing is injected into REK.

While REK is focused, a keyboard hook keeps combo keys from reaching the game,
because 1, 3, 4 and 5 are bound there (get-up, straighten, two dances). A dance
started by the trigger would also block the combo's first kick through the
move-switch cooldown. In any other window those keys type normally.

The session log records pad activity and combo keys. Other typing is never read.

  python rek_combo.py              run the macros
  python rek_combo.py --list       print the keyboard table and combos, then exit
  python rek_combo.py --watch-pad  print pad button names as they are pressed
  python rek_combo.py --dry-run    run triggers and timing without sending keys
"""
import argparse
import collections
import ctypes
import hashlib
import json
import math
import os
import queue
import re
import sys
import threading
import winsound
import time
import traceback
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import rek_paths  # noqa: E402

GAME_EXE = "rek.exe"
GAME_DIR = rek_paths.game_dir()  # REK_GAME_DIR, a running REK.exe, or a Steam library; --game-dir overrides
BAKED = GAME_DIR.joinpath(*rek_paths.BAKED_PARTS)
USER_DIR = rek_paths.user_dir()
PLAYER_LOG = USER_DIR / "Player.log"
STRINGS = USER_DIR / "loc" / "strings_en.json"

ALIASES = {
    "LJab": "shanghai_arm32",  # Left Jab
    "LLK": "shanghai_leg2",    # Left Leg Kick
    "RLFK": "shanghai_leg29",  # Rising Left Front Kick
    "RFK": "shanghai_leg16",   # Right Front Kick
    "RRFK": "shanghai_leg30",  # Rising Right Front Kick
    "HRSK": "shanghai_leg19",  # High Right Side Kick
}

# The patch notes read as three different moves landed within 4 s, and describe() holds fixed step lists to
# that. The game really allows 4 s between one hit and the next: see ComboChain.
COMBO_WINDOW_S = 4.0

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
winmm = ctypes.WinDLL("winmm")


# ---------------------------------------------------------------- key output
class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class MOUSEINPUT(ctypes.Structure):  # only here so the union has the size Windows expects
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
INJECT_TAG = 0x52454B43  # "REKC"; lets the self-test recognise our own events


def _scan_table():
    table = {"LeftShift": 0x2A, "LeftCtrl": 0x1D, "Backspace": 0x0E, "Space": 0x39}
    for row, first in (("1234567890", 0x02), ("QWERTYUIOP", 0x10), ("ASDFGHJKL", 0x1E), ("ZXCVBNM", 0x2C)):
        for i, ch in enumerate(row):
            table[f"Digit{ch}" if ch.isdigit() else ch] = first + i
    return table


SCAN = _scan_table()


class Keyboard:
    """Sends physical-position key events and remembers what is held."""

    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self.held = []
        self.lock = threading.Lock()

    def _send(self, scan, down):
        if self.dry_run:
            return
        flags = KEYEVENTF_SCANCODE | (0 if down else KEYEVENTF_KEYUP)
        event = INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=KEYBDINPUT(0, scan, flags, 0, INJECT_TAG)))
        if user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT)) != 1:
            say(f"!! SendInput failed (error {ctypes.get_last_error()})")

    def down(self, name):
        with self.lock:
            self._send(SCAN[name], True)
            if name not in self.held:
                self.held.append(name)

    def up(self, name):
        with self.lock:
            self._send(SCAN[name], False)
            if name in self.held:
                self.held.remove(name)

    def release_all(self):
        with self.lock:
            for name in reversed(self.held):
                self._send(SCAN[name], False)
            self.held.clear()


# ---------------------------------------------------------------- game focus
def window_is_game(window):
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(window, ctypes.byref(pid))
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        size = wintypes.DWORD(512)
        buf = ctypes.create_unicode_buffer(size.value)
        ok = kernel32.QueryFullProcessImageNameW(wintypes.HANDLE(handle), 0, buf, ctypes.byref(size))
        return bool(ok) and os.path.basename(buf.value).lower() == GAME_EXE
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(handle))


def foreground_is_game():
    return window_is_game(user32.GetForegroundWindow())


WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def game_window():
    """The visible REK window, or None."""
    found = []

    def check(window, _):
        if user32.IsWindowVisible(window) and window_is_game(window):
            found.append(window)
            return False
        return True

    user32.EnumWindows(WNDENUMPROC(check), 0)
    return found[0] if found else None


def bring_to_front(window):
    """Windows refuses a plain request from a background process, so this borrows the foreground thread's input queue."""
    theirs = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
    mine = kernel32.GetCurrentThreadId()
    attached = bool(theirs) and theirs != mine and user32.AttachThreadInput(mine, theirs, True)
    try:
        if user32.IsIconic(window):
            user32.ShowWindow(window, 9)  # SW_RESTORE
        user32.BringWindowToTop(window)
        user32.SetForegroundWindow(window)
    finally:
        if attached:
            user32.AttachThreadInput(mine, theirs, False)


def focus_game(wait_s=0.15):
    """Puts REK in front and returns whether that worked."""
    window = game_window()
    if not window:
        return False
    bring_to_front(window)
    give_up = time.perf_counter() + wait_s
    while time.perf_counter() < give_up:
        if foreground_is_game():
            return True
        time.sleep(0.005)
    return False


_last_beep = 0.0


def warn_beep():
    """A macro that can't fire must not fail silently: mid-fight the pilot can't see this console."""
    global _last_beep
    now = time.perf_counter()
    if now - _last_beep >= 1.5:
        _last_beep = now
        threading.Thread(target=winsound.Beep, args=(880, 200), daemon=True).start()


# ---------------------------------------------------------------- trigger input
class JOYINFOEX(ctypes.Structure):
    _fields_ = [(n, wintypes.DWORD) for n in (
        "dwSize", "dwFlags", "dwXpos", "dwYpos", "dwZpos", "dwRpos", "dwUpos", "dwVpos",
        "dwButtons", "dwButtonNumber", "dwPOV", "dwReserved1", "dwReserved2")]


# DualShock 4 button order as Windows reports it. The d-pad is a hat switch, so
# it gets bits above the 14 real buttons.
PAD_BITS = {"square": 0, "cross": 1, "circle": 2, "triangle": 3, "l1": 4, "r1": 5, "l2": 6, "r2": 7,
            "share": 8, "options": 9, "l3": 10, "r3": 11, "ps": 12, "touchpad": 13,
            "dpad_up": 16, "dpad_right": 17, "dpad_down": 18, "dpad_left": 19}
PAD_NAMES = {bit: name for name, bit in PAD_BITS.items()}
# Pressing one of these mid-combo means the pilot took over, so the rest is dropped.
PILOT_BUTTONS = sum(1 << PAD_BITS[n] for n in ("square", "cross", "circle", "triangle", "l1", "r1", "l2", "r2"))
# A press on one of these takes about 0.14 s to show up as a move, and the game drops a macro press right behind it.
MOVE_BUTTONS = sum(1 << PAD_BITS[n] for n in ("square", "cross", "circle", "triangle",
                                              "dpad_up", "dpad_right", "dpad_down", "dpad_left"))
PILOT_SETTLE_S = 0.18
ARENA_HALF_M = 2.62  # the octagon's half-width, from the positions logged in fights


def wall_distance(x, z):
    """Metres from the octagon's nearest wall."""
    ax, az = abs(x), abs(z)
    return min(ARENA_HALF_M - ax, ARENA_HALF_M - az, ARENA_HALF_M - (ax + az) / math.sqrt(2))
PAD_AXES = ("lx", "ly", "rx", "ry", "u", "v")


def pad_names(mask):
    return [PAD_NAMES.get(bit, f"button{bit}") for bit in range(32) if mask >> bit & 1]


class Pad:
    """Reads the first connected game controller; works while REK has focus."""

    def __init__(self):
        self.id = None
        self.next_scan = 0.0
        self.axes = None

    def _read(self, joy_id):
        info = JOYINFOEX()
        info.dwSize = ctypes.sizeof(JOYINFOEX)
        info.dwFlags = 0xFF  # JOY_RETURNALL: six axes, hat and buttons
        if winmm.joyGetPosEx(joy_id, ctypes.byref(info)) != 0:
            return None
        self.axes = tuple(round(value / 32767.5 - 1, 2) for value in (
            info.dwXpos, info.dwYpos, info.dwZpos, info.dwRpos, info.dwUpos, info.dwVpos))
        mask = info.dwButtons & 0xFFFF
        pov = info.dwPOV
        if pov != 0xFFFF:
            if pov > 27000 or pov < 9000:
                mask |= 1 << PAD_BITS["dpad_up"]
            if 0 < pov < 18000:
                mask |= 1 << PAD_BITS["dpad_right"]
            if 9000 < pov < 27000:
                mask |= 1 << PAD_BITS["dpad_down"]
            if pov > 18000:
                mask |= 1 << PAD_BITS["dpad_left"]
        return mask

    def buttons(self):
        if self.id is not None:
            mask = self._read(self.id)
            if mask is not None:
                return mask
            say("!! controller disconnected")
            self.id = None
        now = time.perf_counter()
        if now >= self.next_scan:  # scanning absent devices is slow, so do it once a second
            self.next_scan = now + 1.0
            for joy_id in range(16):
                mask = self._read(joy_id)
                if mask is not None:
                    self.id = joy_id
                    say(f"controller connected (joystick {joy_id})")
                    return mask
        return 0


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD)
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.CallNextHookEx.argtypes = (wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.UnhookWindowsHookEx.argtypes = (wintypes.HHOOK,)
user32.PostThreadMessageW.argtypes = (wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
WH_KEYBOARD_LL = 13
LLKHF_INJECTED = 0x10
WM_KEYDOWN, WM_SYSKEYDOWN, WM_QUIT = 0x0100, 0x0104, 0x0012


class KeyTriggers(threading.Thread):
    """Catches combo keys and keeps them from the game while REK is focused."""

    def __init__(self, is_focused=foreground_is_game, accept_injected=False):
        super().__init__(daemon=True)
        self.is_focused = is_focused
        self.accept_injected = accept_injected  # the self-test has no physical key to press
        self.watched = frozenset()
        self.observed = frozenset(ord(c) for c in "67890")  # emotes and combination moves in the game's table
        self.other_keys = queue.SimpleQueue()
        self.pressed = queue.SimpleQueue()
        self.held = set()
        self.ready = threading.Event()
        self.thread_id = None

    def decide(self, vk, is_down, injected):
        """True keeps the event from the rest of the system. Runs for every key on the machine, so stays short."""
        if injected and not self.accept_injected:
            return False
        if not is_down:
            if vk in self.held:  # its press was taken, so the release must be too
                self.held.discard(vk)
                return True
            return False
        if vk in self.observed and self.is_focused():  # these reach the game; only noted so the log is complete
            self.other_keys.put(vk)
        if vk not in self.watched:
            return False
        if vk in self.held:  # auto-repeat from holding the key
            return True
        if not self.is_focused():
            return False
        self.held.add(vk)
        self.pressed.put(vk)
        return True

    def run(self):
        self.thread_id = kernel32.GetCurrentThreadId()

        def callback(code, wparam, lparam):
            if code == 0:
                info = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                if self.decide(info.vkCode, wparam in (WM_KEYDOWN, WM_SYSKEYDOWN), bool(info.flags & LLKHF_INJECTED)):
                    return 1
            return user32.CallNextHookEx(None, code, wparam, lparam)

        self.proc = HOOKPROC(callback)
        hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self.proc, None, 0)
        if not hook:
            say(f"!! keyboard hook failed (error {ctypes.get_last_error()}): combo keys will not work")
            return
        message = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)  # creates this thread's message queue
        self.ready.set()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            pass
        user32.UnhookWindowsHookEx(hook)

    def stop(self):
        user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)
        self.join(2)


def vk_from_name(name):
    name = name.lower()
    match = re.fullmatch(r"f(\d{1,2})", name)
    if match and 1 <= int(match.group(1)) <= 24:
        return 0x6F + int(match.group(1))
    match = re.fullmatch(r"numpad(\d)", name)
    if match:
        return 0x60 + int(match.group(1))
    if len(name) == 1 and name.isalnum():
        return ord(name.upper())
    raise ValueError(f"unknown key name {name!r}")


# ---------------------------------------------------------------- game data
class GameData:
    """Move names, chords and strike windows from the installed build."""

    def __init__(self):
        data = json.loads((BAKED / "set.json").read_text(encoding="utf8"))
        self.cooldown_s = data["tuning"]["move_switch_cooldown_s"]
        self.move_ids = {name: i + 1 for i, name in enumerate(data["moves"])}
        self.move_names = {move_id: name for name, move_id in self.move_ids.items()}
        self.chords = {}
        for row in data["bindings"]["keyboard"]["moves"]:
            self.chords[row["move"]] = [key.removeprefix("Key.") for key in row["chord"]]
        system = {name: [key.removeprefix("Key.") for key in chord] for name, chord in data["bindings"]["keyboard"].get("system", {}).items()}
        # What a stuck robot can be sent. The game's own text: "MOTOR FAULT ... cycle E-STOP to recover", then "E-STOP
        # RELEASED: Straighten, then get up". This build binds no e-stop; Passive Mode (dampen) is the nearest thing.
        self.rescue_chords = {name: sorted(chord, key=lambda k: k not in ("LeftShift", "LeftCtrl")) for name, chord in {
            "passive": system.get("dampen"), "straighten": system.get("straighten"), "get_up": self.chords.get("liyudating_getup"),
            "recover": self.chords.get("auto_recovery"), "lie_down": self.chords.get("stance_to_supine")}.items() if chord}
        self.manual_chords = {name: chord for name, chord in {
            "dampen": system.get("dampen"), "liyudating_getup": self.chords.get("liyudating_getup")}.items() if chord}
        self.windows = {}
        self.uninterruptible = set()  # a press during one of these clips does nothing, since build 26496505 all the kicks
        for move in self.chords:
            path = BAKED / f"{move}.json"
            if path.exists():
                clip = json.loads(path.read_text(encoding="utf8"))
                self.windows[move] = clip.get("strike_windows", [])
                if clip.get("interruptible") is False:
                    self.uninterruptible.add(move)
        try:
            strings = json.loads(STRINGS.read_text(encoding="utf8"))
        except (OSError, ValueError):
            strings = {}
        self.titles = {m: strings.get(f"move.t800.{m}", m) for m in self.chords}

    def strike_window(self, move):
        """The kick's window for a leg move, otherwise the first strike of the move."""
        windows = self.windows.get(move) or []
        if "leg" in move:
            for window in windows:
                if "Lower" in window.get("limb", ""):
                    return window
        return windows[0] if windows else None


# ---------------------------------------------------------------- combos.json
class TurnTable:
    """How far each move really turns the robot, measured from fights by build_turn_table.py."""
    FIGHT_MOVE = re.compile(r"shanghai_|flying_side_kick|seven_kick_combo|spinning_back_kick|big_kick|^b1$")

    def __init__(self, curves):
        self.curves = curves  # {move: [(seconds into the move, degrees turned, positive to the right)]}

    @classmethod
    def load(cls, chords, path=HERE / "turn_table.json"):
        try:
            raw = json.loads(Path(path).read_text(encoding="utf8"))
        except (OSError, ValueError):
            return cls({})
        curves = {}
        for move, entry in raw.items():
            if move in chords and cls.FIGHT_MOVE.search(move):
                curves[move] = sorted((float(at), row[0]) for at, row in entry["curve"].items())
        return cls(curves)

    def best(self, degrees, earliest=0.5, latest=1.3):
        """The (move, seconds, degrees) that gets closest to this turn soonest, or None."""
        best = None
        for move, curve in self.curves.items():
            for at, turned in curve:
                if earliest <= at <= latest:
                    cost = abs(turned - degrees) + 25.0 * at
                    if best is None or cost < best[0]:
                        best = (cost, move, at, turned)
        return best[1:] if best else None


class LandTable:
    """How often each move lands from a given angle and distance, measured by land_table.py."""

    def __init__(self, data):
        self.aim_bins = data.get("aim_bins", [])
        self.dist_bins = data.get("dist_bins", [])
        self.moves = data.get("moves", {})

    @classmethod
    def load(cls, path=HERE / "land_table.json"):
        try:
            return cls(json.loads(Path(path).read_text(encoding="utf8")))
        except (OSError, ValueError):
            return cls({})

    @staticmethod
    def _label(value, bins):
        for low, high in bins:
            if low <= value < high:
                return f"{low}..{high}"
        return None

    def _entry(self, move):
        return self.moves.get(move.replace("shanghai_", ""))

    def rate(self, move, aim, distance):
        """Chance this move lands when started from here. Measured from idle; a kick right after a punch does about as well."""
        entry = self._entry(move)
        if not entry:
            return 0.0
        key = f"{self._label(aim, self.aim_bins)}|{self._label(distance, self.dist_bins)}"
        return entry["cells"].get(key, entry["overall"])

    def hit_by(self, move):
        """Seconds after the move starts by which its hit has nearly always shown up."""
        entry = self._entry(move)
        return entry.get("hit_by_s", 0.75) if entry else 0.75

    def ends(self, move):
        """Seconds this clip runs when nothing cuts it short; infinity if not measured."""
        entry = self._entry(move)
        return (entry.get("ends_s") or float("inf")) if entry else float("inf")


class ShotModel:
    """The chance each kick lands from a given setup, fit by shot_model.py on fights since the kick patch and tested on later ones."""

    def __init__(self, data):
        self.weights = dict(zip(data.get("columns", []), data.get("weights", [])))
        self.moves = data.get("moves", {})
        self.lock_s = data.get("lock_s", {})
        self.time_value = data.get("time_value", 0.09)  # hits a second of the robot's time is worth

    @classmethod
    def load(cls, path=HERE / "shot_model.json"):
        try:
            return cls(json.loads(Path(path).read_text(encoding="utf8")))
        except (OSError, ValueError):
            return cls({})

    def lock(self, move):
        """Seconds this kick holds the robot; the game won't let anything cut it."""
        return self.lock_s.get(self.moves.get(move), 2.0)

    def chance(self, move, aim, distance, setup):
        name = self.moves.get(move)
        if name is None:
            return 0.0
        weight = self.weights.get
        a = max(-3.0, min(3.0, aim / 30.0))
        d = max(0.2, min(3.5, distance)) - 1.1
        score = weight(name, 0.0) + weight(f"{name}|a", 0.0) * a + weight(f"{name}|a2", 0.0) * a * a
        score += weight(f"{name}|d", 0.0) * d + weight(f"{name}|d2", 0.0) * d * d
        score += weight("closing", 0.0) * max(-1.5, min(2.0, setup["closing"]))
        score += weight("them_toward", 0.0) * max(-1.0, min(1.5, setup["them_toward"]))
        score += weight(f"them_{setup['them_state']}", 0.0)
        score += weight("human", 0.0) * float(bool(setup["human"])) + weight("idle", 0.0) * float(setup["idle"] >= 0.25)
        return 1.0 / (1.0 + math.exp(-score))


class Step:
    __slots__ = ("move", "after_s", "chord", "min_s", "max_s")

    def __init__(self, move, after_s, chord, min_s=0.0, max_s=0.0):
        self.move, self.after_s, self.chord = move, after_s, chord
        self.min_s, self.max_s = min_s, max_s  # when set, fire inside this range as soon as the robot lines up


class Combo:
    __slots__ = ("name", "pad_mask", "pad", "key", "vk", "steps", "settle_s", "interrupt", "action")

    def __init__(self, name, pad, key, steps, settle_s=0.0, interrupt=False, action=None):
        self.name, self.pad, self.key, self.steps = name, pad, key, steps
        self.interrupt = interrupt  # may cut off a combo that is playing, instead of being ignored
        self.action = action  # free, face, turn or chain, instead of a fixed list of steps
        self.settle_s = settle_s  # how long the last move needs before another press may cut it off
        self.pad_mask = 1 << PAD_BITS[pad] if pad else 0
        self.vk = vk_from_name(key) if key else None

    @property
    def trigger(self):
        return "/".join(x for x in (self.pad, self.key) if x)


class Config:
    def __init__(self, raw, game):
        settings = raw.get("settings", {})
        self.shift_lead_s = settings.get("shift_lead_ms", 25) / 1000
        self.key_hold_s = settings.get("key_hold_ms", 70) / 1000
        self.est_latency_s = settings.get("est_latency_ms", 250) / 1000
        self.abort_vk = vk_from_name(settings.get("abort_key", "f9"))
        self.abort_on_pilot_buttons = settings.get("abort_on_pilot_buttons", True)
        self.facing_offset_deg = settings.get("facing_offset_deg", 45.0)
        self.aim_ok_deg = settings.get("aim_ok_deg", 35.0)
        self.aim_lead_s = settings.get("aim_lead_ms", 140) / 1000  # a press takes this long to start the move
        self.turn_stop_deg = settings.get("turn_stop_deg", 15.0)
        self.cooldown_s = game.cooldown_s + 0.05  # a press inside the game's move-switch cooldown is dropped
        self.game = game
        self.table = TurnTable.load(game.chords)
        self.land = LandTable.load()
        self.shots = ShotModel.load()
        self.repeat_block_s = settings.get("repeat_block_ms", 2000) / 1000  # the same move can't start again sooner
        # How long each of THEIR kicks counts as live (dodge, don't kick into it) at least, by move: past its hit window.
        self.their_live_s = {ALIASES.get(m, m): float(s) for m, s in settings.get("their_live_s", {}).items()}
        rescue = settings.get("rescue", {})
        self.rescue_auto = bool(rescue.get("auto", True))  # never off (the pilot's call, 2026-11-10)
        self.rescue_stuck_s = rescue.get("stuck_s", 7.0)  # 79 Recoveries over 2 s since 11-05: under ~7.5 s most end standing,
        # from 8 s about 21 of 26 ended the round (knockout)
        self.rescue_combo = None
        if rescue.get("steps"):
            _check_rescue_steps("settings.rescue", rescue["steps"], game)
            self.rescue_combo = Combo("Get up: stuck in Recovery", None, None, [], interrupt=True,
                                      action={"kind": "rescue", "steps": rescue["steps"]})
        # Get Up again and again from the moment the robot starts to go over: early enough, the game doesn't call it a slip
        # or a knockdown (the pilot's call, 2026-11-10). Going over: a fall or Recovery clip, or leaning past tilt_deg (Left Leg
        # Kick leans up to about 46 deg on its own; the falls on 11-10 went 54 -> 75 deg).
        spam = rescue.get("spam")
        self.rescue_tilt_deg = float(spam.get("tilt_deg", 55.0)) if spam else 55.0
        self.rescue_spam = None
        if spam:
            chord = spam.get("chord", "get_up")
            if chord not in game.rescue_chords:
                raise ValueError(f"settings.rescue.spam: unknown chord {chord!r} (known: {sorted(game.rescue_chords)})")
            self.rescue_spam = Combo("Get up: going over", None, None, [], interrupt=True,
                                     action={"kind": "rescue", "spam": chord, "every_s": float(spam.get("every_s", 0.15)),
                                             "max_s": float(spam.get("max_s", 6.0))})
        self.combos = []
        seen = {}
        for entry in raw["combos"]:
            if not entry.get("enabled", True):
                continue
            action = entry.get("action")
            if action and action.get("kind") == "chain":
                action = dict(action, openers=[ALIASES.get(m, m) for m in action["openers"]],
                              moves=[ALIASES.get(m, m) for m in action["moves"]],
                              no_cancel=[ALIASES.get(m, m) for m in action.get("no_cancel", [])])
                for key in ("move_min_idle_ms", "move_max_dist", "move_min_dist", "move_aim", "move_min_closing", "move_min_used", "move_hit_s",
                            "move_min_chance", "move_max_dist_moving", "move_aim_near"):
                    if key in action:
                        action[key] = {ALIASES.get(m, m): value for m, value in action[key].items()}
                setup_moves = []
                if "brawl" in action:
                    action["brawl"] = dict(action["brawl"], moves=[ALIASES.get(m, m) for m in action["brawl"]["moves"]])
                    setup_moves += action["brawl"]["moves"]
                if "setup" in action:
                    action["setup"] = dict(action["setup"], move=ALIASES.get(action["setup"]["move"], action["setup"]["move"]))
                    setup_moves.append(action["setup"]["move"])
                if "rush" in action:
                    rush = action["rush"]
                    action["rush"] = dict(rush, moves=[ALIASES.get(m, m) for m in rush["moves"]],
                                          close_moves=[ALIASES.get(m, m) for m in rush.get("close_moves", rush["moves"])],
                                          limits={ALIASES.get(m, m): v for m, v in rush.get("limits", {}).items()})
                    setup_moves += action["rush"]["moves"] + action["rush"]["close_moves"]
                for move in action["openers"] + action["moves"] + setup_moves:
                    if move not in game.chords:
                        raise ValueError(f"{entry['name']}: move {move!r} has no keyboard binding")
            if action and action.get("kind") == "rescue":
                if action.get("spam"):
                    if action["spam"] not in game.rescue_chords:
                        raise ValueError(f"{entry['name']}: unknown spam chord {action['spam']!r} (known: {sorted(game.rescue_chords)})")
                else:
                    _check_rescue_steps(entry["name"], action.get("steps", []), game)
            if action and action.get("kind") == "manualtap":
                command = action.get("command")
                if command not in ("dampen", "liyudating_getup"):
                    raise ValueError(f"{entry['name']}: unknown manual command {command!r}")
                chord = game.manual_chords.get(command)
                if not chord or any(key not in SCAN for key in chord):
                    raise ValueError(f"{entry['name']}: manual command {command!r} has no supported native keyboard chord")
            if action and "stop" in action:
                action = dict(action, stop=ALIASES.get(action["stop"], action["stop"]))
                if action["stop"] not in game.chords:
                    raise ValueError(f"{entry['name']}: move {action['stop']!r} has no keyboard binding")
            steps = []
            for i, row in enumerate(entry.get("steps", [])):
                move = ALIASES.get(row["move"], row["move"])
                if move not in game.chords:
                    raise ValueError(f"{entry['name']}: move {row['move']!r} has no keyboard binding")
                min_s, max_s = row.get("min_ms", 0) / 1000, row.get("max_ms", 0) / 1000
                if i == 0:
                    after_s = 0.0
                elif "after_ms" in row:
                    after_s = row["after_ms"] / 1000
                else:  # without live aim, a gated step fires midway through its range
                    after_s = (min_s + max_s) / 2
                steps.append(Step(move, after_s, game.chords[move], min_s, max_s))
            settle_s = 0.0
            if steps:
                last_stop = max((w["stop_s"] for w in game.windows.get(steps[-1].move, [])), default=0.0)
                settle_s = max(game.cooldown_s, self.est_latency_s + last_stop)
            if "settle_ms" in entry:  # moves with no listed strike windows say how long to protect them
                settle_s = entry["settle_ms"] / 1000
            combo = Combo(entry["name"], entry.get("pad"), entry.get("key"), steps, settle_s,
                          entry.get("interrupt", False), action)
            for trigger in (combo.pad, combo.key):
                if trigger and trigger in seen:
                    raise ValueError(f"{trigger} is used by both {seen[trigger]!r} and {combo.name!r}")
                if trigger:
                    seen[trigger] = combo.name
            self.combos.append(combo)


def strike_move_ids(game, down_ids=frozenset()):
    """Move ids of strikes (kicks and punches), with Recovery and the get-ups left out."""
    return frozenset(i for i, name in game.move_names.items()
                     if name != "auto_recovery" and TurnTable.FIGHT_MOVE.search(name) and i not in down_ids)


def _check_rescue_steps(where, steps, game):
    if not steps:
        raise ValueError(f"{where}: a rescue needs steps")
    for step in steps:
        if len(step) != 2 or step[0] not in game.rescue_chords or not 0 <= float(step[1]) <= 10:
            raise ValueError(f"{where}: bad rescue step {step!r} (known: {sorted(game.rescue_chords)}, wait 0-10 s)")


def describe(combo, config, game):
    """Readable timeline plus warnings about timing the game would reject."""
    lines = [f"[{combo.trigger}] {combo.name}"]
    press = 0.0
    lands = []
    for i, step in enumerate(combo.steps):
        press += step.after_s
        window = game.strike_window(step.move)
        chord = "+".join(k.replace("LeftShift", "Shift").replace("Digit", "") for k in step.chord)
        hit = ""
        if window:
            start = press + config.est_latency_s + window["start_s"]
            stop = press + config.est_latency_s + window["stop_s"]
            lands.append(press + config.est_latency_s + window["peak_s"])
            hit = f"hit ~{start:.2f}-{stop:.2f}s"
        lines.append(f"   +{press:4.2f}s  {chord:9} {game.titles.get(step.move, step.move):24} {hit}")
        if i and step.after_s <= game.cooldown_s:
            lines.append(f"      !! {step.after_s:.2f}s gap is inside the game's "
                         f"{game.cooldown_s:.2f}s move-switch cooldown: this press will be dropped")
        if i + 1 < len(combo.steps) and window:
            # Both presses share the same input delay, so a gap of g cancels at clip time g.
            early = window["stop_s"] - combo.steps[i + 1].after_s
            if early > 0:
                lines.append(f"      !! next press cancels this move before its hit window ends (~{early:.2f}s early)")
    if len(lands) >= 2:
        # A longer chain only needs some three hits inside the window.
        span = min(lands[i + 2] - lands[i] for i in range(len(lands) - 2)) if len(lands) >= 3 else lands[-1] - lands[0]
        note = "" if span <= COMBO_WINDOW_S else f"  !! over the {COMBO_WINDOW_S:.0f}s combo window"
        lines.append(f"   first to last hit ~{span:.2f}s of {COMBO_WINDOW_S:.0f}s{note}")
    return "\n".join(lines)


# ---------------------------------------------------------------- session log
class Session:
    def __init__(self):
        (HERE / "logs").mkdir(exist_ok=True)
        self.path = HERE / "logs" / f"session-{datetime.now():%Y%m%d-%H%M%S}.jsonl"
        self.file = open(self.path, "a", encoding="utf8", buffering=1)
        self.lock = threading.Lock()

    def event(self, kind, **fields):
        with self.lock:
            self.file.write(json.dumps({"t": datetime.now().isoformat(timespec="milliseconds"),
                                        "ev": kind, **fields}) + "\n")


class InputLog:
    """Writes pad buttons at every change and sticks at up to 20 Hz to the session log."""

    def __init__(self, session):
        self.session = session
        self.axes = None
        self.next_axes = 0.0

    def update(self, pressed, released, axes, now):
        if pressed or released:
            self.session.event("pad", down=pad_names(pressed), up=pad_names(released))
        if axes is None or now < self.next_axes:
            return
        if self.axes is None or any(abs(a - b) >= 0.15 for a, b in zip(axes, self.axes)):
            self.axes = axes
            self.next_axes = now + 0.05
            self.session.event("stick", **dict(zip(PAD_AXES, axes)))


_console = queue.SimpleQueue()


def say(text):
    _console.put(f"{datetime.now():%H:%M:%S.%f}"[:-3] + " " + text)


def console_writer():
    # Printing blocks for as long as text is selected in the console window. Only
    # this thread may stall, so a stray click can never freeze a combo mid-chord.
    with open(HERE / "logs" / "console.log", "a", encoding="utf8", buffering=1) as mirror:
        while True:
            line = _console.get()
            print(line, file=mirror)
            print(line, flush=True)


def report_crash(kind, value, trace):
    say("!! crashed: " + "".join(traceback.format_exception(kind, value, trace)))
    time.sleep(0.2)


# ---------------------------------------------------------------- combo player
class ComboChain:
    """Follows the combo rule as the game really scores it: three different moves, each hit within 4 s of the last.

    Worked out by combo_chain_test.py from every logged hit. The score rises by 1 for a clean hit, 4 for COMBO X2,
    5 for a knockdown and 3 when the other robot slips. Since the kick patch a clean hit on me breaks the chain (of 27
    times three different moves landed in time, 18 of 19 that scored had no hit on me inside, all 8 that didn't had one).
    Knockdowns and slips neither add to one nor extend it. Hits from the pad count the same as the macro's.
    """
    GAP_S = 4.0
    CREDIT_S = 0.6  # a hit reported just after its clip ended still belongs to that clip
    TOO_SOON_S = 0.15  # a hit this soon after a clip was cut belongs to the clip it cut

    def __init__(self, recovery_id=0):
        self.recovery_id = recovery_id
        self.moves = ()  # the different moves landed so far in this chain
        self.last_hit = None
        self.landed = 0  # hits landed in total, whatever they scored
        self.last_landed = None
        self.combos = 0
        self.score = None
        self.round = None
        self.strike = 0
        self.strike_at = 0.0
        self.strike_began = 0.0
        self.strike_before = 0
        self.strike_before_at = 0.0
        self.their_score = None

    def update(self, now, round_number, score, move, theirs=None):
        """Takes one reading of the round, my score, the move playing and, when known, the other robot's score."""
        if move and move != self.recovery_id:
            if move != self.strike:
                self.strike_before, self.strike_before_at, self.strike_began = self.strike, self.strike_at, now
            self.strike, self.strike_at = move, now
        if self.moves and now - self.last_hit > self.GAP_S:
            self.moves = ()
        if round_number != self.round or self.score is None or score < self.score:
            self.round, self.score, self.moves = round_number, score, ()
            self.their_score = theirs
            return
        if theirs is not None:
            if self.their_score is not None and theirs - self.their_score in (1, 4, 5):
                self.moves = ()  # hit, combo'd or knocked down: the game has wiped my chain
            self.their_score = theirs
        jump = score - self.score
        self.score = score
        if jump not in (1, 4, 5):
            return
        self.landed += 1
        self.last_landed = now
        if jump == 4:
            self.moves, self.last_hit = (), now
            self.combos += 1
        elif jump == 1:
            self.last_hit = now
            playing = move and move != self.recovery_id
            just_cut = self.strike_before and now - self.strike_began < self.TOO_SOON_S and self.strike_began - self.strike_before_at <= self.TOO_SOON_S
            if playing and just_cut:
                credited = self.strike_before  # a strike can't land this soon, so this hit belongs to the clip it just cut
            else:
                credited = move if playing else (self.strike if now - self.strike_at <= self.CREDIT_S else 0)
            if credited and credited not in self.moves:
                self.moves += (credited,)


class Aim(threading.Thread):
    """Keeps the newest fight state from rek_mem (read-only) about 30 times a second."""

    def __init__(self):
        super().__init__(daemon=True)
        self.state = None
        self.stamp = 0.0
        self.offset_deg = 45.0
        self.rate = 0.0  # own turn rate, degrees per second, positive to the right
        self.started = {}  # when each move id last started, by the local clock
        self.playing = 0
        self.idle_since = 0.0  # when the last clip ended, by the local clock
        self.gaps = collections.deque(maxlen=16)  # (when, distance), about every 0.05 s
        self.enemy_track = collections.deque(maxlen=16)  # (when, enemy x, enemy z), about every 0.05 s
        self.chain = ComboChain()
        self.down_ids = frozenset()  # get-up and Recovery move ids, set from the build
        self.strike_ids = frozenset()  # strike move ids (kicks and punches), set from the build
        self.enemy_down = False
        self.enemy_up_at = None  # when the other robot was last seen getting back up, by the local clock

    def run(self):
        try:
            import rek_mem
        except ImportError as error:
            say(f"!! live aim is off: {error}")
            return
        reader = rek_mem.FightReader()
        previous = reported = None
        while True:
            try:
                state = reader.read()
                me = state["me"]
                if previous and me["at"] > previous["at"]:
                    turned = (me["heading"] - previous["heading"] + 180.0) % 360.0 - 180.0
                    self.rate = 0.5 * self.rate + 0.5 * turned / (me["at"] - previous["at"])
                if not previous or me["at"] > previous["at"]:
                    previous = me
                self.state = state
                self.stamp = time.perf_counter()
                if not self.gaps or self.stamp - self.gaps[-1][0] >= 0.05:
                    self.gaps.append((self.stamp, state["distance"]))
                    self.enemy_track.append((self.stamp, state["them"]["x"], state["them"]["z"]))
                if me["move"] != self.playing:
                    self.playing = me["move"]
                    if me["move"]:
                        self.started[me["move"]] = self.stamp
                    else:
                        self.idle_since = self.stamp
                them = state["them"]
                down = self._enemy_is_down(them)
                if self.enemy_down and not down:
                    self.enemy_up_at = self.stamp
                self.enemy_down = down
                if state.get("hits"):
                    self.chain.update(self.stamp, state["round"], state["hits"][0], me["move"], state["hits"][1])
            except rek_mem.Unavailable:
                self.state = previous = None
                self.rate = 0.0
            except Exception as error:  # every macro needs this thread, so one bad read must not end it
                self.state = previous = None
                self.rate = 0.0
                reader = rek_mem.FightReader()
                if repr(error) != reported:
                    reported = repr(error)
                    say(f"!! live aim error, reconnecting: {reported}")
            time.sleep(0.01 if self.state else 0.5)

    def error(self):
        """Degrees to turn to face the opponent, positive to the right. None without a live round."""
        state = self.state
        if state is None or state["phase"] != 1 or time.perf_counter() - self.stamp > 0.25:
            return None
        return (state["bearing"] - state["me"]["heading"] - self.offset_deg + 180.0) % 360.0 - 180.0

    def predicted(self, lead_s):
        """The error expected lead_s from now, if the robot keeps turning as it is."""
        error = self.error()
        if error is None:
            return None
        return (error - self.rate * lead_s + 180.0) % 360.0 - 180.0

    def in_a_move(self):
        state = self.state
        return bool(state and state["me"]["move"])

    def current_move(self):
        """(move id, seconds into it), or None without live data."""
        state = self.state
        if state is None or time.perf_counter() - self.stamp > 0.25:
            return None
        return state["me"]["move"], state["me"]["elapsed"]

    def closing(self):
        """How fast the gap closed over the last 0.4 s or so, in m/s, negative when it's opening. None without enough data."""
        state = self.state
        now = time.perf_counter()
        if state is None or now - self.stamp > 0.25:
            return None
        for at, gap in self.gaps:
            if now - at <= 0.45:
                return (gap - state["distance"]) / (now - at) if now - at >= 0.2 else None
        return None

    def enemy_approach(self):
        """How fast the enemy itself came toward me over the last 0.4 s or so, in m/s. 0 without enough data."""
        state = self.state
        now = time.perf_counter()
        if state is None or now - self.stamp > 0.25:
            return 0.0
        them, me = state["them"], state["me"]
        for at, x, z in self.enemy_track:
            if now - at <= 0.45:
                span = now - at
                if span < 0.2:
                    return 0.0
                to_me_x, to_me_z = me["x"] - them["x"], me["z"] - them["z"]
                gap = math.hypot(to_me_x, to_me_z) or 1.0
                return ((them["x"] - x) * to_me_x + (them["z"] - z) * to_me_z) / gap / span
        return 0.0

    def _enemy_is_down(self, them):
        """Down or getting up: a fall or get-up move, or leaning past 45 deg outside a strike. Their jump and spin kicks
        lean that far on their own (19:03:57, 11-10: one read as a fall, the foul guard froze my robot and it took the hit);
        a real fall shows up as a Recovery or get-up move straight after."""
        if them["move"] in self.down_ids:
            return True
        return (them.get("tilt") or 0.0) > 45.0 and them["move"] not in self.strike_ids

    def enemy_up_for(self):
        """Seconds since the other robot got back up: 0 while it is down or getting up, infinity if it hasn't been down.
        Hitting a robot that is down or in Recovery gives IT 3 points (a foul): since the kick patch, strikes started while
        it was down or within 0.25 s of it standing drew +3 for it 3-4% of the time; from 0.25 s on, 0 of about 9,500."""
        if self.enemy_down:
            return 0.0
        return float("inf") if self.enemy_up_at is None else time.perf_counter() - self.enemy_up_at

    def own_approach(self):
        """How fast my robot itself came toward the enemy over the last 0.4 s or so, in m/s. 0 without enough data.
        Walking in is what stretches a kick's reach: Left Leg Kick lands 60-71% from 1.25-1.6 m that way, 14-19% standing."""
        closing = self.closing()
        return 0.0 if closing is None else closing - self.enemy_approach()

    def enemy_facing(self):
        """Degrees the enemy is turned away from facing me (0 = squared up), or None without a live round."""
        state = self.state
        if state is None or state["phase"] != 1 or time.perf_counter() - self.stamp > 0.25:
            return None
        return abs((state["bearing"] + 180.0 - state["them"]["heading"] - self.offset_deg + 180.0) % 360.0 - 180.0)

    def shot_inputs(self):
        """What the shot model needs that isn't the angle or distance."""
        state = self.state
        them = state["them"] if state else {"move": 0, "elapsed": 0.0}
        return {"closing": self.closing() or 0.0, "them_toward": self.enemy_approach(), "them_move": them["move"], "them_elapsed": them["elapsed"],
                "human": bool(state and state.get("human")), "idle": self.idle_for()}

    def idle_for(self):
        """Seconds since the last clip ended, which is how long Auto Turning has had. 0 in a clip or without live data."""
        state = self.state
        if state is None or state["me"]["move"] or time.perf_counter() - self.stamp > 0.25:
            return 0.0
        return time.perf_counter() - self.idle_since

    def heading(self):
        state = self.state
        if state is None or time.perf_counter() - self.stamp > 0.25:
            return None
        return state["me"]["heading"]

    def tilt(self):
        """Degrees my robot leans off upright. None without live data."""
        state = self.state
        if state is None or time.perf_counter() - self.stamp > 0.25:
            return None
        return state["me"].get("tilt")

    def positions(self):
        """Both robots' (x, z), which way mine faces (heading plus the facing offset, the bearing's convention) and the gap.
        None without a live round."""
        state = self.state
        if state is None or state["phase"] != 1 or time.perf_counter() - self.stamp > 0.25:
            return None
        me, them = state["me"], state["them"]
        return {"me": (me["x"], me["z"]), "them": (them["x"], them["z"]), "facing": me["heading"] + self.offset_deg,
                "distance": state["distance"]}

    def distance(self):
        state = self.state
        if state is None or time.perf_counter() - self.stamp > 0.25:
            return None
        return state["distance"]

    def hits(self):
        """Hits I have landed, not counting the other robot's slips. None without live data."""
        state = self.state
        if state is None or time.perf_counter() - self.stamp > 0.25 or not state.get("hits"):
            return None
        return self.chain.landed

    def since_landed(self):
        """Seconds since I last landed a hit."""
        landed = self.chain.last_landed
        return float("inf") if landed is None else time.perf_counter() - landed

    def started_since(self, move_id, when):
        """True once this move has started at or after that moment, by the local clock."""
        return self.started.get(move_id, -1.0) >= when

    def since_started(self, move_id):
        """Seconds since this move last started."""
        started = self.started.get(move_id)
        return float("inf") if started is None else time.perf_counter() - started


class Player:
    # Measured live. R reaches the game about 0.12 s after the tap, and the next move can't start until about 0.84 s
    # after a tap that took, which is RECOVERY_BLOCK_S after Recovery is first seen.
    RECOVERY_SHOWS_S = 0.2
    RECOVERY_BLOCK_S = 0.72
    RECOVERY_WORTH_S = 1.3  # a tap that takes lets the next move start 0.84 s later, after a Recovery that also has to
    # settle, so a clip with less than this left ends about as soon; on those R is mostly ignored anyway (LLK 1 of 15)
    RECOVERY_EARLIEST_S = 0.4  # a tap earlier into the clip falls inside the game's switch cooldown
    START_SEEN_S = 0.3  # a move the game took shows by now (about 0.14 s normally), so one that hasn't was dropped

    def __init__(self, keyboard, session, require_focus=True, aim=None):
        self.keyboard = keyboard
        self.session = session
        self.require_focus = require_focus
        self.aim = aim
        self.thread = None
        self.cancel = threading.Event()
        self.sent = collections.Counter()
        self.last_press = 0.0
        self.last_strike = 0.0  # last_press, leaving out R taps: the game ignores most of those, so they start no cooldown
        self.pilot_at = 0.0  # when the pilot last pressed a button that starts a move
        self.stick_pushed = False  # the pilot is steering with the left stick
        self.backing_off = False  # walking out of reach after a strike; a new press cuts it short
        self.position_mode = None  # what _position last did, so only changes are logged
        self.line_up_since = None  # when the robot stopped to let Auto Turning line Left Leg Kick up
        self.pad_now = 0  # pad buttons held right now; the main loop keeps it current (hold_to_arm reads it)
        self.running_pad = None  # the pad trigger whose combo is playing; None for the rescue and keyboard presses

    @property
    def busy(self):
        return self.thread is not None and self.thread.is_alive()

    def stop(self, reason):
        if self.busy:
            self.cancel.set()
            say(f"   stopped: {reason}")
            self.session.event("abort", reason=reason)
            self.thread.join(timeout=1.0)

    def fire(self, combo, config, source):
        if self.require_focus and not foreground_is_game():
            # The pad reaches REK without focus but the macro's keys can't, so a pad press mid-round means put it back.
            # Only then: a press on the keyboard, or outside a round, may well be meant for the window that has focus.
            live = self.aim is not None and self.aim.error() is not None
            if not (live and source == combo.pad and focus_game()):
                say(f"ignored {combo.trigger}: REK is not the focused window")
                self.session.event("ignored", combo=combo.name, reason="not focused")
                if live:
                    warn_beep()
                return False
            say(f"put REK back in front for {combo.trigger}")
            self.session.event("refocused", combo=combo.name)
        if self.busy and (combo.interrupt or self.backing_off):  # a press during a back-off means attack now
            self.stop(f"interrupted by {combo.name}")
        elif (self.busy and source == combo.pad and combo.action
              and self.running_pad is not None and self.running_pad in combo.action.get("takes_over", ())):
            # takes_over (2026-11-13): an R1 chain from presses the pilot never meant kept a held L1 ignored for 14 s,
            # so the main trigger cuts in at once. The rescue has no pad, so no trigger can cut it.
            self.stop(f"interrupted by {combo.name}")
        elif self.busy:  # one press plays the whole combo; only the abort key or an interrupt cuts it short
            self.session.event("ignored", combo=combo.name, reason="another combo is playing")
            return False
        self.cancel.clear()
        self.running_pad = combo.pad if source == combo.pad else None
        self.thread = threading.Thread(target=self._run, args=(combo, config, source), daemon=True)
        self.thread.start()
        return True

    def _wait(self, until):
        """Waits with sub-millisecond accuracy. False means the combo was cancelled."""
        while True:
            left = until - time.perf_counter()
            if self.cancel.is_set():
                return False
            if left <= 0:
                return True
            if left > 0.003:
                time.sleep(min(left - 0.002, 0.005))

    def _press(self, config, combo, move, step, start, target=None):
        """Plays one chord. Returns the moment the key went down, or None if cut short."""
        chord = config.game.chords[move]
        mods, key = chord[:-1], chord[-1]
        if target is None:
            target = time.perf_counter() + (config.shift_lead_s if mods else 0.0)
        if not self._wait(target - (config.shift_lead_s if mods else 0)):
            return None
        if self.require_focus and not foreground_is_game():
            say("   stopped: REK lost focus")
            self.session.event("abort", reason="lost focus")
            return None
        for mod in mods:
            self.keyboard.down(mod)
        if mods and not self._wait(target):
            return None
        actual = time.perf_counter()
        self.keyboard.down(key)
        self.sent[move] += 1
        late_ms = (actual - target) * 1000
        say(f"   +{actual - start:5.3f}s {'+'.join(chord):16} {move} (late {late_ms:.1f} ms)")
        aim_error = self.aim.error() if self.aim else None
        self.session.event("press", combo=combo.name, step=step, move=move, at_s=round(actual - start, 4),
                           late_ms=round(late_ms, 2), gap_s=round(actual - self.last_press, 3),
                           aim_deg=None if aim_error is None else round(aim_error, 1))
        self.last_press = actual
        if move != "auto_recovery":
            self.last_strike = actual
        self._wait_uninterrupted(actual + config.key_hold_s)
        self.keyboard.up(key)
        for mod in reversed(mods):
            self.keyboard.up(mod)
        return actual

    def _ready(self, config):
        """Waits out the game's move-switch cooldown, for macro and pad moves alike."""
        if not self._wait(self.last_press + config.cooldown_s):
            return False
        current = self.aim.current_move() if self.aim else None
        if current and current[0]:
            return self._wait(time.perf_counter() + max(0.0, config.cooldown_s - current[1]))
        return True

    def _free(self, config, combo, start):
        """Ends the current clip with Recovery, so Auto Turning can act. Pressing it mid-clip is what works."""
        if not self._ready(config):
            return
        current = self.aim.current_move() if self.aim else None
        if current and not current[0]:
            return  # nothing is playing, so the robot is already free
        self._press(config, combo, "auto_recovery", 0, start)

    def _turn(self, config, combo, start, degrees=None):
        """Turns by pressing a move and cutting it once it has turned far enough. None means face the opponent.

        The action's "stop" move does the cutting, and Left Leg Kick and Rising Left Front Kick turn the robot least after
        the cut (median about 3 deg at 0.6 s). Since build 26496505 that only works on punches: a kick can't be cut, so
        all that can stop one is R, which takes on about a quarter of tries.
        """
        if self.aim is None or not self._ready(config):
            return
        if degrees is None:
            degrees = self.aim.predicted(config.aim_lead_s)
            if degrees is None:
                return
            if abs(degrees) <= config.aim_ok_deg:  # close enough for Auto Turning: just free the robot
                self._free(config, combo, start)
                return
        choice = config.table.best(degrees)
        if choice is None:
            say("!! no turn data: run build_turn_table.py")
            return
        move, at, expected = choice
        last = self.aim.heading()
        pressed = self._press(config, combo, move, 0, start)
        if pressed is None:
            return
        turned = 0.0
        latest = pressed + max(config.cooldown_s, at) + 0.25
        while time.perf_counter() < latest:
            if self.cancel.is_set():
                return
            heading = self.aim.heading()
            if heading is not None and last is not None:
                turned += (heading - last + 180.0) % 360.0 - 180.0
            if heading is not None:
                last = heading
            ahead = turned + self.aim.rate * config.aim_lead_s
            if time.perf_counter() >= pressed + config.cooldown_s and abs(ahead - degrees) <= config.turn_stop_deg:
                break
            time.sleep(0.003)
        stop = "auto_recovery" if move in config.game.uninterruptible else combo.action.get("stop", "auto_recovery")
        cancelled = self._press(config, combo, stop, 1, start)
        self.session.event("turn", want=round(degrees, 1), move=move, table_s=at, table_deg=expected,
                           turned_at_cancel=round(turned, 1),
                           cancel_s=None if cancelled is None else round(cancelled - pressed, 3))
        say(f"   turn: wanted {degrees:+.0f}, {move} reached {turned:+.0f} when cancelled")

    def _clip_spent(self, config, move_id, elapsed):
        """True once the clip playing can't land anything more: it has already hit, or its hit would have shown by now."""
        name = config.game.move_names.get(move_id, "")
        return elapsed >= config.land.hit_by(name) + 0.05 or self.aim.since_landed() < elapsed

    def _can_start(self, config, move):
        """False when the game would drop this press, or it would cut off a get-up or a strike that may still land."""
        now = time.perf_counter()
        if now - self.last_strike < config.cooldown_s or now - self.pilot_at < PILOT_SETTLE_S:
            return False
        if now - self.last_press < self.RECOVERY_SHOWS_S:  # an R tap may have taken; it can't be seen yet
            return False
        if self.aim.since_started(config.game.move_ids["auto_recovery"]) < self.RECOVERY_BLOCK_S:
            return False
        current = self.aim.current_move()
        if current and current[0]:
            name = config.game.move_names.get(current[0], "")
            if name != "auto_recovery" and not TurnTable.FIGHT_MOVE.search(name):
                return False
            if name in config.game.uninterruptible:
                return False
            if current[1] < config.cooldown_s or not self._clip_spent(config, *current):
                return False
        return self.aim.since_started(config.game.move_ids[move]) >= config.repeat_block_s

    def _free_spent(self, config, combo, start, action, step):
        """Tries to end a clip with nothing left to land, so Auto Turning can act. Leaves get-ups and no_cancel moves alone.

        The game ignores R through most of most clips (it seems to need the robot well off upright; unproven), so the
        tap repeats, faster early in the clip where that window tends to be.
        """
        now = time.perf_counter()
        current = self.aim.current_move()
        if not current or not current[0] or now - self.pilot_at < PILOT_SETTLE_S:
            return
        move_id, elapsed = current
        name = config.game.move_names.get(move_id, "")
        if name in action["no_cancel"] or not TurnTable.FIGHT_MOVE.search(name):
            return
        if now - self.last_strike < config.cooldown_s or now - self.last_press < (0.12 if elapsed < 0.7 else 0.3):
            return
        if elapsed < self.RECOVERY_EARLIEST_S or elapsed + self.RECOVERY_WORTH_S >= config.land.ends(name):
            return
        if self._clip_spent(config, move_id, elapsed):
            self._press(config, combo, "auto_recovery", step, start)

    def _chain_pick(self, config, action, aim, distance, now, may_set_up=True):
        """Returns the move to throw now or None, its chance or the best chance seen, and whether any shot is open from here.

        Since build 26496505 a kick can't be cut, so a press during one does nothing and the only way on is its end (or
        Recovery, which sometimes takes). Strikes from idle land well, and each gap between hits has time to spare, so the
        robot gets to settle and Auto Turning gets to re-aim. In its reach a setup punch goes first: it barely turns the robot,
        and a kick cutting a punch lands about as well as from idle (Left Leg Kick 55% vs 62%).
        Moves are listed shortest clip first, so the first one good enough wins, which also keeps misses cheap.
        A shot can be open with nothing to throw yet, when the game just wouldn't take the press for a moment.
        """
        current = self.aim.current_move()
        playing = current[0] if current else 0
        if playing and config.game.move_names.get(playing, "") in config.game.uninterruptible:
            return None, 0.0, False
        up_for = getattr(self.aim, "enemy_up_for", None)
        if up_for and up_for() < action.get("foul_guard_ms", 0) / 1000:
            return None, 0.0, True  # it's down or only just up: a hit now is a foul worth 3 to it; 0.25-1.25 s after it stands lands best
        chain = self.aim.chain
        used = chain.moves
        left = ComboChain.GAP_S - (now - chain.last_hit) if used else float("inf")
        if used and action.get("backoff_ms") and not self._follow_up_possible(config, action, used, left):
            used, left = (), float("inf")  # this combo can't go on, and a repeat still scores: treat it as a fresh opening
        closing = left <= action.get("last_chance_s", 1.0)
        if abs(aim) > (action.get("last_aim_deg", 45.0) if closing else action.get("max_aim_deg", 30.0)):
            return None, 0.0, False
        max_tilt = action.get("max_tilt_deg")
        if max_tilt is not None:
            tilt = self.aim.tilt()
            if tilt is not None and tilt > max_tilt:  # off balance: kicks from 10-30 deg land 19-29% and fall more, so let it steady first
                return None, 0.0, True
        first = action.get("llk_first")
        setup = self._shot_setup(config) if action.get("wait_out_kicks") else None
        if setup and setup["them_state"] == "kick_active":
            # Their kick is on its way. Into their Left Leg Kick, mine loses: 1 of 10 landed from 0.06-0.5 s into theirs
            # (19:14-19:16 on 11-10; 14-26% before), so that one is dodged. Into anything slower, Left Leg Kick still goes if
            # it's in its spot (the pilot's call, 11-10: the bot went in and never fired as they moved to strike; 2 of 5 landed).
            # Anything else holds and dodges: started during theirs, kicks land 39% and trade; once it's spent, 54%.
            theirs = config.game.move_names.get(setup["them_move"], "")
            pick = None
            # hold_into (2026-11-14, vs a human opponent): kicks waited out like their Left Leg Kick. Our LLK fired into their RLFK/HRSK
            # landed 11 of 38 over two matches, against 34 of 44 into their retracting LLK and 24 of 37 into them idle.
            if first and len(used) <= 1 and theirs not in action.get("hold_into", ("shanghai_leg2",)):
                pick = self._first_pick(config, first, aim, distance)
            return pick or (None, 0.0, True)
        if used:
            needed = action.get("last_min", 0.2) if closing else action.get("next_min", 0.3)
            candidates = action["moves"]
        else:
            needed = action.get("open_min", 0.35)
            candidates = action["openers"]
        idle = self.aim.idle_for()
        settled = playing or closing
        if not settled and idle < action.get("min_idle_ms", 300) / 1000:
            return None, 0.0, True  # the clip only just ended, so the robot is still settling
        setup = action.get("setup")
        # openers_only (the pilot's ArmSeq-then-LLK test, 2026-11-13): the punch only goes before a chain's opener, never before a follow-up.
        if (setup and may_set_up and not playing and not closing and config.game.move_ids[setup["move"]] not in used
                and not (setup.get("openers_only") and used)
                and distance <= setup["max_dist"] and setup.get("min_aim_deg", -setup["max_aim_deg"]) <= aim <= setup["max_aim_deg"]
                and self._can_start(config, setup["move"])):
            return setup["move"], config.land.rate(setup["move"], aim, distance), True
        brawl = action.get("brawl")
        if (brawl and not used and action.get("use_model") and config.shots.moves and distance <= brawl.get("max_dist", 0.9)
                and self._shot_setup(config)["them_state"] == "punch"):
            # In the face of a punching enemy: Big Kick lands about 60% there (punches 1-8%) and often shoves it off.
            relaxed = dict(action, move_min_used={k: v for k, v in action.get("move_min_used", {}).items() if k not in brawl["moves"]})
            pick = self._model_pick(config, relaxed, brawl["moves"], used, left, closing, aim, distance, playing, idle)
            if pick[0]:
                return pick
        if first and not used:
            pick = self._first_pick(config, first, aim, distance)
            if pick:
                return pick
        rush = action.get("rush")
        approach = getattr(self.aim, "enemy_approach", None) if rush and not used else None
        speed = approach() if approach else None
        if speed is not None and speed >= rush.get("min_approach_mps", 0.4):
            # A fast charger is met further out: humans charging at 0.6 m/s or more started 33 of 98 charges past 1.5 m on
            # 2026-11-10, and Left Leg Kick into one landed 5 of 7 from 1.5-1.7 m (n is small).
            fast = speed >= rush.get("fast_mps", float("inf"))
            if distance <= (rush.get("fast_max_dist", rush.get("max_dist", 1.5)) if fast else rush.get("max_dist", 1.5)):
                # It's walking in: kick first, never back off from it. Against a robot coming in at 0.4 m/s or more, Left Leg
                # Kick lands 57-60% from 0.9-1.3 m and 52% from 1.3-1.5 m (31% if it stands); Big Kick 65-76% inside 1.1 m.
                order = rush["close_moves"] if distance <= rush.get("close_m", 1.1) else rush["moves"]
                for move in order:
                    limits = rush["limits"].get(move, {})
                    low, high = limits.get("aim", (-15.0, 25.0))
                    reach = limits.get("max_dist", 1.25)
                    if fast:
                        reach = limits.get("fast_max_dist", reach)
                    if (low <= aim <= high and distance <= reach and self._can_start(config, move)
                            and (not action.get("aim_now_too") or self._aim_now_inside((low, high)))):
                        return move, rush.get("chance", 0.55), True
        if action.get("use_model") and config.shots.moves:
            pick = self._model_pick(config, action, candidates, used, left, closing, aim, distance, playing, idle)
            if pick[0] is None and first and len(used) == 1:
                # One hit in and no follow-up worth throwing: Left Leg Kick again rather than wait in their reach (a repeat
                # still scores, and in their reach they're kicking if I'm not).
                return self._first_pick(config, first, aim, distance) or pick
            return pick
        good = action.get("good_min", 0.45)
        pick, pick_chance, top = None, 0.0, 0.0
        for move in candidates:
            if not self._move_allowed(config, action, move, used, left, aim, distance, playing, closing, idle):
                continue
            chance = config.land.rate(move, aim, distance)
            top = max(top, chance)
            if chance >= needed and self._can_start(config, move):
                if chance >= good:
                    return move, chance, True
                if chance > pick_chance:
                    pick, pick_chance = move, chance
        return pick, (pick_chance if pick else top), top >= needed

    def _first_pick(self, config, first, aim, distance):
        """Left Leg Kick whenever it's in its spot, whatever the model says. The pilot's meta (2026-11-10): land as many Left Leg
        Kicks as possible and weave the combos in; in their reach and not kicking, they are. The spot is the angle and the
        distance it will meet them at (the gap now less the closing speed over lead_s): of 4,654 of mine, from -10..+20 deg
        and due to meet them at 0.6-1.2 m they landed 62-69%, against 41% for all the rest."""
        move = ALIASES.get(first.get("move", "LLK"), first.get("move", "LLK"))
        low, high = first.get("aim", (-10.0, 20.0))
        if not (low <= aim <= high and self._aim_now_inside((low, high))):
            return None
        meet = distance - (self.aim.closing() or 0.0) * first.get("lead_s", 0.45)
        near, far = first.get("contact_m", (0.6, 1.2))
        if not near <= meet <= far or not self._can_start(config, move):
            return None
        return move, first.get("chance", 0.62), True

    def _walk_in(self, action, distance):
        """Holds W while the gap is too wide. A landed kick shoves the enemy away, and forward walks straight at them
        (about 3 deg off), so without this a chain stands and watches. Leaves it to the pilot whenever the stick is pushed."""
        walk_to = action.get("walk_to_m")
        if walk_to is None:
            return
        tilt = self.aim.tilt()
        current = self.aim.current_move()
        # Never while down or getting up: W held through a get-up laid the robot flat again and cost a knockout (22:55, 2026-11-07).
        down = (tilt is not None and tilt > 30) or bool(current and current[0] and current[0] == self.aim.chain.recovery_id)
        want = distance > walk_to and not self.stick_pushed and not down and (not self.require_focus or foreground_is_game())
        if want != ("W" in self.keyboard.held):
            (self.keyboard.down if want else self.keyboard.up)("W")

    MOVE_KEYS = ("W", "A", "S", "D")

    def _own_approach(self):
        own = getattr(self.aim, "own_approach", None)
        return own() if own else 0.0

    def _set_move_keys(self, want):
        """Holds exactly these walking keys, letting go of the rest."""
        for key in self.MOVE_KEYS:
            held = key in self.keyboard.held
            if held and key not in want:
                self.keyboard.up(key)
            elif not held and key in want:
                self.keyboard.down(key)

    def _position(self, config, action, aim, distance, candidates):
        """Where the robot goes while a chain has no shot: never a sitting duck in their reach.

        Measured against real players since the kick patch, with my robot free: standing at 0.8-1.3 m they land 0.20-0.22
        hits a second, backing off 0.05-0.08, walking in without kicking 0.25-0.37; over 1.5 m about 0.05. Nearly all of it
        comes while they're free and squared up; while their kick is spent it's about 0.02. Dodging once their kick has
        started barely helps (53% vs 61%), so it has to be done before. Walking in, Left Leg Kick reaches 1.25-1.6 m and
        lands 60-71%, and into their spent kick 64-76%.
        So: lined up and ready, step in (the kick goes the moment it's in reach; also the counter while they're locked in a
        spent kick or down), but never closer than close_m without the shot: inside that with nothing to throw, or their
        kick live in reach, or not lined up, get out to hover_m and let Auto Turning line up. The pilot's stick always wins,
        and nothing moves while the robot is in a clip, down or getting up."""
        pos = action["position"]
        want, mode = (), "hold"
        current = self.aim.current_move()
        tilt = self.aim.tilt()
        in_clip = bool(current and current[0])
        down = (tilt is not None and tilt > 30) or bool(in_clip and current[0] == self.aim.chain.recovery_id)
        if self.stick_pushed or down or in_clip or (self.require_focus and not foreground_is_game()):
            mode = "pilot" if self.stick_pushed else "busy"
        else:
            them = self._shot_setup(config)["them_state"]
            low, high = pos.get("approach_aim", (-20.0, 30.0))
            lined_up = low <= aim <= high
            ready = any(self._can_start(config, move) for move in candidates)
            close = pos.get("close_m", 1.25)
            hover = pos.get("hover_m", 1.65)
            up_for = getattr(self.aim, "enemy_up_for", None)
            getting_up = bool(up_for) and up_for() < action.get("foul_guard_ms", 0) / 1000
            hunt = action.get("finish_hunt")
            finishing = bool(hunt) and self._finishing(config, action)
            if finishing:
                close = hunt.get("close_m", close)
            if them == "kick_active" and distance <= pos.get("kick_reach_m", 1.6):
                # Still dodged one hit from a combo: a clean hit on me wipes the chain, and dodging keeps it a little more often.
                want, mode = self._backoff_keys(action, 0.4, until_m=hover) or (), "evade"
            elif lined_up and ready and distance > close:
                want, mode = ("W",), ("punish" if them in ("kick_spent", "down") else "finish" if finishing else "step in")
            elif getting_up:
                mode = "wait for it to stand"  # it can't hit back, and the hit goes 0.35 s after it stands
            elif self._lining_up(config, action, aim, distance):
                mode = "line up"  # Left Leg Kick ready and in reach, a few degrees off: still, so Auto Turning brings it in
            elif distance <= hover:
                # Back off after every hit, with two hits in or a charger coming as well: a robot standing in their reach gets
                # hit, and a clean hit on me wipes the combo. ("stand" and "finish hold" did that on 11-10 and were dropped.)
                want, mode = self._backoff_keys(action, 0.4, until_m=hover) or (), "out of reach"
            else:
                mode = "hover"
        self._set_move_keys(want)
        if mode != self.position_mode:
            self.position_mode = mode
            if want or mode in ("hover", "hold", "wait for it to stand", "line up"):
                self.session.event("position", mode=mode, keys="+".join(want), dist=round(distance, 2), aim=round(aim, 1))

    def _lining_up(self, config, action, aim, distance):
        """True for up to line_up_s while Left Leg Kick is ready, they're inside its reach and the aim is just outside its
        window. Standing still lets Auto Turning bring it in, and the kick goes the moment it's in. Walking back and to the
        side fought the turn: 19:13:14 on 11-10, a second at -12..-16 deg 0.65 m from a free robot, and no kick. After
        line_up_s it backs off as before, so it never stands there long."""
        first = action.get("llk_first")
        if not first:
            return False
        low, high = first.get("aim", (-10.0, 20.0))
        slack = first.get("line_up_deg", 20.0)
        move = ALIASES.get(first.get("move", "LLK"), first.get("move", "LLK"))
        inside = first.get("contact_m", (0.6, 1.2))[0] <= distance <= first.get("line_up_m", 1.3)
        if not (inside and low - slack <= aim <= high + slack and self._can_start(config, move)):
            self.line_up_since = None
            return False
        now = time.perf_counter()
        if self.line_up_since is None:
            self.line_up_since = now
        return now - self.line_up_since <= first.get("line_up_s", 0.5)

    def _finishing(self, config, action):
        """True while two different hits are in and a third could still land inside the combo's 4 s."""
        chain = self.aim.chain
        if len(chain.moves) < 2 or chain.last_hit is None:
            return False
        left = ComboChain.GAP_S - (time.perf_counter() - chain.last_hit)
        return left > 0 and self._follow_up_possible(config, action, chain.moves, left)

    def _shot_setup(self, config):
        setup = self.aim.shot_inputs()
        name = config.game.move_names.get(setup["them_move"], "")
        up_for = getattr(self.aim, "enemy_up_for", None)
        if up_for and up_for() == 0.0:
            setup["them_state"] = "down"  # the model has no weight for it, so it rates it like a free robot
        elif not setup["them_move"] or name == "auto_recovery" or not TurnTable.FIGHT_MOVE.search(name):
            setup["them_state"] = "free"
        elif name not in config.game.uninterruptible:
            setup["them_state"] = "punch"
        else:
            windows = config.game.windows.get(name) or []
            stop = max([w.get("stop_s", 0.0) for w in windows], default=0.0)
            # Their Left Leg Kick isn't spent when its hit window closes: mine into it landed 0 of 4 from 0.5-0.9 s in,
            # 4 of 5 from 1.1 s on (19:37-19:39, 11-10). their_live_s keeps it live that long.
            stop = max(stop, config.their_live_s.get(name, 0.0))
            setup["them_state"] = "kick_active" if stop and setup["them_elapsed"] < stop else "kick_spent"
        return setup

    def _model_pick(self, config, action, candidates, used, left, closing, aim, distance, playing, idle):
        """The move most worth throwing from here, or None if nothing clears the bar.

        Worth is the chance it lands times what that scores, less the robot's time it locks up: a lock is no more dangerous
        than standing idle (0.087 points against a second either way against real players), so its cost is the hits that
        time could have landed. The third different hit scores a combo, so it's thrown on far longer odds.
        """
        setup = self._shot_setup(config)
        finishing = len(used) >= 2
        if finishing:  # the last second may lower the finisher's bar, never raise it
            needed = action.get("finish_chance", 0.2)
            if closing:
                needed = min(needed, action.get("last_min", needed))
        elif closing:
            needed = action.get("last_min", 0.2)
        elif used:  # the second hit sets up the combo, so it may go on longer odds than a poke
            needed = action.get("next_chance", action.get("min_chance", 0.35))
        else:
            needed = action.get("min_chance", 0.35)
        value = action.get("combo_value", 4.0) if finishing else 1.0
        pick, pick_worth, pick_chance, top = None, 0.0, 0.0, 0.0
        for move in candidates:
            if not self._move_allowed(config, action, move, used, left, aim, distance, playing, closing, idle):
                continue
            chance = config.shots.chance(move, aim, distance, setup)
            top = max(top, chance)
            worth = value * chance - config.shots.lock(move) * config.shots.time_value
            need = action.get("move_min_chance", {}).get(move, needed)  # a move the model underrates gets its own bar
            if chance >= need and worth > pick_worth and self._can_start(config, move):
                pick, pick_worth, pick_chance = move, worth, chance
        return pick, (pick_chance if pick else top), top >= needed

    def _move_allowed(self, config, action, move, used, left, aim, distance, playing, closing, idle):
        """The hard limits on a move, before anything is weighed."""
        if config.game.move_ids[move] in used:
            return False
        reach = action.get("move_max_dist", {}).get(move, float("inf"))
        moving_reach = action.get("move_max_dist_moving", {}).get(move)
        if moving_reach is not None and self._own_approach() >= action.get("moving_min_mps", 0.45):
            reach = max(reach, moving_reach)  # walking in, Left Leg Kick lands 60-71% from 1.25-1.6 m (14-19% standing)
        if distance > reach:
            return False
        if len(used) < action.get("move_min_used", {}).get(move, 0):  # Big Kick is the last hit in 7 of the 11 combos: its long clip only hurts mid-chain
            return False
        if not used and distance < action.get("move_min_dist", {}).get(move, 0.0):  # up close, an opener waits for the kick that lands best there
            return False
        window = action.get("move_aim", {}).get(move)
        near = action.get("move_aim_near", {}).get(move)
        if near and distance <= action.get("near_m", 1.15):  # close in, Left Leg Kick lands 45-50% from as far left as -20 deg
            window = near
        if window and not window[0] <= aim <= window[1]:  # the kicks turn the robot as they go, so each lands from its own side of the line
            return False
        if window and action.get("aim_now_too") and not self._aim_now_inside(window):
            return False
        if not playing and not closing and idle < action.get("move_min_idle_ms", {}).get(move, 0) / 1000:
            return False
        speed_floor = action.get("move_min_closing", {}).get(move)  # Flying Side Kick 0 of 8 and Big Kick at range 2 of 14 with the gap opening
        if speed_floor is not None:
            speed = self.aim.closing()
            if speed is None or speed < speed_floor:
                return False
        hit_in = action.get("move_hit_s", {}).get(move, config.land.hit_by(move))
        return config.shift_lead_s + config.aim_lead_s + hit_in <= left  # or its hit would come too late to count

    def _aim_now_inside(self, window):
        """The current aim inside the window too, not only the predicted one: tonight (2026-11-09) 169 of 590 macro Left Leg
        Kicks were pressed on the predicted turn alone; half started outside their window and those landed 31-39%, vs 59% inside."""
        now = getattr(self, "_aim_now", None)
        return now is None or window[0] <= now <= window[1]

    def _wait_started(self, config, move, pressed):
        """True once the game shows the move started, False if it dropped the press, None if cut short."""
        move_id = config.game.move_ids[move]
        until = pressed + self.START_SEEN_S
        while time.perf_counter() < until:
            if self.cancel.is_set():
                return None
            if self.aim.started_since(move_id, pressed):
                return True
            time.sleep(0.003)
        return False

    def _chain(self, config, combo, start, action, held=None):
        """One press plays a whole combo: different moves, each hit within 4 s of the one before.

        It waits for a shot that can land, with the robot free and lined up, so the pilot only has to steer. A miss doesn't
        end it while the chain has time left, and a missed opener gets more tries. A press the game drops costs nothing:
        it's pressed again as soon as the game can take it. It stops when the combo scores or time runs out.
        """
        if self.aim is None or not config.land.moves:
            say("!! no live aim or land table: chain not run")
            return
        chain = self.aim.chain
        combos_before = chain.combos
        self.position_mode = None
        held = held or (lambda: False)  # True while hold_to_arm's button is still down: stay armed, no back-off
        open_wait = action.get("open_wait_ms", 1000) / 1000
        give_up = start + open_wait
        # Pressed while the robot is still in a clip (its own kick, or getting up), the wait only starts once it's free, up to this.
        latest = start + action.get("max_wait_ms", 0) / 1000
        opens_left = action.get("opens", 1)
        max_strikes = action.get("max_strikes", 99)  # a setup punch doesn't count
        setup_move = action["setup"]["move"] if "setup" in action else None
        setup_cfg = action.get("setup") or {}
        # cut_on_hit (the pilot's idea, 2026-11-13): punches can be cut and sometimes land, so let the setup punch play and cut it with
        # cut_with the moment one of its hits scores. If none has by cut_by_s, cut anyway while the opponent is still in reach.
        cut_with = setup_cfg.get("cut_with") if setup_cfg.get("cut_on_hit") else None
        forced, forced_until = None, 0.0
        steps = []
        dropped = 0
        aim = distance = None
        best = 0.0
        while chain.combos == combos_before and sum(not s.get("setup") for s in steps) < max_strikes:
            if self.cancel.is_set():
                return
            now = time.perf_counter()
            chain_open = chain.moves and now < chain.last_hit + ComboChain.GAP_S
            if not steps and now < latest:
                current = self.aim.current_move()
                if current and current[0]:
                    give_up = min(latest, max(give_up, now + open_wait))
            if not chain_open and (opens_left <= 0 or now >= give_up):  # a press with an old chain just running out still gets to open a new one
                if not held():
                    break
                opens_left, give_up = max(opens_left, 1), now + open_wait  # its button is still held: stay armed
            if (chain_open and steps and action.get("backoff_ms") and not held()
                    and not self._follow_up_possible(config, action, chain.moves, chain.last_hit + ComboChain.GAP_S - now)):
                break  # a poke: nothing left can land inside this combo's 4 s, so get out rather than stand there
            aim, distance = self.aim.predicted(config.aim_lead_s), self.aim.distance()
            if aim is None or distance is None:
                break
            self._aim_now = self.aim.error()  # where it faces now, not where the turn is expected to take it
            positioned = bool(action.get("position"))
            if not positioned:
                self._walk_in(action, distance)
            may_set_up = not (steps and steps[-1].get("setup"))  # after a setup punch, the kick
            if forced and now > forced_until:
                forced = None  # the game never took the cut: back to normal picking
            if forced:
                move, best, shot_open = forced, 1.0, True
            else:
                move, best, shot_open = self._chain_pick(config, action, aim, distance, now, may_set_up)
            if move is None:
                if positioned:
                    self._position(config, action, aim, distance, action["moves"] if chain_open else action["openers"])
                if not shot_open and action.get("use_recovery", True):  # nothing can land from here, so the robot has to get free to turn
                    self._free_spent(config, combo, start, action, len(steps))
                time.sleep(0.005)
                continue
            if positioned:  # a step-in kick keeps walking in; any other walk stops for the strike
                self._set_move_keys(("W",) if "W" in self.keyboard.held else ())
            idle = self.aim.idle_for()
            before = self.aim.hits()
            strike_before = self.last_strike
            pressed = self._press(config, combo, move, len(steps), start)
            if pressed is None:
                return
            started = self._wait_started(config, move, pressed)
            if started is None:
                return
            if not started:
                self.last_strike = strike_before  # the game ignored it, so no cooldown began
                dropped += 1
                continue
            forced = None
            is_setup = move == setup_move
            # A setup punch is cut as soon as the game allows. For the rest: a press takes about aim_lead_s to start the
            # move, and the score takes a little longer to reach the client.
            if is_setup and cut_with:
                until = pressed + setup_cfg.get("cut_by_s", 1.6)  # its punches get their chance to score first
            else:
                until = pressed + (config.cooldown_s if is_setup else config.aim_lead_s + config.land.hit_by(move) + 0.05)
            landed = self._wait_for_hit(before, until)
            if landed is None:
                return
            if is_setup and cut_with:
                near = self.aim.distance()
                if landed or (near is not None and near <= setup_cfg.get("cut_max_dist", 1.4)):
                    # The game drops a press inside its move-switch cooldown. cut_at_s holds the cut to a fixed moment even after a
                    # hit (the pilot's L-R Punch then Left Leg Kick, copied from another player 2026-11-13: their LLK 0.8-1.3 s after their own
                    # L-R Punch landed 10 of 13).
                    if not self._wait(pressed + max(config.cooldown_s, setup_cfg.get("cut_at_s", 0.0))):
                        return
                    forced, forced_until = cut_with, time.perf_counter() + 0.8
            step = {"move": move.replace("shanghai_", ""), "landed": landed, "aim": round(aim), "dist": round(distance, 2),
                    "chance": round(best, 2), "idle": round(idle, 2)}
            if is_setup:
                step["setup"] = True
                give_up = max(give_up, time.perf_counter() + open_wait)  # the kick still has to follow
            elif not chain_open and not landed:
                opens_left -= 1
                give_up = time.perf_counter() + open_wait
            steps.append(step)
        scored = chain.combos > combos_before
        self._set_move_keys(())
        if not steps:
            if aim is not None and distance is not None:
                say(f"   held fire: {distance:.2f} m, {aim:+.0f} deg, best chance {best:.0%}")
                self.session.event("held", combo=combo.name, dist=round(distance, 2), aim=round(aim, 1), best=round(best, 2), dropped=dropped)
            if action.get("position") and action.get("backoff_ms") and not held():
                self._back_off(config, combo, action)  # no shot came: don't leave the robot standing in their reach
            return
        hits_landed = sum(s["landed"] for s in steps)
        names = " > ".join(("+" if s["landed"] else "x") + s["move"] for s in steps)
        say(f"   chain: {hits_landed} hit{'s' if hits_landed != 1 else ''}{', COMBO' if scored else ''} [{names}]")
        self.session.event("chain", combo=combo.name, hits=hits_landed, scored=scored, steps=steps, dropped=dropped)
        if action.get("backoff_ms"):
            now = time.perf_counter()
            still_on = chain.moves and now < chain.last_hit + ComboChain.GAP_S and self._follow_up_possible(
                config, action, chain.moves, chain.last_hit + ComboChain.GAP_S - now)
            if not still_on and not held():  # a one-strike press that started a combo leaves the robot there for the next press
                self._back_off(config, combo, action)
            return
        if action.get("use_recovery", True) and self.aim.error() is not None and self._wait(self.last_strike + config.cooldown_s):
            self._free_spent(config, combo, start, action, len(steps))

    def _follow_up_possible(self, config, action, used, left):
        """True while a move not yet in the combo could still land inside its 4 s, counting the kick clip still running."""
        busy_s = 0.0
        current = self.aim.current_move()
        if current and current[0]:
            name = config.game.move_names.get(current[0], "")
            if name in config.game.uninterruptible:
                ends = config.land.ends(name)
                busy_s = max(0.0, (ends if ends != float("inf") else config.shots.lock(name)) - current[1])
        for move in action["moves"]:
            if config.game.move_ids[move] in used or len(used) < action.get("move_min_used", {}).get(move, 0):
                continue
            hit_in = action.get("move_hit_s", {}).get(move, config.land.hit_by(move))
            if busy_s + config.shift_lead_s + config.aim_lead_s + hit_in <= left:
                return True
        return False

    def _backoff_keys(self, action, hold_s, until_m=None):
        """The walk that gets out of reach: back and to the right by default, which also takes the robot off the other one's
        Left Leg Kick line (it lands from its centre to its right). Whatever keeps clear of the walls. None if no need.

        The keys walk relative to where the robot faces, and after a kick it can face anywhere (High Right Side Kick ends
        turned right round), so each walk is checked against the direction away from the other robot: S walks into it
        when this one faces away."""
        where = self.aim.positions()
        if where is None or where["distance"] > (action.get("backoff_until_m", 1.8) if until_m is None else until_m):
            return None
        (x, z), facing = where["me"], where["facing"]
        them = where.get("them")
        away = None if them is None else math.degrees(math.atan2(x - them[0], z - them[1]))
        step = action.get("backoff_speed_mps", 0.6) * hold_s
        wall_m = action.get("backoff_wall_m", 0.7)
        options = [(("S", "D"), 135.0), (("S", "A"), -135.0), (("S",), 180.0), (("D",), 90.0), (("A",), -90.0),
                   (("W", "D"), 45.0), (("W", "A"), -45.0), (("W",), 0.0)]
        scored = []
        for rank, (keys, turn) in enumerate(options):
            angle = math.radians(facing + turn)
            clear = wall_distance(x + step * math.sin(angle), z + step * math.cos(angle))
            outward = 1.0 if away is None else math.cos(angle - math.radians(away))
            scored.append((keys, clear, outward, rank))
        # Straight away first: live, S opened the gap about 0.36 m in 0.6 s, S+D 0.18 (my own speed away 0.04 m/s: with Auto
        # Turning keeping the robot facing it, back-and-right mostly circles the other robot).
        good = [s for s in scored if s[2] >= 0.6 and s[1] >= wall_m]  # within about 53 deg of straight away, clear of the walls
        if good:
            return max(good, key=lambda s: (round(s[2], 2), -s[3]))[0]
        sideways = [s for s in scored if s[2] >= -0.1]  # boxed in: at least don't walk toward it
        return max(sideways or scored, key=lambda s: s[1])[0]

    def _back_off(self, config, combo, action):
        """After a poke, a miss or a finished combo, once the kick's clip ends: walk out of reach for backoff_ms. The pilot's
        stick always wins, and nothing happens while the robot is down."""
        hold_s = action["backoff_ms"] / 1000
        until = time.perf_counter() + 4.5  # longer than any kick clip (Big Kick 3.74 s)
        self.backing_off = True
        try:
            self._back_off_once(config, combo, action, hold_s, until)
        finally:
            self.backing_off = False

    def _back_off_once(self, config, combo, action, hold_s, until):
        while True:
            if self.cancel.is_set():
                return
            current = self.aim.current_move()
            if current is None or time.perf_counter() > until:
                return
            if not current[0]:
                break
            time.sleep(0.005)
        tilt = self.aim.tilt()
        if self.stick_pushed or (tilt is not None and tilt > 45) or (self.require_focus and not foreground_is_game()):
            return
        keys = self._backoff_keys(action, hold_s)
        if not keys:
            return
        self.session.event("backoff", combo=combo.name, keys="+".join(keys))
        held = []
        try:
            for key in keys:
                self.keyboard.down(key)
                held.append(key)
            end = time.perf_counter() + hold_s
            while time.perf_counter() < end:
                tilt = self.aim.tilt()
                if self.cancel.is_set() or self.stick_pushed or (tilt is not None and tilt > 45):
                    break
                time.sleep(0.005)
        finally:
            for key in held:
                self.keyboard.up(key)

    def _tap(self, chord):
        """Presses and lets go of one chord that isn't a strike (Passive Mode, Straighten, Get Up). False if REK isn't in front."""
        if self.require_focus and not foreground_is_game():
            return False
        mods = [k for k in chord if k in ("LeftShift", "LeftCtrl")]
        keys = [k for k in chord if k not in mods]
        for mod in mods:
            self.keyboard.down(mod)
        if mods:
            self._wait_uninterrupted(time.perf_counter() + 0.025)
        for key in keys:
            self.keyboard.down(key)
        self._wait_uninterrupted(time.perf_counter() + 0.07)
        for key in reversed(keys):
            self.keyboard.up(key)
        for mod in reversed(mods):
            self.keyboard.up(mod)
        return True

    def _robot_now(self):
        current = self.aim.current_move()
        tilt = self.aim.tilt()
        return (current[0] if current else None), (None if tilt is None else round(tilt, 1))

    def _manual_tap(self, config, action):
        # Explicit presses also work after a win. Native controller activation and move gates still decide acceptance.
        if self.cancel.is_set():
            return
        command = action["command"]
        chord = config.game.manual_chords[command]
        sent = self._tap(chord)
        self.session.event("manual_tap", command=command, chord=list(chord), sent=sent)

    def _standing(self):
        move, tilt = self._robot_now()
        return move is not None and tilt is not None and move not in getattr(self.aim, "down_ids", ()) and tilt < 25

    def _rescue(self, config, action):
        """Tries to stand up a robot stuck in Recovery: each step is one chord and a wait, and it stops once the robot stands.

        Stuck looks like this (22:58, 2026-11-09, a knockout): Recovery restarting every frame for 19 s while the robot rocks
        between 1 and 80 deg. The test version lies down first (Ctrl+M) so each step's effect can be watched in Practice."""
        chords = config.game.rescue_chords
        if action.get("spam"):
            self._spam(config, action, chords[action["spam"]])
            return
        if action.get("lie_down_first"):
            if not self._tap(chords["lie_down"]):
                return
            say("   rescue test: lying down")
            until = time.perf_counter() + 3.0
            while time.perf_counter() < until and not self.cancel.is_set():
                tilt = self._robot_now()[1]
                if tilt is not None and tilt > 60:
                    break
                time.sleep(0.02)
            if not self._wait(time.perf_counter() + action.get("settle_s", 1.5)):
                return
        began = time.perf_counter()
        for name, wait_s in action["steps"]:
            if self.cancel.is_set():
                return
            if self._standing():
                break
            before = self._robot_now()
            if not self._tap(chords[name]):
                say("   rescue stopped: REK is not the focused window")
                return
            end = time.perf_counter() + float(wait_s)
            while time.perf_counter() < end and not self.cancel.is_set() and not self._standing():
                time.sleep(0.02)
            after = self._robot_now()
            say(f"   rescue {name}: move/tilt {before} -> {after}")
            self.session.event("rescue_step", step=name, before=list(before), after=list(after), at_s=round(time.perf_counter() - began, 2))
        standing = self._standing()
        say(f"   rescue {'worked: standing' if standing else 'did not stand the robot up'}")
        self.session.event("rescue_done", standing=standing, total_s=round(time.perf_counter() - began, 2))

    def _spam(self, config, action, chord):
        """Get Up again and again until the robot stands (or max_s). A get-up already playing is left to finish."""
        getting_up = config.game.move_ids.get("liyudating_getup")
        began = time.perf_counter()
        until = began + action.get("max_s", 6.0)
        taps = 0
        while time.perf_counter() < until and not self.cancel.is_set() and not self._standing():
            if self._robot_now()[0] != getting_up:
                if not self._tap(chord):
                    break
                taps += 1
            if not self._wait(time.perf_counter() + action.get("every_s", 0.15)):
                break
        standing = self._standing()
        say(f"   get-up spam: {taps} taps, {'standing' if standing else 'still down'} after {time.perf_counter() - began:.1f} s")
        self.session.event("getup_spam", taps=taps, standing=standing, total_s=round(time.perf_counter() - began, 2))

    def _wait_for_hit(self, before, until):
        """True once the clean-hit counter rises, False if it doesn't in time, None if cut short."""
        while time.perf_counter() < until:
            if self.cancel.is_set():
                return None
            hits = self.aim.hits()
            if before is not None and hits is not None and hits > before:
                return True
            time.sleep(0.003)
        return False

    def _run(self, combo, config, source):
        start = time.perf_counter()
        say(f">> {source}: {combo.name}")
        self.session.event("fire", combo=combo.name, source=source)
        target = start + config.shift_lead_s
        try:
            if combo.action:
                kind = combo.action["kind"]
                if kind == "free":
                    self._free(config, combo, start)
                elif kind == "face":
                    self._turn(config, combo, start)
                elif kind == "turn":
                    self._turn(config, combo, start, combo.action["degrees"])
                elif kind == "chain":
                    # hold_to_arm: while the pad button that fired it stays held, the chain stays armed (2026-11-10: the
                    # LLK exchange goes to whoever kicks first, and each press only armed it for open_wait_ms).
                    hold = bool(combo.action.get("hold_to_arm") and source == combo.pad and combo.pad_mask)
                    held = (lambda: bool(self.pad_now & combo.pad_mask)) if hold else (lambda: False)
                    while True:
                        began = time.perf_counter()
                        self._chain(config, combo, start, combo.action, held)
                        if self.cancel.is_set() or not held() or self.aim is None or not config.land.moves:
                            break
                        if time.perf_counter() - began < 0.05:
                            time.sleep(0.05)  # it ended at once (no live aim): don't spin
                        start = time.perf_counter()  # still held after a combo: arm again
                elif kind == "rescue":
                    self._rescue(config, combo.action)
                elif kind == "manualtap":
                    self._manual_tap(config, combo.action)
                self.session.event("done", combo=combo.name, total_s=round(time.perf_counter() - start, 4))
                return
            previous_press = start
            for i, step in enumerate(combo.steps):
                target += step.after_s
                if step.max_s and i:
                    if not self._wait(previous_press + step.min_s - config.shift_lead_s):
                        return
                    fallback = previous_press + step.after_s - config.shift_lead_s
                    deadline = previous_press + step.max_s - config.shift_lead_s
                    while time.perf_counter() < deadline:
                        if self.cancel.is_set():
                            return
                        aim = self.aim.predicted(config.aim_lead_s) if self.aim else None
                        if aim is None:
                            if time.perf_counter() >= fallback:
                                break
                        elif abs(aim) <= config.aim_ok_deg:
                            break
                        time.sleep(0.003)
                    target = time.perf_counter() + config.shift_lead_s
                actual = self._press(config, combo, step.move, i, start, target)
                if actual is None:
                    return
                previous_press = actual
            if not self._wait(actual + combo.settle_s):
                return
            self.session.event("done", combo=combo.name, total_s=round(time.perf_counter() - start, 4))
        finally:
            self.keyboard.release_all()

    @staticmethod
    def _wait_uninterrupted(until):
        # A press already in progress always gets its full hold; a short tap can miss a frame.
        while time.perf_counter() < until:
            time.sleep(0.001)


# ---------------------------------------------------------------- Player.log
class GameLogTail(threading.Thread):
    """Prints what the game itself reports, next to what the macros sent."""

    SLOT = re.compile(r"custom layout applied \(keyboard slot (-?\d+), gamepad slot (-?\d+)")
    USED = re.compile(r"queued 'move_used' t800/(\S+)=(\d+) mode=(\S+)")
    END = re.compile(r"queued 'fight_end\S*' mode=(\S+)")

    def __init__(self, player, session, keyboard_contract=None):
        super().__init__(daemon=True)
        self.player, self.session = player, session
        self.position = None
        self.keyboard_contract = keyboard_contract

    def compatible_keyboard_slot(self, slot):
        """A custom-slot exemption requires the recorded native source and the currently stored keyboard profile."""
        if self.keyboard_contract is None:
            return False
        try:
            import winreg
            contract = json.loads(self.keyboard_contract.read_text(encoding="utf8"))
            if (contract["schema"] != "rek.t800.keyboard_compatibility.v1"
                    or contract["robot_id"] != "t800" or contract["custom_slot"] != slot):
                return False
            if hashlib.sha256((BAKED / "set.json").read_bytes()).hexdigest() != contract["native_default_set_sha256"]:
                return False
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, contract["registry_path"], 0, winreg.KEY_READ) as key:
                selected, selected_kind = winreg.QueryValueEx(key, contract["keyboard_selection_registry_value"])
                raw, profile_kind = winreg.QueryValueEx(key, contract["profile_registry_value"])
            if (selected_kind != winreg.REG_DWORD or profile_kind != winreg.REG_BINARY
                    or selected & 0xffffffff != contract["keyboard_selected_signed"] & 0xffffffff):
                return False
            profile = json.loads(raw.rstrip(b"\0").decode("utf8"))
            if profile["robotId"] != "t800" or profile["slot"] != slot:
                return False
            fields = contract["keyboard_semantic_fields"]
            if not {"keyboard", "keyboardKnown", "keyboardLocomotion", "keyboardName"}.issubset(fields):
                return False
            canonical = json.dumps({name: profile[name] for name in fields}, sort_keys=True,
                                   separators=(",", ":"), ensure_ascii=False).encode("utf8")
            return hashlib.sha256(canonical).hexdigest() == contract["keyboard_semantic_sha256"]
        except (OSError, ValueError, TypeError, KeyError, UnicodeError):
            return False

    def check_slot(self, line):
        match = self.SLOT.search(line)
        if match:
            keyboard, pad = int(match.group(1)), int(match.group(2))
            self.session.event("layout", keyboard_slot=keyboard, gamepad_slot=pad)
            if keyboard == -1:
                say(f"game layout: keyboard built-in table, gamepad Custom {pad + 1}")
            elif self.compatible_keyboard_slot(keyboard):
                say(f"game layout: keyboard Custom {keyboard + 1} matches the verified native table, gamepad Custom {pad + 1}")
            else:
                say(f"!! keyboard now uses Custom {keyboard + 1}: the built-in key table in this "
                    "harness no longer matches, so macros will play the wrong moves")

    def run(self):
        while True:
            try:
                self.step()
            except OSError:
                pass
            time.sleep(0.5)

    def step(self):
        size = PLAYER_LOG.stat().st_size
        if self.position is None:
            self.position = size
            text = PLAYER_LOG.read_text(encoding="utf8", errors="replace")
            for line in reversed(text.splitlines()):
                if self.SLOT.search(line):
                    self.check_slot(line)
                    break
            return
        if size < self.position:  # the game restarted and began a new log
            self.position = 0
        if size == self.position:
            return
        with open(PLAYER_LOG, "rb") as log:
            log.seek(self.position)
            chunk = log.read()
        cut = chunk.rfind(b"\n") + 1
        self.position += cut
        for line in chunk[:cut].decode("utf8", "replace").splitlines():
            self.check_slot(line)
            end = self.END.search(line)
            if end:
                sent = dict(self.player.sent)
                self.player.sent.clear()
                say(f"fight ended ({end.group(1)}). macros sent: {sent or 'nothing'}")
                self.session.event("fight_end", mode=end.group(1), sent=sent)
            used = self.USED.search(line)
            if used:
                say(f"   game counted {used.group(1)} = {used.group(2)}")
                self.session.event("move_used", move=used.group(1), n=int(used.group(2)), mode=used.group(3))


# ---------------------------------------------------------------- main
def load_config(path, game):
    return Config(json.loads(path.read_text(encoding="utf8")), game)


def print_combos(config, game, out=print):
    for combo in config.combos:
        out(describe(combo, config, game))


def watch_pad():
    say("Press pad buttons (Ctrl+C to quit).")
    pad, previous = Pad(), 0
    while True:
        mask = pad.buttons()
        pressed = mask & ~previous
        previous = mask
        for bit in range(32):
            if pressed >> bit & 1:
                say(f"pad: {PAD_NAMES.get(bit, f'button{bit}')}")
        time.sleep(0.004)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=HERE / "combos.json")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--watch-pad", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-startup-key-release", action="store_true",
                        help="skip stale-key cleanup after a clean helper shutdown")
    parser.add_argument("--keyboard-contract", type=Path,
                        help="verify a preserved native keyboard table before accepting its custom slot")
    parser.add_argument("--game-dir", type=Path, help="the folder that holds REK.exe (found automatically when omitted)")
    args = parser.parse_args()

    global GAME_DIR, BAKED
    found = rek_paths.find_game_dir(args.game_dir)
    if found is None:
        print(rek_paths.NOT_FOUND)
        return 2
    GAME_DIR, BAKED = found, found.joinpath(*rek_paths.BAKED_PARTS)

    winmm.timeBeginPeriod(1)
    kernel32.SetPriorityClass(wintypes.HANDLE(-1), 0x8000)  # this process, above normal: the hook and the presses never queue behind the game
    (HERE / "logs").mkdir(exist_ok=True)
    threading.Thread(target=console_writer, daemon=True).start()
    sys.excepthook = report_crash
    threading.excepthook = lambda a: report_crash(a.exc_type, a.exc_value, a.exc_traceback)
    if args.watch_pad:
        try:
            watch_pad()
        except KeyboardInterrupt:
            return 0

    game = GameData()
    config = load_config(args.config, game)
    if args.list:
        for move, chord in game.chords.items():
            print(f"{'+'.join(chord):22} {game.titles.get(move, move)} ({move})")
        print()
        print_combos(config, game)
        return 0

    kernel32.CreateMutexW.restype = wintypes.HANDLE
    mutex = kernel32.CreateMutexW(None, False, "Local\\rek_combo_harness")
    if ctypes.get_last_error() == 183:  # already exists: two copies would double-fire every press
        say("rek_combo is already running in another window.")
        time.sleep(0.2)
        return 1

    keyboard = Keyboard(dry_run=args.dry_run)
    if not args.no_startup_key_release:
        for name in ("LeftShift", *"YUHJIOKL"):  # a copy killed mid-chord leaves its keys logically down
            keyboard._send(SCAN[name], False)
    session = Session()
    aim = Aim()
    aim.offset_deg = config.facing_offset_deg
    aim.chain.recovery_id = game.move_ids["auto_recovery"]
    aim.down_ids = frozenset(game.move_ids[m] for m in ("auto_recovery", "liyudating_getup", "liyudating", "stance_to_supine") if m in game.move_ids)
    aim.strike_ids = strike_move_ids(game, aim.down_ids)
    aim.start()
    player = Player(keyboard, session, require_focus=not args.dry_run, aim=aim)

    # Closing the window must not leave Shift logically held down.
    handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

    def on_console_event(_event):
        player.cancel.set()
        keyboard.release_all()
        return False

    handler = handler_type(on_console_event)
    kernel32.SetConsoleCtrlHandler(handler, True)

    say(f"REK combo macros{' (DRY RUN: no keys sent)' if args.dry_run else ''}")
    print_combos(config, game, say)
    say(f"Abort: F9, or any attack/shoulder button on the pad. Saving {args.config.name} applies it at once.")
    say(f"Log: {session.path}")
    GameLogTail(player, session, args.keyboard_contract).start()

    sys.setswitchinterval(0.001)  # the keyboard hook must never wait long for Python
    triggers = KeyTriggers()
    triggers.watched = frozenset(combo.vk for combo in config.combos if combo.vk is not None)
    triggers.start()
    if not triggers.ready.wait(3):
        say("!! keyboard hook did not start: combo keys will not work")

    pad = Pad()
    input_log = InputLog(session)
    previous_pad = pad.buttons()
    abort_was_down = False
    config_time = args.config.stat().st_mtime
    next_config_check = 0.0
    next_state_log = 0.0
    previous_hits = previous_round = previous_referee = None
    stuck_since, stuck_noted = None, False  # how long my robot has been down or in Recovery, and whether that's been acted on
    falling_ids = aim.down_ids - {game.move_ids.get("liyudating_getup")}  # going over; a get-up playing is on its way up
    was_going_over = rearm = False  # going over at the last look; a rescue cut a chain, so arm it again if it's still held
    try:
        while True:
            now = time.perf_counter()
            state = aim.state
            if state:
                me, them = state["me"], state["them"]
                snapshot = dict(dist=round(state["distance"], 2), bearing=round(state["bearing"], 1),
                                aim=round(aim.error() or 0.0, 1),
                                me=[round(me["x"], 2), round(me["z"], 2), round(me["heading"], 1), me["move"], me["elapsed"], round(me["tilt"], 1)],
                                them=[round(them["x"], 2), round(them["z"], 2), round(them["heading"], 1), them["move"], them["elapsed"], round(them["tilt"], 1)])
                if not (state["phase"] == 1 and (me["move"] in aim.down_ids or me["tilt"] > 45)):
                    stuck_since, stuck_noted = None, False
                elif stuck_since is None:
                    stuck_since = now
                elif not stuck_noted and now - stuck_since >= config.rescue_stuck_s:
                    stuck_noted = True  # once per episode: logged always, acted on only when the rescue is switched on
                    acting = bool(config.rescue_auto and config.rescue_combo)
                    session.event("stuck", seconds=round(now - stuck_since, 1), rescue=acting, **snapshot)
                    say(f"!! stuck down {now - stuck_since:.1f} s" + (": trying to get up" if acting else " (auto rescue is off)"))
                    if acting and player.fire(config.rescue_combo, config, "stuck"):
                        rearm = True
                going_over = state["phase"] == 1 and (me["move"] in falling_ids or me["tilt"] > config.rescue_tilt_deg)
                if going_over and not was_going_over and config.rescue_auto and config.rescue_spam:
                    session.event("going_over", **snapshot)
                    if player.fire(config.rescue_spam, config, "going over"):
                        rearm = True
                was_going_over = going_over
                hits = state["hits"]
                if hits and previous_hits and state["round"] == previous_round and hits != previous_hits:
                    # A landed clean hit, with where both fighters were at that moment.
                    session.event("hit", mine=hits[0] - previous_hits[0], theirs=hits[1] - previous_hits[1],
                                  round=state["round"], total=hits, **snapshot)
                previous_hits, previous_round = hits, state["round"]
                referee = state["referee"]
                if previous_referee is not None and referee["seq"] != previous_referee:
                    session.event("referee", call=referee["call"], faller=referee["faller"],
                                  points=referee["points"], local=state["local"], **snapshot)
                    say(f"referee call {referee['call']} faller {referee['faller']} points {referee['points']}")
                previous_referee = referee["seq"]
                if now >= next_state_log:  # both fighters' pose and move, so any press can be reviewed later
                    next_state_log = now + 0.1
                    session.event("state", phase=state["phase"], hits=hits, health=state["health"], **snapshot)
            mask = pad.buttons()
            player.pad_now = mask
            pressed = mask & ~previous_pad
            input_log.update(pressed, previous_pad & ~mask, pad.axes, now)
            previous_pad = mask
            if pressed & MOVE_BUTTONS:
                player.pilot_at = now
            player.stick_pushed = bool(pad.axes) and max(abs(pad.axes[0]), abs(pad.axes[1])) > 0.3

            fired = False
            for combo in config.combos:
                if combo.pad_mask and pressed & combo.pad_mask:
                    fired = player.fire(combo, config, combo.pad) or fired
            while not triggers.pressed.empty():
                vk = triggers.pressed.get()
                for combo in config.combos:
                    if combo.vk == vk:
                        fired = player.fire(combo, config, combo.key) or fired
            if rearm and not player.busy:  # the rescue is done: a hold_to_arm chain still held through the fall arms again
                rearm = False
                for combo in config.combos:
                    if combo.pad_mask and mask & combo.pad_mask and combo.action and combo.action.get("hold_to_arm"):
                        fired = player.fire(combo, config, combo.pad) or fired
                        break

            while not triggers.other_keys.empty():
                session.event("key", key=chr(triggers.other_keys.get()),
                              shift=bool(user32.GetAsyncKeyState(0xA0) & 0x8000))

            abort_down = bool(user32.GetAsyncKeyState(config.abort_vk) & 0x8000)
            if abort_down and not abort_was_down:
                player.stop("abort key")
            abort_was_down = abort_down
            if config.abort_on_pilot_buttons and not fired and pressed & PILOT_BUTTONS:
                player.stop("pilot pressed " + "+".join(pad_names(pressed & PILOT_BUTTONS)))

            if now >= next_config_check:
                next_config_check = now + 0.25
                if (HERE / "STOP").exists() and not player.busy:  # a clean stop, never mid-chord
                    (HERE / "STOP").unlink()
                    say("stopped on request")
                    break
                try:
                    stamp = args.config.stat().st_mtime
                    if stamp != config_time:
                        config_time = stamp
                        config = load_config(args.config, game)
                        triggers.watched = frozenset(c.vk for c in config.combos if c.vk is not None)
                        aim.offset_deg = config.facing_offset_deg
                        say("combos.json reloaded")
                        print_combos(config, game, say)
                        session.event("reload", raw=json.loads(args.config.read_text(encoding="utf8")))
                except Exception as error:  # no saved file, however wrong, may stop a running harness
                    say(f"!! combos.json not applied, still using the previous version: {error!r}")
            time.sleep(0.002)
    except KeyboardInterrupt:
        pass
    finally:
        player.cancel.set()
        keyboard.release_all()
        kernel32.CloseHandle(wintypes.HANDLE(mutex))
    return 0


if __name__ == "__main__":
    sys.exit(main())
