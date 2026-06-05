"""FloatTranslate — a floating-window screen translator for Windows.

Position the transparent middle band over text in any app (game, image,
PDF, foreign UI). Click 翻译 for a one-shot translation, or toggle 自动 to
keep re-translating as the underlying text changes. Recognition uses the
built-in Windows OCR; translation uses a pluggable LLM provider.

Two display modes:
- Panel mode (default): the translation is shown in the bottom panel.
- Overlay mode (覆盖): each recognized line is covered with a white box and the
  translation is drawn in place, right over the original text.
"""
from __future__ import annotations

import ctypes
import threading
import tkinter as tk
from tkinter import ttk, font as tkfont

import mss
from PIL import Image

import ocr
import providers
from config import Config
from translator import Translator

# A sentinel colour rendered by our own window is turned fully transparent and
# click-through by Windows. The capture band is filled with it so we can both
# see and screenshot whatever lies behind it.
TRANSPARENT = "#FF00FE"

BAR_BG = "#000000"
BAR_FG = "#e8eaed"
RESULT_BG = "#000000"
RESULT_FG = "#f1f3f4"
ACCENT = "#3b6ef5"
# Solid gray veil over the capture region, drawn in a separate overlay window
# with real per-window transparency (so it's a clean solid colour, not a
# dithered pattern). It's click-through and is flashed invisible at the moment
# of capture so OCR still sees the content behind it.
VEIL = "#808080"
VEIL_ALPHA = 0.4


