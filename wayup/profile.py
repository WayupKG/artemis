# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Per-project profile and compact screen output for the wayup MCP server.

One MCP server is shared by many projects; each project describes itself in an
``.artemis.json`` file found by walking up from the server's working directory
(or pointed to by ``ARTEMIS_PROFILE``)::

    {
      "allowed_packages": ["kg.replai.revision"],
      "device": "10AG3Z32G6001L4",
      "show_system_ui": false
    }

``allowed_packages`` turns on the foreground guard: input actions are refused
unless one of these packages is on top, so a personal phone is never driven
inside unrelated apps. Without a profile the server behaves like upstream.
"""

from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PROFILE_FILE_NAME = ".artemis.json"

#: System permission dialogs appear on top of the app under test; refusing to tap
#: them would make every runtime-permission flow impossible.
ALWAYS_ALLOWED_PACKAGES = frozenset(
    {
        "com.android.permissioncontroller",
        "com.google.android.permissioncontroller",
    }
)

#: Packages dropped from the compact hierarchy unless ``show_system_ui`` is set.
SYSTEM_UI_PACKAGES = frozenset({"com.android.systemui"})

_MAX_LABEL_CHARS = 160


@dataclass
class Profile:
    source: str | None = None
    allowed_packages: list[str] = field(default_factory=list)
    device: str | None = None
    show_system_ui: bool = False


_selected_serial: str | None = None


def find_profile_path(start: Path | None = None) -> Path | None:
    explicit = os.environ.get("ARTEMIS_PROFILE")
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.is_file() else None
    current = (start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        candidate = directory / PROFILE_FILE_NAME
        if candidate.is_file():
            return candidate
    return None


def load_profile(start: Path | None = None) -> Profile:
    """Reads the project profile; re-read on every call so edits apply without restart."""
    profile = Profile()
    path = find_profile_path(start)
    if path is not None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            profile.source = str(path)
            profile.allowed_packages = [str(p) for p in data.get("allowed_packages", [])]
            profile.device = data.get("device") or None
            profile.show_system_ui = bool(data.get("show_system_ui", False))
        except (OSError, ValueError) as exc:
            logger.warning("Ignoring unreadable profile %s: %s", path, exc)
    env_packages = os.environ.get("ARTEMIS_ALLOWED_PACKAGES", "")
    profile.allowed_packages += [p.strip() for p in env_packages.split(",") if p.strip()]
    return profile


def select_serial(serial: str | None) -> None:
    global _selected_serial
    _selected_serial = serial or None


def selected_serial() -> str | None:
    """Session choice first, then the project profile; env fallbacks stay in the caller."""
    return _selected_serial or load_profile().device


def is_package_allowed(package: str | None, profile: Profile) -> bool:
    if not profile.allowed_packages:
        return True
    return package in profile.allowed_packages or package in ALWAYS_ALLOWED_PACKAGES


def guard_message(package: str | None, profile: Profile) -> str:
    return (
        f"Error: refused, foreground app is '{package or 'unknown'}', allowed:"
        f" {', '.join(profile.allowed_packages)} (profile: {profile.source or 'env'})."
        " Launch an allowed app first."
    )


def package_refused_message(package: str, profile: Profile) -> str:
    return (
        f"Error: refused, '{package}' is not in allowed_packages:"
        f" {', '.join(profile.allowed_packages)} (profile: {profile.source or 'env'})."
    )


def _center(node: dict[str, Any]) -> tuple[int, int] | None:
    b = node.get("parsed_bounds")
    if not b:
        return None
    return (b["left"] + b["right"]) // 2, (b["top"] + b["bottom"]) // 2


def _contains(node: dict[str, Any], point: tuple[int, int]) -> bool:
    b = node["parsed_bounds"]
    return b["left"] <= point[0] <= b["right"] and b["top"] <= point[1] <= b["bottom"]


def _label(node: dict[str, Any]) -> str:
    text = node.get("text") or node.get("content-desc") or node.get("accessibilityText") or ""
    text = " ".join(str(text).split())
    if len(text) > _MAX_LABEL_CHARS:
        text = text[: _MAX_LABEL_CHARS - 1] + "…"
    return text


def short_id(node: dict[str, Any]) -> str:
    """resource-id without the ``package:id/`` prefix; a React Native testID as is."""
    return str(node.get("resource-id") or "").split(":id/", 1)[-1]


def _flags(node: dict[str, Any], is_input: bool) -> list[str]:
    flags = []
    if node.get("clickable") == "true":
        flags.append("tap")
    if node.get("long-clickable") == "true" and not is_input:
        flags.append("long")
    if is_input:
        flags.append("input")
    if node.get("scrollable") == "true":
        flags.append("scroll")
    if node.get("checkable") == "true":
        flags.append("checked" if node.get("checked") == "true" else "unchecked")
    if node.get("selected") == "true":
        flags.append("selected")
    if node.get("focused") == "true":
        flags.append("focused")
    if node.get("password") == "true":
        flags.append("password")
    if node.get("enabled") == "false":
        flags.append("disabled")
    return flags


def compact_hierarchy(
    elements: list[dict[str, Any]],
    foreground: str | None = None,
    show_system_ui: bool = False,
    hidden_packages: set[str] | None = None,
) -> str:
    """One line per meaningful element: ``[x,y] Class "label" hint=… id=… flags``.

    Text nodes that only repeat the label of an enclosing interactive element are
    dropped, as are status-bar nodes and unlabeled static containers, unless
    they carry a React Native testID (a resource-id without ``:id/``).
    """
    lines = [f"app: {foreground or 'unknown'}"]
    interactive: list[tuple[dict[str, Any], str]] = []
    for node in elements:
        if node.get("visible-to-user") == "false":
            continue
        if not show_system_ui and node.get("package") in SYSTEM_UI_PACKAGES:
            continue
        if hidden_packages and node.get("package") in hidden_packages:
            continue
        center = _center(node)
        if center is None:
            continue
        class_name = (node.get("class") or "View").rsplit(".", 1)[-1] or "View"
        is_input = "EditText" in class_name
        acts = (
            is_input
            or node.get("clickable") == "true"
            or node.get("long-clickable") == "true"
            or node.get("scrollable") == "true"
        )
        label = _label(node)
        hint = " ".join(str(node.get("hint") or "").split())
        rid = short_id(node)
        test_id = bool(rid) and ":id/" not in str(node.get("resource-id"))
        if not label and not acts and not test_id:
            continue
        if not acts and any(
            label in parent_label and _contains(parent, center)
            for parent, parent_label in interactive
        ):
            continue
        if acts:
            interactive.append((node, label))
        line = f"[{center[0]},{center[1]}] {class_name}"
        if label:
            line += f' "{label}"'
        if hint and hint != label:
            line += f' hint="{hint}"'
        if rid:
            line += f" id={rid}"
        flags = _flags(node, is_input)
        if flags:
            line += " " + " ".join(flags)
        lines.append(line)
    return "\n".join(lines)
