import pyautogui
import random
import time
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


def get_coordinates_from_user(monitor):
    print("\n" + "=" * 60)
    print("  HOVER OVER THE TARGET BUTTON")
    print("  Then press Enter here to capture the current mouse position.")
    print("=" * 60)

    input("\nPress Enter when your mouse is over the button...")
    time.sleep(0.2)

    x, y = pyautogui.position()
    print(f"\nCaptured position: ({x}, {y})")

    confirm = input("Use this position? (y/n): ").strip().lower()
    if confirm != "y":
        return get_coordinates_from_user(monitor)

    return x, y


def do_click(x, y, monitor, end_time):
    if not monitor.wait_until_user_idle(end_time):
        return False

    pyautogui.moveTo(x, y, duration=random.uniform(0.2, 0.6))

    if not monitor.wait_until_user_idle(end_time):
        return False

    time.sleep(random.uniform(0.1, 0.4))

    if not monitor.wait_until_user_idle(end_time):
        return False

    pyautogui.click()
    print(f"  -> Clicked at ({x}, {y})")
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


def run_click_loop(x, y, duration_seconds, monitor):
    end_time = time.time() + duration_seconds
    click_count = 0
    long_wait = True

    print("Bot running. Slam mouse into a corner to stop.")
    print(f"Manual override: any keyboard/mouse input pauses the bot; "
          f"it resumes after {IDLE_THRESHOLD:.0f}s of idle.\n")

    while time.time() < end_time:

        if not monitor.wait_until_user_idle(end_time):
            break

        if long_wait:
            wait_seconds = random.randint(10 * 60, 50 * 60)
            mins = wait_seconds // 60
            secs = wait_seconds % 60
            print(f"[Cycle] Waiting {mins}m {secs}s before next click\n")
        else:
            wait_seconds = random.uniform(3, 15)
            print(f"[Cycle] Waiting {wait_seconds:.1f}s before next click\n")

        if not sleep_responsive(wait_seconds, end_time, monitor):
            break

        if monitor.user_is_active():
            print("[OVERRIDE] Skipping click - user is active.\n")
            continue

        click_count += 1
        print(f"[Click #{click_count}]")

        try:
            if not do_click(x, y, monitor, end_time):
                break
        except pyautogui.FailSafeException:
            print("\nFail-safe triggered. Stopping bot.")
            return

        long_wait = not long_wait

    print(f"\nDone. Total clicks: {click_count}")


if __name__ == "__main__":
    monitor = UserActivityMonitor(idle_threshold=IDLE_THRESHOLD)
    monitor.start()

    try:
        duration_hours = float(input("Enter duration in hours: "))

        mode = input("Enter coordinates manually or hover-capture? (m/h): ").strip().lower()
        if mode == "m":
            x = int(input("Enter X coordinate: "))
            y = int(input("Enter Y coordinate: "))
        else:
            x, y = get_coordinates_from_user(monitor)

        print(f"\nTarget set to ({x}, {y})")
        print(f"Starting bot for {duration_hours} hour(s)...")
        print("Pattern: click -> wait 10-50 min -> click -> wait 3-15s -> repeat\n")
        print(">>> Manual override: move the mouse or press a key to take control.")
        print(">>> Bot resumes 5 seconds after you stop.\n")

        time.sleep(1.0)
        run_click_loop(x, y, duration_seconds=duration_hours * 3600, monitor=monitor)

    except KeyboardInterrupt:
        print("\nStopped by user (Ctrl+C).")
    finally:
        monitor.stop()
