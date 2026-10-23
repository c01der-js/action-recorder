#!/usr/bin/env python3
"""
Рекордер действий пользователя — Windows (с учётом 1С) и macOS.

Пишет клики, перетаскивания, прокрутку, ввод текста (в т.ч. кириллицу),
горячие клавиши, буфер обмена, активное приложение/окно, скриншоты,
а на Windows — название элемента интерфейса под курсором (UI Automation).

Терминальный сервер (1С на сервере, операторы по RDP): запускается внутри
RDP-сессии каждого оператора — автозапуском при входе (start_recorder.bat).
Один экземпляр на сеанс, новая папка каждые сутки, файлы сбрасываются на диск
раз в минуту (на случай выхода из сеанса).

  Ctrl+Shift+F11 — пауза / продолжить
  Ctrl+Shift+F12 — остановить (или Ctrl+C в окне консоли)

Результат — папка <base>/<пользователь>/<дата_время>/:
  events.jsonl    — журнал действий
  elements.jsonl  — элементы интерфейса под курсором при кликах (Windows)
  moves.jsonl     — траектория мыши (раз в 0.2 c)
  shots/*.jpg     — скриншоты (красный круг = место клика)
  meta.json       — длительность, счётчики, мониторы, пользователь
"""
import argparse
import getpass
import json
import os
import platform
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

if IS_WIN:
    import ctypes
    from ctypes import wintypes

    # Координаты мыши и скриншотов в одних и тех же физических пикселях
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

import mss  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402
from pynput import keyboard, mouse  # noqa: E402

MOVE_INTERVAL = 0.2   # сек между точками траектории мыши
DRAG_THRESHOLD = 12   # пикселей: больше — перетаскивание, а не клик
SCROLL_MERGE = 0.8    # сек: прокрутки ближе друг к другу склеиваются
KEY_MERGE = 1.0       # сек: повторы одной клавиши (стрелки, Tab) склеиваются
CLIP_MAX = 300        # символов буфера обмена в журнале
ONEC_EXE = {"1cv8c.exe", "1cv8.exe", "1cv8s.exe"}

SHOT_KEYS = {"enter", "tab", "esc", "delete", "insert"} | {f"f{i}" for i in range(1, 13)}

MODIFIERS = {}
for _name, _label in [
    ("cmd", "win" if IS_WIN else "cmd"), ("cmd_l", "win" if IS_WIN else "cmd"),
    ("cmd_r", "win" if IS_WIN else "cmd"),
    ("ctrl", "ctrl"), ("ctrl_l", "ctrl"), ("ctrl_r", "ctrl"),
    ("alt", "alt"), ("alt_l", "alt"), ("alt_r", "alt"), ("alt_gr", "alt"),
    ("shift", "shift"), ("shift_l", "shift"), ("shift_r", "shift"),
]:
    _k = getattr(keyboard.Key, _name, None)
    if _k is not None:
        MODIFIERS[_k] = _label


def is_onec(app, title):
    return bool((app and app.lower() in ONEC_EXE)
                or (title and ("1С:Предприятие" in title or "1C:Enterprise" in title
                               or "1С:Бухгалтерия" in title)))