def _enable_dpi_awareness() -> None:
    """Make Tk coordinates map 1:1 to physical pixels so capture aligns."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor aware
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


class FloatTranslate:
    def __init__(self) -> None:
        self.cfg = Config.load()
        self._translator: Translator | None = None
        self._translator_signature: tuple | None = None
        self._busy = False
        self._auto = False
        self._auto_job: str | None = None
        self._last_image_hash: int | None = None
        self._last_text: str = ""
        self._minimized = False
        self._ball: tk.Toplevel | None = None
        self._veil_win: tk.Toplevel | None = None
        self._overlay_win: tk.Toplevel | None = None
        self._overlay_canvas: tk.Canvas | None = None

        self.root = tk.Tk()
        self.root.title("FloatTranslate")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=BAR_BG)
        self.root.geometry(self.cfg.geometry)
        # Make the sentinel colour transparent + click-through.
        self.root.attributes("-transparentcolor", TRANSPARENT)

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(60, self._create_overlays)

    # ---------------------------------------------------------------- UI ----
    def _build_ui(self) -> None:
        self.root.rowconfigure(1, weight=1)
        self.root.columnconfigure(0, weight=1)

        # --- control bar (draggable) ---
        bar = tk.Frame(self.root, bg=BAR_BG, height=34)
        bar.grid(row=0, column=0, sticky="ew")
        bar.grid_propagate(False)
        bar.columnconfigure(1, weight=1)

        grip = tk.Label(bar, text="⠿  悬浮翻译", bg=BAR_BG, fg=BAR_FG,
                        font=("Segoe UI", 10, "bold"), cursor="fleur")
        grip.grid(row=0, column=0, padx=(10, 6), sticky="w")

        self.status = tk.Label(bar, text="待命", bg=BAR_BG, fg="#9aa0a6",
                               font=("Segoe UI", 9))
        self.status.grid(row=0, column=1, sticky="w")

        btns = tk.Frame(bar, bg=BAR_BG)
        btns.grid(row=0, column=2, sticky="e", padx=6)

        self._mk_button(btns, "翻译", self.translate_once, accent=True).pack(
            side="left", padx=2)
        self.auto_btn = self._mk_button(btns, "自动:关", self.toggle_auto)
        self.auto_btn.pack(side="left", padx=2)
        self.overlay_btn = self._mk_button(
            btns, f"覆盖:{'开' if self.cfg.overlay_mode else '关'}", self.toggle_overlay)
        self.overlay_btn.pack(side="left", padx=2)
        self._mk_button(btns, "⚙", self.open_settings).pack(side="left", padx=2)
        self._mk_button(btns, "—", self.minimize).pack(side="left", padx=2)
        self._mk_button(btns, "×", self._on_close).pack(side="left", padx=2)

        for w in (bar, grip, self.status):
            w.bind("<Button-1>", self._start_move)
            w.bind("<B1-Motion>", self._on_move)

        # --- transparent capture band ---
        # The canvas body is the sentinel colour = a real see-through hole, so
        # screenshots grab whatever is behind the window. The gray tint is a
        # separate translucent overlay window (see _create_veil).
        self.capture = tk.Canvas(self.root, bg=TRANSPARENT, bd=0,
                                 highlightthickness=2, highlightbackground=ACCENT)
        self.capture.grid(row=1, column=0, sticky="nsew")
        self.capture.bind("<Configure>", self._on_capture_configure)

        # --- result panel ---
        result = tk.Frame(self.root, bg=RESULT_BG, height=120)
        result.grid(row=2, column=0, sticky="ew")
        result.grid_propagate(False)
        result.rowconfigure(0, weight=1)
        result.columnconfigure(0, weight=1)

        self.result_text = tk.Text(
            result, height=4, wrap="word", bg=RESULT_BG, fg=RESULT_FG,
            relief="flat", padx=10, pady=8, font=("Microsoft YaHei UI", 11),
            insertbackground=RESULT_FG, highlightthickness=0,
        )
        self.result_text.grid(row=0, column=0, sticky="nsew")
        self.result_text.insert("1.0", "把中间透明区域对准要翻译的文字，点击「翻译」。")
        self.result_text.configure(state="disabled")

        # --- resize grip (bottom-right) ---
        sizer = tk.Label(result, text="◢", bg=RESULT_BG, fg="#9aa0a6",
                         cursor="size_nw_se", font=("Segoe UI", 10))
        sizer.grid(row=0, column=1, sticky="se", padx=2, pady=2)
        sizer.bind("<Button-1>", self._start_resize)
        sizer.bind("<B1-Motion>", self._on_resize)

    def _mk_button(self, parent, text, cmd, accent=False) -> tk.Button:
        return tk.Button(
            parent, text=text, command=cmd,
            bg=ACCENT if accent else "#3c4043",
            fg="white", activebackground=ACCENT if accent else "#4a4e52",
            activeforeground="white", relief="flat", bd=0,
            font=("Segoe UI", 9), padx=8, pady=2, cursor="hand2",
        )

    # ------------------------------------------------------ move / resize ----
    def _start_move(self, e):
        self._mx, self._my = e.x_root, e.y_root
        self._ox, self._oy = self.root.winfo_x(), self.root.winfo_y()

    def _on_move(self, e):
        self.root.geometry(
            f"+{self._ox + (e.x_root - self._mx)}+{self._oy + (e.y_root - self._my)}")
        self._sync_overlays()

    def _start_resize(self, e):
        self._mx, self._my = e.x_root, e.y_root
        self._ow, self._oh = self.root.winfo_width(), self.root.winfo_height()

    def _on_resize(self, e):
        w = max(280, self._ow + (e.x_root - self._mx))
        h = max(220, self._oh + (e.y_root - self._my))
        self.root.geometry(f"{w}x{h}")
        self._sync_overlays()

    def _on_capture_configure(self, _e=None):
        self._sync_overlays()

    # --------------------------------------------------------- overlays ------
    def _create_overlays(self):
        """Create the two click-through child windows that sit over the capture
        region: a solid gray veil (normal mode) and the white-box translation
        overlay (overlay mode). Only one is shown at a time."""
        # Gray tint veil.
        veil = self._veil_win = tk.Toplevel(self.root)
        veil.overrideredirect(True)
        veil.attributes("-topmost", True)
        veil.configure(bg=VEIL)
        veil.attributes("-alpha", VEIL_ALPHA)
        veil.update_idletasks()
        self._make_click_through(veil)

        # Translation overlay: transparent body, opaque white boxes + text.
        win = self._overlay_win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=TRANSPARENT)
        win.attributes("-transparentcolor", TRANSPARENT)
        cv = self._overlay_canvas = tk.Canvas(
            win, bg=TRANSPARENT, highlightthickness=0, bd=0)
        cv.pack(fill="both", expand=True)
        win.update_idletasks()
        self._make_click_through(win)
        win.withdraw()  # shown only when it has content

        # In overlay mode the veil should not show.
        if self.cfg.overlay_mode:
            veil.withdraw()
        self._sync_overlays()

    def _make_click_through(self, win: tk.Toplevel):
        """Add WS_EX_TRANSPARENT so mouse events pass through the overlay."""
        try:
            GWL_EXSTYLE, WS_EX_LAYERED, WS_EX_TRANSPARENT = -20, 0x80000, 0x20
            user32 = ctypes.windll.user32
            hwnd = user32.GetParent(win.winfo_id())
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(
                hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TRANSPARENT)
        except Exception:
            pass

    def _capture_screen_rect(self) -> tuple[int, int, int, int]:
        """Screen rect (x, y, w, h) of the capture region's interior."""
        self.root.update_idletasks()
        x = self.capture.winfo_rootx() + 2
        y = self.capture.winfo_rooty() + 2
        w = max(1, self.capture.winfo_width() - 4)
        h = max(1, self.capture.winfo_height() - 4)
        return x, y, w, h

    def _sync_overlays(self):
        """Keep the veil and translation overlay aligned with the capture region."""
        if self._minimized:
            return
        x, y, w, h = self._capture_screen_rect()
        geo = f"{w}x{h}+{x}+{y}"
        if self._veil_win is not None:
            self._veil_win.geometry(geo)
        if self._overlay_win is not None:
            self._overlay_win.geometry(geo)

    # ------------------------------------------------------------ status ----
    def _set_status(self, text, color="#9aa0a6"):
        self.status.configure(text=text, fg=color)

    def _show_result(self, text):
        self.result_text.configure(state="normal")
        self.result_text.delete("1.0", "end")
        self.result_text.insert("1.0", text)
        self.result_text.configure(state="disabled")

    # --------------------------------------------------------- translation --
    def _ensure_translator(self) -> Translator | None:
        provider = self.cfg.provider
        key = self.cfg.resolved_api_key(provider)
        signature = (provider, key, self.cfg.model, self.cfg.target_language)
        if not key:
            return None
        if signature != self._translator_signature:
            self._translator = Translator(
                provider, key, self.cfg.model, self.cfg.target_language)
            self._translator_signature = signature
        return self._translator

    def _capture_bbox(self) -> dict | None:
        self.root.update_idletasks()
        x = self.capture.winfo_rootx()
        y = self.capture.winfo_rooty()
        w = self.capture.winfo_width()
        h = self.capture.winfo_height()
        if w < 4 or h < 4:
            return None
        # Inset by the 2px highlight border so we don't grab our own frame.
        return {"left": x + 2, "top": y + 2, "width": w - 4, "height": h - 4}

    def translate_once(self):
        self._scan(force=True)

    def _scan(self, force: bool):
        if self._busy or self._minimized:
            return
        translator = self._ensure_translator()
        if translator is None:
            self._set_status("未设置 API Key", "#f28b82")
            if force:
                self.open_settings()
            return
        img = self._grab_capture()
        if img is None:
            return

        self._busy = True
        self._set_status("识别中…", "#fdd663")
        threading.Thread(
            target=self._worker, args=(translator, img, force), daemon=True
        ).start()

    def _grab_capture(self) -> Image.Image | None:
        """Screenshot the capture region, momentarily hiding the gray veil so
        only the content behind the window is captured. Runs on the UI thread
        (the grab itself is fast; OCR/translation happen in the worker)."""
        bbox = self._capture_bbox()
        if bbox is None:
            return None
        # Flash both overlays invisible so the screenshot sees only the content
        # behind the window, then restore them.
        veil = self._veil_win
        ov = self._overlay_win
        ov_visible = ov is not None and bool(ov.winfo_viewable())
        if veil is not None:
            veil.attributes("-alpha", 0)
        if ov_visible:
            ov.withdraw()
        self.root.update_idletasks()
        try:
            with mss.MSS() as sct:
                shot = sct.grab(bbox)
            return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        finally:
            if veil is not None:
                veil.attributes("-alpha", VEIL_ALPHA)
            if ov_visible:
                ov.deiconify()

    def _worker(self, translator: Translator, img: Image.Image, force: bool):
        try:
            img_hash = hash(img.tobytes())
            if not force and img_hash == self._last_image_hash:
                self.root.after(0, lambda: self._finish(None, None))
                return
            self._last_image_hash = img_hash

            if self.cfg.overlay_mode:
                self._run_overlay(translator, img, force)
            else:
                self._run_panel(translator, img, force)
        except Exception as exc:  # surface any failure in the status line
            msg = str(exc)
            self.root.after(0, lambda: self._finish(None, msg))

    def _run_panel(self, translator: Translator, img: Image.Image, force: bool):
        """Normal mode: translate the whole region, show it in the bottom panel."""
        text = ocr.recognize(img, self.cfg.ocr_language or None).strip()
        if not text:
            self.root.after(0, lambda: self._finish("（未识别到文字）", None))
            return
        if not force and text == self._last_text:
            self.root.after(0, lambda: self._finish(None, None))
            return
        self._last_text = text
        self.root.after(0, lambda: self._set_status("翻译中…", "#fdd663"))
        translated = translator.translate(text)
        self.root.after(0, lambda: self._finish(translated, None))

    def _run_overlay(self, translator: Translator, img: Image.Image, force: bool):
        """Overlay mode: translate each detected line and draw white boxes with
        the translation in place over the original text."""
        lines = [ln for ln in ocr.recognize_lines(img, self.cfg.ocr_language or None)
                 if ln["text"].strip()]
        if not lines:
            self.root.after(0, lambda: self._finish_overlay([], "（未识别到文字）"))
            return
        combined = "\n".join(ln["text"] for ln in lines)
        if not force and combined == self._last_text:
            self.root.after(0, lambda: self._finish(None, None))
            return
        self._last_text = combined

        self.root.after(0, lambda: self._set_status("翻译中…", "#fdd663"))
        items = []
        for ln in lines:
            ln = dict(ln)
            ln["translation"] = translator.translate(ln["text"])
            items.append(ln)
        full = "\n".join(it["translation"] for it in items)
        self.root.after(0, lambda: self._finish_overlay(items, None, full))

    def _finish(self, result: str | None, error: str | None):
        self._busy = False
        if error:
            self._set_status("错误", "#f28b82")
            self._show_result(f"⚠ {error}")
        elif result is not None:
            self._set_status("完成", "#81c995")
            self._show_result(result)
        else:
            self._set_status("无变化", "#9aa0a6")

    def _finish_overlay(self, items: list, message: str | None, full: str | None = None):
        self._busy = False
        if not items:
            self._clear_overlay()
            self._set_status(message or "无变化", "#9aa0a6")
            if message:
                self._show_result(message)
            return
        self._draw_overlay(items)
        self._set_status("完成", "#81c995")
        if full is not None:
            self._show_result(full)

    # ----------------------------------------------------- overlay drawing ----
    def _draw_overlay(self, items: list):
        win, cv = self._overlay_win, self._overlay_canvas
        if win is None or cv is None:
            return
        self._sync_overlays()
        cv.delete("all")
        for it in items:
            x, y, w, h = it["x"], it["y"], it["w"], it["h"]
            # White box covering the original text (slightly padded).
            cv.create_rectangle(x - 2, y - 2, x + w + 2, y + h + 2,
                                fill="#ffffff", outline="#ffffff")
            font = self._fit_font(it["translation"], w, h)
            cv.create_text(x, y + h / 2, text=it["translation"], anchor="w",
                           fill="#000000", font=font)
        if not win.winfo_viewable():
            win.deiconify()

    def _fit_font(self, text: str, max_w: int, max_h: int) -> tkfont.Font:
        size = max(8, int(max_h * 0.78))
        font = tkfont.Font(family="Microsoft YaHei UI", size=size)
        while size > 7 and font.measure(text) > max(1, max_w):
            size -= 1
            font.configure(size=size)
        return font

    def _clear_overlay(self):
        if self._overlay_canvas is not None:
            self._overlay_canvas.delete("all")
        if self._overlay_win is not None and self._overlay_win.winfo_viewable():
            self._overlay_win.withdraw()

    # --------------------------------------------------------------- auto ----
    def toggle_auto(self):
        self._auto = not self._auto
        self.auto_btn.configure(text=f"自动:{'开' if self._auto else '关'}")
        if self._auto:
            self._auto_tick()
        elif self._auto_job:
            self.root.after_cancel(self._auto_job)
            self._auto_job = None

    def _auto_tick(self):
        if not self._auto:
            return
        self._scan(force=False)
        self._auto_job = self.root.after(self.cfg.auto_interval_ms, self._auto_tick)

    # -------------------------------------------------------- overlay mode ----
    def toggle_overlay(self):
        on = not self.cfg.overlay_mode
        self.cfg.overlay_mode = on
        self.overlay_btn.configure(text=f"覆盖:{'开' if on else '关'}")
        if self._veil_win is not None:
            (self._veil_win.withdraw if on else self._veil_win.deiconify)()
        if not on:
            self._clear_overlay()
        # Force a fresh render on the next scan.
        self._last_image_hash = None
        self._last_text = ""
        self._sync_overlays()
        if on and not self._minimized:
            self.translate_once()

    # ----------------------------------------------------------- settings ----
    def open_settings(self):
        SettingsDialog(self)

    # ----------------------------------------------------- minimize / ball ----
    def minimize(self):
        """Hide the main window and show a draggable floating ball instead.

        The ball parks at the bottom-right of the desktop; restoring brings the
        window back to wherever it was when minimized.
        """
        if self._minimized:
            return
        self._minimized = True
        # Remember where to bring the window back to.
        self._restore_pos = (self.root.winfo_x(), self.root.winfo_y())
        self.root.withdraw()
        if self._veil_win is not None:
            self._veil_win.withdraw()
        if self._overlay_win is not None:
            self._overlay_win.withdraw()

        size, margin_x, margin_y = 60, 40, 80  # bottom margin clears the taskbar
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self._show_ball(sw - size - margin_x, sh - size - margin_y)

    def _show_ball(self, x: int, y: int):
        size = 60
        ball = self._ball = tk.Toplevel(self.root)
        ball.overrideredirect(True)
        ball.attributes("-topmost", True)
        ball.configure(bg=TRANSPARENT)
        ball.attributes("-transparentcolor", TRANSPARENT)
        ball.geometry(f"{size}x{size}+{max(0, x)}+{max(0, y)}")

        cv = tk.Canvas(ball, width=size, height=size, bg=TRANSPARENT,
                       highlightthickness=0, cursor="hand2")
        cv.pack()
        # The transparent corners make the window read as a circle.
        cv.create_oval(4, 4, size - 4, size - 4, fill=ACCENT,
                       outline="#ffffff", width=2)
        cv.create_text(size // 2, size // 2, text="译", fill="white",
                       font=("Microsoft YaHei UI", 20, "bold"))

        cv.bind("<Button-1>", self._ball_press)
        cv.bind("<B1-Motion>", self._ball_drag)
        cv.bind("<Double-Button-1>", self._restore)

    def _ball_press(self, e):
        self._bx, self._by = e.x_root, e.y_root
        self._box, self._boy = self._ball.winfo_x(), self._ball.winfo_y()

    def _ball_drag(self, e):
        self._ball.geometry(
            f"+{self._box + (e.x_root - self._bx)}+{self._boy + (e.y_root - self._by)}")

    def _restore(self, _e=None):
        """Double-click the ball: destroy it and bring the window back to the
        position it had when it was minimized."""
        if self._ball is not None:
            self._ball.destroy()
            self._ball = None
        x, y = getattr(self, "_restore_pos", (self.root.winfo_x(), self.root.winfo_y()))
        self.root.geometry(f"+{max(0, x)}+{max(0, y)}")
        self._minimized = False
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        if self._veil_win is not None and not self.cfg.overlay_mode:
            self._veil_win.deiconify()
        self._sync_overlays()
        if self.cfg.overlay_mode:
            self.root.after(150, self.translate_once)

    # -------------------------------------------------------------- close ----
    def _on_close(self):
        if self._ball is not None:
            self._ball.destroy()
            self._ball = None
        if self._veil_win is not None:
            self._veil_win.destroy()
            self._veil_win = None
        if self._overlay_win is not None:
            self._overlay_win.destroy()
            self._overlay_win = None
        try:
            self.cfg.geometry = (
                f"{self.root.winfo_width()}x{self.root.winfo_height()}"
                f"+{self.root.winfo_x()}+{self.root.winfo_y()}"
            )
            self.cfg.save()
        finally:
            self.root.destroy()

    def run(self):
        self.root.mainloop()


