import os
import sys
import time
import math
import glob
import json
import random
import threading
import logging
import subprocess
import queue
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

try:
    import pyautogui
    import pygetwindow as gw
    import psutil
    from pynput import mouse as pynput_mouse, keyboard as pynput_keyboard
    from pywinauto import Application, Desktop
    from pywinauto.findwindows import ElementNotFoundError
    from pywinauto.keyboard import send_keys as uia_send_keys
except ImportError as e:
    sys.stderr.write(f"Missing dependency: {e}\n")
    sys.stderr.write("Run: pip install pyautogui pynput pywinauto pygetwindow psutil\n")
    raise

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.01


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE = _base_dir()


@dataclass
class Config:
    idle_threshold: float = 1.2
    input_poll_interval: float = 0.03
    startup_grace: float = 1.0
    poll_interval: float = 0.15
    window_timeout: float = 15.0
    control_timeout: float = 10.0
    action_retries: int = 3
    action_retry_delay: float = 0.6

    wander_weights: dict = field(default_factory=lambda: {
        "move": 30, "hover": 25, "jitter": 15, "scroll": 10,
        "click_empty": 10, "rest": 10,
    })
    margin: int = 50

    allow_free_roam: bool = False
    whitelisted_apps: tuple = ("notepad.exe", "explorer.exe")
    whitelisted_window_classes: tuple = (
        "Notepad", "CabinetWClass", "ExploreWClass",
    )

    base_dir: Path = BASE
    logbot_dir: Path = BASE
    docs_dir: Path = BASE / "docs"
    log_file: Path = BASE / "logbot.log"

    docs_glob: tuple = ("*.txt", "*.md", "*.csv", "*.log", "*.json", "*.rtf")

    session_min_apps: int = 1
    session_max_apps: int = 2
    actions_per_app_min: int = 1
    actions_per_app_max: int = 3
    app_duration_min: float = 15.0
    app_duration_max: float = 45.0

    typing_interval_min: float = 0.06
    typing_interval_max: float = 0.18
    typing_word_pause_min: float = 0.04
    typing_word_pause_max: float = 0.22
    typing_line_pause_min: float = 0.6
    typing_line_pause_max: float = 1.8
    typing_hesitation_chance: float = 0.06
    typing_hesitation_min: float = 0.4
    typing_hesitation_max: float = 1.4

    rest_chance: float = 0.20
    rest_min: float = 3.0
    rest_max: float = 12.0
    micro_pause_chance: float = 0.25
    micro_pause_min: float = 0.15
    micro_pause_max: float = 0.7

    hover_break_chance: float = 0.25
    hover_break_min_points: int = 2
    hover_break_max_points: int = 6
    hover_break_dwell_min: float = 0.4
    hover_break_dwell_max: float = 2.5
    hover_break_idle_min: float = 5.0
    hover_break_idle_max: float = 20.0

    click_empty_chance: float = 0.35

    memory_high_watermark: float = 82.0
    memory_low_watermark: float = 65.0
    cpu_high_watermark: float = 88.0
    cpu_sample_interval: float = 4.0
    resource_check_interval: float = 20.0
    min_free_memory_percent: float = 15.0
    max_sandboxed_apps: int = 1
    protect_own_pid: bool = True

    freeze_detection_window: float = 25.0
    freeze_poll_interval: float = 5.0

    log_level: int = logging.INFO


CFG = Config()


