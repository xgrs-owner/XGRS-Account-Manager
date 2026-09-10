"""
Resize, reposition and remember the geometry of Roblox client windows.
"""

from __future__ import annotations

from dataclasses import dataclass
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

# Roblox keeps adjusting its window for a while after it appears (it restores
# its own last geometry once the client has loaded), so whatever the manager
# applies is enforced for this long before the window is trusted.
SETTLE_SECONDS = 25.0
MAX_REAPPLIES = 15

# SetWindowPos normally lets the window clamp the size through
# WM_GETMINMAXINFO. Skipping WM_WINDOWPOSCHANGING bypasses that clamp, which
# is how sizes below the Roblox minimum are reached. Windows re-clamps the
# size when a minimized window is restored, so the resizer puts it back then.
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
                if area > largest.get(pid, (-1, 0))[0]:
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


def _placement_state(hwnd: int) -> str:
    """'minimized', 'maximized' or 'normal'."""
    try:
        placement = win32gui.GetWindowPlacement(hwnd)
    except Exception:
        return "normal"
    if not placement or len(placement) < 2:
        return "normal"
    show_state = placement[1]
    if show_state in (win32con.SW_SHOWMINIMIZED, win32con.SW_MINIMIZE):
        return "minimized"
    if show_state in (win32con.SW_SHOWMAXIMIZED, win32con.SW_MAXIMIZE):
        return "maximized"
    return "normal"


def _is_minimized_or_maximized(hwnd: int) -> bool:
    return _placement_state(hwnd) != "normal"


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
) -> dict[str, int] | None:
    """
    Give the window this size (and position). Returns the box the window
    actually ended up with, or None when Windows refused.
    """
    width, height = clamp_size(width, height, unlocked)
    try:
        if _placement_state(hwnd) != "normal":
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

        if position is not None:
            # Move first: landing on a monitor with a different DPI makes
            # Roblox rescale itself, so the size is applied afterwards, on
            # the destination monitor.
            win32gui.SetWindowPos(hwnd, 0, x, y, 0, 0, flags | win32con.SWP_NOSIZE)
        win32gui.SetWindowPos(hwnd, 0, x, y, width, height, flags)

        box = get_window_box(hwnd)
        if (
            unlocked
            and box is not None
            and (box["width"], box["height"]) != (width, height)
        ):
            win32gui.SetWindowPos(
                hwnd, 0, x, y, width, height,
                flags | SWP_NOSENDCHANGING | win32con.SWP_FRAMECHANGED,
            )
            box = get_window_box(hwnd)
        return box or {"x": x, "y": y, "width": width, "height": height}
    except Exception as exc:
        print(f"[Window Resizer] Could not resize window {hwnd}: {exc}")
        return None


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
        is not None
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


@dataclass
class _Options:
    resize: bool
    remember: bool
    width: int
    height: int
    center: bool
    borderless: bool
    unlocked: bool


@dataclass
class _TrackedWindow:
    hwnd: int
    account: str = ""
    intended: dict[str, int] | None = None   # the box the manager asked for
    achieved: dict[str, int] | None = None   # the box right after applying it
    settle_until: float = 0.0
    reapplies: int = 0
    was_minimized: bool = False
    layout_applied: bool = False             # a remembered layout was used


