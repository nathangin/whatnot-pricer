import tkinter as tk
from tkinter import messagebox
import json
import os
import sys

CONFIG_FILE = "region.json"


class RegionSelector:
    def __init__(self):
        self.region = None
        self._start_x = None
        self._start_y = None
        self._rect = None

    def select(self):
        import mss
        from PIL import Image, ImageTk

        with mss.mss() as sct:
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
            if x2 - x1 > 50 and y2 - y1 > 50:
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


def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "Missing API Key",
            "Set the ANTHROPIC_API_KEY environment variable and restart.\n\n"
            "Example (PowerShell):\n$env:ANTHROPIC_API_KEY = 'sk-ant-...'",
        )
        sys.exit(1)

    region = None
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                region = json.load(f)
            print(f"Loaded saved region: {region}")
        except Exception:
            pass

    if region is None:
        print("Select the stream region on screen...")
        sel = RegionSelector()
        region = sel.select()
        if not region:
            print("No region selected — exiting.")
            sys.exit(0)
        with open(CONFIG_FILE, "w") as f:
            json.dump(region, f)
        print(f"Region saved: {region}")

    from overlay import OverlayWindow
    from monitor import Monitor

    root = tk.Tk()
    root.withdraw()

    overlay = OverlayWindow(root)

    def on_result(card_data, price_data):
        root.after(0, lambda: overlay.update_card(card_data, price_data))

    def on_status(msg):
        root.after(0, lambda: overlay.set_status(msg))

    monitor = Monitor(region, on_result, on_status)
    overlay.monitor = monitor
    overlay.show_region_border(region)
    monitor.start()

    try:
        root.mainloop()
    finally:
        monitor.stop()


if __name__ == "__main__":
    main()
