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


@dataclass
class Config:
    idle_threshold: float = 3.0
    startup_grace: float = 1.0
    poll_interval: float = 0.15
    window_timeout: float = 15.0
    control_timeout: float = 10.0
    action_retries: int = 3
    action_retry_delay: float = 0.6

    wander_weights: dict = field(default_factory=lambda: {
        "move": 35, "hover": 25, "jitter": 15, "scroll": 15, "rest": 10,
    })
    margin: int = 50

    allow_free_roam: bool = False
    whitelisted_apps: tuple = (
        "notepad.exe", "explorer.exe", "calc.exe", "CalculatorApp.exe",
        "Calculator.exe", "mspaint.exe", "write.exe", "wordpad.exe",
        "taskmgr.exe",
    )
    whitelisted_window_classes: tuple = (
        "Notepad", "CabinetWClass", "ExploreWClass",
        "ApplicationFrameWindow", "CalcFrame", "MSPaintApp",
        "WordPadClass", "TaskManagerWindow",
    )
    logbot_dir: Path = Path.home() / "Desktop" / "logbot"

    docs_dir: Path = Path.home() / "Desktop" / "logbot" / "docs"
    docs_glob: tuple = ("*.txt", "*.md", "*.csv", "*.log", "*.json", "*.rtf")

    session_min_apps: int = 1
    session_max_apps: int = 3
    actions_per_app_min: int = 1
    actions_per_app_max: int = 3
    app_duration_min: float = 20.0
    app_duration_max: float = 90.0

    typing_interval_min: float = 0.06
    typing_interval_max: float = 0.18
    typing_word_pause_min: float = 0.04
    typing_word_pause_max: float = 0.22
    typing_line_pause_min: float = 0.6
    typing_line_pause_max: float = 1.8
    typing_hesitation_chance: float = 0.06
    typing_hesitation_min: float = 0.4
    typing_hesitation_max: float = 1.4
    typing_mistake_chance: float = 0.015

    rest_chance: float = 0.15
    rest_min: float = 3.0
    rest_max: float = 12.0
    micro_pause_chance: float = 0.25
    micro_pause_min: float = 0.15
    micro_pause_max: float = 0.7

    memory_high_watermark: float = 82.0
    memory_low_watermark: float = 65.0
    cpu_high_watermark: float = 88.0
    cpu_sample_interval: float = 4.0
    resource_check_interval: float = 20.0
    min_free_memory_percent: float = 15.0
    max_sandboxed_apps: int = 2
    protect_own_pid: bool = True

    freeze_detection_window: float = 25.0
    freeze_poll_interval: float = 5.0

    log_file: Path = Path.home() / "Desktop" / "logbot" / "logbot.log"
    log_level: int = logging.INFO


CFG = Config()


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
            ch = logging.StreamHandler(sys.stdout)
            ch.setFormatter(fmt)
            lg.addHandler(fh)
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
        keywords = ("notepad", "file explorer", "calculator", "paint",
                    "wordpad", "task manager")
        return any(k in title for k in keywords)

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
                log.info("user took control - bot paused")

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
            time.sleep(0.05)
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
        chunk = min(0.25, seconds - slept)
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

    def is_visible(self, window) -> bool:
        try:
            return bool(window.is_visible())
        except Exception:
            return False

    def is_ready(self, window) -> bool:
        try:
            return bool(window.is_visible() and window.is_enabled())
        except Exception:
            return False


