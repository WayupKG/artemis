"""Replays YAML phone scenarios without a model: fast regression runs.

A scenario is a list of steps; each step is a one-key mapping::

    name: Overdue tasks from home
    app: com.example.app
    steps:
      - launch: true                  # the scenario app, or a package name;
                                      # returns once the first screen is still
      - tap: Overdue                  # by label/hint, waits up to 3 s for it;
                                      # {text, nth, timeout_ms} or [x, y]; taps
                                      # the uncovered part if a bar overlaps it
      - expect: "Overdue, 7"          # waits up to 3 s; string or list; an item
                                      # can be {text, exact: true}
      - expect_not: Error
      - input: {field: Search tasks…, text: Standup, clear: true}
      - hide_keyboard: true           # Back only if the keyboard is up
      - wait: {text: TASK-1, timeout_ms: 5000, gone: false, exact: false}
      - scroll: down                  # down | up | left | right
      - scroll: {direction: left, within: "Today, 1", until: "All tasks"}
                                      # within: the scrollable container around
                                      # this element; until: swipe until tappable
      - long_press: TASK-3
      - key: KEYCODE_ENTER
      - back: true
      - open_link: example://tasks/TASK-3
      - screenshot: after-open        # saved into the run directory
      - sleep_ms: 300
      - stop: true

An ``expect`` whose texts were all on screen already before the last action
passes without proving that the action worked; the report notes it.

Usage: ``python -m wayup.scenario path/to/file.yaml [more.yaml | dir ...]``.
"""

import argparse
import asyncio
from dataclasses import dataclass, field
import datetime as dt
from pathlib import Path
import sys
import time
from typing import Any

import yaml

from wayup.core import (
    FIND_TIMEOUT_MS,
    SCROLL_MAX_SWIPES,
    Device,
    DeviceError,
    Screen,
    find_node_by_text,
)

DEFAULT_EXPECT_TIMEOUT_MS = 3000
DEFAULT_RUNS_DIR = ".artemis/runs"


@dataclass
class StepResult:
    index: int
    step: str
    ok: bool
    ms: int
    error: str | None = None
    note: str | None = None


@dataclass
class ScenarioResult:
    name: str
    path: str
    steps: list[StepResult] = field(default_factory=list)
    screen: str | None = None
    artifacts: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(step.ok for step in self.steps)

    @property
    def ms(self) -> int:
        return sum(step.ms for step in self.steps)

    def report(self) -> str:
        status = "PASS" if self.ok else "FAIL"
        lines = [f"{status} {self.name} ({self.ms} ms, {len(self.steps)} steps) — {self.path}"]
        for step in self.steps:
            mark = "ok  " if step.ok else "FAIL"
            lines.append(f"  {mark} {step.index:>2}. {step.step} [{step.ms} ms]")
            if step.error:
                lines.append("       " + step.error.split("\n", 1)[0])
            if step.note:
                lines.append("       note: " + step.note)
        lines += [f"  file: {a}" for a in self.artifacts]
        if self.screen:
            lines.append("  screen at failure:")
            lines += ["    " + line for line in self.screen.splitlines()]
        return "\n".join(lines)


def _targets(value: Any) -> list[tuple[str, bool]]:
    """``expect`` items as (text, exact): strings or {text, exact} mappings."""
    items = value if isinstance(value, list) else [value]
    return [
        (str(item["text"]), bool(item.get("exact", False)))
        if isinstance(item, dict)
        else (str(item), False)
        for item in items
    ]


ACTIONS = (
    "launch",
    "tap",
    "long_press",
    "input",
    "hide_keyboard",
    "scroll",
    "key",
    "back",
    "open_link",
)


def _describe(step: dict[str, Any]) -> str:
    ((kind, value),) = step.items()
    return f"{kind}: {value}" if value is not True else kind


