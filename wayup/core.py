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
import time
from typing import Any

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

    def _hidden_packages(self) -> set[str]:
        if not self._ime_checked:
            self._ime_checked = True
            try:
                device = self.controller.ctx.adb_client.device(
                    serial=self.controller.ctx.device.device_id
                )
                ime = str(device.shell("settings get secure default_input_method")).strip()
                self._ime_package = ime.split("/")[0] or None
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.debug("IME lookup failed: %s", exc)
        return {self._ime_package} if self._ime_package else set()

    async def elements(self) -> list[dict[str, Any]]:
        controller = self.controller
        ui = controller.ctx.ui_adb_client
        try:
            xml = await asyncio.to_thread(ui.get_hierarchy)
            return parse_hierarchy_xml_to_elements(xml)
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

    async def scroll(self, direction: str) -> None:
        """Scrolls the content: 'down' reveals what is below, like a finger moving up."""
        device = self.controller.ctx.device
        width = device.device_width or 1080
        height = device.device_height or 2400
        cx, top, bottom = width // 2, int(height * 0.3), int(height * 0.7)
        left, right = int(width * 0.2), int(width * 0.8)
        moves = {
            "down": (cx, bottom, cx, top),
            "up": (cx, top, cx, bottom),
            "right": (right, height // 2, left, height // 2),
            "left": (left, height // 2, right, height // 2),
        }
        if direction not in moves:
            raise DeviceError(f"direction must be one of {', '.join(moves)}")
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

    # ------------------------------------------------------------------ by text

    async def find(self, text: str, nth: int = 1, field: bool = False) -> tuple[int, int]:
        screen = await self.screen()
        match = find_by_text(screen.elements, text, nth=nth, field=field)
        if match is None:
            raise DeviceError(f"no element matching {text!r} on screen:\n{screen.compact}")
        return match

    async def tap_text(self, text: str, nth: int = 1) -> None:
        x, y = await self.find(text, nth=nth)
        await self.tap(x, y)

    async def input_into(self, field: str, text: str, clear: bool = True) -> None:
        x, y = await self.find(field, field=True)
        await self.input_text(x, y, text, clear=clear)

    async def wait_for(self, text: str, timeout_ms: int = 5000, gone: bool = False) -> Screen:
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            screen = await self.screen()
            present = find_by_text(screen.elements, text) is not None
            if present != gone:
                return screen
            if time.monotonic() >= deadline:
                state = "still on" if gone else "not on"
                raise DeviceError(
                    f"{text!r} {state} screen after {timeout_ms} ms:\n{screen.compact}"
                )
            await asyncio.sleep(0.15)


def _bounds(node: dict[str, Any]) -> dict[str, int] | None:
    return node.get("parsed_bounds")


def find_element_at(elements: list[dict[str, Any]], x: int, y: int) -> dict[str, Any] | None:
    """Smallest element whose bounds contain the point."""
    best, best_area = None, None
    for node in elements:
        b = _bounds(node)
        if not b or not (b["left"] <= x <= b["right"] and b["top"] <= y <= b["bottom"]):
            continue
        area = (b["right"] - b["left"]) * (b["bottom"] - b["top"])
        if best_area is None or area < best_area:
            best, best_area = node, area
    return best


def find_by_text(
    elements: list[dict[str, Any]], text: str, nth: int = 1, field: bool = False
) -> tuple[int, int] | None:
    """Center of the element whose label or hint matches ``text``.

    Exact matches win over substring ones, interactive elements over static
    text; ``field`` looks only at text inputs. ``nth`` picks among equals, from 1.
    """
    wanted = " ".join(text.split()).casefold()
    tiers: dict[int, list[tuple[int, int]]] = {}
    for node in elements:
        if node.get("visible-to-user") == "false" or not _bounds(node):
            continue
        is_input = "EditText" in (node.get("class") or "")
        if field and not is_input:
            continue
        labels = [
            node.get("text") or "",
            node.get("content-desc") or "",
            node.get("hint") or "",
        ]
        labels = [" ".join(str(label).split()).casefold() for label in labels if label]
        if not labels:
            continue
        exact = any(label == wanted for label in labels)
        if not exact and not any(wanted in label for label in labels):
            continue
        interactive = is_input or node.get("clickable") == "true"
        tier = (0 if exact else 2) + (0 if interactive else 1)
        b = _bounds(node)
        tiers.setdefault(tier, []).append(
            ((b["left"] + b["right"]) // 2, (b["top"] + b["bottom"]) // 2)
        )
    if not tiers:
        return None
    best = tiers[min(tiers)]
    return best[nth - 1] if len(best) >= nth else None
