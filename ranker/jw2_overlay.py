#!/usr/bin/env python
"""Observer overlay and replay auto-saver for Jurassic Primitive War 2: The Ranker.

The game binary has ASLR disabled and keeps a per-player, per-unit-type
counter table at image RVA 0x307430. The table is laid out as:

    player_index * 0x2A8 + unit_type * 4

0x2A8 bytes is 170 dword counters per player. Several AI routines iterate
only the first 0x60 unit types, so this tool shows both totals.
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import json
import os
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

import psutil


try:
    import tkinter as tk
except Exception:  # pragma: no cover - tkinter availability is environment-specific.
    tk = None


try:
    from PIL import Image, ImageDraw, ImageTk
except Exception:  # pragma: no cover - image support is optional for text/console use.
    Image = None
    ImageDraw = None
    ImageTk = None


try:
    import win32gui
    import win32process
except Exception:  # pragma: no cover - optional window positioning.
    win32gui = None
    win32process = None


ORIGINAL_IMAGE_BASE = 0x00400000

UNIT_COUNT_TABLE_VA = 0x00707430
PLAYER_STRIDE = 0x2A8
MAX_PLAYERS = 8
AI_UNIT_TYPE_COUNT = 0x60
ALL_UNIT_TYPE_COUNT = 0xAA

PLAYER_STATUS_BYTES_VA = 0x012448F0
PLAYER_STATUS_DWORDS_VA = 0x007251F4
LOCAL_PLAYER_INDEX_VA = 0x00725100
PLAYER_NAME_TABLE_VA = 0x00725104
PLAYER_NAME_STRIDE = 0x14
PLAYER_NAME_SIZE = 0x14
GAME_HEARTBEAT_TICK_VA = 0x007071A4
GAME_HEARTBEAT_STALE_SECONDS = 1.5
SAVE_REPLAY_VA = 0x004A0480

REPLAY_TMP = "Replay.tmp"
REPLAY_DIR = "Replays"
REPLAY_MAGIC = b"Jwar2 Replay File."
PLY_MAGIC = b"TRC\x1a"
REPLAY_SLOT_TYPE_TABLE_OFFSET = 0xAB
SLOT_HUMAN = 0x00
SLOT_COMPUTER = 0x01
SLOT_SPECTATOR = 0x02
SLOT_EMPTY = 0x14

OBJECT_LIST_HEAD_OFFSET_VA = 0x007071D4
OBJECT_POOL_BASE_VA = 0x00A03FB8
OBJECT_OWNER_OFFSET = 0x04
OBJECT_PRODUCTION_STATE_OFFSET = 0x60
OBJECT_PRODUCTION_TIMER_OFFSET = 0x64
OBJECT_PRODUCTION_TARGET_OFFSET = 0x68
OBJECT_PRODUCTION_ACTIVE_STATE = 81
OBJECT_BUILD_PROGRESS_OFFSET = 0x2C
OBJECT_BUILD_FLAG_OFFSET = 0x30
OBJECT_BUILD_ACTIVE_STATE = 1
OBJECT_MAX_HEALTH_OFFSET = 0x10
OBJECT_CURRENT_HEALTH_OFFSET = 0x18
OBJECT_UPGRADE_PROGRESS_OFFSET = 0x2C
OBJECT_UPGRADE_TARGET_OFFSET = 0x68
OBJECT_UPGRADE_ACTIVE_STATE = 78
OBJECT_UPGRADE_TOTAL_TICKS = 1400
OBJECT_NEXT_OFFSET = 0x1CC

TYPE_INDEX_TABLE_VA = 0x0087C050
TYPE_INFO_BASE_VA = 0x0087C2F8
TYPE_PRODUCTION_TICKS_OFFSET = 0x18C
TYPE_UPGRADE_COUNT_OFFSET = 0x2C8
TYPE_UPGRADE_LIST_OFFSET = 0x2CC

PROCESS_NAMES = ("ranker.exe", "ranker800.exe", "rank1024.exe")
DEFAULT_RESULT_SERVER_URL = "http://jw2-arena.com:80/replay"
PLAYER_COLORS = (
    "#58A6FF",
    "#F85149",
    "#3FB950",
    "#D29922",
    "#BC8CFF",
    "#39C5CF",
    "#FF8F40",
    "#DB61A2",
)
MAPEDITOR_ICON_SIZE = 38
MAPEDITOR_ICON_COUNT = ALL_UNIT_TYPE_COUNT
ITEM_ICON_SIZE = 38
UPGRADE_ICON_SIZE = 38
UNIT_ITEM_BADGE_ICONS = {
    10: 9,    # PowerMan (Shield)
    11: 5,    # Soldier (Fire Arrow)
    12: 6,    # Soldier (Poison Arrow)
    13: 7,    # Knight (Spear)
    91: 93,   # Silvan (Fade Robe) - robe-shaped icon
    93: 11,   # Sky Ballista (Sky Bullet), user-selected item_badge_candidates/11.png
    94: 95,   # Giant (Fire Wallet) - flame icon
    95: 22,   # Bow Machine (Double Bow), user-selected item_badge_candidates/22.png
}
PROCESS_CREATE_THREAD = 0x0002
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
MEM_RELEASE = 0x8000
PAGE_EXECUTE_READWRITE = 0x40
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
WAIT_OBJECT_0 = 0x00000000

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("th32ModuleID", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("GlblcntUsage", wintypes.DWORD),
        ("ProccntUsage", wintypes.DWORD),
        ("modBaseAddr", ctypes.POINTER(wintypes.BYTE)),
        ("modBaseSize", wintypes.DWORD),
        ("hModule", wintypes.HMODULE),
        ("szModule", wintypes.WCHAR * 256),
        ("szExePath", wintypes.WCHAR * 260),
    ]


kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.ReadProcessMemory.argtypes = [
    wintypes.HANDLE,
    wintypes.LPCVOID,
    wintypes.LPVOID,
    ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t),
]
kernel32.ReadProcessMemory.restype = wintypes.BOOL
kernel32.WriteProcessMemory.argtypes = [
    wintypes.HANDLE,
    wintypes.LPVOID,
    wintypes.LPCVOID,
    ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t),
]
kernel32.WriteProcessMemory.restype = wintypes.BOOL
kernel32.VirtualAllocEx.argtypes = [
    wintypes.HANDLE,
    wintypes.LPVOID,
    ctypes.c_size_t,
    wintypes.DWORD,
    wintypes.DWORD,
]
kernel32.VirtualAllocEx.restype = wintypes.LPVOID
kernel32.VirtualFreeEx.argtypes = [
    wintypes.HANDLE,
    wintypes.LPVOID,
    ctypes.c_size_t,
    wintypes.DWORD,
]
kernel32.VirtualFreeEx.restype = wintypes.BOOL
kernel32.CreateRemoteThread.argtypes = [
    wintypes.HANDLE,
    wintypes.LPVOID,
    ctypes.c_size_t,
    wintypes.LPVOID,
    wintypes.LPVOID,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]
kernel32.CreateRemoteThread.restype = wintypes.HANDLE
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.GetExitCodeThread.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel32.GetExitCodeThread.restype = wintypes.BOOL
kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.Module32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
kernel32.Module32FirstW.restype = wintypes.BOOL
kernel32.Module32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
kernel32.Module32NextW.restype = wintypes.BOOL


class MemoryReadError(RuntimeError):
    pass


class AutoSaveError(RuntimeError):
    pass


@dataclass
class ProcessInfo:
    pid: int
    name: str
    exe: str | None


@dataclass(frozen=True)
class ReplayTmpState:
    path: Path
    size: int
    mtime_ns: int

    @property
    def key(self) -> tuple[int, int]:
        return self.size, self.mtime_ns


@dataclass
class PlayerSnapshot:
    slot: int
    name: str
    status_byte: int | None
    status_dword: int | None
    unit_counts: list[int]
    total_96: int
    total_170: int
    object_count: int | None


@dataclass
class ProductionSnapshot:
    slot: int
    object_offset: int
    building_type: int
    unit_type: int
    progress: int
    total: int


@dataclass
class UpgradeSnapshot:
    slot: int
    object_offset: int
    building_type: int
    upgrade_id: int
    progress: int
    total: int


@dataclass
class BuildingSnapshot:
    slot: int
    object_offset: int
    building_type: int
    progress: int
    total: int


@dataclass
class Snapshot:
    process: ProcessInfo | None
    local_player: int | None
    players: list[PlayerSnapshot]
    note: str = ""
    game_active: bool = True
    game_tick: int | None = None
    productions: list[ProductionSnapshot] = field(default_factory=list)
    upgrades: list[UpgradeSnapshot] = field(default_factory=list)
    buildings: list[BuildingSnapshot] = field(default_factory=list)
    player_names: list[str] = field(default_factory=list)
    is_observer: bool = False
    is_player: bool = False


class ProcessReader:
    def __init__(self, pid: int, module_base: int):
        self.module_base = module_base
        self.handle = kernel32.OpenProcess(
            PROCESS_QUERY_INFORMATION | PROCESS_VM_READ,
            False,
            pid,
        )
        if not self.handle:
            raise MemoryReadError(f"OpenProcess failed: {ctypes.get_last_error()}")

    def close(self) -> None:
        if self.handle:
            kernel32.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self) -> "ProcessReader":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def addr(self, original_va: int) -> int:
        return self.module_base + (original_va - ORIGINAL_IMAGE_BASE)

    def read(self, original_va: int, size: int) -> bytes:
        address = self.addr(original_va)
        buf = ctypes.create_string_buffer(size)
        got = ctypes.c_size_t()
        ok = kernel32.ReadProcessMemory(
            self.handle,
            ctypes.c_void_p(address),
            buf,
            size,
            ctypes.byref(got),
        )
        if not ok or got.value != size:
            raise MemoryReadError(
                f"ReadProcessMemory failed at 0x{address:08X}: "
                f"{ctypes.get_last_error()} ({got.value}/{size})"
            )
        return buf.raw

    def read_u8(self, original_va: int) -> int:
        return self.read(original_va, 1)[0]

    def read_u32(self, original_va: int) -> int:
        return struct.unpack("<I", self.read(original_va, 4))[0]


class ProcessHandle:
    def __init__(self, pid: int, access: int):
        self.handle = kernel32.OpenProcess(access, False, pid)
        if not self.handle:
            raise AutoSaveError(f"OpenProcess failed: {ctypes.get_last_error()}")

    def close(self) -> None:
        if self.handle:
            kernel32.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self) -> "ProcessHandle":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def normalize_path(path: str | None) -> Path | None:
    if not path:
        return None
    try:
        return Path(path).resolve()
    except OSError:
        return None


def find_process(
    preferred_dir: Path | None = None,
    require_preferred: bool = False,
) -> ProcessInfo | None:
    preferred_dir = preferred_dir.resolve() if preferred_dir else None
    fallback: ProcessInfo | None = None

    for proc in psutil.process_iter(["pid", "name", "exe"]):
        try:
            name = proc.info.get("name") or ""
            if name.lower() not in PROCESS_NAMES:
                continue
            exe = proc.info.get("exe")
            info = ProcessInfo(proc.info["pid"], name, exe)
            if fallback is None:
                fallback = info
            if preferred_dir:
                exe_path = normalize_path(exe)
                if exe_path and exe_path.parent == preferred_dir:
                    return info
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if require_preferred and preferred_dir:
        return None
    return fallback


def module_base(pid: int, module_names: Iterable[str] = PROCESS_NAMES) -> int:
    names = {name.lower() for name in module_names}
    snapshot = kernel32.CreateToolhelp32Snapshot(
        TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32,
        pid,
    )
    if snapshot == INVALID_HANDLE_VALUE:
        return ORIGINAL_IMAGE_BASE

    try:
        entry = MODULEENTRY32W()
        entry.dwSize = ctypes.sizeof(MODULEENTRY32W)
        ok = kernel32.Module32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            if entry.szModule.lower() in names:
                return ctypes.cast(entry.modBaseAddr, ctypes.c_void_p).value
            ok = kernel32.Module32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)

    return ORIGINAL_IMAGE_BASE


def actual_va(module_base_address: int, original_va: int) -> int:
    return module_base_address + (original_va - ORIGINAL_IMAGE_BASE)


def replay_tmp_state(game_dir: Path) -> ReplayTmpState | None:
    path = game_dir / REPLAY_TMP
    try:
        st = path.stat()
    except OSError:
        return None
    if st.st_size < len(REPLAY_MAGIC):
        return None
    return ReplayTmpState(path=path, size=st.st_size, mtime_ns=st.st_mtime_ns)


def can_read_replay_header(path: Path) -> bool:
    try:
        with path.open("rb") as fp:
            return fp.read(len(REPLAY_MAGIC)) == REPLAY_MAGIC
    except OSError:
        # The game can keep Replay.tmp open without read sharing. The internal
        # saver still owns the handle and can finalize it.
        return True


def looks_like_saved_replay(path: Path) -> bool:
    try:
        with path.open("rb") as fp:
            return fp.read(len(PLY_MAGIC)) == PLY_MAGIC
    except OSError:
        return False


def encode_ansi_path(path: Path) -> bytes:
    text = str(path)
    try:
        return text.encode("mbcs") + b"\x00"
    except LookupError:
        return os.fsencode(text) + b"\x00"


def write_remote(handle: int, address: int, payload: bytes) -> None:
    written = ctypes.c_size_t()
    ok = kernel32.WriteProcessMemory(
        handle,
        ctypes.c_void_p(address),
        payload,
        len(payload),
        ctypes.byref(written),
    )
    if not ok or written.value != len(payload):
        raise AutoSaveError(
            f"WriteProcessMemory failed: {ctypes.get_last_error()} "
            f"({written.value}/{len(payload)})"
        )


def call_remote_save(pid: int, module_base_address: int, output_path: Path) -> int:
    encoded_path = encode_ansi_path(output_path)
    save_func = actual_va(module_base_address, SAVE_REPLAY_VA)

    access = (
        PROCESS_CREATE_THREAD
        | PROCESS_QUERY_INFORMATION
        | PROCESS_VM_OPERATION
        | PROCESS_VM_WRITE
        | PROCESS_VM_READ
    )
    with ProcessHandle(pid, access) as proc:
        total_size = len(encoded_path) + 32
        remote = kernel32.VirtualAllocEx(
            proc.handle,
            None,
            total_size,
            MEM_COMMIT | MEM_RESERVE,
            PAGE_EXECUTE_READWRITE,
        )
        if not remote:
            raise AutoSaveError(f"VirtualAllocEx failed: {ctypes.get_last_error()}")

        remote_addr = ctypes.cast(remote, ctypes.c_void_p).value
        path_addr = remote_addr
        code_addr = remote_addr + len(encoded_path)

        if path_addr > 0xFFFFFFFF or code_addr > 0xFFFFFFFF or save_func > 0xFFFFFFFF:
            kernel32.VirtualFreeEx(proc.handle, remote, 0, MEM_RELEASE)
            raise AutoSaveError("target address is outside 32-bit range")

        code = (
            b"\x68"
            + path_addr.to_bytes(4, "little")
            + b"\xB8"
            + save_func.to_bytes(4, "little")
            + b"\xFF\xD0"
            + b"\x83\xC4\x04"
            + b"\xC2\x04\x00"
        )

        try:
            write_remote(proc.handle, path_addr, encoded_path)
            write_remote(proc.handle, code_addr, code)

            thread_id = wintypes.DWORD()
            thread = kernel32.CreateRemoteThread(
                proc.handle,
                None,
                0,
                ctypes.c_void_p(code_addr),
                None,
                0,
                ctypes.byref(thread_id),
            )
            if not thread:
                raise AutoSaveError(
                    f"CreateRemoteThread failed: {ctypes.get_last_error()}"
                )

            try:
                wait = kernel32.WaitForSingleObject(thread, 15000)
                if wait != WAIT_OBJECT_0:
                    raise AutoSaveError(f"remote save timed out: wait=0x{wait:X}")

                exit_code = wintypes.DWORD()
                if not kernel32.GetExitCodeThread(thread, ctypes.byref(exit_code)):
                    raise AutoSaveError(
                        f"GetExitCodeThread failed: {ctypes.get_last_error()}"
                    )
                return int(exit_code.value)
            finally:
                kernel32.CloseHandle(thread)
        finally:
            kernel32.VirtualFreeEx(proc.handle, remote, 0, MEM_RELEASE)


def read_replay_participant_slots(game_dir: Path) -> list[int]:
    path = game_dir / REPLAY_TMP
    try:
        with path.open("rb") as fp:
            data = fp.read(REPLAY_SLOT_TYPE_TABLE_OFFSET + MAX_PLAYERS)
    except OSError:
        return []

    required = REPLAY_SLOT_TYPE_TABLE_OFFSET + MAX_PLAYERS
    if len(data) < required or not data.startswith(REPLAY_MAGIC):
        return []

    slot_types = data[REPLAY_SLOT_TYPE_TABLE_OFFSET:required]
    return [
        slot
        for slot, type_code in enumerate(slot_types)
        if type_code in (SLOT_HUMAN, SLOT_COMPUTER)
    ]


def safe_replay_name_piece(name: str, fallback: str) -> str:
    invalid = set('<>:"/\\|?*')
    cleaned = "".join(
        "_" if ch in invalid or ord(ch) < 32 else ch
        for ch in name.strip()
    ).strip(" ._")
    return cleaned or fallback


def snapshot_participant_slots(snapshot: Snapshot) -> list[int]:
    slots: list[int] = []
    for player in snapshot.players:
        has_units = (
            player.total_96 > 0
            or player.total_170 > 0
            or (player.object_count or 0) > 0
        )
        participant_status = any(
            value in (SLOT_HUMAN, SLOT_COMPUTER)
            for value in (player.status_byte, player.status_dword)
        )
        if has_units or participant_status:
            slots.append(player.slot)
    return slots


def replay_player_names(snapshot: Snapshot, game_dir: Path) -> tuple[str, str]:
    slots = read_replay_participant_slots(game_dir)
    if len(slots) < 2:
        slots = snapshot_participant_slots(snapshot)
    if len(slots) < 2:
        slots = [0, 1]

    left_slot, right_slot = slots[:2]
    left = safe_replay_name_piece(
        player_display_name(left_slot, snapshot.player_names),
        f"P{left_slot + 1}",
    )
    right = safe_replay_name_piece(
        player_display_name(right_slot, snapshot.player_names),
        f"P{right_slot + 1}",
    )
    return left, right


def next_match_replay_path(replay_dir: Path, snapshot: Snapshot, game_dir: Path) -> Path:
    replay_dir.mkdir(parents=True, exist_ok=True)
    left, right = replay_player_names(snapshot, game_dir)
    stamp = datetime.now().strftime("%Y_%m_%d_%H_%M_%S")
    base = replay_dir / f"{left}vs{right}_{stamp}.ply"
    if not base.exists():
        return base

    for index in range(2, 1000):
        candidate = replay_dir / f"{left}vs{right}_{stamp}_{index:03d}.ply"
        if not candidate.exists():
            return candidate

    raise AutoSaveError("could not allocate an output replay name")


def upload_replay_to_result_server(
    replay_path: Path,
    server_url: str,
    timeout: float,
) -> dict[str, object]:
    data = replay_path.read_bytes()
    request = urllib.request.Request(
        server_url,
        data=data,
        headers={
            "Content-Type": "application/octet-stream",
            "X-Filename": urllib.parse.quote(replay_path.name),
        },
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()

    if not body:
        return {}
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"raw_response": body.decode("utf-8", errors="replace")}
    if isinstance(payload, dict):
        return payload
    return {"response": payload}


def upload_replay_to_result_server_with_retries(
    replay_path: Path,
    server_url: str,
    timeout: float,
    attempts: int,
    retry_seconds: float,
) -> None:
    attempts = max(1, attempts)
    for attempt in range(1, attempts + 1):
        try:
            payload = upload_replay_to_result_server(
                replay_path,
                server_url,
                timeout,
            )
            duplicate = payload.get("duplicate")
            counted = payload.get("counted")
            record = payload.get("record")
            status = record.get("status") if isinstance(record, dict) else None
            print(
                "[result-upload] uploaded "
                f"{replay_path.name} "
                f"(duplicate={duplicate}, counted={counted}, status={status})"
            )
            return
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            print(
                "[result-upload] server rejected "
                f"{replay_path.name}: HTTP {exc.code} {detail}",
                file=sys.stderr,
            )
            return
        except Exception as exc:
            if attempt >= attempts:
                print(
                    "[result-upload] failed "
                    f"{replay_path.name}: {exc}",
                    file=sys.stderr,
                )
                return
            print(
                "[result-upload] retrying "
                f"{replay_path.name} after error: {exc}",
                file=sys.stderr,
            )
            time.sleep(max(0.0, retry_seconds))


def sane_count(value: int) -> bool:
    return 0 <= value <= 5000


def read_unit_counts(reader: ProcessReader) -> tuple[list[list[int]], str]:
    raw = reader.read(UNIT_COUNT_TABLE_VA, MAX_PLAYERS * PLAYER_STRIDE)
    rows: list[list[int]] = []
    note = ""

    for player in range(MAX_PLAYERS):
        start = player * PLAYER_STRIDE
        row = list(struct.unpack_from("<" + "I" * ALL_UNIT_TYPE_COUNT, raw, start))
        if any(not sane_count(value) for value in row):
            note = "unit table contains unexpected values"
        rows.append(row)

    return rows, note


def read_statuses(reader: ProcessReader) -> tuple[list[int | None], list[int | None]]:
    status_bytes: list[int | None] = []
    status_dwords: list[int | None] = []

    for slot in range(MAX_PLAYERS):
        try:
            status_bytes.append(reader.read_u8(PLAYER_STATUS_BYTES_VA + slot))
        except MemoryReadError:
            status_bytes.append(None)
        try:
            status_dwords.append(reader.read_u32(PLAYER_STATUS_DWORDS_VA + slot * 4))
        except MemoryReadError:
            status_dwords.append(None)

    return status_bytes, status_dwords


def read_status_pair(reader: ProcessReader, slot: int | None) -> tuple[int | None, int | None]:
    if slot is None or not 0 <= slot < MAX_PLAYERS:
        return None, None

    try:
        status_byte = reader.read_u8(PLAYER_STATUS_BYTES_VA + slot)
    except MemoryReadError:
        status_byte = None

    try:
        status_dword = reader.read_u32(PLAYER_STATUS_DWORDS_VA + slot * 4)
    except MemoryReadError:
        status_dword = None

    return status_byte, status_dword


def decode_player_name(raw: bytes, slot: int) -> str:
    name_bytes = raw.split(b"\0", 1)[0].strip()
    if name_bytes:
        for encoding in ("cp949", "utf-8", "latin1"):
            try:
                name = name_bytes.decode(encoding).strip()
            except UnicodeDecodeError:
                continue
            if name:
                return name
    return f"P{slot + 1}"


def read_player_names(reader: ProcessReader) -> list[str]:
    names = []
    for slot in range(MAX_PLAYERS):
        try:
            raw = reader.read(
                PLAYER_NAME_TABLE_VA + slot * PLAYER_NAME_STRIDE,
                PLAYER_NAME_SIZE,
            )
        except MemoryReadError:
            names.append(f"P{slot + 1}")
            continue
        names.append(decode_player_name(raw, slot))
    return names


def read_local_player(reader: ProcessReader) -> int | None:
    try:
        value = reader.read_u32(LOCAL_PLAYER_INDEX_VA)
    except MemoryReadError:
        return None
    if 0 <= value < MAX_PLAYERS:
        return value
    return None


def read_game_tick(reader: ProcessReader) -> int | None:
    try:
        return reader.read_u32(GAME_HEARTBEAT_TICK_VA)
    except MemoryReadError:
        return None


def read_object_counts(reader: ProcessReader, max_nodes: int = 20000) -> list[int] | None:
    try:
        next_offset = reader.read_u32(OBJECT_LIST_HEAD_OFFSET_VA)
    except MemoryReadError:
        return None

    counts = [0] * MAX_PLAYERS
    seen: set[int] = set()

    for _ in range(max_nodes):
        if next_offset == 0:
            return counts
        if next_offset in seen:
            return counts
        seen.add(next_offset)

        object_va = OBJECT_POOL_BASE_VA + next_offset
        try:
            unit_type = reader.read_u32(object_va)
            owner = reader.read_u32(object_va + OBJECT_OWNER_OFFSET)
            next_offset = reader.read_u32(object_va + OBJECT_NEXT_OFFSET)
        except MemoryReadError:
            return None

        if 0 <= owner < MAX_PLAYERS and 0 <= unit_type < ALL_UNIT_TYPE_COUNT:
            counts[owner] += 1

    return counts


def read_unit_production_ticks(reader: ProcessReader, unit_type: int) -> int | None:
    if not 0 <= unit_type < ALL_UNIT_TYPE_COUNT:
        return None
    try:
        type_index = reader.read_u32(TYPE_INDEX_TABLE_VA + unit_type * 4)
        ticks = reader.read_u32(TYPE_INFO_BASE_VA + type_index + TYPE_PRODUCTION_TICKS_OFFSET)
    except MemoryReadError:
        return None
    if 0 < ticks <= 100000:
        return ticks
    return None


def read_productions(reader: ProcessReader, max_nodes: int = 20000) -> list[ProductionSnapshot]:
    try:
        next_offset = reader.read_u32(OBJECT_LIST_HEAD_OFFSET_VA)
    except MemoryReadError:
        return []

    productions: list[ProductionSnapshot] = []
    seen: set[int] = set()

    for _ in range(max_nodes):
        if next_offset == 0:
            break
        if next_offset in seen:
            break
        seen.add(next_offset)

        object_offset = next_offset
        object_va = OBJECT_POOL_BASE_VA + object_offset
        try:
            unit_type = reader.read_u32(object_va)
            owner = reader.read_u32(object_va + OBJECT_OWNER_OFFSET)
            state = reader.read_u32(object_va + OBJECT_PRODUCTION_STATE_OFFSET)
            progress = reader.read_u32(object_va + OBJECT_PRODUCTION_TIMER_OFFSET)
            target_unit = reader.read_u32(object_va + OBJECT_PRODUCTION_TARGET_OFFSET)
            next_offset = reader.read_u32(object_va + OBJECT_NEXT_OFFSET)
        except MemoryReadError:
            break

        if (
            not 0 <= owner < MAX_PLAYERS
            or not AI_UNIT_TYPE_COUNT <= unit_type < ALL_UNIT_TYPE_COUNT
            or state != OBJECT_PRODUCTION_ACTIVE_STATE
            or not 0 <= target_unit < AI_UNIT_TYPE_COUNT
        ):
            continue

        total = read_unit_production_ticks(reader, target_unit)
        if total is None:
            continue

        productions.append(
            ProductionSnapshot(
                slot=owner,
                object_offset=object_offset,
                building_type=unit_type,
                unit_type=target_unit,
                progress=min(progress, total),
                total=total,
            )
        )

    return productions


def read_building_upgrade_ids(reader: ProcessReader, building_type: int) -> list[int]:
    if not AI_UNIT_TYPE_COUNT <= building_type < ALL_UNIT_TYPE_COUNT:
        return []
    try:
        type_index = reader.read_u32(TYPE_INDEX_TABLE_VA + building_type * 4)
        type_base = TYPE_INFO_BASE_VA + type_index
        count = reader.read_u32(type_base + TYPE_UPGRADE_COUNT_OFFSET)
    except MemoryReadError:
        return []
    if not 0 < count <= 32:
        return []

    upgrade_ids = []
    for index in range(count):
        try:
            upgrade_id = reader.read_u32(type_base + TYPE_UPGRADE_LIST_OFFSET + index * 4)
        except MemoryReadError:
            break
        if 0 <= upgrade_id < ALL_UNIT_TYPE_COUNT:
            upgrade_ids.append(upgrade_id)
    return upgrade_ids


def read_upgrades(reader: ProcessReader, max_nodes: int = 20000) -> list[UpgradeSnapshot]:
    try:
        next_offset = reader.read_u32(OBJECT_LIST_HEAD_OFFSET_VA)
    except MemoryReadError:
        return []

    upgrades: list[UpgradeSnapshot] = []
    upgrade_ids_by_building: dict[int, set[int]] = {}
    seen: set[int] = set()

    for _ in range(max_nodes):
        if next_offset == 0:
            break
        if next_offset in seen:
            break
        seen.add(next_offset)

        object_offset = next_offset
        object_va = OBJECT_POOL_BASE_VA + object_offset
        try:
            building_type = reader.read_u32(object_va)
            owner = reader.read_u32(object_va + OBJECT_OWNER_OFFSET)
            state = reader.read_u32(object_va + OBJECT_PRODUCTION_STATE_OFFSET)
            progress = reader.read_u32(object_va + OBJECT_UPGRADE_PROGRESS_OFFSET)
            upgrade_id = reader.read_u32(object_va + OBJECT_UPGRADE_TARGET_OFFSET)
            next_offset = reader.read_u32(object_va + OBJECT_NEXT_OFFSET)
        except MemoryReadError:
            break

        if (
            not 0 <= owner < MAX_PLAYERS
            or not AI_UNIT_TYPE_COUNT <= building_type < ALL_UNIT_TYPE_COUNT
            or state != OBJECT_UPGRADE_ACTIVE_STATE
        ):
            continue

        upgrade_ids = upgrade_ids_by_building.get(building_type)
        if upgrade_ids is None:
            upgrade_ids = set(read_building_upgrade_ids(reader, building_type))
            upgrade_ids_by_building[building_type] = upgrade_ids
        if upgrade_id not in upgrade_ids:
            continue

        upgrades.append(
            UpgradeSnapshot(
                slot=owner,
                object_offset=object_offset,
                building_type=building_type,
                upgrade_id=upgrade_id,
                progress=min(progress, OBJECT_UPGRADE_TOTAL_TICKS),
                total=OBJECT_UPGRADE_TOTAL_TICKS,
            )
        )

    return upgrades


def read_buildings(reader: ProcessReader, max_nodes: int = 20000) -> list[BuildingSnapshot]:
    try:
        next_offset = reader.read_u32(OBJECT_LIST_HEAD_OFFSET_VA)
    except MemoryReadError:
        return []

    buildings: list[BuildingSnapshot] = []
    seen: set[int] = set()

    for _ in range(max_nodes):
        if next_offset == 0:
            break
        if next_offset in seen:
            break
        seen.add(next_offset)

        object_offset = next_offset
        object_va = OBJECT_POOL_BASE_VA + object_offset
        try:
            building_type = reader.read_u32(object_va)
            owner = reader.read_u32(object_va + OBJECT_OWNER_OFFSET)
            state = reader.read_u32(object_va + OBJECT_PRODUCTION_STATE_OFFSET)
            progress = reader.read_u32(object_va + OBJECT_BUILD_PROGRESS_OFFSET)
            build_flag = reader.read_u32(object_va + OBJECT_BUILD_FLAG_OFFSET)
            max_health = reader.read_u32(object_va + OBJECT_MAX_HEALTH_OFFSET)
            current_health = reader.read_u32(object_va + OBJECT_CURRENT_HEALTH_OFFSET)
            next_offset = reader.read_u32(object_va + OBJECT_NEXT_OFFSET)
        except MemoryReadError:
            break

        if (
            not 0 <= owner < MAX_PLAYERS
            or not AI_UNIT_TYPE_COUNT <= building_type < ALL_UNIT_TYPE_COUNT
            or state != OBJECT_BUILD_ACTIVE_STATE
            or build_flag != 1
            or max_health <= 0
            or current_health >= max_health
        ):
            continue

        total = read_unit_production_ticks(reader, building_type)
        if total is None or progress >= total:
            continue

        buildings.append(
            BuildingSnapshot(
                slot=owner,
                object_offset=object_offset,
                building_type=building_type,
                progress=max(0, min(progress, total)),
                total=total,
            )
        )

    return buildings


def active_player_status(value: int | None) -> bool:
    return value not in (None, 0, 2, 0x14, 0xFFFFFFFF)


def observer_status(value: int | None) -> bool:
    return value == SLOT_SPECTATOR


def player_status(value: int | None) -> bool:
    return value in (SLOT_HUMAN, SLOT_COMPUTER)


def auto_visible_slots(
    unit_rows: list[list[int]],
    status_bytes: list[int | None],
    status_dwords: list[int | None],
    object_counts: list[int] | None,
) -> list[int]:
    slots = []
    for slot, row in enumerate(unit_rows):
        total_96 = sum(row[:AI_UNIT_TYPE_COUNT])
        total_170 = sum(row)
        object_count = object_counts[slot] if object_counts else 0
        status_byte = status_bytes[slot]
        status_dword = status_dwords[slot]
        if (
            total_96
            or total_170
            or object_count
            or active_player_status(status_byte)
            or active_player_status(status_dword)
        ):
            slots.append(slot)

    if not slots:
        return [0, 1]
    return slots[:MAX_PLAYERS]


def make_snapshot(
    players_arg: str | None = None,
    include_objects: bool = False,
    preferred_dir: Path | None = None,
    require_preferred: bool = False,
) -> Snapshot:
    process = find_process(
        preferred_dir or Path(__file__).resolve().parent,
        require_preferred=require_preferred,
    )
    if not process:
        return Snapshot(None, None, [], "waiting for ranker.exe", False)

    base = module_base(process.pid)
    with ProcessReader(process.pid, base) as reader:
        local_player = read_local_player(reader)
        game_tick = read_game_tick(reader)
        local_status_byte, local_status_dword = read_status_pair(reader, local_player)

        game_active = True
        is_observer = (
            observer_status(local_status_byte)
            or observer_status(local_status_dword)
        )
        is_player = (
            player_status(local_status_byte)
            or player_status(local_status_dword)
        )

        if not is_observer:
            player_names = read_player_names(reader) if is_player else []
            return Snapshot(
                process,
                local_player,
                [],
                "waiting for observer mode",
                game_active,
                game_tick,
                player_names=player_names,
                is_observer=False,
                is_player=is_player,
            )

        status_bytes, status_dwords = read_statuses(reader)
        player_names = read_player_names(reader)
        unit_rows, note = read_unit_counts(reader)
        object_counts = read_object_counts(reader) if include_objects else None
        productions = read_productions(reader)
        upgrades = read_upgrades(reader)
        buildings = read_buildings(reader)

    if players_arg:
        slots = []
        for part in players_arg.split(","):
            part = part.strip()
            if not part:
                continue
            slot = int(part)
            if not 0 <= slot < MAX_PLAYERS:
                raise ValueError("player slots must be 0-7")
            slots.append(slot)
    else:
        slots = auto_visible_slots(unit_rows, status_bytes, status_dwords, object_counts)

    players = []
    for slot in slots:
        row = unit_rows[slot]
        players.append(
            PlayerSnapshot(
                slot=slot,
                name=player_display_name(slot, player_names),
                status_byte=status_bytes[slot],
                status_dword=status_dwords[slot],
                unit_counts=row[:AI_UNIT_TYPE_COUNT],
                total_96=sum(row[:AI_UNIT_TYPE_COUNT]),
                total_170=sum(row),
                object_count=object_counts[slot] if include_objects and object_counts else None,
            )
        )

    return Snapshot(
        process,
        local_player,
        players,
        note,
        game_active,
        game_tick,
        productions,
        upgrades,
        buildings,
        player_names,
        is_observer,
        is_player,
    )


def format_snapshot(snapshot: Snapshot, verbose: bool = False) -> str:
    if not snapshot.process:
        return snapshot.note

    header = f"{snapshot.process.name} pid={snapshot.process.pid}"
    if snapshot.local_player is not None:
        header += f" local={player_display_name(snapshot.local_player, snapshot.player_names)}"
    lines = [header]

    for player in snapshot.players:
        line = (
            f"{player.name}: units96={player.total_96:3d} "
            f"all170={player.total_170:3d}"
        )
        if player.object_count is not None:
            line += f" objects={player.object_count:3d}"
        if verbose:
            line += (
                f" status_byte={fmt_optional_hex(player.status_byte)}"
                f" status_dword={fmt_optional_hex(player.status_dword)}"
            )
        lines.append(line)

    for production in snapshot.productions:
        percent = int((production.progress / production.total) * 100) if production.total else 0
        lines.append(
            f"{player_display_name(production.slot, snapshot.player_names)}: producing "
            f"U{production.unit_type:02d} "
            f"{production.progress}/{production.total} ({percent:3d}%)"
        )

    for upgrade in snapshot.upgrades:
        percent = int((upgrade.progress / upgrade.total) * 100) if upgrade.total else 0
        lines.append(
            f"{player_display_name(upgrade.slot, snapshot.player_names)}: upgrading "
            f"G{upgrade.upgrade_id:02d} "
            f"{upgrade.progress}/{upgrade.total} ({percent:3d}%)"
        )

    for building in snapshot.buildings:
        percent = int((building.progress / building.total) * 100) if building.total else 0
        lines.append(
            f"{player_display_name(building.slot, snapshot.player_names)}: building "
            f"B{building.building_type:02d} "
            f"{building.progress}/{building.total} ({percent:3d}%)"
        )

    if snapshot.note:
        lines.append(snapshot.note)
    return "\n".join(lines)


def fmt_optional_hex(value: int | None) -> str:
    if value is None:
        return "?"
    return f"0x{value:X}"


def player_display_name(slot: int, player_names: list[str] | None = None) -> str:
    if player_names and 0 <= slot < len(player_names) and player_names[slot]:
        return player_names[slot]
    return f"P{slot + 1}"


def unit_display_name(source_name: str, unit_index: int) -> str:
    stem = Path(source_name).stem if source_name else ""
    stem = stem.replace("_", " ").strip()
    return stem or f"Unit {unit_index}"


class UnitIconLibrary:
    """Loads MapEditor/JW2 unit art as small Tk images."""

    def __init__(self, size: int, icon_root: Path | None = None):
        if tk is None:
            raise RuntimeError("tkinter is not available")

        self.size = max(16, min(size, 96))
        self.script_dir = Path(__file__).resolve().parent
        self.overlay_data_dir = self.script_dir / "overlay_data"
        self.cache_dir = self.overlay_data_dir / "unit_icons" / f"mapeditor_badged_v4_{self.size}px"
        self.upgrade_icon_dir = self.overlay_data_dir / "upgrade_icon_candidates"
        self.explicit_root = icon_root.resolve() if icon_root else None
        self.mapeditor_raw_dir = self._find_mapeditor_raw_dir()
        self.decoded_root = self._find_decoded_root()
        self.source_dirs = self._load_source_dirs()
        self.names = self._load_names()
        self.prebuilt_dirs = self._prebuilt_dirs()

        self._prepare_cache()
        self.photos = {
            unit_type: self._load_photo(unit_type)
            for unit_type in range(AI_UNIT_TYPE_COUNT)
        }
        self.upgrade_photos = self._load_upgrade_photos()

    def _find_mapeditor_raw_dir(self) -> Path | None:
        candidates: list[Path] = []
        if self.explicit_root:
            candidates.extend(
                [
                    self.explicit_root,
                    self.explicit_root / "mapeditor_raw",
                    self.explicit_root / "raw",
                    self.explicit_root / "overlay_data" / "mapeditor_raw",
                    self.explicit_root / "jw2_02_out" / "raw",
                ]
            )

        candidates.extend(
            [
                self.overlay_data_dir / "mapeditor_raw",
                self.overlay_data_dir / "jw2_02_out" / "raw",
            ]
        )

        for candidate in candidates:
            if (candidate / "char_small").exists() and (candidate / "char_small__2").exists():
                return candidate
        return None

    def _find_decoded_root(self) -> Path | None:
        candidates: list[Path] = []
        if self.explicit_root:
            candidates.append(self.explicit_root)
            candidates.append(self.explicit_root / "decoded_chr")
            candidates.append(self.explicit_root / "overlay_data" / "decoded_chr")

        candidates.extend(
            [
                self.overlay_data_dir / "decoded_chr",
                self.overlay_data_dir / "jw2_09_out" / "decoded_chr",
            ]
        )

        for candidate in candidates:
            if candidate.is_dir() and (
                (candidate / "units.csv").exists()
                or any(candidate.glob("[0-9][0-9][0-9]_*"))
            ):
                return candidate
        return None

    def _load_source_dirs(self) -> dict[int, Path]:
        source_dirs: dict[int, Path] = {}
        if not self.decoded_root:
            return source_dirs

        for child in self.decoded_root.iterdir():
            if not child.is_dir():
                continue
            prefix = child.name[:3]
            if prefix.isdigit():
                source_dirs[int(prefix)] = child
        return source_dirs

    def _load_names(self) -> dict[int, str]:
        names: dict[int, str] = {}
        units_csv = self.decoded_root / "units.csv" if self.decoded_root else None
        if units_csv and units_csv.exists():
            with units_csv.open("r", encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    try:
                        unit_index = int(row.get("unit_index") or "")
                    except ValueError:
                        continue
                    names[unit_index] = unit_display_name(
                        row.get("source_name", ""),
                        unit_index,
                    )

        for unit_index, source_dir in self.source_dirs.items():
            names.setdefault(unit_index, unit_display_name(source_dir.name[4:], unit_index))
        return names

    def _prebuilt_dirs(self) -> list[Path]:
        candidates: list[Path] = []
        if self.explicit_root:
            candidates.extend(
                [
                    self.explicit_root,
                    self.explicit_root / "unit_icons" / f"mapeditor_badged_v4_{self.size}px",
                    self.explicit_root / f"mapeditor_badged_v4_{self.size}px",
                    self.explicit_root / f"mapeditor_badged_v3_{self.size}px",
                    self.explicit_root / f"mapeditor_badged_v2_{self.size}px",
                    self.explicit_root / f"mapeditor_badged_{self.size}px",
                    self.explicit_root / f"mapeditor_{self.size}px",
                    self.explicit_root / f"{self.size}px",
                    self.explicit_root / "unit_icons" / f"mapeditor_badged_v3_{self.size}px",
                    self.explicit_root / "unit_icons" / f"mapeditor_badged_v2_{self.size}px",
                    self.explicit_root / "unit_icons" / f"mapeditor_badged_{self.size}px",
                    self.explicit_root / "unit_icons" / f"mapeditor_{self.size}px",
                    self.explicit_root / "unit_icons" / f"{self.size}px",
                ]
            )
        candidates.append(self.cache_dir)
        return [candidate for candidate in candidates if candidate.is_dir()]

    def _source_image(self, unit_type: int) -> Path | None:
        source_dir = self.source_dirs.get(unit_type)
        if not source_dir:
            return None

        candidates = [
            source_dir / "frames" / "frame_000.png",
            source_dir / "preview.png",
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    def _cached_image(self, unit_type: int) -> Path | None:
        name = f"{unit_type:03d}.png"
        for directory in self.prebuilt_dirs:
            candidate = directory / name
            if candidate.exists():
                return candidate
        return None

    def _prepare_cache(self) -> None:
        if Image is None:
            return

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        if self.cache_dir not in self.prebuilt_dirs:
            self.prebuilt_dirs.append(self.cache_dir)

        if self._prepare_mapeditor_cache():
            return

        if not self.decoded_root:
            return

        for unit_type in range(AI_UNIT_TYPE_COUNT):
            target = self.cache_dir / f"{unit_type:03d}.png"
            if target.exists():
                continue

            source = self._source_image(unit_type)
            if not source:
                continue

            try:
                image = Image.open(source).convert("RGBA")
                alpha_bbox = image.getchannel("A").getbbox()
                if alpha_bbox:
                    image = image.crop(alpha_bbox)
                image.thumbnail((self.size, self.size), Image.Resampling.LANCZOS)

                icon = Image.new("RGBA", (self.size, self.size), (0, 0, 0, 0))
                x = (self.size - image.width) // 2
                y = (self.size - image.height) // 2
                icon.paste(image, (x, y), image)
                icon.save(target)
            except Exception:
                continue

    def _prepare_mapeditor_cache(self) -> bool:
        if Image is None or not self.mapeditor_raw_dir:
            return False

        palette_path = self.mapeditor_raw_dir / "char_small"
        data_path = self.mapeditor_raw_dir / "char_small__2"
        try:
            colors = self._read_mapeditor_palette(palette_path)
            data = data_path.read_bytes()
        except OSError:
            return False

        icon_bytes = MAPEDITOR_ICON_SIZE * MAPEDITOR_ICON_SIZE
        icon_count = min(MAPEDITOR_ICON_COUNT, len(data) // icon_bytes)
        if icon_count <= 0:
            return False

        item_icons = self._load_item_icons()
        for unit_type in range(icon_count):
            target = self.cache_dir / f"{unit_type:03d}.png"
            if target.exists():
                continue

            start = unit_type * icon_bytes
            chunk = data[start : start + icon_bytes]
            try:
                icon = self._make_mapeditor_icon(chunk, colors)
                badge_item = UNIT_ITEM_BADGE_ICONS.get(unit_type)
                if badge_item is not None and badge_item in item_icons:
                    icon = self._add_item_badge(icon, item_icons[badge_item])
                icon.save(target)
            except Exception:
                continue
        return True

    def _load_item_icons(self) -> dict[int, "Image.Image"]:
        if Image is None or not self.mapeditor_raw_dir:
            return {}

        palette_path = self.mapeditor_raw_dir / "item.pnt"
        data_path = self.mapeditor_raw_dir / "item.trt"
        try:
            colors = self._read_mapeditor_palette(palette_path)
            data = data_path.read_bytes()
        except OSError:
            return {}

        icon_bytes = ITEM_ICON_SIZE * ITEM_ICON_SIZE
        icon_count = len(data) // icon_bytes
        icons: dict[int, "Image.Image"] = {}
        for item_id in set(UNIT_ITEM_BADGE_ICONS.values()):
            if not 0 <= item_id < icon_count:
                continue
            override = self._load_item_icon_override(item_id)
            if override is not None:
                icons[item_id] = override
                continue

            start = item_id * icon_bytes
            chunk = data[start : start + icon_bytes]
            icons[item_id] = self._make_indexed_icon(
                chunk,
                colors,
                ITEM_ICON_SIZE,
                ITEM_ICON_SIZE,
            )
        return icons

    def _load_item_icon_override(self, item_id: int) -> "Image.Image | None":
        if Image is None:
            return None

        override_dir = self.overlay_data_dir / "item_badge_candidates"
        candidates = [
            override_dir / f"{item_id}.png",
            override_dir / f"{item_id:03d}.png",
        ]
        for candidate in candidates:
            if not candidate.exists():
                continue
            try:
                icon = Image.open(candidate).convert("RGBA")
            except OSError:
                continue
            if icon.size != (ITEM_ICON_SIZE, ITEM_ICON_SIZE):
                icon = icon.resize((ITEM_ICON_SIZE, ITEM_ICON_SIZE), Image.Resampling.NEAREST)
            return icon
        return None

    def _load_upgrade_photos(self) -> dict[int, tk.PhotoImage]:
        photos = self._load_prebuilt_upgrade_photos()
        if Image is None or ImageTk is None or not self.mapeditor_raw_dir:
            return photos

        palette_path = self.mapeditor_raw_dir / "upgrade.pnt"
        data_path = self.mapeditor_raw_dir / "upgrade.trt"
        try:
            colors = self._read_mapeditor_palette(palette_path)
            data = data_path.read_bytes()
        except OSError:
            return photos

        icon_bytes = UPGRADE_ICON_SIZE * UPGRADE_ICON_SIZE
        icon_count = len(data) // icon_bytes
        for upgrade_id in range(icon_count):
            if upgrade_id in photos:
                continue

            start = upgrade_id * icon_bytes
            chunk = data[start : start + icon_bytes]
            try:
                icon = self._make_indexed_icon(
                    chunk,
                    colors,
                    UPGRADE_ICON_SIZE,
                    self.size,
                )
                photos[upgrade_id] = ImageTk.PhotoImage(icon)
            except Exception:
                continue
        return photos

    def _load_prebuilt_upgrade_photos(self) -> dict[int, tk.PhotoImage]:
        photos: dict[int, tk.PhotoImage] = {}
        if not self.upgrade_icon_dir.is_dir():
            return photos

        for candidate in sorted(self.upgrade_icon_dir.glob("*.png")):
            try:
                upgrade_id = int(candidate.stem)
            except ValueError:
                continue

            try:
                if self.size == UPGRADE_ICON_SIZE:
                    photos[upgrade_id] = tk.PhotoImage(file=str(candidate))
                elif Image is not None and ImageTk is not None:
                    image = Image.open(candidate).convert("RGBA")
                    image = image.resize((self.size, self.size), Image.Resampling.NEAREST)
                    photos[upgrade_id] = ImageTk.PhotoImage(image)
            except Exception:
                continue
        return photos

    def _read_mapeditor_palette(self, path: Path) -> list[tuple[int, int, int, int]]:
        blob = path.read_bytes()
        if len(blob) != 1024:
            raise ValueError(f"{path.name} is {len(blob)} bytes, expected 1024")
        colors = []
        for index in range(256):
            r, g, b, _unused = blob[index * 4 : index * 4 + 4]
            colors.append((r, g, b, 255))
        return colors

    def _make_mapeditor_icon(
        self,
        indices: bytes,
        colors: list[tuple[int, int, int, int]],
    ) -> "Image.Image":
        return self._make_indexed_icon(
            indices,
            colors,
            MAPEDITOR_ICON_SIZE,
            self.size,
        )

    def _make_indexed_icon(
        self,
        indices: bytes,
        colors: list[tuple[int, int, int, int]],
        source_size: int,
        target_size: int,
    ) -> "Image.Image":
        assert Image is not None
        rgba = bytearray()
        for value in indices:
            rgba.extend(colors[value])
        icon = Image.frombytes(
            "RGBA",
            (source_size, source_size),
            bytes(rgba),
        )
        if target_size != source_size:
            icon = icon.resize((target_size, target_size), Image.Resampling.NEAREST)
        return icon

    def _add_item_badge(self, icon: "Image.Image", item_icon: "Image.Image") -> "Image.Image":
        assert Image is not None
        composed = icon.copy().convert("RGBA")
        badge_size = max(10, min(round(self.size * 0.36), self.size - 2))
        badge = item_icon.resize((badge_size, badge_size), Image.Resampling.NEAREST)
        x = 0
        y = self.size - badge_size

        if ImageDraw is not None:
            draw = ImageDraw.Draw(composed)
            draw.rectangle(
                (x, y, x + badge_size - 1, y + badge_size - 1),
                fill=(0, 0, 0, 210),
            )

        composed.paste(badge, (x, y))
        if ImageDraw is not None:
            draw = ImageDraw.Draw(composed)
            draw.rectangle(
                (x, y, x + badge_size - 1, y + badge_size - 1),
                outline=(255, 226, 92, 255),
            )
        return composed

    def _load_photo(self, unit_type: int) -> tk.PhotoImage:
        cached = self._cached_image(unit_type)
        if cached:
            try:
                return tk.PhotoImage(file=str(cached))
            except tk.TclError:
                pass

        if Image is not None and ImageTk is not None:
            source = self._source_image(unit_type)
            if source:
                try:
                    return ImageTk.PhotoImage(self._make_icon_image(source))
                except Exception:
                    pass

            try:
                return ImageTk.PhotoImage(self._placeholder_image(unit_type))
            except Exception:
                pass

        photo = tk.PhotoImage(width=self.size, height=self.size)
        photo.put("#252A32", to=(0, 0, self.size, self.size))
        return photo

    def _make_icon_image(self, source: Path) -> "Image.Image":
        assert Image is not None
        image = Image.open(source).convert("RGBA")
        alpha_bbox = image.getchannel("A").getbbox()
        if alpha_bbox:
            image = image.crop(alpha_bbox)
        image.thumbnail((self.size, self.size), Image.Resampling.LANCZOS)

        icon = Image.new("RGBA", (self.size, self.size), (0, 0, 0, 0))
        x = (self.size - image.width) // 2
        y = (self.size - image.height) // 2
        icon.paste(image, (x, y), image)
        return icon

    def _placeholder_image(self, unit_type: int) -> "Image.Image":
        assert Image is not None
        image = Image.new("RGBA", (self.size, self.size), (37, 42, 50, 255))
        if ImageDraw is not None:
            draw = ImageDraw.Draw(image)
            draw.rectangle(
                (0, 0, self.size - 1, self.size - 1),
                outline=(80, 89, 105, 255),
            )
            text = str(unit_type)
            draw.text((3, self.size // 2 - 5), text, fill=(220, 226, 236, 255))
        return image

    def photo(self, unit_type: int) -> tk.PhotoImage:
        photo = self.photos.get(unit_type)
        if photo is None:
            photo = self._load_photo(unit_type)
            self.photos[unit_type] = photo
        return photo

    def upgrade_photo(self, upgrade_id: int) -> tk.PhotoImage:
        return self.upgrade_photos.get(upgrade_id) or self.photo(upgrade_id)

    def name(self, unit_type: int) -> str:
        return self.names.get(unit_type, f"Unit {unit_type}")


def format_unit_count(value: int) -> str:
    if value >= 10000:
        return f"{value // 1000}k"
    if value >= 1000:
        return f"{value / 1000:.1f}k"
    return str(value)


class AutoReplaySaver:
    def __init__(self, args: argparse.Namespace, game_dir: Path):
        self.args = args
        self.default_game_dir = game_dir.resolve()
        self.game_dir = self.default_game_dir
        self.replay_dir = self.game_dir / REPLAY_DIR
        self.last_pid: int | None = None
        self.last_game_dir: Path | None = None
        self.last_tick: int | None = None
        self.last_tick_change = time.monotonic()
        self.stable_since: float | None = None
        self.last_state_key: tuple[int, int] | None = None
        self.armed = bool(args.save_existing_replay)
        self.saved_this_session = False
        self.attempt_count = 0
        self.last_attempt_time = 0.0
        self.pending_output_path: Path | None = None
        self.post_save_ignore_until = 0.0
        self.last_saved_state_key: tuple[int, int] | None = None

    def queue_result_upload(self, replay_path: Path) -> None:
        if self.args.no_result_upload:
            return

        server_url = self.args.result_server_url.strip()
        if not server_url:
            return

        thread = threading.Thread(
            target=upload_replay_to_result_server_with_retries,
            args=(
                replay_path,
                server_url,
                self.args.result_upload_timeout,
                self.args.result_upload_attempts,
                self.args.result_upload_retry_seconds,
            ),
            name=f"jw2-result-upload-{replay_path.stem}",
            daemon=True,
        )
        thread.start()

    def resolve_game_dir(self, snapshot: Snapshot) -> Path:
        explicit_game_dir = getattr(self.args, "game_dir", None)
        if explicit_game_dir:
            return explicit_game_dir.resolve()

        if snapshot.process and snapshot.process.exe:
            exe_path = normalize_path(snapshot.process.exe)
            if exe_path:
                return exe_path.parent

        return self.default_game_dir

    def resolve_replay_dir(self, game_dir: Path) -> Path:
        return game_dir / REPLAY_DIR

    def reset(self) -> None:
        self.last_pid = None
        self.last_game_dir = None
        self.last_tick = None
        self.last_tick_change = time.monotonic()
        self.stable_since = None
        self.last_state_key = None
        self.armed = bool(self.args.save_existing_replay)
        self.saved_this_session = False
        self.attempt_count = 0
        self.last_attempt_time = 0.0
        self.pending_output_path = None
        self.post_save_ignore_until = 0.0
        self.last_saved_state_key = None

    def update(self, snapshot: Snapshot) -> Path | None:
        if self.args.no_auto_save:
            return None

        if snapshot.process is None:
            self.reset()
            return None

        game_dir = self.resolve_game_dir(snapshot)
        if self.last_pid is not None and snapshot.process.pid != self.last_pid:
            self.reset()
        if self.last_game_dir is not None and game_dir != self.last_game_dir:
            self.reset()
        self.last_pid = snapshot.process.pid
        self.last_game_dir = game_dir
        self.game_dir = game_dir
        self.replay_dir = self.resolve_replay_dir(game_dir)

        if not snapshot.is_player:
            return None

        now = time.monotonic()
        state = replay_tmp_state(self.game_dir)
        tick = snapshot.game_tick

        if self.last_tick is None:
            self.last_tick = tick
            self.last_tick_change = now
        elif tick != self.last_tick:
            self.last_tick = tick
            self.last_tick_change = now
            if now >= self.post_save_ignore_until:
                self.armed = True
                self.saved_this_session = False
                self.attempt_count = 0
                self.last_attempt_time = 0.0
                self.pending_output_path = None

        if state is None:
            self.stable_since = None
            self.last_state_key = None
            return None

        if state.key != self.last_state_key:
            self.last_state_key = state.key
            self.stable_since = now

        tick_idle = now - self.last_tick_change >= self.args.replay_heartbeat_idle_seconds
        file_stable = (
            self.stable_since is not None
            and now - self.stable_since >= self.args.replay_stable_seconds
        )
        ready_to_retry = (
            self.attempt_count == 0
            or now - self.last_attempt_time >= self.args.replay_retry_seconds
        )

        if not (
            self.armed
            and not self.saved_this_session
            and tick_idle
            and file_stable
            and ready_to_retry
            and state.key != self.last_saved_state_key
            and can_read_replay_header(state.path)
        ):
            return None

        if self.pending_output_path is None or self.pending_output_path.exists():
            self.pending_output_path = next_match_replay_path(
                self.replay_dir,
                snapshot,
                self.game_dir,
            ).resolve()

        output_path = self.pending_output_path
        self.attempt_count += 1
        self.last_attempt_time = now
        print(
            f"[auto-save] saving {output_path.name} "
            f"(attempt {self.attempt_count}/{self.args.replay_max_attempts})"
        )

        try:
            base = module_base(snapshot.process.pid)
            result = call_remote_save(snapshot.process.pid, base, output_path)
            saved = output_path.exists() and looks_like_saved_replay(output_path)
            if saved:
                self.saved_this_session = True
                self.armed = False
                self.pending_output_path = None
                self.post_save_ignore_until = (
                    now + self.args.replay_post_save_lock_seconds
                )
                self.last_saved_state_key = state.key
                print(f"[auto-save] saved {output_path}")
                self.queue_result_upload(output_path)
                return output_path

            print(
                "[auto-save] save failed "
                f"(internal result={result}, will retry later)"
            )
        except Exception as exc:
            print(f"[auto-save] error: {exc}", file=sys.stderr)

        if (
            not self.saved_this_session
            and self.attempt_count >= self.args.replay_max_attempts
        ):
            self.armed = False
            self.pending_output_path = None
            print("[auto-save] giving up until the next game activity")

        return None


def initial_game_dir(args: argparse.Namespace) -> Path:
    explicit_game_dir = getattr(args, "game_dir", None)
    if explicit_game_dir:
        return explicit_game_dir.resolve()
    return Path(__file__).resolve().parent


def console_loop(args: argparse.Namespace) -> int:
    game_dir = initial_game_dir(args)
    require_game_dir = bool(getattr(args, "game_dir", None))
    auto_saver = AutoReplaySaver(args, game_dir)
    while True:
        try:
            snapshot = make_snapshot(
                args.players,
                args.objects,
                game_dir,
                require_game_dir,
            )
            if not args.once:
                auto_saver.update(snapshot)
            print(format_snapshot(snapshot, args.verbose))
            print()
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            if args.once:
                return 1

        if args.once:
            return 0
        time.sleep(args.interval)


def visible_process_windows(pid: int) -> list[tuple[int, str, str, tuple[int, int, int, int]]]:
    if not win32gui or not win32process:
        return []

    result: list[tuple[int, str, str, tuple[int, int, int, int]]] = []

    def enum_window(hwnd: int, _param: object) -> None:
        if not win32gui.IsWindowVisible(hwnd):
            return
        _thread_id, window_pid = win32process.GetWindowThreadProcessId(hwnd)
        if window_pid != pid:
            return
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        if right <= left or bottom <= top:
            return
        result.append(
            (
                hwnd,
                win32gui.GetClassName(hwnd),
                win32gui.GetWindowText(hwnd),
                (left, top, right, bottom),
            )
        )

    win32gui.EnumWindows(enum_window, None)
    return result


def find_process_window(pid: int) -> tuple[int, int] | None:
    if not visible_process_windows(pid):
        return None
    return (0, 0)


class Overlay:
    def __init__(self, args: argparse.Namespace):
        if tk is None:
            raise RuntimeError("tkinter is not available; use --console")

        self.args = args
        self.root = tk.Tk()
        self.root.title("Ranker Unit Counter")
        self.root.attributes("-topmost", True)
        if not args.window_frame:
            self.root.overrideredirect(True)
        try:
            alpha = max(0.05, min(1.0, float(args.overlay_alpha)))
            self.root.attributes("-alpha", alpha)
        except tk.TclError:
            pass

        self.game_dir = initial_game_dir(args)
        self.require_game_dir = bool(getattr(args, "game_dir", None))
        self.bg = args.overlay_bg_color
        self.panel_bg = args.overlay_panel_bg_color
        self.text_fg = "#F2F5F9"
        self.muted_fg = "#AAB4C3"
        self.root.configure(bg=self.bg)
        self.root.resizable(False, False)

        self.drag_origin: tuple[int, int] | None = None
        self.user_moved = False
        self.last_auto_position: tuple[int, int] | None = None
        self.inactive_updates = 0
        self.hidden_for_inactive = False
        self.last_game_tick: int | None = None
        self.last_game_tick_change_at: float | None = None
        self.game_tick_seen_moving = False
        self.auto_saver = AutoReplaySaver(args, self.game_dir)
        self.root.bind("<Escape>", lambda _event: self.root.destroy())
        self.root.bind("<ButtonPress-1>", self._start_drag)
        self.root.bind("<B1-Motion>", self._drag)
        self.root.bind("<ButtonPress-3>", lambda _event: self.root.destroy())
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)

        if args.text_overlay:
            self.text = tk.Label(
                self.root,
                text="waiting for ranker.exe",
                font=("Consolas", 13),
                fg=self.text_fg,
                bg=self.panel_bg,
                justify="left",
                padx=10,
                pady=8,
            )
            self.text.pack()
            self.icon_library = None
            self.container = None
        else:
            self.text = None
            self.icon_library = UnitIconLibrary(args.icon_size, args.icon_root)
            self.container = tk.Frame(self.root, bg=self.bg, padx=8, pady=7)
            self.container.pack()
            self.icon_layout_key: object | None = None
            self.note_label: tk.Label | None = None
            self.message_label: tk.Label | None = None
            self.player_heading_labels: dict[int, tk.Label] = {}
            self.player_empty_labels: dict[int, tk.Label] = {}
            self.player_count_items: dict[tuple[int, int], tuple[tk.Canvas, int, int]] = {}
            self.production_items: dict[int, tuple[tk.Canvas, int, int, int]] = {}
            self.upgrade_items: dict[int, tuple[tk.Canvas, int, int, int]] = {}
            self.building_items: dict[int, tuple[tk.Canvas, int, int, int]] = {}

    def _start_drag(self, event: tk.Event) -> None:
        self.drag_origin = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def _drag(self, event: tk.Event) -> None:
        if not self.drag_origin:
            return
        self.user_moved = True
        offset_x, offset_y = self.drag_origin
        self.root.geometry(f"+{event.x_root - offset_x}+{event.y_root - offset_y}")

    def _heartbeat_indicates_active_game(self, tick: int | None) -> bool:
        if self.args.no_game_state_check:
            return True

        now = time.monotonic()
        if tick is None:
            self.last_game_tick = None
            self.last_game_tick_change_at = None
            self.game_tick_seen_moving = False
            return False

        if self.last_game_tick is None:
            self.last_game_tick = tick
            self.last_game_tick_change_at = now
            self.game_tick_seen_moving = False
            return False

        if tick != self.last_game_tick:
            self.last_game_tick = tick
            self.last_game_tick_change_at = now
            self.game_tick_seen_moving = True
            return True

        if not self.game_tick_seen_moving or self.last_game_tick_change_at is None:
            return False

        return (now - self.last_game_tick_change_at) <= GAME_HEARTBEAT_STALE_SECONDS

    def update(self) -> None:
        try:
            snapshot = make_snapshot(
                self.args.players,
                self.args.objects,
                self.game_dir,
                self.require_game_dir,
            )
            if snapshot.process is not None:
                snapshot.game_active = self._heartbeat_indicates_active_game(snapshot.game_tick)
            else:
                self.last_game_tick = None
                self.last_game_tick_change_at = None
                self.game_tick_seen_moving = False
            inactive_game = (
                snapshot.process is not None
                and not snapshot.game_active
            )
            if inactive_game:
                self.inactive_updates += 1
            else:
                self.inactive_updates = 0

            self.auto_saver.update(snapshot)

            has_rendered_content = self.args.text_overlay or self.icon_layout_key is not None
            if snapshot.process is None:
                self._hide_inactive_overlay(snapshot.note)
            elif not snapshot.is_observer:
                self._hide_inactive_overlay("waiting for observer mode")
            elif inactive_game and (self.inactive_updates >= 2 or not has_rendered_content):
                self._hide_inactive_overlay(snapshot.note)
            elif not inactive_game:
                self._show_overlay_window()
                if self.args.text_overlay:
                    assert self.text is not None
                    self.text.configure(text=format_snapshot(snapshot, self.args.verbose))
                else:
                    self._render_icon_overlay(snapshot)

            if snapshot.process and snapshot.is_observer and not inactive_game:
                pos = find_process_window(snapshot.process.pid)
                if pos and not self.user_moved and pos != self.last_auto_position:
                    self.root.geometry(f"+{pos[0]}+{pos[1]}")
                    self.last_auto_position = pos
        except Exception as exc:
            self._show_overlay_window()
            self._render_error(f"error: {exc}")

        self.root.after(max(100, int(self.args.interval * 1000)), self.update)

    def _show_overlay_window(self) -> None:
        if self.hidden_for_inactive:
            self.root.deiconify()
            self.hidden_for_inactive = False

    def _hide_inactive_overlay(self, message: str) -> None:
        if self.args.text_overlay:
            assert self.text is not None
            self.text.configure(text=message or "waiting for active game")
        else:
            if self.icon_layout_key != ("inactive",):
                self._clear_container()
                self.icon_layout_key = ("inactive",)

        if not self.hidden_for_inactive:
            self.root.withdraw()
            self.hidden_for_inactive = True

    def _render_error(self, message: str) -> None:
        if self.args.text_overlay:
            assert self.text is not None
            self.text.configure(text=message)
            return

        assert self.container is not None
        if self.icon_layout_key != ("error",):
            self._clear_container()
            self.message_label = tk.Label(
                self.container,
                text=message,
                font=("Consolas", 11),
                fg="#FFB4AB",
                bg=self.bg,
                padx=6,
                pady=4,
            )
            self.message_label.grid(row=0, column=0)
            self.icon_layout_key = ("error",)
        elif self.message_label is not None:
            self.message_label.configure(text=message)

    def _clear_container(self) -> None:
        assert self.container is not None
        for child in self.container.winfo_children():
            child.destroy()
        self.note_label = None
        self.message_label = None
        self.player_heading_labels = {}
        self.player_empty_labels = {}
        self.player_count_items = {}
        self.production_items = {}
        self.upgrade_items = {}
        self.building_items = {}

    def _render_icon_overlay(self, snapshot: Snapshot) -> None:
        assert self.container is not None

        if not snapshot.process:
            if self.icon_layout_key != ("message",):
                self._clear_container()
                self.message_label = tk.Label(
                    self.container,
                    text=snapshot.note,
                    font=("Consolas", 11),
                    fg=self.muted_fg,
                    bg=self.bg,
                    padx=6,
                    pady=4,
                )
                self.message_label.grid(row=0, column=0)
                self.icon_layout_key = ("message",)
            elif self.message_label is not None:
                self.message_label.configure(text=snapshot.note)
            return

        layout_key = self._snapshot_layout_key(snapshot)
        if self.icon_layout_key != layout_key:
            self._build_icon_overlay(snapshot)
            self.icon_layout_key = layout_key
        else:
            self._update_icon_values(snapshot)

    def _snapshot_layout_key(self, snapshot: Snapshot) -> tuple:
        player_layout = tuple(
            (
                player.slot,
                player.name,
                tuple(
                    unit_type
                    for unit_type, count in enumerate(player.unit_counts)
                    if count > 0
                ),
                tuple(
                    (
                        production.object_offset,
                        production.building_type,
                        production.unit_type,
                    )
                    for production in snapshot.productions
                    if production.slot == player.slot
                ),
                tuple(
                    (
                        upgrade.object_offset,
                        upgrade.building_type,
                        upgrade.upgrade_id,
                    )
                    for upgrade in snapshot.upgrades
                    if upgrade.slot == player.slot
                ),
                tuple(
                    (
                        building.object_offset,
                        building.building_type,
                    )
                    for building in snapshot.buildings
                    if building.slot == player.slot
                ),
            )
            for player in snapshot.players
        )
        return (
            "icons",
            player_layout,
            bool(snapshot.note),
            max(1, self.args.icons_per_row),
            self.args.icon_size,
        )

    def _build_icon_overlay(self, snapshot: Snapshot) -> None:
        assert self.container is not None
        self._clear_container()

        row_index = 0
        for player in snapshot.players:
            productions = [
                production
                for production in snapshot.productions
                if production.slot == player.slot
            ]
            upgrades = [
                upgrade
                for upgrade in snapshot.upgrades
                if upgrade.slot == player.slot
            ]
            buildings = [
                building
                for building in snapshot.buildings
                if building.slot == player.slot
            ]
            self._build_player_row(row_index, player, productions, upgrades, buildings)
            row_index += 1

        if snapshot.note:
            self.note_label = tk.Label(
                self.container,
                text=snapshot.note,
                font=("Consolas", 9),
                fg="#FFCF70",
                bg=self.bg,
                anchor="w",
            )
            self.note_label.grid(row=row_index, column=0, sticky="w", pady=(4, 0))

    def _update_icon_values(self, snapshot: Snapshot) -> None:
        for player in snapshot.players:
            heading_label = self.player_heading_labels.get(player.slot)
            if heading_label is not None:
                heading_label.configure(text=self._format_player_heading(player))

            if not any(count > 0 for count in player.unit_counts):
                empty_label = self.player_empty_labels.get(player.slot)
                if empty_label is not None:
                    empty_label.configure(text="0")
                continue

            for unit_type, count in enumerate(player.unit_counts):
                if count <= 0:
                    continue
                items = self.player_count_items.get((player.slot, unit_type))
                if items is None:
                    continue
                canvas, shadow_id, text_id = items
                text = format_unit_count(count)
                canvas.itemconfigure(shadow_id, text=text)
                canvas.itemconfigure(text_id, text=text)

        for production in snapshot.productions:
            items = self.production_items.get(production.object_offset)
            if items is None:
                continue
            canvas, fill_id, shadow_id, text_id = items
            fill_width = self._production_fill_width(production)
            canvas.coords(
                fill_id,
                3,
                self.args.icon_size + 3,
                3 + fill_width,
                self.args.icon_size + 7,
            )
            text = self._format_production_percent(production)
            canvas.itemconfigure(shadow_id, text=text)
            canvas.itemconfigure(text_id, text=text)

        for upgrade in snapshot.upgrades:
            items = self.upgrade_items.get(upgrade.object_offset)
            if items is None:
                continue
            canvas, fill_id, shadow_id, text_id = items
            fill_width = self._upgrade_fill_width(upgrade)
            canvas.coords(
                fill_id,
                3,
                self.args.icon_size + 3,
                3 + fill_width,
                self.args.icon_size + 7,
            )
            text = self._format_upgrade_percent(upgrade)
            canvas.itemconfigure(shadow_id, text=text)
            canvas.itemconfigure(text_id, text=text)

        for building in snapshot.buildings:
            items = self.building_items.get(building.object_offset)
            if items is None:
                continue
            canvas, fill_id, shadow_id, text_id = items
            fill_width = self._building_fill_width(building)
            canvas.coords(
                fill_id,
                3,
                self.args.icon_size + 3,
                3 + fill_width,
                self.args.icon_size + 7,
            )
            text = self._format_building_percent(building)
            canvas.itemconfigure(shadow_id, text=text)
            canvas.itemconfigure(text_id, text=text)

        if self.note_label is not None:
            self.note_label.configure(text=snapshot.note)

    def _build_player_row(
        self,
        row_index: int,
        player: PlayerSnapshot,
        productions: list[ProductionSnapshot],
        upgrades: list[UpgradeSnapshot],
        buildings: list[BuildingSnapshot],
    ) -> None:
        assert self.container is not None
        assert self.icon_library is not None

        color = PLAYER_COLORS[player.slot % len(PLAYER_COLORS)]
        player_frame = tk.Frame(self.container, bg=self.bg)
        player_frame.grid(row=row_index, column=0, sticky="w", pady=(2, 7))

        label = tk.Label(
            player_frame,
            text=self._format_player_heading(player),
            font=("Consolas", 10, "bold"),
            fg=color,
            bg=self.bg,
            anchor="w",
            justify="left",
        )
        label.grid(row=0, column=0, sticky="w", pady=(0, 2))
        self.player_heading_labels[player.slot] = label

        units = [
            (unit_type, count)
            for unit_type, count in enumerate(player.unit_counts)
            if count > 0
        ]

        next_row = 1
        if not units:
            empty_label = tk.Label(
                player_frame,
                text="0",
                font=("Consolas", 11),
                fg=self.muted_fg,
                bg=self.bg,
                width=3,
            )
            empty_label.grid(row=next_row, column=0, sticky="w")
            self.player_empty_labels[player.slot] = empty_label
            next_row += 1
        else:
            grid = tk.Frame(player_frame, bg=self.bg)
            grid.grid(row=next_row, column=0, sticky="w")
            next_row += 1

            max_columns = max(1, self.args.icons_per_row)
            cell_size = self.args.icon_size + 10
            for index, (unit_type, count) in enumerate(units):
                row = index // max_columns
                column = index % max_columns
                canvas = tk.Canvas(
                    grid,
                    width=cell_size,
                    height=cell_size,
                    highlightthickness=0,
                    bd=0,
                    bg=self.panel_bg,
                )
                canvas.grid(row=row, column=column, padx=1, pady=1)
                canvas.create_image(
                    cell_size // 2,
                    cell_size // 2 - 2,
                    image=self.icon_library.photo(unit_type),
                )
                text = format_unit_count(count)
                shadow_id = canvas.create_text(
                    cell_size - 2,
                    cell_size - 1,
                    text=text,
                    anchor="se",
                    font=("Consolas", 9, "bold"),
                    fill="#000000",
                )
                text_id = canvas.create_text(
                    cell_size - 3,
                    cell_size - 2,
                    text=text,
                    anchor="se",
                    font=("Consolas", 9, "bold"),
                    fill="#FFFFFF",
                )
                canvas.create_rectangle(
                    0,
                    0,
                    cell_size - 1,
                    cell_size - 1,
                    outline="#2C3542",
                )
                canvas.tooltip_text = self.icon_library.name(unit_type)  # type: ignore[attr-defined]
                self.player_count_items[(player.slot, unit_type)] = (
                    canvas,
                    shadow_id,
                    text_id,
                )

        if productions or upgrades or buildings:
            self._build_progress_row(player_frame, productions, upgrades, buildings, next_row)

    def _format_player_heading(self, player: PlayerSnapshot) -> str:
        return player.name or f"P{player.slot + 1}"

    def _build_progress_row(
        self,
        player_frame: tk.Frame,
        productions: list[ProductionSnapshot],
        upgrades: list[UpgradeSnapshot],
        buildings: list[BuildingSnapshot],
        grid_row: int,
    ) -> None:
        assert self.icon_library is not None

        grid = tk.Frame(player_frame, bg=self.bg)
        grid.grid(row=grid_row, column=0, sticky="w", pady=(2, 0))

        max_columns = max(1, self.args.icons_per_row)
        for index, production in enumerate(productions):
            self._build_production_cell(grid, index, max_columns, production)
        for index, upgrade in enumerate(upgrades, start=len(productions)):
            self._build_upgrade_cell(grid, index, max_columns, upgrade)
        start = len(productions) + len(upgrades)
        for index, building in enumerate(buildings, start=start):
            self._build_building_cell(grid, index, max_columns, building)

    def _build_production_cell(
        self,
        grid: tk.Frame,
        index: int,
        max_columns: int,
        production: ProductionSnapshot,
    ) -> None:
        assert self.icon_library is not None

        cell_size = self.args.icon_size + 10
        row = index // max_columns
        column = index % max_columns
        canvas = tk.Canvas(
            grid,
            width=cell_size,
            height=cell_size,
            highlightthickness=0,
            bd=0,
            bg="#121A22",
        )
        canvas.grid(row=row, column=column, padx=1, pady=1)
        canvas.create_image(
            cell_size // 2,
            cell_size // 2 - 2,
            image=self.icon_library.photo(production.unit_type),
        )
        canvas.create_rectangle(
            2,
            self.args.icon_size + 2,
            cell_size - 3,
            self.args.icon_size + 8,
            fill="#24303B",
            outline="#344250",
        )
        fill_id = canvas.create_rectangle(
            3,
            self.args.icon_size + 3,
            3 + self._production_fill_width(production),
            self.args.icon_size + 7,
            fill="#41D99B",
            outline="",
        )
        text = self._format_production_percent(production)
        shadow_id = canvas.create_text(
            cell_size - 2,
            2,
            text=text,
            anchor="ne",
            font=("Consolas", 8, "bold"),
            fill="#000000",
        )
        text_id = canvas.create_text(
            cell_size - 3,
            1,
            text=text,
            anchor="ne",
            font=("Consolas", 8, "bold"),
            fill="#DFFCF1",
        )
        canvas.create_rectangle(
            0,
            0,
            cell_size - 1,
            cell_size - 1,
            outline="#3B4C5C",
        )
        canvas.tooltip_text = self.icon_library.name(production.unit_type)  # type: ignore[attr-defined]
        self.production_items[production.object_offset] = (
            canvas,
            fill_id,
            shadow_id,
            text_id,
        )

    def _production_fill_width(self, production: ProductionSnapshot) -> int:
        cell_size = self.args.icon_size + 10
        max_width = max(1, cell_size - 6)
        if production.total <= 0:
            return 0
        return max(0, min(max_width, round(max_width * production.progress / production.total)))

    def _format_production_percent(self, production: ProductionSnapshot) -> str:
        if production.total <= 0:
            return ""
        percent = max(0, min(99, int(production.progress * 100 / production.total)))
        return f"{percent}%"

    def _build_upgrade_cell(
        self,
        grid: tk.Frame,
        index: int,
        max_columns: int,
        upgrade: UpgradeSnapshot,
    ) -> None:
        assert self.icon_library is not None

        cell_size = self.args.icon_size + 10
        row = index // max_columns
        column = index % max_columns
        canvas = tk.Canvas(
            grid,
            width=cell_size,
            height=cell_size,
            highlightthickness=0,
            bd=0,
            bg="#1B1711",
        )
        canvas.grid(row=row, column=column, padx=1, pady=1)
        canvas.create_image(
            cell_size // 2,
            cell_size // 2 - 2,
            image=self.icon_library.upgrade_photo(upgrade.upgrade_id),
        )
        canvas.create_rectangle(
            2,
            self.args.icon_size + 2,
            cell_size - 3,
            self.args.icon_size + 8,
            fill="#312B1C",
            outline="#5B4A28",
        )
        fill_id = canvas.create_rectangle(
            3,
            self.args.icon_size + 3,
            3 + self._upgrade_fill_width(upgrade),
            self.args.icon_size + 7,
            fill="#F2C14E",
            outline="",
        )
        text = self._format_upgrade_percent(upgrade)
        shadow_id = canvas.create_text(
            cell_size - 2,
            2,
            text=text,
            anchor="ne",
            font=("Consolas", 8, "bold"),
            fill="#000000",
        )
        text_id = canvas.create_text(
            cell_size - 3,
            1,
            text=text,
            anchor="ne",
            font=("Consolas", 8, "bold"),
            fill="#FFF2C2",
        )
        canvas.create_rectangle(
            0,
            0,
            cell_size - 1,
            cell_size - 1,
            outline="#6C5526",
        )
        canvas.tooltip_text = f"Upgrade {upgrade.upgrade_id}"  # type: ignore[attr-defined]
        self.upgrade_items[upgrade.object_offset] = (
            canvas,
            fill_id,
            shadow_id,
            text_id,
        )

    def _upgrade_fill_width(self, upgrade: UpgradeSnapshot) -> int:
        cell_size = self.args.icon_size + 10
        max_width = max(1, cell_size - 6)
        if upgrade.total <= 0:
            return 0
        return max(0, min(max_width, round(max_width * upgrade.progress / upgrade.total)))

    def _format_upgrade_percent(self, upgrade: UpgradeSnapshot) -> str:
        if upgrade.total <= 0:
            return ""
        percent = max(0, min(99, int(upgrade.progress * 100 / upgrade.total)))
        return f"{percent}%"

    def _build_building_cell(
        self,
        grid: tk.Frame,
        index: int,
        max_columns: int,
        building: BuildingSnapshot,
    ) -> None:
        assert self.icon_library is not None

        cell_size = self.args.icon_size + 10
        row = index // max_columns
        column = index % max_columns
        canvas = tk.Canvas(
            grid,
            width=cell_size,
            height=cell_size,
            highlightthickness=0,
            bd=0,
            bg="#111A24",
        )
        canvas.grid(row=row, column=column, padx=1, pady=1)
        canvas.create_image(
            cell_size // 2,
            cell_size // 2 - 2,
            image=self.icon_library.photo(building.building_type),
        )
        canvas.create_rectangle(
            2,
            self.args.icon_size + 2,
            cell_size - 3,
            self.args.icon_size + 8,
            fill="#1E2A38",
            outline="#354A5F",
        )
        fill_id = canvas.create_rectangle(
            3,
            self.args.icon_size + 3,
            3 + self._building_fill_width(building),
            self.args.icon_size + 7,
            fill="#58A6FF",
            outline="",
        )
        text = self._format_building_percent(building)
        shadow_id = canvas.create_text(
            cell_size - 2,
            2,
            text=text,
            anchor="ne",
            font=("Consolas", 8, "bold"),
            fill="#000000",
        )
        text_id = canvas.create_text(
            cell_size - 3,
            1,
            text=text,
            anchor="ne",
            font=("Consolas", 8, "bold"),
            fill="#D8ECFF",
        )
        canvas.create_rectangle(
            0,
            0,
            cell_size - 1,
            cell_size - 1,
            outline="#40617D",
        )
        canvas.tooltip_text = self.icon_library.name(building.building_type)  # type: ignore[attr-defined]
        self.building_items[building.object_offset] = (
            canvas,
            fill_id,
            shadow_id,
            text_id,
        )

    def _building_fill_width(self, building: BuildingSnapshot) -> int:
        cell_size = self.args.icon_size + 10
        max_width = max(1, cell_size - 6)
        if building.total <= 0:
            return 0
        return max(0, min(max_width, round(max_width * building.progress / building.total)))

    def _format_building_percent(self, building: BuildingSnapshot) -> str:
        if building.total <= 0:
            return ""
        percent = max(0, min(99, int(building.progress * 100 / building.total)))
        return f"{percent}%"

    def run(self) -> None:
        self.update()
        self.root.mainloop()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ranker observer overlay and replay auto-saver"
    )
    parser.add_argument(
        "--console",
        action="store_true",
        help="print to console instead of showing a topmost overlay",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="read once and exit; implies --console",
    )
    parser.add_argument(
        "--players",
        help="comma-separated zero-based slots to show, e.g. 0,1",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=0.5,
        help="refresh interval in seconds",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="include raw status values",
    )
    parser.add_argument(
        "--objects",
        action="store_true",
        help="also walk the live object list for a diagnostic object count",
    )
    parser.add_argument(
        "--no-game-state-check",
        action="store_true",
        help="do not hide the overlay when the game heartbeat stops",
    )
    parser.add_argument(
        "--no-auto-save",
        action="store_true",
        help="disable automatic replay saving when the watched game ends",
    )
    parser.add_argument(
        "--save-existing-replay",
        action="store_true",
        help="allow saving an already idle Replay.tmp without seeing game activity first",
    )
    parser.add_argument(
        "--replay-stable-seconds",
        type=float,
        default=3.0,
        help="Replay.tmp must keep the same size/mtime this long before saving",
    )
    parser.add_argument(
        "--replay-heartbeat-idle-seconds",
        type=float,
        default=3.0,
        help="game heartbeat must stop changing this long before replay saving",
    )
    parser.add_argument(
        "--replay-retry-seconds",
        type=float,
        default=10.0,
        help="seconds to wait before retrying a failed replay save",
    )
    parser.add_argument(
        "--replay-max-attempts",
        type=int,
        default=3,
        help="maximum replay save attempts for one game session",
    )
    parser.add_argument(
        "--replay-post-save-lock-seconds",
        type=float,
        default=30.0,
        help="ignore new heartbeat activity this long after a successful replay save",
    )
    parser.add_argument(
        "--result-server-url",
        default=DEFAULT_RESULT_SERVER_URL,
        help="POST /replay URL for uploading saved .ply results",
    )
    parser.add_argument(
        "--no-result-upload",
        action="store_true",
        help="do not upload saved .ply files to the result server",
    )
    parser.add_argument(
        "--result-upload-timeout",
        type=float,
        default=5.0,
        help="seconds to wait for each result server upload attempt",
    )
    parser.add_argument(
        "--result-upload-attempts",
        type=int,
        default=3,
        help="maximum result server upload attempts per saved replay",
    )
    parser.add_argument(
        "--result-upload-retry-seconds",
        type=float,
        default=10.0,
        help="seconds between result server upload attempts",
    )
    parser.add_argument(
        "--text-overlay",
        action="store_true",
        help="use the old text-only overlay instead of unit icon rows",
    )
    parser.add_argument(
        "--window-frame",
        action="store_true",
        help="show the normal window frame around the overlay",
    )
    parser.add_argument(
        "--overlay-bg-color",
        default="#080A0D",
        help="overlay background color, e.g. #080A0D",
    )
    parser.add_argument(
        "--overlay-panel-bg-color",
        default="#10151C",
        help="overlay panel background color, e.g. #10151C",
    )
    parser.add_argument(
        "--overlay-alpha",
        type=float,
        default=0.94,
        help="overlay window opacity from 0.05 to 1.0",
    )
    parser.add_argument(
        "--icon-size",
        type=int,
        default=MAPEDITOR_ICON_SIZE,
        help="unit icon size in pixels for the graphical overlay",
    )
    parser.add_argument(
        "--icons-per-row",
        type=int,
        default=18,
        help="maximum unit icons per player row before wrapping",
    )
    parser.add_argument(
        "--icon-root",
        type=Path,
        help="explicit overlay asset root, decoded_chr directory, or prebuilt unit_icons directory",
    )
    parser.add_argument(
        "--game-dir",
        type=Path,
        help="explicit ranker.exe directory containing Replay.tmp and Replays",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.once:
        args.console = True

    if args.console:
        return console_loop(args)

    overlay = Overlay(args)
    overlay.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
