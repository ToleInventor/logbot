import os
import sys
import re
import time
import json
import random
import threading
import logging
import subprocess
import queue
import shutil
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, simpledialog, filedialog

try:
    import pyautogui
    import psutil
    from pynput import mouse as pynput_mouse, keyboard as pynput_keyboard
    from pywinauto import Desktop
    from pywinauto.findwindows import ElementNotFoundError
except ImportError as e:
    sys.stderr.write(f"Missing dependency: {e}\n")
    sys.stderr.write("Run: pip install pyautogui pynput pywinauto pygetwindow psutil flask\n")
    raise

try:
    from flask import Flask, send_from_directory, abort
except ImportError:
    sys.stderr.write("Missing dependency: flask\n")
    sys.stderr.write("Run: pip install flask\n")
    raise

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.0


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE = _base_dir()
PROJECT_ROOT = Path.home() / "Desktop" / "logbot_project"
LOG_FILE = BASE / "webdevbot.log"

ALLOWED_EXTS = {".html", ".htm", ".css", ".js"}

NOTEPADPP_CANDIDATES = [
    r"C:\Program Files\Notepad++\notepad++.exe",
    r"C:\Program Files (x86)\Notepad++\notepad++.exe",
    r"C:\Program Files\Notepad++\notepad++",
]


@dataclass
class Config:
    idle_threshold: float = 1.0
    input_poll_interval: float = 0.03
    startup_grace: float = 0.6

    typing_delay_min: float = 0.035
    typing_delay_max: float = 0.115
    typing_delay_burst_chance: float = 0.22
    typing_delay_burst_min: float = 0.005
    typing_delay_burst_max: float = 0.030
    typing_hesitation_chance: float = 0.028
    typing_hesitation_min: float = 0.35
    typing_hesitation_max: float = 1.25
    typing_space_pause_min: float = 0.02
    typing_space_pause_max: float = 0.12
    typing_punct_pause_min: float = 0.06
    typing_punct_pause_max: float = 0.28
    typing_newline_pause_min: float = 0.12
    typing_newline_pause_max: float = 0.55
    typing_indent_pause_min: float = 0.03
    typing_indent_pause_max: float = 0.10

    typing_break_chance_per_line: float = 0.18
    typing_break_chance_per_chunk: float = 0.06
    typing_break_chunk_size_min: int = 40
    typing_break_chunk_size_max: int = 140
    typing_break_duration_min: float = 2.0
    typing_break_duration_max: float = 12.0
    typing_break_hover_min_points: int = 2
    typing_break_hover_max_points: int = 5
    typing_break_return_settle: float = 0.25

    save_every_chars_html: int = 40
    save_every_chars_other: int = 120
    save_every_seconds: float = 6.0

    np_launch_timeout: float = 15.0
    np_close_timeout: float = 6.0
    np_poll_interval: float = 0.2

    hover_min_points: int = 2
    hover_max_points: int = 5
    hover_dwell_min: float = 0.6
    hover_dwell_max: float = 3.5
    hover_idle_min: float = 4.0
    hover_idle_max: float = 16.0
    hover_move_duration_min: float = 0.5
    hover_move_duration_max: float = 1.8
    hover_margin: int = 80

    flask_host: str = "127.0.0.1"
    flask_port: int = 5500

    log_level: int = logging.INFO


CFG = Config()


class UiLogQueue(logging.Handler):
    def __init__(self, q: "queue.Queue[str]"):
        super().__init__()
        self.q = q

    def emit(self, record):
        try:
            self.q.put_nowait(self.format(record))
        except Exception:
            pass


