#!/usr/bin/env python3
"""HTTP result server for Jwar2 .ply replays."""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import importlib.util
import json
import sys
import tempfile
import threading
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = BASE_DIR / "data"
DEFAULT_ANALYZER_PATH = BASE_DIR / "jwar2_replay_result.py"

HASHES_FILENAME = "replay_hashes.json"
STATS_FILENAME = "player_stats.json"
TRC_MAGIC = b"TRC\x1a"
UPLOAD_FIELD_NAMES = {"file", "replay", "ply"}


class RequestError(Exception):
    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_analyzer(path: Path) -> Any:
    if not path.exists():
        raise FileNotFoundError(f"analyzer not found: {path}")

    module_name = "jw2_replay_result_dynamic"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load analyzer spec: {path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    if not hasattr(module, "analyze_replay"):
        raise RuntimeError(f"analyzer has no analyze_replay function: {path}")
    return module


def dataclass_to_dict(value: Any) -> dict[str, Any]:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return value
    raise TypeError(f"unsupported analyzer result type: {type(value)!r}")


def clean_nickname(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def slot_fallback(slot: Any, fallback_index: int = 0) -> str:
    if isinstance(slot, int) and slot >= 0:
        return f"slot_{slot + 1}"
    return f"player_{fallback_index + 1}"


def names_with_fallback(names: Any, slots: Any) -> list[str]:
    if not isinstance(names, list):
        return []
    slot_values = slots if isinstance(slots, list) else []

    output: list[str] = []
    for index, name in enumerate(names):
        cleaned = clean_nickname(name)
        if not cleaned:
            slot = slot_values[index] if index < len(slot_values) else None
            cleaned = slot_fallback(slot, index)
        output.append(cleaned)
    return output


def build_stat(nickname: str, wins: int = 0, losses: int = 0) -> dict[str, Any]:
    games = wins + losses
    win_rate = round((wins / games) * 100, 2) if games else 0.0
    return {
        "nickname": nickname,
        "wins": wins,
        "losses": losses,
        "games": games,
        "win_rate": win_rate,
    }


def ranking_score(player: dict[str, Any]) -> float:
    wins = int(player.get("wins", 0))
    games = int(player.get("games", 0))
    if games <= 0:
        return 0.0

    z = 1.96
    ratio = wins / games
    z2 = z * z
    denominator = 1 + z2 / games
    center = ratio + z2 / (2 * games)
    margin = z * ((ratio * (1 - ratio) + z2 / (4 * games)) / games) ** 0.5
    return (center - margin) / denominator


def player_rank_key(player: dict[str, Any]) -> tuple[float, int, int, int, str]:
    games = int(player.get("games", 0))
    wins = int(player.get("wins", 0))
    losses = int(player.get("losses", 0))
    nickname = str(player.get("nickname", "")).casefold()
    return (-ranking_score(player), -games, -wins, losses, nickname)


class ResultStore:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.hashes_path = data_dir / HASHES_FILENAME
        self.stats_path = data_dir / STATS_FILENAME
        self.lock = threading.RLock()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_json_file(self.hashes_path)
        self._ensure_json_file(self.stats_path)
        self._cleanup_replay_hashes()

    def _ensure_json_file(self, path: Path) -> None:
        if path.exists():
            return
        self._write_json(path, {})

    def _read_json(self, path: Path) -> dict[str, Any]:
        try:
            with path.open("r", encoding="utf-8") as fp:
                data = json.load(fp)
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid JSON storage file: {path}") from exc

        if not isinstance(data, dict):
            raise RuntimeError(f"JSON storage root must be an object: {path}")
        return data

    def _write_json(self, path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
            text=True,
        )
        try:
            with open(fd, "w", encoding="utf-8", newline="\n") as fp:
                json.dump(data, fp, ensure_ascii=False, indent=2, sort_keys=True)
                fp.write("\n")
            Path(temp_name).replace(path)
        except Exception:
            temp_path = Path(temp_name)
            if temp_path.exists():
                temp_path.unlink()
            raise

    def get_replay(self, replay_hash: str) -> dict[str, Any] | None:
        with self.lock:
            return self._read_json(self.hashes_path).get(replay_hash)

    def _cleanup_replay_hashes(self) -> None:
        with self.lock:
            hashes = self._read_json(self.hashes_path)
            changed = False
            for record in hashes.values():
                if not isinstance(record, dict):
                    continue
                for key in ("reason", "upload_name", "warnings"):
                    if key in record:
                        del record[key]
                        changed = True
            if changed:
                self._write_json(self.hashes_path, hashes)

    def get_player(self, nickname: str) -> dict[str, Any] | None:
        with self.lock:
            raw = self._read_json(self.stats_path).get(nickname)
            if raw is None:
                return None
            return self._normalize_stat(nickname, raw)

    def get_all_players(self) -> list[dict[str, Any]]:
        with self.lock:
            stats = self._read_json(self.stats_path)
            players = [
                self._normalize_stat(nickname, raw)
                for nickname, raw in stats.items()
            ]
        return sorted(players, key=player_rank_key)

    def _normalize_stat(self, nickname: str, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            return build_stat(nickname)
        wins = int(raw.get("wins", 0))
        losses = int(raw.get("losses", 0))
        return build_stat(clean_nickname(raw.get("nickname")) or nickname, wins, losses)

    def record_replay(
        self,
        replay_hash: str,
        byte_size: int,
        result: dict[str, Any],
    ) -> tuple[bool, dict[str, Any], list[dict[str, Any]]]:
        with self.lock:
            hashes = self._read_json(self.hashes_path)
            existing = hashes.get(replay_hash)
            if existing is not None:
                return True, existing, self.get_all_players()

            stats = self._read_json(self.stats_path)
            winner_name = clean_nickname(result.get("winner_name"))
            if not winner_name:
                winner_name = slot_fallback(result.get("winner_slot"))
            loser_names = names_with_fallback(
                result.get("loser_names"),
                result.get("loser_slots"),
            )

            counted = False
            if (
                result.get("status") == "determined"
                and winner_name
                and loser_names
            ):
                self._increment(stats, winner_name, wins=1)
                for loser_name in loser_names:
                    self._increment(stats, loser_name, losses=1)
                counted = True

            record = {
                "sha256": replay_hash,
                "byte_size": byte_size,
                "received_at": utc_now(),
                "counted": counted,
                "status": result.get("status"),
                "winner_name": winner_name if counted else result.get("winner_name"),
                "loser_names": loser_names if counted else result.get("loser_names", []),
            }

            hashes[replay_hash] = record
            self._write_json(self.stats_path, stats)
            self._write_json(self.hashes_path, hashes)
            return False, record, self.get_all_players()

    def _increment(
        self,
        stats: dict[str, Any],
        nickname: str,
        wins: int = 0,
        losses: int = 0,
    ) -> None:
        current = self._normalize_stat(nickname, stats.get(nickname, {}))
        updated = build_stat(
            nickname=nickname,
            wins=current["wins"] + wins,
            losses=current["losses"] + losses,
        )
        stats[nickname] = updated


def content_type_name(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().lower()


def extract_replay_upload(
    body: bytes,
    content_type: str,
) -> bytes:
    if not body:
        raise RequestError(HTTPStatus.BAD_REQUEST, "empty request body")

    kind = content_type_name(content_type)
    if kind == "multipart/form-data":
        return extract_multipart_upload(body, content_type)
    if kind == "application/json":
        return extract_json_upload(body)
    if kind == "application/x-www-form-urlencoded":
        raise RequestError(
            HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
            "send .ply as multipart/form-data, raw bytes, or JSON base64",
        )

    return body


def extract_multipart_upload(
    body: bytes,
    content_type: str,
) -> bytes:
    header = (
        b"Content-Type: "
        + content_type.encode("ascii", errors="replace")
        + b"\r\nMIME-Version: 1.0\r\n\r\n"
    )
    message = BytesParser(policy=policy.default).parsebytes(header + body)
    if not message.is_multipart():
        raise RequestError(HTTPStatus.BAD_REQUEST, "invalid multipart body")

    candidates: list[tuple[bytes, bool]] = []
    for part in message.walk():
        if part.is_multipart():
            continue

        field_name = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        payload = part.get_payload(decode=True)
        if payload is None:
            content = part.get_content()
            if isinstance(content, str):
                payload = content.encode("utf-8")
            else:
                continue

        preferred = bool(filename) or field_name in UPLOAD_FIELD_NAMES
        candidates.append((payload, preferred))

    for payload, preferred in candidates:
        if preferred and payload:
            return payload
    for payload, _preferred in candidates:
        if payload and payload.startswith(TRC_MAGIC):
            return payload

    raise RequestError(HTTPStatus.BAD_REQUEST, "multipart .ply file field not found")


def extract_json_upload(body: bytes) -> bytes:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RequestError(HTTPStatus.BAD_REQUEST, "invalid JSON body") from exc

    if not isinstance(payload, dict):
        raise RequestError(HTTPStatus.BAD_REQUEST, "JSON body must be an object")

    encoded = None
    for key in ("ply_base64", "replay_base64", "file_base64"):
        if key in payload:
            encoded = payload[key]
            break
    if not isinstance(encoded, str):
        raise RequestError(
            HTTPStatus.BAD_REQUEST,
            "JSON body needs ply_base64, replay_base64, or file_base64",
        )

    try:
        replay_bytes = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise RequestError(HTTPStatus.BAD_REQUEST, "invalid base64 replay") from exc

    return replay_bytes


def format_win_rate(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    if number.is_integer():
        return f"{int(number)}%"
    return f"{number:.2f}%"


def players_table_html(players: list[dict[str, Any]]) -> str:
    rows = []
    for player in players:
        nickname = html.escape(str(player.get("nickname", "")))
        wins = html.escape(str(player.get("wins", 0)))
        losses = html.escape(str(player.get("losses", 0)))
        games = html.escape(str(player.get("games", 0)))
        win_rate = html.escape(format_win_rate(player.get("win_rate", 0.0)))
        rows.append(
            "<tr>"
            f"<td>{nickname}</td>"
            f"<td class=\"num\">{wins}</td>"
            f"<td class=\"num\">{losses}</td>"
            f"<td class=\"num\">{games}</td>"
            f"<td class=\"num\">{win_rate}</td>"
            "</tr>"
        )

    if not rows:
        rows.append(
            "<tr><td class=\"empty\" colspan=\"5\">저장된 플레이어가 없습니다.</td></tr>"
        )

    return """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <title>JW2 Results</title>
  <style>
    body {
      margin: 32px;
      color: #1f2933;
      background: #f6f8fa;
      font-family: "Segoe UI", "Malgun Gothic", Arial, sans-serif;
    }
    main {
      max-width: 860px;
      margin: 0 auto;
    }
    h1 {
      margin: 0 0 18px;
      font-size: 24px;
      font-weight: 700;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      background: #ffffff;
      border: 1px solid #d8dee4;
    }
    th,
    td {
      padding: 11px 14px;
      border-bottom: 1px solid #d8dee4;
      text-align: left;
      font-size: 14px;
    }
    th {
      background: #eef2f6;
      font-weight: 700;
    }
    tr:last-child td {
      border-bottom: 0;
    }
    .num {
      text-align: right;
      font-variant-numeric: tabular-nums;
    }
    .empty {
      color: #667085;
      text-align: center;
    }
  </style>
</head>
<body>
  <main>
    <h1>JW2 전체 전적</h1>
    <table>
      <thead>
        <tr>
          <th>닉네임</th>
          <th class="num">승</th>
          <th class="num">패</th>
          <th class="num">대전</th>
          <th class="num">승률</th>
        </tr>
      </thead>
      <tbody>
        """ + "\n        ".join(rows) + """
      </tbody>
    </table>
  </main>
</body>
</html>
"""


class ReplayResultHTTPServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        store: ResultStore,
        analyzer: Any,
        replay_encoding: str,
        max_body_bytes: int,
    ):
        super().__init__(server_address, ReplayResultHandler)
        self.store = store
        self.analyzer = analyzer
        self.replay_encoding = replay_encoding
        self.max_body_bytes = max_body_bytes


class ReplayResultHandler(BaseHTTPRequestHandler):
    server: ReplayResultHTTPServer

    def do_GET(self) -> None:
        try:
            self._handle_get()
        except RequestError as exc:
            self._send_json(exc.status, {"error": exc.message})
        except Exception as exc:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "internal server error", "detail": str(exc)},
            )

    def do_POST(self) -> None:
        try:
            self._handle_post()
        except RequestError as exc:
            self._send_json(exc.status, {"error": exc.message})
        except Exception as exc:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "internal server error", "detail": str(exc)},
            )

    def _handle_get(self) -> None:
        parsed = urlparse(self.path)
        parts = [part for part in parsed.path.split("/") if part]

        if not parts:
            self._send_all_players(parsed)
            return

        if len(parts) != 1:
            raise RequestError(HTTPStatus.NOT_FOUND, "not found")

        segment = unquote(parts[0])
        if segment in ("all", ":all"):
            self._send_all_players(parsed)
            return

        nickname = segment[1:] if segment.startswith(":") else segment
        nickname = nickname.strip()
        if not nickname:
            raise RequestError(HTTPStatus.BAD_REQUEST, "nickname is empty")

        player = self.server.store.get_player(nickname)
        if player is None:
            player = build_stat(nickname)
            found = False
        else:
            found = True
        self._send_json(HTTPStatus.OK, {"player": player, "found": found})

    def _send_all_players(self, parsed: Any) -> None:
        players = self.server.store.get_all_players()
        if self._wants_json(parsed):
            self._send_json(
                HTTPStatus.OK,
                {"players": players, "count": len(players)},
            )
        else:
            self._send_html(HTTPStatus.OK, players_table_html(players))

    def _wants_json(self, parsed: Any) -> bool:
        query = parse_qs(parsed.query)
        if query.get("format", [""])[0].casefold() == "json":
            return True

        accept = self.headers.get("Accept", "")
        return "application/json" in accept and "text/html" not in accept

    def _handle_post(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.rstrip("/") != "/replay":
            raise RequestError(HTTPStatus.NOT_FOUND, "not found")

        body = self._read_body()
        upload_bytes = extract_replay_upload(
            body,
            self.headers.get("Content-Type", "application/octet-stream"),
        )
        if not upload_bytes:
            raise RequestError(HTTPStatus.BAD_REQUEST, "empty replay upload")

        replay_hash = hashlib.sha256(upload_bytes).hexdigest()
        duplicate = self.server.store.get_replay(replay_hash)
        if duplicate is not None:
            self._send_json(
                HTTPStatus.OK,
                {
                    "duplicate": True,
                    "sha256": replay_hash,
                    "record": duplicate,
                    "players": self.server.store.get_all_players(),
                },
            )
            return

        result = self._analyze_upload(upload_bytes)
        result_data = dataclass_to_dict(result)
        duplicate_after_analyze, record, players = self.server.store.record_replay(
            replay_hash=replay_hash,
            byte_size=len(upload_bytes),
            result=result_data,
        )

        self._send_json(
            HTTPStatus.OK,
            {
                "duplicate": duplicate_after_analyze,
                "sha256": replay_hash,
                "counted": record.get("counted", False),
                "record": record,
                "result": result_data,
                "players": players,
            },
        )

    def _read_body(self) -> bytes:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise RequestError(HTTPStatus.LENGTH_REQUIRED, "Content-Length required")

        try:
            length = int(raw_length)
        except ValueError as exc:
            raise RequestError(HTTPStatus.BAD_REQUEST, "invalid Content-Length") from exc

        if length < 0:
            raise RequestError(HTTPStatus.BAD_REQUEST, "invalid Content-Length")
        if length > self.server.max_body_bytes:
            raise RequestError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "upload too large")

        return self.rfile.read(length)

    def _analyze_upload(self, replay_bytes: bytes) -> Any:
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                suffix=".ply",
                prefix="upload_",
                dir=str(self.server.store.data_dir),
                delete=False,
            ) as fp:
                fp.write(replay_bytes)
                temp_path = Path(fp.name)

            try:
                return self.server.analyzer.analyze_replay(
                    temp_path,
                    encoding=self.server.replay_encoding,
                )
            except (
                OSError,
                getattr(self.server.analyzer, "ReplayFormatError", ValueError),
            ) as exc:
                raise RequestError(
                    HTTPStatus.BAD_REQUEST,
                    f"replay analysis failed: {exc}",
                ) from exc
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status: HTTPStatus, body_text: str) -> None:
        body = body_text.encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write(
            "%s - - [%s] %s\n"
            % (self.client_address[0], self.log_date_time_string(), format % args)
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Serve win/loss stats from uploaded Jwar2 .ply replays."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--encoding", default="cp949")
    parser.add_argument(
        "--analyzer-path",
        type=Path,
        default=DEFAULT_ANALYZER_PATH,
        help="Path to jwar2_replay_result.py",
    )
    parser.add_argument(
        "--max-upload-mb",
        type=int,
        default=256,
        help="Maximum replay upload size.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    analyzer = load_analyzer(args.analyzer_path.resolve())
    store = ResultStore(args.data_dir.resolve())
    max_body_bytes = max(1, args.max_upload_mb) * 1024 * 1024

    server = ReplayResultHTTPServer(
        (args.host, args.port),
        store=store,
        analyzer=analyzer,
        replay_encoding=args.encoding,
        max_body_bytes=max_body_bytes,
    )

    host, port = server.server_address
    print(f"jw2_result_server listening on http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
