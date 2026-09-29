"""Always-on-top Tkinter overlay: current card, prices, deal rating and a session log."""

from __future__ import annotations

import time
import tkinter as tk
from typing import Any, Callable

from prices import DEAL_FOREIGN, deal_for

# ------------------------------------------------------------------ constants

LANG_FLAGS = {
    "english": "\U0001f1fa\U0001f1f8",   # 🇺🇸
    "japanese": "\U0001f1ef\U0001f1f5",  # 🇯🇵
    "chinese": "\U0001f1e8\U0001f1f3",   # 🇨🇳
    "korean": "\U0001f1f0\U0001f1f7",    # 🇰🇷
}
UNKNOWN_FLAG = "\U0001f30d"              # 🌍

# deal key (see prices.rate_deal) -> (label, colour)
DEAL_STYLES = {
    "great":   ("\U0001f525 Great Deal", "#00e676"),           # 🔥
    "good":    ("✅ Good Deal", "#b2ff59"),
    "fair":    ("⚖️ Fair", "#ffab40"),
    "over":    ("⚠️ Overpriced", "#ff5252"),
    "none":    ("❓ No Price", "#9e9e9e"),
    "foreign": ("\U0001f30f Check foreign market", "#ffab40"),  # 🌏
}

LOG_SIZE = 20
BORDER_WIDTH = 3

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

# Widgets that keep their own mouse handling instead of dragging the window.
NO_DRAG_WIDGETS = (tk.Button, tk.Scrollbar, tk.Text)


def _fmt_price(v: float | None) -> str:
    if v is None:
        return "—"
    return f"${v:.2f}"


def _flag(card: dict[str, Any]) -> str:
    return LANG_FLAGS.get((card.get("language") or "English").lower(), UNKNOWN_FLAG)


