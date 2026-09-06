"""
Resize and reposition Roblox client windows.
"""

from __future__ import annotations

import threading
from typing import Callable

import win32api
import win32con
import win32gui

from classes.operation_result import OperationResult
import features.presence as presence_mod

MIN_WIDTH = 320
MIN_HEIGHT = 240
MAX_WIDTH = 7680
MAX_HEIGHT = 4320

DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 720

_BORDER_STYLES = (
    win32con.WS_CAPTION
    | win32con.WS_THICKFRAME
    | win32con.WS_MINIMIZEBOX
    | win32con.WS_MAXIMIZEBOX
    | win32con.WS_SYSMENU
)


def clamp_size(width: int, height: int) -> tuple[int, int]:
    try:
        width = int(width)
        height = int(height)
    except (TypeError, ValueError):
        return DEFAULT_WIDTH, DEFAULT_HEIGHT
    width = max(MIN_WIDTH, min(MAX_WIDTH, width))
    height = max(MIN_HEIGHT, min(MAX_HEIGHT, height))
    return width, height


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
) -> bool:
    width, height = clamp_size(width, height)
    try:
        if _is_minimized_or_maximized(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)

        set_borderless(hwnd, borderless)

        if center:
            left, top, right, bottom = _work_area_for(hwnd)
            x = left + max(0, (right - left - width) // 2)
            y = top + max(0, (bottom - top - height) // 2)
        elif position is not None:
            x, y = int(position[0]), int(position[1])
        else:
            current_left, current_top, _, _ = win32gui.GetWindowRect(hwnd)
            x, y = current_left, current_top

        win32gui.SetWindowPos(
            hwnd, 0, x, y, width, height,
            win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE,
        )
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
        if apply_to_window(hwnd, width, height, center, position, borderless)
    )
    if not resized:
        return OperationResult.failure(
            "ROBLOX_WINDOWS_RESIZE_FAILED",
            "Roblox Windows Could Not Be Resized",
            "Windows prevented the Roblox windows from being resized.",
        )

    applied_width, applied_height = clamp_size(width, height)
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
    Applies the configured size to Roblox windows as they appear, so a client
    that Auto Connect relaunches comes back at the same size.
    """

    def __init__(
        self,
        get_settings: Callable[[], dict],
        interval_sec: float = 2.0,
    ):
        self._get_settings = get_settings
        self._interval = max(1.0, float(interval_sec))
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._applied: dict[int, tuple[int, int, int, bool]] = {}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._applied.clear()
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
        self._applied.clear()
        print("[Window Resizer] Stopped.")

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def forget(self) -> None:
        self._applied.clear()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._apply_once()
            except Exception as exc:
                print(f"[Window Resizer] Scan failed: {type(exc).__name__}: {exc}")
            if self._stop_event.wait(self._interval):
                break

    def _apply_once(self) -> None:
        settings = self._get_settings() or {}
        if not settings.get("roblox_window_resize_enabled", False):
            return

        width, height = clamp_size(
            settings.get("roblox_window_width", DEFAULT_WIDTH),
            settings.get("roblox_window_height", DEFAULT_HEIGHT),
        )
        center = bool(settings.get("roblox_window_center", True))
        borderless = bool(settings.get("roblox_window_borderless", False))
        signature = (width, height, borderless)

        windows = get_roblox_windows()
        live = set(windows)
        for pid in list(self._applied):
            if pid not in live:
                self._applied.pop(pid, None)

        for pid, hwnd in windows.items():
            previous = self._applied.get(pid)
            if previous is not None and previous[0] == hwnd and previous[1:] == signature:
                continue
            if apply_to_window(hwnd, width, height, center, None, borderless):
                self._applied[pid] = (hwnd, width, height, borderless)
