#!/usr/bin/env python3
"""Read-only view of REK's fight state from another process.

Opens REK with query and read rights only: no writes, no injection, no thread
suspension. Classes and fields are found by name in the game's own IL2CPP tables
each time, so a patch that moves offsets does not break it.

  python rek_mem.py --dump     print the fight classes' pose-related fields
"""
import argparse
import ctypes
import json
import math
import re
import struct
import sys
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE = HERE / "logs" / "class_cache.json"
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("th32ModuleID", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("GlblcntUsage", wintypes.DWORD), ("ProccntUsage", wintypes.DWORD), ("modBaseAddr", ctypes.c_void_p),
                ("modBaseSize", wintypes.DWORD), ("hModule", wintypes.HMODULE), ("szModule", wintypes.WCHAR * 256),
                ("szExePath", wintypes.WCHAR * 260)]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]


kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
kernel32.ReadProcessMemory.argtypes = (wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                       ctypes.POINTER(ctypes.c_size_t))
kernel32.ReadProcessMemory.restype = wintypes.BOOL
kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.Module32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W))
kernel32.Module32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W))
kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
INVALID_HANDLE = ctypes.c_void_p(-1).value


class Unavailable(Exception):
    """The value is not readable right now, usually because no fight is loaded."""


def find_pid(exe="rek.exe"):
    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() == exe:
                return entry.th32ProcessID
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    raise Unavailable("REK is not running")


class Process:
    def __init__(self, pid):
        self.handle = kernel32.OpenProcess(0x0410, False, pid)  # QUERY_INFORMATION | VM_READ, nothing else
        if not self.handle:
            raise Unavailable(f"cannot open REK (error {ctypes.get_last_error()})")
        self.base, self.size, self.module_path = self._module(pid, "gameassembly.dll")

    def _module(self, pid, name):
        for _ in range(5):
            snapshot = kernel32.CreateToolhelp32Snapshot(0x18, pid)
            if snapshot != INVALID_HANDLE:
                break
            time.sleep(0.05)
        else:
            raise Unavailable("module list unavailable")
        entry = MODULEENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        try:
            ok = kernel32.Module32FirstW(snapshot, ctypes.byref(entry))
            while ok:
                if entry.szModule.lower() == name:
                    return entry.modBaseAddr, entry.modBaseSize, Path(entry.szExePath)
                ok = kernel32.Module32NextW(snapshot, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snapshot)
        raise Unavailable(f"{name} is not loaded")

    def close(self):
        if self.handle:
            kernel32.CloseHandle(self.handle)
            self.handle = None

    def read(self, address, count):
        if not 0x10000 <= address < 0x7FFFFFFFFFFF:
            raise Unavailable("address out of range")
        buffer = ctypes.create_string_buffer(count)
        got = ctypes.c_size_t()
        if not kernel32.ReadProcessMemory(self.handle, address, buffer, count, ctypes.byref(got)) or got.value != count:
            raise Unavailable("memory not readable")
        return buffer.raw

    def ptr(self, address):
        return struct.unpack("<Q", self.read(address, 8))[0]

    def text(self, address, limit=96):
        return self.read(address, limit).split(b"\0", 1)[0].decode("ascii", "strict")

    def data_sections(self):
        header = self.read(self.base, 0x1000)
        pe = struct.unpack_from("<I", header, 0x3C)[0]
        sections, optional = struct.unpack_from("<H", header, pe + 6)[0], struct.unpack_from("<H", header, pe + 20)[0]
        for i in range(sections):
            row = pe + 24 + optional + i * 40
            virtual_size, rva = struct.unpack_from("<II", header, row + 8)
            flags = struct.unpack_from("<I", header, row + 36)[0]
            if flags & 0x80000000:  # writable: where il2cpp keeps its class pointers
                yield rva, virtual_size


