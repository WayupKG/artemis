"""Text lookup and the YAML scenario runner, against a fake device."""

import asyncio

import pytest

from wayup.core import DeviceError, Screen, find_by_text, find_element_at
from wayup.scenario import Runner


def _node(text="", cls="android.widget.TextView", bounds=(0, 0, 100, 100), **attrs):
    left, top, right, bottom = bounds
    node = {
        "text": text,
        "class": cls,
        "content-desc": "",
        "hint": "",
        "clickable": "false",
        "visible-to-user": "true",
        "parsed_bounds": {"left": left, "top": top, "right": right, "bottom": bottom},
    }
    node.update(attrs)
    return node


HOME = [
    _node(
        cls="android.widget.Button",
        bounds=(0, 100, 600, 300),
        clickable="true",
        **{"content-desc": "Overdue: 7"},
    ),
    _node("Overdue", bounds=(10, 210, 300, 290)),
    _node("Overdue tasks", bounds=(0, 400, 600, 500)),
    _node(
        cls="android.widget.EditText",
        bounds=(0, 600, 1000, 700),
        clickable="true",
        hint="Search tasks…",
    ),
    _node("Done", cls="android.widget.Button", bounds=(0, 800, 200, 900), clickable="true"),
    _node("Done", cls="android.widget.Button", bounds=(300, 800, 500, 900), clickable="true"),
]


def test_exact_label_beats_substring():
    assert find_by_text(HOME, "overdue") == (155, 250)


def test_substring_prefers_interactive_element():
    assert find_by_text(HOME, "Overdue:") == (300, 200)


def test_field_lookup_matches_hint_of_inputs_only():
    assert find_by_text(HOME, "search tasks", field=True) == (500, 650)
    assert find_by_text(HOME, "Done", field=True) is None


def test_nth_picks_among_equal_matches():
    assert find_by_text(HOME, "Done", nth=2) == (400, 850)
    assert find_by_text(HOME, "Done", nth=3) is None


def test_element_at_point_is_the_smallest():
    assert find_element_at(HOME, 20, 220)["text"] == "Overdue"


class FakeDevice:
    """Records actions; the screen is whatever the test sets."""

    def __init__(self, elements):
        self.elements = elements
        self.actions = []

    async def screen(self, show_keyboard=False):
        return Screen("kg.replai.revision", self.elements, "app: kg.replai.revision")

    async def settled_screen(self):
        return await self.screen()

    async def find(self, text, nth=1, field=False):
        match = find_by_text(self.elements, text, nth=nth, field=field)
        if match is None:
            raise DeviceError(f"no element matching {text!r}")
        return match

    async def launch(self, package):
        self.actions.append(("launch", package))

    async def tap(self, x, y):
        self.actions.append(("tap", x, y))

    async def input_into(self, field, text, clear=True):
        self.actions.append(("input", field, text, clear))

    async def wait_for(self, text, timeout_ms=5000, gone=False):
        if (find_by_text(self.elements, text) is None) != gone:
            raise DeviceError(f"{text!r} not on screen")
        return await self.screen()

    async def back(self):
        self.actions.append(("back",))

    async def screenshot_png(self):
        return b"png"


def _run(tmp_path, yaml_text, device):
    path = tmp_path / "flow.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    runner = Runner(device=device, runs_dir=tmp_path / "runs")
    return asyncio.run(runner.run_file(path))


def test_scenario_runs_steps_in_order(tmp_path):
    device = FakeDevice(HOME)
    result = _run(
        tmp_path,
        """
name: Home
app: kg.replai.revision
steps:
  - launch: true
  - tap: Overdue
  - input: {field: Search tasks…, text: Standup}
  - expect: ["Overdue tasks", "Overdue: 7"]
  - expect_not: Error
  - back: true
""",
        device,
    )

    assert result.ok, result.report()
    assert device.actions == [
        ("launch", "kg.replai.revision"),
        ("tap", 155, 250),
        ("input", "Search tasks…", "Standup", True),
        ("back",),
    ]


def test_scenario_stops_at_first_failure_and_saves_screenshot(tmp_path):
    device = FakeDevice(HOME)
    result = _run(
        tmp_path,
        """
steps:
  - expect: Missing
  - back: true
""",
        device,
    )

    assert not result.ok
    assert [s.ok for s in result.steps] == [False]
    assert result.screen == "app: kg.replai.revision"
    assert any(a.endswith("flow-step1-fail.png") for a in result.artifacts)
    assert device.actions == []


@pytest.mark.parametrize("step", ["- frobnicate: 1", "- {tap: a, back: true}", "- launch: true"])
def test_scenario_reports_bad_steps(tmp_path, step):
    result = _run(tmp_path, f"steps:\n  {step}\n", FakeDevice(HOME))

    assert not result.ok
