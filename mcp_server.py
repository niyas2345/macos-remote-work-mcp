#!/usr/bin/env python3
"""macOS Remote Work & Accessibility MCP server.

Exposes 10 user-level macOS accessibility / remote-work tools over MCP
(streamable HTTP on 127.0.0.1:8000). All operations use safe, user-level
system calls only (defaults / osascript, no sudo, no cloud calls).

State lives in cursor_themes.json and macos_profiles.json (next to this
file); every tool call is appended to ~/.macos-remote-work/logs.json.
"""

import copy
import functools
import json
import math
import platform
import re
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from fastmcp import FastMCP

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = Path.home() / ".macos-remote-work"
LOG_FILE = STATE_DIR / "logs.json"
BACKUP_DIR = STATE_DIR / "backups"
THEMES_FILE = BASE_DIR / "cursor_themes.json"
PROFILES_FILE = BASE_DIR / "macos_profiles.json"

HOST = "127.0.0.1"
PORT = 8000
HTTP_PATH = "/mcp"
BRIDGE_PORT = 8010

UA_DOMAIN = "com.apple.universalaccess"
POINTER_KEY = "mouseDriverCursorSize"
POINTER_MIN = 0.5
POINTER_MAX = 3.0
CONTRAST_LEVELS = ("low", "normal", "high", "max")
KEYBOARD_UI_MODE_KEY = "AppleKeyboardUIMode"
KEYBOARD_NAV_ON = 2
KEYBOARD_NAV_OFF = 0
# Accessibility keys this server manages (removed on restore when the backup
# did not contain them, so settings return to their pre-change state).
MANAGED_KEYS = (
    POINTER_KEY,
    "increaseContrast",
    "reduceTransparency",
    "differentiateWithoutColor",
)
VOICEOVER_APP = "/System/Library/CoreServices/VoiceOver.app"
MAX_LOG_ENTRIES = 500
MAX_PROFILE_NAME_LEN = 64

# Contrast level -> macOS preference keys (user-level, com.apple.universalaccess).
# low and normal are the system baseline (macOS has no "below default" contrast);
# high enables Increase Contrast + Reduce Transparency; max additionally enables
# Differentiate Without Color.
CONTRAST_SETTINGS: Dict[str, Dict[str, bool]] = {
    "low": {
        "increaseContrast": False,
        "reduceTransparency": False,
        "differentiateWithoutColor": False,
    },
    "normal": {
        "increaseContrast": False,
        "reduceTransparency": False,
        "differentiateWithoutColor": False,
    },
    "high": {
        "increaseContrast": True,
        "reduceTransparency": True,
        "differentiateWithoutColor": False,
    },
    "max": {
        "increaseContrast": True,
        "reduceTransparency": True,
        "differentiateWithoutColor": True,
    },
}

DEFAULT_THEMES: Dict[str, Any] = {
    "themes": [
        {
            "name": "apple_cursor",
            "pointer_size": 1.5,
            "contrast_level": "high",
            "description": (
                "High-contrast companion preset for the apple_cursor theme (crisp, "
                "colorful, pixel-perfect macOS-style cursors by ful1e5). Enlarges the "
                "pointer and raises display contrast so the cursor stays visible "
                "during screen shares."
            ),
            "source": "https://github.com/ful1e5/apple_cursor",
        },
        {
            "name": "high_contrast",
            "pointer_size": 1.5,
            "contrast_level": "max",
            "description": (
                "Maximum display contrast with a moderately enlarged pointer for "
                "low-vision users and bright environments."
            ),
            "source": "builtin",
        },
        {
            "name": "large_pointer",
            "pointer_size": 3.0,
            "contrast_level": "normal",
            "description": (
                "Largest supported pointer size with default contrast for users who "
                "frequently lose the cursor on multi-monitor setups."
            ),
            "source": "builtin",
        },
        {
            "name": "remote_work_clear",
            "pointer_size": 1.5,
            "contrast_level": "high",
            "description": (
                "Readable preset for video calls and screen sharing: high contrast "
                "and a distinctly visible pointer without being distracting."
            ),
            "source": "builtin",
        },
    ],
    "state": {"active_theme": None, "last_applied": None},
}