class Il2cpp:
    """Finds classes and field offsets by name using the game's own runtime tables."""
    NAME_AT, NAMESPACE_AT = 0x10, 0x18

    def __init__(self, process):
        self.p = process
        self.fields_at = None
        self.layouts = {}

    def class_name(self, klass):
        return self.p.text(self.p.ptr(klass + self.NAMESPACE_AT)) + "." + self.p.text(self.p.ptr(klass + self.NAME_AT))

    def find_class_rva(self, name):
        """Finds the global slot that holds a class pointer. Cached per game file, since a scan takes seconds."""
        stamp = self.p.module_path.stat()
        key = f"{stamp.st_size}-{int(stamp.st_mtime)}-{name}"
        cache = {}
        if CACHE.exists():
            cache = json.loads(CACHE.read_text(encoding="utf8"))
            if key in cache:
                rva = cache[key]
                klass = self.p.ptr(self.p.base + rva)
                if self.p.text(self.p.ptr(klass + self.NAME_AT), 64) == name:
                    return rva
        wanted = name.encode() + b"\0"
        p = self.p
        low, high, base, end = 0x10000, 0x7FFFFFFFFFFF, p.base, p.base + p.size
        checked = set()
        for section_rva, length in p.data_sections():
            for offset in range(0, length, 1 << 20):
                try:
                    chunk = p.read(p.base + section_rva + offset, min(1 << 20, length - offset) & ~7)
                except Unavailable:
                    continue
                for index, candidate in enumerate(memoryview(chunk).cast("Q")):
                    # a heap pointer: 8-aligned, in user space, outside the module itself; each one is checked once
                    if candidate & 7 or not low < candidate < high or base <= candidate < end or candidate in checked:
                        continue
                    checked.add(candidate)
                    try:
                        if p.read(p.ptr(candidate + self.NAME_AT), len(wanted)) == wanted:
                            rva = section_rva + offset + index * 8
                            cache[key] = rva
                            CACHE.parent.mkdir(exist_ok=True)
                            CACHE.write_text(json.dumps(cache), encoding="utf8")
                            return rva
                    except Unavailable:
                        continue
        raise Unavailable(f"class {name} not found")

    def fields(self, klass):
        """Returns {field name: offset} for a class, cached per class pointer."""
        if klass in self.layouts:
            return self.layouts[klass]
        p = self.p
        if self.fields_at is None:
            for at in range(0x40, 0x100, 8):
                try:
                    table = p.ptr(klass + at)
                    if struct.unpack("<QQQ", p.read(table, 24))[2] == klass:
                        self.fields_at = at
                        break
                except Unavailable:
                    continue
            else:
                raise Unavailable("field table not found")
        table = p.ptr(klass + self.fields_at)
        layout = {}
        for i in range(2000):
            try:
                name_ptr, _type, parent, offset = struct.unpack("<QQQi", p.read(table + i * 0x20, 28))
                if parent != klass:
                    break
                layout[p.text(name_ptr)] = offset
            except (Unavailable, UnicodeError):
                break
        self.layouts[klass] = layout
        return layout


def wrap_degrees(value):
    return (value + 180.0) % 360.0 - 180.0


