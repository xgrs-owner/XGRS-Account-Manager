"""
features/themes.py
Colour palettes for the interface.
"""

from __future__ import annotations

import json
import os
import re
import threading

from utils.app_paths import get_data_dir

_FILE = os.path.join(get_data_dir(), "theme.json")
_LOCK = threading.RLock()
_CACHE: dict | None = None

_HEX_PATTERN = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

COLOR_KEYS = (
    "bg",
    "panel",
    "input",
    "text",
    "muted",
    "line",
    "select",
    "note",
    "accent",
)

COLOR_LABELS = {
    "bg": "Background",
    "panel": "Panels",
    "input": "Inputs and lists",
    "text": "Text",
    "muted": "Secondary text",
    "line": "Borders",
    "select": "Selection",
    "note": "Highlight",
    "accent": "Accent",
}

PRESETS: dict[str, dict[str, str]] = {
    "Midnight": {
        "bg": "#0E0E0E",
        "panel": "#151515",
        "input": "#1A1A1A",
        "text": "#EDEDED",
        "muted": "#AAAAAA",
        "line": "#242424",
        "select": "#2A2A2A",
        "note": "#D6BB7D",
        "accent": "#0078D7",
    },
    "Carbon": {
        "bg": "#121212",
        "panel": "#1B1B1B",
        "input": "#202020",
        "text": "#E8E8E8",
        "muted": "#9A9A9A",
        "line": "#2C2C2C",
        "select": "#333333",
        "note": "#E0C070",
        "accent": "#4C8DFF",
    },
    "Nord": {
        "bg": "#2E3440",
        "panel": "#3B4252",
        "input": "#434C5E",
        "text": "#ECEFF4",
        "muted": "#A8B1C2",
        "line": "#4C566A",
        "select": "#4C566A",
        "note": "#EBCB8B",
        "accent": "#88C0D0",
    },
    "Dracula": {
        "bg": "#282A36",
        "panel": "#313442",
        "input": "#3A3D4D",
        "text": "#F8F8F2",
        "muted": "#A5A8B8",
        "line": "#44475A",
        "select": "#44475A",
        "note": "#F1FA8C",
        "accent": "#BD93F9",
    },
    "Ocean": {
        "bg": "#0B1520",
        "panel": "#12202E",
        "input": "#17293A",
        "text": "#E6F1FF",
        "muted": "#9BB3C9",
        "line": "#1E3446",
        "select": "#24405A",
        "note": "#F0C674",
        "accent": "#2DA6F2",
    },
    "Crimson": {
        "bg": "#140E10",
        "panel": "#1D1416",
        "input": "#241A1C",
        "text": "#F2E8EA",
        "muted": "#B09AA0",
        "line": "#33242A",
        "select": "#3D2B31",
        "note": "#E8B4A0",
        "accent": "#E0384E",
    },
    "Forest": {
        "bg": "#0D1411",
        "panel": "#141F1A",
        "input": "#1A2822",
        "text": "#E6F0EA",
        "muted": "#9AB3A6",
        "line": "#22342C",
        "select": "#2A4238",
        "note": "#D9C27E",
        "accent": "#3FB27F",
    },
    "Light": {
        "bg": "#F4F5F7",
        "panel": "#FFFFFF",
        "input": "#FFFFFF",
        "text": "#1B1D21",
        "muted": "#6B7280",
        "line": "#D8DBE0",
        "select": "#E3E7EE",
        "note": "#A6761D",
        "accent": "#0B65C2",
    },
}

DEFAULT_PRESET = "Midnight"


def is_valid_color(value: object) -> bool:
    return bool(isinstance(value, str) and _HEX_PATTERN.match(value.strip()))


def normalize_color(value: str) -> str:
    text = value.strip()
    if len(text) == 4:
        return "#" + "".join(char * 2 for char in text[1:]).upper()
    return "#" + text[1:].upper()


def get_preset_names() -> list[str]:
    return list(PRESETS)


def load_state() -> dict:
    global _CACHE
    with _LOCK:
        if _CACHE is not None:
            return {"preset": _CACHE["preset"], "custom": dict(_CACHE["custom"])}

        preset = DEFAULT_PRESET
        custom: dict[str, str] = {}
        if os.path.exists(_FILE):
            try:
                with open(_FILE, "r", encoding="utf-8") as handle:
                    loaded = json.load(handle)
            except (OSError, ValueError, TypeError):
                loaded = {}
            if isinstance(loaded, dict):
                name = str(loaded.get("preset", "") or "")
                if name in PRESETS:
                    preset = name
                raw_custom = loaded.get("custom", {})
                if isinstance(raw_custom, dict):
                    for key, value in raw_custom.items():
                        if key in COLOR_KEYS and is_valid_color(value):
                            custom[key] = normalize_color(value)

        _CACHE = {"preset": preset, "custom": custom}
        return {"preset": preset, "custom": dict(custom)}


def save_state(state: dict) -> None:
    global _CACHE
    preset = state.get("preset", DEFAULT_PRESET)
    if preset not in PRESETS:
        preset = DEFAULT_PRESET
    custom = {
        key: normalize_color(value)
        for key, value in (state.get("custom", {}) or {}).items()
        if key in COLOR_KEYS and is_valid_color(value)
    }

    os.makedirs(get_data_dir(), exist_ok=True)
    payload = {"preset": preset, "custom": custom}
    temp_file = _FILE + ".tmp"
    try:
        with open(temp_file, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        os.replace(temp_file, _FILE)
    except OSError as exc:
        print(f"[WARNING] Theme save failed: {exc}")
        if os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except OSError:
                pass
        return

    with _LOCK:
        _CACHE = {"preset": preset, "custom": dict(custom)}


def get_palette(state: dict | None = None) -> dict[str, str]:
    active = state if state is not None else load_state()
    palette = dict(PRESETS.get(active.get("preset", DEFAULT_PRESET), PRESETS[DEFAULT_PRESET]))
    for key, value in (active.get("custom", {}) or {}).items():
        if key in palette and is_valid_color(value):
            palette[key] = normalize_color(value)
    return palette


def get_preset_palette(name: str) -> dict[str, str]:
    return dict(PRESETS.get(name, PRESETS[DEFAULT_PRESET]))


def set_preset(name: str) -> dict[str, str]:
    state = load_state()
    state["preset"] = name if name in PRESETS else DEFAULT_PRESET
    state["custom"] = {}
    save_state(state)
    return get_palette(state)


def set_color(key: str, value: str) -> dict[str, str]:
    state = load_state()
    if key in COLOR_KEYS and is_valid_color(value):
        state["custom"][key] = normalize_color(value)
        save_state(state)
    return get_palette(state)


def reset_custom() -> dict[str, str]:
    state = load_state()
    state["custom"] = {}
    save_state(state)
    return get_palette(state)


def is_dark(palette: dict[str, str] | None = None) -> bool:
    active = palette or get_palette()
    color = active.get("bg", "#000000").lstrip("#")
    red, green, blue = (int(color[i:i + 2], 16) for i in (0, 2, 4))
    return (red * 299 + green * 587 + blue * 114) / 1000 < 128