DEFAULT_PROFILES: Dict[str, Any] = {
    "profiles": [
        {
            "name": "Remote Work",
            "description": (
                "Everyday remote-work setup: slightly larger pointer, high contrast, "
                "full keyboard navigation, remote-work mode on."
            ),
            "pointer_size": 1.5,
            "display_contrast": "high",
            "voiceover_enabled": False,
            "keyboard_navigation": True,
            "remote_work_mode": True,
        },
        {
            "name": "Accessibility Power User",
            "description": (
                "Maximum accessibility: large pointer, max contrast, full keyboard "
                "navigation, VoiceOver ready."
            ),
            "pointer_size": 2.0,
            "display_contrast": "max",
            "voiceover_enabled": True,
            "keyboard_navigation": True,
            "remote_work_mode": False,
        },
        {
            "name": "Developer Focus",
            "description": (
                "Crisp high contrast and a slightly larger pointer with minimal "
                "distraction for long coding sessions."
            ),
            "pointer_size": 1.3,
            "display_contrast": "high",
            "voiceover_enabled": False,
            "keyboard_navigation": True,
            "remote_work_mode": False,
        },
        {
            "name": "High Contrast",
            "description": (
                "Full accessibility preset: maximum contrast and enlarged pointer."
            ),
            "pointer_size": 2.0,
            "display_contrast": "max",
            "voiceover_enabled": False,
            "keyboard_navigation": True,
            "remote_work_mode": False,
        },
    ],
    "state": {
        "active_profile": None,
        "remote_work_mode": False,
        "last_applied": None,
    },
}

mcp = FastMCP("macos-remote-work")


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _run(cmd: list, timeout: int = 15) -> Dict[str, Any]:
    """Run a safe user-level command (no shell) and capture output."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return {
            "cmd": " ".join(cmd),
            "returncode": proc.returncode,
            "stdout": proc.stdout or "",
            "stderr": proc.stderr or "",
        }
    except subprocess.TimeoutExpired:
        return {
            "cmd": " ".join(cmd),
            "returncode": -1,
            "stdout": "",
            "stderr": f"timeout after {timeout}s",
        }
    except Exception as exc:  # pragma: no cover - defensive
        return {
            "cmd": " ".join(cmd),
            "returncode": -1,
            "stdout": "",
            "stderr": str(exc),
        }


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return copy.deepcopy(default)
    except (json.JSONDecodeError, OSError):
        return copy.deepcopy(default)


def _is_corrupt(path: Path) -> bool:
    try:
        json.loads(path.read_text(encoding="utf-8"))
        return False
    except Exception:
        return True


def ensure_state() -> None:
    """Create config/state directories and seed JSON files when missing."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    for path, default in ((THEMES_FILE, DEFAULT_THEMES), (PROFILES_FILE, DEFAULT_PROFILES)):
        if not path.exists():
            _write_json(path, default)
        elif _is_corrupt(path):
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            corrupt = path.with_name(path.name + ".corrupt-" + ts)
            try:
                path.replace(corrupt)
            except OSError:
                pass
            _write_json(path, default)
    if not LOG_FILE.exists():
        _write_json(LOG_FILE, [])


def _log(tool: str, success: bool, arguments: Dict[str, Any], duration_ms: float) -> None:
    try:
        entries = _load_json(LOG_FILE, [])
        if not isinstance(entries, list):
            entries = []
        entries.append(
            {
                "timestamp": _now(),
                "tool": tool,
                "success": bool(success),
                "duration_ms": round(duration_ms, 1),
                "arguments": arguments,
            }
        )
        entries = entries[-MAX_LOG_ENTRIES:]
        _write_json(LOG_FILE, entries)
    except Exception:
        pass  # logging must never break a tool call


