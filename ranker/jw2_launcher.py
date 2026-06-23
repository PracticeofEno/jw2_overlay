#!/usr/bin/env python
"""GUI launcher for Ranker, cnc-ddraw settings, and jw2_overlay.py."""

from __future__ import annotations

import configparser
import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox, ttk
import tkinter as tk

try:
    import psutil
except Exception:  # pragma: no cover - the overlay already requires psutil.
    psutil = None


def runtime_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


HERE = runtime_dir()
ROOT = HERE.parent
DEFAULT_GAME_DIR = ROOT / "_ranker_unit_counter_unused" / "ranker"
OVERLAY_SCRIPT = HERE / "jw2_overlay.py"
CONFIG_PATH = HERE / "jw2_launcher_config.json"
OVERLAY_LOG_PATH = HERE / "jw2_overlay_launcher.log"
OVERLAY_SUBPROCESS_FLAG = "--jw2-overlay-subprocess"
DDRAW_SECTION = "ddraw"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

PROCESS_NAMES = ("ranker.exe", "ranker800.exe", "rank1024.exe")
RESOLUTION_PRESETS = (
    "800x600",
    "1024x768",
    "1280x960",
    "1600x1200",
    "1920x1080",
    "2560x1440",
)

DEFAULTS = {
    "game_dir": str(DEFAULT_GAME_DIR),
    "exe_name": "ranker.exe",
    "overlay_bg_color": "#080A0D",
    "overlay_panel_bg_color": "#10151C",
    "overlay_alpha": 0.94,
    "width": 1280,
    "height": 960,
    "windowed": True,
    "fullscreen": True,
    "border": True,
    "resizing": True,
    "boxing": False,
    "vsync": False,
}


def normalize_path(path: str | Path | None) -> Path | None:
    if not path:
        return None
    try:
        return Path(path).resolve()
    except OSError:
        return None


def parse_bool(value: str | bool | None, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "yes", "true", "on"}


def bool_text(value: bool) -> str:
    return "true" if value else "false"


def is_hex_color(value: str) -> bool:
    return bool(re.fullmatch(r"#[0-9A-Fa-f]{6}", value.strip()))


def read_json_config() -> dict[str, object]:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def read_ddraw_values(game_dir: Path) -> dict[str, object]:
    values = DEFAULTS.copy()
    parser = configparser.ConfigParser()
    parser.optionxform = str
    parser.read(game_dir / "ddraw.ini", encoding="utf-8")

    if not parser.has_section(DDRAW_SECTION):
        return values

    section = parser[DDRAW_SECTION]
    for key in ("width", "height"):
        try:
            values[key] = int(section.get(key, values[key]))
        except (TypeError, ValueError):
            pass

    for key in ("windowed", "fullscreen", "border", "resizing", "boxing", "vsync"):
        values[key] = parse_bool(section.get(key), bool(values[key]))

    return values


def write_ddraw_values(game_dir: Path, values: dict[str, object]) -> None:
    path = game_dir / "ddraw.ini"
    parser = configparser.ConfigParser()
    parser.optionxform = str
    parser.read(path, encoding="utf-8")
    if not parser.has_section(DDRAW_SECTION):
        parser.add_section(DDRAW_SECTION)

    section = parser[DDRAW_SECTION]
    section["width"] = str(values["width"])
    section["height"] = str(values["height"])
    section["windowed"] = bool_text(bool(values["windowed"]))
    section["fullscreen"] = bool_text(bool(values["fullscreen"]))
    section["border"] = bool_text(bool(values["border"]))
    section["resizing"] = bool_text(bool(values["resizing"]))
    section["boxing"] = bool_text(bool(values["boxing"]))
    section["vsync"] = bool_text(bool(values["vsync"]))

    with path.open("w", encoding="utf-8", newline="\n") as fp:
        parser.write(fp)


