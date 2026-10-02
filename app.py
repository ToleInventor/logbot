import pyautogui
import random
import time

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.01


def get_coordinates_from_user():
    print("\n" + "=" * 60)
    print("  HOVER OVER THE TARGET BUTTON")
    print("  Then press Enter here to capture the current mouse position.")
    print("=" * 60)

    input("\nPress Enter when your mouse is over the button...")
    x, y = pyautogui.position()
    print(f"\nCaptured position: ({x}, {y})")

    confirm = input("Use this position? (y/n): ").strip().lower()
    if confirm != "y":
        return get_coordinates_from_user()

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

        mode = input("Enter coordinates manually or hover-capture? (m/h): ").strip().lower()
        if mode == "m":
            x = int(input("Enter X coordinate: "))
            y = int(input("Enter Y coordinate: "))
        else:
            x, y = get_coordinates_from_user()

        print(f"\nTarget set to ({x}, {y})")
        print(f"Starting bot for {duration_hours} hour(s)...")
        print("Pattern: click -> wait 10-50 min -> click -> wait 3-15s -> repeat\n")

        run_click_loop(x, y, duration_seconds=duration_hours * 3600)

    except KeyboardInterrupt:
        print("\nStopped by user (Ctrl+C).")