class OverlayWindow:
    def __init__(self, root: tk.Tk, on_reset: Callable[[], None] | None = None) -> None:
        self.root = root
        self.monitor = None      # injected by main after construction
        self.on_reset = on_reset  # called after the windows close on "↺"
        self._paused = False
        self._last_update: float | None = None
        self._log: list[tuple[str, str]] = []   # (text, deal_key)
        self._border_wins: list[tk.Toplevel] = []
        self._drag_offset: tuple[int, int] | None = None

        self._build()
        self._tick_clock()

    # ================================================================= build

    def _build(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("Whatnot Pricer")
        win.geometry("320x560+20+20")
        win.wm_attributes("-topmost", True)
        win.wm_attributes("-alpha", 0.93)
        win.configure(bg=BG_DARK)
        win.resizable(False, True)
        win.protocol("WM_DELETE_WINDOW", self._on_close)
        self.win = win

        # Bound on the toplevel so it applies to every child widget;
        # _drag_start ignores buttons, the scrollbar and the log text.
        win.bind("<ButtonPress-1>", self._drag_start)
        win.bind("<B1-Motion>",     self._drag_move)

        self._build_header()
        self._build_status_bar()
        self._build_card_panel()
        self._build_price_panel()
        self._build_deal_row()
        tk.Frame(win, bg="#2a2a44", height=1).pack(fill="x", padx=10, pady=6)
        self._build_log_panel()

    # -------------------------------------------------------------- header

    def _build_header(self) -> None:
        hdr = tk.Frame(self.win, bg=BG_HEADER, pady=6)
        hdr.pack(fill="x")

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

    def _build_status_bar(self) -> None:
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

    def _build_card_panel(self) -> None:
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

    def _build_price_panel(self) -> None:
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
        row(frame, "Match:",  "_match_var",  FG_MID, font_size=8)

    # ----------------------------------------------------------- deal row

    def _build_deal_row(self) -> None:
        self._deal_frame = tk.Frame(self.win, bg=BG_DARK, pady=6)
        self._deal_frame.pack(fill="x", padx=8)
        self._deal_var = tk.StringVar(value="")
        self._deal_lbl = tk.Label(
            self._deal_frame, textvariable=self._deal_var,
            bg=BG_DARK, fg=FG_DIM, font=("Segoe UI", 13, "bold"),
        )
        self._deal_lbl.pack()

    # ----------------------------------------------------------- log panel

    def _build_log_panel(self) -> None:
        hdr = tk.Frame(self.win, bg=BG_DARK)
        hdr.pack(fill="x", padx=10)
        tk.Label(hdr, text=f"Session Log  (last {LOG_SIZE})",
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
        for key, (_, color) in DEAL_STYLES.items():
            self._log_text.tag_config(key, foreground=color)

    # ================================================================= drag

    def _drag_start(self, event: tk.Event) -> None:
        if isinstance(event.widget, NO_DRAG_WIDGETS):
            self._drag_offset = None
            return
        self._drag_offset = (event.x_root - self.win.winfo_x(),
                             event.y_root - self.win.winfo_y())

    def _drag_move(self, event: tk.Event) -> None:
        if self._drag_offset is None:
            return
        dx, dy = self._drag_offset
        self.win.geometry(f"+{event.x_root - dx}+{event.y_root - dy}")

    # ================================================================= clock

    def _tick_clock(self) -> None:
        if self._last_update:
            secs = int(time.time() - self._last_update)
            self._time_var.set(f"updated {secs}s ago")
        self.win.after(1000, self._tick_clock)

    # ================================================================= public API

    def set_status(self, msg: str) -> None:
        self._status_var.set(msg)

    def update_card(self, card_data: dict[str, Any], price_data: dict[str, Any] | None) -> None:
        """Show a detected card and its price lookup. Must run on the Tk thread."""
        self._last_update = time.time()
        price_data = price_data or {}

        card_name  = card_data.get("cardName") or "Unknown"
        is_holo    = card_data.get("isHolo", False)
        is_graded  = card_data.get("isGraded", False)
        grade_co   = card_data.get("gradingCompany") or ""
        grade      = card_data.get("grade")
        stream_p   = card_data.get("streamPrice")
        confidence = card_data.get("confidence", "low")
        set_name   = card_data.get("setName") or ""
        card_num   = card_data.get("cardNumber") or ""
        condition  = card_data.get("condition") or ""

        # ---- name line ----
        conf_tag = " (?)" if confidence == "low" else ""
        self._name_var.set(f"{_flag(card_data)}  {card_name}{conf_tag}")

        # ---- meta line ----
        badges = []
        if is_holo:
            badges.append("✨Holo")
        if is_graded and grade_co:
            grade_txt = f"{grade:g}" if isinstance(grade, (int, float)) else ""
            badges.append(f"\U0001f4cb{grade_co} {grade_txt}".rstrip())
        parts = [p for p in [set_name, f"#{card_num}" if card_num else "",
                              condition] if p] + badges
        self._meta_var.set("  ·  ".join(parts))

        # ---- stream price ----
        self._stream_var.set(_fmt_price(stream_p))

        # ---- TCG prices ----
        deal_key, market = deal_for(card_data, price_data)
        best = price_data.get("best_match") or {}
        error = price_data.get("error")

        # which printing the price belongs to, e.g. "Base #4 · holofoil"
        match = "—"
        if best:
            match = f"{best.get('set') or '?'} #{best.get('number') or '?'}"
            if best.get("price_type"):
                match += f" · {best['price_type']}"
        self._match_var.set(match)

        if deal_key == DEAL_FOREIGN:
            self._market_var.set("⚠️ Foreign card")
            self._range_var.set("TCGPlayer = English only")
        elif market is not None:
            p = best["prices"]
            self._market_var.set(_fmt_price(market))
            low, high = p.get("low"), p.get("high")
            self._range_var.set(
                f"{_fmt_price(low)} – {_fmt_price(high)}"
                if low and high else _fmt_price(low or high)
            )
        else:
            self._market_var.set("—")
            self._range_var.set("Price lookup failed" if error else "Not on TCGPlayer")

        if deal_key == DEAL_FOREIGN or market is not None:
            label, color = DEAL_STYLES[deal_key]
            self._deal_var.set(label)
            self._deal_lbl.config(fg=color)
        else:
            self._deal_var.set("")

        self.set_status(f"⚠️ Price lookup: {error}" if error else f"✅ {card_name}")
        self._push_log(card_data, deal_key, market)

    def show_region_border(self, region: dict[str, int]) -> None:
        """Outline the captured region with four thin always-on-top strips.

        The strips sit just outside the region, so they never show up in the
        captured frames, and they need no transparent-window support, so they
        work on Windows, macOS and Linux alike.
        """
        w = BORDER_WIDTH
        left, top = region["left"], region["top"]
        width, height = region["width"], region["height"]
        edges = [
            (left - w, top - w, width + 2 * w, w),   # top
            (left - w, top + height, width + 2 * w, w),  # bottom
            (left - w, top, w, height),              # left
            (left + width, top, w, height),          # right
        ]
        for x, y, ew, eh in edges:
            strip = tk.Toplevel(self.root)
            strip.overrideredirect(True)
            strip.wm_attributes("-topmost", True)
            strip.configure(bg=BORDER_COLOR)
            strip.geometry(f"{ew}x{eh}+{x}+{y}")
            self._border_wins.append(strip)

    # ================================================================ internal

    def _push_log(self, card_data: dict[str, Any], deal_key: str, market: float | None) -> None:
        card_name = card_data.get("cardName") or "Unknown"
        label = DEAL_STYLES[deal_key][0]
        line = (
            f"{_flag(card_data)} {card_name}\n"
            f"  {_fmt_price(card_data.get('streamPrice'))} stream  |  "
            f"{_fmt_price(market)} mkt  {label}\n"
        )
        self._log.append((line, deal_key))
        if len(self._log) > LOG_SIZE:
            self._log.pop(0)

        self._log_text.config(state="normal")
        self._log_text.delete("1.0", "end")
        for text, tag in reversed(self._log):
            self._log_text.insert("end", text, tag)
        self._log_text.config(state="disabled")

    def _toggle_pause(self) -> None:
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

    def _reset_region(self) -> None:
        if self.monitor:
            self.monitor.stop()
        self.root.destroy()   # also closes the overlay and border windows
        if self.on_reset:
            self.on_reset()

    def _on_close(self) -> None:
        if self.monitor:
            self.monitor.stop()
        self.root.destroy()