class UiLogQueue(logging.Handler):
    def __init__(self, q: "queue.Queue[str]"):
        super().__init__()
        self.q = q

    def emit(self, record):
        try:
            msg = self.format(record)
        except Exception:
            return
        try:
            self.q.put_nowait(msg)
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
            CFG.logbot_dir.mkdir(parents=True, exist_ok=True)
            lg = logging.getLogger("logbot")
            lg.setLevel(CFG.log_level)
            fmt = logging.Formatter(
                "%(asctime)s [%(levelname)s] %(threadName)s %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
            fh = logging.FileHandler(CFG.log_file, encoding="utf-8")
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


class SafetyViolation(RuntimeError):
    pass


class SafetyGuard:
    @staticmethod
    def resolve_path(p) -> Path:
        target = Path(p).resolve()
        jail = CFG.logbot_dir.resolve()
        try:
            target.relative_to(jail)
        except ValueError:
            raise SafetyViolation(
                f"Refused: path '{target}' is outside the sandbox '{jail}'."
            )
        return target

    @staticmethod
    def is_safe_exe(exe: str) -> bool:
        name = Path(exe).name.lower()
        return name in (a.lower() for a in CFG.whitelisted_apps)

    @staticmethod
    def is_safe_window(title: str, window_class: str = "") -> bool:
        if CFG.allow_free_roam:
            return True
        if window_class and window_class in CFG.whitelisted_window_classes:
            return True
        title = (title or "").lower()
        return ("notepad" in title) or ("file explorer" in title) or ("explorer" in title)

    @staticmethod
    def assert_safe_exe(exe: str) -> None:
        if not SafetyGuard.is_safe_exe(exe):
            raise SafetyViolation(f"Executable '{exe}' is not on the whitelist.")

    @staticmethod
    def assert_safe_window(title: str, cls: str = "") -> None:
        if not SafetyGuard.is_safe_window(title, cls):
            raise SafetyViolation(
                f"Window '{title}' (class='{cls}') is not whitelisted."
            )


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
        log.info("monitor armed")

    def _on_user_input(self, *_a, **_k) -> None:
        if not self._armed:
            return
        if self._shield.is_set():
            return
        with self._lock:
            self.last_input_time = time.time()
            if not self.user_in_control:
                self.user_in_control = True
                log.info("user took control - bot pausing")

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
            time.sleep(CFG.input_poll_interval)
        if was_active:
            log.info("user idle - bot resuming")
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


def get_screen_size():
    return pyautogui.size()


def random_point(width: int, height: int, margin: int = None):
    m = margin if margin is not None else CFG.margin
    x = random.randint(m, max(m + 1, width - m))
    y = random.randint(m, max(m + 1, height - m))
    return x, y


class DocLibrary:
    def __init__(self, folder: Path, patterns: tuple):
        self.folder = folder
        self.patterns = patterns
        self.folder.mkdir(parents=True, exist_ok=True)
        self._cache: list = []
        self._lock = threading.Lock()

    def refresh(self) -> None:
        files = []
        for pat in self.patterns:
            files.extend(Path(p) for p in glob.glob(str(self.folder / pat)))
        chunks = []
        for f in files:
            try:
                raw = f.read_text(encoding="utf-8", errors="ignore")
            except Exception as e:
                log.warning(f"skipping {f.name}: {e}")
                continue
            cleaned = " ".join(raw.split())
            if cleaned:
                chunks.append(cleaned)
        with self._lock:
            self._cache = chunks
        log.info(f"doc library loaded: {len(chunks)} file(s) from {self.folder}")

    def sample(self, max_chars: int = 400) -> str:
        with self._lock:
            if not self._cache:
                return "logbot placeholder text"
            chunk = random.choice(self._cache)
        if len(chunk) <= max_chars:
            return chunk
        start = random.randint(0, len(chunk) - max_chars)
        return chunk[start:start + max_chars]

    def sample_lines(self, n: int = 3, max_chars: int = 200) -> list:
        text = self.sample(max_chars * n)
        words = text.split()
        if not words:
            return []
        lines = []
        for _ in range(n):
            k = random.randint(4, max(5, max_chars // 6))
            lines.append(" ".join(random.choices(words, k=k)))
        return lines


class WindowManager:
    def __init__(self, monitor: UserActivityMonitor):
        self.monitor = monitor
        self._opened_by_bot: set = set()
        self._lock = threading.Lock()

    def owned_pids(self) -> set:
        with self._lock:
            return set(self._opened_by_bot)

    def mark_opened(self, pid: int) -> None:
        with self._lock:
            self._opened_by_bot.add(pid)

    def forget(self, pid: int) -> None:
        with self._lock:
            self._opened_by_bot.discard(pid)

    def is_alive(self, pid: int) -> bool:
        try:
            p = psutil.Process(pid)
            return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False

    def launch(self, exe: str, args: Optional[list] = None) -> subprocess.Popen:
        SafetyGuard.assert_safe_exe(exe)
        args = args or []
        log.info(f"launching {exe} {args}")
        proc = subprocess.Popen([exe, *args])
        self.mark_opened(proc.pid)
        return proc

    def wait_for_window(self,
                        title_re: str,
                        timeout: float = None,
                        process_id: Optional[int] = None):
        timeout = timeout if timeout is not None else CFG.window_timeout
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.monitor.user_is_active():
                time.sleep(CFG.poll_interval)
                continue
            try:
                kwargs = {"title_re": title_re, "visible_only": True}
                if process_id is not None:
                    kwargs["process"] = process_id
                wins = Desktop(backend="uia").windows(**kwargs)
                for w in wins:
                    try:
                        if w.is_visible() and w.is_enabled():
                            return w
                    except Exception:
                        continue
            except ElementNotFoundError:
                pass
            except Exception as e:
                log.debug(f"wait_for_window poll: {e}")
            time.sleep(CFG.poll_interval)
        raise TimeoutError(f"No visible window matching '{title_re}' within {timeout}s")

    def activate(self, window) -> None:
        title = ""
        try:
            title = window.window_text()
        except Exception:
            pass
        SafetyGuard.assert_safe_window(title)
        try:
            if window.is_minimized():
                window.restore()
            window.set_focus()
            log.info(f"activated: {title}")
        except Exception as e:
            log.warning(f"activate failed for '{title}': {e}")

    def maximize(self, window) -> None:
        try:
            window.maximize()
        except Exception:
            with self.monitor.input_shield():
                try:
                    window.set_focus()
                    pyautogui.hotkey("win", "up")
                except Exception as e:
                    log.debug(f"maximize fallback failed: {e}")


class Actions:
    def __init__(self, monitor: UserActivityMonitor, wm: WindowManager):
        self.monitor = monitor
        self.wm = wm

    def _send(self, fn: Callable[[], Any], label: str) -> Any:
        with self.monitor.input_shield():
            try:
                return fn()
            except Exception as e:
                log.warning(f"{label} failed: {e}")
                raise

    def _type_delay(self) -> float:
        return random.uniform(CFG.typing_interval_min, CFG.typing_interval_max)

    def _maybe_hesitate(self, end_time: Optional[float] = None) -> None:
        if random.random() < CFG.typing_hesitation_chance:
            pause = random.uniform(CFG.typing_hesitation_min, CFG.typing_hesitation_max)
            if end_time is None:
                time.sleep(pause)
            else:
                sleep_responsive(pause, end_time, self.monitor)

    def _maybe_micro_pause(self, end_time: Optional[float] = None) -> None:
        if random.random() < CFG.micro_pause_chance:
            pause = random.uniform(CFG.micro_pause_min, CFG.micro_pause_max)
            if end_time is None:
                time.sleep(pause)
            else:
                sleep_responsive(pause, end_time, self.monitor)

    def smooth_move(self,
                    target_x: int,
                    target_y: int,
                    duration: Optional[float] = None,
                    end_time: Optional[float] = None) -> bool:
        if duration is None:
            duration = random.uniform(0.3, 1.5)
        start_x, start_y = pyautogui.position()
        ctrl_x = (start_x + target_x) / 2 + random.randint(-200, 200)
        ctrl_y = (start_y + target_y) / 2 + random.randint(-200, 200)
        steps = max(int(duration * 60), 10)
        for i in range(steps + 1):
            if end_time is not None and time.time() >= end_time:
                return False
            if self.monitor.user_is_active():
                return False
            t = i / steps
            x = (1 - t) ** 2 * start_x + 2 * (1 - t) * t * ctrl_x + t ** 2 * target_x
            y = (1 - t) ** 2 * start_y + 2 * (1 - t) * t * ctrl_y + t ** 2 * target_y
            self._send(lambda px=int(x), py=int(y): pyautogui.moveTo(px, py), "moveTo")
            time.sleep(duration / steps)
        return True

    def click_at(self, x: int, y: int) -> None:
        self._send(lambda: pyautogui.click(x, y), f"click_at({x},{y})")

    def click_control(self, control) -> None:
        def _do():
            control.wait("visible enabled ready", timeout=CFG.control_timeout)
            control.click_input()
        self._send(_do, "click_control")

    def type_text(self, text: str, end_time: Optional[float] = None) -> None:
        for ch in text:
            if end_time is not None and time.time() >= end_time:
                return
            if self.monitor.user_is_active():
                return
            self._send(lambda c=ch: pyautogui.write(c, interval=0), f"write('{ch}')")
            time.sleep(self._type_delay())
            if ch == " ":
                pause = random.uniform(CFG.typing_word_pause_min, CFG.typing_word_pause_max)
                if end_time is None:
                    time.sleep(pause)
                else:
                    sleep_responsive(pause, end_time, self.monitor)
            if ch in ".,;:!?":
                pause = random.uniform(0.15, 0.45)
                if end_time is None:
                    time.sleep(pause)
                else:
                    sleep_responsive(pause, end_time, self.monitor)
            self._maybe_hesitate(end_time)

    def type_into_control(self, control, text: str, end_time: Optional[float] = None) -> None:
        control.wait("visible enabled ready", timeout=CFG.control_timeout)
        control.set_focus()
        buf = []

        def _flush():
            if buf:
                self._send(
                    lambda s="".join(buf): control.type_keys(s, with_spaces=True, with_newlines=False),
                    "type_keys flush",
                )
                buf.clear()

        for ch in text:
            if end_time is not None and time.time() >= end_time:
                _flush()
                return
            if self.monitor.user_is_active():
                _flush()
                return

            if ch == "\n":
                _flush()
                self._send(
                    lambda: control.type_keys("{ENTER}", with_newlines=False),
                    "type_keys ENTER",
                )
                pause = random.uniform(CFG.typing_line_pause_min, CFG.typing_line_pause_max)
                if end_time is None:
                    time.sleep(pause)
                else:
                    sleep_responsive(pause, end_time, self.monitor)
                continue

            buf.append(ch)

            if ch == " ":
                _flush()
                self._send(
                    lambda: control.type_keys(" ", with_spaces=True, with_newlines=False),
                    "type_keys space",
                )
                pause = random.uniform(CFG.typing_word_pause_min, CFG.typing_word_pause_max)
                if end_time is None:
                    time.sleep(pause)
                else:
                    sleep_responsive(pause, end_time, self.monitor)
                self._maybe_micro_pause(end_time)
            elif ch in ".,;:!?":
                _flush()
                self._send(
                    lambda c=ch: control.type_keys(c, with_spaces=True, with_newlines=False),
                    f"type_keys '{ch}'",
                )
                pause = random.uniform(0.2, 0.55)
                if end_time is None:
                    time.sleep(pause)
                else:
                    sleep_responsive(pause, end_time, self.monitor)
            else:
                _flush()
                self._send(
                    lambda c=ch: control.type_keys(c, with_spaces=True, with_newlines=False),
                    f"type_keys '{ch}'",
                )
                time.sleep(self._type_delay())
                self._maybe_hesitate(end_time)

        _flush()

    def hotkey(self, *keys: str) -> None:
        self._send(lambda: pyautogui.hotkey(*keys), f"hotkey{keys}")

    def send_win_keys(self, seq: str) -> None:
        self._send(lambda: uia_send_keys(seq), f"send_keys({seq})")

    def scroll(self, clicks: int) -> None:
        self._send(lambda: pyautogui.scroll(clicks), "scroll")

    def jitter(self, count: Optional[int] = None,
               end_time: Optional[float] = None) -> bool:
        if count is None:
            count = random.randint(5, 25)
        for _ in range(count):
            if end_time is not None and time.time() >= end_time:
                return False
            if self.monitor.user_is_active():
                return False
            dx = random.randint(-4, 4)
            dy = random.randint(-4, 4)
            self._send(
                lambda a=dx, b=dy: pyautogui.moveRel(a, b, duration=0.02),
                "moveRel",
            )
        return True

    def hover_around(self, end_time: float) -> bool:
        w, h = get_screen_size()
        n_points = random.randint(
            CFG.hover_break_min_points, CFG.hover_break_max_points
        )
        log.info(f"hover break: {n_points} random point(s)")
        for _ in range(n_points):
            if time.time() >= end_time:
                return False
            if not self.monitor.wait_until_user_idle(end_time):
                return False
            x, y = random_point(w, h)
            if not self.smooth_move(x, y, end_time=end_time):
                return False
            dwell = random.uniform(
                CFG.hover_break_dwell_min, CFG.hover_break_dwell_max
            )
            sleep_responsive(dwell, end_time, self.monitor)
            if random.random() < CFG.click_empty_chance:
                try:
                    self.click_at(x, y)
                except Exception:
                    pass
                sleep_responsive(random.uniform(0.3, 1.0), end_time, self.monitor)
        idle_for = random.uniform(
            CFG.hover_break_idle_min, CFG.hover_break_idle_max
        )
        log.info(f"hover break: idle {idle_for:.1f}s")
        sleep_responsive(idle_for, end_time, self.monitor)
        return True

    def open_save_dialog(self) -> None:
        self.hotkey("ctrl", "s")

    def open_save_as_dialog(self) -> None:
        self.hotkey("ctrl", "shift", "s")

    def open_file_menu(self) -> None:
        self.hotkey("alt", "f")

    def accept_dialog(self) -> None:
        self.hotkey("enter")

    def cancel_dialog(self) -> None:
        self.hotkey("esc")

    def open_task_manager(self) -> None:
        self.hotkey("ctrl", "shift", "esc")

    def close_active_window(self) -> None:
        self.hotkey("alt", "f4")


class ResourceManager:
    def __init__(self, monitor: UserActivityMonitor, wm: WindowManager):
        self.monitor = monitor
        self.wm = wm
        self.self_pid = os.getpid()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_check = 0.0
        self._last_cpu = 0.0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="ResourceMgr", daemon=True)
        self._thread.start()
        log.info("resource manager started")

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                now = time.time()
                if now - self._last_check >= CFG.resource_check_interval:
                    self._last_check = now
                    if not self.monitor.user_is_active():
                        self._evaluate()
            except Exception as e:
                log.warning(f"resource loop error: {e}")
            time.sleep(CFG.cpu_sample_interval)

    def _evaluate(self) -> None:
        try:
            cpu = psutil.cpu_percent(interval=None)
            vm = psutil.virtual_memory()
            mem = vm.percent
        except Exception as e:
            log.debug(f"resource sample failed: {e}")
            return

        self._last_cpu = cpu
        log.debug(f"resource sample cpu={cpu:.1f}% mem={mem:.1f}%")

        if cpu >= CFG.cpu_high_watermark or mem >= CFG.memory_high_watermark:
            log.warning(f"resource spike detected cpu={cpu:.1f}% mem={mem:.1f}%")
            self._relieve_pressure()

        self._enforce_min_apps()

    def _relieve_pressure(self) -> None:
        owned = self.wm.owned_pids()
        if not owned:
            return
        procs = []
        for pid in list(owned):
            try:
                p = psutil.Process(pid)
                mem_mb = p.memory_info().rss / (1024 * 1024)
                procs.append((mem_mb, pid, p.name()))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                self.wm.forget(pid)
        procs.sort(reverse=True)
        target_mem = CFG.memory_low_watermark
        for mem_mb, pid, name in procs:
            try:
                vm = psutil.virtual_memory()
                if vm.percent <= target_mem:
                    break
            except Exception:
                break
            self._safe_terminate(pid, name)

    def _enforce_min_apps(self) -> None:
        owned = self.wm.owned_pids()
        if len(owned) <= CFG.max_sandboxed_apps:
            return
        survivors = list(owned)[: CFG.max_sandboxed_apps]
        for pid in list(owned):
            if pid in survivors:
                continue
            try:
                p = psutil.Process(pid)
                self._safe_terminate(pid, p.name())
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                self.wm.forget(pid)

    def _safe_terminate(self, pid: int, name: str) -> None:
        if CFG.protect_own_pid and pid == self.self_pid:
            log.warning("refused to terminate self")
            return
        try:
            p = psutil.Process(pid)
            if not SafetyGuard.is_safe_exe(p.name()):
                log.warning(f"refused to terminate non-whitelisted '{p.name()}'")
                return
            log.info(f"terminating sandboxed app pid={pid} name={name}")
            p.terminate()
            try:
                p.wait(timeout=5)
            except psutil.TimeoutExpired:
                p.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            log.debug(f"terminate {pid} skipped: {e}")
        finally:
            self.wm.forget(pid)


class FreezeGuard:
    def __init__(self, monitor: UserActivityMonitor, actions: Actions):
        self.monitor = monitor
        self.actions = actions
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_tick = time.time()

    def heartbeat(self) -> None:
        self._last_tick = time.time()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="FreezeGuard", daemon=True)
        self._thread.start()
        log.info("freeze guard started")

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            time.sleep(CFG.freeze_poll_interval)
            if self.monitor.user_is_active():
                self._last_tick = time.time()
                continue
            stall = time.time() - self._last_tick
            if stall >= CFG.freeze_detection_window:
                log.warning(f"freeze suspected (no heartbeat for {stall:.1f}s)")
                self._recover()
                self._last_tick = time.time()

    def _recover(self) -> None:
        try:
            with self.monitor.input_shield():
                pyautogui.hotkey("ctrl", "alt", "delete")
                time.sleep(1.5)
                pyautogui.press("esc")
            log.info("freeze recovery sent ctrl+alt+del then esc")
        except Exception as e:
            log.error(f"freeze recovery failed: {e}")


class WorkflowEngine:
    def __init__(self,
                 monitor: UserActivityMonitor,
                 wm: WindowManager,
                 actions: Actions,
                 freeze_guard: FreezeGuard):
        self.monitor = monitor
        self.wm = wm
        self.actions = actions
        self.freeze_guard = freeze_guard
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def stopped(self) -> bool:
        return self._stop.is_set()

    def run_step(self,
                 name: str,
                 fn: Callable[[], Any],
                 verify: Optional[Callable[[], bool]] = None,
                 retries: Optional[int] = None,
                 timeout: Optional[float] = None) -> bool:
        retries = retries if retries is not None else CFG.action_retries
        timeout = timeout if timeout is not None else CFG.control_timeout
        for attempt in range(1, retries + 1):
            if self._stop.is_set():
                return False
            if self.monitor.user_is_active():
                if not self.monitor.wait_until_user_idle(time.time() + 3600):
                    return False
            try:
                self.freeze_guard.heartbeat()
                log.info(f"step '{name}' attempt {attempt}/{retries}")
                fn()
                if verify is not None:
                    ok = self._verify_with_timeout(verify, timeout)
                    if not ok:
                        raise RuntimeError(f"verify failed for '{name}'")
                self.freeze_guard.heartbeat()
                log.info(f"step '{name}' ok")
                return True
            except Exception as e:
                log.warning(f"step '{name}' error: {e}")
                time.sleep(CFG.action_retry_delay)
        log.error(f"step '{name}' exhausted retries")
        return False

    def _verify_with_timeout(self, verify: Callable[[], bool], timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if verify():
                    return True
            except Exception as e:
                log.debug(f"verify poll: {e}")
            time.sleep(CFG.poll_interval)
        return False


class Workflows:
    def __init__(self,
                 monitor: UserActivityMonitor,
                 wm: WindowManager,
                 actions: Actions,
                 engine: WorkflowEngine):
        self.monitor = monitor
        self.wm = wm
        self.actions = actions
        self.engine = engine

    def wander(self, duration_seconds: float) -> None:
        w, h = get_screen_size()
        end_time = time.time() + duration_seconds
        log.info(f"wander starting for {duration_seconds:.0f}s")
        while time.time() < end_time and not self.engine.stopped():
            if not self.monitor.wait_until_user_idle(end_time):
                break
            if self.monitor.user_is_active():
                continue
            action = random.choices(
                list(CFG.wander_weights.keys()),
                weights=list(CFG.wander_weights.values()),
                k=1,
            )[0]
            try:
                self.engine.freeze_guard.heartbeat()
                if action == "move":
                    x, y = random_point(w, h)
                    self.actions.smooth_move(x, y, end_time=end_time)
                elif action == "hover":
                    x, y = random_point(w, h)
                    if self.actions.smooth_move(x, y, end_time=end_time):
                        sleep_responsive(
                            random.uniform(0.5, 3.0), end_time, self.monitor
                        )
                elif action == "jitter":
                    self.actions.jitter(end_time=end_time)
                elif action == "scroll":
                    x, y = random_point(w, h)
                    if self.actions.smooth_move(x, y, end_time=end_time):
                        self.actions.scroll(
                            random.choice([-1, 1]) * random.randint(50, 300)
                        )
                elif action == "click_empty":
                    x, y = random_point(w, h)
                    if self.actions.smooth_move(x, y, end_time=end_time):
                        sleep_responsive(
                            random.uniform(0.2, 0.8), end_time, self.monitor
                        )
                        try:
                            self.actions.click_at(x, y)
                        except Exception:
                            pass
                elif action == "rest":
                    sleep_responsive(
                        random.uniform(0.5, 3.0), end_time, self.monitor
                    )
            except pyautogui.FailSafeException:
                log.info("fail-safe triggered")
                return


class SessionRunner:
    def __init__(self,
                 monitor: UserActivityMonitor,
                 wm: WindowManager,
                 actions: Actions,
                 engine: WorkflowEngine,
                 workflows: Workflows,
                 docs: DocLibrary):
        self.monitor = monitor
        self.wm = wm
        self.actions = actions
        self.engine = engine
        self.workflows = workflows
        self.docs = docs

        self._recipes = {
            "notepad": self._run_notepad,
            "explorer": self._run_explorer,
        }

    def _available_recipes(self) -> list:
        return list(self._recipes.keys())

    def _maybe_rest(self, end_time: float) -> None:
        if random.random() < CFG.rest_chance:
            pause = random.uniform(CFG.rest_min, CFG.rest_max)
            log.info(f"taking a rest for {pause:.1f}s")
            sleep_responsive(pause, end_time, self.monitor)

    def _maybe_hover_break(self, end_time: float) -> None:
        if random.random() < CFG.hover_break_chance:
            log.info("starting hover break")
            self.actions.hover_around(end_time)

    def run(self, duration_seconds: float, mode: str = "sessions") -> None:
        end_time = time.time() + duration_seconds
        log.info(f"session runner started in mode '{mode}'")

        while time.time() < end_time and not self.engine.stopped():
            if not self.monitor.wait_until_user_idle(end_time):
                break
            if self.monitor.user_is_active():
                continue

            self._maybe_hover_break(end_time)

            if time.time() >= end_time or self.engine.stopped():
                break

            if mode == "notepad":
                plan = ["notepad"]
            elif mode == "explorer":
                plan = ["explorer"]
            elif mode == "sessions":
                n = random.randint(CFG.session_min_apps, CFG.session_max_apps)
                plan = random.sample(
                    self._available_recipes(),
                    k=min(n, len(self._recipes)),
                )
            else:
                log.warning(f"unknown mode '{mode}' - defaulting to 'sessions'")
                n = random.randint(CFG.session_min_apps, CFG.session_max_apps)
                plan = random.sample(
                    self._available_recipes(),
                    k=min(n, len(self._recipes)),
                )

            log.info(f"session plan: {plan}")

            for app_name in plan:
                if time.time() >= end_time or self.engine.stopped():
                    break
                if not self.monitor.wait_until_user_idle(end_time):
                    break
                try:
                    self._recipes[app_name](end_time)
                except Exception as e:
                    log.warning(f"recipe '{app_name}' raised: {e}")
                self._maybe_rest(end_time)

            sleep_responsive(random.uniform(2.0, 6.0), end_time, self.monitor)

        log.info("session runner finished")

    def _pick_duration(self) -> float:
        return random.uniform(CFG.app_duration_min, CFG.app_duration_max)

    def _run_notepad(self, end_time: float) -> None:
        app_deadline = min(end_time, time.time() + self._pick_duration())
        log.info("entering notepad recipe")

        proc = None
        try:
            proc = self.wm.launch("notepad.exe")
            try:
                win = self.wm.wait_for_window(r".*Notepad.*", process_id=proc.pid)
            except TimeoutError as e:
                log.error(f"notepad did not open: {e}")
                return

            self.wm.activate(win)
            self.wm.maximize(win)

            edit = None
            try:
                edit = win.child_window(control_type="Edit")
                edit.wait("visible enabled ready", timeout=CFG.control_timeout)
            except Exception as e:
                log.warning(f"edit control not resolved: {e}")

            n_actions = random.randint(
                CFG.actions_per_app_min, CFG.actions_per_app_max
            )
            log.info(f"notepad: performing {n_actions} action block(s)")

            for block in range(n_actions):
                if time.time() >= app_deadline or self.engine.stopped():
                    break
                if not self.monitor.wait_until_user_idle(app_deadline):
                    break
                if not self.wm.is_alive(proc.pid):
                    log.info("notepad was closed by user - ending recipe cleanly")
                    self.wm.forget(proc.pid)
                    return

                lines = self.docs.sample_lines(n=random.randint(2, 5))
                if not lines:
                    lines = ["logbot placeholder"]

                for line in lines:
                    if time.time() >= app_deadline or self.engine.stopped():
                        break
                    if not self.wm.is_alive(proc.pid):
                        log.info("notepad was closed by user mid-typing - stopping recipe")
                        self.wm.forget(proc.pid)
                        return
                    if self.monitor.user_is_active():
                        self.monitor.wait_until_user_idle(app_deadline)
                        continue

                    text = line + "\n"
                    if edit is not None:
                        ok = self.engine.run_step(
                            f"notepad type block {block+1}",
                            lambda t=text: self.actions.type_into_control(edit, t, app_deadline),
                        )
                        if not ok and not self.wm.is_alive(proc.pid):
                            self.wm.forget(proc.pid)
                            return
                    else:
                        ok = self.engine.run_step(
                            f"notepad type block {block+1}",
                            lambda t=text: self.actions.type_text(t, app_deadline),
                        )
                        if not ok and not self.wm.is_alive(proc.pid):
                            self.wm.forget(proc.pid)
                            return

                    sleep_responsive(
                        random.uniform(CFG.typing_line_pause_min, CFG.typing_line_pause_max),
                        app_deadline,
                        self.monitor,
                    )

                sleep_responsive(random.uniform(1.0, 3.0), app_deadline, self.monitor)

            if not self.engine.stopped() and time.time() < app_deadline:
                if self.wm.is_alive(proc.pid):
                    out = SafetyGuard.resolve_path(
                        CFG.logbot_dir / f"note_{int(time.time())}.txt"
                    )

                    def _save():
                        self.actions.open_save_as_dialog()
                        time.sleep(0.8)
                        self.actions.type_text(str(out))
                        time.sleep(0.3)
                        self.actions.accept_dialog()

                    self.engine.run_step(
                        "notepad save as",
                        _save,
                        verify=lambda: out.exists(),
                        timeout=10,
                    )
                else:
                    log.info("notepad closed by user before save - skipping save")

        finally:
            if proc is not None:
                self._maybe_close(proc.pid, "notepad.exe")

    def _run_explorer(self, end_time: float) -> None:
        app_deadline = min(end_time, time.time() + self._pick_duration())
        log.info("entering explorer recipe")

        target = CFG.logbot_dir
        target.mkdir(parents=True, exist_ok=True)

        proc = None
        try:
            proc = self.wm.launch("explorer.exe", [str(target)])
            try:
                win = self.wm.wait_for_window(
                    r".*logbot.*", process_id=proc.pid, timeout=10
                )
            except TimeoutError:
                try:
                    win = self.wm.wait_for_window(r".*logbot.*", timeout=10)
                except TimeoutError as e:
                    log.error(f"explorer window not found: {e}")
                    return

            self.wm.activate(win)
            self.wm.maximize(win)

            n_actions = random.randint(
                CFG.actions_per_app_min, CFG.actions_per_app_max
            )
            log.info(f"explorer: performing {n_actions} action block(s)")

            for block in range(n_actions):
                if time.time() >= app_deadline or self.engine.stopped():
                    break
                if not self.monitor.wait_until_user_idle(app_deadline):
                    break
                if not self.wm.is_alive(proc.pid):
                    log.info("explorer was closed by user - ending recipe cleanly")
                    self.wm.forget(proc.pid)
                    return
                if self.monitor.user_is_active():
                    continue

                action = random.choice(["browse", "new_folder", "open_file", "rename", "back"])

                try:
                    if action == "browse":
                        self.actions.jitter(end_time=app_deadline)
                        self.actions.scroll(random.choice([-1, 1]) * random.randint(80, 400))
                        sleep_responsive(random.uniform(0.8, 2.2), app_deadline, self.monitor)

                    elif action == "new_folder":
                        name = f"folder_{random.randint(1000, 9999)}"
                        self.actions.hotkey("ctrl", "shift", "n")
                        sleep_responsive(random.uniform(0.4, 1.0), app_deadline, self.monitor)
                        self.actions.type_text(name, app_deadline)
                        sleep_responsive(random.uniform(0.3, 0.8), app_deadline, self.monitor)
                        self.actions.accept_dialog()
                        sleep_responsive(random.uniform(0.5, 1.2), app_deadline, self.monitor)

                    elif action == "open_file":
                        self.actions.hotkey("alt", "up")
                        sleep_responsive(random.uniform(0.5, 1.2), app_deadline, self.monitor)
                        self.actions.hotkey("alt", "down")
                        sleep_responsive(random.uniform(0.5, 1.2), app_deadline, self.monitor)
                        self.actions.accept_dialog()
                        sleep_responsive(random.uniform(0.5, 1.5), app_deadline, self.monitor)

                    elif action == "rename":
                        self.actions.jitter(end_time=app_deadline)
                        sleep_responsive(random.uniform(0.4, 1.0), app_deadline, self.monitor)
                        self.actions.hotkey("f2")
                        sleep_responsive(random.uniform(0.3, 0.8), app_deadline, self.monitor)
                        self.actions.hotkey("esc")

                    elif action == "back":
                        self.actions.hotkey("alt", "left")
                        sleep_responsive(random.uniform(0.5, 1.2), app_deadline, self.monitor)
                        self.actions.hotkey("alt", "right")
                        sleep_responsive(random.uniform(0.5, 1.2), app_deadline, self.monitor)

                except Exception as e:
                    log.debug(f"explorer action '{action}' failed: {e}")

        finally:
            if proc is not None:
                self._maybe_close(proc.pid, "explorer.exe")

    def _maybe_close(self, pid: int, exe_name: str) -> None:
        if pid not in self.wm.owned_pids():
            return
        if pid == os.getpid():
            return
        if not self.wm.is_alive(pid):
            log.info(f"{exe_name} pid={pid} already gone - nothing to close")
            self.wm.forget(pid)
            return
        try:
            p = psutil.Process(pid)
            if not SafetyGuard.is_safe_exe(p.name()):
                self.wm.forget(pid)
                return
            log.info(f"closing {exe_name} pid={pid}")
            p.terminate()
            try:
                p.wait(timeout=5)
            except psutil.TimeoutExpired:
                p.kill()
        except Exception as e:
            log.debug(f"close {pid} skipped: {e}")
        finally:
            self.wm.forget(pid)


class BotController:
    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._monitor: Optional[UserActivityMonitor] = None
        self._engine: Optional[WorkflowEngine] = None
        self._resources: Optional[ResourceManager] = None
        self._freeze_guard: Optional[FreezeGuard] = None
        self._state = "idle"

    def state(self) -> str:
        return self._state

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, duration_hours: float, mode: str) -> None:
        if self.is_running():
            return
        self._stop_event.clear()
        self._state = "starting"
        self._thread = threading.Thread(
            target=self._run,
            args=(duration_hours, mode),
            name="BotWorker",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._engine is not None:
            self._engine.stop()
        self._state = "stopping"

    def _run(self, duration_hours: float, mode: str) -> None:
        try:
            self._state = "running"
            docs = DocLibrary(CFG.docs_dir, CFG.docs_glob)
            docs.refresh()

            self._monitor = UserActivityMonitor()
            wm = WindowManager(self._monitor)
            actions = Actions(self._monitor, wm)
            self._freeze_guard = FreezeGuard(self._monitor, actions)
            self._engine = WorkflowEngine(self._monitor, wm, actions, self._freeze_guard)
            self._resources = ResourceManager(self._monitor, wm)
            workflows = Workflows(self._monitor, wm, actions, self._engine)
            runner = SessionRunner(
                self._monitor, wm, actions, self._engine, workflows, docs
            )

            self._monitor.start()
            self._freeze_guard.start()
            self._resources.start()

            duration_seconds = duration_hours * 3600
            end_time = time.time() + duration_seconds

            time.sleep(CFG.startup_grace)
            self._monitor.arm()

            log.info(f"waiting for {CFG.idle_threshold:.1f}s idle before starting")
            while self._monitor.user_is_active():
                if time.time() >= end_time or self._stop_event.is_set():
                    log.info("aborted before start")
                    return
                time.sleep(CFG.input_poll_interval)
            log.info("idle confirmed - starting")

            if self._stop_event.is_set():
                return

            if mode == "wander":
                workflows.wander(duration_seconds)
            else:
                runner.run(duration_seconds, mode=mode)

        except SafetyViolation as e:
            log.error(f"safety violation: {e}")
        except Exception as e:
            log.exception(f"fatal: {e}")
        finally:
            try:
                if self._engine is not None:
                    self._engine.stop()
                if self._resources is not None:
                    self._resources.stop()
                if self._freeze_guard is not None:
                    self._freeze_guard.stop()
                if self._monitor is not None:
                    self._monitor.stop()
            except Exception:
                pass
            self._state = "idle"
            log.info("logbot stopped")


class LogbotApp(tk.Tk):
    def __init__(self):
        super().__init__()

        self.title("LogBot - Human-Supervised Desktop Automation")
        self.geometry("980x680")
        self.minsize(820, 560)
        self.configure(bg="#1e1f22")

        self._log_queue: "queue.Queue[str]" = queue.Queue()
        self._ui_log_handler = UiLogQueue(self._log_queue)
        self._ui_log_handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%H:%M:%S",
        ))
        log.addHandler(self._ui_log_handler)

        self._controller = BotController()
        self._start_time: Optional[float] = None

        self._build_style()
        self._build_layout()
        self._wire_events()

        self.after(100, self._drain_log_queue)
        self.after(400, self._update_status)

        log.info(f"base dir: {CFG.base_dir}")
        log.info(f"docs dir: {CFG.docs_dir}")
        log.info(f"log file: {CFG.log_file}")

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        bg = "#1e1f22"
        fg = "#e6e6e6"
        accent = "#4a9eff"
        accent_hover = "#6fb4ff"
        panel = "#2a2c30"
        border = "#3a3d43"

        style.configure(".", background=bg, foreground=fg, fieldbackground=panel)
        style.configure("TFrame", background=bg)
        style.configure("TLabel", background=bg, foreground=fg, font=("Segoe UI", 10))
        style.configure("TButton", background=panel, foreground=fg, borderwidth=0,
                        focusthickness=0, padding=(12, 8), font=("Segoe UI", 10))
        style.map("TButton",
                  background=[("active", border), ("disabled", panel)],
                  foreground=[("disabled", "#7a7a7a")])
        style.configure("Accent.TButton", background=accent, foreground="#ffffff",
                        font=("Segoe UI", 10, "bold"))
        style.map("Accent.TButton",
                  background=[("active", accent_hover), ("disabled", panel)])
        style.configure("Stop.TButton", background="#c9484b", foreground="#ffffff",
                        font=("Segoe UI", 10, "bold"))
        style.map("Stop.TButton",
                  background=[("active", "#e05a5d"), ("disabled", panel)])
        style.configure("TEntry", fieldbackground=panel, foreground=fg,
                        insertcolor=fg, bordercolor=border, lightcolor=border, darkcolor=border)
        style.configure("TCombobox", fieldbackground=panel, background=panel,
                        foreground=fg, arrowcolor=fg)
        style.configure("TLabelframe", background=bg, foreground=fg,
                        bordercolor=border, relief="solid")
        style.configure("TLabelframe.Label", background=bg, foreground=accent,
                        font=("Segoe UI", 10, "bold"))
        style.configure("Status.TLabel", font=("Segoe UI", 10, "bold"))

    def _build_layout(self):
        outer = ttk.Frame(self, padding=16)
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 12))
        ttk.Label(header, text="LogBot",
                  font=("Segoe UI", 20, "bold")).pack(side="left")
        ttk.Label(header, text="human-supervised desktop automation",
                  font=("Segoe UI", 10, "italic"),
                  foreground="#9a9a9a").pack(side="left", padx=(10, 0))

        self._status_var = tk.StringVar(value="Idle")
        self._status_label = ttk.Label(header, textvariable=self._status_var,
                                       style="Status.TLabel", foreground="#9a9a9a")
        self._status_label.pack(side="right")

        controls = ttk.LabelFrame(outer, text=" Session Control ", padding=14)
        controls.pack(fill="x", pady=(0, 12))

        row1 = ttk.Frame(controls)
        row1.pack(fill="x", pady=(0, 10))

        ttk.Label(row1, text="Duration (hours):").pack(side="left")
        self._duration_var = tk.StringVar(value="1")
        ttk.Entry(row1, textvariable=self._duration_var, width=8).pack(side="left", padx=(8, 20))

        ttk.Label(row1, text="Mode:").pack(side="left")
        self._mode_var = tk.StringVar(value="sessions")
        mode_combo = ttk.Combobox(
            row1,
            textvariable=self._mode_var,
            values=("notepad", "explorer", "sessions", "wander"),
            state="readonly",
            width=14,
        )
        mode_combo.pack(side="left", padx=(8, 20))

        ttk.Label(row1, text="Idle threshold (s):").pack(side="left")
        self._idle_var = tk.StringVar(value=str(CFG.idle_threshold))
        ttk.Entry(row1, textvariable=self._idle_var, width=6).pack(side="left", padx=(8, 0))

        row2 = ttk.Frame(controls)
        row2.pack(fill="x")

        self._start_btn = ttk.Button(row2, text="Start", style="Accent.TButton",
                                     command=self._on_start)
        self._start_btn.pack(side="left")
        self._stop_btn = ttk.Button(row2, text="Stop", style="Stop.TButton",
                                    command=self._on_stop, state="disabled")
        self._stop_btn.pack(side="left", padx=(8, 0))
        ttk.Button(row2, text="Open docs folder",
                   command=self._open_docs).pack(side="left", padx=(16, 0))
        ttk.Button(row2, text="Open log file",
                   command=self._open_log).pack(side="left", padx=(8, 0))
        ttk.Button(row2, text="Refresh docs",
                   command=self._refresh_docs).pack(side="left", padx=(8, 0))
        ttk.Button(row2, text="Clear log",
                   command=self._clear_log).pack(side="left", padx=(8, 0))

        metrics = ttk.LabelFrame(outer, text=" Live Metrics ", padding=14)
        metrics.pack(fill="x", pady=(0, 12))

        grid = ttk.Frame(metrics)
        grid.pack(fill="x")

        self._cpu_var = tk.StringVar(value="CPU: -")
        self._mem_var = tk.StringVar(value="RAM: -")
        self._uptime_var = tk.StringVar(value="Elapsed: 00:00:00")
        self._mode_metric_var = tk.StringVar(value="Mode: -")
        self._docs_var = tk.StringVar(value=f"Docs: {CFG.docs_dir}")

        for i, var in enumerate([self._cpu_var, self._mem_var,
                                 self._uptime_var, self._mode_metric_var]):
            ttk.Label(grid, textvariable=var, font=("Consolas", 10)).grid(
                row=0, column=i, sticky="w", padx=(0, 28)
            )
        ttk.Label(grid, textvariable=self._docs_var,
                  font=("Consolas", 9), foreground="#9a9a9a").grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(6, 0)
        )

        log_frame = ttk.LabelFrame(outer, text=" Live Log ", padding=8)
        log_frame.pack(fill="both", expand=True)

        self._log_text = scrolledtext.ScrolledText(
            log_frame,
            wrap="word",
            bg="#15161a",
            fg="#d7d7d7",
            insertbackground="#d7d7d7",
            font=("Consolas", 9),
            relief="flat",
            borderwidth=0,
        )
        self._log_text.pack(fill="both", expand=True)
        self._log_text.configure(state="disabled")

        self._log_text.tag_configure("INFO", foreground="#d7d7d7")
        self._log_text.tag_configure("WARNING", foreground="#e6b455")
        self._log_text.tag_configure("ERROR", foreground="#e05a5d")
        self._log_text.tag_configure("CRITICAL", foreground="#ff7a7a")

        footer = ttk.Frame(outer)
        footer.pack(fill="x", pady=(10, 0))
        ttk.Label(footer,
                  text="Fail-safe: slam mouse into a corner to abort.  "
                       "Manual override: any keyboard/mouse input pauses the bot for the idle threshold.",
                  font=("Segoe UI", 9), foreground="#9a9a9a").pack(side="left")

    def _wire_events(self):
        self.bind("<Control-s>", lambda _e: self._on_start())
        self.bind("<Control-S>", lambda _e: self._on_start())
        self.bind("<Escape>", lambda _e: self._on_stop())

    def _on_start(self):
        if self._controller.is_running():
            messagebox.showinfo("LogBot", "A session is already running.")
            return
        try:
            duration_hours = float(self._duration_var.get())
            if duration_hours <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("LogBot", "Duration must be a positive number.")
            return
        try:
            idle = float(self._idle_var.get())
            if idle <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("LogBot", "Idle threshold must be a positive number.")
            return

        CFG.idle_threshold = idle
        mode = self._mode_var.get()
        self._start_time = time.time()
        self._mode_metric_var.set(f"Mode: {mode}")
        self._set_running(True)
        log.info(f"UI start requested: duration={duration_hours}h mode={mode} idle={idle}s")
        self._controller.start(duration_hours, mode)

    def _on_stop(self):
        if not self._controller.is_running():
            return
        log.info("UI stop requested")
        self._controller.stop()

    def _open_docs(self):
        CFG.docs_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(CFG.docs_dir))
        except Exception as e:
            messagebox.showerror("LogBot", f"Could not open docs folder:\n{e}")

    def _open_log(self):
        if CFG.log_file.exists():
            try:
                os.startfile(str(CFG.log_file))
                return
            except Exception as e:
                messagebox.showerror("LogBot", f"Could not open log:\n{e}")
        else:
            messagebox.showinfo("LogBot", "Log file not created yet.")

    def _refresh_docs(self):
        try:
            lib = DocLibrary(CFG.docs_dir, CFG.docs_glob)
            lib.refresh()
            messagebox.showinfo("LogBot", "Docs refreshed - see log for the count.")
        except Exception as e:
            messagebox.showerror("LogBot", f"Refresh failed:\n{e}")

    def _clear_log(self):
        self._log_text.configure(state="normal")
        self._log_text.delete("1.0", "end")
        self._log_text.configure(state="disabled")

    def _set_running(self, running: bool):
        self._start_btn.configure(state="disabled" if running else "normal")
        self._stop_btn.configure(state="normal" if running else "disabled")

    def _drain_log_queue(self):
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
        self.after(100, self._drain_log_queue)

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
        if line_count > 5000:
            self._log_text.delete("1.0", f"{line_count - 4000}.0")

        self._log_text.configure(state="disabled")

    def _update_status(self):
        try:
            cpu = psutil.cpu_percent(interval=None)
            vm = psutil.virtual_memory()
            self._cpu_var.set(f"CPU: {cpu:5.1f}%")
            self._mem_var.set(f"RAM: {vm.percent:5.1f}%")
        except Exception:
            self._cpu_var.set("CPU: -")
            self._mem_var.set("RAM: -")

        if self._start_time is not None and self._controller.is_running():
            elapsed = int(time.time() - self._start_time)
            self._uptime_var.set(
                f"Elapsed: {elapsed // 3600:02d}:{(elapsed % 3600) // 60:02d}:{elapsed % 60:02d}"
            )
        elif not self._controller.is_running():
            self._uptime_var.set("Elapsed: 00:00:00")

        state = self._controller.state()
        colors = {
            "idle": "#9a9a9a",
            "starting": "#e6b455",
            "running": "#6fc07a",
            "stopping": "#e6b455",
        }
        self._status_var.set(state.capitalize())
        self._status_label.configure(foreground=colors.get(state, "#9a9a9a"))

        if not self._controller.is_running() and str(self._stop_btn["state"]) == "normal":
            self._set_running(False)
            self._start_time = None

        self.after(400, self._update_status)

    def _on_close(self):
        if self._controller.is_running():
            if not messagebox.askyesno("LogBot", "A session is running. Stop it and exit?"):
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
    log.info("logbot (UI) starting")
    app = LogbotApp()
    app.mainloop()


if __name__ == "__main__":
    main()