class Runner:
    def __init__(self, device: Device | None = None, runs_dir: Path | None = None) -> None:
        self.device = device or Device()
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.run_dir = (runs_dir or Path(DEFAULT_RUNS_DIR)) / stamp
        self._screen: Screen | None = None  # settled screen after the last action
        self._before: tuple[str, Screen] | None = None  # last action, screen before it
        self._note: str | None = None

    async def run_file(self, path: Path) -> ScenarioResult:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        result = ScenarioResult(name=str(data.get("name") or path.stem), path=str(path))
        app = data.get("app")
        self._screen = self._before = None
        for index, step in enumerate(data.get("steps") or [], start=1):
            if not isinstance(step, dict) or len(step) != 1:
                result.steps.append(
                    StepResult(index, repr(step), False, 0, "step must be a one-key mapping")
                )
                break
            started = time.perf_counter()
            self._note = None
            try:
                artifact = await self._step(step, app, path.stem)
                if artifact:
                    result.artifacts.append(artifact)
                ms = int((time.perf_counter() - started) * 1000)
                result.steps.append(StepResult(index, _describe(step), True, ms, note=self._note))
            except (DeviceError, ValueError, KeyError, TypeError) as exc:
                ms = int((time.perf_counter() - started) * 1000)
                result.steps.append(StepResult(index, _describe(step), False, ms, str(exc)))
                await self._capture_failure(result, path.stem, index)
                break
        return result

    async def _capture_failure(self, result: ScenarioResult, stem: str, index: int) -> None:
        try:
            result.screen = (await self.device.screen()).compact
            target = self.run_dir / f"{stem}-step{index}-fail.png"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(await self.device.screenshot_png())
            result.artifacts.append(str(target))
        except Exception as exc:  # pylint: disable=broad-exception-caught
            result.artifacts.append(f"(failure capture failed: {exc})")

    async def _step(self, step: dict[str, Any], app: str | None, stem: str) -> str | None:
        ((kind, value),) = step.items()
        d = self.device
        if kind == "launch":
            package = app if value is True else str(value)
            if not package:
                raise ValueError("launch: true needs 'app' in the scenario")
            await d.launch(package)
        elif kind == "stop":
            package = app if value is True else str(value)
            if not package:
                raise ValueError("stop: true needs 'app' in the scenario")
            await d.stop(package)
        elif kind in ("tap", "long_press"):
            if isinstance(value, list):
                x, y = int(value[0]), int(value[1])
            else:
                spec = value if isinstance(value, dict) else {"text": value}
                x, y = await d.find(
                    str(spec["text"]),
                    nth=int(spec.get("nth", 1)),
                    timeout_ms=int(spec.get("timeout_ms", FIND_TIMEOUT_MS)),
                )
            if kind == "tap":
                await d.tap(x, y)
            else:
                await d.long_press(x, y)
        elif kind == "input":
            await d.input_into(
                str(value["field"]), str(value["text"]), bool(value.get("clear", True))
            )
        elif kind == "hide_keyboard":
            await d.hide_keyboard()
        elif kind == "expect":
            targets = _targets(value)
            for text, exact in targets:
                await d.wait_for(text, timeout_ms=DEFAULT_EXPECT_TIMEOUT_MS, exact=exact)
            self._note_stale(targets)
        elif kind == "expect_not":
            screen = await d.screen()
            for text, exact in _targets(value):
                if find_node_by_text(screen.elements, text, exact=exact) is not None:
                    raise DeviceError(f"{text!r} is on screen but must not be:\n{screen.compact}")
        elif kind == "wait":
            spec = value if isinstance(value, dict) else {"text": value}
            await d.wait_for(
                str(spec["text"]),
                timeout_ms=int(spec.get("timeout_ms", 5000)),
                gone=bool(spec.get("gone", False)),
                exact=bool(spec.get("exact", False)),
            )
        elif kind == "scroll":
            spec = value if isinstance(value, dict) else {"direction": value}
            await d.scroll(
                str(spec.get("direction", "down")),
                within=spec.get("within"),
                until=spec.get("until"),
                max_swipes=int(spec.get("max_swipes", SCROLL_MAX_SWIPES)),
            )
        elif kind == "key":
            await d.press_key(str(value))
        elif kind == "back":
            await d.back()
        elif kind == "open_link":
            await d.open_link(str(value))
        elif kind == "screenshot":
            target = self.run_dir / f"{stem}-{value}.png"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(await d.screenshot_png())
            return str(target)
        elif kind == "sleep_ms":
            await asyncio.sleep(int(value) / 1000)
        else:
            raise ValueError(f"unknown step '{kind}'")
        if kind in ACTIONS:
            before, self._screen = self._screen, await d.settled_screen()
            self._before = (_describe(step), before) if before is not None else None
        return None

    def _note_stale(self, targets: list[tuple[str, bool]]) -> None:
        """Notes an expect that the screen before the last action already satisfied."""
        if self._before is None:
            return
        action, screen = self._before
        if all(find_node_by_text(screen.elements, t, exact=e) is not None for t, e in targets):
            self._note = f"already on screen before '{action}': proves nothing about it"


def collect(paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_dir():
            files += sorted([*path.glob("*.yaml"), *path.glob("*.yml")])
        else:
            files.append(path)
    return files


async def run_paths(paths: list[str], runs_dir: Path | None = None) -> list[ScenarioResult]:
    runner = Runner(runs_dir=runs_dir)
    return [await runner.run_file(path) for path in collect(paths)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run YAML phone scenarios on a device.")
    parser.add_argument("paths", nargs="+", help="scenario files or directories")
    args = parser.parse_args(argv)
    results = asyncio.run(run_paths(args.paths))
    for result in results:
        print(result.report())
    passed = sum(r.ok for r in results)
    print(f"\n{passed}/{len(results)} scenarios passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
