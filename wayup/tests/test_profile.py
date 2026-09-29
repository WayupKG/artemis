# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Per-project profile, foreground guard and compact hierarchy of the stdio server."""

import json

import pytest

from wayup import profile as local_profile


def _node(text="", cls="android.widget.TextView", bounds=(0, 0, 100, 100), **attrs):
    left, top, right, bottom = bounds
    node = {
        "text": text,
        "class": cls,
        "package": "kg.replai.revision",
        "content-desc": "",
        "clickable": "false",
        "enabled": "true",
        "visible-to-user": "true",
        "parsed_bounds": {"left": left, "top": top, "right": right, "bottom": bottom},
        "hint": "",
    }
    node.update(attrs)
    return node


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.delenv("ARTEMIS_PROFILE", raising=False)
    monkeypatch.delenv("ARTEMIS_ALLOWED_PACKAGES", raising=False)
    monkeypatch.chdir(tmp_path)
    local_profile.select_serial(None)
    yield
    local_profile.select_serial(None)


def test_profile_found_in_parent_directory(tmp_path):
    (tmp_path / ".artemis.json").write_text(
        json.dumps({"allowed_packages": ["kg.replai.revision"], "device": "SERIAL1"})
    )
    nested = tmp_path / "app" / "src"
    nested.mkdir(parents=True)

    profile = local_profile.load_profile(nested)

    assert profile.allowed_packages == ["kg.replai.revision"]
    assert profile.device == "SERIAL1"
    assert profile.source == str(tmp_path / ".artemis.json")


def test_no_profile_allows_everything():
    profile = local_profile.load_profile()

    assert profile.source is None
    assert local_profile.is_package_allowed("com.openai.chatgpt", profile)


def test_guard_refuses_foreign_app_but_allows_permission_dialog(monkeypatch):
    monkeypatch.setenv("ARTEMIS_ALLOWED_PACKAGES", "kg.replai.revision, com.example")
    profile = local_profile.load_profile()

    assert local_profile.is_package_allowed("com.example", profile)
    assert local_profile.is_package_allowed("com.android.permissioncontroller", profile)
    assert not local_profile.is_package_allowed("com.openai.chatgpt", profile)
    assert not local_profile.is_package_allowed(None, profile)
    assert "com.openai.chatgpt" in local_profile.guard_message("com.openai.chatgpt", profile)


def test_session_selection_overrides_profile_device(tmp_path):
    (tmp_path / ".artemis.json").write_text(json.dumps({"device": "FROM_PROFILE"}))

    assert local_profile.selected_serial() == "FROM_PROFILE"
    local_profile.select_serial("EMULATOR")
    assert local_profile.selected_serial() == "EMULATOR"


def test_compact_hierarchy_drops_repeated_labels_and_status_bar():
    elements = [
        _node("23:52", package="com.android.systemui"),
        _node(cls="android.widget.ScrollView", bounds=(0, 100, 1000, 2000), scrollable="true"),
        _node(
            cls="android.widget.Button",
            bounds=(0, 100, 600, 300),
            clickable="true",
            **{"content-desc": "Просрочено: 7"},
        ),
        _node("07", bounds=(10, 110, 200, 200)),
        _node("Просрочено", bounds=(10, 210, 300, 290)),
        _node("Здравствуйте, Test", bounds=(0, 400, 600, 500)),
        _node(
            cls="android.widget.EditText",
            bounds=(0, 600, 1000, 700),
            clickable="true",
            hint="Комментарий…",
        ),
        _node(
            cls="android.widget.Button",
            bounds=(0, 800, 200, 900),
            clickable="true",
            enabled="false",
            **{"content-desc": "Удалить задачу"},
        ),
        _node(cls="android.view.View", bounds=(0, 0, 10, 10)),
    ]

    compact = local_profile.compact_hierarchy(elements, foreground="kg.replai.revision")

    assert compact.splitlines() == [
        "app: kg.replai.revision",
        "[500,1050] ScrollView scroll",
        '[300,200] Button "Просрочено: 7" tap',
        '[105,155] TextView "07"',
        '[300,450] TextView "Здравствуйте, Test"',
        '[500,650] EditText hint="Комментарий…" tap input',
        '[100,850] Button "Удалить задачу" tap disabled',
    ]
