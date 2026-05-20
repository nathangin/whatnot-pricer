import time
import tkinter as tk

# ------------------------------------------------------------------ constants

LANG_FLAGS = {
    "english": "\U0001f1fa\U0001f1f8",   # 🇺🇸
    "japanese": "\U0001f1ef\U0001f1f5",  # 🇯🇵
    "chinese": "\U0001f1e8\U0001f1f3",   # 🇨🇳
    "korean": "\U0001f1f0\U0001f1f7",    # 🇰🇷
}

# ratio = stream_price / market_price
DEAL_BREAKPOINTS = [
    (0.80, "great", "\U0001f525 Great Deal", "#00e676"),   # 🔥
    (0.95, "good",  "✅ Good Deal",      "#b2ff59"),   # ✅
    (1.05, "fair",  "⚖️ Fair",     "#ffab40"),   # ⚖️
]
DEAL_OVER  = ("over",  "⚠️ Overpriced", "#ff5252")   # ⚠️
DEAL_NONE  = ("none",  "❓ No Price",  "#9e9e9e")           # ❓

# bg palette
BG_DARK   = "#12121f"
BG_PANEL  = "#1a1a30"
BG_HEADER = "#0d0d1f"
BG_ENTRY  = "#0a0a18"
FG_DIM    = "#6b6b8a"
FG_MID    = "#9999bb"
FG_LIGHT  = "#ccccee"
FG_WHITE  = "#ffffff"
ACCENT    = "#6c63ff"
CYAN      = "#00cfff"
BORDER_COLOR = "#00e676"


def _fmt_price(v) -> str:
    if v is None:
        return "—"
    return f"${v:.2f}"