class RobloxWindowResizer:
    """
    Applies the configured size to Roblox windows as they appear, keeps it
    there while the client finishes loading, and remembers where each
    account's window was so a relaunched client comes back to the same place.
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
        self._tracked: dict[int, _TrackedWindow] = {}
        self._signature: tuple | None = None
        self._layouts = load_layouts()
        self._layouts_dirty = False
        self._last_save = 0.0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._tracked.clear()
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
        self._tracked.clear()
        print("[Window Resizer] Stopped.")

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def forget(self) -> None:
        """Treat every window as new on the next scan (settings changed)."""
        self._tracked.clear()
        self._signature = None

    def clear_layouts(self) -> None:
        self._layouts = {}
        self._layouts_dirty = False
        forget_layouts()
        for tracked in self._tracked.values():
            tracked.layout_applied = False

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

    def _read_options(self) -> _Options | None:
        settings = self._get_settings() or {}
        resize_enabled = bool(settings.get("roblox_window_resize_enabled", False))
        remember_enabled = bool(settings.get("roblox_window_remember_position", False))
        if not resize_enabled and not remember_enabled:
            return None
        unlocked = bool(settings.get("roblox_window_unlock_size", False))
        width, height = clamp_size(
            settings.get("roblox_window_width", DEFAULT_WIDTH),
            settings.get("roblox_window_height", DEFAULT_HEIGHT),
            unlocked,
        )
        # Centering and the frame belong to the resize feature; with only
        # "remember" on, windows stay where Roblox (or the layout) puts them.
        return _Options(
            resize=resize_enabled,
            remember=remember_enabled,
            width=width,
            height=height,
            center=resize_enabled and bool(settings.get("roblox_window_center", True)),
            borderless=resize_enabled and bool(settings.get("roblox_window_borderless", False)),
            unlocked=unlocked,
        )

    def _apply_once(self) -> None:
        options = self._read_options()
        if options is None:
            self._tracked.clear()
            return

        signature = (
            options.resize, options.remember, options.width, options.height,
            options.center, options.borderless, options.unlocked,
        )
        if signature != self._signature:
            self._signature = signature
            self._tracked.clear()

        windows = get_roblox_windows()
        for pid in list(self._tracked):
            if self._tracked[pid].hwnd != windows.get(pid):
                self._tracked.pop(pid, None)
        if not windows:
            return

        accounts = self._resolve_accounts() if options.remember else {}
        now = time.monotonic()

        for pid, hwnd in windows.items():
            account = accounts.get(pid, "")
            tracked = self._tracked.get(pid)
            if tracked is None:
                tracked = _TrackedWindow(hwnd=hwnd, account=account)
                self._tracked[pid] = tracked
                self._apply_target(pid, tracked, options, now, "new window")
                continue

            if account and account != tracked.account:
                tracked.account = account
                # The account is only known a few seconds after the window
                # appears; its remembered spot is applied as soon as it is.
                if (
                    options.remember
                    and account in self._layouts
                    and not tracked.layout_applied
                ):
                    self._apply_target(pid, tracked, options, now, "account known")
                    continue

            state = _placement_state(hwnd)
            if state == "minimized":
                tracked.was_minimized = True
                continue
            if state == "maximized":
                continue

            box = get_window_box(hwnd)
            if box is None:
                continue

            if tracked.was_minimized:
                tracked.was_minimized = False
                if tracked.intended and (
                    (box["width"], box["height"])
                    != (tracked.intended["width"], tracked.intended["height"])
                ):
                    self._reapply(pid, tracked, options, (box["x"], box["y"]), "restored")
                continue

            if now < tracked.settle_until:
                if (
                    tracked.intended
                    and tracked.achieved
                    and box != tracked.achieved
                    and tracked.reapplies < MAX_REAPPLIES
                ):
                    tracked.reapplies += 1
                    self._reapply(
                        pid, tracked, options,
                        (tracked.intended["x"], tracked.intended["y"]),
                        "startup drift",
                    )
                continue

            if options.remember and tracked.account:
                self._record(tracked.account, box)

    def _apply_target(
        self,
        pid: int,
        tracked: _TrackedWindow,
        options: _Options,
        now: float,
        reason: str,
    ) -> None:
        saved = (
            self._layouts.get(tracked.account)
            if options.remember and tracked.account else None
        )
        position = (saved["x"], saved["y"]) if saved else None
        if options.resize:
            width, height = options.width, options.height
        elif saved:
            width, height = saved["width"], saved["height"]
        else:
            # Only "remember" is on and nothing is stored yet: leave the
            # window alone, but let it settle before its spot is recorded.
            tracked.intended = None
            tracked.achieved = None
            tracked.settle_until = now + SETTLE_SECONDS
            return

        achieved = apply_to_window(
            tracked.hwnd, width, height,
            center=options.center and position is None,
            position=position,
            borderless=options.borderless,
            unlocked=options.unlocked,
        )
        if achieved is None:
            return

        x, y = position if position is not None else (achieved["x"], achieved["y"])
        tracked.intended = {"x": x, "y": y, "width": width, "height": height}
        tracked.achieved = achieved
        tracked.settle_until = now + SETTLE_SECONDS
        tracked.reapplies = 0
        tracked.was_minimized = False
        tracked.layout_applied = saved is not None
        label = f" for {tracked.account}" if tracked.account else ""
        source = " (remembered)" if saved else ""
        print(
            f"[Window Resizer] PID {pid}{label}: {reason}, "
            f"{width}x{height} at ({x}, {y}){source}."
        )
        if (achieved["width"], achieved["height"]) != (width, height):
            print(
                f"[Window Resizer] PID {pid}: the window kept "
                f"{achieved['width']}x{achieved['height']}"
                + ("." if options.unlocked else "; enable Unlock resize for sizes below the Roblox minimum.")
            )

    def _reapply(
        self,
        pid: int,
        tracked: _TrackedWindow,
        options: _Options,
        position: tuple[int, int],
        reason: str,
    ) -> None:
        intended = tracked.intended
        if not intended:
            return
        achieved = apply_to_window(
            tracked.hwnd, intended["width"], intended["height"],
            center=False,
            position=position,
            borderless=options.borderless,
            unlocked=options.unlocked,
        )
        if achieved is None:
            return
        tracked.achieved = achieved
        tracked.intended = {
            "x": position[0], "y": position[1],
            "width": intended["width"], "height": intended["height"],
        }
        if reason == "restored" or tracked.reapplies in (1, MAX_REAPPLIES):
            suffix = (
                f" ({tracked.reapplies}/{MAX_REAPPLIES})"
                if reason != "restored" else ""
            )
            print(
                f"[Window Resizer] PID {pid}: {reason}, put back "
                f"{intended['width']}x{intended['height']} at "
                f"({position[0]}, {position[1]}){suffix}."
            )

    def _record(self, account: str, box: dict[str, int]) -> None:
        if self._layouts.get(account) == box:
            return
        self._layouts[account] = dict(box)
        self._layouts_dirty = True
