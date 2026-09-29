"""Whatnot Pricer entry point: load settings, pick the stream region, then run
the overlay window and the capture thread."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parent
ENV_FILE = APP_DIR / ".env"
REGION_FILE = APP_DIR / "region.json"
REGION_KEYS = ("left", "top", "width", "height")
MIN_REGION_SIZE = 50  # px; smaller drags are ignored

log = logging.getLogger("whatnot_pricer")


class RegionSelector:
    """Full-screen, dimmed screenshot of the primary monitor to drag a box on."""

    def __init__(self) -> None:
        self.region: dict[str, int] | None = None
        self._start_x: int | None = None
        self._start_y: int | None = None
        self._rect = None

    def select(self) -> dict[str, int] | None:
        import tkinter as tk

        import mss
        from PIL import Image, ImageTk

        with mss.MSS() as sct:
            mon = sct.monitors[1]  # Primary monitor
            shot = sct.grab(mon)
            img = Image.frombytes("RGB", shot.size, shot.rgb)

        screen_w, screen_h = img.width, img.height
        offset_x, offset_y = mon["left"], mon["top"]

        root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.geometry(f"{screen_w}x{screen_h}+{offset_x}+{offset_y}")
        root.configure(cursor="crosshair")

        img_tk = ImageTk.PhotoImage(img)

        canvas = tk.Canvas(root, width=screen_w, height=screen_h,
                           highlightthickness=0, cursor="crosshair")
        canvas.pack()
        canvas.create_image(0, 0, anchor="nw", image=img_tk)
        canvas.create_rectangle(0, 0, screen_w, screen_h,
                                fill="black", stipple="gray50", outline="")
        canvas.create_text(
            screen_w // 2, 44,
            text="Drag to select the Whatnot stream region  •  Esc to cancel",
            fill="white", font=("Arial", 14, "bold"),
        )

        sel = self

        def on_press(e):
            sel._start_x, sel._start_y = e.x, e.y
            if sel._rect:
                canvas.delete(sel._rect)
            sel._rect = None

        def on_drag(e):
            if sel._start_x is None:
                return
            if sel._rect:
                canvas.delete(sel._rect)
            sel._rect = canvas.create_rectangle(
                sel._start_x, sel._start_y, e.x, e.y,
                outline="#00ff44", width=2,
            )

        def on_release(e):
            if sel._start_x is None:
                return
            x1, x2 = sorted([sel._start_x, e.x])
            y1, y2 = sorted([sel._start_y, e.y])
            if x2 - x1 > MIN_REGION_SIZE and y2 - y1 > MIN_REGION_SIZE:
                sel.region = {
                    "left": x1 + offset_x,
                    "top": y1 + offset_y,
                    "width": x2 - x1,
                    "height": y2 - y1,
                }
                root.destroy()

        def on_esc(e):
            root.destroy()

        canvas.bind("<ButtonPress-1>", on_press)
        canvas.bind("<B1-Motion>", on_drag)
        canvas.bind("<ButtonRelease-1>", on_release)
        root.bind("<Escape>", on_esc)
        root.mainloop()
        return self.region


# ================================================================== settings

def load_env(env_file: Path = ENV_FILE) -> None:
    """Load .env from the project folder. Variables already set in the
    environment take precedence over the file."""
    load_dotenv(env_file, override=False)


def load_region(path: Path = REGION_FILE) -> dict[str, int] | None:
    """Saved capture region, or None if the file is missing or invalid."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        region = {key: int(data[key]) for key in REGION_KEYS}
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError, KeyError) as exc:
        log.warning("Ignoring invalid %s (%s)", path.name, exc)
        return None
    if region["width"] < MIN_REGION_SIZE or region["height"] < MIN_REGION_SIZE:
        log.warning("Ignoring %s: region is too small", path.name)
        return None
    return region


def save_region(region: dict[str, int], path: Path = REGION_FILE) -> None:
    path.write_text(json.dumps(region), encoding="utf-8")


def restart_with_new_region() -> None:
    """Forget the saved region and relaunch, so the selector shows again."""
    REGION_FILE.unlink(missing_ok=True)
    subprocess.Popen([sys.executable, str(APP_DIR / "main.py")], cwd=APP_DIR)


def show_missing_key_error() -> None:
    import tkinter as tk
    from tkinter import messagebox

    message = (
        "ANTHROPIC_API_KEY is not set.\n\n"
        f"Create a .env file in {APP_DIR} containing:\n"
        "ANTHROPIC_API_KEY=sk-ant-...\n\n"
        "or set it in your shell, e.g. (PowerShell):\n"
        "$env:ANTHROPIC_API_KEY = 'sk-ant-...'"
    )
    log.error("ANTHROPIC_API_KEY is not set (see README: create a .env file)")
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror("Missing API Key", message)
    root.destroy()


# ====================================================================== main

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    load_env()
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        show_missing_key_error()
        sys.exit(1)

    import tkinter as tk

    from monitor import Monitor
    from overlay import OverlayWindow

    region = load_region()
    if region:
        log.info("Loaded saved region: %s", region)
    else:
        log.info("Select the stream region on screen...")
        region = RegionSelector().select()
        if not region:
            log.info("No region selected, exiting.")
            sys.exit(0)
        save_region(region)
        log.info("Region saved: %s", region)

    root = tk.Tk()
    root.withdraw()

    overlay = OverlayWindow(root, on_reset=restart_with_new_region)

    def post(callback) -> None:
        """Run callback on the Tk thread; ignore it once the window is gone."""
        try:
            root.after(0, callback)
        except (RuntimeError, tk.TclError):
            pass

    def on_result(card_data, price_data):
        post(lambda: overlay.update_card(card_data, price_data))

    def on_status(msg):
        post(lambda: overlay.set_status(msg))

    monitor = Monitor(region, on_result, on_status)
    log.info("Using model %s", monitor.model)
    overlay.monitor = monitor
    overlay.show_region_border(region)
    monitor.start()

    try:
        root.mainloop()
    finally:
        monitor.stop()


if __name__ == "__main__":
    main()
