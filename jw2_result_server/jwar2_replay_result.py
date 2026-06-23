#!/usr/bin/env python3
"""Jwar2/Jurassic War 2 .ply 리플레이의 항복·이탈 결과를 판정한다.

제공된 리플레이 5개를 교차 비교해 확인한 구조:

* .ply는 ``TRC\x1a`` 컨테이너이다.
* 내부 ``Replay`` 엔트리는 zlib 압축 데이터이다.
* Replay+0x63 : 경기 참가자 수(uint32 little-endian)
* Replay+0x6B : 0x63을 기준으로 한 이름 블록 상대 위치
* Replay+0xAB : 8개 슬롯의 종류(byte[8])
    - 0x00: 사람 참가자
    - 0x01: 컴퓨터 참가자
    - 0x02: 관전자
    - 0x14: 빈/닫힌 슬롯
* 이름 블록 : 슬롯당 32바이트, 최대 8개, CP949 C 문자열
* 이름 블록 뒤 : 36바이트 고정 길이 이벤트 레코드

항복/이탈 판정:

1. opcode 0x13, 마지막 uint32 0x14인 레코드를 이탈 요청으로 본다.
2. 그 뒤 가까운 opcode 0x1D 레코드의 target_slot이 요청 슬롯과 같으면
   해당 이탈이 확인된 것으로 본다.
3. 관전자를 제외하고 가장 먼저 확인된 참가자 이탈이 패배를 결정한다.
4. 그 시점에 남은 참가자가 한 명이면 그 참가자를 승자로 판정한다.
   이후 승자가 리플레이 종료 과정에서 이탈한 이벤트는 결과에 반영하지 않는다.

주의:
이 코드는 현재 확보된 포맷과 "항복/이탈로 끝난 경기"에 대해 검증한
역분석 기반 판정기이다. 목표물 파괴 등 이탈 이벤트 없이 정상 종료된 경기,
팀전의 팀 승패는 추가 샘플이 필요하다.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import zlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence


TRC_MAGIC = b"TRC\x1a"
TRC_HEADER_SIZE = 0x20
TRC_ENTRY_SIZE = 0x20
TRC_ENTRY_STRUCT = struct.Struct("<12sIIIII")

REPLAY_MAGIC = b"Jwar2 Replay File."
PARTICIPANT_COUNT_OFFSET = 0x63
NAME_BLOCK_RELATIVE_OFFSET_FIELD = 0x6B
NAME_BLOCK_RELATIVE_BASE = 0x63
SLOT_TYPE_TABLE_OFFSET = 0xAB
MAX_SLOTS = 8
NAME_SLOT_SIZE = 0x20
EVENT_RECORD_STRUCT = struct.Struct("<9I")
EVENT_RECORD_SIZE = EVENT_RECORD_STRUCT.size

SLOT_HUMAN = 0x00
SLOT_COMPUTER = 0x01
SLOT_SPECTATOR = 0x02
SLOT_EMPTY = 0x14

LEAVE_REQUEST_OPCODE = 0x13
LEAVE_REQUEST_MARKER = 0x14
LEAVE_CONFIRM_OPCODE = 0x1D

# 확인 레코드는 제공된 모든 샘플에서 요청 직후 또는 매우 가까운 위치에 있으며,
# 요청 tick과 확인 tick의 차이는 7이었다. 포맷 변형에 대비해 여유를 둔다.
MAX_CONFIRM_RECORD_GAP = 32
MAX_CONFIRM_TICK_GAP = 64


class ReplayFormatError(ValueError):
    """지원하지 않거나 손상된 리플레이 형식."""


@dataclass(frozen=True)
class TrcEntry:
    name: str
    relative_offset: int
    unpacked_size: int
    stored_size: int


@dataclass(frozen=True)
class PlayerSlot:
    slot: int
    name: str
    type_code: int
    role: str

    @property
    def is_participant(self) -> bool:
        return self.type_code in (SLOT_HUMAN, SLOT_COMPUTER)


@dataclass(frozen=True)
class EventRecord:
    index: int
    enabled: int
    tick: int
    source_slot: int
    opcode: int
    target_slot: int
    trailer: int

    @property
    def is_leave_request(self) -> bool:
        return (
            self.enabled != 0
            and self.opcode == LEAVE_REQUEST_OPCODE
            and self.trailer == LEAVE_REQUEST_MARKER
            and 0 <= self.source_slot < MAX_SLOTS
        )

    @property
    def is_leave_confirmation(self) -> bool:
        return (
            self.enabled != 0
            and self.opcode == LEAVE_CONFIRM_OPCODE
            and 0 <= self.target_slot < MAX_SLOTS
        )


@dataclass(frozen=True)
class Departure:
    slot: int
    request_event_index: int
    confirm_event_index: int
    request_tick: int
    confirm_tick: int
    tick_gap: int


@dataclass(frozen=True)
class ReplayResult:
    path: str
    declared_participant_count: int
    participant_slots: list[int]
    slots: list[PlayerSlot]
    departures: list[Departure]
    decisive_departure: Departure | None
    winner_slot: int | None
    winner_name: str | None
    loser_slots: list[int]
    loser_names: list[str]
    status: str
    reason: str
    warnings: list[str]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _u32(data: bytes, offset: int, field: str) -> int:
    if offset < 0 or offset + 4 > len(data):
        raise ReplayFormatError(
            f"{field} 읽기 범위가 데이터를 벗어났습니다: offset=0x{offset:X}"
        )
    return struct.unpack_from("<I", data, offset)[0]


def _decode_c_string(raw: bytes, preferred_encoding: str = "cp949") -> str:
    value = raw.split(b"\x00", 1)[0]
    if not value:
        return ""

    encodings = []
    for encoding in (preferred_encoding, "utf-8", "euc-kr"):
        if encoding not in encodings:
            encodings.append(encoding)

    for encoding in encodings:
        try:
            return value.decode(encoding)
        except UnicodeDecodeError:
            pass
    return value.decode(preferred_encoding, errors="replace")


def _slot_role(type_code: int) -> str:
    if type_code == SLOT_HUMAN:
        return "사람 참가자"
    if type_code == SLOT_COMPUTER:
        return "컴퓨터 참가자"
    if type_code == SLOT_SPECTATOR:
        return "관전자"
    if type_code == SLOT_EMPTY:
        return "빈 슬롯"
    return f"알 수 없음(0x{type_code:02X})"


def _parse_trc(blob: bytes) -> tuple[int, list[TrcEntry]]:
    if len(blob) < TRC_HEADER_SIZE or blob[:4] != TRC_MAGIC:
        raise ReplayFormatError("TRC 형식의 .ply 파일이 아닙니다.")

    entry_count = _u32(blob, 0x04, "TRC 엔트리 수")
    data_base = _u32(blob, 0x0C, "TRC 데이터 시작 위치")
    directory_end = TRC_HEADER_SIZE + entry_count * TRC_ENTRY_SIZE

    if entry_count <= 0 or entry_count > 4096:
        raise ReplayFormatError(f"비정상적인 TRC 엔트리 수입니다: {entry_count}")
    if directory_end > len(blob):
        raise ReplayFormatError("TRC 엔트리 테이블이 파일 범위를 벗어났습니다.")
    if not (directory_end <= data_base <= len(blob)):
        raise ReplayFormatError(
            "TRC 데이터 시작 위치가 잘못되었습니다: "
            f"directory_end=0x{directory_end:X}, data_base=0x{data_base:X}"
        )

    entries: list[TrcEntry] = []
    for index in range(entry_count):
        offset = TRC_HEADER_SIZE + index * TRC_ENTRY_SIZE
        raw_name, relative, unpacked, stored, *_ = (
            TRC_ENTRY_STRUCT.unpack_from(blob, offset)
        )
        name = raw_name.split(b"\x00", 1)[0].decode("ascii", errors="replace")

        if name:
            start = data_base + relative
            end = start + stored
            if start < data_base or end > len(blob):
                raise ReplayFormatError(
                    f"TRC 엔트리 범위가 잘못되었습니다: {name!r}, "
                    f"0x{start:X}-0x{end:X}"
                )

        entries.append(
            TrcEntry(
                name=name,
                relative_offset=relative,
                unpacked_size=unpacked,
                stored_size=stored,
            )
        )

    return data_base, entries


def extract_replay(ply_path: Path) -> bytes:
    blob = ply_path.read_bytes()
    data_base, entries = _parse_trc(blob)

    entry = next(
        (item for item in entries if item.name.casefold() == "replay"),
        None,
    )
    if entry is None:
        raise ReplayFormatError(".ply 내부에서 Replay 엔트리를 찾지 못했습니다.")

    start = data_base + entry.relative_offset
    stored = blob[start : start + entry.stored_size]

    try:
        replay = zlib.decompress(stored)
    except zlib.error as exc:
        if entry.stored_size == entry.unpacked_size:
            replay = stored
        else:
            raise ReplayFormatError(f"Replay zlib 압축 해제 실패: {exc}") from exc

    if len(replay) != entry.unpacked_size:
        raise ReplayFormatError(
            "Replay 압축 해제 크기가 TRC 헤더와 다릅니다: "
            f"expected={entry.unpacked_size}, actual={len(replay)}"
        )
    if not replay.startswith(REPLAY_MAGIC):
        raise ReplayFormatError("지원하는 Jwar2 Replay 데이터가 아닙니다.")

    return replay


def parse_slots(
    replay: bytes,
    encoding: str = "cp949",
) -> tuple[int, int, list[PlayerSlot], list[str]]:
    warnings: list[str] = []

    declared_count = _u32(
        replay,
        PARTICIPANT_COUNT_OFFSET,
        "Replay 참가자 수",
    )
    if not 1 <= declared_count <= MAX_SLOTS:
        raise ReplayFormatError(f"비정상적인 참가자 수입니다: {declared_count}")

    relative = _u32(
        replay,
        NAME_BLOCK_RELATIVE_OFFSET_FIELD,
        "이름 블록 상대 위치",
    )
    names_offset = NAME_BLOCK_RELATIVE_BASE + relative
    events_offset = names_offset + MAX_SLOTS * NAME_SLOT_SIZE

    required = max(
        SLOT_TYPE_TABLE_OFFSET + MAX_SLOTS,
        events_offset,
    )
    if required > len(replay):
        raise ReplayFormatError(
            "Replay 헤더/이름 블록 위치가 데이터 범위를 벗어났습니다: "
            f"names=0x{names_offset:X}, events=0x{events_offset:X}, "
            f"size=0x{len(replay):X}"
        )

    slots: list[PlayerSlot] = []
    for slot in range(MAX_SLOTS):
        start = names_offset + slot * NAME_SLOT_SIZE
        name = _decode_c_string(
            replay[start : start + NAME_SLOT_SIZE],
            preferred_encoding=encoding,
        )
        type_code = replay[SLOT_TYPE_TABLE_OFFSET + slot]

        slots.append(
            PlayerSlot(
                slot=slot,
                name=name,
                type_code=type_code,
                role=_slot_role(type_code),
            )
        )

    actual_count = sum(1 for slot in slots if slot.is_participant)
    if actual_count != declared_count:
        warnings.append(
            "헤더의 참가자 수와 슬롯 종류표에서 확인한 참가자 수가 다릅니다: "
            f"declared={declared_count}, detected={actual_count}"
        )

    return declared_count, events_offset, slots, warnings


def parse_events(
    replay: bytes,
    events_offset: int,
) -> tuple[list[EventRecord], list[str]]:
    warnings: list[str] = []
    if events_offset > len(replay):
        raise ReplayFormatError("이벤트 시작 위치가 Replay 범위를 벗어났습니다.")

    remaining = len(replay) - events_offset
    record_count, trailing_size = divmod(remaining, EVENT_RECORD_SIZE)
    if trailing_size:
        warnings.append(
            "이벤트 블록 끝에 36바이트 레코드에 포함되지 않는 데이터가 있습니다: "
            f"{trailing_size} bytes"
        )

    events: list[EventRecord] = []
    for index in range(record_count):
        offset = events_offset + index * EVENT_RECORD_SIZE
        record = replay[offset : offset + EVENT_RECORD_SIZE]
        values = EVENT_RECORD_STRUCT.unpack(record)
        packed_source_opcode = values[3]

        events.append(
            EventRecord(
                index=index,
                enabled=values[0],
                tick=values[1],
                source_slot=packed_source_opcode & 0xFF,
                opcode=(packed_source_opcode >> 24) & 0xFF,
                target_slot=values[4],
                trailer=values[8],
            )
        )

    return events, warnings


def find_confirmed_departures(events: Sequence[EventRecord]) -> list[Departure]:
    departures: list[Departure] = []
    used_confirmation_indexes: set[int] = set()

    for request_pos, request in enumerate(events):
        if not request.is_leave_request:
            continue

        stop = min(len(events), request_pos + 1 + MAX_CONFIRM_RECORD_GAP)
        confirmation: EventRecord | None = None

        for candidate in events[request_pos + 1 : stop]:
            if candidate.index in used_confirmation_indexes:
                continue
            if not candidate.is_leave_confirmation:
                continue
            if candidate.target_slot != request.source_slot:
                continue

            tick_gap = candidate.tick - request.tick
            if not 0 <= tick_gap <= MAX_CONFIRM_TICK_GAP:
                continue

            confirmation = candidate
            break

        if confirmation is None:
            continue

        used_confirmation_indexes.add(confirmation.index)
        departures.append(
            Departure(
                slot=request.source_slot,
                request_event_index=request.index,
                confirm_event_index=confirmation.index,
                request_tick=request.tick,
                confirm_tick=confirmation.tick,
                tick_gap=confirmation.tick - request.tick,
            )
        )

    departures.sort(
        key=lambda item: (item.request_event_index, item.confirm_event_index)
    )
    return departures


def determine_result(
    path: Path,
    declared_count: int,
    slots: list[PlayerSlot],
    departures: list[Departure],
    warnings: list[str],
) -> ReplayResult:
    participant_slots = [slot.slot for slot in slots if slot.is_participant]
    active = set(participant_slots)
    eliminated: list[int] = []
    decisive: Departure | None = None
    winner_slot: int | None = None

    for departure in departures:
        slot = departure.slot
        if slot not in active:
            # 관전자, 빈 슬롯, 이미 제거된 참가자는 승패에서 제외한다.
            continue

        active.remove(slot)
        eliminated.append(slot)

        # 참가자가 한 명 남는 최초 시점이 경기 결과가 확정되는 순간이다.
        # 그 뒤 승자가 리플레이 종료 과정에서 이탈해도 무시한다.
        if len(active) == 1:
            decisive = departure
            winner_slot = next(iter(active))
            break

    slot_by_index = {slot.slot: slot for slot in slots}

    if winner_slot is not None and eliminated:
        winner_name = slot_by_index[winner_slot].name
        loser_names = [slot_by_index[index].name for index in eliminated]
        status = "determined"
        reason = (
            "관전자를 제외한 참가자 중 최초로 확인된 이탈 슬롯을 제거했을 때 "
            "한 명만 남았습니다. 이 최초 이탈이 패배를 결정하며, 이후 종료 과정의 "
            "추가 이탈은 결과에서 제외했습니다."
        )
    else:
        winner_name = None
        loser_names = []
        status = "undetermined"
        if len(participant_slots) < 2:
            reason = "확인된 경기 참가자가 두 명 미만입니다."
        elif not departures:
            reason = (
                "0x13 이탈 요청과 같은 슬롯을 대상으로 한 0x1D 확인 이벤트의 "
                "쌍을 찾지 못했습니다."
            )
        else:
            reason = (
                "확인된 이탈을 적용한 뒤에도 승자 한 명으로 좁혀지지 않았습니다. "
                "이탈 없이 끝난 정상 승리 또는 팀전일 수 있습니다."
            )

    return ReplayResult(
        path=str(path),
        declared_participant_count=declared_count,
        participant_slots=participant_slots,
        slots=slots,
        departures=departures,
        decisive_departure=decisive,
        winner_slot=winner_slot,
        winner_name=winner_name,
        loser_slots=eliminated,
        loser_names=loser_names,
        status=status,
        reason=reason,
        warnings=warnings,
    )


def analyze_replay(path: Path, encoding: str = "cp949") -> ReplayResult:
    replay = extract_replay(path)
    declared_count, events_offset, slots, slot_warnings = parse_slots(
        replay,
        encoding=encoding,
    )
    events, event_warnings = parse_events(replay, events_offset)
    departures = find_confirmed_departures(events)
    return determine_result(
        path=path,
        declared_count=declared_count,
        slots=slots,
        departures=departures,
        warnings=slot_warnings + event_warnings,
    )


def _player_label(name: str | None, slot: int | None) -> str:
    if name:
        return name
    if slot is not None:
        return f"slot {slot}"
    return "판정 불가"


def _error_result(path: Path, error: Exception) -> ReplayResult:
    return ReplayResult(
        path=str(path),
        declared_participant_count=0,
        participant_slots=[],
        slots=[],
        departures=[],
        decisive_departure=None,
        winner_slot=None,
        winner_name=None,
        loser_slots=[],
        loser_names=[],
        status="error",
        reason=str(error),
        warnings=[],
    )


def print_human(result: ReplayResult) -> None:
    if result.status == "determined":
        losers = (
            _player_label(name, slot)
            for name, slot in zip(result.loser_names, result.loser_slots)
        )
        print(f"승자: {_player_label(result.winner_name, result.winner_slot)}")
        print(f"패자: {', '.join(losers) or '판정 불가'}")
    else:
        print("승자: 판정 불가")
        print("패자: 판정 불가")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Jwar2 .ply 리플레이에서 항복/이탈 승패를 판정합니다."
    )
    parser.add_argument(
        "ply",
        nargs="+",
        type=Path,
        help="분석할 .ply 파일. 여러 개를 한 번에 지정할 수 있습니다.",
    )
    parser.add_argument(
        "--encoding",
        default="cp949",
        help="닉네임 문자열 인코딩(기본값: cp949)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="결과를 JSON으로 출력합니다.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    results: list[ReplayResult] = []
    had_error = False

    for path in args.ply:
        try:
            results.append(analyze_replay(path, encoding=args.encoding))
        except (OSError, ReplayFormatError) as exc:
            had_error = True
            if args.json:
                results.append(_error_result(path, exc))
            else:
                print(f"파일: {path}", file=sys.stderr)
                print(f"오류: {exc}", file=sys.stderr)

    if args.json:
        payload: object
        if len(results) == 1:
            payload = results[0].to_dict()
        else:
            payload = [result.to_dict() for result in results]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for index, result in enumerate(results):
            if index:
                print()
            print_human(result)

    return 1 if had_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