class Actions:
    def __init__(self, monitor: UserActivityMonitor, wm: WindowManager):
        self.monitor = monitor
        self.wm = wm

    def _guarded(self, fn: Callable[[], Any], label: str) -> Any:
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
            with self.monitor.input_shield():
                pyautogui.moveTo(int(x), int(y))
            time.sleep(duration / steps)
        return True

    def click_at(self, x: int, y: int) -> None:
        self._guarded(lambda: pyautogui.click(x, y), f"click_at({x},{y})")

    def click_control(self, control) -> None:
        def _do():
            control.wait("visible enabled ready", timeout=CFG.control_timeout)
            control.click_input()
        self._guarded(_do, "click_control")

    def type_text(self, text: str) -> None:
        def _do():
            for ch in text:
                pyautogui.write(ch, interval=0)
                time.sleep(self._type_delay())
                if ch == " ":
                    time.sleep(random.uniform(
                        CFG.typing_word_pause_min, CFG.typing_word_pause_max
                    ))
                if ch in ".,;:!?":
                    time.sleep(random.uniform(0.15, 0.45))
                self._maybe_hesitate()
        self._guarded(_do, "type_text")

    def type_into_control(self, control, text: str, end_time: Optional[float] = None) -> None:
        def _do():
            control.wait("visible enabled ready", timeout=CFG.control_timeout)
            control.set_focus()
            buf = []
            for ch in text:
                if end_time is not None and time.time() >= end_time:
                    break
                if self.monitor.user_is_active():
                    break
                if ch == "\n":
                    if buf:
                        control.type_keys(
                            "".join(buf),
                            with_spaces=True, with_newlines=False,
                        )
                        buf = []
                    control.type_keys("{ENTER}", with_newlines=False)
                    time.sleep(random.uniform(
                        CFG.typing_line_pause_min, CFG.typing_line_pause_max
                    ))
                    continue
                buf.append(ch)
                if ch == " ":
                    control.type_keys(" ", with_spaces=True, with_newlines=False)
                    buf = []
                    time.sleep(random.uniform(
                        CFG.typing_word_pause_min, CFG.typing_word_pause_max
                    ))
                    self._maybe_micro_pause(end_time)
                elif ch in ".,;:!?":
                    control.type_keys(ch, with_spaces=True, with_newlines=False)
                    buf = []
                    time.sleep(random.uniform(0.2, 0.55))
                else:
                    control.type_keys(ch, with_spaces=True, with_newlines=False)
                    time.sleep(self._type_delay())
                    self._maybe_hesitate(end_time)
            if buf:
                control.type_keys("".join(buf), with_spaces=True, with_newlines=False)
        self._guarded(_do, "type_into_control")

    def hotkey(self, *keys: str) -> None:
        self._guarded(lambda: pyautogui.hotkey(*keys), f"hotkey{keys}")

    def send_win_keys(self, seq: str) -> None:
        self._guarded(lambda: uia_send_keys(seq), f"send_keys({seq})")

    def scroll(self, clicks: int) -> None:
        self._guarded(lambda: pyautogui.scroll(clicks), "scroll")

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
            with self.monitor.input_shield():
                pyautogui.moveRel(dx, dy, duration=0.02)
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
                 engine: WorkflowEngine,
                 docs: DocLibrary):
        self.monitor = monitor
        self.wm = wm
        self.actions = actions
        self.engine = engine
        self.docs = docs

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
                        sleep_responsive(random.uniform(0.5, 3.0), end_time, self.monitor)
                elif action == "jitter":
                    self.actions.jitter(end_time=end_time)
                elif action == "scroll":
                    x, y = random_point(w, h)
                    if self.actions.smooth_move(x, y, end_time=end_time):
                        self.actions.scroll(random.choice([-1, 1]) * random.randint(50, 300))
                elif action == "rest":
                    sleep_responsive(random.uniform(0.5, 3.0), end_time, self.monitor)
            except pyautogui.FailSafeException:
                log.info("fail-safe triggered")
                return


class SessionRunner:
    def __init__(self,
                 monitor: UserActivityMonitor,
                 wm: WindowManager,
                 actions: Actions,
                 engine: WorkflowEngine,
                 docs: DocLibrary):
        self.monitor = monitor
        self.wm = wm
        self.actions = actions
        self.engine = engine
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

    def run(self, duration_seconds: float) -> None:
        end_time = time.time() + duration_seconds
        log.info("session runner started")

        while time.time() < end_time and not self.engine.stopped():
            if not self.monitor.wait_until_user_idle(end_time):
                break
            if self.monitor.user_is_active():
                continue

            apps_this_session = random.randint(
                CFG.session_min_apps, CFG.session_max_apps
            )
            choices = random.sample(
                self._available_recipes(),
                k=min(apps_this_session, len(self._recipes)),
            )
            log.info(f"session plan: {choices}")

            for app_name in choices:
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
                            lambda t=text: self.actions.type_text(t),
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
                        self.actions.type_text(name)
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


def wait_for_idle_start(monitor: UserActivityMonitor, end_time: float) -> bool:
    log.info(f"waiting for {CFG.idle_threshold:.0f}s idle before starting")
    while monitor.user_is_active():
        if time.time() >= end_time:
            return False
        time.sleep(0.05)
    log.info("idle confirmed - starting")
    return True


def main() -> None:
    log.info("=" * 60)
    log.info("logbot starting")

    docs = DocLibrary(CFG.docs_dir, CFG.docs_glob)
    docs.refresh()

    monitor = UserActivityMonitor()
    wm = WindowManager(monitor)
    actions = Actions(monitor, wm)
    freeze_guard = FreezeGuard(monitor, actions)
    engine = WorkflowEngine(monitor, wm, actions, freeze_guard)
    resources = ResourceManager(monitor, wm)
    workflows = Workflows(monitor, wm, actions, engine, docs)
    runner = SessionRunner(monitor, wm, actions, engine, docs)

    monitor.start()
    freeze_guard.start()
    resources.start()

    try:
        duration_hours = float(input("Enter duration in hours: "))
        duration_seconds = duration_hours * 3600
        end_time = time.time() + duration_seconds

        time.sleep(CFG.startup_grace)
        monitor.arm()

        if not wait_for_idle_start(monitor, end_time):
            log.info("aborted before start")
            return

        runner.run(duration_seconds)

    except KeyboardInterrupt:
        log.info("interrupted by user")
    except SafetyViolation as e:
        log.error(f"safety violation: {e}")
    except Exception as e:
        log.exception(f"fatal: {e}")
    finally:
        engine.stop()
        resources.stop()
        freeze_guard.stop()
        monitor.stop()
        log.info("logbot stopped")


if __name__ == "__main__":
    main()