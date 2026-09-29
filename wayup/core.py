"""Fast device layer shared by the wayup MCP server and the scenario runner.

Upstream tools fetch a screenshot *and* the hierarchy on every call and sleep
before it (~1 s). Here the hierarchy (~120 ms) and the screenshot (~400 ms) are
read separately, and after an action we poll until the screen stops changing
instead of sleeping a fixed time.
"""

import asyncio
from dataclasses import dataclass
import io
import logging
import os
import re
import time
from typing import Any
import xml.etree.ElementTree as ET

from PIL import Image as PILImage

from artemis.mcp import adb_server
from third_party.mobile_use.clients.ui_automator_client import parse_hierarchy_xml_to_elements
from third_party.mobile_use.controllers.platform_specific_commands_controller import (
    get_current_foreground_package,
)
from third_party.mobile_use.utils.app_launch_utils import launch_app_with_retries
from wayup import profile as profiles

logger = logging.getLogger(__name__)

SETTLE_FIRST_DELAY_S = 0.1
SETTLE_POLL_S = 0.05
SETTLE_TIMEOUT_S = 2.5

# Tapping by text waits this long for the element to show up.
FIND_TIMEOUT_MS = 3000
FIND_POLL_S = 0.1

# After a launch the app counts as ready once its screen has been still this long.
LAUNCH_QUIET_S = 2.0
LAUNCH_READY_TIMEOUT_S = 15.0

KEYBOARD_HIDE_TIMEOUT_S = 1.5

SCROLL_MAX_SWIPES = 10
DIRECTIONS = ("down", "up", "right", "left")

# Tap points tried inside a partly covered element: a TAP_GRID x TAP_GRID grid.
TAP_GRID = 9
# In-app views bigger than this share of the screen do not count as covering:
# they are usually pass-through hosts for overlays, not the overlays themselves.
OVERLAY_MAX_SCREEN_SHARE = 0.9

# Element state that expect/wait can require: {"selected": True, ...}.
STATE_FLAGS = ("selected", "checked", "enabled", "focused")

# logcat -v threadtime: date time pid tid level tag: message
LOG_LINE = re.compile(
    r"^\d\d-\d\d \d\d:\d\d:\d\d\.\d+\s+(\d+)\s+\d+\s+([VDIWEF])\s+(.+?)\s*: (.*)$"
)


@dataclass
class LogEntry:
    level: str
    tag: str
    message: str
    # "js" (JS console), "native" (React Native Java/C++), "crash" (AndroidRuntime)
    # or "app" (other error lines of the process, often vendor noise)
    kind: str = "app"
    count: int = 1

    def __str__(self) -> str:
        repeat = f" (x{self.count})" if self.count > 1 else ""
        return f"{self.level} {self.tag}: {self.message}{repeat}"


class DeviceError(Exception):
    """An action was refused or failed; the message is meant for the model."""


@dataclass
class Screen:
    foreground: str | None
    elements: list[dict[str, Any]]
    compact: str


def _apply_device() -> None:
    # Upstream resolves the device from ARTEMIS_DEVICE_ID first and caches
    # controllers per serial, so steering the env var is enough to switch.
    serial = profiles.selected_serial()
    if serial:
        os.environ["ARTEMIS_DEVICE_ID"] = serial