class SettingsDialog:
    def __init__(self, app: FloatTranslate):
        self.app = app
        cfg = app.cfg
        top = self.top = tk.Toplevel(app.root)
        top.title("设置")
        top.configure(bg=BAR_BG, padx=16, pady=16)
        top.attributes("-topmost", True)
        top.resizable(False, False)
        top.columnconfigure(1, weight=1)

        try:
            langs = ["（自动）"] + ocr.available_languages()
        except Exception:
            langs = ["（自动）"]

        # Per-provider keys are edited on a local copy so 取消 discards changes.
        self.keys: dict[str, str] = dict(cfg.api_keys)
        self.models_cache: dict[str, list[str]] = {}
        self._validating = False
        self._current_pid = cfg.provider

        self._pids = providers.provider_ids()
        self._labels = [providers.provider_label(p) for p in self._pids]
        self._label_to_id = dict(zip(self._labels, self._pids))

        self.var_provider = tk.StringVar(value=providers.provider_label(cfg.provider))
        self.var_api_key = tk.StringVar(value=self.keys.get(cfg.provider, ""))
        self.var_model = tk.StringVar(value=cfg.model)
        self.var_target = tk.StringVar(value=cfg.target_language)
        self.var_ocr = tk.StringVar(value=cfg.ocr_language or "（自动）")
        self.var_interval = tk.StringVar(value=str(cfg.auto_interval_ms))

        r = 0
        # --- provider ---
        self._label(top, "服务商", r)
        self.cmb_provider = ttk.Combobox(
            top, textvariable=self.var_provider, values=self._labels,
            state="readonly", width=30)
        self.cmb_provider.grid(row=r, column=1, sticky="ew", padx=(10, 0), pady=4)
        self.cmb_provider.bind("<<ComboboxSelected>>", self._on_provider_change)
        r += 1

        # --- api key ---
        self._label(top, "API Key", r)
        self.ent_key = tk.Entry(top, textvariable=self.var_api_key, width=33, show="*")
        self.ent_key.grid(row=r, column=1, sticky="ew", padx=(10, 0), pady=4)
        r += 1

        # --- validate + status ---
        vbar = tk.Frame(top, bg=BAR_BG)
        vbar.grid(row=r, column=1, sticky="w", padx=(10, 0), pady=(0, 4))
        self.btn_validate = app._mk_button(vbar, "验证并获取模型", self._validate,
                                           accent=True)
        self.btn_validate.pack(side="left")
        self.lbl_validate = tk.Label(vbar, text="", bg=BAR_BG, fg="#9aa0a6",
                                     font=("Segoe UI", 8))
        self.lbl_validate.pack(side="left", padx=8)
        r += 1

        # --- model ---
        self._label(top, "模型", r)
        self.cmb_model = ttk.Combobox(
            top, textvariable=self.var_model,
            values=self._initial_models(cfg.provider, cfg.model), width=30)
        self.cmb_model.grid(row=r, column=1, sticky="ew", padx=(10, 0), pady=4)
        r += 1

        # --- target language ---
        self._label(top, "目标语言", r)
        tk.Entry(top, textvariable=self.var_target, width=33).grid(
            row=r, column=1, sticky="ew", padx=(10, 0), pady=4)
        r += 1

        # --- ocr language ---
        self._label(top, "OCR 源语言", r)
        ttk.Combobox(top, textvariable=self.var_ocr, values=langs,
                     state="readonly", width=30).grid(
            row=r, column=1, sticky="ew", padx=(10, 0), pady=4)
        r += 1

        # --- auto interval ---
        self._label(top, "自动间隔 (毫秒)", r)
        tk.Entry(top, textvariable=self.var_interval, width=33).grid(
            row=r, column=1, sticky="ew", padx=(10, 0), pady=4)
        r += 1

        hint = tk.Label(
            top, fg="#9aa0a6", bg=BAR_BG, font=("Segoe UI", 8), justify="left",
            text="留空 API Key 时将使用对应环境变量（如 OPENAI_API_KEY）。\n"
                 "点「验证并获取模型」可校验 Key 并拉取可用模型列表。",
        )
        hint.grid(row=r, column=0, columnspan=2, sticky="w", pady=(8, 0))
        r += 1

        btns = tk.Frame(top, bg=BAR_BG)
        btns.grid(row=r, column=0, columnspan=2, sticky="e", pady=(12, 0))
        app._mk_button(btns, "取消", top.destroy).pack(side="right", padx=4)
        app._mk_button(btns, "保存", self._save, accent=True).pack(side="right")

    # ---- helpers --------------------------------------------------------- #
    def _label(self, parent, text, row):
        tk.Label(parent, text=text, bg=BAR_BG, fg=BAR_FG,
                 font=("Segoe UI", 9)).grid(row=row, column=0, sticky="w", pady=4)

    def _initial_models(self, pid, current) -> list[str]:
        models = self.models_cache.get(pid) or providers.default_models(pid)
        if current and current not in models:
            models = [current] + models
        return list(dict.fromkeys(models))

    def _set_validate(self, text, color="#9aa0a6"):
        self.lbl_validate.configure(text=text, fg=color)

    def _on_provider_change(self, _evt=None):
        # Commit the visible key to the provider we're leaving.
        self.keys[self._current_pid] = self.var_api_key.get().strip()
        self._current_pid = self._label_to_id[self.var_provider.get()]
        self.var_api_key.set(self.keys.get(self._current_pid, ""))
        models = self._initial_models(self._current_pid, "")
        self.cmb_model.configure(values=models)
        if self.var_model.get() not in models:
            self.var_model.set(models[0] if models else "")
        self._set_validate("")

    # ---- validation (runs off the UI thread) ----------------------------- #
    def _validate(self):
        if self._validating:
            return
        pid = self._current_pid
        key = self.var_api_key.get().strip() or self.app.cfg.resolved_api_key(pid)
        if not key:
            self._set_validate("请先填入 API Key", "#f28b82")
            return
        self._validating = True
        self.btn_validate.configure(state="disabled")
        self._set_validate("验证中…", "#fdd663")
        threading.Thread(target=self._validate_worker, args=(pid, key),
                         daemon=True).start()

    def _validate_worker(self, pid, key):
        try:
            models = providers.get_provider(pid).list_models(key)
            self.top.after(0, lambda: self._validate_done(pid, models, None))
        except Exception as exc:  # noqa: BLE001 — show the message in the dialog
            msg = str(exc)
            self.top.after(0, lambda: self._validate_done(pid, None, msg))

    def _validate_done(self, pid, models, error):
        self._validating = False
        if not self.top.winfo_exists():
            return
        self.btn_validate.configure(state="normal")
        if error:
            self._set_validate(f"✗ {error}", "#f28b82")
            return
        if not models:
            self._set_validate("✓ Key 可用，但未获取到模型", "#fdd663")
            return
        self.models_cache[pid] = models
        if pid == self._current_pid:
            self.cmb_model.configure(values=models)
            if self.var_model.get() not in models:
                self.var_model.set(models[0])
        self._set_validate(f"✓ Key 可用，{len(models)} 个模型", "#81c995")

    # ---- save ------------------------------------------------------------ #
    def _save(self):
        cfg = self.app.cfg
        self.keys[self._current_pid] = self.var_api_key.get().strip()
        cfg.provider = self._current_pid
        cfg.api_keys = dict(self.keys)
        cfg.api_key = ""  # legacy field now lives in api_keys
        cfg.model = self.var_model.get().strip() or cfg.model
        cfg.target_language = self.var_target.get().strip() or cfg.target_language
        ocr_lang = self.var_ocr.get().strip()
        cfg.ocr_language = "" if ocr_lang in ("", "（自动）") else ocr_lang
        try:
            cfg.auto_interval_ms = max(400, int(self.var_interval.get()))
        except ValueError:
            pass
        cfg.save()
        # Force translator rebuild with new settings.
        self.app._translator_signature = None
        self.app._set_status("设置已保存", "#81c995")
        self.top.destroy()


if __name__ == "__main__":
    _enable_dpi_awareness()
    FloatTranslate().run()
