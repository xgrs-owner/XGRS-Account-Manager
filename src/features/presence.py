"""
Account Activity Monitor process scanning.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
import re
import threading
import time
from typing import Callable

import psutil
import win32api
import win32gui
import win32process


# The manager generates a 16-digit browser tracker id for every launch. Roblox
# echoes it in the log ("suggestedBrowserTrackerId=...") and, when the
# bootstrapper passes the launch URI through, in the client's command line.
# Requiring the full digit run keeps "BrowserTrackerIdRequest: ... V2" from
# being read as tracker "2".
_TRACKER_PATTERN = re.compile(
    r"browsertrackerid[^0-9]{0,32}(\d{15,20})",
    re.IGNORECASE,
)
# "2026-09-10T00:16:54.403Z,0.403854,..." is wall-clock time plus seconds
# since the process started. Together they pin a log to its process far more
# precisely than the one-second file name timestamp.
_LOG_LINE_PATTERN = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?)Z,(\d+(?:\.\d+)?),",
    re.MULTILINE,
)
# Roblox keeps a background "tray" copy of RobloxPlayerBeta.exe that never
# joins a game and whose log never gets a user id. It is started with
# "--launch-to-tray" and only its user agent says "TrayMode";
# "[FLog::Systray]" lines appear in ordinary client logs as well.
_TRAY_MARKERS = ("AppState/TrayMode",)
_TRAY_CMDLINE_MARKERS = ("--launch-to-tray", "-launch-to-tray")
_LOG_HEAD_BYTES = 96_000

_PROCESS_CACHE_LOCK = threading.RLock()
_PROCESS_CACHE: dict[int, tuple[float, psutil.Process]] = {}
_PROCESS_CACHE_TIME = 0.0
_VALIDATION_CACHE: dict[tuple[int, float], bool] = {}
_PROCESS_CACHE_MAX_AGE = 0.4
_LOG_CACHE_LOCK = threading.RLock()
_LOG_CACHE: dict[str, tuple[int, int, RobloxLogEntry | None]] = {}


@dataclass(frozen=True)
class RobloxLogEntry:
    timestamp: datetime
    path: str
    user_id: str
    browser_tracker_id: str
    started_at: datetime | None = None  # process start derived from the log
    is_tray: bool = False


# Process identification
#
# Every Roblox client writes one "*_Player_*_last.log", and the account behind
# a client is only known from that log ("userid:"). So each process is paired
# with exactly one log, the strongest evidence first:
#   1. the browser tracker id in the client's command line matches the log
#   2. the client holds the log file open
#   3. the process start time recorded inside the log matches the process
# A log claimed by one process is never handed to another, and the tray
# process claims its own (user-less) log instead of stealing the next
# client's, which is what used to make Auto Connect launch duplicates.

_IDENTITY_LOCK = threading.RLock()
_IDENTITIES: dict[tuple[int, float], "_ProcessIdentity"] = {}
_CLAIMED_LOGS: dict[str, tuple[int, float]] = {}
_RESOLVE_CACHE: tuple[float, frozenset, dict[int, str]] = (0.0, frozenset(), {})
_RESOLVE_CACHE_MAX_AGE = 0.5
_LOG_EARLY_TOLERANCE_SEC = 1.5   # file names are truncated to whole seconds
_LOG_STARTUP_WINDOW_SEC = 120.0
_START_BEFORE_PROCESS_SEC = 0.5  # a log cannot be older than its process
_START_MATCH_TOLERANCE_SEC = 4.0
_PAIRING_GRACE_SEC = 15.0        # wait this long for direct evidence
_EVIDENCE_RETRY_SEC = 5.0
_EVIDENCE_MAX_TRIES = 6
_TRAY_REFRESH_SEC = 30.0         # how often a tray log is re-read


@dataclass
class _ProcessIdentity:
    pid: int
    create_time: float
    user_id: str = ""
    log_path: str = ""
    tracker: str = ""
    is_tray: bool = False
    evidence: str = ""
    cmdline_checked: bool = False
    evidence_tries: int = 0
    next_evidence_at: float = 0.0
    next_refresh_at: float = 0.0
    announced: bool = False

    @property
    def key(self) -> tuple[int, float]:
        return (self.pid, self.create_time)


def _utc_naive(create_time: float) -> datetime:
    return datetime.fromtimestamp(create_time, tz=timezone.utc).replace(tzinfo=None)


def _normalized_path(path: str) -> str:
    return os.path.normcase(os.path.abspath(str(path or "")))


def _inspect_cmdline(process: psutil.Process) -> tuple[str, bool]:
    """(browser tracker id, started as the tray process) from the command line."""
    try:
        command_line = " ".join(str(value) for value in process.cmdline())
    except (OSError, psutil.Error, ValueError):
        return "", False
    matches = set(_TRACKER_PATTERN.findall(command_line))
    tracker = next(iter(matches)) if len(matches) == 1 else ""
    lowered = command_line.lower()
    is_tray = any(marker in lowered for marker in _TRAY_CMDLINE_MARKERS)
    return tracker, is_tray


def _open_log_paths(process: psutil.Process) -> set[str]:
    paths: set[str] = set()
    try:
        for opened in process.open_files():
            path = str(getattr(opened, "path", "") or "")
            if path.lower().endswith("_last.log"):
                paths.add(_normalized_path(path))
    except (OSError, psutil.Error, ValueError):
        pass
    return paths


def _absorb_entry(identity: _ProcessIdentity, entry: RobloxLogEntry) -> None:
    if entry.browser_tracker_id and not identity.tracker:
        identity.tracker = entry.browser_tracker_id
    if entry.is_tray and not identity.is_tray:
        identity.is_tray = True
        if not identity.announced and not entry.user_id:
            identity.announced = True
            print(f"[Roblox] PID {identity.pid} is the Roblox tray process, not a client.")
    if entry.user_id and entry.user_id != identity.user_id:
        identity.user_id = entry.user_id
        identity.announced = True
        print(
            f"[Roblox] PID {identity.pid} runs user {entry.user_id} "
            f"({identity.evidence or 'log'})."
        )


def _claim_log(identity: _ProcessIdentity, entry: RobloxLogEntry, evidence: str) -> None:
    if identity.log_path and identity.log_path != entry.path:
        if _CLAIMED_LOGS.get(identity.log_path) == identity.key:
            _CLAIMED_LOGS.pop(identity.log_path, None)
    identity.log_path = entry.path
    identity.evidence = evidence
    _CLAIMED_LOGS[entry.path] = identity.key
    _absorb_entry(identity, entry)


def _prune_identities(live_keys: frozenset) -> None:
    for key in list(_IDENTITIES):
        if key in live_keys:
            continue
        identity = _IDENTITIES.pop(key)
        if identity.log_path and _CLAIMED_LOGS.get(identity.log_path) == key:
            _CLAIMED_LOGS.pop(identity.log_path, None)


def _resolve_pending(
    live: dict[tuple[int, float], psutil.Process],
    pending: list[tuple[int, float]],
) -> None:
    keys = sorted(pending, key=lambda key: (key[1], key[0]))
    for key in keys:
        if key not in _IDENTITIES:
            _IDENTITIES[key] = _ProcessIdentity(pid=key[0], create_time=key[1])

    earliest = _utc_naive(keys[0][1]) - timedelta(seconds=_LOG_EARLY_TOLERANCE_SEC)
    latest = _utc_naive(keys[-1][1]) + timedelta(seconds=_LOG_STARTUP_WINDOW_SEC)
    entries = get_roblox_log_entries(earliest, latest, include_unidentified=True)
    by_path = {entry.path: entry for entry in entries}
    by_norm_path = {_normalized_path(entry.path): entry for entry in entries}

    unpaired: list[_ProcessIdentity] = []
    for key in keys:
        identity = _IDENTITIES[key]
        if identity.log_path:
            entry = by_path.get(identity.log_path) or read_log_entry(identity.log_path)
            if entry is not None:
                _absorb_entry(identity, entry)
            if identity.is_tray and not identity.user_id:
                identity.next_refresh_at = time.monotonic() + _TRAY_REFRESH_SEC
            continue
        unpaired.append(identity)
    if not unpaired:
        return

    tracker_entries: dict[str, list[RobloxLogEntry]] = {}
    for entry in entries:
        if entry.browser_tracker_id:
            tracker_entries.setdefault(entry.browser_tracker_id, []).append(entry)

    now = time.monotonic()
    remaining: list[_ProcessIdentity] = []
    for identity in unpaired:
        process = live.get(identity.key)
        if process is None:
            continue
        if not identity.cmdline_checked:
            identity.cmdline_checked = True
            identity.tracker, started_as_tray = _inspect_cmdline(process)
            if started_as_tray and not identity.is_tray:
                identity.is_tray = True
                identity.announced = True
                print(f"[Roblox] PID {identity.pid} is the Roblox tray process, not a client.")
        if identity.tracker:
            matches = [
                entry for entry in tracker_entries.get(identity.tracker, [])
                if entry.path not in _CLAIMED_LOGS
            ]
            if len(matches) == 1:
                _claim_log(identity, matches[0], "browser tracker id")
                continue
        if (
            identity.evidence_tries < _EVIDENCE_MAX_TRIES
            and now >= identity.next_evidence_at
        ):
            identity.evidence_tries += 1
            identity.next_evidence_at = now + _EVIDENCE_RETRY_SEC
            matches = []
            for path in _open_log_paths(process):
                entry = by_norm_path.get(path) or read_log_entry(path)
                if entry is not None and entry.path not in _CLAIMED_LOGS:
                    matches.append(entry)
            if len(matches) == 1:
                _claim_log(identity, matches[0], "open log file")
                continue
        remaining.append(identity)
    if not remaining:
        return

    # Start-time pairing: every unclaimed log in the window is scored by how
    # soon after the process creation it was started. A log that predates
    # its process is impossible, so such candidates are dropped, which keeps
    # two clients started within the same second from swapping logs.
    scored: list[tuple] = []
    for identity in remaining:
        create_utc = _utc_naive(identity.create_time)
        for entry in entries:
            if entry.path in _CLAIMED_LOGS:
                continue
            file_diff = (entry.timestamp - create_utc).total_seconds()
            if file_diff < -_LOG_EARLY_TOLERANCE_SEC or file_diff > _LOG_STARTUP_WINDOW_SEC:
                continue
            if entry.started_at is not None:
                delta = (entry.started_at - create_utc).total_seconds()
                if delta < -_START_BEFORE_PROCESS_SEC:
                    continue
                score = delta if delta >= 0 else -delta + 1.0
            else:
                score = max(0.0, file_diff) + 1.0
            scored.append((
                score, identity.create_time, identity.pid,
                entry.timestamp, entry.path, identity, entry,
            ))
    scored.sort(key=lambda item: item[:5])

    wall_now = time.time()
    paired: set[tuple[int, float]] = set()
    for score, _, _, _, _, identity, entry in scored:
        if identity.key in paired or entry.path in _CLAIMED_LOGS:
            continue
        confident = score <= _START_MATCH_TOLERANCE_SEC
        if not confident and wall_now - identity.create_time < _PAIRING_GRACE_SEC:
            continue  # give direct evidence, or a better log, a chance first
        _claim_log(identity, entry, "log start time" if confident else "log timestamp")
        paired.add(identity.key)


def resolve_pid_user_ids(
    processes: dict[int, tuple[float, psutil.Process]] | None = None,
    max_age: float = _RESOLVE_CACHE_MAX_AGE,
) -> dict[int, str]:
    """
    User id for every running Roblox client, keyed by pid. The value is an
    empty string while a client has not written its user id yet, and for the
    Roblox tray process, which never does.
    """
    global _RESOLVE_CACHE
    if processes is None:
        processes = get_roblox_processes()
    live = {(pid, data[0]): data[1] for pid, data in processes.items()}
    live_keys = frozenset(live)

    with _IDENTITY_LOCK:
        cached_at, cached_keys, cached_map = _RESOLVE_CACHE
        if cached_keys == live_keys and time.monotonic() - cached_at <= max(0.0, max_age):
            return dict(cached_map)

        _prune_identities(live_keys)
        now = time.monotonic()
        pending = []
        for key in live_keys:
            identity = _IDENTITIES.get(key)
            if identity is None or (
                not identity.user_id and now >= identity.next_refresh_at
            ):
                pending.append(key)
        if pending:
            try:
                _resolve_pending(live, pending)
            except Exception as exc:
                print(
                    f"[WARNING] Roblox process identification failed: "
                    f"{type(exc).__name__}: {exc}"
                )

        result: dict[int, str] = {}
        for key in live_keys:
            identity = _IDENTITIES.get(key)
            result[key[0]] = identity.user_id if identity else ""
        _RESOLVE_CACHE = (time.monotonic(), live_keys, dict(result))
        return result


def get_process_identity(pid: int) -> dict | None:
    """What is known about one Roblox process (after a resolve pass)."""
    with _IDENTITY_LOCK:
        for key, identity in _IDENTITIES.items():
            if key[0] == pid:
                return {
                    "pid": identity.pid,
                    "create_time": identity.create_time,
                    "user_id": identity.user_id,
                    "is_tray": identity.is_tray,
                    "log_path": identity.log_path,
                    "evidence": identity.evidence,
                }
    return None


def is_tray_process(pid: int) -> bool:
    identity = get_process_identity(pid)
    return bool(identity and identity["is_tray"] and not identity["user_id"])


def _get_user_id_from_pid(
    pid: int,
    used_logs: set[str] | None = None,
) -> str | None:
    """
    User id of the account running in this Roblox client, or None while the
    client has not written it yet. `used_logs` is accepted for compatibility;
    the shared resolver claims logs itself.
    """
    try:
        return resolve_pid_user_ids().get(int(pid)) or None
    except Exception:
        return None


def get_roblox_logs_dir() -> str:
    return os.path.join(os.getenv("LOCALAPPDATA", ""), "Roblox", "logs")


def _log_time_from_name(filename: str) -> datetime | None:
    match = re.search(r"(\d{8}T\d{6}Z)", filename)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ")
    except ValueError:
        return None


def _parse_log_content(
    content: str,
    log_time: datetime,
    log_path: str,
) -> RobloxLogEntry:
    content_lower = content.lower()
    user_id = ""
    if "userid:" in content_lower:
        candidate = content_lower.split("userid:", 1)[1].split(",", 1)[0].strip()
        if candidate.isdigit():
            user_id = candidate

    tracker_match = _TRACKER_PATTERN.search(content)
    browser_tracker_id = tracker_match.group(1) if tracker_match else ""

    started_at: datetime | None = None
    line_match = _LOG_LINE_PATTERN.search(content)
    if line_match:
        try:
            stamp = line_match.group(1)
            line_format = "%Y-%m-%dT%H:%M:%S.%f" if "." in stamp else "%Y-%m-%dT%H:%M:%S"
            line_time = datetime.strptime(stamp, line_format)
            started_at = line_time - timedelta(seconds=float(line_match.group(2)))
        except (ValueError, OverflowError):
            started_at = None

    return RobloxLogEntry(
        timestamp=log_time,
        path=log_path,
        user_id=user_id,
        browser_tracker_id=browser_tracker_id,
        started_at=started_at,
        is_tray=any(marker in content for marker in _TRAY_MARKERS),
    )


def read_log_entry(log_path: str) -> RobloxLogEntry | None:
    """Parse one Roblox log whatever its timestamp, through the cache."""
    log_time = _log_time_from_name(os.path.basename(log_path))
    if log_time is None:
        return None
    try:
        stat_result = os.stat(log_path)
    except OSError:
        return None
    cache_key = (stat_result.st_mtime_ns, stat_result.st_size)
    with _LOG_CACHE_LOCK:
        cached = _LOG_CACHE.get(log_path)
    if cached is not None and cached[:2] == cache_key:
        return cached[2]
    try:
        with open(log_path, "r", encoding="utf-8", errors="ignore") as handle:
            content = handle.read(_LOG_HEAD_BYTES)
    except (OSError, UnicodeError, ValueError):
        return None
    entry = _parse_log_content(content, log_time, log_path)
    with _LOG_CACHE_LOCK:
        _LOG_CACHE[log_path] = (*cache_key, entry)
    return entry


def get_roblox_log_entries(
    earliest_time: datetime | None = None,
    latest_time: datetime | None = None,
    include_unidentified: bool = False,
) -> list[RobloxLogEntry]:
    """
    Player logs whose file timestamp lies inside the window. Logs without a
    user id (a client that is still starting, or the tray process) are only
    returned when `include_unidentified` is set.
    """
    logs_dir = get_roblox_logs_dir()
    if not os.path.isdir(logs_dir):
        return []

    entries: list[RobloxLogEntry] = []
    try:
        filenames = os.listdir(logs_dir)
    except OSError:
        return entries

    current_paths: set[str] = set()
    for filename in filenames:
        if not filename.endswith("_last.log") or "_Studio_" in filename:
            continue
        log_time = _log_time_from_name(filename)
        if log_time is None:
            continue
        log_path = os.path.join(logs_dir, filename)
        current_paths.add(log_path)
        try:
            if earliest_time is not None and log_time < earliest_time:
                continue
            if latest_time is not None and log_time > latest_time:
                continue
            stat_result = os.stat(log_path)
            cache_key = (stat_result.st_mtime_ns, stat_result.st_size)
            with _LOG_CACHE_LOCK:
                cached = _LOG_CACHE.get(log_path)
            if cached is not None and cached[:2] == cache_key:
                entry = cached[2]
            else:
                with open(
                    log_path,
                    "r",
                    encoding="utf-8",
                    errors="ignore",
                ) as handle:
                    content = handle.read(_LOG_HEAD_BYTES)
                entry = _parse_log_content(content, log_time, log_path)
                with _LOG_CACHE_LOCK:
                    _LOG_CACHE[log_path] = (*cache_key, entry)
            if entry is not None and (entry.user_id or include_unidentified):
                entries.append(entry)
        except (OSError, UnicodeError, ValueError):
            continue

    with _LOG_CACHE_LOCK:
        stale_paths = set(_LOG_CACHE) - current_paths
        for path in stale_paths:
            _LOG_CACHE.pop(path, None)

    entries.sort(key=lambda entry: (entry.timestamp, entry.path))
    return entries


def _get_exe_description(pid: int) -> str:
    try:
        process = psutil.Process(pid)
        executable = process.exe()
        translations = win32api.GetFileVersionInfo(
            executable,
            r"\VarFileInfo\Translation",
        )
        if not translations:
            return ""
        lang, codepage = translations[0]
        key = (
            f"\\StringFileInfo\\{lang:04X}{codepage:04X}"
            "\\FileDescription"
        )
        return win32api.GetFileVersionInfo(executable, key) or ""
    except Exception:
        return ""


def is_valid_roblox_game_client(
    pid: int,
    process_name_lower: str | None = None,
) -> bool:
    try:
        if process_name_lower is None:
            try:
                process_name_lower = psutil.Process(pid).name().lower()
            except Exception:
                return False

        if process_name_lower != "robloxplayerbeta.exe":
            return False

        try:
            create_time = float(psutil.Process(pid).create_time())
        except Exception:
            return False
        cache_key = (pid, create_time)
        with _PROCESS_CACHE_LOCK:
            cached = _VALIDATION_CACHE.get(cache_key)
        if cached is not None:
            return cached

        description = _get_exe_description(pid)
        if description:
            valid = "roblox" in description.lower()
        else:
            valid = True
        with _PROCESS_CACHE_LOCK:
            _VALIDATION_CACHE[cache_key] = valid
        return valid
    except Exception:
        return process_name_lower == "robloxplayerbeta.exe" if process_name_lower else False

def get_roblox_processes(
    force: bool = False,
    max_age: float = _PROCESS_CACHE_MAX_AGE,
) -> dict[int, tuple[float, psutil.Process]]:
    global _PROCESS_CACHE
    global _PROCESS_CACHE_TIME
    now = time.monotonic()
    with _PROCESS_CACHE_LOCK:
        if (
            not force
            and _PROCESS_CACHE_TIME
            and now - _PROCESS_CACHE_TIME <= max(0.0, max_age)
        ):
            return dict(_PROCESS_CACHE)

    processes: dict[int, tuple[float, psutil.Process]] = {}
    try:
        process_iter = psutil.process_iter(
            ["pid", "name", "create_time"],
        )
        for process in process_iter:
            try:
                process_name = (process.info.get("name") or "").lower()
                if process_name != "robloxplayerbeta.exe":
                    continue

                pid = int(process.info["pid"])
                if not is_valid_roblox_game_client(pid, process_name):
                    continue

                create_time = process.info.get("create_time")
                if create_time is None:
                    create_time = process.create_time()
                processes[pid] = (float(create_time), process)
            except (
                OSError,
                psutil.NoSuchProcess,
                psutil.AccessDenied,
                psutil.ZombieProcess,
            ):
                continue
    except (
        OSError,
        psutil.NoSuchProcess,
        psutil.AccessDenied,
        psutil.ZombieProcess,
    ):
        pass
    live_keys = {(pid, value[0]) for pid, value in processes.items()}
    with _PROCESS_CACHE_LOCK:
        _VALIDATION_CACHE.clear()
        for key in live_keys:
            _VALIDATION_CACHE[key] = True
        _PROCESS_CACHE = dict(processes)
        _PROCESS_CACHE_TIME = time.monotonic()
        return dict(_PROCESS_CACHE)


def get_windows_by_pid(
    pids: set[int] | None = None,
) -> dict[int, list[int]]:
    target_pids = set(pids) if pids is not None else set(get_roblox_processes())
    windows: dict[int, list[int]] = {pid: [] for pid in target_pids}
    if not target_pids:
        return windows

    def _callback(hwnd, _):
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid in target_pids:
                windows[pid].append(hwnd)
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(_callback, None)
    except Exception:
        pass
    return windows


def _get_roblox_processes() -> dict[int, tuple[float, psutil.Process]]:
    return get_roblox_processes()


def _get_roblox_pids() -> set[int]:
    return set(_get_roblox_processes())


def _build_uid_map(manager) -> dict[str, str]:
    result: dict[str, str] = {}
    accounts_lock = getattr(manager, "_accounts_lock", None)
    if accounts_lock is not None:
        with accounts_lock:
            accounts = list(manager.accounts.items())
    else:
        accounts = list(manager.accounts.items())

    for username, data in accounts:
        if not isinstance(data, dict):
            continue
        user_id = str(data.get("user_id", "") or "")
        if user_id and user_id != "0":
            result[user_id] = username
    return result


class PresenceScanner:
    def __init__(
        self,
        manager,
        on_update: Callable[[dict[str, dict]], None],
        interval_sec: int = 5,
    ):
        self._manager = manager
        self._on_update = on_update
        self._interval = max(5, int(interval_sec))
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None
        self._pid_uid_cache: dict[int, tuple[float, str]] = {}
        self._process_cache: dict[int, tuple[float, psutil.Process]] = {}
        self._logical_cpu_count = max(1, psutil.cpu_count() or 1)
        self.latest_snapshot: dict[str, dict] = {}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="AccountActivityMonitor",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        thread = self._thread
        self._thread = None
        if thread and thread.is_alive():
            thread.join(timeout=1.5)
        self._pid_uid_cache.clear()
        self._process_cache.clear()
        self.latest_snapshot = {}

    def _run(self) -> None:
        while not self._stop_evt.is_set():
            self._do_scan()
            if self._stop_evt.wait(self._interval):
                break

    def _resolve_pid_user_id(
        self,
        pid: int,
        create_time: float,
        used_logs: set[str],
    ) -> str | None:
        cached = self._pid_uid_cache.get(pid)
        if cached is not None and cached[0] == create_time:
            return cached[1]

        user_id = _get_user_id_from_pid(pid, used_logs)
        if user_id:
            self._pid_uid_cache[pid] = (create_time, user_id)
        return user_id

    def _do_scan(self) -> None:
        try:
            discovered_processes = get_roblox_processes()
            current_pids = set(discovered_processes)
            self._pid_uid_cache = {
                pid: value
                for pid, value in self._pid_uid_cache.items()
                if pid in current_pids
                and value[0] == discovered_processes[pid][0]
            }
            self._process_cache = {
                pid: value
                for pid, value in self._process_cache.items()
                if pid in current_pids
                and value[0] == discovered_processes[pid][0]
            }

            current_processes: dict[int, tuple[float, psutil.Process]] = {}
            for pid, (create_time, discovered_process) in discovered_processes.items():
                cached = self._process_cache.get(pid)
                if cached is None:
                    cached = (create_time, discovered_process)
                    self._process_cache[pid] = cached
                current_processes[pid] = cached

            uid_map = _build_uid_map(self._manager)
            if not uid_map:
                self.latest_snapshot = {}
                self._on_update({})
                return

            used_logs: set[str] = set()
            matched_processes: list[tuple[str, psutil.Process]] = []
            for pid in sorted(current_processes):
                create_time, process = current_processes[pid]
                user_id = self._resolve_pid_user_id(
                    pid,
                    create_time,
                    used_logs,
                )
                username = uid_map.get(user_id or "")
                if username:
                    matched_processes.append((username, process))

            aggregates: dict[str, dict] = {}
            for username, process in matched_processes:
                entry = aggregates.setdefault(username, {
                    "pids": [],
                    "ram_mb": 0.0,
                    "cpu_percent": 0.0,
                    "ram_available": False,
                    "cpu_available": False,
                })
                try:
                    entry["pids"].append(process.pid)
                    entry["ram_mb"] += process.memory_info().rss / 1024 / 1024
                    entry["ram_available"] = True
                except (
                    OSError,
                    psutil.NoSuchProcess,
                    psutil.AccessDenied,
                    psutil.ZombieProcess,
                ):
                    pass
                try:
                    entry["cpu_percent"] += max(
                        0.0,
                        float(process.cpu_percent(interval=None)),
                    )
                    entry["cpu_available"] = True
                except (
                    OSError,
                    psutil.NoSuchProcess,
                    psutil.AccessDenied,
                    psutil.ZombieProcess,
                ):
                    pass

            snapshot: dict[str, dict] = {}
            for username, entry in aggregates.items():
                entry["cpu_percent"] = min(
                    100.0,
                    entry["cpu_percent"] / self._logical_cpu_count,
                )
                snapshot[username] = entry

            self.latest_snapshot = snapshot
            try:
                self._on_update(snapshot)
            except Exception:
                pass
        except Exception as exc:
            print(
                f"[ERROR] Account Activity Monitor scan failed: "
                f"{type(exc).__name__}: {exc}"
            )