class Device:
    def __init__(self) -> None:
        self._ime_package: str | None = None
        self._ime_checked = False

    @property
    def controller(self) -> Any:
        _apply_device()
        return adb_server._get_controller()  # pylint: disable=protected-access

    # ------------------------------------------------------------------ reading

    def foreground(self) -> str | None:
        return get_current_foreground_package(self.controller.ctx)

    def _shell(self, command: str) -> str:
        ctx = self.controller.ctx
        return str(ctx.adb_client.device(serial=ctx.device.device_id).shell(command))

    def _hidden_packages(self) -> set[str]:
        if not self._ime_checked:
            self._ime_checked = True
            try:
                ime = self._shell("settings get secure default_input_method").strip()
                self._ime_package = ime.split("/")[0] or None
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.debug("IME lookup failed: %s", exc)
        return {self._ime_package} if self._ime_package else set()

    async def elements(self) -> list[dict[str, Any]]:
        controller = self.controller
        ui = controller.ctx.ui_adb_client
        try:
            xml = await asyncio.to_thread(ui.get_hierarchy)
            elements = parse_hierarchy_xml_to_elements(xml)
            annotate_subtrees(xml, elements)
            return elements
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning("Fast hierarchy failed, using upstream path: %s", exc)
            return await controller.get_ui_elements()

    async def screen(self, show_keyboard: bool = False) -> Screen:
        profile = profiles.load_profile()
        elements, foreground = await asyncio.gather(
            self.elements(), asyncio.to_thread(self.foreground)
        )
        hidden = set() if show_keyboard else self._hidden_packages()
        compact = profiles.compact_hierarchy(
            elements,
            foreground=foreground,
            show_system_ui=profile.show_system_ui,
            hidden_packages=hidden,
        )
        return Screen(foreground, elements, compact)

    async def settled_screen(self) -> Screen:
        """Polls the hierarchy until two consecutive reads match."""
        await asyncio.sleep(SETTLE_FIRST_DELAY_S)
        deadline = time.monotonic() + SETTLE_TIMEOUT_S
        previous = await self.screen()
        while time.monotonic() < deadline:
            await asyncio.sleep(SETTLE_POLL_S)
            current = await self.screen()
            if current.compact == previous.compact:
                return current
            previous = current
        return previous

    async def screenshot_png(self) -> bytes:
        ui = self.controller.ctx.ui_adb_client
        picture = await asyncio.to_thread(ui.get_screenshot)
        if picture is None:
            raise DeviceError("screenshot capture failed")
        buffer = io.BytesIO()
        picture.save(buffer, format="PNG")
        return buffer.getvalue()

    async def screenshot_jpeg(self, max_side: int, save_path: str | None = None) -> bytes:
        png = await self.screenshot_png()
        if save_path:
            target = os.path.expanduser(save_path)
            os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
            with open(target, "wb") as fh:
                fh.write(png)
        picture = PILImage.open(io.BytesIO(png)).convert("RGB")
        picture.thumbnail((max_side, max_side))
        buffer = io.BytesIO()
        picture.save(buffer, format="JPEG", quality=75)
        return buffer.getvalue()

    # ------------------------------------------------------------------ guard

    def guard(self) -> Any:
        """Controller for an input action, refused unless an allowed app is on top."""
        controller = self.controller
        profile = profiles.load_profile()
        if profile.allowed_packages:
            package = get_current_foreground_package(controller.ctx)
            if not profiles.is_package_allowed(package, profile):
                raise DeviceError(profiles.guard_message(package, profile))
        return controller

    @staticmethod
    def guard_package(package: str) -> None:
        profile = profiles.load_profile()
        if not profiles.is_package_allowed(package, profile):
            raise DeviceError(profiles.package_refused_message(package, profile))

    # ------------------------------------------------------------------ actions

    async def tap(self, x: int, y: int, times: int = 1, delay_ms: int = 100) -> None:
        result = await self.guard().tap_at(x=x, y=y, times=times, delay_ms=delay_ms)
        if result.error:
            raise DeviceError(result.error)

    async def long_press(self, x: int, y: int, duration: int = 1000) -> None:
        result = await self.guard().tap_at(x=x, y=y, long_press=True, long_press_duration=duration)
        if result.error:
            raise DeviceError(result.error)

    async def swipe(self, x1: int, y1: int, x2: int, y2: int, duration: int = 400) -> None:
        error = await self.guard().swipe_coords(
            start_x=x1, start_y=y1, end_x=x2, end_y=y2, duration=duration
        )
        if error:
            raise DeviceError(error)

    async def scroll(
        self,
        direction: str,
        within: str | None = None,
        until: str | None = None,
        max_swipes: int = SCROLL_MAX_SWIPES,
    ) -> None:
        """Scrolls the content: 'down' reveals what is below, like a finger moving up.

        ``within`` swipes inside the scrollable container around the element with
        this label (a horizontal chip bar, a list in a sheet) instead of the
        middle of the screen. ``until`` keeps swiping until an element with this
        label can be tapped, and fails at the end of the content or after
        ``max_swipes``.
        """
        if direction not in DIRECTIONS:
            raise DeviceError(f"direction must be one of {', '.join(DIRECTIONS)}")
        if until is None:
            await self._swipe_region(direction, await self._scroll_region(within))
            return
        screen = await self.screen()
        # Found once: the element named by 'within' may scroll out of view itself.
        region = await self._scroll_region(within, screen)
        for _ in range(max_swipes):
            if _tappable(screen.elements, until):
                return
            await self._swipe_region(direction, region)
            after = await self.settled_screen()
            if after.compact == screen.compact:
                raise DeviceError(
                    f"reached the end scrolling {direction} without {until!r}:\n{after.compact}"
                )
            screen = after
        if not _tappable(screen.elements, until):
            raise DeviceError(
                f"{until!r} not found after {max_swipes} swipes {direction}:\n{screen.compact}"
            )

    async def _scroll_region(
        self, within: str | None, screen: Screen | None = None
    ) -> tuple[int, int, int, int]:
        if within is None:
            device = self.controller.ctx.device
            return 0, 0, device.device_width or 1080, device.device_height or 2400
        screen = screen or await self.screen()
        index = find_node_by_text(screen.elements, within)
        if index is None:
            raise DeviceError(f"no element matching {within!r} on screen:\n{screen.compact}")
        x, y = _center(_bounds(screen.elements[index]))
        container = find_element_at(screen.elements, x, y, scrollable=True)
        if container is None:
            raise DeviceError(f"no scrollable container around {within!r}:\n{screen.compact}")
        b = _bounds(container)
        return b["left"], b["top"], b["right"], b["bottom"]

    async def _swipe_region(self, direction: str, region: tuple[int, int, int, int]) -> None:
        left, top, right, bottom = region
        width, height = right - left, bottom - top
        cx, cy = left + width // 2, top + height // 2
        near_x, far_x = left + int(width * 0.2), left + int(width * 0.8)
        near_y, far_y = top + int(height * 0.3), top + int(height * 0.7)
        moves = {
            "down": (cx, far_y, cx, near_y),
            "up": (cx, near_y, cx, far_y),
            "right": (far_x, cy, near_x, cy),
            "left": (near_x, cy, far_x, cy),
        }
        await self.swipe(*moves[direction], duration=350)

    async def back(self) -> None:
        if not await self.guard().go_back():
            raise DeviceError("back failed")

    async def press_key(self, keycode: str) -> None:
        if not await self.guard().press_key(keycode):
            raise DeviceError(f"key {keycode} failed")

    async def launch(self, package: str) -> None:
        self.guard_package(package)
        success, error = await launch_app_with_retries(self.controller.ctx, package)
        if not success:
            raise DeviceError(error or f"launch of {package} failed")
        await self.wait_until_ready(package)

    async def wait_until_ready(self, package: str) -> None:
        """Waits until ``package`` is on top and its screen has been still for a while.

        The launcher returns as soon as the app shows a window, but a React Native
        app then swaps the splash for its first screen and animates it in, and taps
        in that time are lost. A screen that never calms down (a spinner) is given
        up on after LAUNCH_READY_TIMEOUT_S without an error.
        """
        deadline = time.monotonic() + LAUNCH_READY_TIMEOUT_S
        still_since, previous = time.monotonic(), None
        while time.monotonic() < deadline:
            screen = await self.screen()
            current = screen.compact if screen.foreground == package else None
            if current is None or current != previous:
                still_since, previous = time.monotonic(), current
            elif time.monotonic() - still_since >= LAUNCH_QUIET_S:
                return
            await asyncio.sleep(SETTLE_POLL_S)
        logger.info("%s did not settle within %s s after launch", package, LAUNCH_READY_TIMEOUT_S)

    async def stop(self, package: str) -> None:
        self.guard_package(package)
        if not await self.controller.terminate_app(package):
            raise DeviceError(f"stop of {package} failed")

    async def open_link(self, url: str) -> None:
        if not await self.controller.open_url(url):
            raise DeviceError(f"opening {url} failed")

    async def _focus(self, x: int, y: int) -> None:
        elements = await self.elements()
        element = find_element_at(elements, x, y)
        if element is not None and element.get("focused") == "true":
            return
        await self.tap(x, y)
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            await asyncio.sleep(0.1)
            element = find_element_at(await self.elements(), x, y)
            if element is not None and element.get("focused") == "true":
                return

    async def input_text(self, x: int, y: int, text: str, clear: bool) -> None:
        controller = self.guard()
        await self._focus(x, y)
        if clear:
            if not await controller.erase_text():
                raise DeviceError("clearing the field failed")
        else:
            await controller.press_key("123")  # KEYCODE_MOVE_END: append at the end
        if not await controller.type_text(text, clear_existing=False):
            raise DeviceError("typing failed")

    async def clear_text(self, x: int, y: int) -> None:
        controller = self.guard()
        await self._focus(x, y)
        if not await controller.erase_text():
            raise DeviceError("clearing the field failed")

    async def erase_one_char(self) -> None:
        if not await self.guard().erase_text(nb_chars=1):
            raise DeviceError("erase failed")

    async def log_mark(self, seconds_ago: int = 0) -> str:
        """Device clock in the form ``logcat -T`` takes, to read the log since then."""
        now = await asyncio.to_thread(self._shell, "date +%s")
        return f"{int(now.strip()) - seconds_ago}.000"

    async def app_log(self, since: str, package: str | None = None) -> list[LogEntry]:
        """Warnings and errors the app logged since ``since`` (see filter_app_log)."""
        raw = await asyncio.to_thread(self._shell, f"logcat -d -v threadtime -T {since}")
        pids: set[str] = set()
        if package:
            pids.update((await asyncio.to_thread(self._shell, f"pidof {package}")).split())
        return filter_app_log(raw.splitlines(), package, pids)

    async def keyboard_shown(self) -> bool:
        output = await asyncio.to_thread(self._shell, "dumpsys input_method | grep mInputShown")
        return "mInputShown=true" in output

    async def hide_keyboard(self) -> bool:
        """Closes the on-screen keyboard; returns False if it was not shown.

        Back is pressed only while the keyboard is up, so this never navigates away.
        """
        if not await self.keyboard_shown():
            return False
        await self.press_key("KEYCODE_BACK")
        deadline = time.monotonic() + KEYBOARD_HIDE_TIMEOUT_S
        while time.monotonic() < deadline:
            await asyncio.sleep(0.1)
            if not await self.keyboard_shown():
                return True
        raise DeviceError("the keyboard is still shown after Back")

    # ------------------------------------------------------------------ by text

    async def find(
        self,
        text: str,
        nth: int = 1,
        field: bool = False,
        timeout_ms: int = FIND_TIMEOUT_MS,
        by_id: bool = False,
    ) -> tuple[int, int]:
        """Point to tap on the matching element, waiting up to ``timeout_ms`` for it.

        The point is the element's center, or its uncovered spot nearest to the
        center when a tab bar, the keyboard or another overlay is drawn over it.
        A match on the first read is returned at once. One that shows up later
        means the screen is still changing, so it is returned only when two reads
        in a row agree on the point: a sheet sliding in is not tapped mid-way.
        ``by_id`` looks ``text`` up as a resource-id (a React Native testID).
        """
        deadline = time.monotonic() + timeout_ms / 1000
        screen = await self.screen()
        point, cover = locate(screen.elements, text, nth=nth, field=field, by_id=by_id)
        if point is not None:
            return point
        while time.monotonic() < deadline:
            await asyncio.sleep(FIND_POLL_S)
            screen = await self.screen()
            previous = point
            point, cover = locate(screen.elements, text, nth=nth, field=field, by_id=by_id)
            if point is not None and point == previous:
                return point
        if point is not None:
            return point
        what = _what(text, by_id)
        if cover is not None:
            raise DeviceError(
                f"{what} is covered by {cover}; scroll it into view or close what covers it:\n"
                f"{screen.compact}"
            )
        raise DeviceError(f"no {what} on screen after {timeout_ms} ms:\n{screen.compact}")

    async def tap_text(
        self, text: str, nth: int = 1, timeout_ms: int = FIND_TIMEOUT_MS, by_id: bool = False
    ) -> None:
        x, y = await self.find(text, nth=nth, timeout_ms=timeout_ms, by_id=by_id)
        await self.tap(x, y)

    async def input_into(
        self,
        field: str,
        text: str,
        clear: bool = True,
        timeout_ms: int = FIND_TIMEOUT_MS,
        by_id: bool = False,
    ) -> None:
        x, y = await self.find(field, field=True, timeout_ms=timeout_ms, by_id=by_id)
        await self.input_text(x, y, text, clear=clear)

    async def wait_for(
        self,
        text: str,
        timeout_ms: int = 5000,
        gone: bool = False,
        exact: bool = False,
        by_id: bool = False,
        state: dict[str, bool] | None = None,
    ) -> Screen:
        """Waits for an element (in ``state``, e.g. {"selected": True}) to appear or go."""
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            screen = await self.screen()
            index = find_node_by_text(screen.elements, text, exact=exact, by_id=by_id, state=state)
            if (index is not None) != gone:
                return screen
            if time.monotonic() >= deadline:
                what = _what(text, by_id)
                if gone:
                    problem = f"{what} still on screen"
                elif (
                    state
                    and find_node_by_text(screen.elements, text, exact=exact, by_id=by_id)
                    is not None
                ):
                    problem = f"{what} is on screen, but not {describe_state(state)}"
                else:
                    problem = f"{what} not on screen"
                raise DeviceError(f"{problem} after {timeout_ms} ms:\n{screen.compact}")
            await asyncio.sleep(0.15)


