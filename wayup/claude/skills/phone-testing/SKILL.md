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

Steps: `launch`, `stop`, `tap` (text, `{text, nth}` or `[x, y]`), `long_press`,
`input: {field, text, clear}`, `expect` (waits up to 3 s), `expect_not`,
`wait: {text, timeout_ms, gone}`, `scroll: down|up|left|right`, `key`, `back`,
`open_link`, `screenshot`, `sleep_ms`.

Run with `run_scenario(["phone-scenarios"])` or from a terminal:
`~/tools/artemis/.venv/bin/python -m wayup.scenario phone-scenarios` (exit code 1 on failure).
Results and failure screenshots go to `.artemis/runs/` (add `.artemis/` to `.gitignore`).

## Don't
- Don't change data without consent: create, delete or send only when asked.
- Don't type the owner's passwords or PINs.
