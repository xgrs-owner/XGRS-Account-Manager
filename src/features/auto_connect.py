"""
features/auto_connect.py
Auto Connect keeps one Roblox client alive per account.

It watches every configured account for:
  - whether its Roblox client is open (process matched through the Roblox logs)
  - RAM / CPU usage, network ping and in-game presence
  - Roblox error prompts (error code 264/266/267/268/270/277/279/280/403/524/600,
    "Failed to Load Library", ...)

and relaunches the client of that exact account whenever it closes, crashes or
gets disconnected.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import socket
import struct
import subprocess
import threading
import time
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
from typing import Callable

import psutil
import requests

import features.account_actions as actions
import features.presence as presence_mod
from classes.roblox_api import RobloxAPI
from utils.app_paths import get_data_dir

# Account states reported to the UI
STATE_STOPPED = "stopped"      # monitoring is off for this account
STATE_LAUNCHING = "launching"  # launch requested, client not visible yet
STATE_RUNNING = "running"      # client process alive, not confirmed in a game
STATE_IN_GAME = "in_game"      # client alive and presence says it is in a game
STATE_CLOSED = "closed"        # no client process for this account
STATE_WAITING = "waiting"      # waiting before the next relaunch attempt
STATE_ERROR = "error"          # Roblox reported an error, restart in progress

_CONFIG_FILE = os.path.join(get_data_dir(), "auto_connect.json")
_CONFIG_LOCK = threading.RLock()
_CONFIG_CACHE: dict | None = None

DEFAULT_CONFIG: dict = {
    "place_id": "",
    "private_server": "",
    "job_id": "",
    "check_interval": 5,
    "relaunch_delay": 10,
    "launch_grace": 60,
    "max_retries": 0,
    "check_internet": True,
    "restart_on_error": True,
    "restart_when_stuck": True,
    "stuck_timeout": 180,
    "check_presence": True,
    "measure_ping": True,
    "auto_start": False,
}

# Roblox error codes that mean "this client can no longer play, restart it"
ERROR_CODES = (264, 266, 267, 268, 270, 277, 279, 280, 403, 524, 600)

_ERROR_CODE_PATTERNS = (
    re.compile(r"error\s*code[^0-9]{0,12}(\d{3})", re.IGNORECASE),
    re.compile(r"errorcode[\"'\s:=]{1,8}(\d{3})", re.IGNORECASE),
    re.compile(r"\berror\s*[:=#]\s*(\d{3})\b", re.IGNORECASE),
)
_ERROR_ID_17_PATTERN = re.compile(r"\bid\s*[:=]?\s*17\b", re.IGNORECASE)
_ERROR_TEXT_PATTERNS = (
    (re.compile(r"failed to load library", re.IGNORECASE), "Failed to Load Library"),
    (re.compile(r"an unexpected error occurred and roblox needs to quit", re.IGNORECASE),
     "Roblox needs to quit"),
    (re.compile(r"you have been kicked", re.IGNORECASE), "Kicked from game"),
    (re.compile(r"lost connection to the game server", re.IGNORECASE), "Lost connection"),
)

_LOG_TAIL_LIMIT = 512 * 1024  # never read more than this per scan


def load_configs() -> dict:
    """Read the persisted Auto Connect configuration (one entry per account)."""
    global _CONFIG_CACHE
    with _CONFIG_LOCK:
        if _CONFIG_CACHE is None:
            data: dict = {}
            if os.path.exists(_CONFIG_FILE):
                try:
                    with open(_CONFIG_FILE, "r", encoding="utf-8") as handle:
                        loaded = json.load(handle)
                    if isinstance(loaded, dict):
                        data = {
                            str(account): normalize_config(config)
                            for account, config in loaded.items()
                            if isinstance(config, dict)
                        }
                except (OSError, ValueError, TypeError):
                    pass
            _CONFIG_CACHE = data
        return {account: dict(config) for account, config in _CONFIG_CACHE.items()}


def save_configs(configs: dict) -> None:
    """Persist the Auto Connect configuration atomically."""
    global _CONFIG_CACHE
    with _CONFIG_LOCK:
        if _CONFIG_CACHE == configs:
            return
    os.makedirs(get_data_dir(), exist_ok=True)
    temp_file = _CONFIG_FILE + ".tmp"
    try:
        with open(temp_file, "w", encoding="utf-8") as handle:
            json.dump(configs, handle, indent=2)
        os.replace(temp_file, _CONFIG_FILE)
        with _CONFIG_LOCK:
            _CONFIG_CACHE = {
                account: dict(config) for account, config in configs.items()
            }
    except OSError as exc:
        print(f"[WARNING] Auto Connect config save failed: {exc}")
        if os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except OSError:
                pass


def normalize_config(config: dict) -> dict:
    """Fill missing keys with defaults so older config files keep working."""
    merged = dict(DEFAULT_CONFIG)
    merged.update({
        key: value for key, value in config.items() if key in DEFAULT_CONFIG
    })
    merged["place_id"] = str(merged.get("place_id", "") or "").strip()
    merged["private_server"] = str(merged.get("private_server", "") or "").strip()
    merged["job_id"] = str(merged.get("job_id", "") or "").strip()
    merged["check_interval"] = max(3, int(merged.get("check_interval", 5) or 5))
    merged["relaunch_delay"] = max(0, int(merged.get("relaunch_delay", 10) or 0))
    merged["launch_grace"] = max(20, int(merged.get("launch_grace", 60) or 60))
    merged["max_retries"] = max(0, int(merged.get("max_retries", 0) or 0))
    merged["stuck_timeout"] = max(30, int(merged.get("stuck_timeout", 180) or 180))
    return merged


def has_internet(timeout: int = 3) -> bool:
    for url in ("https://www.google.com/generate_204",
                "https://www.cloudflare.com/cdn-cgi/trace"):
        try:
            if requests.get(url, timeout=timeout).status_code < 500:
                return True
        except requests.RequestException:
            pass
    return False


# Roblox log inspection

def find_log_path_for_pid(pid: int) -> str | None:
    """Return the Roblox log file written by this client process."""
    try:
        create_utc = datetime.fromtimestamp(
            psutil.Process(pid).create_time(),
            tz=timezone.utc,
        ).replace(tzinfo=None)
    except (OSError, psutil.Error):
        return None

    entries = presence_mod.get_roblox_log_entries(
        earliest_time=create_utc,
        latest_time=create_utc + timedelta(seconds=60),
    )
    best_path: str | None = None
    best_diff: float | None = None
    for entry in entries:
        diff = (entry.timestamp - create_utc).total_seconds()
        if 0 <= diff <= 60 and (best_diff is None or diff < best_diff):
            best_path, best_diff = entry.path, diff
    return best_path


def scan_log_for_error(path: str, offset: int) -> tuple[str | None, int]:
    """
    Read the log file from `offset` and look for a Roblox error prompt.
    Returns (error label or None, new offset).
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return None, offset

    if size < offset:  # the file was rotated
        offset = 0
    if size == offset:
        return None, offset

    start = max(offset, size - _LOG_TAIL_LIMIT)
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            handle.seek(start)
            chunk = handle.read()
    except OSError:
        return None, offset

    return detect_error(chunk), size


