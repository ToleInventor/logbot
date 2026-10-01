import pyautogui
import random
import time
import math

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.01 


def get_screen_size():
    return pyautogui.size()


def random_point(width, height, margin=50):
    x = random.randint(margin, width - margin)
    y = random.randint(margin, height - margin)
    return x, y


def smooth_move(target_x, target_y, duration=None):
    if duration is None:
        duration = random.uniform(0.3, 1.5)

    start_x, start_y = pyautogui.position()

    ctrl_x = (start_x + target_x) / 2 + random.randint(-200, 200)
    ctrl_y = (start_y + target_y) / 2 + random.randint(-200, 200)

    steps = max(int(duration * 60), 10)  # ~60 FPS

    for i in range(steps + 1):
        t = i / steps
        x = (1 - t) ** 2 * start_x + 2 * (1 - t) * t * ctrl_x + t ** 2 * target_x
        y = (1 - t) ** 2 * start_y + 2 * (1 - t) * t * ctrl_y + t ** 2 * target_y
        pyautogui.moveTo(int(x), int(y))
        time.sleep(duration / steps)


def jitter(count=None):
    if count is None:
        count = random.randint(5, 25)

    for _ in range(count):
        dx = random.randint(-4, 4)
        dy = random.randint(-4, 4)
        x, y = pyautogui.position()
        pyautogui.moveRel(dx, dy, duration=0.02)


def random_scroll():
    w, h = get_screen_size()
    x, y = random_point(w, h)
    smooth_move(x, y, duration=random.uniform(0.2, 0.8))

    time.sleep(random.uniform(0.2, 1.0))

    clicks = random.randint(1, 15)
    direction = random.choice([-1, 1])

    for _ in range(clicks):
        pyautogui.scroll(direction * random.randint(50, 300))
        time.sleep(random.uniform(0.02, 0.15))


def hover(x, y, seconds=None):
    if seconds is None:
        seconds = random.uniform(0.5, 3.0)
    smooth_move(x, y)
    time.sleep(seconds)


def random_wander(duration_seconds=60):
    w, h = get_screen_size()
    end_time = time.time() + duration_seconds

    print(f"Bot running for {duration_seconds}s. Slam mouse into a corner to stop.")

    while time.time() < end_time:
        action = random.choices(
            ["move", "hover", "jitter", "scroll", "rest"],
            weights=[35, 25, 15, 15, 10],
            k=1,
        )[0]

        try:
            if action == "move":
                x, y = random_point(w, h)
                smooth_move(x, y)

            elif action == "hover":
                x, y = random_point(w, h)
                hover(x, y)

            elif action == "jitter":
                jitter()

            elif action == "scroll":
                random_scroll()

            elif action == "rest":
                time.sleep(random.uniform(0.5, 3.0))

        except pyautogui.FailSafeException:
            print("Fail-safe triggered. Stopping bot.")
            return


if __name__ == "__main__":
    try:
        duration = int(input("Enter duration in hours: "))
        random_wander(duration_seconds=duration * 3600)
    except KeyboardInterrupt:
        print("\nStopped by user (Ctrl+C).")