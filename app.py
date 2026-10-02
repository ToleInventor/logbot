import pyautogui
import random
import time

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.01


def wait_for_target_click():
    print("\n" + "=" * 60)
    print("  CLICK THE BUTTON YOU WANT THE BOT TO AUTO-CLICK")
    print("  The bot will record that exact position.")
    print("=" * 60)
    print("Waiting for your click...\n")

    try:
        from pynput import mouse
        captured = {}

        def on_click(x, y, button, pressed):
            if pressed and button == mouse.Button.left:
                captured["x"] = x
                captured["y"] = y
                return False

        with mouse.Listener(on_click=on_click) as listener:
            listener.join()

        print(f"Target recorded at ({captured['x']}, {captured['y']})\n")
        return captured["x"], captured["y"]

    except ImportError:
        print("(pynput not found - falling back to 'hover and press Enter' mode)")
        print("Hover your mouse over the button, then press Enter here.")
        input()
        x, y = pyautogui.position()
        print(f"Target recorded at ({x}, {y})\n")
        return x, y


def do_click(x, y):
    pyautogui.moveTo(x, y, duration=random.uniform(0.2, 0.6))
    time.sleep(random.uniform(0.1, 0.4))
    pyautogui.click()
    print(f"  -> Clicked at ({x}, {y})")


def sleep_responsive(seconds, end_time):
    slept = 0.0
    while slept < seconds:
        if time.time() >= end_time:
            return False
        chunk = min(1.0, seconds - slept)
        time.sleep(chunk)
        slept += chunk
    return True


def run_click_loop(x, y, duration_seconds):
    end_time = time.time() + duration_seconds
    click_count = 0
    long_wait = True

    print("Bot running. Slam mouse into a corner to stop.\n")

    while time.time() < end_time:
        if long_wait:
            wait_seconds = random.randint(10 * 60, 50 * 60)
            mins = wait_seconds // 60
            secs = wait_seconds % 60
            print(f"[Cycle] Waiting {mins}m {secs}s before next click\n")
        else:
            wait_seconds = random.uniform(3, 15)
            print(f"[Cycle] Waiting {wait_seconds:.1f}s before next click\n")

        if not sleep_responsive(wait_seconds, end_time):
            break

        click_count += 1
        print(f"[Click #{click_count}]")

        try:
            do_click(x, y)
        except pyautogui.FailSafeException:
            print("\nFail-safe triggered. Stopping bot.")
            return

        long_wait = not long_wait

    print(f"\nDone. Total clicks: {click_count}")


if __name__ == "__main__":
    try:
        duration_hours = float(input("Enter duration in hours: "))
        target_x, target_y = wait_for_target_click()

        print(f"Starting bot for {duration_hours} hour(s)...")
        print("Pattern: click -> wait 10-50 min -> click -> wait 3-15s -> repeat\n")

        run_click_loop(target_x, target_y, duration_seconds=duration_hours * 3600)

    except KeyboardInterrupt:
        print("\nStopped by user (Ctrl+C).")