class FightReader:
    """Live position, heading and current move of both fighters, from what the server sends the client."""
    STRIDE = 0x48  # one BoneSnapshot: its last field (a pointer) sits at +0x50 counting the 0x10 object header
    SLOTS = 8

    def __init__(self):
        self.process = None
        self.pid = None

    def _connect(self):
        pid = find_pid()
        if self.process is not None and pid == self.pid:
            return
        if self.process is not None:
            self.process.close()
            self.process = None
        # REK can still be loading here, so nothing counts as connected until all of this has worked.
        process = Process(pid)
        try:
            il2cpp = Il2cpp(process)
            slot_rva = il2cpp.find_class_rva("FightCoordinator")
            klass = process.ptr(process.base + slot_rva)
            fc = il2cpp.fields(klass)
        except Exception:
            process.close()
            raise
        self.process, self.pid, self.il2cpp, self.slot_rva, self.klass, self.fc = process, pid, il2cpp, slot_rva, klass, fc

    def read(self):
        """Returns the fight state, or raises Unavailable when no fight is loaded."""
        try:
            self._connect()
            p = self.process
            coordinator = p.ptr(p.ptr(self.klass + 0xA0))
            if not coordinator or p.ptr(coordinator) != self.klass:
                raise Unavailable("no fight coordinator")
            local = struct.unpack("<i", p.read(coordinator + self.fc["localFighterIndex"], 4))[0]
            phase = struct.unpack("<i", p.read(coordinator + self.fc["currentPhase"], 4))[0]
            array = p.ptr(coordinator + self.fc["fighters"])
            if not array or p.ptr(array + 0x18) != 2:
                raise Unavailable("no fighters")
            robots = struct.unpack("<QQ", p.read(array + 0x20, 16))
            if not all(robots):
                raise Unavailable("fighters not loaded")
            ring_at = self.il2cpp.fields(p.ptr(robots[0]))["snapshotRing"]
            fighters = []
            for robot in robots:
                ring = p.ptr(robot + ring_at)
                raw = p.read(ring + 0x20, self.STRIDE * self.SLOTS)
                newest, best = -1.0, 0
                for i in range(self.SLOTS):
                    received = struct.unpack_from("<f", raw, i * self.STRIDE)[0]
                    if received > newest:
                        newest, best = received, i
                base = best * self.STRIDE
                move = struct.unpack_from("<H", raw, base + 0x0A)[0]
                elapsed = struct.unpack_from("<H", raw, base + 0x0C)[0] / 100.0
                x, y, z = struct.unpack_from("<fff", raw, base + 0x10)
                qx, qy, qz, qw = struct.unpack_from("<ffff", raw, base + 0x1C)
                heading = math.degrees(math.atan2(2 * (qw * qy + qx * qz), 1 - 2 * (qy * qy + qx * qx)))
                tilt = math.degrees(math.acos(max(-1.0, min(1.0, 1 - 2 * (qx * qx + qz * qz)))))  # body up vs world up
                fighters.append({"at": newest, "x": x, "y": y, "z": z, "heading": heading, "tilt": tilt, "move": move, "elapsed": elapsed})
            hits = health = falls = None
            round_number = 0
            current_round = p.ptr(coordinator + self.fc["currentRound"])
            if current_round:
                round_fields = self.il2cpp.fields(p.ptr(current_round))
                round_number = struct.unpack("<i", p.read(current_round + round_fields["RoundNumber"], 4))[0]
                hits = self._pair(current_round + round_fields["CleanHits"], "<2i")
                falls = self._pair(current_round + round_fields["Falls"], "<2i")
                health = self._pair(current_round + round_fields["Health"], "<2f")
            call_seq, call, faller, call_points = p.read(coordinator + self.fc["refereeCallSeq"], 4)
            human = not p.read(coordinator + self.fc["opponentIsAI"], 1)[0]
        except (Unavailable, KeyError, struct.error, UnicodeError) as error:
            self.process = None if isinstance(error, KeyError) else self.process
            raise Unavailable(str(error)) from error
        if local not in (0, 1):
            raise Unavailable("not a fighter in this fight")
        me, them = fighters[local], fighters[1 - local]
        dx, dz = them["x"] - me["x"], them["z"] - me["z"]

        def ordered(pair):  # always [mine, theirs]
            return None if pair is None else [pair[local], pair[1 - local]]

        return {"phase": phase, "local": local, "me": me, "them": them, "human": human, "distance": math.hypot(dx, dz),
                "bearing": math.degrees(math.atan2(dx, dz)), "round": round_number, "hits": ordered(hits),
                "falls": ordered(falls), "health": ordered(health),
                "referee": {"seq": call_seq, "call": call, "faller": faller, "points": call_points}}

    def _pair(self, array_field, fmt):
        array = self.process.ptr(array_field)
        if not array or self.process.ptr(array + 0x18) != 2:
            return None
        return struct.unpack(fmt, self.process.read(array + 0x20, 8))