def _safe_args(args: Dict[str, Any]) -> Dict[str, Any]:
    safe: Dict[str, Any] = {}
    for key, value in args.items():
        try:
            json.dumps(value)
            safe[key] = value
        except (TypeError, ValueError):
            safe[key] = str(value)
        if isinstance(safe[key], str) and len(safe[key]) > 300:
            safe[key] = safe[key][:300] + "..."
    return safe


def _ok(tool: str, message: str, **meta: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "success": True,
        "tool": tool,
        "message": message,
        "timestamp": _now(),
    }
    payload.update(meta)
    return payload


def _err(tool: str, message: str, **meta: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "success": False,
        "tool": tool,
        "message": message,
        "timestamp": _now(),
    }
    payload.update(meta)
    return payload


def _guarded(fn):
    """Wrap a tool so every call is logged and unexpected errors become JSON."""
    name = fn.__name__

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        start = time.time()
        call_args = dict(kwargs)
        bound_names = fn.__code__.co_varnames[: fn.__code__.co_argcount]
        for index, value in enumerate(args):
            if index < len(bound_names):
                call_args[bound_names[index]] = value
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:  # defensive: always return valid JSON
            result = _err(
                name,
                f"unexpected error: {exc}",
                error_type=type(exc).__name__,
            )
        if not isinstance(result, dict):
            result = _err(name, "tool returned a non-JSON result")
        _log(name, bool(result.get("success")), _safe_args(call_args), (time.time() - start) * 1000)
        return result

    return wrapper