def detect_error(text: str) -> str | None:
    """Return a short label for the first known Roblox error found in `text`."""
    if not text:
        return None

    for pattern in _ERROR_CODE_PATTERNS:
        for match in pattern.finditer(text):
            try:
                code = int(match.group(1))
            except (TypeError, ValueError):
                continue
            if code in ERROR_CODES:
                if code == 279 and _ERROR_ID_17_PATTERN.search(text):
                    return "Error Code 279 (ID 17)"
                return f"Error Code {code}"

    for pattern, label in _ERROR_TEXT_PATTERNS:
        if pattern.search(text):
            return label
    return None


# Ping measurement

_IPHLPAPI = None
_IPHLPAPI_READY = False
_ICMP_INVALID_HANDLE = ctypes.c_void_p(-1).value


class _IpOptionInformation(ctypes.Structure):
    _fields_ = [
        ("Ttl", ctypes.c_ubyte),
        ("Tos", ctypes.c_ubyte),
        ("Flags", ctypes.c_ubyte),
        ("OptionsSize", ctypes.c_ubyte),
        ("OptionsData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


class _IcmpEchoReply(ctypes.Structure):
    _fields_ = [
        ("Address", ctypes.c_uint32),
        ("Status", ctypes.c_ulong),
        ("RoundTripTime", ctypes.c_ulong),
        ("DataSize", ctypes.c_ushort),
        ("Reserved", ctypes.c_ushort),
        ("Data", ctypes.c_void_p),
        ("Options", _IpOptionInformation),
    ]


def _load_iphlpapi():
    """Bind IcmpSendEcho once. It works without administrator rights."""
    global _IPHLPAPI, _IPHLPAPI_READY
    if _IPHLPAPI_READY:
        return _IPHLPAPI
    _IPHLPAPI_READY = True
    try:
        library = ctypes.WinDLL("iphlpapi.dll")
        library.IcmpCreateFile.restype = wintypes.HANDLE
        library.IcmpCloseHandle.argtypes = [wintypes.HANDLE]
        library.IcmpCloseHandle.restype = wintypes.BOOL
        library.IcmpSendEcho.argtypes = [
            wintypes.HANDLE, ctypes.c_uint32, ctypes.c_void_p,
            ctypes.c_ushort, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_ulong, ctypes.c_ulong,
        ]
        library.IcmpSendEcho.restype = ctypes.c_ulong
        _IPHLPAPI = library
    except (OSError, AttributeError):
        _IPHLPAPI = None
    return _IPHLPAPI


def icmp_ping_ms(ip: str, timeout_ms: int = 1000) -> float | None:
    library = _load_iphlpapi()
    if library is None:
        return None
    handle = library.IcmpCreateFile()
    if not handle or handle == _ICMP_INVALID_HANDLE:
        return None
    try:
        destination = struct.unpack("<I", socket.inet_aton(ip))[0]
        payload = b"ram-auto-connect"
        reply_size = ctypes.sizeof(_IcmpEchoReply) + len(payload) + 8
        buffer = ctypes.create_string_buffer(reply_size)
        replies = library.IcmpSendEcho(
            handle, destination, payload, len(payload),
            None, buffer, reply_size, timeout_ms,
        )
        if not replies:
            return None
        reply = ctypes.cast(buffer, ctypes.POINTER(_IcmpEchoReply)).contents
        if reply.Status != 0:
            return None
        return float(reply.RoundTripTime)
    except (OSError, struct.error, ValueError):
        return None
    finally:
        try:
            library.IcmpCloseHandle(handle)
        except OSError:
            pass


def tcp_ping_ms(
    host: str = "www.roblox.com",
    port: int = 443,
    timeout: float = 2.5,
) -> float | None:
    started = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except OSError:
        return None
    return (time.perf_counter() - started) * 1000.0


def get_game_server_ip(pid: int) -> str | None:
    """Find the game server this client is talking to (Roblox uses UDP)."""
    try:
        process = psutil.Process(pid)
        lister = getattr(process, "net_connections", None) or process.connections
        connections = list(lister(kind="inet"))
    except (psutil.Error, OSError, AttributeError, ValueError):
        return None

    for connection in connections:
        remote = connection.raddr
        if not remote:
            continue
        ip = getattr(remote, "ip", "")
        if not ip or ":" in ip:  # skip IPv6, IcmpSendEcho is IPv4 only
            continue
        try:
            address = socket.inet_aton(ip)
        except OSError:
            continue
        if address[0] in (10, 127) or ip.startswith("192.168.") or ip.startswith("169.254."):
            continue
        if address[0] == 172 and 16 <= address[1] <= 31:
            continue
        return ip
    return None


def measure_ping_ms(pid: int | None) -> tuple[float | None, str]:
    """
    Ping the game server the client is connected to, falling back to Roblox's
    web front-end. Returns (milliseconds, source label).
    """
    if pid:
        server_ip = get_game_server_ip(pid)
        if server_ip:
            latency = icmp_ping_ms(server_ip)
            if latency is not None:
                return latency, f"game server {server_ip}"
    latency = tcp_ping_ms()
    if latency is not None:
        return latency, "roblox.com"
    return None, ""


# Presence

def fetch_presence_batch(user_ids: list[str], cookie: str) -> dict[str, dict]:
    """One presence request covering every monitored account."""
    wanted = [uid for uid in dict.fromkeys(user_ids) if uid and uid != "0"]
    if not wanted or not cookie:
        return {}

    csrf_token = RobloxAPI.get_csrf_token(cookie)
    if not csrf_token:
        return {}

    try:
        response = requests.post(
            "https://presence.roblox.com/v1/presence/users",
            headers={
                "Cookie": f".ROBLOSECURITY={cookie}",
                "Content-Type": "application/json",
                "X-CSRF-TOKEN": csrf_token,
            },
            json={"userIds": [int(uid) for uid in wanted]},
            timeout=8,
        )
    except (requests.RequestException, ValueError):
        return {}

    if response.status_code != 200:
        return {}

    try:
        payload = response.json()
    except ValueError:
        return {}

    result: dict[str, dict] = {}
    for presence in payload.get("userPresences", []) or []:
        user_id = str(presence.get("userId", "") or "")
        if not user_id:
            continue
        result[user_id] = {
            "in_game": presence.get("userPresenceType") == 2,
            "place_id": presence.get("placeId"),
            "game_id": presence.get("gameId", ""),
            "last_location": presence.get("lastLocation", ""),
        }
    return result


def kill_pid(pid: int) -> None:
    try:
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        pass


ROBLOX_PROCESS_NAMES = {
    "robloxplayerbeta.exe",
    "robloxplayerlauncher.exe",
    "robloxcrashhandler.exe",
    "robloxstudiobeta.exe",
}

BOOTSTRAPPER_NAMES = {
    "bloxstrap.exe",
    "fishstrap.exe",
    "roblox.exe",
    "robloxplayerinstaller.exe",
}


def get_configured_launcher_names() -> set[str]:
    names: set[str] = set()
    try:
        settings = actions.load_ui_settings()
    except Exception:
        return names
    path = str(settings.get("custom_roblox_launcher_path", "") or "").strip()
    if path:
        names.add(os.path.basename(path).lower())
    return names


def close_all_roblox(include_bootstrapper: bool = True) -> int:
    targets = set(ROBLOX_PROCESS_NAMES)
    if include_bootstrapper:
        targets |= BOOTSTRAPPER_NAMES | get_configured_launcher_names()

    victims: list[int] = []
    for process in psutil.process_iter(["pid", "name"]):
        try:
            name = (process.info.get("name") or "").lower()
        except (psutil.Error, OSError):
            continue
        if name in targets:
            victims.append(int(process.info["pid"]))

    for pid in victims:
        kill_pid(pid)

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        still_running = []
        for pid in victims:
            try:
                if psutil.Process(pid).is_running():
                    still_running.append(pid)
            except (psutil.Error, OSError):
                continue
        if not still_running:
            break
        time.sleep(0.25)
        for pid in still_running:
            try:
                psutil.Process(pid).kill()
            except (psutil.Error, OSError):
                pass

    presence_mod.get_roblox_processes(force=True)
    return len(victims)


def list_roblox_processes(manager=None) -> list[dict]:
    targets = ROBLOX_PROCESS_NAMES | BOOTSTRAPPER_NAMES | get_configured_launcher_names()
    uid_to_name: dict[str, str] = {}
    if manager is not None:
        for username, data in list(getattr(manager, "accounts", {}).items()):
            if isinstance(data, dict):
                user_id = str(data.get("user_id", "") or "")
                if user_id and user_id != "0":
                    uid_to_name[user_id] = username

    used_logs: set[str] = set()
    now = time.time()
    rows: list[dict] = []
    for process in psutil.process_iter(["pid", "name", "create_time"]):
        try:
            raw_name = process.info.get("name") or ""
        except (psutil.Error, OSError):
            continue
        if raw_name.lower() not in targets:
            continue

        pid = int(process.info["pid"])
        created = float(process.info.get("create_time") or now)
        try:
            ram_mb = process.memory_info().rss / 1024 / 1024
        except (psutil.Error, OSError):
            ram_mb = 0.0

        is_client = raw_name.lower() == "robloxplayerbeta.exe"
        account = ""
        if is_client and uid_to_name:
            user_id = presence_mod._get_user_id_from_pid(pid, used_logs)
            account = uid_to_name.get(str(user_id or ""), "")

        rows.append({
            "pid": pid,
            "name": raw_name,
            "account": account,
            "ram_mb": ram_mb,
            "uptime_seconds": max(0.0, now - created),
            "is_client": is_client,
        })

    rows.sort(key=lambda row: (not row["is_client"], row["pid"]))
    return rows


class _AccountState:
    """Everything Auto Connect knows about one monitored account."""

    def __init__(self, account: str, config: dict):
        self.account = account
        self.config = normalize_config(config)
        self.user_id = ""
        self.enabled = False
        self.state = STATE_STOPPED
        self.state_since = time.time()
        self.pid: int | None = None
        self.in_game = False
        self.ram_mb = 0.0
        self.cpu_percent = 0.0
        self.ping_ms: float | None = None
        self.ping_source = ""
        self.last_error = ""
        self.restarts = 0
        self.attempts = 0
        self.launching = False
        self.launch_started = 0.0
        self.launch_token = 0
        self.log_path: str | None = None
        self.log_offset = 0
        self.log_pid: int | None = None
        self.running_since = 0.0

    def snapshot(self, now: float) -> dict:
        elapsed = max(0.0, now - self.state_since)
        is_closed = self.state in (STATE_CLOSED, STATE_WAITING, STATE_ERROR)
        return {
            "account": self.account,
            "state": self.state,
            "state_seconds": elapsed,
            "closed_seconds": elapsed if is_closed else 0.0,
            "uptime_seconds": max(0.0, now - self.running_since) if self.running_since else 0.0,
            "enabled": self.enabled,
            "pid": self.pid,
            "is_running": self.pid is not None,
            "in_game": self.in_game,
            "ram_mb": self.ram_mb,
            "cpu_percent": self.cpu_percent,
            "ping_ms": self.ping_ms,
            "ping_source": self.ping_source,
            "last_error": self.last_error,
            "restarts": self.restarts,
            "place_id": self.config.get("place_id", ""),
            "private_server": self.config.get("private_server", ""),
        }


class AutoConnectSupervisor:
    """
    One background thread scans every Roblox client, matches it to an account
    and relaunches the accounts whose client is gone or broken.
    """

    def __init__(
        self,
        manager,
        on_update: Callable[[dict], None],
        interval_sec: float = 4.0,
    ):
        self._manager = manager
        self._on_update = on_update
        self._interval = max(1.0, float(interval_sec))
        self._states: dict[str, _AccountState] = {}
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._probe_thread: threading.Thread | None = None
        self._pid_uid_cache: dict[int, tuple[float, str]] = {}
        self._cpu_count = max(1, psutil.cpu_count() or 1)
        self._presence_interval = 20.0

    def set_interval(self, seconds: float) -> None:
        self._interval = max(1.0, float(seconds))

    # Lifecycle

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="AutoConnectSupervisor",
        )
        self._thread.start()
        self._probe_thread = threading.Thread(
            target=self._run_probes, daemon=True, name="AutoConnectProbes",
        )
        self._probe_thread.start()
        print("[INFO] Auto Connect supervisor started.")

    def stop(self, join_timeout: float = 2.0) -> None:
        threads = [self._thread, getattr(self, "_probe_thread", None)]
        if not any(threads):
            return
        self._stop_event.set()
        current = threading.current_thread()
        for thread in threads:
            if thread and thread.is_alive() and thread is not current:
                thread.join(timeout=join_timeout)
        self._thread = None
        self._probe_thread = None
        print("[INFO] Auto Connect supervisor stopped.")

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # Configuration

    def set_configs(self, configs: dict) -> None:
        with self._lock:
            for account, config in configs.items():
                state = self._states.get(account)
                if state is None:
                    self._states[account] = _AccountState(account, config)
                else:
                    state.config = normalize_config(config)
            for account in list(self._states):
                if account not in configs:
                    self._states.pop(account, None)

    def remove_account(self, account: str) -> None:
        with self._lock:
            self._states.pop(account, None)

    def is_account_enabled(self, account: str) -> bool:
        with self._lock:
            state = self._states.get(account)
            return bool(state and state.enabled)

    def has_enabled_accounts(self) -> bool:
        with self._lock:
            return any(state.enabled for state in self._states.values())

    def enable_account(self, account: str) -> None:
        with self._lock:
            state = self._states.get(account)
            if state is None or state.enabled:
                return
            state.enabled = True
            state.attempts = 0
            state.last_error = ""
            self._set_state(state, STATE_CLOSED, time.time())
        self.start()

    def disable_account(self, account: str, close_client: bool = False) -> None:
        with self._lock:
            state = self._states.get(account)
            if state is None:
                return
            state.enabled = False
            state.launching = False
            state.launch_token += 1
            user_id = state.user_id
            pid = state.pid
            self._set_state(state, STATE_STOPPED, time.time())

        if close_client:
            killed = 0
            for cached_pid, (_, cached_uid) in list(self._pid_uid_cache.items()):
                if user_id and cached_uid == user_id:
                    kill_pid(cached_pid)
                    self._pid_uid_cache.pop(cached_pid, None)
                    killed += 1
            if not killed and pid:
                kill_pid(pid)
                killed = 1
            with self._lock:
                if state is not None:
                    state.pid = None
                    state.in_game = False
                    state.ram_mb = 0.0
                    state.cpu_percent = 0.0
                    state.ping_ms = None
            if killed:
                print(f"[Auto Connect] [{account}] Closed {killed} client(s).")

        if not self.has_enabled_accounts():
            self.stop(join_timeout=0.5)

    def enable_all(self) -> None:
        with self._lock:
            accounts = list(self._states)
        for account in accounts:
            self.enable_account(account)

    def enable_auto_start_accounts(self) -> int:
        """Resume the accounts flagged with 'Start with the app'."""
        with self._lock:
            accounts = [
                account for account, state in self._states.items()
                if state.config.get("auto_start", False)
            ]
        for account in accounts:
            self.enable_account(account)
        return len(accounts)

    def disable_all(self, close_clients: bool = False) -> int:
        with self._lock:
            accounts = list(self._states)
        for account in accounts:
            self.disable_account(account, close_client=False)

        if not close_clients:
            return 0

        with self._lock:
            for state in self._states.values():
                state.launch_token += 1
                state.launching = False

        closed = close_all_roblox()
        self._pid_uid_cache.clear()
        with self._lock:
            for state in self._states.values():
                state.pid = None
                state.in_game = False
                state.ram_mb = 0.0
                state.cpu_percent = 0.0
                state.ping_ms = None
                state.log_path = None
                state.log_offset = 0
                state.log_pid = None
                state.running_since = 0.0
        print(f"[Auto Connect] Stop All closed {closed} Roblox process(es).")
        return closed

    def restart_account(self, account: str) -> None:
        """Force-close the client of this account and start it again."""
        with self._lock:
            state = self._states.get(account)
            if state is None:
                return
            state.enabled = True
            state.attempts = 0
        self._force_close(account)
        self.start()

    def get_snapshot(self) -> dict:
        now = time.time()
        with self._lock:
            return {
                account: state.snapshot(now)
                for account, state in self._states.items()
            }

    # Monitor loop

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as exc:
                print(f"[ERROR] Auto Connect scan failed: {type(exc).__name__}: {exc}")
            self._emit()
            if self._stop_event.wait(self._interval):
                break

    def _emit(self) -> None:
        try:
            self._on_update(self.get_snapshot())
        except Exception:
            pass

    def _tick(self) -> None:
        now = time.time()
        self._resolve_user_ids()

        processes = presence_mod.get_roblox_processes()
        uid_to_pids = self._map_processes(processes)

        with self._lock:
            states = list(self._states.values())

        for state in states:
            self._update_account(state, processes, uid_to_pids, now)

    def _resolve_user_ids(self) -> None:
        with self._lock:
            pending = [state for state in self._states.values() if not state.user_id]
        for state in pending:
            account = self._manager.accounts.get(state.account)
            if not isinstance(account, dict):
                continue
            user_id = str(account.get("user_id", "") or "")
            if user_id and user_id != "0":
                state.user_id = user_id

    def _map_processes(self, processes: dict) -> dict[str, list[int]]:
        """Match every running Roblox client to the account that launched it."""
        current_pids = set(processes)
        self._pid_uid_cache = {
            pid: value for pid, value in self._pid_uid_cache.items()
            if pid in current_pids and value[0] == processes[pid][0]
        }

        used_logs: set[str] = set()
        for pid in sorted(current_pids):
            if pid in self._pid_uid_cache:
                continue
            create_time = processes[pid][0]
            user_id = presence_mod._get_user_id_from_pid(pid, used_logs)
            if user_id:
                self._pid_uid_cache[pid] = (create_time, user_id)

        uid_to_pids: dict[str, list[int]] = {}
        for pid, (_, user_id) in self._pid_uid_cache.items():
            uid_to_pids.setdefault(user_id, []).append(pid)
        return uid_to_pids

    def _refresh_presence(self) -> None:
        """Ask Roblox whether each account is in a game, using its own cookie."""
        with self._lock:
            states = [
                state for state in self._states.values()
                if state.enabled and state.user_id
                and state.config.get("check_presence", True)
            ]

        for state in states:
            if self._stop_event.is_set():
                return
            account = self._manager.accounts.get(state.account)
            cookie = account.get("cookie", "") if isinstance(account, dict) else ""
            if not cookie:
                continue
            presence = fetch_presence_batch([state.user_id], cookie)
            entry = presence.get(state.user_id)
            if entry is not None:
                state.in_game = bool(entry.get("in_game"))
            self._stop_event.wait(0.4)  # stay under the presence rate limit

    def _update_account(
        self,
        state: _AccountState,
        processes: dict,
        uid_to_pids: dict[str, list[int]],
        now: float,
    ) -> None:
        pids = uid_to_pids.get(state.user_id, []) if state.user_id else []
        pid = max(pids) if pids else None

        if pid != state.pid:
            state.pid = pid
            state.log_path = None
            state.log_offset = 0
            state.log_pid = None
            state.running_since = now if pid else 0.0

        if not state.enabled:
            if pid is None:
                state.ram_mb = 0.0
                state.cpu_percent = 0.0
                state.in_game = False
                state.ping_ms = None
                state.ping_source = ""
            else:
                self._read_metrics(state, processes.get(pid))
            state.launching = False
            self._set_state(state, STATE_STOPPED, now)
            return

        if pid is None:
            state.ram_mb = 0.0
            state.cpu_percent = 0.0
            state.in_game = False
            state.ping_ms = None
            state.ping_source = ""
            self._handle_closed(state, now)
            return

        state.launching = False
        self._read_metrics(state, processes.get(pid))

        if state.enabled and state.config.get("restart_on_error", True):
            error = self._scan_errors(state)
            if error:
                print(f"[Auto Connect] [{state.account}] {error} detected, restarting client.")
                state.last_error = error
                state.state = STATE_ERROR
                state.state_since = now
                self._force_close(state.account)
                return

        if state.in_game or not state.config.get("check_presence", True):
            state.last_error = ""
            state.attempts = 0  # a healthy client resets the retry budget
            self._set_state(
                state,
                STATE_IN_GAME if state.in_game else STATE_RUNNING,
                now,
            )
            return

        self._set_state(state, STATE_RUNNING, now)
        stuck_timeout = int(state.config.get("stuck_timeout", 180))
        if (
            state.enabled
            and state.config.get("restart_when_stuck", True)
            and now - state.state_since >= stuck_timeout
        ):
            print(
                f"[Auto Connect] [{state.account}] Client is not in a game for "
                f"{stuck_timeout}s, restarting."
            )
            state.last_error = "Stuck outside a game"
            state.state = STATE_ERROR
            state.state_since = now
            self._force_close(state.account)

    def _read_metrics(self, state: _AccountState, entry) -> None:
        if not entry:
            return
        process = entry[1]
        try:
            state.ram_mb = process.memory_info().rss / 1024 / 1024
        except (psutil.Error, OSError):
            state.ram_mb = 0.0
        try:
            state.cpu_percent = min(
                100.0,
                max(0.0, float(process.cpu_percent(interval=None))) / self._cpu_count,
            )
        except (psutil.Error, OSError):
            state.cpu_percent = 0.0
        if not state.running_since:
            state.running_since = time.time()

    def _scan_errors(self, state: _AccountState) -> str | None:
        if state.pid is None:
            return None
        if state.log_path is None or state.log_pid != state.pid:
            state.log_path = find_log_path_for_pid(state.pid)
            state.log_pid = state.pid
            state.log_offset = 0
        if not state.log_path:
            return None
        error, state.log_offset = scan_log_for_error(state.log_path, state.log_offset)
        return error

    def _handle_closed(self, state: _AccountState, now: float) -> None:
        if not state.enabled:
            self._set_state(state, STATE_STOPPED, now)
            return

        if state.launching:
            grace = int(state.config.get("launch_grace", 60))
            if now - state.launch_started < grace:
                self._set_state(state, STATE_LAUNCHING, now)
                return
            state.launching = False

        # STATE_ERROR is kept until the relaunch fires, so the row keeps showing
        # which Roblox error closed the client.
        if state.state not in (STATE_CLOSED, STATE_WAITING, STATE_ERROR):
            self._set_state(state, STATE_CLOSED, now)

        max_retries = int(state.config.get("max_retries", 0))
        if max_retries and state.attempts >= max_retries:
            return

        delay = int(state.config.get("relaunch_delay", 10))
        if now - state.state_since < delay:
            self._set_state(state, STATE_WAITING, now, keep_since=True)
            return

        self._launch(state, now)

    def _set_state(
        self,
        state: _AccountState,
        new_state: str,
        now: float,
        keep_since: bool = False,
    ) -> None:
        if state.state == new_state:
            return
        state.state = new_state
        if not keep_since:
            state.state_since = now

    def _force_close(self, account: str) -> None:
        """Kill every Roblox client that belongs to this account."""
        with self._lock:
            state = self._states.get(account)
        user_id = state.user_id if state else ""
        killed = 0
        for pid, (_, cached_uid) in list(self._pid_uid_cache.items()):
            if user_id and cached_uid == user_id:
                kill_pid(pid)
                self._pid_uid_cache.pop(pid, None)
                killed += 1
        if state is not None:
            if not killed and state.pid:
                kill_pid(state.pid)
                killed = 1
            state.pid = None
            state.log_path = None
            state.log_offset = 0
            state.log_pid = None
            state.running_since = 0.0
            state.in_game = False
            state.launching = False
            state.restarts += 1
            if state.state != STATE_ERROR:
                state.state = STATE_CLOSED
                state.state_since = time.time()
        if killed:
            print(f"[Auto Connect] [{account}] Force-closed {killed} client(s).")

    def _launch(self, state: _AccountState, now: float) -> None:
        if state.launching:
            return
        place_id = state.config.get("place_id", "")
        private_server = state.config.get("private_server", "")
        if not place_id and not private_server:
            state.last_error = "No Place ID or VIP link"
            state.enabled = False
            self._set_state(state, STATE_STOPPED, now)
            return

        state.launching = True
        state.launch_started = now
        state.attempts += 1
        state.launch_token += 1
        self._set_state(state, STATE_LAUNCHING, now)

        threading.Thread(
            target=self._launch_worker,
            args=(state, state.launch_token),
            daemon=True,
            name=f"AutoConnect-launch-{state.account}",
        ).start()

    def _launch_worker(self, state: _AccountState, token: int) -> None:
        config = state.config

        def cancelled() -> bool:
            return (
                self._stop_event.is_set()
                or not state.enabled
                or state.launch_token != token
            )

        try:
            if cancelled():
                state.launching = False
                return
            if config.get("check_internet", True) and not has_internet():
                print(f"[Auto Connect] [{state.account}] No internet, delaying launch.")
                state.launching = False
                state.state_since = time.time()
                return

            settings = actions.load_ui_settings()
            if cancelled():
                state.launching = False
                return
            print(
                f"[Auto Connect] [{state.account}] Launching "
                f"(place {config.get('place_id') or 'from VIP link'}, "
                f"attempt {state.attempts})"
            )
            result = self._manager.launch_roblox(
                state.account,
                config.get("place_id", ""),
                config.get("private_server", ""),
                settings.get("roblox_launcher", "default"),
                config.get("job_id", ""),
                settings.get("custom_roblox_launcher_path", ""),
            )
            if not result:
                state.last_error = getattr(result, "message", "") or "Launch failed"
                print(f"[Auto Connect] [{state.account}] Launch failed: {state.last_error}")
                state.launching = False
                state.state_since = time.time()
                return
            state.last_error = ""
            if cancelled():
                state.launching = False
                print(
                    f"[Auto Connect] [{state.account}] Launch cancelled, "
                    "closing the client that just started."
                )
                self._stop_event.wait(3.0)
                self._force_close(state.account)
                state.state = STATE_STOPPED
                state.state_since = time.time()
        except Exception as exc:
            state.last_error = f"{type(exc).__name__}: {exc}"
            print(f"[Auto Connect] [{state.account}] Launch error: {state.last_error}")
            state.launching = False
            state.state_since = time.time()

    # Network probes: ping and presence run off the monitor thread so a slow
    # request never delays process tracking or a relaunch.

    def _run_probes(self) -> None:
        last_presence = 0.0
        while not self._stop_event.is_set():
            try:
                self._refresh_pings()
                now = time.time()
                if now - last_presence >= self._presence_interval:
                    last_presence = now
                    self._refresh_presence()
            except Exception as exc:
                print(f"[ERROR] Auto Connect probe failed: {type(exc).__name__}: {exc}")
            if self._stop_event.wait(5.0):
                break

    def _refresh_pings(self) -> None:
        with self._lock:
            states = [
                state for state in self._states.values()
                if state.pid and state.config.get("measure_ping", True)
            ]
        for state in states:
            if self._stop_event.is_set():
                return
            latency, source = measure_ping_ms(state.pid)
            state.ping_ms = latency
            state.ping_source = source
