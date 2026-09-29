---
name: phone-testing
description: Testing a mobile app on an Android phone through the artemis MCP server (WayupKG/artemis fork). Use when asked to check, run or test a flow on the phone, capture an app screen, reproduce a bug on a device, or write/run a YAML phone scenario.
---

# Phone testing with artemis

## Before you start
1. Call `current_app`: foreground app, active device, and whether input is allowed (`input_allowed`).
   - `foreground: null` means the screen is locked. Ask the user to unlock it; never enter a PIN.
   - `profile: null` means the project has no `.artemis.json`. Offer to create one:
     `{"allowed_packages": ["<app package>"], "device": "<serial from list_devices>"}`.
     Without it the guard is off, and the phone may be a personal one.
2. If the project has scenarios (usually `phone-scenarios/*.yaml`) and the user asks to
   run them, call `run_scenario` directly instead of stepping manually.

## Exploring manually
- Actions (`tap_text`, `tap`, `input_into`, `back`, `scroll`, `launch_app`, ...) return the
  screen once it has settled. **Do not call `get_ui_hierarchy` after them.**
- Tap by text: `tap_text("Overdue")`; type by field label or hint:
  `input_into("Search tasks…", "Standup")`. Use coordinates only for elements without text.
  Both wait up to 3 s for the element, so a sheet or screen that is still opening is fine.
  If a tab bar or the keyboard is drawn over the element, its uncovered part is tapped;
  a fully covered one is an error that names what covers it.
- Scroll a horizontal chip bar or a list inside a sheet with
  `scroll("left", within="<label of an element in it>")`; add `until="<label>"` to swipe
  until that element can be tapped.
- `launch_app` returns once the app's first screen has been still for 2 s; taps sent
  earlier are often lost to the splash and entry animations.
- Close the keyboard with `hide_keyboard`, never with `back`: Back with no keyboard
  shown leaves the screen. The keyboard can hide the tab bar and the end of a list.
- Screen line format: `[x,y] Class "label" flags`; flags are `tap input scroll selected disabled ...`.
- Wait for loading with `wait_for("text", timeout_ms=...)`, not with sleeps.
- Take a screenshot only when the visual matters (colors, layout): `take_screenshot`.
  For a bug report use `take_screenshot(save_path=...)` and attach the file to the task.
- "refused, foreground app is ..." means another app is on top. Do not work around the
  guard; bring the allowed app back with `launch_app`.

## Turning a flow into a scenario
Once a path works manually, save it as YAML so the next run needs no model:

```yaml
name: Overdue tasks from home
app: com.example.app
steps:
  - launch: true
  - tap: Home
  - tap: Overdue
  - expect: "Overdue, 7"          # quote values that contain commas
  - tap: Push notifications for assignments
  - expect: [In progress, Critical]
  - screenshot: task-card
  - back: true
```

Steps: `launch`, `stop`, `tap` (text, `{text, nth, timeout_ms}` or `[x, y]`; waits up
to 3 s for the text), `long_press`, `input: {field, text, clear}`, `hide_keyboard`,
`expect` (waits up to 3 s; items are text or `{text, exact: true}`), `expect_not`,
`wait: {text, timeout_ms, gone, exact}`, `scroll: down|up|left|right` or
`scroll: {direction, within, until, max_swipes}`, `key`, `back`, `open_link`,
`screenshot`, `sleep_ms`.

Expect something that only the target screen has. Labels match by substring, so
`expect: TASK-3` also passes on a list that shows TASK-3; the report adds a `note` when
every expected text was already on screen before the last action.

Run with `run_scenario(["phone-scenarios"])` or from a terminal:
`PYTHONPATH=~/tools/artemis ~/tools/artemis/.venv/bin/python -m wayup.scenario phone-scenarios`
(exit code 1 on failure).
Results and failure screenshots go to `.artemis/runs/` (add `.artemis/` to `.gitignore`).

## Don't
- Don't change data without consent: create, delete or send only when asked.
- Don't type the owner's passwords or PINs.