def log_fight(seconds, path):
    reader = FightReader()
    end = time.perf_counter() + seconds
    rows = misses = 0
    with open(path, "w", encoding="utf8") as out:
        while time.perf_counter() < end:
            try:
                state = reader.read()
                state["t"] = datetime.now().isoformat(timespec="milliseconds")
                out.write(json.dumps(state) + "\n")
                rows += 1
            except Unavailable:
                misses += 1
            time.sleep(0.02)
    print(f"{rows} samples, {misses} unavailable, saved {path}")


def dump():
    pid = find_pid()
    process = Process(pid)
    il2cpp = Il2cpp(process)
    started = time.perf_counter()
    rva = il2cpp.find_class_rva("FightCoordinator")
    klass = process.ptr(process.base + rva)
    print(f"class {il2cpp.class_name(klass)} via rva {rva:#x} ({time.perf_counter() - started:.1f} s)")
    fields = il2cpp.fields(klass)
    print(f"fields found: {len(fields)} (table at +{il2cpp.fields_at:#x})")
    pattern = re.compile(r"(?i)fighter|robot|local|phase|round|slot|opponent|target")
    print("FightCoordinator:", {k: hex(v) for k, v in fields.items() if pattern.search(k)})

    statics = None
    for at in (0xA0, 0xA8, 0xB0, 0xB8, 0x98, 0x90):
        try:
            candidate = process.ptr(klass + at)
            instance = process.ptr(candidate)
            if instance and process.ptr(instance) == klass:
                statics, coordinator = at, instance
                break
        except Unavailable:
            continue
    if statics is None:
        print("no live FightCoordinator: be in a fight and run this again")
        return 1
    print(f"static fields at +{statics:#x}, coordinator {coordinator:#x}")

    robots = None
    for name, offset in fields.items():
        if not re.search(r"(?i)fighter|robot", name):
            continue
        try:
            array = process.ptr(coordinator + offset)
            if process.ptr(array + 0x18) == 2:
                first = process.ptr(array + 0x20)
                if first and il2cpp.class_name(process.ptr(first)) == "REKApp.Robot":
                    robots = [first, process.ptr(array + 0x28)]
                    print(f"fighters: field {name} at {offset:#x}")
                    break
        except (Unavailable, UnicodeError):
            continue
    if not robots:
        print("fighters not found: be in a fight and run this again")
        return 1
    robot_klass = process.ptr(robots[0])
    robot_fields = il2cpp.fields(robot_klass)
    print(f"Robot fields: {len(robot_fields)}")
    pose = re.compile(r"(?i)pos|rot|quat|yaw|root|transform|aim|face|head|pelvis|torso|handle|body|site|target")
    interesting = {k: v for k, v in robot_fields.items() if pose.search(k)}
    print("pose-like:", {k: hex(v) for k, v in interesting.items()})
    print("\nvalues as three floats, for each fighter, twice 0.5 s apart:")
    for round_number in range(2):
        for name, offset in interesting.items():
            rows = []
            for robot in robots:
                try:
                    rows.append(struct.unpack("<fff", process.read(robot + offset, 12)))
                except Unavailable:
                    rows.append(None)
            if all(r and all(abs(v) < 1000 and (v == 0 or abs(v) > 1e-6) for v in r) for r in rows) and any(any(r) for r in rows):
                print(f"  {name:32} " + " | ".join(" ".join(f"{v:8.3f}" for v in r) for r in rows))
        print("  ---")
        time.sleep(0.5)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dump", action="store_true")
    parser.add_argument("--log", type=float, metavar="SECONDS", help="record the fight state to a file")
    parser.add_argument("--out", type=Path, default=HERE / "logs" / "fight_state.jsonl")
    arguments = parser.parse_args()
    if arguments.log:
        log_fight(arguments.log, arguments.out)
        sys.exit(0)
    if arguments.dump:
        try:
            sys.exit(dump())
        except Unavailable as error:
            print("unavailable:", error)
            sys.exit(1)
    parser.print_help()
