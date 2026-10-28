"""Glagol Control — оконное управление Яндекс Станцией по локальному протоколу."""

import json
import os
import queue
import ssl
import sys
import threading
import time
import traceback

import tkinter as tk
from tkinter import messagebox, ttk

import websockets
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect as ws_connect

from glagol import (
    BASE_DIR,
    account_login,
    conversation_token,
    load_config,
    make_envelope,
    pick_device,
)

APP = "Glagol Control"

BG = "#15161a"
CARD = "#1f2127"
CARD2 = "#272b33"
FG = "#e9eaee"
MUTED = "#9aa1ad"
ACCENT = "#7c9cff"
OK = "#5ad18a"
ERR = "#ff6f6f"
WARN = "#ffc266"

FONT = "Segoe UI"
STEPS = 5


class StationError(Exception):
    pass


def _close_info(exc):
    for attr in ("rcvd", "sent"):
        msg = getattr(exc, attr, None)
        if msg is not None:
            return getattr(msg, "code", None), getattr(msg, "reason", "") or ""
    return None, ""


class Station:
    """Одно подключение к колонке. Все методы вызываются из рабочего потока."""

    def __init__(self, cfg, dev):
        self.cfg = cfg
        self.dev = dev
        self.ws = None
        self.token = None

    def _ensure(self):
        if self.ws is not None:
            return
        self.token = conversation_token(self.cfg, self.dev)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            try:
                # websockets >= 15: держим соединение открытым между командами
                self.ws = ws_connect(
                    f"wss://{self.dev['host']}:1961",
                    ssl=ctx,
                    open_timeout=8,
                    close_timeout=2,
                    legacy=True,
                )
            except TypeError:
                self.ws = ws_connect(
                    f"wss://{self.dev['host']}:1961",
                    ssl=ctx,
                    open_timeout=8,
                    close_timeout=2,
                )
        except OSError as e:
            raise StationError(
                f"Колонка недоступна ({self.dev['host']}:1961): {e}"
            )

    def request(self, payload, timeout=10):
        self._ensure()
        rid, text = make_envelope(self.token, payload)
        try:
            self.ws.send(text)
        except Exception:
            self.close()
            raise StationError("Соединение потеряно. Нажмите «Подключиться».")

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                raw = self.ws.recv(timeout=max(0.1, deadline - time.time()))
            except TimeoutError:
                break
            except ConnectionClosed as e:
                code, reason = _close_info(e)
                self.close()
                if code == 4000:
                    raise StationError(
                        "Колонка отвергла токен (4000 Invalid token).\n"
                        "Проверьте, что music_token взят под аккаунтом-владельцем\n"
                        "колонки, и перезагрузите колонку."
                    )
                raise StationError(f"Колонка закрыла соединение: {code} {reason}")
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if msg.get("requestId") == rid:
                return msg
        raise StationError("Колонка не ответила вовремя. Попробуйте ещё раз.")

    def probe(self):
        return self.request({"command": "softwareVersion"})

    def set_volume(self, percent):
        self.request({"command": "setVolume", "volume": round(percent / 100, 2)})

    def close(self):
        try:
            if self.ws is not None:
                self.ws.close()
        except Exception:
            pass
        self.ws = None


def fmt_time(seconds):
    try:
        seconds = int(seconds or 0)
    except (TypeError, ValueError):
        return "—"
    return f"{seconds // 60}:{seconds % 60:02d}"


