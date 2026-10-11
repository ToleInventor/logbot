import os
import sys
import time
import math
import random
import threading
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Callable

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
        self._guarded(lambda: pyautogui.typewrite(text, interval=0.03), "type_text")

    def type_into_control(self, control, text: str) -> None:
        def _do():
            control.wait("visible enabled ready", timeout=CFG.control_timeout)
            control.set_focus()
            control.type_keys(text, with_spaces=True, with_newlines=True)
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
                with self.wm._lock:
                    self.wm._opened_by_bot.discard(pid)
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
                with self.wm._lock:
                    self.wm._opened_by_bot.discard(pid)

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
            with self.wm._lock:
                self.wm._opened_by_bot.discard(pid)


class FreezeGuard:
    def __init__(self, monitor: UserActivityMonitor, actions: Actions):
        self.monitor = monitor
        self.actions = actions
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_tick = time.time()
        self._last_heartbeat = time.time()

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

    def notepad_session(self, duration_seconds: float) -> None:
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
            log.warning(f"could not resolve edit control: {e}")

        end_time = time.time() + duration_seconds
        lines = [
            "logbot session start",
            f"timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            "human supervised automation",
        ]
        for line in lines:
            if time.time() >= end_time or self.engine.stopped():
                break
            if self.monitor.user_is_active():
                self.monitor.wait_until_user_idle(end_time)
                continue
            if edit is not None:
                self.engine.run_step(
                    f"type '{line[:20]}'",
                    lambda l=line: self.actions.type_into_control(edit, l + "\n"),
                )
            else:
                self.engine.run_step(
                    f"type '{line[:20]}'",
                    lambda l=line: self.actions.type_text(l + "\n"),
                )
            sleep_responsive(random.uniform(0.4, 1.2), end_time, self.monitor)

        if self.engine.stopped():
            return

        out = SafetyGuard.resolve_path(CFG.logbot_dir / f"session_{int(time.time())}.txt")

        def _save():
            self.actions.open_save_as_dialog()
            time.sleep(0.8)
            self.actions.type_text(str(out))
            time.sleep(0.3)
            self.actions.accept_dialog()

        def _verify():
            return out.exists()

        self.engine.run_step("save as", _save, verify=_verify, timeout=10)

        if not out.exists():
            log.warning("save verification failed; leaving notepad open for inspection")

    def explorer_session(self, duration_seconds: float) -> None:
        target = CFG.logbot_dir
        target.mkdir(parents=True, exist_ok=True)
        proc = self.wm.launch("explorer.exe", [str(target)])
        try:
            win = self.wm.wait_for_window(r".*logbot.*", process_id=proc.pid, timeout=10)
        except TimeoutError:
            try:
                win = self.wm.wait_for_window(r".*logbot.*", timeout=10)
            except TimeoutError as e:
                log.error(f"explorer window not found: {e}")
                return
        self.wm.activate(win)
        self.wm.maximize(win)

        end_time = time.time() + duration_seconds
        while time.time() < end_time and not self.engine.stopped():
            if not self.monitor.wait_until_user_idle(end_time):
                break
            if self.monitor.user_is_active():
                continue
            self.actions.jitter(end_time=end_time)
            sleep_responsive(random.uniform(1.0, 3.0), end_time, self.monitor)


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

    monitor = UserActivityMonitor()
    wm = WindowManager(monitor)
    actions = Actions(monitor, wm)
    freeze_guard = FreezeGuard(monitor, actions)
    engine = WorkflowEngine(monitor, wm, actions, freeze_guard)
    resources = ResourceManager(monitor, wm)
    workflows = Workflows(monitor, wm, actions, engine)

    monitor.start()
    freeze_guard.start()
    resources.start()

    try:
        duration_hours = float(input("Enter duration in hours: "))
        mode = input("Mode: (1) wander  (2) notepad  (3) explorer  [1]: ").strip() or "1"
        duration_seconds = duration_hours * 3600
        end_time = time.time() + duration_seconds

        time.sleep(CFG.startup_grace)
        monitor.arm()

        if not wait_for_idle_start(monitor, end_time):
            log.info("aborted before start")
            return

        if mode == "2":
            workflows.notepad_session(duration_seconds)
        elif mode == "3":
            workflows.explorer_session(duration_seconds)
        else:
            workflows.wander(duration_seconds)

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