# ======================= Windows: системные функции =======================
if IS_WIN:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.InternalGetWindowText.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.InternalGetWindowText.restype = ctypes.c_int
    user32.GetKeyboardLayout.argtypes = [wintypes.DWORD]
    user32.GetKeyboardLayout.restype = ctypes.c_void_p
    user32.MapVirtualKeyExW.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p]
    user32.MapVirtualKeyExW.restype = wintypes.UINT
    user32.ToUnicodeEx.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_ubyte),
                                   wintypes.LPWSTR, ctypes.c_int, wintypes.UINT, ctypes.c_void_p]
    user32.ToUnicodeEx.restype = ctypes.c_int
    user32.GetKeyState.argtypes = [ctypes.c_int]
    user32.GetKeyState.restype = ctypes.c_short
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                    ctypes.POINTER(wintypes.DWORD)]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]

    _proc_cache = {}

    def _proc_name(pid):
        if pid in _proc_cache:
            return _proc_cache[pid]
        name = None
        h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if h:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                name = os.path.basename(buf.value)
            kernel32.CloseHandle(h)
        if len(_proc_cache) > 500:
            _proc_cache.clear()
        _proc_cache[pid] = name
        return name

    def active_window():
        try:
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return {"app": None, "title": None, "onec": False}
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            buf = ctypes.create_unicode_buffer(512)
            user32.InternalGetWindowText(hwnd, buf, 512)  # не шлёт сообщений — не зависнет
            app, title = _proc_name(pid.value), buf.value or None
            return {"app": app, "title": title, "onec": is_onec(app, title)}
        except Exception:
            return {"app": None, "title": None, "onec": False}

    def win_char(vk, shift):
        """Символ клавиши в раскладке активного окна (кириллица, цифры, знаки)."""
        try:
            tid = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
            hkl = user32.GetKeyboardLayout(tid)
            state = (ctypes.c_ubyte * 256)()
            if shift:
                state[0x10] = 0x80
            if user32.GetKeyState(0x14) & 1:  # Caps Lock
                state[0x14] = 0x01
            sc = user32.MapVirtualKeyExW(vk, 0, hkl)
            buf = ctypes.create_unicode_buffer(8)
            # флаг 4: не менять состояние клавиатуры (не ломает мёртвые клавиши)
            n = user32.ToUnicodeEx(vk, sc, state, buf, 8, 4, hkl)
            if n > 0:
                return buf.value[:n]
        except Exception:
            pass
        return None

    def read_clipboard():
        for _ in range(10):
            if user32.OpenClipboard(None):
                break
            time.sleep(0.05)
        else:
            return None
        try:
            h = user32.GetClipboardData(13)  # CF_UNICODETEXT
            if not h:
                return None
            p = kernel32.GlobalLock(h)
            if not p:
                return None
            try:
                return ctypes.wstring_at(p)
            finally:
                kernel32.GlobalUnlock(h)
        finally:
            user32.CloseClipboard()

    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    _mutex = None

    def single_instance():
        """Один рекордер на сессию пользователя (Local\\ — своё пространство у каждой RDP-сессии)."""
        global _mutex
        _mutex = kernel32.CreateMutexW(None, False, "Local\\ActionRecorderSingleInstance")
        return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS

# ======================= macOS: системные функции =======================
else:
    try:
        from Quartz import (
            CGWindowListCopyWindowInfo,
            kCGNullWindowID,
            kCGWindowListExcludeDesktopElements,
            kCGWindowListOptionOnScreenOnly,
        )
    except ImportError:
        CGWindowListCopyWindowInfo = None

    def active_window():
        if CGWindowListCopyWindowInfo is None:
            return {"app": None, "title": None, "onec": False}
        try:
            opts = kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements
            for w in CGWindowListCopyWindowInfo(opts, kCGNullWindowID) or []:
                if w.get("kCGWindowLayer") == 0:
                    app = w.get("kCGWindowOwnerName")
                    title = w.get("kCGWindowName")
                    app = str(app) if app else None
                    title = str(title) if title else None
                    return {"app": app, "title": title, "onec": is_onec(app, title)}
        except Exception:
            pass
        return {"app": None, "title": None, "onec": False}

    def win_char(vk, shift):
        return None

    def single_instance():
        return True

    def read_clipboard():
        if not IS_MAC:
            return None
        try:
            r = subprocess.run(["pbpaste"], capture_output=True, timeout=2)
            return r.stdout.decode("utf-8", "replace")
        except Exception:
            return None


_VK_NAMES = {0x6A: "num*", 0x6B: "num+", 0x6D: "num-", 0x6E: "num.", 0x6F: "num/",
             0xBA: ";", 0xBB: "=", 0xBC: ",", 0xBD: "-", 0xBE: ".", 0xBF: "/", 0xC0: "`",
             0xDB: "[", 0xDC: "\\", 0xDD: "]", 0xDE: "'"}


def key_name(key):
    """Имя клавиши для горячих клавиш — не зависит от раскладки (ctrl+c, а не ctrl+с)."""
    if isinstance(key, keyboard.Key):
        return key.name
    vk = getattr(key, "vk", None)
    if IS_WIN and vk is not None:
        if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
            return chr(vk).lower()
        if 0x60 <= vk <= 0x69:
            return f"num{vk - 0x60}"
        if vk in _VK_NAMES:
            return _VK_NAMES[vk]
    ch = getattr(key, "char", None)
    if ch and ch.isprintable():
        return ch.lower()
    return f"vk{vk}"