class Logger:
    _lock = threading.Lock()
    _logger: Optional[logging.Logger] = None

    @classmethod
    def get(cls) -> logging.Logger:
        if cls._logger is not None:
            return cls._logger
        with cls._lock:
            if cls._logger is not None:
                return cls._logger
            BASE.mkdir(parents=True, exist_ok=True)
            lg = logging.getLogger("webdevbot")
            lg.setLevel(CFG.log_level)
            fmt = logging.Formatter(
                "%(asctime)s [%(levelname)s] %(threadName)s %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
            fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
            fh.setFormatter(fmt)
            lg.addHandler(fh)
            if sys.stdout is not None:
                ch = logging.StreamHandler(sys.stdout)
                ch.setFormatter(fmt)
                lg.addHandler(ch)
            lg.propagate = False
            cls._logger = lg
            return lg


log = Logger.get()


class _InputShield:
    def __init__(self, ev: threading.Event):
        self._ev = ev

    def __enter__(self):
        self._ev.set()
        return self

    def __exit__(self, *_):
        self._ev.clear()
        return False


class UserActivityMonitor:
    def __init__(self, idle_threshold: float = CFG.idle_threshold):
        self.idle_threshold = idle_threshold
        self.last_input_time = 0.0
        self.user_in_control = False
        self._armed = False
        self._shield = threading.Event()
        self._lock = threading.Lock()
        self._listeners: list = []

    def input_shield(self):
        return _InputShield(self._shield)

    def arm(self) -> None:
        with self._lock:
            self._armed = True
            self.last_input_time = 0.0
            self.user_in_control = False

    def _on_user_input(self, *_a, **_k) -> None:
        if not self._armed:
            return
        if self._shield.is_set():
            return
        with self._lock:
            self.last_input_time = time.time()
            if not self.user_in_control:
                self.user_in_control = True
                log.info("human input detected - all activity halted")

    def start(self) -> None:
        kb = pynput_keyboard.Listener(
            on_press=self._on_user_input,
            on_release=self._on_user_input,
        )
        ms = pynput_mouse.Listener(
            on_move=self._on_user_input,
            on_click=self._on_user_input,
            on_scroll=self._on_user_input,
        )
        kb.daemon = True
        ms.daemon = True
        kb.start()
        ms.start()
        self._listeners = [kb, ms]

    def stop(self) -> None:
        for lst in self._listeners:
            try:
                lst.stop()
            except Exception:
                pass

    def user_is_active(self) -> bool:
        with self._lock:
            last = self.last_input_time
        if last == 0.0:
            return False
        return (time.time() - last) < self.idle_threshold

    def wait_until_user_idle(self, end_time: float) -> bool:
        was_active = False
        while self.user_is_active():
            if time.time() >= end_time:
                return False
            if not was_active:
                was_active = True
                log.info("waiting for human to be idle...")
            time.sleep(CFG.input_poll_interval)
        if was_active:
            log.info("human idle - resuming")
        return True


def sleep_responsive(seconds: float, end_time: float, monitor: UserActivityMonitor) -> bool:
    slept = 0.0
    while slept < seconds:
        if time.time() >= end_time:
            return False
        if monitor.user_is_active():
            return True
        chunk = min(CFG.input_poll_interval, seconds - slept)
        time.sleep(chunk)
        slept += chunk
    return True


class ProjectTree:
    def __init__(self):
        self.folders: set = set()
        self.files: dict = {}
        self.entry: str = "index.html"

    def add_folder(self, path: str) -> None:
        path = path.replace("\\", "/").strip("/")
        if not path:
            return
        parts = path.split("/")
        acc = []
        for p in parts:
            acc.append(p)
            self.folders.add("/".join(acc))

    def add_file(self, path: str, content: str = "") -> None:
        path = path.replace("\\", "/").strip("/")
        if not path:
            return
        if "/" in path:
            self.add_folder("/".join(path.split("/")[:-1]))
        ext = Path(path).suffix.lower()
        if ext not in ALLOWED_EXTS:
            raise ValueError(
                f"Unsupported extension '{ext}'. Allowed: {sorted(ALLOWED_EXTS)}"
            )
        self.files[path] = content

    def remove(self, path: str) -> None:
        path = path.replace("\\", "/").strip("/")
        if path in self.files:
            del self.files[path]
            return
        for f in [p for p in self.files if p.startswith(path + "/")]:
            del self.files[f]
        self.folders.discard(path)
        for d in [p for p in self.folders if p.startswith(path + "/")]:
            self.folders.discard(d)

    def ordered_writes(self) -> list:
        ordered = sorted(self.files.keys(), key=lambda p: (p.count("/"), p))
        return [(p, self.files[p]) for p in ordered]

    def to_json(self) -> str:
        return json.dumps({
            "folders": sorted(self.folders),
            "files": self.files,
            "entry": self.entry,
        }, indent=2)

    @classmethod
    def from_json(cls, s: str) -> "ProjectTree":
        data = json.loads(s)
        t = cls()
        t.folders = set(data.get("folders", []))
        t.files = dict(data.get("files", {}))
        t.entry = data.get("entry", "index.html")
        return t


def find_notepadpp() -> str:
    for c in NOTEPADPP_CANDIDATES:
        if Path(c).exists():
            return c
    which = shutil.which("notepad++") or shutil.which("notepad++.exe")
    if which:
        return which
    raise FileNotFoundError(
        "Notepad++ not found. Install it or edit NOTEPADPP_CANDIDATES in the script."
    )


class NppWindow:
    def __init__(self, monitor: UserActivityMonitor):
        self.monitor = monitor
        self.proc: Optional[subprocess.Popen] = None
        self.window = None
        self.target_path: Optional[Path] = None

    def open(self, file_path: Path) -> None:
        npp = find_notepadpp()
        log.info(f"launching Notepad++ for {file_path.name}")
        self.proc = subprocess.Popen([npp, str(file_path)])
        self.target_path = file_path
        self.window = self._wait_for_window(file_path.name, timeout=CFG.np_launch_timeout)
        self._ensure_not_minimized()
        self._ensure_maximized()

    def _wait_for_window(self, title_hint: str, timeout: float):
        deadline = time.time() + timeout
        pattern = r".*" + re.escape(title_hint) + r".*"
        while time.time() < deadline:
            if self.monitor.user_is_active():
                time.sleep(CFG.np_poll_interval)
                continue
            try:
                wins = Desktop(backend="uia").windows(title_re=pattern, visible_only=True)
                for w in wins:
                    try:
                        if w.is_visible() and w.is_enabled():
                            return w
                    except Exception:
                        continue
            except ElementNotFoundError:
                pass
            except Exception as e:
                log.debug(f"npp poll: {e}")
            time.sleep(CFG.np_poll_interval)
        raise TimeoutError(f"Notepad++ window for '{title_hint}' not found within {timeout}s")

    def _ensure_not_minimized(self) -> None:
        if self.window is None:
            return
        try:
            if self.window.is_minimized():
                log.info("notepad++ was minimized - restoring")
                self.window.restore()
                time.sleep(0.2)
        except Exception as e:
            log.debug(f"restore failed: {e}")

    def _ensure_maximized(self) -> None:
        if self.window is None:
            return
        try:
            if not self.window.is_maximized():
                self.window.maximize()
                time.sleep(0.2)
        except Exception as e:
            log.debug(f"maximize failed: {e}")

    def focus(self) -> None:
        if self.window is None:
            return
        try:
            if self.window.is_minimized():
                self.window.restore()
                time.sleep(0.15)
            if not self.window.is_maximized():
                self.window.maximize()
                time.sleep(0.15)
            self.window.set_focus()
            time.sleep(0.15)
        except Exception as e:
            log.debug(f"npp focus: {e}")

    def is_alive(self) -> bool:
        if self.window is None:
            return False
        try:
            return bool(self.window.is_visible())
        except Exception:
            return False

    def close(self) -> None:
        try:
            if self.window is not None:
                self.window.close()
        except Exception as e:
            log.debug(f"npp close: {e}")
        try:
            if self.proc is not None:
                deadline = time.time() + CFG.np_close_timeout
                while time.time() < deadline:
                    if self.proc.poll() is not None:
                        break
                    time.sleep(0.1)
                if self.proc.poll() is None:
                    self.proc.terminate()
                    try:
                        self.proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self.proc.kill()
        except Exception as e:
            log.debug(f"npp proc cleanup: {e}")
        self.proc = None
        self.window = None


class Typer:
    def __init__(self, monitor: UserActivityMonitor):
        self.monitor = monitor
        self._last_save_time = 0.0
        self._active_window = None

    def bind_window(self, window) -> None:
        self._active_window = window

    def _guarded_send(self, fn: Callable[[], Any]) -> Any:
        with self.monitor.input_shield():
            return fn()

    def _char_delay(self) -> float:
        if random.random() < CFG.typing_delay_burst_chance:
            return random.uniform(CFG.typing_delay_burst_min, CFG.typing_delay_burst_max)
        return random.uniform(CFG.typing_delay_min, CFG.typing_delay_max)

    def type_content(self, text: str, save_path: Path, is_html: bool,
                     end_time: float) -> bool:
        chars_since_save = 0
        chars_since_break = 0
        next_break_at = random.randint(
            CFG.typing_break_chunk_size_min, CFG.typing_break_chunk_size_max
        )
        save_every = CFG.save_every_chars_html if is_html else CFG.save_every_chars_other
        self._last_save_time = time.time()

        for ch in text:
            if time.time() >= end_time:
                self._save()
                return False

            if self.monitor.user_is_active():
                log.info("typing paused - waiting for human to release control")
                if not self.monitor.wait_until_user_idle(end_time):
                    self._save()
                    return False
                self._refocus_editor()

            if ch == "\n":
                self._guarded_send(lambda: pyautogui.press("enter"))
                self._sleep(random.uniform(
                    CFG.typing_newline_pause_min, CFG.typing_newline_pause_max
                ), end_time)
                if random.random() < CFG.typing_break_chance_per_line:
                    if not self._take_typing_break(end_time):
                        self._save()
                        return False
            elif ch == "\t":
                self._guarded_send(lambda: pyautogui.press("tab"))
                self._sleep(random.uniform(
                    CFG.typing_indent_pause_min, CFG.typing_indent_pause_max
                ), end_time)
            else:
                self._guarded_send(lambda c=ch: pyautogui.write(c, interval=0))
                self._sleep(self._char_delay(), end_time)

            chars_since_save += 1
            chars_since_break += 1

            if ch == " ":
                self._sleep(random.uniform(
                    CFG.typing_space_pause_min, CFG.typing_space_pause_max
                ), end_time)
            elif ch in ".,;:!?{}()<>=":
                self._sleep(random.uniform(
                    CFG.typing_punct_pause_min, CFG.typing_punct_pause_max
                ), end_time)

            if random.random() < CFG.typing_hesitation_chance:
                self._sleep(random.uniform(
                    CFG.typing_hesitation_min, CFG.typing_hesitation_max
                ), end_time)

            now = time.time()
            if chars_since_save >= save_every or (now - self._last_save_time) >= CFG.save_every_seconds:
                self._save()
                chars_since_save = 0
                self._last_save_time = now

            if chars_since_break >= next_break_at:
                chars_since_break = 0
                next_break_at = random.randint(
                    CFG.typing_break_chunk_size_min, CFG.typing_break_chunk_size_max
                )
                if random.random() < CFG.typing_break_chance_per_chunk:
                    if not self._take_typing_break(end_time):
                        self._save()
                        return False

        self._save()
        return True

    def _take_typing_break(self, end_time: float) -> bool:
        duration = random.uniform(
            CFG.typing_break_duration_min, CFG.typing_break_duration_max
        )
        log.info(f"typing break: hovering for {duration:.1f}s")
        break_end = min(end_time, time.time() + duration)

        points = random.randint(
            CFG.typing_break_hover_min_points, CFG.typing_break_hover_max_points
        )
        for _ in range(points):
            if time.time() >= break_end:
                break
            if self.monitor.user_is_active():
                break
            self._hover_once(break_end)

        while time.time() < break_end:
            if self.monitor.user_is_active():
                if not self.monitor.wait_until_user_idle(end_time):
                    return False
                break
            sleep_responsive(
                min(0.5, break_end - time.time()), end_time, self.monitor
            )

        if time.time() >= end_time:
            return False

        self._refocus_editor()
        return True

    def _refocus_editor(self) -> None:
        if self._active_window is None:
            return
        try:
            if self._active_window.is_minimized():
                log.info("notepad++ minimized mid-session - restoring")
                self._active_window.restore()
                time.sleep(0.2)
            if not self._active_window.is_maximized():
                self._active_window.maximize()
                time.sleep(0.2)
            self._active_window.set_focus()
            time.sleep(CFG.typing_break_return_settle)
            with self.monitor.input_shield():
                pyautogui.click()
                time.sleep(0.06)
                pyautogui.hotkey("ctrl", "end")
                time.sleep(0.05)
        except Exception as e:
            log.debug(f"refocus failed: {e}")

    def _save(self) -> None:
        try:
            self._guarded_send(lambda: pyautogui.hotkey("ctrl", "s"))
            time.sleep(0.08)
        except Exception as e:
            log.debug(f"save failed: {e}")

    def _sleep(self, seconds: float, end_time: float) -> None:
        sleep_responsive(seconds, end_time, self.monitor)

    def _hover_once(self, end_time: float) -> None:
        w, h = pyautogui.size()
        m = CFG.hover_margin
        x = random.randint(m, max(m + 1, w - m))
        y = random.randint(m, max(m + 1, h - m))
        duration = random.uniform(
            CFG.hover_move_duration_min, CFG.hover_move_duration_max
        )
        self._smooth_move(x, y, duration, end_time)
        dwell = random.uniform(CFG.hover_dwell_min, CFG.hover_dwell_max)
        sleep_responsive(dwell, end_time, self.monitor)

    def _smooth_move(self, tx: int, ty: int, duration: float, end_time: float) -> None:
        sx, sy = pyautogui.position()
        cx = (sx + tx) / 2 + random.randint(-180, 180)
        cy = (sy + ty) / 2 + random.randint(-180, 180)
        steps = max(int(duration * 60), 10)
        for i in range(steps + 1):
            if time.time() >= end_time:
                return
            if self.monitor.user_is_active():
                return
            t = i / steps
            x = (1 - t) ** 2 * sx + 2 * (1 - t) * t * cx + t ** 2 * tx
            y = (1 - t) ** 2 * sy + 2 * (1 - t) * t * cy + t ** 2 * ty
            with self.monitor.input_shield():
                try:
                    pyautogui.moveTo(int(x), int(y))
                except Exception:
                    return
            time.sleep(duration / steps)


class Wanderer:
    def __init__(self, monitor: UserActivityMonitor, typer: Typer):
        self.monitor = monitor
        self.typer = typer
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def stopped(self) -> bool:
        return self._stop.is_set()

    def run(self, end_time: float) -> None:
        log.info("wander session started - hovering only, no clicks")
        while not self._stop.is_set():
            if time.time() >= end_time:
                break
            if self.monitor.user_is_active():
                if not self.monitor.wait_until_user_idle(end_time):
                    break
                continue
            points = random.randint(CFG.hover_min_points, CFG.hover_max_points)
            for _ in range(points):
                if self._stop.is_set() or time.time() >= end_time:
                    return
                if self.monitor.user_is_active():
                    break
                self.typer._hover_once(end_time)
            idle = random.uniform(CFG.hover_idle_min, CFG.hover_idle_max)
            sleep_responsive(idle, end_time, self.monitor)


class FlaskServer:
    def __init__(self, root: Path, entry: str, host: str, port: int):
        self.root = root
        self.entry = entry
        self.host = host
        self.port = port
        self._thread: Optional[threading.Thread] = None
        self._app: Optional[Flask] = None

    def start(self) -> None:
        self._app = self._build_app()
        self._thread = threading.Thread(target=self._serve, name="FlaskSrv", daemon=True)
        self._thread.start()

    def _build_app(self) -> Flask:
        root = self.root
        entry = self.entry
        app = Flask("webdevbot_preview", static_folder=None)

        @app.route("/")
        def index():
            target = root / entry
            if not target.exists():
                return ("<html><body style='font-family:system-ui;padding:2rem;"
                        "background:#14161a;color:#e6e8ec'>"
                        "<h2>Preview not ready</h2>"
                        "<p>The bot is still typing the entry file.</p>"
                        "</body></html>"), 200
            return send_from_directory(str(root), entry)

        @app.route("/<path:filename>")
        def any_file(filename):
            target = root / filename
            if not target.exists() or target.is_dir():
                abort(404)
            return send_from_directory(str(root), filename)

        @app.after_request
        def no_cache(resp):
            resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            resp.headers["Pragma"] = "no-cache"
            resp.headers["Expires"] = "0"
            return resp

        return app

    def _serve(self) -> None:
        try:
            log.info(f"flask server on http://{self.host}:{self.port}/")
            self._app.run(
                host=self.host,
                port=self.port,
                debug=False,
                use_reloader=False,
                threaded=True,
            )
        except Exception as e:
            log.error(f"flask server stopped: {e}")


class BotController:
    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._monitor: Optional[UserActivityMonitor] = None
        self._wanderer: Optional[Wanderer] = None
        self._flask: Optional[FlaskServer] = None
        self._tree: Optional[ProjectTree] = None
        self._duration_seconds: float = 0.0
        self._stop_event = threading.Event()
        self._state = "idle"
        self._current_npp: Optional[NppWindow] = None

    def state(self) -> str:
        return self._state

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, tree: ProjectTree, duration_hours: float) -> None:
        if self.is_running():
            return
        self._tree = tree
        self._duration_seconds = max(0.0, duration_hours * 3600.0)
        self._stop_event.clear()
        self._state = "starting"
        self._thread = threading.Thread(
            target=self._run, name="BotWorker", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._wanderer is not None:
            self._wanderer.stop()
        self._state = "stopping"

    def _run(self) -> None:
        try:
            self._state = "running"
            self._scaffold_project()

            self._flask = FlaskServer(
                PROJECT_ROOT, self._tree.entry, CFG.flask_host, CFG.flask_port
            )
            self._flask.start()
            time.sleep(0.4)
            try:
                webbrowser.open(f"http://{CFG.flask_host}:{CFG.flask_port}/")
            except Exception:
                pass

            self._monitor = UserActivityMonitor()
            self._monitor.start()

            typer = Typer(self._monitor)
            self._wanderer = Wanderer(self._monitor, typer)

            time.sleep(CFG.startup_grace)
            self._monitor.arm()

            end_time = (time.time() + self._duration_seconds
                        if self._duration_seconds > 0 else float("inf"))

            log.info(f"waiting for {CFG.idle_threshold:.1f}s idle before starting")
            if not self._monitor.wait_until_user_idle(end_time):
                log.info("session ended before typing started")
                return
            log.info("human idle - starting typing")

            for path, content in self._tree.ordered_writes():
                if self._stop_event.is_set() or time.time() >= end_time:
                    break
                self._type_file(path, content, typer, end_time)

            if self._stop_event.is_set():
                return

            remaining = end_time - time.time()
            if remaining > 5:
                log.info(f"typing complete - entering hover-only wander "
                         f"for remaining {remaining / 60:.1f} min")
                self._state = "wandering"
                self._wanderer.run(end_time=end_time)
            else:
                log.info("typing complete - no time remaining for wander")

        except Exception as e:
            log.exception(f"fatal: {e}")
        finally:
            try:
                if self._monitor is not None:
                    self._monitor.stop()
            except Exception:
                pass
            self._state = "idle"
            log.info("bot stopped")

    def _scaffold_project(self) -> None:
        PROJECT_ROOT.mkdir(parents=True, exist_ok=True)
        for folder in sorted(self._tree.folders):
            (PROJECT_ROOT / folder).mkdir(parents=True, exist_ok=True)
        for path, _ in self._tree.ordered_writes():
            target = PROJECT_ROOT / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("", encoding="utf-8")
        log.info(f"scaffolded {len(self._tree.folders)} folder(s) and "
                 f"{len(self._tree.files)} file(s) under {PROJECT_ROOT}")

    def _type_file(self, rel_path: str, content: str, typer: Typer,
                   end_time: float) -> None:
        if self._stop_event.is_set() or time.time() >= end_time:
            return
        target = PROJECT_ROOT / rel_path
        npp = NppWindow(self._monitor)
        self._current_npp = npp
        try:
            npp.open(target)
            npp.focus()
            time.sleep(0.35)
            self._select_all_and_clear()
            typer.bind_window(npp.window)
            is_html = target.suffix.lower() in (".html", ".htm")
            log.info(f"typing {rel_path} ({len(content)} chars)")
            typer.type_content(content, target, is_html, end_time)
            log.info(f"finished typing {rel_path}")
        except Exception as e:
            log.error(f"typing {rel_path} failed: {e}")
        finally:
            typer.bind_window(None)
            self._current_npp = None
            npp.close()
            time.sleep(0.25)

    def _select_all_and_clear(self) -> None:
        try:
            with self._monitor.input_shield():
                pyautogui.hotkey("ctrl", "a")
                time.sleep(0.06)
                pyautogui.press("delete")
                time.sleep(0.08)
        except Exception as e:
            log.debug(f"pre-clear failed: {e}")

    def enforce_window_state(self) -> None:
        npp = self._current_npp
        if npp is None or npp.window is None:
            return
        try:
            if npp.window.is_minimized():
                log.info("notepad++ minimized detected - restoring to maximized")
                npp.window.restore()
                time.sleep(0.15)
            if not npp.window.is_maximized():
                npp.window.maximize()
                time.sleep(0.15)
        except Exception as e:
            log.debug(f"window state enforcement failed: {e}")


class Palette:
    bg = "#14161a"
    surface = "#1c1f25"
    surface_hi = "#232730"
    surface_hi2 = "#2a2f3a"
    border = "#2b3038"
    border_hi = "#3a4050"
    text = "#e6e8ec"
    text_dim = "#8b909a"
    text_muted = "#5f6672"
    accent = "#7c9cff"
    accent_hi = "#9bb2ff"
    accent_dim = "#4a5f9e"
    success = "#5ec07f"
    warn = "#e5b757"
    danger = "#e06060"
    danger_hi = "#ef7373"
    code_bg = "#0f1115"
    code_fg = "#dde1e8"


def _rounded_rect(canvas: tk.Canvas, x1, y1, x2, y2, r, **kwargs):
    pts = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return canvas.create_polygon(pts, smooth=True, **kwargs)


class RoundedCard(tk.Frame):
    def __init__(self, master, padding=16, radius=16,
                 bg=None, border=None, **kwargs):
        super().__init__(master, bg=master["bg"], **kwargs)
        bg = bg or Palette.surface
        border = border or Palette.border
        self._radius = radius
        self._bg = bg
        self._border = border
        self._padding = padding

        self.canvas = tk.Canvas(
            self, bg=master["bg"], highlightthickness=0, bd=0
        )
        self.canvas.pack(fill="both", expand=True)

        self.inner = tk.Frame(self.canvas, bg=bg)
        self._win_id = self.canvas.create_window(
            padding, padding, anchor="nw", window=self.inner
        )

        self.canvas.bind("<Configure>", self._on_resize)

    def _on_resize(self, event):
        self.canvas.delete("card_bg")
        _rounded_rect(
            self.canvas, 1, 1, event.width - 1, event.height - 1,
            self._radius, fill=self._bg, outline=self._border, width=1,
            tags="card_bg",
        )
        self.canvas.tag_lower("card_bg")
        self.canvas.itemconfigure(
            self._win_id,
            width=max(event.width - 2 * self._padding, 1),
            height=max(event.height - 2 * self._padding, 1),
        )


class StyledButton(tk.Label):
    def __init__(self, master, text, command, variant="default",
                 padx=16, pady=8, font=("Segoe UI Semibold", 10)):
        palettes = {
            "default": (Palette.surface_hi, Palette.border_hi, Palette.text,
                        Palette.surface_hi2, Palette.text),
            "accent": (Palette.accent, Palette.accent, "#0e1014",
                       Palette.accent_hi, "#0e1014"),
            "danger": (Palette.danger, Palette.danger, "#ffffff",
                       Palette.danger_hi, "#ffffff"),
            "ghost": (Palette.surface, Palette.border, Palette.text_dim,
                      Palette.surface_hi, Palette.text),
        }
        bg, border, fg, hover_bg, hover_fg = palettes[variant]
        super().__init__(
            master, text=text, bg=bg, fg=fg,
            padx=padx, pady=pady, font=font, cursor="hand2",
            bd=0, highlightthickness=1,
            highlightbackground=border, highlightcolor=border,
        )
        self._hover_bg = hover_bg
        self._hover_fg = hover_fg
        self._bg = bg
        self._fg = fg
        self._cmd = command
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", lambda _e: self._cmd())

    def _on_enter(self, _e):
        self.configure(bg=self._hover_bg, fg=self._hover_fg)

    def _on_leave(self, _e):
        self.configure(bg=self._bg, fg=self._fg)


class ProjectPanel(tk.Frame):
    def __init__(self, master, tree: ProjectTree,
                 on_select: Callable[[str], None],
                 on_change: Callable[[], None]):
        super().__init__(master, bg=Palette.surface)
        self.tree_data = tree
        self.on_select = on_select
        self.on_change = on_change
        self.current: Optional[str] = None
        self._build()
        self.refresh()

    def _build(self):
        header = tk.Frame(self, bg=Palette.surface)
        header.pack(fill="x", padx=4, pady=(2, 10))
        tk.Label(header, text="Project Structure",
                 bg=Palette.surface, fg=Palette.text,
                 font=("Segoe UI Semibold", 11)).pack(side="left")

        body = tk.Frame(self, bg=Palette.surface)
        body.pack(fill="both", expand=True)

        style = ttk.Style()
        style.configure(
            "Project.Treeview",
            background=Palette.surface,
            fieldbackground=Palette.surface,
            foreground=Palette.text,
            bordercolor=Palette.surface,
            lightcolor=Palette.surface,
            darkcolor=Palette.surface,
            rowheight=26,
            font=("Segoe UI", 10),
        )
        style.map(
            "Project.Treeview",
            background=[("selected", Palette.accent_dim)],
            foreground=[("selected", "#ffffff")],
        )

        self.treeview = ttk.Treeview(
            body, show="tree", style="Project.Treeview", selectmode="browse",
        )
        self.treeview.pack(side="left", fill="both", expand=True)

        sb = ttk.Scrollbar(body, orient="vertical", command=self.treeview.yview)
        sb.pack(side="right", fill="y")
        self.treeview.configure(yscrollcommand=sb.set)

        self.treeview.bind("<<TreeviewSelect>>", self._on_select)
        self.treeview.bind("<Button-3>", self._on_right_click)

        actions = tk.Frame(self, bg=Palette.surface)
        actions.pack(fill="x", pady=(10, 0))
        StyledButton(actions, "+ File", self._new_file,
                     variant="ghost", padx=10, pady=6).pack(side="left")
        StyledButton(actions, "+ Folder", self._new_folder,
                     variant="ghost", padx=10, pady=6).pack(side="left", padx=(6, 0))
        StyledButton(actions, "Remove", self._remove_selected,
                     variant="ghost", padx=10, pady=6).pack(side="left", padx=(6, 0))

    def _on_select(self, _event):
        sel = self.treeview.selection()
        if not sel:
            return
        item = sel[0]
        values = self.treeview.item(item, "values")
        if not values:
            return
        kind, rel = values[0], values[1]
        if kind == "file":
            self.current = rel
            self.on_select(rel)
        else:
            self.current = None

    def _on_right_click(self, event):
        item = self.treeview.identify_row(event.y)
        if not item:
            return
        self.treeview.selection_set(item)
        values = self.treeview.item(item, "values")
        if not values:
            return
        kind, rel = values[0], values[1]
        menu = tk.Menu(self, tearoff=0,
                       bg=Palette.surface_hi, fg=Palette.text,
                       activebackground=Palette.accent_dim,
                       activeforeground="#ffffff", bd=0)
        if kind == "file":
            menu.add_command(label="Set as entry file",
                             command=lambda r=rel: self._set_entry(r))
        menu.add_command(label="Remove",
                         command=self._remove_selected)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _new_file(self):
        parent = ""
        sel = self.treeview.selection()
        if sel:
            values = self.treeview.item(sel[0], "values")
            if values:
                kind, rel = values[0], values[1]
                parent = rel if kind == "folder" else "/".join(rel.split("/")[:-1])
        name = simpledialog.askstring(
            "New file", "Path relative to project root (e.g. index.html or js/app.js):",
            initialvalue=(parent + "/" if parent else "") + "new.html",
            parent=self,
        )
        if not name:
            return
        name = name.strip()
        try:
            self.tree_data.add_file(name, "")
        except ValueError as e:
            messagebox.showerror("Unsupported", str(e))
            return
        self.refresh()
        self._select_path(name)
        self.on_change()

    def _new_folder(self):
        parent = ""
        sel = self.treeview.selection()
        if sel:
            values = self.treeview.item(sel[0], "values")
            if values:
                kind, rel = values[0], values[1]
                parent = rel if kind == "folder" else "/".join(rel.split("/")[:-1])
        name = simpledialog.askstring(
            "New folder", "Folder path relative to project root:",
            initialvalue=(parent + "/" if parent else "") + "assets",
            parent=self,
        )
        if not name:
            return
        self.tree_data.add_folder(name)
        self.refresh()
        self.on_change()

    def _remove_selected(self):
        sel = self.treeview.selection()
        if not sel:
            return
        values = self.treeview.item(sel[0], "values")
        if not values:
            return
        kind, rel = values[0], values[1]
        if not messagebox.askyesno("Remove", f"Remove {rel}?"):
            return
        self.tree_data.remove(rel)
        if self.current == rel:
            self.current = None
            self.on_select("")
        self.refresh()
        self.on_change()

    def _set_entry(self, rel: str):
        self.tree_data.entry = rel
        log.info(f"entry set to {rel}")
        self.refresh()
        self.on_change()

    def _select_path(self, path: str):
        for item in self.treeview.get_children(""):
            if self._walk_select(item, path):
                return

    def _walk_select(self, item, target) -> bool:
        values = self.treeview.item(item, "values")
        if values and values[1] == target:
            self.treeview.selection_set(item)
            self.treeview.see(item)
            return True
        for child in self.treeview.get_children(item):
            if self._walk_select(child, target):
                return True
        return False

    def refresh(self):
        open_state = {}
        for item in self.treeview.get_children(""):
            open_state[item] = self.treeview.item(item, "open")
        self.treeview.delete(*self.treeview.get_children(""))

        root_nodes = {}

        def ensure(folder):
            if folder in root_nodes:
                return root_nodes[folder]
            parent = "/".join(folder.split("/")[:-1]) if "/" in folder else ""
            name = folder.split("/")[-1]
            parent_node = ensure(parent) if parent else ""
            node = self.treeview.insert(
                parent_node, "end", text="  " + name,
                values=("folder", folder), open=True,
            )
            root_nodes[folder] = node
            return node

        for folder in sorted(self.tree_data.folders):
            ensure(folder)

        for path in sorted(self.tree_data.files.keys()):
            parent = "/".join(path.split("/")[:-1]) if "/" in path else ""
            parent_node = ensure(parent) if parent else ""
            name = path.split("/")[-1]
            marker = " *" if path == self.tree_data.entry else ""
            self.treeview.insert(
                parent_node, "end", text="  " + name + marker,
                values=("file", path),
            )


class EditorPanel(tk.Frame):
    def __init__(self, master, tree: ProjectTree,
                 on_change: Callable[[str, str], None]):
        super().__init__(master, bg=Palette.surface)
        self.tree_data = tree
        self.on_change = on_change
        self.current: Optional[str] = None
        self._build()

    def _build(self):
        header = tk.Frame(self, bg=Palette.surface)
        header.pack(fill="x", padx=4, pady=(2, 10))

        self._title_var = tk.StringVar(value="No file selected")
        tk.Label(header, textvariable=self._title_var,
                 bg=Palette.surface, fg=Palette.text,
                 font=("Segoe UI Semibold", 11)).pack(side="left")

        self._entry_var = tk.StringVar(value="")
        tk.Label(header, textvariable=self._entry_var,
                 bg=Palette.surface, fg=Palette.accent,
                 font=("Segoe UI Semibold", 10)).pack(side="right")

        wrap = tk.Frame(self, bg=Palette.code_bg)
        wrap.pack(fill="both", expand=True)

        self.text = tk.Text(
            wrap, wrap="none",
            bg=Palette.code_bg, fg=Palette.code_fg,
            insertbackground=Palette.accent,
            selectbackground=Palette.accent_dim, selectforeground="#ffffff",
            font=("Cascadia Code", 10), relief="flat", bd=0,
            padx=14, pady=12, undo=True,
        )
        self.text.pack(side="left", fill="both", expand=True)

        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.text.yview)
        sb.pack(side="right", fill="y")
        self.text.configure(yscrollcommand=sb.set)

        self.text.bind("<<Modified>>", self._on_modified)

    def _on_modified(self, _event):
        if not self.text.edit_modified():
            return
        self.text.edit_modified(False)
        if self.current is None:
            return
        self.tree_data.files[self.current] = self.text.get("1.0", "end-1c")
        self.on_change(self.current, self.tree_data.files[self.current])

    def load(self, path: str) -> None:
        if path == "":
            self.current = None
            self._title_var.set("No file selected")
            self._entry_var.set("")
            self.text.configure(state="normal")
            self.text.delete("1.0", "end")
            return
        self.current = path
        self._title_var.set(path)
        if path == self.tree_data.entry:
            self._entry_var.set("ENTRY")
        else:
            self._entry_var.set("")
        content = self.tree_data.files.get(path, "")
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", content)
        self.text.edit_modified(False)

    def refresh_entry_badge(self):
        if self.current is None:
            return
        if self.current == self.tree_data.entry:
            self._entry_var.set("ENTRY")
        else:
            self._entry_var.set("")


class WebDevBotApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("WebDevBot")
        self.geometry("1240x780")
        self.minsize(1040, 640)
        self.configure(bg=Palette.bg)

        self._tree_data = ProjectTree()
        self._tree_data.add_file("index.html", "")
        self._tree_data.add_file("styles/main.css", "")
        self._tree_data.add_file("scripts/app.js", "")

        self._log_queue: "queue.Queue[str]" = queue.Queue()
        self._ui_log_handler = UiLogQueue(self._log_queue)
        self._ui_log_handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%H:%M:%S",
        ))
        log.addHandler(self._ui_log_handler)

        self._controller = BotController()
        self._start_time: Optional[float] = None

        self._build_ui()
        self.after(100, self._drain_log)
        self.after(400, self._update_status)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        log.info(f"project root: {PROJECT_ROOT}")
        log.info(f"log file: {LOG_FILE}")

    def _build_ui(self):
        outer = tk.Frame(self, bg=Palette.bg)
        outer.pack(fill="both", expand=True, padx=18, pady=18)

        header = tk.Frame(outer, bg=Palette.bg)
        header.pack(fill="x", pady=(0, 14))

        tk.Label(header, text="WebDevBot",
                 bg=Palette.bg, fg=Palette.text,
                 font=("Segoe UI Semibold", 20)).pack(side="left")
        tk.Label(header, text="  code-typing automation for html / css / js",
                 bg=Palette.bg, fg=Palette.text_dim,
                 font=("Segoe UI", 10)).pack(side="left", padx=(10, 0))

        self._status_var = tk.StringVar(value="Idle")
        self._status_label = tk.Label(
            header, textvariable=self._status_var,
            bg=Palette.bg, fg=Palette.text_dim,
            font=("Segoe UI Semibold", 10),
        )
        self._status_label.pack(side="right")

        body = tk.Frame(outer, bg=Palette.bg)
        body.pack(fill="both", expand=True)

        left = RoundedCard(body, padding=16)
        left.pack(side="left", fill="both", expand=False, padx=(0, 14))
        left.configure(width=320)
        left.pack_propagate(False)

        self.project_panel = ProjectPanel(
            left.inner, self._tree_data,
            on_select=self._on_file_selected,
            on_change=self._on_tree_changed,
        )
        self.project_panel.pack(fill="both", expand=True)

        right = tk.Frame(body, bg=Palette.bg)
        right.pack(side="left", fill="both", expand=True)

        editor_card = RoundedCard(right, padding=16)
        editor_card.pack(fill="both", expand=True, pady=(0, 14))

        self.editor_panel = EditorPanel(
            editor_card.inner, self._tree_data,
            on_change=self._on_editor_change,
        )
        self.editor_panel.pack(fill="both", expand=True)

        bottom = tk.Frame(right, bg=Palette.bg)
        bottom.pack(fill="both", expand=False)

        controls_card = RoundedCard(bottom, padding=16)
        controls_card.pack(side="left", fill="both", expand=True, padx=(0, 14))

        controls = controls_card.inner

        row = tk.Frame(controls, bg=Palette.surface)
        row.pack(fill="x")

        tk.Label(row, text="Duration (hours)",
                 bg=Palette.surface, fg=Palette.text_dim,
                 font=("Segoe UI", 9)).pack(anchor="w")
        self._duration_var = tk.StringVar(value="1")
        dur_entry = tk.Entry(
            row, textvariable=self._duration_var,
            bg=Palette.code_bg, fg=Palette.text,
            insertbackground=Palette.accent,
            relief="flat", bd=0, width=10,
            font=("Segoe UI", 11),
        )
        dur_entry.pack(anchor="w", pady=(4, 12), ipady=6)

        tk.Label(row, text="Entry file",
                 bg=Palette.surface, fg=Palette.text_dim,
                 font=("Segoe UI", 9)).pack(anchor="w")
        self._entry_var = tk.StringVar(value=self._tree_data.entry)
        entry_display = tk.Label(
            row, textvariable=self._entry_var,
            bg=Palette.surface, fg=Palette.accent,
            font=("Segoe UI Semibold", 11),
        )
        entry_display.pack(anchor="w", pady=(4, 0))

        btn_row = tk.Frame(controls, bg=Palette.surface)
        btn_row.pack(fill="x", pady=(16, 0))

        self._start_btn = StyledButton(
            btn_row, "Start", self._on_start, variant="accent"
        )
        self._start_btn.pack(side="left")
        self._stop_btn = StyledButton(
            btn_row, "Stop", self._on_stop, variant="danger"
        )
        self._stop_btn.pack(side="left", padx=(8, 0))
        self._stop_btn.configure(state="disabled")

        metrics_card = RoundedCard(bottom, padding=16)
        metrics_card.pack(side="left", fill="both", expand=False)
        metrics_card.configure(width=280)
        metrics_card.pack_propagate(False)

        self._cpu_var = tk.StringVar(value="CPU  -")
        self._mem_var = tk.StringVar(value="RAM  -")
        self._uptime_var = tk.StringVar(value="Elapsed  00:00:00")
        self._mode_var = tk.StringVar(value="State  idle")

        for var in (self._cpu_var, self._mem_var, self._uptime_var, self._mode_var):
            tk.Label(
                metrics_card.inner, textvariable=var,
                bg=Palette.surface, fg=Palette.text,
                font=("Cascadia Code", 10),
                anchor="w",
            ).pack(fill="x", pady=2)

        tk.Label(
            metrics_card.inner, text=f"Root\n{PROJECT_ROOT}",
            bg=Palette.surface, fg=Palette.text_muted,
            font=("Segoe UI", 8), justify="left", anchor="w",
        ).pack(fill="x", pady=(10, 0))

        log_card = RoundedCard(right, padding=16)
        log_card.pack(fill="both", expand=True, pady=(0, 0))

        tk.Label(log_card.inner, text="Live Log",
                 bg=Palette.surface, fg=Palette.text_dim,
                 font=("Segoe UI Semibold", 10),
                 anchor="w").pack(fill="x", pady=(0, 6))

        self._log_text = scrolledtext.ScrolledText(
            log_card.inner, wrap="word",
            bg=Palette.code_bg, fg=Palette.code_fg,
            insertbackground=Palette.accent,
            font=("Cascadia Code", 9),
            relief="flat", bd=0,
        )
        self._log_text.pack(fill="both", expand=True)
        self._log_text.configure(state="disabled")
        self._log_text.tag_configure("INFO", foreground=Palette.code_fg)
        self._log_text.tag_configure("WARNING", foreground=Palette.warn)
        self._log_text.tag_configure("ERROR", foreground=Palette.danger)
        self._log_text.tag_configure("CRITICAL", foreground=Palette.danger_hi)

    def _on_file_selected(self, path: str):
        self.editor_panel.load(path)

    def _on_tree_changed(self):
        self.editor_panel.refresh_entry_badge()
        self._entry_var.set(self._tree_data.entry)

    def _on_editor_change(self, path: str, content: str):
        pass

    def _on_start(self):
        if self._controller.is_running():
            messagebox.showinfo("WebDevBot", "A session is already running.")
            return
        try:
            hours = float(self._duration_var.get())
            if hours <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("WebDevBot", "Duration must be a positive number.")
            return
        if not self._tree_data.files:
            messagebox.showerror("WebDevBot", "Add at least one file.")
            return
        if self._tree_data.entry not in self._tree_data.files:
            messagebox.showerror(
                "WebDevBot",
                f"Entry file '{self._tree_data.entry}' is not in the project."
            )
            return

        self._start_time = time.time()
        self._set_running(True)
        self._controller.start(self._tree_data, hours)
        log.info(f"session started - duration {hours}h")

    def _on_stop(self):
        if not self._controller.is_running():
            return
        self._controller.stop()
        log.info("session stopped by user")

    def _set_running(self, running: bool):
        if running:
            self._start_btn.configure(state="disabled")
            self._stop_btn.configure(state="normal")
        else:
            self._start_btn.configure(state="normal")
            self._stop_btn.configure(state="disabled")

    def _drain_log(self):
        drained = 0
        while True:
            try:
                msg = self._log_queue.get_nowait()
            except queue.Empty:
                break
            drained += 1
            self._append_log(msg)
            if drained >= 200:
                break
        self.after(100, self._drain_log)

    def _append_log(self, msg: str):
        level = "INFO"
        if "[WARNING]" in msg:
            level = "WARNING"
        elif "[ERROR]" in msg:
            level = "ERROR"
        elif "[CRITICAL]" in msg:
            level = "CRITICAL"

        self._log_text.configure(state="normal")
        self._log_text.insert("end", msg + "\n", level)
        self._log_text.see("end")
        line_count = int(self._log_text.index("end-1c").split(".")[0])
        if line_count > 4000:
            self._log_text.delete("1.0", f"{line_count - 3000}.0")
        self._log_text.configure(state="disabled")

    def _update_status(self):
        try:
            cpu = psutil.cpu_percent(interval=None)
            vm = psutil.virtual_memory()
            self._cpu_var.set(f"CPU  {cpu:5.1f}%")
            self._mem_var.set(f"RAM  {vm.percent:5.1f}%")
        except Exception:
            pass

        if self._start_time is not None and self._controller.is_running():
            elapsed = int(time.time() - self._start_time)
            self._uptime_var.set(
                f"Elapsed  {elapsed // 3600:02d}:{(elapsed % 3600) // 60:02d}:{elapsed % 60:02d}"
            )
        elif not self._controller.is_running():
            self._uptime_var.set("Elapsed  00:00:00")

        state = self._controller.state()
        self._status_var.set(state.capitalize())
        self._mode_var.set(f"State  {state}")

        colors = {
            "idle": Palette.text_dim,
            "starting": Palette.warn,
            "running": Palette.success,
            "wandering": Palette.accent,
            "stopping": Palette.warn,
        }
        self._status_label.configure(fg=colors.get(state, Palette.text_dim))

        if not self._controller.is_running() and str(self._stop_btn["state"]) == "normal":
            self._set_running(False)
            self._start_time = None

        self.after(400, self._update_status)

    def _on_close(self):
        if self._controller.is_running():
            if not messagebox.askyesno("WebDevBot", "A session is running. Stop and exit?"):
                return
            self._controller.stop()
            deadline = time.time() + 5.0
            while self._controller.is_running() and time.time() < deadline:
                time.sleep(0.05)
        try:
            log.removeHandler(self._ui_log_handler)
        except Exception:
            pass
        self.destroy()


def main() -> None:
    log.info("=" * 60)
    log.info("webdevbot starting")
    app = WebDevBotApp()
    app.mainloop()


if __name__ == "__main__":
    main()