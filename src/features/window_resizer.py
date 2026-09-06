"""
Resize, reposition and remember the geometry of Roblox client windows.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Callable

import win32api
import win32con
import win32gui

from classes.operation_result import OperationResult
from utils.app_paths import get_data_dir
import features.presence as presence_mod

MIN_WIDTH = 320
MIN_HEIGHT = 240
UNLOCKED_MIN_WIDTH = 1
UNLOCKED_MIN_HEIGHT = 1
MAX_WIDTH = 7680
MAX_HEIGHT = 4320

DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 720

SWP_NOSENDCHANGING = getattr(win32con, "SWP_NOSENDCHANGING", 0x0400)

_BORDER_STYLES = (
    win32con.WS_CAPTION
    | win32con.WS_THICKFRAME
    | win32con.WS_MINIMIZEBOX
    | win32con.WS_MAXIMIZEBOX
    | win32con.WS_SYSMENU
)

_LAYOUT_FILE = os.path.join(get_data_dir(), "window_layout.json")
_LAYOUT_LOCK = threading.RLock()
_LAYOUT_CACHE: dict | None = None


def clamp_size(width: int, height: int, unlocked: bool = False) -> tuple[int, int]:
    try:
        width = int(width)
        height = int(height)
    except (TypeError, ValueError):
        return DEFAULT_WIDTH, DEFAULT_HEIGHT
    lowest_width = UNLOCKED_MIN_WIDTH if unlocked else MIN_WIDTH
    lowest_height = UNLOCKED_MIN_HEIGHT if unlocked else MIN_HEIGHT
    width = max(lowest_width, min(MAX_WIDTH, width))
    height = max(lowest_height, min(MAX_HEIGHT, height))
    return width, height


def load_layouts() -> dict[str, dict[str, int]]:
    global _LAYOUT_CACHE
    with _LAYOUT_LOCK:
        if _LAYOUT_CACHE is not None:
            return {name: dict(box) for name, box in _LAYOUT_CACHE.items()}

        layouts: dict[str, dict[str, int]] = {}
        if os.path.exists(_LAYOUT_FILE):
            try:
                with open(_LAYOUT_FILE, "r", encoding="utf-8") as handle:
                    loaded = json.load(handle)
            except (OSError, ValueError, TypeError):
                loaded = {}
            if isinstance(loaded, dict):
                for name, box in loaded.items():
                    if not isinstance(box, dict):
                        continue
                    try:
                        layouts[str(name)] = {
                            "x": int(box["x"]),
                            "y": int(box["y"]),
                            "width": int(box["width"]),
                            "height": int(box["height"]),
                        }
                    except (KeyError, TypeError, ValueError):
                        continue
        _LAYOUT_CACHE = layouts
        return {name: dict(box) for name, box in layouts.items()}


def save_layouts(layouts: dict[str, dict[str, int]]) -> None:
    global _LAYOUT_CACHE
    with _LAYOUT_LOCK:
        if _LAYOUT_CACHE == layouts:
            return

    os.makedirs(get_data_dir(), exist_ok=True)
    temp_file = _LAYOUT_FILE + ".tmp"
    try:
        with open(temp_file, "w", encoding="utf-8") as handle:
            json.dump(layouts, handle, indent=2)
        os.replace(temp_file, _LAYOUT_FILE)
    except OSError as exc:
        print(f"[Window Resizer] Layout save failed: {exc}")
        if os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except OSError:
                pass
        return

    with _LAYOUT_LOCK:
        _LAYOUT_CACHE = {name: dict(box) for name, box in layouts.items()}


def forget_layouts() -> None:
    save_layouts({})


def get_roblox_windows() -> dict[int, int]:
    """Main window handle for every running Roblox client, keyed by pid."""
    pids = set(presence_mod.get_roblox_processes())
    if not pids:
        return {}

    largest: dict[int, tuple[int, int]] = {}
    for pid, handles in presence_mod.get_windows_by_pid(pids).items():
        for hwnd in handles:
            try:
                if not win32gui.IsWindowVisible(hwnd):
                    continue
                if win32gui.GetWindow(hwnd, win32con.GW_OWNER):
                    continue
                if not win32gui.GetWindowText(hwnd).strip():
                    continue
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                area = max(0, right - left) * max(0, bottom - top)
                if area > largest.get(pid, (0, 0))[0]:
                    largest[pid] = (area, hwnd)
            except Exception:
                continue
    return {pid: handle for pid, (_, handle) in largest.items()}


def get_window_box(hwnd: int) -> dict[str, int] | None:
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    except Exception:
        return None
    width = right - left
    height = bottom - top
    if width <= 0 or height <= 0:
        return None
    return {"x": left, "y": top, "width": width, "height": height}


def _is_minimized_or_maximized(hwnd: int) -> bool:
    try:
        placement = win32gui.GetWindowPlacement(hwnd)
    except Exception:
        return False
    if not placement or len(placement) < 2:
        return False
    return placement[1] in (
        win32con.SW_SHOWMINIMIZED,
        win32con.SW_SHOWMAXIMIZED,
        win32con.SW_MAXIMIZE,
        win32con.SW_MINIMIZE,
    )


def _work_area_for(hwnd: int) -> tuple[int, int, int, int]:
    try:
        monitor = win32api.MonitorFromWindow(
            hwnd, win32con.MONITOR_DEFAULTTONEAREST
        )
        return tuple(win32api.GetMonitorInfo(monitor)["Work"])
    except Exception:
        return 0, 0, win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1)


def set_borderless(hwnd: int, borderless: bool) -> None:
    try:
        style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
        new_style = style & ~_BORDER_STYLES if borderless else style | _BORDER_STYLES
        if new_style == style:
            return
        win32gui.SetWindowLong(hwnd, win32con.GWL_STYLE, new_style)
        win32gui.SetWindowPos(
            hwnd, 0, 0, 0, 0, 0,
            win32con.SWP_NOMOVE
            | win32con.SWP_NOSIZE
            | win32con.SWP_NOZORDER
            | win32con.SWP_NOACTIVATE
            | win32con.SWP_FRAMECHANGED,
        )
    except Exception as exc:
        print(f"[Window Resizer] Could not change the frame of window {hwnd}: {exc}")


def apply_to_window(
    hwnd: int,
    width: int,
    height: int,
    center: bool = True,
    position: tuple[int, int] | None = None,
    borderless: bool = False,
    unlocked: bool = False,
) -> bool:
    width, height = clamp_size(width, height, unlocked)
    try:
        if _is_minimized_or_maximized(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)

        set_borderless(hwnd, borderless)

        if position is not None:
            x, y = int(position[0]), int(position[1])
        elif center:
            left, top, right, bottom = _work_area_for(hwnd)
            x = left + max(0, (right - left - width) // 2)
            y = top + max(0, (bottom - top - height) // 2)
        else:
            current_left, current_top, _, _ = win32gui.GetWindowRect(hwnd)
            x, y = current_left, current_top

        flags = win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE
        if unlocked:
            flags |= SWP_NOSENDCHANGING

        win32gui.SetWindowPos(hwnd, 0, x, y, width, height, flags)
        return True
    except Exception as exc:
        print(f"[Window Resizer] Could not resize window {hwnd}: {exc}")
        return False


def apply_to_all(
    width: int,
    height: int,
    center: bool = True,
    position: tuple[int, int] | None = None,
    borderless: bool = False,
    unlocked: bool = False,
) -> OperationResult:
    windows = get_roblox_windows()
    if not windows:
        return OperationResult.failure(
            "ROBLOX_WINDOWS_NOT_FOUND",
            "No Roblox Windows Found",
            "Open at least one visible Roblox window first.",
        )

    resized = sum(
        1 for hwnd in windows.values()
        if apply_to_window(hwnd, width, height, center, position, borderless, unlocked)
    )
    if not resized:
        return OperationResult.failure(
            "ROBLOX_WINDOWS_RESIZE_FAILED",
            "Roblox Windows Could Not Be Resized",
            "Windows prevented the Roblox windows from being resized.",
        )

    applied_width, applied_height = clamp_size(width, height, unlocked)
    print(
        f"[Window Resizer] Resized {resized} Roblox window(s) "
        f"to {applied_width}x{applied_height}."
    )
    return OperationResult.success(
        f"Resized {resized} Roblox window(s) to {applied_width}x{applied_height}.",
        data={"count": resized, "width": applied_width, "height": applied_height},
    )


class RobloxWindowResizer:
    """
    Applies the configured size to Roblox windows as they appear and remembers
    where each account's window was, so a client that Auto Connect relaunches
    comes back at the same place and size.
    """

    SAVE_INTERVAL = 10.0

    def __init__(
        self,
        get_settings: Callable[[], dict],
        resolve_accounts: Callable[[], dict[int, str]] | None = None,
        interval_sec: float = 2.0,
    ):
        self._get_settings = get_settings
        self._resolve_accounts = resolve_accounts or (lambda: {})
        self._interval = max(1.0, float(interval_sec))
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._handled: dict[int, int] = {}
        self._signature: tuple | None = None
        self._layouts = load_layouts()
        self._layouts_dirty = False
        self._last_save = 0.0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._handled.clear()
        self._layouts = load_layouts()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="RobloxWindowResizer",
        )
        self._thread.start()
        print("[Window Resizer] Started.")

    def stop(self, join_timeout: float = 2.0) -> None:
        if not self._thread:
            return
        self._stop_event.set()
        thread = self._thread
        self._thread = None
        if thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=join_timeout)
        self._flush_layouts(force=True)
        self._handled.clear()
        print("[Window Resizer] Stopped.")

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def forget(self) -> None:
        self._handled.clear()
        self._signature = None

    def clear_layouts(self) -> None:
        self._layouts = {}
        self._layouts_dirty = False
        forget_layouts()

    def get_layouts(self) -> dict[str, dict[str, int]]:
        return {name: dict(box) for name, box in self._layouts.items()}

    def _flush_layouts(self, force: bool = False) -> None:
        if not self._layouts_dirty:
            return
        now = time.monotonic()
        if not force and now - self._last_save < self.SAVE_INTERVAL:
            return
        save_layouts(self._layouts)
        self._layouts_dirty = False
        self._last_save = now

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._apply_once()
                self._flush_layouts()
            except Exception as exc:
                print(f"[Window Resizer] Scan failed: {type(exc).__name__}: {exc}")
            if self._stop_event.wait(self._interval):
                break
        self._flush_layouts(force=True)

    def _apply_once(self) -> None:
        settings = self._get_settings() or {}
        resize_enabled = bool(settings.get("roblox_window_resize_enabled", False))
        remember_enabled = bool(settings.get("roblox_window_remember_position", False))
        if not resize_enabled and not remember_enabled:
            return

        unlocked = bool(settings.get("roblox_window_unlock_size", False))
        width, height = clamp_size(
            settings.get("roblox_window_width", DEFAULT_WIDTH),
            settings.get("roblox_window_height", DEFAULT_HEIGHT),
            unlocked,
        )
        center = bool(settings.get("roblox_window_center", True))
        borderless = bool(settings.get("roblox_window_borderless", False))

        signature = (resize_enabled, remember_enabled, width, height, center, borderless, unlocked)
        if signature != self._signature:
            self._signature = signature
            self._handled.clear()

        windows = get_roblox_windows()
        for pid in list(self._handled):
            if pid not in windows:
                self._handled.pop(pid, None)
        if not windows:
            return

        accounts = self._resolve_accounts() if remember_enabled else {}

        for pid, hwnd in windows.items():
            account = accounts.get(pid, "")
            if self._handled.get(pid) == hwnd:
                if remember_enabled and account:
                    self._record(account, hwnd)
                continue

            saved = self._layouts.get(account) if (remember_enabled and account) else None
            position = (saved["x"], saved["y"]) if saved else None
            target_width, target_height = width, height
            if not resize_enabled:
                if saved:
                    target_width, target_height = saved["width"], saved["height"]
                else:
                    box = get_window_box(hwnd)
                    if box is None:
                        continue
                    target_width, target_height = box["width"], box["height"]

            if apply_to_window(
                hwnd, target_width, target_height,
                center=center and position is None,
                position=position,
                borderless=borderless,
                unlocked=unlocked,
            ):
                self._handled[pid] = hwnd
                if remember_enabled and account:
                    self._record(account, hwnd)

    def _record(self, account: str, hwnd: int) -> None:
        if _is_minimized_or_maximized(hwnd):
            return
        box = get_window_box(hwnd)
        if box is None:
            return
        if self._layouts.get(account) == box:
            return
        self._layouts[account] = box
        self._layouts_dirty = True
