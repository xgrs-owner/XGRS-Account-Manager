"""
Shared filesystem paths for source runs and PyInstaller builds.
"""

from __future__ import annotations

import os
import sys


def get_project_root() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    source_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if os.path.basename(source_root).casefold() == "src":
        return os.path.dirname(source_root)
    return source_root


def get_app_dir() -> str:
    return get_project_root()


def get_bundle_root() -> str:
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", get_project_root())
    return get_project_root()


def get_resource_path(*parts: str) -> str:
    return os.path.join(get_bundle_root(), *parts)


DATA_DIR_NAME = "XGRSManagerData"
LEGACY_DATA_DIR_NAMES = ("AccountManagerData",)

_resolved_data_dir: str | None = None


def _resolve_data_dir() -> str:
    global _resolved_data_dir
    if _resolved_data_dir is not None:
        return _resolved_data_dir

    root = get_project_root()
    target = os.path.join(root, DATA_DIR_NAME)
    if os.path.exists(target):
        _resolved_data_dir = target
        return target

    for name in LEGACY_DATA_DIR_NAMES:
        legacy = os.path.join(root, name)
        if not os.path.isdir(legacy):
            continue
        try:
            os.rename(legacy, target)
            _resolved_data_dir = target
        except OSError as exc:
            print(f"[WARNING] Could not rename {name} to {DATA_DIR_NAME}: {exc}")
            _resolved_data_dir = legacy
        return _resolved_data_dir

    _resolved_data_dir = target
    return target


def get_data_dir() -> str:
    return _resolve_data_dir()
