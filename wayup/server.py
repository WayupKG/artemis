"""wayup MCP server: upstream's stdio ADB server, made fast and Claude Code friendly.

Upstream code stays untouched: this module has its own FastMCP object that
re-implements every upstream adb tool on the fast ``wayup.core`` layer (same
names and parameters, plus ``observe``) and adds new ones. Upstream's
controller setup, drivers and stdio configuration are reused as is.

Run: ``python -m wayup.server``.
"""

import json
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from adbutils import AdbClient
from mcp.server.fastmcp import Context, FastMCP, Image

from artemis.mcp import adb_server
from wayup import profile as profiles
from wayup.core import Device, DeviceError
from wayup.scenario import run_paths

INSTRUCTIONS = """\
Android phone control over adb.
- Start with current_app: it shows the foreground app, the device and whether
  input is allowed by the project's .artemis.json.
- Action tools return the settled screen after the action, so there is no need
  to call get_ui_hierarchy after them. Screen lines are
  '[x,y] Class "label" flags'; [x,y] is the element center.
- Prefer tap_text / input_into (by visible label or hint) over coordinates.
- Once a flow works, save it as a YAML scenario and replay it with run_scenario;
  replay needs no reasoning per step and is much faster.
- foreground null usually means the screen is locked: ask the user to unlock.
"""

mcp = FastMCP("Android_ADB_Controller_wayup", instructions=INSTRUCTIONS)

device = Device()


def _tool() -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """``mcp.tool()`` with upstream's Python-version-independent description."""

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        return mcp.tool(description=adb_server._tool_description(fn))(fn)  # pylint: disable=protected-access

    return decorator


async def _act(action: Callable[[], Awaitable[None]], observe: bool) -> str:
    started = time.perf_counter()
    try:
        await action()
        if not observe:
            return f"ok · {int((time.perf_counter() - started) * 1000)} ms"
        screen = await device.settled_screen()
        return f"ok · {int((time.perf_counter() - started) * 1000)} ms\n{screen.compact}"
    except DeviceError as exc:
        return f"Error: {exc}"
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return f"Error: {type(exc).__name__}: {exc}"


def _pair(coordinates: list[int]) -> tuple[int, int]:
    if len(coordinates) != 2:
        raise DeviceError("coordinates must be [x, y]")
    return coordinates[0], coordinates[1]


# ---------------------------------------------------------------------- reading


@_tool()
async def get_ui_hierarchy(ctx: Context, full: bool = False, keyboard: bool = False) -> str:
    """Returns the current screen, one element per line.

    Format: '[x,y] Class "label" hint="..." flags'. [x,y] is the element center
    for tap/input tools; flags: tap, long, input, scroll, checked, unchecked,
    selected, focused, password, disabled. The first line names the foreground
    app. Text repeating an enclosing button, the status bar and the on-screen
    keyboard are omitted ('keyboard' keeps the keyboard). 'full' returns the raw
    JSON element list.
    """
    if full:
        return json.dumps(await device.elements(), ensure_ascii=False)
    return (await device.screen(show_keyboard=keyboard)).compact


@_tool()
async def take_screenshot(ctx: Context, save_path: str | None = None) -> Image:
    """Takes a screenshot and returns it as an image.

    Downscaled to ARTEMIS_SCREENSHOT_MAX_SIDE pixels (default 1280). 'save_path'
    also writes the full-resolution PNG there, e.g. to attach it to a bug report.
    """
    max_side = int(os.environ.get("ARTEMIS_SCREENSHOT_MAX_SIDE", "1280"))
    return Image(data=await device.screenshot_jpeg(max_side, save_path), format="jpeg")


@_tool()
async def current_app(ctx: Context) -> str:
    """Reports the foreground app, the active device and the project profile."""
    try:
        controller = device.controller
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return f"Error: {exc}"
    profile = profiles.load_profile()
    package = device.foreground()
    return json.dumps(
        {
            "foreground": package,
            "device": controller.ctx.device.device_id,
            "input_allowed": profiles.is_package_allowed(package, profile),
            "allowed_packages": profile.allowed_packages,
            "profile": profile.source,
        },
        ensure_ascii=False,
    )


@_tool()
async def wait_for(ctx: Context, text: str, timeout_ms: int = 5000, gone: bool = False) -> str:
    """Waits until an element with this label appears ('gone': disappears).

    Returns the screen at that moment, or an error with the last screen.
    """
    try:
        return (await device.wait_for(text, timeout_ms=timeout_ms, gone=gone)).compact
    except DeviceError as exc:
        return f"Error: {exc}"


# ---------------------------------------------------------------------- actions


@_tool()
async def tap(
    ctx: Context, coordinates: list[int], times: int = 1, delay_ms: int = 100, observe: bool = True
) -> str:
    """Taps at [x, y]; returns the settled screen unless 'observe' is false.

    'times' consecutive taps with 'delay_ms' between them.
    """

    async def action() -> None:
        await device.tap(*_pair(coordinates), times=times, delay_ms=delay_ms)

    return await _act(action, observe)


@_tool()
async def tap_text(ctx: Context, text: str, nth: int = 1, observe: bool = True) -> str:
    """Taps the element whose label or hint matches 'text' (case-insensitive).

    Exact matches win over substrings, buttons over plain text; 'nth' picks the
    n-th of equal matches, from 1. Returns the settled screen.
    """
    return await _act(lambda: device.tap_text(text, nth=nth), observe)


