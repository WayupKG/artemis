"""Text lookup and the YAML scenario runner, against a fake device."""

import asyncio

import pytest

from wayup import core
from wayup.core import (
    Device,
    DeviceError,
    Screen,
    annotate_subtrees,
    find_by_text,
    find_element_at,
    locate,
)
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


def test_exact_skips_substring_matches():
    assert find_by_text(HOME, "Overdue:", exact=True) is None
    assert find_by_text(HOME, "overdue: 7", exact=True) == (300, 200)


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
        compact = "\n".join(f"{n.get('parsed_bounds')}{n.get('text')}" for n in elements)
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


# A list row half under a floating tab bar, then a keyboard in another window.
SCREEN_XML = """<hierarchy rotation="0">
  <node class="android.widget.FrameLayout" package="app" bounds="[0,0][1000,2000]">
    <node class="android.widget.ScrollView" package="app" bounds="[0,0][1000,2000]" scrollable="true">
      <node class="android.widget.Button" package="app" text="Row" clickable="true" bounds="[0,1700][1000,1900]">
        <node class="android.widget.TextView" package="app" text="Row" bounds="[20,1720][500,1880]" />
      </node>
      <node class="android.widget.HorizontalScrollView" package="app" bounds="[0,100][1000,200]" scrollable="true">
        <node class="android.view.View" package="app" text="Today, 1" clickable="true" bounds="[0,100][300,200]" />
      </node>
    </node>
    <node class="android.view.View" package="app" bounds="[50,1750][950,1950]">
      <node class="android.view.View" package="app" text="Home" clickable="true" bounds="[50,1750][500,1950]" />
    </node>
  </node>
</hierarchy>"""


def _elements(xml=SCREEN_XML):
    from third_party.mobile_use.clients.ui_automator_client import (
        parse_hierarchy_xml_to_elements,
    )

    elements = parse_hierarchy_xml_to_elements(xml)
    for node in elements:
        node.setdefault("visible-to-user", "true")
    annotate_subtrees(xml, elements)
    return elements


def test_subtree_ends_follow_the_parser_order():
    elements = _elements()
    row = next(i for i, n in enumerate(elements) if n.get("class") == "android.widget.Button")

    assert elements[row]["subtree_end"] == row + 2
    assert elements[0]["subtree_end"] == len(elements)


def test_tap_goes_to_the_uncovered_part_of_a_row_under_the_tab_bar():
    point, cover = locate(_elements(), "Row")

    # The bar covers y 1750..1950 over x 50..950; the row is 1700..1900.
    assert cover is None
    x, y = point
    assert y < 1750 or x < 50 or x >= 950


def test_fully_covered_element_names_what_covers_it():
    keyboard = SCREEN_XML.replace(
        "</hierarchy>",
        '<node class="android.widget.FrameLayout" package="ime" bounds="[0,1600][1000,2000]" />'
        "</hierarchy>",
    )

    point, cover = locate(_elements(keyboard), "Row")

    assert point is None
    assert cover == "'android.widget.FrameLayout'"


def test_descendants_and_earlier_nodes_do_not_cover():
    point, cover = locate(_elements(), "Today, 1")

    assert (point, cover) == ((150, 150), None)


def test_scroll_within_swipes_inside_the_container(fast):
    device = ScriptedDevice([_elements()])
    swipes = []

    async def swipe(*args, duration=400):
        swipes.append(args)

    device.swipe = swipe
    asyncio.run(device.scroll("left", within="Today, 1"))

    assert swipes == [(200, 150, 800, 150)]


def test_scroll_until_stops_when_the_element_can_be_tapped(fast):
    device = ScriptedDevice([[_node("First")], [_node("Second")], _sheet(700)])
    device.settled_screen = device.screen
    swipes = []

    async def region(within, screen=None):
        return 0, 0, 1000, 2000

    async def swipe(*args, duration=400):
        swipes.append(args)

    device._scroll_region, device.swipe = region, swipe
    asyncio.run(device.scroll("down", until="Reset"))

    assert len(swipes) == 2


def test_scroll_until_fails_at_the_end_of_the_content(fast):
    device = ScriptedDevice([[]])
    device.settled_screen = device.screen

    async def region(within, screen=None):
        return 0, 0, 1000, 2000

    async def swipe(*args, duration=400):
        pass

    device._scroll_region, device.swipe = region, swipe
    with pytest.raises(DeviceError, match="reached the end scrolling down without 'Reset'"):
        asyncio.run(device.scroll("down", until="Reset"))


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

    async def wait_for(self, text, timeout_ms=5000, gone=False, exact=False):
        if (find_by_text(self.elements, text, exact=exact) is None) != gone:
            raise DeviceError(f"{text!r} not on screen")
        return await self.screen()

    async def back(self):
        self.actions.append(("back",))

    async def scroll(self, direction, within=None, until=None, max_swipes=10):
        self.actions.append(("scroll", direction, within, until, max_swipes))

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


def test_scenario_scroll_spec_and_exact_expect(tmp_path):
    device = FakeDevice(HOME)
    result = _run(
        tmp_path,
        """
steps:
  - scroll: down
  - scroll: {direction: left, within: Overdue tasks, until: Done, max_swipes: 3}
  - expect: [{text: "Overdue: 7", exact: true}]
  - expect_not: {text: "Overdue:", exact: true}
""",
        device,
    )

    assert result.ok, result.report()
    assert device.actions == [
        ("scroll", "down", None, None, 10),
        ("scroll", "left", "Overdue tasks", "Done", 3),
    ]


def test_expect_already_true_before_the_action_gets_a_note(tmp_path):
    device = FakeDevice(HOME)
    result = _run(
        tmp_path,
        """
steps:
  - tap: Done
  - tap: Overdue
  - expect: Overdue tasks
  - expect: Missing
""",
        device,
    )

    assert [s.note for s in result.steps[:3]] == [
        None,
        None,
        "already on screen before 'tap: Overdue': proves nothing about it",
    ]


def test_scroll_until_keeps_the_container_when_its_anchor_scrolls_away(fast):
    anchored = _elements()
    moved = [n for n in _elements() if n.get("text") != "Today, 1"] + [_node("Later")]
    device = ScriptedDevice([anchored, moved, _sheet(700)])
    device.settled_screen = device.screen
    swipes = []

    async def swipe(*args, duration=400):
        swipes.append(args)

    device.swipe = swipe
    asyncio.run(device.scroll("left", within="Today, 1", until="Reset"))

    assert swipes == [(200, 150, 800, 150)] * 2
