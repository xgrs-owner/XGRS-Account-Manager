from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
ASSETS_ROOT = PROJECT_ROOT / "assets"
SPEC_PATH = PROJECT_ROOT / "packaging" / "RobloxAccountManager.spec"
VERSION_MODULE_PATH = SOURCE_ROOT / "utils" / "version.py"
VERSION_INFO_PATH = PROJECT_ROOT / "build" / "version_info.txt"
OUTPUT_PATH = PROJECT_ROOT / "dist" / "RobloxAccountManager.exe"

_VERSION_ASSIGNMENT = re.compile(
    r'^\s*APP_VERSION\s*=\s*["\']([^"\']+)["\']\s*$'
)
_VERSION_FORMAT = re.compile(r"^\d+\.\d+\.\d+(?:\.\d+)?$")


def read_app_version() -> str:
    for line in VERSION_MODULE_PATH.read_text(encoding="utf-8").splitlines():
        match = _VERSION_ASSIGNMENT.fullmatch(line)
        if match:
            version = match.group(1)
            if not _VERSION_FORMAT.fullmatch(version):
                raise ValueError(
                    "APP_VERSION must use three or four numeric components."
                )
            return version
    raise ValueError("APP_VERSION was not found in src/utils/version.py.")


def generate_version_info(version: str) -> None:
    parts = [int(part) for part in version.split(".")]
    while len(parts) < 4:
        parts.append(0)
    version_tuple = tuple(parts[:4])
    windows_version = ".".join(str(part) for part in version_tuple)
    content = "\n".join([
        "# UTF-8",
        "VSVersionInfo(",
        "  ffi=FixedFileInfo(",
        f"    filevers={version_tuple},",
        f"    prodvers={version_tuple},",
        "    mask=0x3f,",
        "    flags=0x0,",
        "    OS=0x40004,",
        "    fileType=0x1,",
        "    subtype=0x0,",
        "    date=(0, 0)",
        "    ),",
        "  kids=[",
        "    StringFileInfo(",
        "      [",
        "      StringTable(",
        "        u'040904B0',",
        "        [StringStruct(u'CompanyName', u'evanovar'),",
        "        StringStruct(u'FileDescription', u'Roblox Account Manager'),",
        f"        StringStruct(u'FileVersion', u'{windows_version}'),",
        "        StringStruct(u'InternalName', u'RobloxAccountManager'),",
        "        StringStruct(u'LegalCopyright', u'Copyright (C) evanovar'),",
        "        StringStruct(u'OriginalFilename', u'RobloxAccountManager.exe'),",
        "        StringStruct(u'ProductName', u'Roblox Account Manager'),",
        f"        StringStruct(u'ProductVersion', u'{windows_version}')])",
        "      ]),",
        "    VarFileInfo([VarStruct(u'Translation', [1033, 1200])])",
        "  ]",
        ")",
        "",
    ])
    VERSION_INFO_PATH.parent.mkdir(parents=True, exist_ok=True)
    VERSION_INFO_PATH.write_text(content, encoding="utf-8")


def validate_build_files() -> None:
    required_paths = (
        SOURCE_ROOT / "main.py",
        ASSETS_ROOT / "icon.ico",
        ASSETS_ROOT / "discordlogo.png",
        SPEC_PATH,
        VERSION_MODULE_PATH,
    )
    missing = [str(path) for path in required_paths if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            "Required build files are missing:\n" + "\n".join(missing)
        )


def validate_release_tag(version: str) -> None:
    release_tag = os.environ.get("RAM_RELEASE_TAG", "").strip()
    if not release_tag:
        return
    tag_version = release_tag.removeprefix("v")
    if tag_version != version:
        raise ValueError(
            f"Release tag {release_tag} does not match APP_VERSION {version}."
        )


def main() -> int:
    try:
        validate_build_files()
        version = read_app_version()
        validate_release_tag(version)
        generate_version_info(version)
    except Exception as exc:
        print(f"[ERROR] Build preparation failed: {exc}")
        return 1

    print(f"[INFO] Building Evanovar RAM {version}")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            str(SPEC_PATH),
        ],
        cwd=PROJECT_ROOT,
        check=False,
    )
    if result.returncode != 0:
        print(f"[ERROR] PyInstaller exited with code {result.returncode}.")
        return result.returncode
    if not OUTPUT_PATH.is_file():
        print(f"[ERROR] Build output was not found: {OUTPUT_PATH}")
        return 1

    print(f"[SUCCESS] Build complete: {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
