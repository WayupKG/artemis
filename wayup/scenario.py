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
      - tap: {id: close-button}       # by resource-id (React Native testID)
      - expect: "Overdue, 7"          # waits up to 3 s; string or list
      - expect: {text: Overdue, exact: true, selected: true}
                                      # item keys: text or id, exact, and state:
                                      # selected, checked, enabled, focused
      - expect_not: Error
      - input: {field: Search tasks…, text: Standup, clear: true}   # or {id: …}
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

The report also lists the app's logcat warnings and errors during the
scenario (JS console, crashes; other error lines are only counted), and
saves them all to ``<scenario>-logcat.txt`` in the run directory.

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
    STATE_FLAGS,
    Device,
    DeviceError,
    LogEntry,
    Screen,
    find_node_by_text,
)

DEFAULT_EXPECT_TIMEOUT_MS = 3000
DEFAULT_RUNS_DIR = ".artemis/runs"
LOG_REPORT_LINES = 10
LOG_REPORT_WIDTH = 200


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
    log: list[LogEntry] = field(default_factory=list)

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
        if self.log:
            lines += _log_summary(self.log)
        lines += [f"  file: {a}" for a in self.artifacts]
        if self.screen:
            lines.append("  screen at failure:")
            lines += ["    " + line for line in self.screen.splitlines()]
        return "\n".join(lines)


@dataclass(frozen=True)
class Target:
    """What a step looks for: a label (or resource-id) and the state it must be in."""

    text: str
    exact: bool = False
    by_id: bool = False
    state: tuple[tuple[str, bool], ...] = ()

    def kwargs(self) -> dict[str, Any]:
        return {"exact": self.exact, "by_id": self.by_id, "state": dict(self.state) or None}


def _target(item: Any, key: str = "text") -> Target:
    """A string, or a mapping with ``key`` or ``id``, ``exact`` and state flags."""
    if not isinstance(item, dict):
        return Target(str(item))
    by_id = "id" in item
    return Target(
        str(item["id"] if by_id else item[key]),
        exact=bool(item.get("exact", False)),
        by_id=by_id,
        state=tuple((flag, bool(item[flag])) for flag in STATE_FLAGS if flag in item),
    )


def _targets(value: Any) -> list[Target]:
    return [_target(item) for item in (value if isinstance(value, list) else [value])]


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


def _log_summary(log: list[LogEntry]) -> list[str]:
    """Counts by kind and the JS, React Native native and crash lines, all without
    stack frames; other app errors are only counted (everything is in the log file)."""

    log = [e for e in log if not e.message.lstrip().startswith(("at ", "..."))]

    def count(kind: str, *levels: str) -> int:
        return sum(e.count for e in log if e.kind == kind and e.level in levels)

    lines = [
        f"  logcat: JS {count('js', 'E', 'F')} errors, {count('js', 'W')} warnings; "
        f"native {count('native', 'E', 'F')}; crash {count('crash', 'E', 'F')}; "
        f"other app errors {count('app', 'E', 'F')}"
    ]
    shown = [e for e in log if e.kind in ("js", "native", "crash")]
    for entry in shown[:LOG_REPORT_LINES]:
        text = str(entry)
        lines.append(
            "    " + (text if len(text) <= LOG_REPORT_WIDTH else text[: LOG_REPORT_WIDTH - 1] + "…")
        )
    if len(shown) > LOG_REPORT_LINES:
        lines.append(f"    … {len(shown) - LOG_REPORT_LINES} more")
    return lines


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
        mark = await self._log_mark()
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
        await self._collect_log(result, app, mark, path.stem)
        return result

    async def _log_mark(self) -> str | None:
        try:
            return await self.device.log_mark()
        except Exception:  # pylint: disable=broad-exception-caught
            return None

    async def _collect_log(
        self, result: ScenarioResult, app: str | None, mark: str | None, stem: str
    ) -> None:
        """Adds the app's warnings and errors to the result and saves them all to a file."""
        if mark is None:
            return
        try:
            result.log = await self.device.app_log(mark, app)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            result.artifacts.append(f"(logcat failed: {exc})")
            return
        if result.log:
            target = self.run_dir / f"{stem}-logcat.txt"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("\n".join(map(str, result.log)) + "\n", encoding="utf-8")
            result.artifacts.append(str(target))

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
                target = _target(spec)
                x, y = await d.find(
                    target.text,
                    nth=int(spec.get("nth", 1)),
                    timeout_ms=int(spec.get("timeout_ms", FIND_TIMEOUT_MS)),
                    by_id=target.by_id,
                )
            if kind == "tap":
                await d.tap(x, y)
            else:
                await d.long_press(x, y)
        elif kind == "input":
            target = _target(value, key="field")
            await d.input_into(
                target.text,
                str(value["text"]),
                bool(value.get("clear", True)),
                by_id=target.by_id,
            )
        elif kind == "hide_keyboard":
            await d.hide_keyboard()
        elif kind == "expect":
            targets = _targets(value)
            for target in targets:
                await d.wait_for(
                    target.text, timeout_ms=DEFAULT_EXPECT_TIMEOUT_MS, **target.kwargs()
                )
            self._note_stale(targets)
        elif kind == "expect_not":
            screen = await d.screen()
            for target in _targets(value):
                if find_node_by_text(screen.elements, target.text, **target.kwargs()) is not None:
                    raise DeviceError(
                        f"{target.text!r} is on screen but must not be:\n{screen.compact}"
                    )
        elif kind == "wait":
            spec = value if isinstance(value, dict) else {"text": value}
            target = _target(spec)
            await d.wait_for(
                target.text,
                timeout_ms=int(spec.get("timeout_ms", 5000)),
                gone=bool(spec.get("gone", False)),
                **target.kwargs(),
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

    def _note_stale(self, targets: list[Target]) -> None:
        """Notes an expect that the screen before the last action already satisfied."""
        if self._before is None:
            return
        action, screen = self._before
        if all(
            find_node_by_text(screen.elements, t.text, **t.kwargs()) is not None for t in targets
        ):
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
