#!/usr/bin/env python3
"""Finds the REK install and REK's user folder on this PC. Standard library only.

The game folder is looked for in this order:
  1. the REK_GAME_DIR environment variable (or --game-dir on rek_combo.py)
  2. the folder of a running REK.exe
  3. every Steam library listed in Steam's libraryfolders.vdf
  4. the default Steam folder

  python rek_paths.py      print what was found
"""
import ctypes
import os
import re
import sys
from ctypes import wintypes
from pathlib import Path

DEFAULT_GAME_DIR = Path(r"C:\Program Files (x86)\Steam\steamapps\common\REK Alpha Test")
BAKED_PARTS = ("REK_Data", "StreamingAssets", "rek", "motions", "t800_default", "baked")


def is_game_dir(path):
    return path is not None and (Path(path).joinpath(*BAKED_PARTS) / "set.json").is_file()


def running_game_dir():
    """The folder of a running REK.exe, or None."""
    if sys.platform != "win32":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
    kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
    kernel32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                     ctypes.POINTER(wintypes.DWORD))
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return None
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() == "rek.exe":
                handle = kernel32.OpenProcess(0x1000, False, entry.th32ProcessID)  # QUERY_LIMITED_INFORMATION only
                if handle:
                    try:
                        size = wintypes.DWORD(1024)
                        buffer = ctypes.create_unicode_buffer(size.value)
                        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                            folder = Path(buffer.value).parent
                            if is_game_dir(folder):
                                return folder
                    finally:
                        kernel32.CloseHandle(handle)
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return None


def steam_libraries():
    """Every Steam library folder on this PC, the main Steam folder first."""
    roots = []
    try:
        import winreg
        for hive, key, value in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                                 (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
                                 (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath")):
            try:
                with winreg.OpenKey(hive, key) as handle:
                    roots.append(Path(winreg.QueryValueEx(handle, value)[0]))
            except OSError:
                continue
    except ImportError:
        pass
    roots.append(DEFAULT_GAME_DIR.parents[2])
    libraries = []
    for root in roots:
        for library in [root] + _listed_libraries(root / "steamapps" / "libraryfolders.vdf"):
            if library not in libraries:
                libraries.append(library)
    return libraries


def _listed_libraries(vdf):
    try:
        text = vdf.read_text(encoding="utf8", errors="replace")
    except OSError:
        return []
    return [Path(m.replace("\\\\", "\\")) for m in re.findall(r'"path"\s+"([^"]+)"', text)]


def find_game_dir(override=None):
    """The REK folder, or None when none of the places above holds the game."""
    for candidate in (override, os.environ.get("REK_GAME_DIR")):
        if candidate:
            return Path(candidate) if is_game_dir(candidate) else None
    found = running_game_dir()
    if found:
        return found
    for library in steam_libraries():
        common = library / "steamapps" / "common"
        if is_game_dir(common / DEFAULT_GAME_DIR.name):
            return common / DEFAULT_GAME_DIR.name
        try:
            for folder in sorted(common.iterdir()):
                if folder.name.lower().startswith("rek") and is_game_dir(folder):
                    return folder
        except OSError:
            continue
    return None


def game_dir(override=None):
    """The REK folder, falling back to the default Steam path so callers can report what is missing."""
    return find_game_dir(override) or Path(override or os.environ.get("REK_GAME_DIR") or DEFAULT_GAME_DIR)


def baked_dir(override=None):
    return game_dir(override).joinpath(*BAKED_PARTS)


def set_json(override=None):
    return baked_dir(override) / "set.json"


def user_dir():
    """Where REK keeps Player.log and its strings: %USERPROFILE%\\AppData\\LocalLow\\REK\\REK."""
    return Path(os.environ.get("USERPROFILE", str(Path.home()))) / "AppData" / "LocalLow" / "REK" / "REK"


NOT_FOUND = ("REK was not found. Start REK first, or point to the folder that holds REK.exe with\n"
             "  --game-dir \"D:\\SteamLibrary\\steamapps\\common\\REK Alpha Test\"\n"
             "or the REK_GAME_DIR environment variable.")


if __name__ == "__main__":
    found = find_game_dir(sys.argv[1] if len(sys.argv) > 1 else None)
    print(f"game folder: {found}" if found else NOT_FOUND)
    print(f"user folder: {user_dir()}  (Player.log {'found' if (user_dir() / 'Player.log').exists() else 'missing'})")