class VolumeSlider(tk.Canvas):
    """Ползунок громкости с шагом 5 — 21 положение от 0 до 100."""

    def __init__(self, parent, value=35, on_move=None, on_change=None, **kwargs):
        super().__init__(
            parent,
            height=38,
            bg=CARD,
            highlightthickness=0,
            bd=0,
            takefocus=True,
            **kwargs,
        )
        self.value = max(0, min(100, int(round(value / STEPS) * STEPS)))
        self.on_move = on_move
        self.on_change = on_change
        self._dragging = False
        self.bind("<Configure>", lambda e: self._redraw())
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Left>", lambda e: self._step(-STEPS))
        self.bind("<Right>", lambda e: self._step(STEPS))
        self.bind("<Home>", lambda e: self._set_and_commit(0))
        self.bind("<End>", lambda e: self._set_and_commit(100))
        self.bind("<MouseWheel>", self._on_wheel)

    def _geometry(self):
        width = max(self.winfo_width(), 40)
        pad = 14
        return pad, width - pad, self.winfo_height() // 2

    def _x_for(self, value):
        x0, x1, _ = self._geometry()
        return x0 + (x1 - x0) * value / 100

    def _value_for(self, x):
        x0, x1, _ = self._geometry()
        ratio = (x - x0) / max(1, x1 - x0)
        return max(0, min(100, int(round(ratio * 100 / STEPS) * STEPS)))

    def _redraw(self):
        self.delete("all")
        x0, x1, y = self._geometry()
        if x1 <= x0:
            return
        self.create_line(x0, y, x1, y, fill="#2f333c", width=8, capstyle="round")
        kx = self._x_for(self.value)
        if kx > x0:
            self.create_line(x0, y, kx, y, fill=ACCENT, width=8, capstyle="round")
        for tick in (0, 25, 50, 75, 100):
            tx = self._x_for(tick)
            self.create_line(tx, y + 9, tx, y + 12, fill="#4a4f5a")
        r = 8
        self.create_oval(
            kx - r,
            y - r,
            kx + r,
            y + r,
            fill="#ffffff",
            outline=ACCENT,
            width=3,
        )

    def get(self):
        return self.value

    def set(self, value, notify=False):
        value = max(0, min(100, int(round(float(value) / STEPS) * STEPS)))
        if value != self.value:
            self.value = value
            self._redraw()
        if notify and self.on_move:
            self.on_move(self.value)

    def _on_press(self, event):
        self.focus_set()
        self._dragging = True
        self.set(self._value_for(event.x), notify=True)
        return "break"

    def _on_drag(self, event):
        if self._dragging:
            self.set(self._value_for(event.x), notify=True)
        return "break"

    def _on_release(self, event):
        if not self._dragging:
            return
        self._dragging = False
        self.set(self._value_for(event.x), notify=True)
        if self.on_change:
            self.on_change(self.value)
        return "break"

    def _step(self, delta):
        self._set_and_commit(self.value + delta)
        return "break"

    def _set_and_commit(self, value):
        self.set(value, notify=True)
        if self.on_change:
            self.on_change(self.value)

    def _on_wheel(self, event):
        self._step(STEPS if event.delta > 0 else -STEPS)
        return "break"


