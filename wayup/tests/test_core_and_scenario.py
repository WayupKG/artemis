"""Text lookup and the YAML scenario runner, against a fake device."""

import asyncio

import pytest

from wayup import core
from wayup.core import Device, DeviceError, Screen, find_by_text, find_element_at
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


def _sheet(top):
    return [
        _node(
            "Reset", cls="android.widget.Button", bounds=(0, top, 200, top + 100), clickable="true"
        )
    ]


class ScriptedDevice(Device):
    """Real Device logic over a scripted sequence of screens; the last one repeats."""

    def __init__(self, screens, foregrounds=None):
        super().__init__()
        self.screens = screens
        self.foregrounds = foregrounds or ["kg.replai.revision"] * len(screens)
        self.reads = 0
        self.keyboard = False
        self.keys = []

    async def screen(self, show_keyboard=False):
        i = min(self.reads, len(self.screens) - 1)
        self.reads += 1
        elements = self.screens[i]
        compact = "\n".join(str(n["parsed_bounds"]) + n["text"] for n in elements)
        return Screen(self.foregrounds[i], elements, compact)

    async def keyboard_shown(self):
        return self.keyboard

    async def press_key(self, keycode):
        self.keys.append(keycode)
        self.keyboard = False


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(core, "FIND_POLL_S", 0)
    monkeypatch.setattr(core, "SETTLE_POLL_S", 0)
    monkeypatch.setattr(core, "LAUNCH_QUIET_S", 0)


def test_find_returns_at_once_when_the_element_is_there(fast):
    device = ScriptedDevice([_sheet(700)])

    assert asyncio.run(device.find("Reset")) == (100, 750)
    assert device.reads == 1


def test_find_waits_for_a_late_element_to_stop_moving(fast):
    device = ScriptedDevice([[], _sheet(900), _sheet(700), _sheet(700)])

    assert asyncio.run(device.find("Reset")) == (100, 750)
    assert device.reads == 4


def test_find_gives_up_with_the_last_screen(fast):
    device = ScriptedDevice([[]])

    with pytest.raises(DeviceError, match="'Reset' on screen after 0 ms"):
        asyncio.run(device.find("Reset", timeout_ms=0))


def test_app_is_ready_once_its_own_screen_is_still(fast):
    launcher, splash, home = [_node("Apps")], [_node("Revision")], HOME
    device = ScriptedDevice(
        [launcher, splash, home, home, home],
        foregrounds=["launcher"] + ["kg.replai.revision"] * 4,
    )

    asyncio.run(device.wait_until_ready("kg.replai.revision"))

    assert device.reads == 4


def test_hide_keyboard_presses_back_only_while_the_keyboard_is_up(fast):
    device = ScriptedDevice([HOME])

    assert asyncio.run(device.hide_keyboard()) is False
    assert device.keys == []

    device.keyboard = True
    assert asyncio.run(device.hide_keyboard()) is True
    assert device.keys == ["KEYCODE_BACK"]


class FakeDevice:
    """Records actions; the screen is whatever the test sets."""

    def __init__(self, elements):
        self.elements = elements
        self.actions = []

    async def screen(self, show_keyboard=False):
        return Screen("kg.replai.revision", self.elements, "app: kg.replai.revision")

    async def settled_screen(self):
        return await self.screen()

    async def find(self, text, nth=1, field=False, timeout_ms=core.FIND_TIMEOUT_MS):
        self.actions.append(("find", text, timeout_ms))
        match = find_by_text(self.elements, text, nth=nth, field=field)
        if match is None:
            raise DeviceError(f"no element matching {text!r}")
        return match

    async def hide_keyboard(self):
        self.actions.append(("hide_keyboard",))
        return True

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
        ("find", "Overdue", 3000),
        ("tap", 155, 250),
        ("input", "Search tasks…", "Standup", True),
        ("back",),
    ]


def test_scenario_tap_timeout_and_hide_keyboard(tmp_path):
    device = FakeDevice(HOME)
    result = _run(
        tmp_path,
        """
steps:
  - tap: {text: Done, nth: 2, timeout_ms: 8000}
  - hide_keyboard: true
""",
        device,
    )

    assert result.ok, result.report()
    assert device.actions == [("find", "Done", 8000), ("tap", 400, 850), ("hide_keyboard",)]


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
