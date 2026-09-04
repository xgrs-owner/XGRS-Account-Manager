"""
Browser selection and executable resolution.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import shutil

from classes.operation_result import OperationResult
import features.chromium as chromium_mod


@dataclass(frozen=True)
class BrowserDescriptor:
    key: str
    label: str
    driver_type: str
    executable_path: str
    driver_path: str = ""
    bundled: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "driver_type": self.driver_type,
            "executable_path": self.executable_path,
            "driver_path": self.driver_path,
            "bundled": self.bundled,
        }


SUPPORTED_BROWSERS = (
    ("chrome", "Google Chrome"),
    ("firefox", "Mozilla Firefox"),
    ("edge", "Microsoft Edge"),
    ("brave", "Brave"),
    ("opera_gx", "Opera GX"),
    ("opera", "Opera"),
    ("vivaldi", "Vivaldi"),
    ("yandex", "Yandex Browser"),
    ("chromium", "Chromium"),
    ("custom", "Custom Browser"),
)

DRIVER_TYPES = ("chrome", "firefox", "edge")

_ENGINE_BY_KEY = {
    "chrome": "chrome",
    "firefox": "firefox",
    "edge": "edge",
    "brave": "chrome",
    "opera_gx": "chrome",
    "opera": "chrome",
    "vivaldi": "chrome",
    "yandex": "chrome",
    "chromium": "chrome",
}

_INSTALL_PATHS = {
    "chrome": (
        ("ProgramFiles", "Google/Chrome/Application/chrome.exe"),
        ("ProgramFiles(x86)", "Google/Chrome/Application/chrome.exe"),
        ("LOCALAPPDATA", "Google/Chrome/Application/chrome.exe"),
    ),
    "firefox": (
        ("ProgramFiles", "Mozilla Firefox/firefox.exe"),
        ("ProgramFiles(x86)", "Mozilla Firefox/firefox.exe"),
    ),
    "edge": (
        ("ProgramFiles(x86)", "Microsoft/Edge/Application/msedge.exe"),
        ("ProgramFiles", "Microsoft/Edge/Application/msedge.exe"),
        ("LOCALAPPDATA", "Microsoft/Edge/Application/msedge.exe"),
    ),
    "brave": (
        ("ProgramFiles", "BraveSoftware/Brave-Browser/Application/brave.exe"),
        ("ProgramFiles(x86)", "BraveSoftware/Brave-Browser/Application/brave.exe"),
        ("LOCALAPPDATA", "BraveSoftware/Brave-Browser/Application/brave.exe"),
    ),
    "opera_gx": (
        ("LOCALAPPDATA", "Programs/Opera GX/opera.exe"),
        ("ProgramFiles", "Opera GX/opera.exe"),
        ("ProgramFiles(x86)", "Opera GX/opera.exe"),
    ),
    "opera": (
        ("LOCALAPPDATA", "Programs/Opera/opera.exe"),
        ("ProgramFiles", "Opera/opera.exe"),
        ("ProgramFiles(x86)", "Opera/opera.exe"),
    ),
    "vivaldi": (
        ("LOCALAPPDATA", "Vivaldi/Application/vivaldi.exe"),
        ("ProgramFiles", "Vivaldi/Application/vivaldi.exe"),
        ("ProgramFiles(x86)", "Vivaldi/Application/vivaldi.exe"),
    ),
    "yandex": (
        ("LOCALAPPDATA", "Yandex/YandexBrowser/Application/browser.exe"),
        ("ProgramFiles", "Yandex/YandexBrowser/Application/browser.exe"),
        ("ProgramFiles(x86)", "Yandex/YandexBrowser/Application/browser.exe"),
    ),
}

_PATH_LOOKUP_NAMES = {
    "chrome": ("chrome.exe", "chrome"),
    "firefox": ("firefox.exe", "firefox"),
    "edge": ("msedge.exe", "msedge"),
    "brave": ("brave.exe", "brave"),
    "opera_gx": ("opera.exe",),
    "opera": ("opera.exe",),
    "vivaldi": ("vivaldi.exe", "vivaldi"),
    "yandex": ("browser.exe",),
}


def get_supported_browsers() -> tuple[tuple[str, str], ...]:
    return SUPPORTED_BROWSERS


def get_engine_for(browser_key: str) -> str:
    return _ENGINE_BY_KEY.get(str(browser_key or "").lower(), "chrome")


def infer_engine_from_path(path: str) -> str:
    name = os.path.basename(str(path or "")).lower()
    if name.startswith("firefox"):
        return "firefox"
    if name.startswith("msedge"):
        return "edge"
    return "chrome"


def _standard_paths(browser_type: str) -> list[str]:
    entries = _INSTALL_PATHS.get(browser_type)
    if not entries:
        return []

    candidates: list[str] = []
    for variable, relative in entries:
        root = os.environ.get(variable, "")
        if root:
            candidates.append(os.path.join(root, *relative.split("/")))

    for name in _PATH_LOOKUP_NAMES.get(browser_type, ()):
        found = shutil.which(name)
        if found:
            candidates.append(found)

    return list(dict.fromkeys(path for path in candidates if path))


def _missing_result(browser_type: str, checked_paths: list[str]) -> OperationResult:
    labels = dict(SUPPORTED_BROWSERS)
    key = browser_type.upper().replace(" ", "_")
    label = labels.get(browser_type, "Selected browser")
    known = browser_type in labels
    return OperationResult.failure(
        f"{key}_NOT_INSTALLED" if known else "BROWSER_SELECTION_INVALID",
        f"{label} Not Found" if known else "Invalid Browser Selection",
        (
            f"{label} could not be found. Select another browser or use the built-in "
            "Chromium under Settings -> Misc."
            if known
            else "Choose a browser in Browser Engine settings."
        ),
        detail=(
            "No registry or default-browser lookup was used. Checked paths:\n"
            + ("\n".join(checked_paths) if checked_paths else "none")
        ),
    )


def _custom_result(custom_path: str, custom_engine: str) -> OperationResult:
    path = str(custom_path or "").strip().strip('"')
    if not path:
        return OperationResult.failure(
            "CUSTOM_BROWSER_NOT_SET",
            "Custom Browser Not Set",
            "Choose the browser executable under Settings -> Misc -> Browser Engine.",
        )
    if not os.path.isfile(path):
        return OperationResult.failure(
            "CUSTOM_BROWSER_NOT_FOUND",
            "Custom Browser Not Found",
            "The selected browser executable no longer exists.",
            detail=f"Configured path: {path}",
        )

    engine = str(custom_engine or "").strip().lower()
    if engine not in DRIVER_TYPES:
        engine = infer_engine_from_path(path)

    return OperationResult.success(data={
        "browser": BrowserDescriptor(
            key="custom",
            label=f"Custom Browser ({os.path.basename(path)})",
            driver_type=engine,
            executable_path=path,
        ).as_dict(),
    })


def resolve_browser(
    browser_type: str,
    custom_path: str = "",
    custom_engine: str = "",
) -> OperationResult:
    selected = str(browser_type or "chrome").strip().lower()
    labels = dict(SUPPORTED_BROWSERS)
    if selected not in labels:
        return _missing_result(selected, [])

    if selected == "custom":
        return _custom_result(custom_path, custom_engine)

    if selected == "chromium":
        validation = chromium_mod.validate_chromium()
        if not validation:
            return validation
        return OperationResult.success(data={
            "browser": BrowserDescriptor(
                key="chromium",
                label="Chromium",
                driver_type="chrome",
                executable_path=validation.data.get("browser_path", ""),
                driver_path=validation.data.get("driver_path", ""),
                bundled=True,
            ).as_dict(),
        })

    checked_paths = _standard_paths(selected)
    for candidate in checked_paths:
        if os.path.isfile(candidate):
            return OperationResult.success(data={
                "browser": BrowserDescriptor(
                    key=selected,
                    label=labels[selected],
                    driver_type=get_engine_for(selected),
                    executable_path=candidate,
                ).as_dict(),
            })

    return _missing_result(selected, checked_paths)