def annotate_subtrees(xml: str, elements: list[dict[str, Any]]) -> None:
    """Stores in every element the index just past its last descendant.

    Upstream's parser flattens the XML in pre-order, one element per node, so
    the same walk gives each node's extent; app nodes after it are drawn over
    it. Skipped when the counts disagree: taps then go to the center as before.
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return
    ends: list[int] = []

    def walk(node: ET.Element) -> None:
        index = len(ends)
        ends.append(0)
        for child in node:
            walk(child)
        ends[index] = len(ends)

    walk(root)
    if len(ends) != len(elements):
        return
    for node, end in zip(elements, ends, strict=True):
        node["subtree_end"] = end


def _bounds(node: dict[str, Any]) -> dict[str, int] | None:
    return node.get("parsed_bounds")


def _center(b: dict[str, int]) -> tuple[int, int]:
    return (b["left"] + b["right"]) // 2, (b["top"] + b["bottom"]) // 2


def _area(b: dict[str, int]) -> int:
    return max(0, b["right"] - b["left"]) * max(0, b["bottom"] - b["top"])


def _inside(x: int, y: int, b: dict[str, int]) -> bool:
    return b["left"] <= x < b["right"] and b["top"] <= y < b["bottom"]


def _overlaps(a: dict[str, int], b: dict[str, int]) -> bool:
    return (
        a["left"] < b["right"]
        and b["left"] < a["right"]
        and a["top"] < b["bottom"]
        and b["top"] < a["bottom"]
    )


def _label(node: dict[str, Any]) -> str:
    return repr(node.get("text") or node.get("content-desc") or node.get("class") or "?")


def find_element_at(
    elements: list[dict[str, Any]], x: int, y: int, scrollable: bool = False
) -> dict[str, Any] | None:
    """Smallest element whose bounds contain the point (only scrollable ones if asked)."""
    best, best_area = None, None
    for node in elements:
        b = _bounds(node)
        if not b or not (b["left"] <= x <= b["right"] and b["top"] <= y <= b["bottom"]):
            continue
        if scrollable and node.get("scrollable") != "true":
            continue
        area = _area(b)
        if best_area is None or area < best_area:
            best, best_area = node, area
    return best


def covering(elements: list[dict[str, Any]], index: int) -> list[dict[str, Any]]:
    """Elements drawn over ``elements[index]`` that would take a tap meant for it.

    Other windows (keyboard, status bar) always count. Within the app, nodes
    after the target's subtree are drawn on top of it: clickable ones take the
    tap, and in React Native so does any view under the finger, except near
    full-screen containers that only host overlays. Without subtree data
    (fallback hierarchy) only other windows are known.
    """
    target = elements[index]
    tb = _bounds(target)
    if not tb:
        return []
    end = target.get("subtree_end")
    package = target.get("package")
    screen_area = max((_area(b) for b in map(_bounds, elements) if b), default=0)
    covers = []
    for j, node in enumerate(elements):
        b = _bounds(node)
        if j == index or not b or node.get("visible-to-user") == "false" or not _overlaps(b, tb):
            continue
        other = node.get("package")
        if other and package and other != package:
            covers.append(node)
        elif (
            end is not None
            and j >= end
            and (
                node.get("clickable") == "true"
                or node.get("long-clickable") == "true"
                or _area(b) < OVERLAY_MAX_SCREEN_SHARE * screen_area
            )
        ):
            covers.append(node)
    return covers


def visible_point(elements: list[dict[str, Any]], index: int) -> tuple[int, int] | None:
    """Uncovered point of the element nearest to its center; None if fully covered."""
    b = _bounds(elements[index])
    if not b:
        return None
    covers = [_bounds(node) for node in covering(elements, index)]
    cx, cy = _center(b)
    if not any(_inside(cx, cy, c) for c in covers):
        return cx, cy
    width, height = b["right"] - b["left"], b["bottom"] - b["top"]
    points = [
        (
            b["left"] + width * (i + 1) // (TAP_GRID + 1),
            b["top"] + height * (k + 1) // (TAP_GRID + 1),
        )
        for i in range(TAP_GRID)
        for k in range(TAP_GRID)
    ]
    points.sort(key=lambda p: (p[0] - cx) ** 2 + (p[1] - cy) ** 2)
    for x, y in points:
        if not any(_inside(x, y, c) for c in covers):
            return x, y
    return None


def _what(text: str, by_id: bool) -> str:
    return f"element with id {text!r}" if by_id else f"element matching {text!r}"


def _flag(node: dict[str, Any], flag: str) -> bool:
    """A state flag of the element; a missing 'enabled' means enabled."""
    value = node.get(flag)
    return flag == "enabled" if value is None else value == "true"


def describe_state(state: dict[str, bool]) -> str:
    return ", ".join(flag if wanted else f"not {flag}" for flag, wanted in state.items())


def filter_app_log(
    lines: list[str], package: str | None = None, pids: set[str] | None = None
) -> list[LogEntry]:
    """The app's warnings and errors from ``logcat -v threadtime`` lines.

    Kept: React Native JS warnings and errors (tag ReactNativeJS, kind "js"),
    and error or fatal lines of the app's processes - the given pids, the pids
    that logged JS, and a crashed process that AndroidRuntime names as
    ``package``: React Native native errors (tags ``unknown:*`` and
    ``ReactNative*``, kind "native"), crash reports (AndroidRuntime, kind
    "crash") and the rest (kind "app", often vendor noise). Repeats are merged
    with a count, in order of first appearance.
    """
    parsed = [match.groups() for match in map(LOG_LINE.match, lines) if match]
    app_pids = set(pids or ())
    for pid, _level, tag, message in parsed:
        if tag == "ReactNativeJS" or (
            tag == "AndroidRuntime" and package and message.startswith(f"Process: {package}")
        ):
            app_pids.add(pid)
    entries: dict[tuple[str, str, str], LogEntry] = {}
    for pid, level, tag, message in parsed:
        js = tag == "ReactNativeJS" and level in ("W", "E", "F")
        if not js and not (pid in app_pids and level in ("E", "F")):
            continue
        key = (level, tag, message)
        if key in entries:
            entries[key].count += 1
        else:
            kind = "js" if js else _log_kind(tag)
            entries[key] = LogEntry(level, tag, message, kind)
    return list(entries.values())


def _log_kind(tag: str) -> str:
    if tag == "AndroidRuntime":
        return "crash"
    if tag.startswith(("unknown:", "ReactNative")):
        return "native"
    return "app"


def locate(
    elements: list[dict[str, Any]],
    text: str,
    nth: int = 1,
    field: bool = False,
    by_id: bool = False,
) -> tuple[tuple[int, int] | None, str | None]:
    """Tap point for the matching element, or None and what covers it (None if absent)."""
    index = find_node_by_text(elements, text, nth=nth, field=field, by_id=by_id)
    if index is None:
        return None, None
    point = visible_point(elements, index)
    if point is not None:
        return point, None
    cx, cy = _center(_bounds(elements[index]))
    covers = [n for n in covering(elements, index) if _inside(cx, cy, _bounds(n))]
    return None, _label(covers[-1]) if covers else "another element"


def _tappable(elements: list[dict[str, Any]], text: str) -> bool:
    """An element with this label is on screen and not fully covered."""
    index = find_node_by_text(elements, text)
    return index is not None and visible_point(elements, index) is not None


def find_by_text(
    elements: list[dict[str, Any]],
    text: str,
    nth: int = 1,
    field: bool = False,
    exact: bool = False,
    by_id: bool = False,
) -> tuple[int, int] | None:
    """Center of the element whose label or hint matches ``text`` (see find_node_by_text)."""
    index = find_node_by_text(elements, text, nth=nth, field=field, exact=exact, by_id=by_id)
    return None if index is None else _center(_bounds(elements[index]))


def find_node_by_text(
    elements: list[dict[str, Any]],
    text: str,
    nth: int = 1,
    field: bool = False,
    exact: bool = False,
    by_id: bool = False,
    state: dict[str, bool] | None = None,
) -> int | None:
    """Index of the element whose label or hint matches ``text``.

    Exact matches win over substring ones (``exact`` allows only them),
    interactive elements over static text; ``field`` looks only at text
    inputs. ``nth`` picks among equals, from 1. ``by_id`` matches ``text``
    against the resource-id instead, whole or after ``:id/``. ``state`` keeps
    only elements with these flags, e.g. {"selected": True, "enabled": False}.
    """
    wanted = " ".join(text.split()).casefold()
    tiers: dict[int, list[int]] = {}
    for i, node in enumerate(elements):
        if node.get("visible-to-user") == "false" or not _bounds(node):
            continue
        is_input = "EditText" in (node.get("class") or "")
        if field and not is_input:
            continue
        if state and any(_flag(node, flag) != want for flag, want in state.items()):
            continue
        if by_id:
            if text not in (node.get("resource-id"), profiles.short_id(node)):
                continue
            interactive = is_input or node.get("clickable") == "true"
            tiers.setdefault(0 if interactive else 1, []).append(i)
            continue
        labels = [
            node.get("text") or "",
            node.get("content-desc") or "",
            node.get("hint") or "",
        ]
        labels = [" ".join(str(label).split()).casefold() for label in labels if label]
        if not labels:
            continue
        whole = any(label == wanted for label in labels)
        if not whole and (exact or not any(wanted in label for label in labels)):
            continue
        interactive = is_input or node.get("clickable") == "true"
        tier = (0 if whole else 2) + (0 if interactive else 1)
        tiers.setdefault(tier, []).append(i)
    if not tiers:
        return None
    best = tiers[min(tiers)]
    return best[nth - 1] if len(best) >= nth else None
