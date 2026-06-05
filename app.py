"""FloatTranslate — a floating-window screen translator for Windows.

Position the transparent middle band over text in any app (game, image,
PDF, foreign UI). Click 翻译 for a one-shot translation, or toggle 自动 to
keep re-translating as the underlying text changes. Recognition uses the
built-in Windows OCR; translation uses the Claude API.
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

BAR_BG = "#202124"
BAR_FG = "#e8eaed"
RESULT_BG = "#2b2c2f"
RESULT_FG = "#f1f3f4"
ACCENT = "#3b6ef5"
# Light-gray veil drawn over the capture region (dithered so it reads as
# semi-transparent and you can still see the text underneath).
VEIL = "#d9d9d9"


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
        self._mk_button(btns, "⚙", self.open_settings).pack(side="left", padx=2)
        self._mk_button(btns, "—", self.minimize).pack(side="left", padx=2)
        self._mk_button(btns, "×", self._on_close).pack(side="left", padx=2)

        for w in (bar, grip, self.status):
            w.bind("<Button-1>", self._start_move)
            w.bind("<B1-Motion>", self._on_move)

        # --- transparent capture band (with a light-gray veil) ---
        # The canvas body is the sentinel colour = a real see-through hole.
        # A dithered light-gray rectangle on top reads as a semi-transparent
        # veil; it is hidden for the instant we take the screenshot so OCR
        # only sees what lies behind the window.
        self.capture = tk.Canvas(self.root, bg=TRANSPARENT, bd=0,
                                 highlightthickness=2, highlightbackground=ACCENT)
        self.capture.grid(row=1, column=0, sticky="nsew")
        self._veil = self.capture.create_rectangle(
            0, 0, 1, 1, fill=VEIL, stipple="gray25", outline="")
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

    def _start_resize(self, e):
        self._mx, self._my = e.x_root, e.y_root
        self._ow, self._oh = self.root.winfo_width(), self.root.winfo_height()

    def _on_resize(self, e):
        w = max(280, self._ow + (e.x_root - self._mx))
        h = max(220, self._oh + (e.y_root - self._my))
        self.root.geometry(f"{w}x{h}")

    def _on_capture_configure(self, e):
        # Keep the gray veil covering the whole capture canvas.
        self.capture.coords(self._veil, 0, 0, e.width, e.height)

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
        self.capture.itemconfigure(self._veil, state="hidden")
        self.capture.update_idletasks()
        try:
            with mss.MSS() as sct:
                shot = sct.grab(bbox)
            return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        finally:
            self.capture.itemconfigure(self._veil, state="normal")

    def _worker(self, translator: Translator, img: Image.Image, force: bool):
        try:
            img_hash = hash(img.tobytes())
            if not force and img_hash == self._last_image_hash:
                self.root.after(0, lambda: self._finish(None, None))
                return
            self._last_image_hash = img_hash

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
        except Exception as exc:  # surface any failure in the status line
            msg = str(exc)
            self.root.after(0, lambda: self._finish(None, msg))

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

    # ----------------------------------------------------------- settings ----
    def open_settings(self):
        SettingsDialog(self)

    # ----------------------------------------------------- minimize / ball ----
    def minimize(self):
        """Hide the main window and show a draggable floating ball instead."""
        if self._minimized:
            return
        self._minimized = True
        x, y = self.root.winfo_x(), self.root.winfo_y()
        self.root.withdraw()
        self._show_ball(x, y)

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
        """Double-click the ball: destroy it and bring the window back where
        the ball now sits."""
        if self._ball is not None:
            bx, by = self._ball.winfo_x(), self._ball.winfo_y()
            self._ball.destroy()
            self._ball = None
            self.root.geometry(f"+{max(0, bx)}+{max(0, by)}")
        self._minimized = False
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)

    # -------------------------------------------------------------- close ----
    def _on_close(self):
        if self._ball is not None:
            self._ball.destroy()
            self._ball = None
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
