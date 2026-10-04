import pyautogui
import random
import time
import math
import threading
from pynput import mouse, keyboard

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.01

IDLE_THRESHOLD = 5.0


class UserActivityMonitor:
    def __init__(self, idle_threshold=IDLE_THRESHOLD):
        self.idle_threshold = idle_threshold
        self.last_input_time = 0.0
        self.user_in_control = False
        self._lock = threading.Lock()
        self._listeners = []

    def _on_user_input(self, *_args, **_kwargs):
        with self._lock:
            self.last_input_time = time.time()
            if not self.user_in_control:
                self.user_in_control = True
                print("\n[OVERRIDE] User took control - bot paused.")

    def start(self):
        kb = keyboard.Listener(
            on_press=self._on_user_input,
            on_release=self._on_user_input,
        )
        ms = mouse.Listener(
            on_move=self._on_user_input,
            on_click=self._on_user_input,
            on_scroll=self._on_user_input,
        )
        kb.daemon = True
        ms.daemon = True
        kb.start()
        ms.start()
        self._listeners = [kb, ms]

    def stop(self):
        for lst in self._listeners:
            try:
                lst.stop()
            except Exception:
                pass

    def user_is_active(self):
        with self._lock:
            last = self.last_input_time
        if last == 0.0:
            return False
        return (time.time() - last) < self.idle_threshold

    def wait_until_user_idle(self, end_time):
        was_active = False
        while self.user_is_active():
            if time.time() >= end_time:
                return False
            if not was_active:
                was_active = True
            time.sleep(0.05)
        if was_active:
            print("[OVERRIDE] User idle - bot resuming.\n")
        return True


def get_screen_size():
    return pyautogui.size()


def random_point(width, height, margin=50):
    x = random.randint(margin, width - margin)
    y = random.randint(margin, height - margin)
    return x, y


def smooth_move(target_x, target_y, duration=None, monitor=None, end_time=None):
    if duration is None:
        duration = random.uniform(0.3, 1.5)

    start_x, start_y = pyautogui.position()

    ctrl_x = (start_x + target_x) / 2 + random.randint(-200, 200)
    ctrl_y = (start_y + target_y) / 2 + random.randint(-200, 200)

    steps = max(int(duration * 60), 10)

    for i in range(steps + 1):
        if monitor is not None and end_time is not None:
            if time.time() >= end_time:
                return False
            if monitor.user_is_active():
                return False
        t = i / steps
        x = (1 - t) ** 2 * start_x + 2 * (1 - t) * t * ctrl_x + t ** 2 * target_x
        y = (1 - t) ** 2 * start_y + 2 * (1 - t) * t * ctrl_y + t ** 2 * target_y
        pyautogui.moveTo(int(x), int(y))
        time.sleep(duration / steps)
    return True


def jitter(count=None, monitor=None, end_time=None):
    if count is None:
        count = random.randint(5, 25)

    for _ in range(count):
        if monitor is not None and end_time is not None:
            if time.time() >= end_time:
                return False
            if monitor.user_is_active():
                return False
        dx = random.randint(-4, 4)
        dy = random.randint(-4, 4)
        pyautogui.moveRel(dx, dy, duration=0.02)
    return True


def random_scroll(monitor=None, end_time=None):
    w, h = get_screen_size()
    x, y = random_point(w, h)
    if not smooth_move(x, y, duration=random.uniform(0.2, 0.8), monitor=monitor, end_time=end_time):
        return False

    if monitor is not None and end_time is not None:
        if not sleep_responsive(random.uniform(0.2, 1.0), end_time, monitor):
            return False
        if monitor.user_is_active():
            return False
    else:
        time.sleep(random.uniform(0.2, 1.0))

    clicks = random.randint(1, 15)
    direction = random.choice([-1, 1])

    for _ in range(clicks):
        if monitor is not None and end_time is not None:
            if time.time() >= end_time:
                return False
            if monitor.user_is_active():
                return False
        pyautogui.scroll(direction * random.randint(50, 300))
        time.sleep(random.uniform(0.02, 0.15))
    return True


def hover(x, y, seconds=None, monitor=None, end_time=None):
    if seconds is None:
        seconds = random.uniform(0.5, 3.0)
    if not smooth_move(x, y, monitor=monitor, end_time=end_time):
        return False
    if monitor is not None and end_time is not None:
        return sleep_responsive(seconds, end_time, monitor)
    time.sleep(seconds)
    return True


def sleep_responsive(seconds, end_time, monitor):
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


def random_wander(duration_seconds=60, monitor=None):
    w, h = get_screen_size()
    end_time = time.time() + duration_seconds

    print(f"Bot running for {duration_seconds}s. Slam mouse into a corner to stop.")
    print(f"Manual override: any keyboard/mouse input pauses the bot; "
          f"it resumes after {IDLE_THRESHOLD:.0f}s of idle.\n")

    while time.time() < end_time:

        if not monitor.wait_until_user_idle(end_time):
            break

        if monitor.user_is_active():
            continue

        action = random.choices(
            ["move", "hover", "jitter", "scroll", "rest"],
            weights=[35, 25, 15, 15, 10],
            k=1,
        )[0]

        try:
            if action == "move":
                x, y = random_point(w, h)
                if not smooth_move(x, y, monitor=monitor, end_time=end_time):
                    if time.time() >= end_time:
                        break
                    continue

            elif action == "hover":
                x, y = random_point(w, h)
                if not hover(x, y, monitor=monitor, end_time=end_time):
                    if time.time() >= end_time:
                        break
                    continue

            elif action == "jitter":
                if not jitter(monitor=monitor, end_time=end_time):
                    if time.time() >= end_time:
                        break
                    continue

            elif action == "scroll":
                if not random_scroll(monitor=monitor, end_time=end_time):
                    if time.time() >= end_time:
                        break
                    continue

            elif action == "rest":
                if not sleep_responsive(random.uniform(0.5, 3.0), end_time, monitor):
                    break

        except pyautogui.FailSafeException:
            print("Fail-safe triggered. Stopping bot.")
            return


if __name__ == "__main__":
    monitor = UserActivityMonitor(idle_threshold=IDLE_THRESHOLD)
    monitor.start()

    try:
        duration = int(input("Enter duration in hours: "))
        time.sleep(1.0)
        random_wander(duration_seconds=duration * 3600, monitor=monitor)
    except KeyboardInterrupt:
        print("\nStopped by user (Ctrl+C).")
    finally:
        monitor.stop()