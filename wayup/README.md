# wayup — our layer on top of Google ARTEMIS

A fast MCP server for testing apps on an Android phone, tuned for an
LLM coding agent driving the device. All fork code lives in `wayup/` (plus
`.github/workflows/wayup-sync.yml`); upstream files are never edited, so
Google's updates merge without conflicts.

## Compared to `artemis.mcp.adb_server`

| | upstream | wayup |
|---|---|---|
| Screen hierarchy | ~1000 ms, ~70k chars of JSON | ~150 ms, ~1.5k chars, one line per element |
| After an action | "Success", screen needs another call | settled screen in the same response |
| Screenshot | base64 as text, ~1000 ms | MCP image, ~400 ms |
| Focusing a field before typing | ~2 s (hierarchy + fixed 1 s sleep) | waits for actual focus |
| Tap / type by visible text | no | `tap_text`, `input_into`, `wait_for`; wait up to 3 s for the element |
| Launching an app | returns when the window shows | returns once the first screen has been still for 2 s |
| Closing the keyboard | Back, which navigates away if no keyboard is up | `hide_keyboard`: Back only while the keyboard is shown |
| Replay without a model | no | YAML scenarios, `run_scenario` / `python -m wayup.scenario` |
| Personal phone guard | no | `.artemis.json` → `allowed_packages` |
| Several devices | env only | `list_devices`, `select_device`, `device` in the profile |

## Setup

Register the server with your MCP client, for example:

```sh
claude mcp add artemis --scope user \
  -e PYTHONUNBUFFERED=1 -e PYTHONPATH=$HOME/tools/artemis -e ARTEMIS_KEEP_DEVICE_AWAKE=false \
  -- $HOME/tools/artemis/.venv/bin/python -m wayup.server
```

The agent skill describing the testing workflow is in
`wayup/claude/skills/phone-testing`; link it into your skills directory:

```sh
ln -s $HOME/tools/artemis/wayup/claude/skills/phone-testing ~/.claude/skills/phone-testing
```

## Project profile `.artemis.json`

Looked up from the server's working directory upwards (or set `ARTEMIS_PROFILE`):

```json
{
  "allowed_packages": ["com.example.app"],
  "device": "SERIAL",
  "show_system_ui": false
}
```

With `allowed_packages` set, input actions are refused unless one of these
packages is in the foreground, and only these packages can be launched or
stopped. System permission dialogs are always allowed.

## Scenarios

The step format is documented in `wayup/scenario.py` and in the
`phone-testing` skill. Run from a terminal:

```sh
PYTHONPATH=~/tools/artemis ~/tools/artemis/.venv/bin/python -m wayup.scenario phone-scenarios/
```

Exit code is 1 if any scenario fails. Run output and failure screenshots go
to `.artemis/runs/` in the current directory.

## Upstream updates

- Automatic: `wayup-sync.yml` runs weekly, merges `google/artemis` main into a
  `sync/upstream-DATE` branch, runs the wayup tests and opens a PR (or an issue
  on a merge conflict). Nothing reaches `main` without a manual merge.
- Manual: `wayup/sync_upstream.sh`.

## Tests

```sh
.venv/bin/python -m pytest wayup/tests -q
```