@_tool()
async def long_press_on(
    ctx: Context, coordinates: list[int], duration: int = 1000, observe: bool = True
) -> str:
    """Long presses at [x, y] for 'duration' ms; returns the settled screen."""
    return await _act(lambda: device.long_press(*_pair(coordinates), duration=duration), observe)


@_tool()
async def swipe(
    ctx: Context, coordinates: list[int], duration: int = 400, observe: bool = True
) -> str:
    """Swipes [start_x, start_y, end_x, end_y]; returns the settled screen.

    Set duration >= 1000 to drag-and-drop.
    """
    if len(coordinates) != 4:
        return "Error: coordinates must be [start_x, start_y, end_x, end_y]"
    return await _act(lambda: device.swipe(*coordinates, duration=duration), observe)


@_tool()
async def scroll(ctx: Context, direction: str = "down", observe: bool = True) -> str:
    """Scrolls the content: 'down' shows what is below; also up, left, right."""
    return await _act(lambda: device.scroll(direction), observe)


@_tool()
async def back(ctx: Context, observe: bool = True) -> str:
    """Presses the system back button; returns the settled screen."""
    return await _act(device.back, observe)


@_tool()
async def press_key(ctx: Context, keycode: str, observe: bool = True) -> str:
    """Presses an Android key (e.g. KEYCODE_ENTER, KEYCODE_HOME)."""
    return await _act(lambda: device.press_key(keycode), observe)


@_tool()
async def launch_app(ctx: Context, package_name: str, observe: bool = True) -> str:
    """Launches an app by package name (must be allowed by the project profile)."""
    return await _act(lambda: device.launch(package_name), observe)


@_tool()
async def stop_app(ctx: Context, package_name: str) -> str:
    """Force stops an app by package name (must be allowed by the project profile)."""
    return await _act(lambda: device.stop(package_name), observe=False)


@_tool()
async def open_link(ctx: Context, url: str, observe: bool = True) -> str:
    """Opens a URL or deep link on the device; returns the settled screen."""
    return await _act(lambda: device.open_link(url), observe)


@_tool()
async def focus_and_input_text(
    ctx: Context,
    coordinates: list[int],
    text: str,
    clear_before_input: bool = False,
    observe: bool = True,
) -> str:
    """Focuses the field at [x, y] and types 'text' (Cyrillic and '\\n' work).

    'clear_before_input' replaces the content; otherwise text is appended.
    """
    return await _act(
        lambda: device.input_text(*_pair(coordinates), text, clear=clear_before_input), observe
    )


@_tool()
async def input_into(
    ctx: Context, field: str, text: str, clear: bool = True, observe: bool = True
) -> str:
    """Types 'text' into the input whose label or hint matches 'field'.

    Replaces the content unless 'clear' is false. Returns the settled screen.
    """
    return await _act(lambda: device.input_into(field, text, clear=clear), observe)


@_tool()
async def focus_and_clear_text(ctx: Context, coordinates: list[int], observe: bool = True) -> str:
    """Focuses the field at [x, y] and clears it."""
    return await _act(lambda: device.clear_text(*_pair(coordinates)), observe)


@_tool()
async def erase_one_char(ctx: Context) -> str:
    """Erases a single character (Backspace)."""
    return await _act(device.erase_one_char, observe=False)


# ---------------------------------------------------------------------- devices


@_tool()
async def list_devices(ctx: Context) -> str:
    """Lists Android devices connected to adb with their model names."""
    try:
        adb = AdbClient(
            host=os.environ.get("ADB_HOST", "localhost"),
            port=int(os.environ.get("ADB_PORT", "5037")),
        )
        devices = []
        for item in adb.device_list():
            try:
                model = str(item.shell("getprop ro.product.model")).strip()
            except Exception:  # pylint: disable=broad-exception-caught
                model = ""
            devices.append({"serial": item.serial, "model": model})
        return json.dumps(
            {"devices": devices, "selected": profiles.selected_serial()}, ensure_ascii=False
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return f"Error: {exc}"


@_tool()
async def select_device(ctx: Context, serial: str) -> str:
    """Switches this session to the device with this adb serial (see list_devices)."""
    profiles.select_serial(serial)
    try:
        return f"ok: using {device.controller.ctx.device.device_id}"
    except Exception as exc:  # pylint: disable=broad-exception-caught
        profiles.select_serial(None)
        return f"Error: {exc}"


# ---------------------------------------------------------------------- scenarios


@_tool()
async def run_scenario(ctx: Context, paths: list[str]) -> str:
    """Replays YAML scenarios (files or directories) and reports each step.

    Paths are relative to the project directory. Failures include the screen
    and a screenshot path under .artemis/runs/. See wayup/scenario.py for the
    step format (launch, tap, input, expect, expect_not, wait, scroll, key,
    back, open_link, screenshot, sleep_ms, stop).
    """
    missing = [p for p in paths if not Path(p).expanduser().exists()]
    if missing:
        return f"Error: not found: {', '.join(missing)} (cwd {Path.cwd()})"
    results = await run_paths(paths)
    passed = sum(r.ok for r in results)
    reports = "\n\n".join(r.report() for r in results)
    return f"{passed}/{len(results)} scenarios passed\n\n{reports}"


if __name__ == "__main__":
    from artemis.runtime import shutdown_awake_service, start_awake_service

    adb_server.configure_stdio_mode()
    start_awake_service()
    try:
        mcp.run(transport="stdio")
    finally:
        shutdown_awake_service()