class OverlayWindow:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.monitor = None      # injected by main after construction
        self._paused = False
        self._last_update: float | None = None
        self._log: list[tuple[str, str]] = []   # (text, deal_key)
        self._border_win: tk.Toplevel | None = None

        self._build()
        self._tick_clock()

    # ================================================================= build

    def _build(self):
        win = tk.Toplevel(self.root)
        win.title("Whatnot Pricer")
        win.geometry("320x540+20+20")
        win.wm_attributes("-topmost", True)
        win.wm_attributes("-alpha", 0.93)
        win.configure(bg=BG_DARK)
        win.resizable(False, True)
        win.protocol("WM_DELETE_WINDOW", self._on_close)
        self.win = win

        # drag support on the window background frames
        win.bind("<ButtonPress-1>",   self._drag_start)
        win.bind("<B1-Motion>",       self._drag_move)

        self._build_header()
        self._build_status_bar()
        self._build_card_panel()
        self._build_price_panel()
        self._build_deal_row()
        tk.Frame(win, bg="#2a2a44", height=1).pack(fill="x", padx=10, pady=6)
        self._build_log_panel()

    # -------------------------------------------------------------- header

    def _build_header(self):
        hdr = tk.Frame(self.win, bg=BG_HEADER, pady=6)
        hdr.pack(fill="x")
        hdr.bind("<ButtonPress-1>", self._drag_start)
        hdr.bind("<B1-Motion>",     self._drag_move)

        tk.Label(
            hdr, text="\U0001f3b4 Whatnot Pricer",
            bg=BG_HEADER, fg=ACCENT, font=("Segoe UI", 11, "bold"),
        ).pack(side="left", padx=10)

        btn_cfg = dict(bg="#1e1e3a", fg=FG_LIGHT, relief="flat",
                       font=("Segoe UI", 9), padx=6, pady=1,
                       activebackground="#2a2a4a", activeforeground=FG_WHITE,
                       bd=0, cursor="hand2")

        self._pause_btn = tk.Button(hdr, text="⏸", command=self._toggle_pause,
                                    **btn_cfg)
        self._pause_btn.pack(side="right", padx=4)

        tk.Button(hdr, text="↺", command=self._reset_region,
                  **btn_cfg).pack(side="right", padx=2)

    # ------------------------------------------------------------ status bar

    def _build_status_bar(self):
        bar = tk.Frame(self.win, bg="#0d0d24", pady=3)
        bar.pack(fill="x")

        self._status_var = tk.StringVar(value="\U0001f440 Watching...")
        tk.Label(bar, textvariable=self._status_var,
                 bg="#0d0d24", fg=FG_DIM, font=("Segoe UI", 8),
                 anchor="w").pack(side="left", padx=10)

        self._time_var = tk.StringVar(value="")
        tk.Label(bar, textvariable=self._time_var,
                 bg="#0d0d24", fg=FG_DIM, font=("Segoe UI", 8),
                 anchor="e").pack(side="right", padx=10)

    # ------------------------------------------------------------ card panel

    def _build_card_panel(self):
        frame = tk.Frame(self.win, bg=BG_PANEL, pady=8)
        frame.pack(fill="x", padx=8, pady=(6, 2))

        self._name_var = tk.StringVar(value="No card detected")
        tk.Label(
            frame, textvariable=self._name_var,
            bg=BG_PANEL, fg=FG_WHITE, font=("Segoe UI", 13, "bold"),
            wraplength=290, justify="left",
        ).pack(anchor="w", padx=10)

        self._meta_var = tk.StringVar(value="")
        tk.Label(
            frame, textvariable=self._meta_var,
            bg=BG_PANEL, fg=FG_MID, font=("Segoe UI", 8),
            wraplength=290, justify="left",
        ).pack(anchor="w", padx=10)

    # ------------------------------------------------------------ price panel

    def _build_price_panel(self):
        frame = tk.Frame(self.win, bg=BG_ENTRY, pady=8, padx=12)
        frame.pack(fill="x", padx=8, pady=2)

        def row(parent, label, var_attr, fg, font_size=11):
            r = tk.Frame(parent, bg=BG_ENTRY)
            r.pack(fill="x", pady=1)
            tk.Label(r, text=label, bg=BG_ENTRY, fg=FG_DIM,
                     font=("Segoe UI", 8), width=8, anchor="w").pack(side="left")
            v = tk.StringVar(value="—")
            setattr(self, var_attr, v)
            tk.Label(r, textvariable=v, bg=BG_ENTRY, fg=fg,
                     font=("Segoe UI", font_size, "bold")).pack(side="left", padx=6)

        row(frame, "Stream:", "_stream_var", FG_WHITE)
        row(frame, "Market:", "_market_var", CYAN)
        row(frame, "Range:",  "_range_var",  FG_MID, font_size=8)

    # ----------------------------------------------------------- deal row

    def _build_deal_row(self):
        self._deal_frame = tk.Frame(self.win, bg=BG_DARK, pady=6)
        self._deal_frame.pack(fill="x", padx=8)
        self._deal_var = tk.StringVar(value="")
        self._deal_lbl = tk.Label(
            self._deal_frame, textvariable=self._deal_var,
            bg=BG_DARK, fg=FG_DIM, font=("Segoe UI", 13, "bold"),
        )
        self._deal_lbl.pack()

    # ----------------------------------------------------------- log panel

    def _build_log_panel(self):
        hdr = tk.Frame(self.win, bg=BG_DARK)
        hdr.pack(fill="x", padx=10)
        tk.Label(hdr, text="Session Log  (last 20)",
                 bg=BG_DARK, fg=FG_DIM, font=("Segoe UI", 7, "bold")).pack(side="left")

        container = tk.Frame(self.win, bg=BG_DARK)
        container.pack(fill="both", expand=True, padx=8, pady=(4, 8))

        sb = tk.Scrollbar(container, troughcolor=BG_DARK, bg=BG_PANEL,
                          relief="flat", bd=0)
        sb.pack(side="right", fill="y")

        self._log_text = tk.Text(
            container, bg=BG_ENTRY, fg=FG_MID,
            font=("Courier New", 8), height=6,
            wrap="word", state="disabled", relief="flat",
            highlightthickness=0, yscrollcommand=sb.set,
            padx=6, pady=4,
        )
        self._log_text.pack(side="left", fill="both", expand=True)
        sb.config(command=self._log_text.yview)

        # colour tags for deal classes
        self._log_text.tag_config("great", foreground="#00e676")
        self._log_text.tag_config("good",  foreground="#b2ff59")
        self._log_text.tag_config("fair",  foreground="#ffab40")
        self._log_text.tag_config("over",  foreground="#ff5252")
        self._log_text.tag_config("none",  foreground="#9e9e9e")
        self._log_text.tag_config("bold",  font=("Courier New", 8, "bold"))

    # ================================================================= drag

    def _drag_start(self, event):
        self._dx = event.x
        self._dy = event.y

    def _drag_move(self, event):
        x = self.win.winfo_x() + (event.x - self._dx)
        y = self.win.winfo_y() + (event.y - self._dy)
        self.win.geometry(f"+{x}+{y}")

    # ================================================================= clock

    def _tick_clock(self):
        if self._last_update:
            secs = int(time.time() - self._last_update)
            self._time_var.set(f"updated {secs}s ago")
        self.win.after(1000, self._tick_clock)

    # ================================================================= public API

    def set_status(self, msg: str):
        self._status_var.set(msg)

    def update_card(self, card_data: dict, price_data: dict):
        self._last_update = time.time()

        card_name  = card_data.get("cardName", "Unknown")
        language   = card_data.get("language", "English").lower()
        flag       = LANG_FLAGS.get(language, "\U0001f30d")
        is_holo    = card_data.get("isHolo", False)
        is_graded  = card_data.get("isGraded", False)
        grade_co   = card_data.get("gradingCompany") or ""
        grade      = card_data.get("grade")
        stream_p   = card_data.get("streamPrice")
        confidence = card_data.get("confidence", "low")
        set_name   = card_data.get("setName", "")
        card_num   = card_data.get("cardNumber", "")
        condition  = card_data.get("condition", "")

        # ---- name line ----
        conf_tag = " (?)" if confidence == "low" else ""
        self._name_var.set(f"{flag}  {card_name}{conf_tag}")

        # ---- meta line ----
        badges = []
        if is_holo:
            badges.append("✨Holo")
        if is_graded and grade_co:
            badges.append(f"\U0001f4cb{grade_co} {grade or ''}")
        parts = [p for p in [set_name, f"#{card_num}" if card_num else "",
                              condition] if p] + badges
        self._meta_var.set("  ·  ".join(parts))

        # ---- stream price ----
        self._stream_var.set(_fmt_price(stream_p))

        # ---- TCG prices ----
        is_foreign = (price_data or {}).get("is_foreign", False)
        best = (price_data or {}).get("best_match")

        if is_foreign:
            self._market_var.set("⚠️ Foreign card")
            self._range_var.set("TCGPlayer = English only")
            self._deal_var.set("\U0001f30f Check foreign market")
            self._deal_lbl.config(fg="#ffab40")
        elif best and best.get("prices", {}).get("market"):
            p = best["prices"]
            market = p["market"]
            self._market_var.set(_fmt_price(market))
            low, high = p.get("low"), p.get("high")
            self._range_var.set(
                f"{_fmt_price(low)} – {_fmt_price(high)}"
                if low and high else _fmt_price(low or high)
            )
            deal_key, label, color = self._deal(stream_p, market)
            self._deal_var.set(label)
            self._deal_lbl.config(fg=color)
        else:
            self._market_var.set("—")
            self._range_var.set("Not on TCGPlayer")
            self._deal_var.set("")

        self.set_status(f"✅ {card_name}")
        self._push_log(card_data, price_data)

    def show_region_border(self, region: dict):
        bw = tk.Toplevel(self.root)
        bw.overrideredirect(True)
        bw.wm_attributes("-topmost", True)
        # Windows-only: make a specific colour fully transparent
        bw.wm_attributes("-transparentcolor", "#010101")
        bw.configure(bg="#010101")
        bw.geometry(
            f"{region['width']}x{region['height']}"
            f"+{region['left']}+{region['top']}"
        )
        cv = tk.Canvas(bw, bg="#010101", highlightthickness=0,
                       width=region["width"], height=region["height"])
        cv.pack()
        # Thin border rectangle — interior is transparent (#010101)
        cv.create_rectangle(
            2, 2, region["width"] - 2, region["height"] - 2,
            outline=BORDER_COLOR, width=3,
        )
        self._border_win = bw

    # ================================================================ internal

    def _deal(self, stream_p, market_p):
        if stream_p is None or market_p is None or market_p == 0:
            return (DEAL_NONE[0], DEAL_NONE[1], DEAL_NONE[2])
        ratio = stream_p / market_p
        for threshold, key, label, color in DEAL_BREAKPOINTS:
            if ratio <= threshold:
                return key, label, color
        return (DEAL_OVER[0], DEAL_OVER[1], DEAL_OVER[2])

    def _push_log(self, card_data: dict, price_data: dict):
        card_name = card_data.get("cardName", "Unknown")
        language  = card_data.get("language", "English").lower()
        flag      = LANG_FLAGS.get(language, "\U0001f30d")
        stream_p  = card_data.get("streamPrice")
        best      = (price_data or {}).get("best_match")
        market    = (best or {}).get("prices", {}).get("market") if best else None
        deal_key, label, _ = self._deal(stream_p, market)

        line = (
            f"{flag} {card_name}\n"
            f"  {_fmt_price(stream_p)} stream  |  {_fmt_price(market)} mkt  {label}\n"
        )
        self._log.append((line, deal_key))
        if len(self._log) > 20:
            self._log.pop(0)

        self._log_text.config(state="normal")
        self._log_text.delete("1.0", "end")
        for text, tag in reversed(self._log):
            self._log_text.insert("end", text, tag)
        self._log_text.config(state="disabled")

    def _toggle_pause(self):
        self._paused = not self._paused
        if self.monitor:
            if self._paused:
                self.monitor.pause()
                self._pause_btn.config(text="▶")
                self.set_status("⏸ Paused")
            else:
                self.monitor.resume()
                self._pause_btn.config(text="⏸")
                self.set_status("\U0001f440 Watching...")

    def _reset_region(self):
        import os, subprocess, sys
        if os.path.exists("region.json"):
            os.remove("region.json")
        if self._border_win:
            self._border_win.destroy()
        self.win.destroy()
        self.root.destroy()
        subprocess.Popen([sys.executable, "main.py"])

    def _on_close(self):
        if self.monitor:
            self.monitor.stop()
        self.root.destroy()