def _normalize(name: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _match_name(items: list, name: Any) -> Optional[Dict[str, Any]]:
    """Exact match first, then case/punctuation-insensitive match."""
    text = str(name).strip()
    for item in items:
        if item.get("name") == text:
            return item
    target = _normalize(text)
    matches = [item for item in items if _normalize(item.get("name", "")) == target]
    if len(matches) == 1:
        return matches[0]
    return None


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


# --------------------------------------------------------------------------
# macOS setting primitives (safe, user-level only)
# --------------------------------------------------------------------------

def _defaults_read(domain: str, key: str) -> Optional[str]:
    result = _run(["defaults", "read", domain, key], timeout=10)
    if result["returncode"] != 0:
        return None
    return result["stdout"].strip()


def _read_pointer_size() -> Optional[float]:
    raw = _defaults_read(UA_DOMAIN, POINTER_KEY)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _read_bool_key(key: str) -> bool:
    raw = _defaults_read(UA_DOMAIN, key)
    if raw is None:
        return False
    return raw.strip().lower() in ("1", "true", "yes")


def _read_contrast_level() -> str:
    if not _read_bool_key("increaseContrast"):
        return "normal"
    if _read_bool_key("differentiateWithoutColor"):
        return "max"
    return "high"


def _read_keyboard_nav() -> bool:
    raw = _defaults_read("-g", KEYBOARD_UI_MODE_KEY)
    if raw is None:
        return False
    try:
        return int(float(raw)) >= 1
    except ValueError:
        return False


def _voiceover_running() -> bool:
    return _run(["pgrep", "-x", "VoiceOver"], timeout=10)["returncode"] == 0


def _wait_for_voiceover(seconds: float = 5.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if _voiceover_running():
            return True
        time.sleep(0.5)
    return _voiceover_running()


def _apply_pointer_size(size: float) -> bool:
    result = _run(["defaults", "write", UA_DOMAIN, POINTER_KEY, "-float", f"{float(size):.4f}"])
    return result["returncode"] == 0


def _apply_contrast(level: str) -> tuple:
    failed = []
    for key, enabled in CONTRAST_SETTINGS[level].items():
        result = _run(
            ["defaults", "write", UA_DOMAIN, key, "-bool", "true" if enabled else "false"]
        )
        if result["returncode"] != 0:
            failed.append(key)
    return (not failed, failed)


def _apply_keyboard_nav(enabled: bool) -> bool:
    value = KEYBOARD_NAV_ON if enabled else KEYBOARD_NAV_OFF
    result = _run(["defaults", "write", "-g", KEYBOARD_UI_MODE_KEY, "-int", str(value)])
    return result["returncode"] == 0


def _load_themes() -> Dict[str, Any]:
    data = _load_json(THEMES_FILE, DEFAULT_THEMES)
    if not isinstance(data, dict) or "themes" not in data:
        data = copy.deepcopy(DEFAULT_THEMES)
    return data


def _load_profiles() -> Dict[str, Any]:
    data = _load_json(PROFILES_FILE, DEFAULT_PROFILES)
    if not isinstance(data, dict) or "profiles" not in data:
        data = copy.deepcopy(DEFAULT_PROFILES)
    return data


def _uptime_seconds() -> Optional[int]:
    result = _run(["sysctl", "-n", "kern.boottime"], timeout=10)
    match = re.search(r"sec = (\d+)", result["stdout"])
    if not match:
        return None
    return max(0, int(time.time()) - int(match.group(1)))


def _settings_snapshot() -> Dict[str, Any]:
    return {
        "pointer_size": _read_pointer_size(),
        "display_contrast": _read_contrast_level(),
        "voiceover_enabled": _voiceover_running(),
        "keyboard_navigation": _read_keyboard_nav(),
    }


def _list_backups() -> list:
    try:
        return sorted(path.name for path in BACKUP_DIR.glob("*.plist"))
    except OSError:
        return []


def _plist_keys(path: Path) -> Optional[set]:
    """Return the top-level keys of a plist file (None on failure)."""
    result = _run(["plutil", "-convert", "json", "-o", "-", str(path)], timeout=10)
    if result["returncode"] != 0:
        return None
    try:
        return set(json.loads(result["stdout"]).keys())
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------
# Tools (10)
# --------------------------------------------------------------------------

@mcp.tool()
@_guarded
def get_system_status() -> Dict[str, Any]:
    """Get the current macOS version, uptime, and live accessibility settings
    (pointer size, display contrast, VoiceOver, keyboard navigation) plus the
    active profile/theme and server info."""
    mac_ver = platform.mac_ver()
    state_profiles = _load_profiles().get("state", {}) or {}
    state_themes = _load_themes().get("state", {}) or {}
    backups = _list_backups()
    return _ok(
        "get_system_status",
        "System status retrieved.",
        os="macOS",
        os_version=mac_ver[0] or platform.release(),
        build=mac_ver[1][2] if mac_ver[1] and len(mac_ver[1]) > 2 else None,
        architecture=platform.machine(),
        hostname=socket.gethostname(),
        uptime_seconds=_uptime_seconds(),
        python_version=platform.python_version(),
        current_settings=_settings_snapshot(),
        active_profile=state_profiles.get("active_profile"),
        active_theme=state_themes.get("active_theme"),
        remote_work_mode=bool(state_profiles.get("remote_work_mode", False)),
        server={"host": HOST, "port": PORT, "endpoint": f"http://{HOST}:{PORT}{HTTP_PATH}"},
        backup_count=len(backups),
        latest_backup=backups[-1] if backups else None,
    )


@mcp.tool()
@_guarded
def list_cursor_themes() -> Dict[str, Any]:
    """List the available cursor themes (apple_cursor, high_contrast,
    large_pointer, remote_work_clear) with pointer size, contrast level,
    description, and source. Also reports the currently active theme."""
    data = _load_themes()
    return _ok(
        "list_cursor_themes",
        f"Found {len(data['themes'])} cursor theme(s).",
        themes=data["themes"],
        active_theme=(data.get("state") or {}).get("active_theme"),
    )


@mcp.tool()
@_guarded
def apply_cursor_theme(theme_name: str) -> Dict[str, Any]:
    """Apply a cursor theme by name (e.g. 'apple_cursor', 'high_contrast',
    'large_pointer', 'remote_work_clear'). Applies the theme's pointer size and
    display contrast settings and marks it active."""
    themes = _load_themes()
    theme = _match_name(themes["themes"], theme_name)
    if theme is None:
        return _err(
            "apply_cursor_theme",
            f"Unknown cursor theme '{theme_name}'.",
            available_themes=[t["name"] for t in themes["themes"]],
        )

    failures = []
    if not _apply_pointer_size(float(theme["pointer_size"])):
        failures.append("pointer_size")
    contrast = str(theme.get("contrast_level", "normal")).lower()
    if contrast not in CONTRAST_LEVELS:
        contrast = "normal"
    contrast_ok, _ = _apply_contrast(contrast)
    if not contrast_ok:
        failures.append("display_contrast")

    themes.setdefault("state", {})
    themes["state"]["active_theme"] = theme["name"]
    themes["state"]["last_applied"] = _now()
    _write_json(THEMES_FILE, themes)

    if failures:
        return _err(
            "apply_cursor_theme",
            f"Theme '{theme['name']}' only partially applied.",
            theme=theme,
            failed_settings=failures,
        )
    return _ok(
        "apply_cursor_theme",
        f"Cursor theme '{theme['name']}' applied.",
        theme=theme,
        applied_settings={
            "pointer_size": _read_pointer_size(),
            "display_contrast": _read_contrast_level(),
        },
        note="Pointer/contrast values are persisted immediately; log out and back in if the on-screen cursor has not resized yet.",
    )


@mcp.tool()
@_guarded
def list_accessibility_profiles() -> Dict[str, Any]:
    """List the available macOS accessibility/remote-work profiles (Remote Work,
    Accessibility Power User, Developer Focus, High Contrast, plus any custom
    profiles) with their settings. Also reports the active profile and whether
    remote-work mode is on."""
    data = _load_profiles()
    state = data.get("state") or {}
    return _ok(
        "list_accessibility_profiles",
        f"Found {len(data['profiles'])} profile(s).",
        profiles=data["profiles"],
        active_profile=state.get("active_profile"),
        remote_work_mode=bool(state.get("remote_work_mode", False)),
    )


@mcp.tool()
@_guarded
def create_accessibility_profile(
    profile_name: str,
    pointer_size: Optional[float] = None,
    display_contrast: Optional[str] = None,
    voiceover_enabled: Optional[bool] = None,
    keyboard_navigation: Optional[bool] = None,
    remote_work_mode: Optional[bool] = None,
    description: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a custom accessibility profile. Only 'profile_name' is required;
    any omitted setting is snapshotted from the current system state. Optional
    settings: pointer_size (0.5-3.0), display_contrast ('low'|'normal'|'high'|
    'max'), voiceover_enabled (bool), keyboard_navigation (bool),
    remote_work_mode (bool), description (str)."""
    if not isinstance(profile_name, str) or not profile_name.strip():
        return _err("create_accessibility_profile", "profile_name must be a non-empty string.")
    name = profile_name.strip()
    if len(name) > MAX_PROFILE_NAME_LEN:
        return _err(
            "create_accessibility_profile",
            f"profile_name must be at most {MAX_PROFILE_NAME_LEN} characters.",
        )
    if re.search(r"[\x00-\x1f]", name):
        return _err("create_accessibility_profile", "profile_name contains invalid control characters.")

    data = _load_profiles()
    if _match_name(data["profiles"], name) is not None:
        return _err(
            "create_accessibility_profile",
            f"Profile '{name}' already exists.",
            existing_profiles=[p["name"] for p in data["profiles"]],
        )

    snapshot = _settings_snapshot()
    profile: Dict[str, Any] = {"name": name}

    if pointer_size is None:
        profile["pointer_size"] = snapshot["pointer_size"] if snapshot["pointer_size"] is not None else 1.0
    else:
        try:
            size = float(pointer_size)
        except (TypeError, ValueError):
            return _err(
                "create_accessibility_profile",
                f"pointer_size must be a number between {POINTER_MIN} and {POINTER_MAX}.",
            )
        if not math.isfinite(size) or not (POINTER_MIN <= size <= POINTER_MAX):
            return _err(
                "create_accessibility_profile",
                f"pointer_size must be between {POINTER_MIN} and {POINTER_MAX}.",
                received=size,
            )
        profile["pointer_size"] = round(size, 4)

    if display_contrast is None:
        profile["display_contrast"] = snapshot["display_contrast"]
    else:
        level = str(display_contrast).strip().lower()
        if level not in CONTRAST_LEVELS:
            return _err(
                "create_accessibility_profile",
                f"display_contrast must be one of {list(CONTRAST_LEVELS)}.",
            )
        profile["display_contrast"] = level

    profile["voiceover_enabled"] = (
        snapshot["voiceover_enabled"] if voiceover_enabled is None else _to_bool(voiceover_enabled)
    )
    profile["keyboard_navigation"] = (
        snapshot["keyboard_navigation"] if keyboard_navigation is None else _to_bool(keyboard_navigation)
    )
    profile["remote_work_mode"] = (
        bool((data.get("state") or {}).get("remote_work_mode", False))
        if remote_work_mode is None
        else _to_bool(remote_work_mode)
    )
    profile["description"] = (
        description.strip()
        if isinstance(description, str) and description.strip()
        else f"Custom accessibility profile '{name}'."
    )
    profile["custom"] = True
    profile["created_at"] = _now()

    data["profiles"].append(profile)
    _write_json(PROFILES_FILE, data)

    return _ok(
        "create_accessibility_profile",
        f"Profile '{name}' created.",
        profile=profile,
        total_profiles=len(data["profiles"]),
    )


@mcp.tool()
@_guarded
def apply_remote_work_profile(profile_name: str) -> Dict[str, Any]:
    """Apply an accessibility/remote-work profile by name (e.g. 'Remote Work',
    'Accessibility Power User', 'Developer Focus', 'High Contrast', or a custom
    profile). Sets pointer size, display contrast, keyboard navigation, enables
    VoiceOver when the profile requests it, and updates remote-work mode state."""
    data = _load_profiles()
    profile = _match_name(data["profiles"], profile_name)
    if profile is None:
        return _err(
            "apply_remote_work_profile",
            f"Unknown profile '{profile_name}'.",
            available_profiles=[p["name"] for p in data["profiles"]],
        )

    failures = []
    notes = []

    if not _apply_pointer_size(float(profile.get("pointer_size", 1.0))):
        failures.append("pointer_size")

    level = str(profile.get("display_contrast", "normal")).lower()
    if level not in CONTRAST_LEVELS:
        level = "normal"
    contrast_ok, _ = _apply_contrast(level)
    if not contrast_ok:
        failures.append("display_contrast")

    if not _apply_keyboard_nav(_to_bool(profile.get("keyboard_navigation", False))):
        failures.append("keyboard_navigation")

    if _to_bool(profile.get("voiceover_enabled", False)):
        vo_result = enable_voiceover()
        if not vo_result.get("success"):
            failures.append("voiceover")
            notes.append("VoiceOver could not be enabled (it may need Accessibility permission).")
    else:
        notes.append("VoiceOver left unchanged (profile does not request it).")

    state = data.setdefault("state", {})
    state["active_profile"] = profile["name"]
    state["remote_work_mode"] = _to_bool(profile.get("remote_work_mode", False))
    state["last_applied"] = _now()
    _write_json(PROFILES_FILE, data)

    applied = dict(_settings_snapshot())
    applied["remote_work_mode"] = state["remote_work_mode"]

    if failures:
        return _err(
            "apply_remote_work_profile",
            f"Profile '{profile['name']}' partially applied.",
            profile=profile,
            applied_settings=applied,
            failed_settings=failures,
            notes=notes,
        )
    return _ok(
        "apply_remote_work_profile",
        f"Profile '{profile['name']}' applied.",
        profile=profile,
        applied_settings=applied,
        notes=notes,
    )


@mcp.tool()
@_guarded
def set_pointer_size(size: float) -> Dict[str, Any]:
    """Set the macOS pointer/cursor size. Allowed range: 0.5 to 3.0
    (1.0 is the system default)."""
    try:
        value = float(size)
    except (TypeError, ValueError):
        return _err(
            "set_pointer_size",
            f"size must be a number between {POINTER_MIN} and {POINTER_MAX}.",
            received=repr(size),
        )
    if not math.isfinite(value) or not (POINTER_MIN <= value <= POINTER_MAX):
        return _err(
            "set_pointer_size",
            f"size must be between {POINTER_MIN} and {POINTER_MAX}.",
            received=value,
            allowed_min=POINTER_MIN,
            allowed_max=POINTER_MAX,
        )

    result = _run(["defaults", "write", UA_DOMAIN, POINTER_KEY, "-float", f"{value:.4f}"])
    if result["returncode"] != 0:
        return _err(
            "set_pointer_size",
            "Could not write pointer size (check macOS permissions).",
            stderr=result["stderr"].strip(),
        )

    current = _read_pointer_size()
    return _ok(
        "set_pointer_size",
        f"Pointer size set to {value}.",
        requested_size=value,
        pointer_size=current if current is not None else value,
        note="Persisted immediately; log out and back in if the on-screen cursor has not resized yet.",
    )


@mcp.tool()
@_guarded
def set_display_contrast(level: str) -> Dict[str, Any]:
    """Set display contrast. Allowed levels: 'low', 'normal', 'high', 'max'.
    'high' enables Increase Contrast + Reduce Transparency; 'max' additionally
    enables Differentiate Without Color; 'low'/'normal' restore the system
    baseline."""
    try:
        normalized = str(level).strip().lower()
    except Exception:
        normalized = ""
    if normalized not in CONTRAST_LEVELS:
        return _err(
            "set_display_contrast",
            f"level must be one of {list(CONTRAST_LEVELS)}.",
            received=repr(level),
            allowed_levels=list(CONTRAST_LEVELS),
        )

    ok, failed = _apply_contrast(normalized)
    if not ok:
        return _err(
            "set_display_contrast",
            "Could not write all contrast settings (check macOS permissions).",
            failed_keys=failed,
        )

    return _ok(
        "set_display_contrast",
        f"Display contrast set to '{normalized}'.",
        requested_level=normalized,
        display_contrast=_read_contrast_level(),
        settings=CONTRAST_SETTINGS[normalized],
        note="'low' and 'normal' both report back as 'normal' (macOS has no below-default contrast).",
    )


@mcp.tool()
@_guarded
def enable_voiceover() -> Dict[str, Any]:
    """Enable the macOS VoiceOver screen reader (Cmd+F5). Safe to call when
    VoiceOver is already running - it will not toggle it off."""
    if _voiceover_running():
        return _ok(
            "enable_voiceover",
            "VoiceOver is already enabled.",
            voiceover_enabled=True,
            method="already_running",
        )

    # Preferred: the official Cmd+F5 toggle (requires Accessibility permission
    # for the calling process).
    key_result = _run(
        ["osascript", "-e", 'tell application "System Events" to key code 55 using {command down}'],
        timeout=10,
    )
    if key_result["returncode"] == 0 and _wait_for_voiceover(4.0):
        return _ok(
            "enable_voiceover",
            "VoiceOver enabled via Cmd+F5.",
            voiceover_enabled=True,
            method="keyboard_shortcut",
        )

    # Fallback: launch VoiceOver.app directly (no Accessibility permission needed).
    _run(["open", VOICEOVER_APP], timeout=10)
    if _wait_for_voiceover(6.0):
        return _ok(
            "enable_voiceover",
            "VoiceOver enabled by launching VoiceOver.app.",
            voiceover_enabled=True,
            method="launch_app",
        )

    detail = (key_result["stderr"] or key_result["stdout"]).strip()[:400]
    return _err(
        "enable_voiceover",
        "Could not enable VoiceOver. Grant Accessibility permission to your "
        "terminal app (System Preferences > Security & Privacy > Privacy > "
        "Accessibility) and try again.",
        voiceover_enabled=False,
        osascript_output=detail,
    )


@mcp.tool()
@_guarded
def backup_macos_settings(restore_from: Optional[str] = None) -> Dict[str, Any]:
    """Backup current macOS accessibility settings to
    ~/.macos-remote-work/backups/. With no arguments it creates a new backup
    (plist + metadata JSON). Pass restore_from=<path to a previous backup .plist>
    to restore a backup instead (must live inside the backups directory)."""
    if restore_from:
        target = Path(str(restore_from)).expanduser()
        try:
            resolved = target.resolve()
        except OSError:
            return _err("backup_macos_settings", "Invalid restore path.")
        if not resolved.is_relative_to(BACKUP_DIR.resolve()):
            return _err(
                "backup_macos_settings",
                "restore_from must point to a file inside ~/.macos-remote-work/backups/.",
                allowed_directory=str(BACKUP_DIR),
            )
        if not resolved.is_file() or resolved.suffix != ".plist":
            return _err(
                "backup_macos_settings",
                "restore_from must be an existing .plist backup file.",
                received=str(resolved),
            )

        import_result = _run(["defaults", "import", UA_DOMAIN, str(resolved)], timeout=20)
        if import_result["returncode"] != 0:
            return _err(
                "backup_macos_settings",
                "Restore failed (defaults import error).",
                stderr=import_result["stderr"].strip(),
            )

        # Flush the per-user preferences cache so imported values are read back
        # (cfprefsd restarts automatically via launchd).
        _run(["killall", "cfprefsd"], timeout=10)
        time.sleep(0.5)

        # Remove managed accessibility keys that the backup did not contain, so
        # settings changed after the backup are fully rolled back.
        backup_keys = _plist_keys(resolved)
        deleted_keys = []
        if backup_keys is not None:
            for key in MANAGED_KEYS:
                if key not in backup_keys:
                    if _run(["defaults", "delete", UA_DOMAIN, key], timeout=10)["returncode"] == 0:
                        deleted_keys.append(key)

        keyboard_restored = None
        sidecar = resolved.with_suffix(".json")
        if sidecar.is_file():
            meta = _load_json(sidecar, {})
            keyboard_value = (meta.get("captured") or {}).get("keyboard_uimode")
            if keyboard_value is not None:
                keyboard_restored = _apply_keyboard_nav(int(keyboard_value) >= 1)

        return _ok(
            "backup_macos_settings",
            f"Restored settings from {resolved.name}.",
            restored_from=str(resolved),
            restored_settings=_settings_snapshot(),
            removed_keys=deleted_keys,
            keyboard_navigation_restored=keyboard_restored,
        )

    # Backup mode
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    plist_path = BACKUP_DIR / f"mac_settings_{stamp}.plist"
    meta_path = BACKUP_DIR / f"mac_settings_{stamp}.json"

    export_result = _run(["defaults", "export", UA_DOMAIN, str(plist_path)], timeout=20)
    if export_result["returncode"] != 0 or not plist_path.is_file():
        return _err(
            "backup_macos_settings",
            "Backup failed (defaults export error).",
            stderr=export_result["stderr"].strip(),
        )

    raw_keyboard = _defaults_read("-g", KEYBOARD_UI_MODE_KEY)
    meta = {
        "timestamp": _now(),
        "os_version": platform.mac_ver()[0],
        "backup_file": str(plist_path),
        "captured": {
            **_settings_snapshot(),
            "keyboard_uimode": int(raw_keyboard) if raw_keyboard and raw_keyboard.lstrip("-").isdigit() else 0,
        },
    }
    _write_json(meta_path, meta)

    return _ok(
        "backup_macos_settings",
        f"Settings backed up to {plist_path.name}.",
        backup_file=str(plist_path),
        metadata_file=str(meta_path),
        captured_settings=meta["captured"],
        restore_hint="Restore with backup_macos_settings(restore_from=<backup_file>).",
    )


# --------------------------------------------------------------------------
# Entrypoint
# --------------------------------------------------------------------------

def main() -> None:
    ensure_state()
    print(
        f"[macos-remote-work] MCP server listening on http://{HOST}:{PORT}{HTTP_PATH}",
        flush=True,
    )
    mcp.run(transport="http", host=HOST, port=PORT, path=HTTP_PATH, log_level="info")


if __name__ == "__main__":
    main()