class App(tk.Tk):
    def __init__(self, autoconnect=True, hidden=False):
        super().__init__()
        self.withdraw()
        self.title(APP)
        icon = os.path.join(BASE_DIR, "glagol.ico")
        if os.path.exists(icon):
            try:
                self.iconbitmap(default=icon)
            except tk.TclError:
                pass
        self.geometry("640x730")
        self.minsize(560, 620)
        self.configure(bg=BG)

        try:
            self.cfg = load_config()
        except SystemExit:
            self.destroy()
            raise
        self.devices = self.cfg.get("devices") or {}
        self.device_key = self.cfg.get("default_device") or next(iter(self.devices), None)
        if not self.device_key or self.device_key not in self.devices:
            self.destroy()
            raise SystemExit("В config.json не найден список устройств")

        self.station = None
        self.jobs = queue.Queue()
        self.results = queue.Queue()
        self.muted = False
        self.last_nonzero = 35
        self.login = None
        self._closing = False

        self._style()
        self._build()

        self.worker = threading.Thread(target=self._worker, daemon=True)
        self.worker.start()
        self.after(40, self._poll)

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        if hidden:
            self.update_idletasks()
        else:
            self.deiconify()
            if autoconnect:
                self.after(300, self.connect)

    # ---------------------------------------------------------------- style

    def _style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "TNotebook",
            background=BG,
            borderwidth=0,
            relief="flat",
            bordercolor=CARD,
            lightcolor=CARD,
            darkcolor=CARD,
        )
        style.configure(
            "TNotebook.Tab",
            background=CARD,
            foreground=MUTED,
            padding=(14, 6),
            borderwidth=0,
            lightcolor=CARD,
            darkcolor=CARD,
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", CARD2)],
            foreground=[("selected", FG)],
        )
        self.option_add("*Menu.background", CARD2)
        self.option_add("*Menu.foreground", FG)
        self.option_add("*Menu.activeBackground", ACCENT)
        self.option_add("*Menu.activeForeground", "#ffffff")

    def _button(self, parent, text, command, width=None, accent=False):
        bg = ACCENT if accent else CARD2
        fg = "#ffffff" if accent else FG
        options = dict(
            text=text,
            command=command,
            bg=bg,
            fg=fg,
            activebackground=ACCENT if accent else "#333844",
            activeforeground="#ffffff",
            relief="flat",
            bd=0,
            padx=12,
            pady=7,
            cursor="hand2",
            font=(FONT, 10),
        )
        if width is not None:
            options["width"] = width
        return tk.Button(parent, **options)

    # ------------------------------------------------------------------ ui

    def _build(self):
        header = tk.Frame(self, bg=BG)
        header.pack(fill="x", padx=16, pady=(14, 4))
        tk.Label(
            header, text="Glagol Control", font=(FONT, 18, "bold"), bg=BG, fg=FG
        ).pack(side="left")
        self.status = tk.Label(
            header, text="не подключено", font=(FONT, 9), bg=BG, fg=MUTED
        )
        self.status.pack(side="right")

        row = tk.Frame(self, bg=BG)
        row.pack(fill="x", padx=16, pady=(4, 0))
        tk.Label(row, text="Колонка", font=(FONT, 10), bg=BG, fg=MUTED).pack(
            side="left"
        )

        self.dev_labels = {
            f"{d.get('name', key)} — {d.get('host', '?')}": key
            for key, d in self.devices.items()
        }
        self.device_label = next(
            (label for label, key in self.dev_labels.items() if key == self.device_key),
            next(iter(self.dev_labels), ""),
        )
        self.device_btn = tk.Menubutton(
            row,
            text=self.device_label,
            bg=CARD2,
            fg=FG,
            activebackground="#333844",
            activeforeground=FG,
            relief="flat",
            bd=0,
            padx=10,
            pady=6,
            font=(FONT, 10),
            cursor="hand2",
            anchor="w",
        )
        menu = tk.Menu(
            self.device_btn,
            tearoff=0,
            bg=CARD2,
            fg=FG,
            activebackground=ACCENT,
            activeforeground="#ffffff",
            font=(FONT, 10),
            bd=0,
        )
        for label, key in self.dev_labels.items():
            menu.add_command(
                label=label,
                command=lambda k=key, l=label: self._select_device(k, l),
            )
        self.device_btn.config(menu=menu)
        self.device_btn.pack(side="left", padx=(8, 8))

        self._button(row, "Подключиться", self.connect, accent=True).pack(
            side="left"
        )

        # volume ---------------------------------------------------------
        vol = tk.Frame(self, bg=CARD)
        vol.pack(fill="x", padx=16, pady=(12, 8))

        top = tk.Frame(vol, bg=CARD)
        top.pack(fill="x", padx=14, pady=(10, 0))
        tk.Label(
            top, text="Громкость", font=(FONT, 11, "bold"), bg=CARD, fg=FG
        ).pack(side="left")
        self.vol_label = tk.Label(
            top, text="—", font=(FONT, 22, "bold"), bg=CARD, fg=ACCENT
        )
        self.vol_label.pack(side="right")

        self.scale = VolumeSlider(
            vol,
            value=35,
            on_move=self._on_scale_move,
            on_change=self._on_volume_commit,
        )
        self.scale.pack(fill="x", padx=14, pady=(6, 2))

        bottom = tk.Frame(vol, bg=CARD)
        bottom.pack(fill="x", padx=14, pady=(0, 10))
        tk.Label(
            bottom,
            text="21 ступень: 0, 5, 10 … 100",
            font=(FONT, 8),
            bg=CARD,
            fg=MUTED,
        ).pack(side="left")

        self.mute_btn = self._button(bottom, "Без звука", self.toggle_mute)
        self.mute_btn.pack(side="right")
        self._button(bottom, "+5", lambda: self.step_volume(5)).pack(
            side="right", padx=(0, 6)
        )
        self._button(bottom, "−5", lambda: self.step_volume(-5)).pack(
            side="right", padx=(0, 6)
        )

        # playback -------------------------------------------------------
        play = tk.Frame(self, bg=CARD)
        play.pack(fill="x", padx=16, pady=(0, 8))
        ptop = tk.Frame(play, bg=CARD)
        ptop.pack(fill="x", padx=14, pady=(10, 4))
        self.track_label = tk.Label(
            ptop,
            text="Ничего не играет",
            font=(FONT, 10),
            bg=CARD,
            fg=FG,
            anchor="w",
            justify="left",
        )
        self.track_label.pack(side="left", fill="x", expand=True)
        self.play_state = tk.Label(
            ptop, text="", font=(FONT, 9), bg=CARD, fg=MUTED
        )
        self.play_state.pack(side="right")

        prow = tk.Frame(play, bg=CARD)
        prow.pack(fill="x", padx=14, pady=(0, 10))
        self._button(prow, "⏮", lambda: self.send_cmd({"command": "prev"})).pack(
            side="left", padx=(0, 6)
        )
        self._button(prow, "▶", lambda: self.send_cmd({"command": "play"})).pack(
            side="left", padx=(0, 6)
        )
        self._button(prow, "⏸", lambda: self.send_cmd({"command": "stop"})).pack(
            side="left", padx=(0, 6)
        )
        self._button(prow, "⏭", lambda: self.send_cmd({"command": "next"})).pack(
            side="left", padx=(0, 6)
        )

        self.repeat_mode = "None"
        self.repeat_btn = self._button(prow, "Повтор: выкл", self._cycle_repeat)
        self.repeat_btn.pack(side="left", padx=(14, 0))

        self.shuffle_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            prow,
            text="Перемешать",
            variable=self.shuffle_var,
            command=self._on_shuffle,
            bg=CARD,
            fg=FG,
            selectcolor=CARD2,
            activebackground=CARD,
            activeforeground=FG,
            font=(FONT, 9),
            bd=0,
            highlightthickness=0,
        ).pack(side="left", padx=(12, 0))

        # tts ------------------------------------------------------------
        tts = tk.Frame(self, bg=CARD)
        tts.pack(fill="x", padx=16, pady=(0, 8))
        tk.Label(
            tts, text="Сказать", font=(FONT, 9), bg=CARD, fg=MUTED
        ).pack(side="left", padx=(14, 6), pady=10)
        self.tts_var = tk.StringVar()
        self.tts_entry = tk.Entry(
            tts,
            textvariable=self.tts_var,
            bg=CARD2,
            fg=FG,
            insertbackground=FG,
            relief="flat",
            font=(FONT, 10),
        )
        self.tts_entry.pack(side="left", fill="x", expand=True, ipady=5)
        self.tts_entry.bind("<Return>", lambda e: self.say())
        self._button(tts, "Сказать", self.say).pack(
            side="left", padx=(8, 14), pady=6
        )

        # tabs -----------------------------------------------------------
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=16, pady=(2, 8))

        self.state_text = self._text_tab(nb, "Состояние")
        self.features_text = self._text_tab(nb, "Возможности")

        adv = tk.Frame(nb, bg=CARD)
        nb.add(adv, text="Команда")
        tk.Label(
            adv,
            text="JSON-команда в локальный протокол (для опытных)",
            font=(FONT, 9),
            bg=CARD,
            fg=MUTED,
        ).pack(anchor="w", padx=10, pady=(8, 4))
        self.adv_text = tk.Text(
            adv,
            height=5,
            bg=CARD2,
            fg=FG,
            insertbackground=FG,
            relief="flat",
            bd=0,
            highlightthickness=0,
            font=("Consolas", 9),
            wrap="word",
        )
        self.adv_text.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        self.adv_text.insert(
            "1.0",
            json.dumps(
                {"command": "softwareVersion"}, ensure_ascii=False, indent=2
            ),
        )
        self._button(adv, "Отправить JSON", self.send_json).pack(
            anchor="w", padx=10, pady=(0, 10)
        )

        # log ------------------------------------------------------------
        log_frame = tk.Frame(self, bg=BG)
        log_frame.pack(fill="x", padx=16, pady=(0, 12))
        self.log_text = tk.Text(
            log_frame,
            height=5,
            bg=CARD,
            fg=MUTED,
            relief="flat",
            bd=0,
            highlightthickness=0,
            font=("Consolas", 8),
            wrap="word",
            state="disabled",
        )
        self.log_text.pack(fill="x")

        self.scale.set(35)
        self.vol_label.config(text="35%")

    def _text_tab(self, nb, title):
        frame = tk.Frame(nb, bg=CARD)
        nb.add(frame, text=title)
        text = tk.Text(
            frame,
            height=10,
            bg=CARD,
            fg=FG,
            relief="flat",
            bd=0,
            highlightthickness=0,
            font=("Consolas", 9),
            wrap="word",
            state="disabled",
        )
        text.pack(fill="both", expand=True, padx=8, pady=8)
        return text

    def _set_text(self, widget, value):
        widget.config(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", value)
        widget.config(state="disabled")

    # -------------------------------------------------------------- worker

    def _worker(self):
        while True:
            fn, ok, err = self.jobs.get()
            try:
                result = fn()
            except SystemExit as e:
                self.results.put((err, str(e.code) if e.code else "Остановлено"))
            except Exception as e:
                self.results.put((err, str(e)))
            else:
                self.results.put((ok, result))

    def _poll(self):
        while True:
            try:
                cb, arg = self.results.get_nowait()
            except queue.Empty:
                break
            if cb is not None:
                try:
                    cb(arg)
                except Exception:
                    self.log(traceback.format_exc())
        if not self._closing:
            self.after(40, self._poll)

    def submit(self, fn, on_ok=None, on_err=None):
        self.jobs.put((fn, on_ok or (lambda _: None), on_err or self.on_error))

    def _station(self):
        if self.station is None:
            self.station = Station(self.cfg, pick_device(self.cfg, self.device_key))
        return self.station

    # ------------------------------------------------------------- actions

    def connect(self):
        self.set_status("подключаюсь…", WARN)
        label = self.device_label
        self.log(f"Подключение: {label}")

        def job():
            st = self._station()
            st.close()
            msg = st.probe()
            return msg, account_login(st.cfg)

        self.submit(job, self.on_probe, self.on_error)

    def on_probe(self, result):
        msg, login = result
        if login:
            self.login = login
        self.set_status("подключено", OK)

        state = msg.get("state") or {}
        vol = state.get("volume")
        if isinstance(vol, (int, float)):
            pct = int(round(vol * 100 / STEPS) * STEPS)
            self.scale.set(pct)
            self.vol_label.config(text=f"{pct}%")
            if pct > 0:
                self.last_nonzero = pct
                self.muted = False
            else:
                self.muted = True
            self._update_mute_btn()

        self.show_state(msg)
        self.show_features(msg)
        self.update_player(state)
        self.log(f"OK: {self.device_label}")

    def on_error(self, msg):
        self.set_status("ошибка", ERR)
        text = str(msg)
        self.log("Ошибка: " + text.replace("\n", " "))

    def set_status(self, text, color=MUTED):
        self.status.config(text=text, fg=color)

    def log(self, text):
        stamp = time.strftime("%H:%M:%S")
        self.log_text.config(state="normal")
        self.log_text.insert("end", f"[{stamp}] {text}\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def show_state(self, msg):
        state = msg.get("state") or {}
        ps = state.get("playerState") or {}
        vol = state.get("volume")
        lines = [
            f"Прошивка: {msg.get('softwareVersion') or '—'}",
            f"Аккаунт: {self.login or '—'}",
            f"Громкость: {round((vol or 0) * 100)}%",
            f"Играет: {'да' if state.get('playing') else 'нет'}",
        ]
        if ps:
            lines += [
                f"Трек: {ps.get('title') or '—'}",
                f"Исполнитель: {ps.get('subtitle') or '—'}",
                f"Тип: {ps.get('playerType') or '—'}",
                "Прогресс: "
                f"{fmt_time(ps.get('progress'))} / {fmt_time(ps.get('duration'))}",
            ]
            ent = ps.get("entityInfo") or {}
            lines.append(
                f"Повтор: {ent.get('repeatMode', '—')}   "
                f"Перемешивание: {ent.get('shuffled', '—')}"
            )
        lines += ["", "Полное состояние:", json.dumps(msg, ensure_ascii=False, indent=2)]
        self._set_text(self.state_text, "\n".join(lines))

    def show_features(self, msg):
        sf = msg.get("supported_features") or []
        uf = msg.get("unsupported_features") or []
        lines = [f"Поддерживается ({len(sf)}):"]
        lines += [f"  + {x}" for x in sf]
        if uf:
            lines += ["", f"Не поддерживается ({len(uf)}):"]
            lines += [f"  − {x}" for x in uf]
        self._set_text(self.features_text, "\n".join(lines))

    def update_player(self, state):
        ps = state.get("playerState") or {}
        if ps:
            title = ps.get("title") or "Без названия"
            sub = ps.get("subtitle")
            self.track_label.config(text=f"{title}\n{sub}" if sub else title)
            self.play_state.config(
                text="играет" if state.get("playing") else "пауза",
                fg=OK if state.get("playing") else MUTED,
            )
            ent = ps.get("entityInfo") or {}
            self.repeat_mode = ent.get("repeatMode") or "None"
            self.repeat_btn.config(
                text="Повтор: "
                + {"All": "все", "One": "один", "None": "выкл"}.get(
                    self.repeat_mode, "выкл"
                )
            )
            if "shuffled" in ent:
                self.shuffle_var.set(bool(ent["shuffled"]))
        else:
            self.track_label.config(text="Ничего не играет")
            self.play_state.config(text="", fg=MUTED)

    # ------------------------------------------------------------- volume

    def _on_scale_move(self, value):
        self.vol_label.config(text=f"{int(value)}%")

    def _on_volume_commit(self, value):
        self.apply_volume()

    def _update_mute_btn(self):
        self.mute_btn.config(text="Включить звук" if self.muted else "Без звука")

    def step_volume(self, delta):
        pct = int(round((float(self.scale.get()) + delta) / STEPS) * STEPS)
        pct = max(0, min(100, pct))
        self.scale.set(pct)
        self.apply_volume()

    def apply_volume(self):
        pct = int(round(float(self.scale.get()) / STEPS) * STEPS)
        self.scale.set(pct)
        self.vol_label.config(text=f"{pct}%")
        if pct > 0:
            self.last_nonzero = pct
            self.muted = False
        else:
            self.muted = True
        self._update_mute_btn()
        self.set_status(f"громкость {pct}%…", WARN)

        def job():
            st = self._station()
            st.set_volume(pct)
            return st.probe()

        self.submit(job, self.on_probe, self.on_error)

    def toggle_mute(self):
        if self.muted:
            pct = self.last_nonzero or 35
        else:
            self.last_nonzero = int(float(self.scale.get())) or 35
            pct = 0
        self.muted = not self.muted
        self.scale.set(pct)
        self.apply_volume()

    # ----------------------------------------------------------- commands

    def send_cmd(self, payload, refresh=True, label=None):
        def job():
            st = self._station()
            resp = st.request(payload)
            msg = st.probe() if refresh else None
            return resp, msg

        def ok(result):
            resp, msg = result
            status = resp.get("status", "?")
            name = label or payload.get("command", "команда")
            self.log(f"{name}: {status}")
            if msg:
                self.on_probe((msg, None))

        self.submit(job, ok, self.on_error)

    def _cycle_repeat(self):
        order = [("None", "выкл"), ("All", "все"), ("One", "один")]
        current = next(
            (i for i, (mode, _) in enumerate(order) if mode == self.repeat_mode), 0
        )
        self.repeat_mode, label = order[(current + 1) % len(order)]
        self.repeat_btn.config(text=f"Повтор: {label}")
        self.send_cmd({"command": "repeat", "mode": self.repeat_mode}, label="повтор")

    def _on_shuffle(self):
        self.send_cmd(
            {"command": "shuffle", "enable": bool(self.shuffle_var.get())},
            label="перемешивание",
        )

    def say(self):
        text = self.tts_var.get().strip()
        if not text:
            return
        self.tts_var.set("")
        self.send_cmd({"command": "sendText", "text": text}, refresh=False, label="текст")

    def send_json(self):
        raw = self.adv_text.get("1.0", "end").strip()
        try:
            payload = json.loads(raw)
        except ValueError as e:
            self.log(f"JSON не разобран: {e}")
            return
        if not isinstance(payload, dict) or "command" not in payload:
            self.log("В JSON должно быть поле command")
            return
        self.send_cmd(payload)

    # -------------------------------------------------------------- misc

    def _select_device(self, key, label):
        if key == self.device_key:
            return
        self.device_key = key
        self.device_label = label
        self.device_btn.config(text=label)

        def job():
            if self.station is not None:
                self.station.close()
            self.station = None

        self.submit(job, lambda _: self.connect(), self.on_error)

    def _on_close(self):
        self._closing = True
        try:
            if self.station is not None:
                self.station.close()
        except Exception:
            pass
        self.destroy()


def main():
    selftest = "--selftest" in sys.argv
    app = App(autoconnect=not selftest, hidden=selftest)
    if selftest:
        app.update()
        app.destroy()
        return
    app.mainloop()


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(APP, str(e.code) if e.code else "Остановлено")
        root.destroy()
    except Exception:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(APP, traceback.format_exc())
        root.destroy()