class Recorder:
    def __init__(self, out_dir: Path, record_text=True, screenshots=True, clipboard=True,
                 elements=True, max_width=1440, max_minutes=None, rotate_daily=False):
        self.rotate_daily = rotate_daily
        self.dir = out_dir
        self.shots_dir = out_dir / "shots"
        self.shots_dir.mkdir(parents=True, exist_ok=True)
        self.record_text = record_text
        self.screenshots = screenshots
        self.clipboard = clipboard
        self.max_width = max_width
        self.max_seconds = max_minutes * 60 if max_minutes else None

        self.events_f = open(out_dir / "events.jsonl", "a", encoding="utf-8")
        self.moves_f = open(out_dir / "moves.jsonl", "a", encoding="utf-8")
        self.lock = threading.RLock()
        self.t0 = time.time()
        self.started_at = datetime.now()
        self.stop_evt = threading.Event()
        self.paused = False

        self.mods = set()
        self.text_buf = []
        self.text_t = None
        self.scroll = None
        self.key_rep = None
        self.press = None        # (x, y, t, ref, shot)
        self.last_click = None   # (x, y, t, button, count)
        self.last_move_t = 0.0
        self.last_app = None
        self.counts = {}
        self.ref_n = 0
        self.mouse_ctl = mouse.Controller()

        self.shot_q = queue.Queue()
        self.aux_q = queue.Queue()
        self.threads = [threading.Thread(target=self._shot_worker, daemon=True),
                        threading.Thread(target=self._aux_worker, daemon=True)]
        self.elem_q = None
        if IS_WIN and elements:
            self.elem_q = queue.Queue()
            self.elements_f = open(out_dir / "elements.jsonl", "a", encoding="utf-8")
            self.threads.append(threading.Thread(target=self._elem_worker, daemon=True))
        for th in self.threads:
            th.start()

    # ---------- журнал ----------
    def now(self):
        return round(time.time() - self.t0, 3)

    def new_ref(self):
        self.ref_n += 1
        return f"{self.ref_n:05d}"

    def _write(self, ev):
        self.events_f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        self.events_f.flush()
        self.counts[ev["type"]] = self.counts.get(ev["type"], 0) + 1

    def emit(self, type_, t=None, **data):
        with self.lock:
            ts = self.now() if t is None else t
            win = active_window()
            if win["app"] != self.last_app:
                self._write({"t": ts, "type": "app_switch", "from": self.last_app, **win})
                self.last_app = win["app"]
            self._write({"t": ts, "type": type_, **data, **win})

    def flush_pending(self):
        """Сбрасывает накопленный ввод текста, прокрутку и повторы клавиш в журнал."""
        with self.lock:
            if self.text_buf:
                txt = "".join(self.text_buf)
                self.text_buf = []
                self.emit("type", t=self.text_t,
                          text=txt if self.record_text else f"<скрыто: {len(txt)} симв.>")
            if self.scroll:
                s, self.scroll = self.scroll, None
                self.emit("scroll", t=s["t"], x=s["x"], y=s["y"], dx=s["dx"], dy=s["dy"],
                          ref=s["ref"], shot=s["shot"])
            if self.key_rep:
                k, self.key_rep = self.key_rep, None
                self.emit("key", t=k["t"], key=k["key"], times=k["n"], ref=k["ref"], shot=k["shot"])

    # ---------- скриншоты и элементы ----------
    def request_shot(self, ref, x=None, y=None):
        if not self.screenshots:
            return None
        mark = x is not None
        if x is None:
            x, y = self.mouse_ctl.position
        name = f"{ref}.jpg"
        self.shot_q.put((name, x, y, mark))
        return f"shots/{name}"

    def request_element(self, ref, x, y):
        if self.elem_q is not None:
            self.elem_q.put((ref, int(x), int(y)))

    def _shot_worker(self):
        with mss.mss() as sct:
            while True:
                item = self.shot_q.get()
                try:
                    if item is None:
                        return
                    self._grab(sct, *item)
                except Exception as e:
                    print(f"[ошибка скриншота] {e}")
                finally:
                    self.shot_q.task_done()

    def _grab(self, sct, name, x, y, mark):
        mons = sct.monitors[1:] or sct.monitors
        mon = next((m for m in mons
                    if m["left"] <= x < m["left"] + m["width"]
                    and m["top"] <= y < m["top"] + m["height"]), mons[0])
        raw = sct.grab(mon)
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        scale = img.width / mon["width"]  # Retina на Mac; на Windows = 1
        if mark:
            cx, cy = (x - mon["left"]) * scale, (y - mon["top"]) * scale
            r = 18 * scale
            ImageDraw.Draw(img).ellipse((cx - r, cy - r, cx + r, cy + r),
                                        outline=(255, 0, 0), width=max(3, int(3 * scale)))
        if img.width > self.max_width:
            img = img.resize((self.max_width, int(img.height * self.max_width / img.width)),
                             Image.LANCZOS)
        img.save(self.shots_dir / name, "JPEG", quality=70)

    def _elem_worker(self):
        """Windows UI Automation: что за элемент под курсором (кнопка, поле, строка таблицы)."""
        try:
            import uiautomation as auto
        except Exception:
            auto = None
        if auto is None:
            while self.elem_q.get() is not None:
                pass
            return
        with auto.UIAutomationInitializerInThread(debug=False):
            while True:
                item = self.elem_q.get()
                if item is None:
                    return
                ref, x, y = item
                try:
                    c = auto.ControlFromPoint(x, y)
                    if not c:
                        continue
                    parents, p = [], c.GetParentControl()
                    while p and len(parents) < 3:
                        if p.Name:
                            parents.append(p.Name[:80])
                        p = p.GetParentControl()
                    info = {"ref": ref, "name": (c.Name or "")[:120], "type": c.ControlTypeName,
                            "aid": c.AutomationId or None, "parents": parents}
                    if info["name"] or parents:
                        self.elements_f.write(json.dumps(info, ensure_ascii=False) + "\n")
                        self.elements_f.flush()
                except Exception:
                    pass

    def _aux_worker(self):
        """Чтение буфера обмена после копирования/вставки (не в обработчике клавиатуры)."""
        while True:
            item = self.aux_q.get()
            if item is None:
                return
            op, t, delay = item
            try:
                time.sleep(delay)
                txt = read_clipboard()
                if txt:
                    txt = txt.strip()
                    shown = (txt[:CLIP_MAX] + ("…" if len(txt) > CLIP_MAX else "")) if self.record_text \
                        else f"<скрыто: {len(txt)} симв.>"
                    self.emit("clipboard", t=t, op=op, text=shown)
            except Exception:
                pass

    # ---------- мышь ----------
    def on_move(self, x, y):
        if self.paused:
            return
        t = time.time()
        if t - self.last_move_t < MOVE_INTERVAL:
            return
        self.last_move_t = t
        with self.lock:
            self.moves_f.write(json.dumps({"t": self.now(), "x": int(x), "y": int(y)}) + "\n")

    def on_click(self, x, y, button, pressed):
        if self.paused:
            return
        btn = getattr(button, "name", str(button))
        with self.lock:
            if pressed:
                self.flush_pending()
                ref = self.new_ref()
                self.press = (x, y, self.now(), ref, self.request_shot(ref, x, y))
                self.request_element(ref, x, y)
                return
            if not self.press:
                return
            px, py, pt, ref, shot = self.press
            self.press = None
            if abs(x - px) + abs(y - py) > DRAG_THRESHOLD:
                self.emit("drag", t=pt, button=btn, x=int(px), y=int(py),
                          to_x=int(x), to_y=int(y), ref=ref, shot=shot)
                return
            count = 1
            lc = self.last_click
            if lc and lc[3] == btn and pt - lc[2] < 0.45 and abs(px - lc[0]) + abs(py - lc[1]) < 8:
                count = lc[4] + 1
            self.last_click = (px, py, pt, btn, count)
            self.emit("click", t=pt, button=btn, x=int(px), y=int(py), clicks=count, ref=ref, shot=shot)

    def on_scroll(self, x, y, dx, dy):
        if self.paused:
            return
        with self.lock:
            s = self.scroll
            if s and self.now() - s["last"] < SCROLL_MERGE:
                s["dx"] += dx
                s["dy"] += dy
                s["last"] = self.now()
                return
            self.flush_pending()
            ref = self.new_ref()
            self.scroll = {"t": self.now(), "last": self.now(), "x": int(x), "y": int(y),
                           "dx": dx, "dy": dy, "ref": ref, "shot": self.request_shot(ref, x, y)}

    # ---------- клавиатура ----------
    def on_press(self, key):
        with self.lock:
            if key in MODIFIERS:
                self.mods.add(MODIFIERS[key])
                return
            name = key_name(key)

            if {"ctrl", "shift"} <= self.mods and name in ("f11", "f12"):
                if name == "f12":
                    self.stop_evt.set()
                    return False
                self.flush_pending()
                self.paused = not self.paused
                self.emit("pause" if self.paused else "resume")
                print("⏸  Пауза (Ctrl+Shift+F11 — продолжить)" if self.paused else "▶️  Запись продолжена")
                return
            if self.paused:
                return

            # горячие клавиши: Ctrl/Alt/Win/Cmd + клавиша
            combo = sorted(self.mods - {"shift"})
            if combo:
                if "shift" in self.mods:
                    combo.append("shift")
                keys = "+".join(combo + [name])
                self.flush_pending()
                ref = self.new_ref()
                self.emit("hotkey", keys=keys, ref=ref, shot=self.request_shot(ref))
                self._maybe_clipboard(combo, name)
                return
            if self.mods == {"shift"} and name == "insert":  # Shift+Ins — вставка
                self._maybe_clipboard(["shift"], name)

            # печатаемый символ
            ch = " " if key == keyboard.Key.space else None
            if ch is None and not isinstance(key, keyboard.Key):
                vk = getattr(key, "vk", None)
                ch = (win_char(vk, "shift" in self.mods) if (IS_WIN and vk) else None) \
                    or getattr(key, "char", None)
            if ch and ch.isprintable():
                if self.scroll or self.key_rep:
                    self.flush_pending()
                if not self.text_buf:
                    self.text_t = self.now()
                self.text_buf.append(ch)
                return
            if key == keyboard.Key.backspace and self.text_buf:
                self.text_buf.pop()
                return

            # прочие клавиши: Enter, Tab, стрелки, F-клавиши 1С (F4, F8, F9…)
            kr = self.key_rep
            if kr and kr["key"] == name and self.now() - kr["last"] < KEY_MERGE:
                kr["n"] += 1
                kr["last"] = self.now()
                return
            self.flush_pending()
            ref = self.new_ref()
            self.key_rep = {"key": name, "n": 1, "t": self.now(), "last": self.now(), "ref": ref,
                            "shot": self.request_shot(ref) if name in SHOT_KEYS else None}

    def _maybe_clipboard(self, combo, name):
        if not self.clipboard:
            return
        main = {"ctrl", "cmd"} & set(combo)
        op = None
        if main and name in ("c", "x"):
            op = "copy" if name == "c" else "cut"
        elif main and name == "v":
            op = "paste"
        elif combo == ["ctrl"] and name == "insert":
            op = "copy"
        elif combo == ["shift"] and name == "insert":
            op = "paste"
        if op:
            self.aux_q.put((op, self.now(), 0.0 if op == "paste" else 0.3))

    def on_release(self, key):
        if key in MODIFIERS:
            with self.lock:
                self.mods.discard(MODIFIERS[key])

    # ---------- запуск ----------
    def write_meta(self, final=False):
        try:
            with mss.mss() as s:
                monitors = s.monitors[1:]
        except Exception:
            monitors = []
        meta = {
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "ended_at": datetime.now().isoformat(timespec="seconds"),
            "finished": final,
            "duration_sec": self.now(),
            "counts": self.counts,
            "screenshots": self.screenshots,
            "monitors": monitors,
            "platform": platform.platform(),
            "user": getpass.getuser(),
            "machine": platform.node(),
            "rdp_session": os.environ.get("SESSIONNAME", ""),
            "record_text": self.record_text,
        }
        (self.dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def run(self):
        """Возвращает причину остановки: 'stop', 'limit' или 'rotate' (сменились сутки)."""
        self.emit("start")
        self.write_meta()
        reason, last_meta = "stop", time.time()
        with mouse.Listener(on_move=self.on_move, on_click=self.on_click, on_scroll=self.on_scroll), \
                keyboard.Listener(on_press=self.on_press, on_release=self.on_release):
            try:
                while not self.stop_evt.wait(0.5):
                    if self.max_seconds and time.time() - self.t0 > self.max_seconds:
                        print("⏱  Достигнут лимит времени записи")
                        reason = "limit"
                        break
                    if self.rotate_daily and datetime.now().date() != self.started_at.date():
                        reason = "rotate"
                        break
                    if time.time() - last_meta > 60:
                        # при выходе из сеанса процесс убивается — держим файлы актуальными
                        last_meta = time.time()
                        with self.lock:
                            self.moves_f.flush()
                        self.write_meta()
            except KeyboardInterrupt:
                pass
        self.flush_pending()
        self.emit("stop")
        time.sleep(0.5)  # дать дописаться буферу обмена
        for q in (self.shot_q, self.aux_q, self.elem_q):
            if q is not None:
                q.put(None)
        for th in self.threads:
            th.join(timeout=60)
        self.events_f.close()
        self.moves_f.close()
        if self.elem_q is not None:
            self.elements_f.close()
        self.write_meta(final=True)
        return reason


def selftest(out_path=None):
    """Проверка, что все компоненты работают на этой машине (и что .exe собран правильно).
    Код выхода: 0 — компоненты загружаются, 2 — не хватает модуля (ошибка сборки)."""
    res = {"platform": platform.platform(), "frozen": bool(getattr(sys, "frozen", False)),
           "user": getpass.getuser(), "rdp_session": os.environ.get("SESSIONNAME", "")}

    def chk(name, fn):
        try:
            res[name] = fn() or "ok"
        except Exception as e:
            res[name] = f"ERROR: {e!r}"

    chk("active_window", active_window)

    def shot():
        with mss.mss() as s:
            mon = s.monitors[1] if len(s.monitors) > 1 else s.monitors[0]
            raw = s.grab(mon)
            Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
            return f"ok {raw.size[0]}x{raw.size[1]}"
    chk("screenshot", shot)

    def hooks():
        m, k = mouse.Listener(), keyboard.Listener()
        m.start()
        k.start()
        time.sleep(0.5)
        m.stop()
        k.stop()
    chk("input_hooks", hooks)

    if IS_WIN:
        def uia():
            import uiautomation as auto
            with auto.UIAutomationInitializerInThread(debug=False):
                c = auto.ControlFromPoint(10, 10)
                return f"ok {c.ControlTypeName if c else 'нет элемента'}"
        chk("ui_automation", uia)
        chk("clipboard", lambda: f"ok, символов: {len(read_clipboard() or '')}")
        chk("single_instance", lambda: "ok" if single_instance() else "уже запущен другой экземпляр")

    text = json.dumps(res, ensure_ascii=False, indent=2)
    print(text)
    if out_path:
        Path(out_path).write_text(text, encoding="utf-8")
    missing = any(isinstance(v, str) and ("ModuleNotFoundError" in v or "ImportError" in v)
                  for v in res.values())
    return 2 if missing else 0


def safe_name(s):
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in (s or "user"))