class LauncherApp:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("JW2 Ranker Launcher")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        stored_config = read_json_config()
        game_dir = normalize_path(str(stored_config.get("game_dir", DEFAULT_GAME_DIR))) or DEFAULT_GAME_DIR
        ddraw_values = DEFAULTS.copy()
        ddraw_values.update(read_ddraw_values(game_dir))
        ddraw_values.update(stored_config)

        self.game_process: subprocess.Popen[bytes] | None = None
        self.overlay_process: subprocess.Popen[bytes] | None = None
        self.overlay_log_handle = None
        self.detected_game_pid: int | None = None
        self.stop_requested = False
        self.monitor_thread: threading.Thread | None = None

        self.game_dir_var = tk.StringVar(value=str(game_dir))
        self.exe_var = tk.StringVar(value=str(ddraw_values.get("exe_name", "ranker.exe")))
        self.overlay_bg_var = tk.StringVar(value=str(ddraw_values["overlay_bg_color"]))
        self.overlay_panel_bg_var = tk.StringVar(value=str(ddraw_values["overlay_panel_bg_color"]))
        self.overlay_alpha_percent_var = tk.DoubleVar(
            value=max(5, min(100, round(float(ddraw_values["overlay_alpha"]) * 100)))
        )
        self.width_var = tk.StringVar(value=str(ddraw_values["width"]))
        self.height_var = tk.StringVar(value=str(ddraw_values["height"]))
        self.preset_var = tk.StringVar(value=self._preset_text())
        self.windowed_var = tk.BooleanVar(value=bool(ddraw_values["windowed"]))
        self.fullscreen_var = tk.BooleanVar(value=bool(ddraw_values["fullscreen"]))
        self.border_var = tk.BooleanVar(value=bool(ddraw_values["border"]))
        self.resizing_var = tk.BooleanVar(value=bool(ddraw_values["resizing"]))
        self.boxing_var = tk.BooleanVar(value=bool(ddraw_values["boxing"]))
        self.vsync_var = tk.BooleanVar(value=bool(ddraw_values["vsync"]))
        self.status_var = tk.StringVar(value="대기 중")

        self._build_ui()
        self._refresh_exe_choices()
        self._update_color_previews()
        self._update_alpha_label()

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=12)
        outer.grid(row=0, column=0, sticky="nsew")

        path_frame = ttk.LabelFrame(outer, text="게임 경로", padding=10)
        path_frame.grid(row=0, column=0, sticky="ew")
        path_frame.columnconfigure(1, weight=1)
        ttk.Label(path_frame, text="폴더").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(path_frame, width=66, textvariable=self.game_dir_var).grid(row=0, column=1, sticky="ew")
        ttk.Button(path_frame, text="찾기", command=self.browse_game_dir).grid(row=0, column=2, padx=(8, 0))
        ttk.Label(path_frame, text="실행 파일").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(8, 0))
        self.exe_combo = ttk.Combobox(path_frame, width=24, textvariable=self.exe_var, values=PROCESS_NAMES)
        self.exe_combo.grid(row=1, column=1, sticky="w", pady=(8, 0))

        overlay_frame = ttk.LabelFrame(outer, text="오버레이", padding=10)
        overlay_frame.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        overlay_frame.columnconfigure(1, weight=1)
        ttk.Label(overlay_frame, text="배경색").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(overlay_frame, width=12, textvariable=self.overlay_bg_var).grid(row=0, column=1, sticky="w")
        self.bg_preview = tk.Label(overlay_frame, width=4, relief="groove")
        self.bg_preview.grid(row=0, column=2, padx=(8, 0))
        ttk.Button(overlay_frame, text="선택", command=lambda: self.pick_color(self.overlay_bg_var)).grid(row=0, column=3, padx=(8, 0))

        ttk.Label(overlay_frame, text="패널색").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(8, 0))
        ttk.Entry(overlay_frame, width=12, textvariable=self.overlay_panel_bg_var).grid(row=1, column=1, sticky="w", pady=(8, 0))
        self.panel_preview = tk.Label(overlay_frame, width=4, relief="groove")
        self.panel_preview.grid(row=1, column=2, padx=(8, 0), pady=(8, 0))
        ttk.Button(overlay_frame, text="선택", command=lambda: self.pick_color(self.overlay_panel_bg_var)).grid(row=1, column=3, padx=(8, 0), pady=(8, 0))

        ttk.Label(overlay_frame, text="투명도").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=(8, 0))
        ttk.Scale(
            overlay_frame,
            from_=5,
            to=100,
            variable=self.overlay_alpha_percent_var,
            command=lambda _value: self._update_alpha_label(),
        ).grid(row=2, column=1, columnspan=2, sticky="ew", pady=(8, 0))
        self.alpha_label = ttk.Label(overlay_frame, width=5)
        self.alpha_label.grid(row=2, column=3, sticky="e", pady=(8, 0))

        ddraw_frame = ttk.LabelFrame(outer, text="ddraw 해상도", padding=10)
        ddraw_frame.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        ttk.Label(ddraw_frame, text="프리셋").grid(row=0, column=0, sticky="w", padx=(0, 8))
        preset_combo = ttk.Combobox(
            ddraw_frame,
            width=16,
            textvariable=self.preset_var,
            values=RESOLUTION_PRESETS,
            state="readonly",
        )
        preset_combo.grid(row=0, column=1, sticky="w")
        preset_combo.bind("<<ComboboxSelected>>", self.apply_resolution_preset)

        ttk.Label(ddraw_frame, text="가로").grid(row=0, column=2, sticky="w", padx=(16, 8))
        ttk.Entry(ddraw_frame, width=8, textvariable=self.width_var).grid(row=0, column=3, sticky="w")
        ttk.Label(ddraw_frame, text="세로").grid(row=0, column=4, sticky="w", padx=(16, 8))
        ttk.Entry(ddraw_frame, width=8, textvariable=self.height_var).grid(row=0, column=5, sticky="w")

        checks = ttk.Frame(ddraw_frame)
        checks.grid(row=1, column=0, columnspan=6, sticky="w", pady=(10, 0))
        ttk.Checkbutton(checks, text="windowed", variable=self.windowed_var).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(checks, text="fullscreen", variable=self.fullscreen_var).grid(row=0, column=1, sticky="w", padx=(14, 0))
        ttk.Checkbutton(checks, text="border", variable=self.border_var).grid(row=0, column=2, sticky="w", padx=(14, 0))
        ttk.Checkbutton(checks, text="resizing", variable=self.resizing_var).grid(row=0, column=3, sticky="w", padx=(14, 0))
        ttk.Checkbutton(checks, text="boxing", variable=self.boxing_var).grid(row=0, column=4, sticky="w", padx=(14, 0))
        ttk.Checkbutton(checks, text="vsync", variable=self.vsync_var).grid(row=0, column=5, sticky="w", padx=(14, 0))

        control_frame = ttk.Frame(outer)
        control_frame.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        self.start_button = ttk.Button(control_frame, text="실행", command=self.start)
        self.start_button.grid(row=0, column=0)
        self.stop_button = ttk.Button(control_frame, text="종료", command=self.stop, state="disabled")
        self.stop_button.grid(row=0, column=1, padx=(8, 0))
        ttk.Label(control_frame, textvariable=self.status_var).grid(row=0, column=2, sticky="w", padx=(16, 0))

        self.log_text = tk.Text(outer, width=82, height=8, state="disabled", wrap="word")
        self.log_text.grid(row=4, column=0, sticky="ew", pady=(10, 0))

    def _preset_text(self) -> str:
        return f"{self.width_var.get()}x{self.height_var.get()}" if hasattr(self, "width_var") else "1280x960"

    def _refresh_exe_choices(self) -> None:
        game_dir = Path(self.game_dir_var.get())
        choices = [name for name in PROCESS_NAMES if (game_dir / name).exists()]
        if not choices:
            choices = list(PROCESS_NAMES)
        self.exe_combo.configure(values=choices)
        if self.exe_var.get() not in choices:
            self.exe_var.set(choices[0])

    def _update_color_previews(self) -> None:
        for variable, preview in (
            (self.overlay_bg_var, self.bg_preview),
            (self.overlay_panel_bg_var, self.panel_preview),
        ):
            color = variable.get().strip()
            preview.configure(bg=color if is_hex_color(color) else "#FFFFFF")

    def _update_alpha_label(self) -> None:
        self.alpha_label.configure(text=f"{round(self.overlay_alpha_percent_var.get()):d}%")

    def pick_color(self, variable: tk.StringVar) -> None:
        color = colorchooser.askcolor(color=variable.get(), parent=self.root)[1]
        if color:
            variable.set(color.upper())
            self._update_color_previews()

    def browse_game_dir(self) -> None:
        selected = filedialog.askdirectory(
            title="ranker.exe 폴더 선택",
            initialdir=self.game_dir_var.get() or str(DEFAULT_GAME_DIR),
            parent=self.root,
        )
        if not selected:
            return
        self.game_dir_var.set(selected)
        ddraw_values = read_ddraw_values(Path(selected))
        self.width_var.set(str(ddraw_values["width"]))
        self.height_var.set(str(ddraw_values["height"]))
        self.windowed_var.set(bool(ddraw_values["windowed"]))
        self.fullscreen_var.set(bool(ddraw_values["fullscreen"]))
        self.border_var.set(bool(ddraw_values["border"]))
        self.resizing_var.set(bool(ddraw_values["resizing"]))
        self.boxing_var.set(bool(ddraw_values["boxing"]))
        self.vsync_var.set(bool(ddraw_values["vsync"]))
        self.preset_var.set(f"{self.width_var.get()}x{self.height_var.get()}")
        self._refresh_exe_choices()

    def apply_resolution_preset(self, _event: tk.Event | None = None) -> None:
        preset = self.preset_var.get()
        if "x" not in preset:
            return
        width, height = preset.split("x", 1)
        self.width_var.set(width)
        self.height_var.set(height)

    def collect_values(self) -> dict[str, object]:
        game_dir = normalize_path(self.game_dir_var.get())
        if game_dir is None or not game_dir.exists():
            raise ValueError("게임 폴더가 존재하지 않습니다.")

        exe_name = self.exe_var.get().strip()
        exe_path = game_dir / exe_name
        if not exe_path.exists():
            raise ValueError(f"{exe_name} 파일을 찾을 수 없습니다.")

        if not getattr(sys, "frozen", False) and not OVERLAY_SCRIPT.exists():
            raise ValueError(f"오버레이 파일을 찾을 수 없습니다: {OVERLAY_SCRIPT}")

        bg = self.overlay_bg_var.get().strip()
        panel_bg = self.overlay_panel_bg_var.get().strip()
        if not is_hex_color(bg):
            raise ValueError("오버레이 배경색은 #RRGGBB 형식이어야 합니다.")
        if not is_hex_color(panel_bg):
            raise ValueError("오버레이 패널색은 #RRGGBB 형식이어야 합니다.")

        try:
            width = int(self.width_var.get())
            height = int(self.height_var.get())
        except ValueError as exc:
            raise ValueError("해상도는 숫자로 입력해야 합니다.") from exc

        if width < 320 or height < 240:
            raise ValueError("해상도는 최소 320x240 이상이어야 합니다.")

        return {
            "game_dir": str(game_dir),
            "exe_name": exe_name,
            "overlay_bg_color": bg.upper(),
            "overlay_panel_bg_color": panel_bg.upper(),
            "overlay_alpha": round(self.overlay_alpha_percent_var.get()) / 100.0,
            "width": width,
            "height": height,
            "windowed": self.windowed_var.get(),
            "fullscreen": self.fullscreen_var.get(),
            "border": self.border_var.get(),
            "resizing": self.resizing_var.get(),
            "boxing": self.boxing_var.get(),
            "vsync": self.vsync_var.get(),
        }

    def save_launcher_config(self, values: dict[str, object]) -> None:
        CONFIG_PATH.write_text(
            json.dumps(values, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def start(self) -> None:
        if self.game_process and self.game_process.poll() is None:
            messagebox.showinfo("실행 중", "이미 실행 중입니다.", parent=self.root)
            return

        try:
            values = self.collect_values()
            game_dir = Path(str(values["game_dir"]))
            write_ddraw_values(game_dir, values)
            self.save_launcher_config(values)
        except Exception as exc:
            messagebox.showerror("실행 실패", str(exc), parent=self.root)
            return

        self.stop_requested = False
        self.detected_game_pid = None
        self._set_running_state(True)
        self._log("ddraw.ini 저장 완료")

        exe_path = Path(str(values["game_dir"])) / str(values["exe_name"])
        try:
            self.game_process = subprocess.Popen(
                [str(exe_path)],
                cwd=str(exe_path.parent),
            )
        except Exception as exc:
            self._set_running_state(False)
            messagebox.showerror("실행 실패", str(exc), parent=self.root)
            return

        self._log(f"게임 실행: {exe_path.name}")
        self.monitor_thread = threading.Thread(
            target=self.monitor_processes,
            args=(values,),
            daemon=True,
        )
        self.monitor_thread.start()

    def monitor_processes(self, values: dict[str, object]) -> None:
        game_dir = Path(str(values["game_dir"]))
        exe_name = str(values["exe_name"])
        deadline = time.monotonic() + 20.0

        while not self.stop_requested:
            proc = self.find_ranker_process(game_dir, exe_name)
            if proc is not None:
                self.detected_game_pid = proc.pid
                self._ui_log(f"ranker.exe 감지: PID {proc.pid}")
                self._start_overlay(values)
                break

            if psutil is None and self.game_process and self.game_process.poll() is None:
                self._ui_log("psutil을 사용할 수 없어 실행한 프로세스 기준으로 오버레이를 시작합니다.")
                self._start_overlay(values)
                break

            if self.game_process and self.game_process.poll() is not None:
                self._ui_log("게임 프로세스가 오버레이 시작 전에 종료되었습니다.")
                break

            if time.monotonic() >= deadline and self.game_process and self.game_process.poll() is None:
                self._ui_log("프로세스 감지가 지연되어 오버레이를 먼저 시작합니다.")
                self._start_overlay(values)
                break

            time.sleep(0.5)

        while not self.stop_requested and self.game_is_running(game_dir, exe_name):
            time.sleep(0.5)

        self._ui_log("게임 종료 감지")
        self._stop_overlay()
        self.root.after(0, lambda: self._set_running_state(False))

    def find_ranker_process(self, game_dir: Path, exe_name: str):
        if psutil is None:
            return None

        target = normalize_path(game_dir / exe_name)
        for proc in psutil.process_iter(["pid", "name", "exe"]):
            try:
                name = proc.info.get("name") or ""
                if name.lower() not in PROCESS_NAMES:
                    continue
                exe = normalize_path(proc.info.get("exe"))
                if target and exe == target:
                    return proc
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return None

    def game_is_running(self, game_dir: Path, exe_name: str) -> bool:
        if self.game_process and self.game_process.poll() is None:
            return True

        if self.detected_game_pid is None or psutil is None:
            return False

        try:
            proc = psutil.Process(self.detected_game_pid)
            if not proc.is_running():
                return False
            exe = normalize_path(proc.exe())
            target = normalize_path(game_dir / exe_name)
            return exe == target
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            return False

    def overlay_command(self, values: dict[str, object]) -> list[str]:
        args = [
            "--game-dir",
            str(values["game_dir"]),
            "--overlay-bg-color",
            str(values["overlay_bg_color"]),
            "--overlay-panel-bg-color",
            str(values["overlay_panel_bg_color"]),
            "--overlay-alpha",
            f"{float(values['overlay_alpha']):.2f}",
        ]
        if getattr(sys, "frozen", False):
            return [sys.executable, OVERLAY_SUBPROCESS_FLAG, *args]
        return [sys.executable, str(OVERLAY_SCRIPT), *args]

    def _start_overlay(self, values: dict[str, object]) -> None:
        if self.overlay_process and self.overlay_process.poll() is None:
            return

        try:
            self.overlay_log_handle = OVERLAY_LOG_PATH.open("a", encoding="utf-8")
            self.overlay_log_handle.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] launcher start\n")
            self.overlay_log_handle.flush()
            self.overlay_process = subprocess.Popen(
                self.overlay_command(values),
                cwd=str(HERE),
                stdout=self.overlay_log_handle,
                stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW,
            )
            self._ui_log("오버레이 시작")
        except Exception as exc:
            self._ui_log(f"오버레이 시작 실패: {exc}")
            self._close_overlay_log()

    def _stop_overlay(self) -> None:
        if self.overlay_process and self.overlay_process.poll() is None:
            self._ui_log("오버레이 종료")
            self.overlay_process.terminate()
            try:
                self.overlay_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.overlay_process.kill()
                self.overlay_process.wait(timeout=3)
        self.overlay_process = None
        self._close_overlay_log()

    def _close_overlay_log(self) -> None:
        if self.overlay_log_handle:
            try:
                self.overlay_log_handle.close()
            except OSError:
                pass
            self.overlay_log_handle = None

    def stop(self) -> None:
        self.stop_requested = True
        self._stop_overlay()

        if self.game_process and self.game_process.poll() is None:
            self._log("게임 종료 요청")
            self.game_process.terminate()

        self._set_running_state(False)

    def close(self) -> None:
        if self.game_process and self.game_process.poll() is None:
            if not messagebox.askyesno("종료", "게임과 오버레이를 종료할까요?", parent=self.root):
                return
        self.stop()
        self.root.destroy()

    def _set_running_state(self, running: bool) -> None:
        self.start_button.configure(state="disabled" if running else "normal")
        self.stop_button.configure(state="normal" if running else "disabled")
        self.status_var.set("실행 중" if running else "대기 중")

    def _ui_log(self, message: str) -> None:
        self.root.after(0, lambda: self._log(message))

    def _log(self, message: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {message}\n"
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    app = LauncherApp()
    app.run()
    return 0


def entrypoint() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == OVERLAY_SUBPROCESS_FLAG:
        import jw2_overlay

        return jw2_overlay.main(sys.argv[2:])
    return main()


if __name__ == "__main__":
    raise SystemExit(entrypoint())