def main():
    p = argparse.ArgumentParser(description="Запись действий пользователя (Windows / macOS, с учётом 1С)")
    p.add_argument("--base", default=os.environ.get("RECORDER_DIR", "sessions"),
                   help="корневая папка записей; внутри — <пользователь>/<дата_время> "
                        "(по умолчанию sessions или переменная RECORDER_DIR)")
    p.add_argument("--out", help="конкретная папка сессии (отключает разбивку по дням)")
    p.add_argument("--no-rotate", action="store_true", help="не начинать новую папку в полночь")
    p.add_argument("--no-text", action="store_true", help="не сохранять набранный текст и буфер обмена, только длину")
    p.add_argument("--no-screens", action="store_true", help="без скриншотов")
    p.add_argument("--no-clipboard", action="store_true", help="не записывать буфер обмена")
    p.add_argument("--no-elements", action="store_true", help="не опрашивать UI Automation (Windows)")
    p.add_argument("--max-minutes", type=float, help="остановить запись автоматически через N минут")
    p.add_argument("--selftest", nargs="?", const="", metavar="FILE",
                   help="проверить компоненты (скриншоты, перехват ввода, UI Automation) и выйти")
    a = p.parse_args()

    if a.selftest is not None:
        sys.exit(selftest(a.selftest or None))

    if not single_instance():
        print("Рекордер уже запущен в этом сеансе")
        return

    user_dir = Path(a.base) / safe_name(getpass.getuser())
    while True:
        out = Path(a.out) if a.out else user_dir / f"{datetime.now():%Y-%m-%d_%H-%M-%S}"
        out.mkdir(parents=True, exist_ok=True)
        print(f"● Запись идёт → {out}\n  Ctrl+Shift+F11 — пауза, Ctrl+Shift+F12 — стоп (или Ctrl+C)")
        reason = Recorder(out, record_text=not a.no_text, screenshots=not a.no_screens,
                          clipboard=not a.no_clipboard, elements=not a.no_elements,
                          max_minutes=a.max_minutes, rotate_daily=not (a.out or a.no_rotate)).run()
        print(f"■ Готово: {out}\n  Дальше: python generate.py {out}")
        if reason != "rotate":
            break


if __name__ == "__main__":
    main()
