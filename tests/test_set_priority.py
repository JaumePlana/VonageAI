"""Queue - Set Priority (setPriority.har): a priority and an optional next applet, saved through Applet/Save."""

import json
from unittest.mock import MagicMock

import pytest

from vccflow.compare import compare_docs
from vccflow.config import Account
from vccflow.export import PRIORITY_LABELS, export_applet
from vccflow.importer import import_plan, unsupported_reason

PLAN_ID = "b918444e-7c29-41b0-9a65-9a64b8568257"
ACCOUNT = Account(name="Demo29", region="emea", allow_portal_writes=True, portal_account="NVMDemo29")
# VCC's answer to the last recorded save (#13).
PROPS = {"Priority": 0.1, "DialledNumber": PLAN_ID, "AppletName": "QSP Test", "DisplayName": "Queue - Set Priority",
         "AppletType": "Priority", "NextApplet": "ANN TTS", "NextAppletGuid": "8a6bc72d-d6d7-11f1-ab24-0240f276eb0d",
         "Url": "Applet/Priority", "ConfigurationUrl": "Applet", "ApplicationType": 24, "EntityId": 165897270,
         "AppletGuid": "f575f74b-e996-11f1-8a44-0ab3d743c8e3"}
LISTED = [{"id": 165897270, "guid": "f575f74b-e996-11f1-8a44-0ab3d743c8e3", "name": "QSP Test"},
          {"id": 2, "guid": "8a6bc72d-d6d7-11f1-ab24-0240f276eb0d", "name": "ANN TTS"}]


def exported(props=None):
    portal = MagicMock()
    portal.get_applet_state.return_value = {"appletProperties": dict(props or PROPS), "metadata": {}}
    applet = {"name": "QSP Test", "appletType": "Queue - Set Priority", "configUrl": "Applet?application=Priority"}
    return export_applet(portal, PLAN_ID, applet, None, LISTED)


def test_the_choices_are_the_editors():
    # The editor's options, as the recorded metadata lists them.
    assert PRIORITY_LABELS == {"0.9": "Highest", "0.75": "Higher", "0.5": "Normal", "0.25": "Lower", "0.1": "Lowest"}


def test_export_keeps_the_priority_and_the_next_applet_by_name():
    e = exported()
    assert e["type"] == "Priority"
    assert e["settings"] == {"Priority": 0.1}
    assert e["next"] == {"NextApplet": "ANN TTS"}
    assert "connectionProblems" not in e


def test_export_without_a_next_applet():
    e = exported({**PROPS, "NextApplet": "", "NextAppletGuid": None, "Priority": 0.5})
    assert e["settings"] == {"Priority": 0.5} and e["next"] == {}


@pytest.mark.parametrize("value", [0.9, "0.75", 0.5, "0.25", 0.1])
def test_every_recorded_value_can_be_imported(value):
    assert unsupported_reason({"name": "Q", "type": "Priority", "settings": {"Priority": value}, "next": {}}) is None


@pytest.mark.parametrize("settings, message", [
    ({"Priority": 0.3}, "is not one of the editor's choices"),
    ({}, "is not one of the editor's choices"),
    ({"Priority": 0.5, "Other": 1}, "settings Other have not been recorded yet"),
])
def test_unrecorded_values_are_refused(settings, message):
    reason = unsupported_reason({"name": "Q", "type": "Priority", "settings": settings, "next": {}})
    assert reason and message in reason


class FakePortal:
    account = ACCOUNT

    def __init__(self):
        self.applets, self.writes = {}, []

    def list_applets(self, plan_id):
        return {"name": "Scratch", "applets": [{"id": 100 + i, "guid": f"g-{n}", "name": n, "appletType": t}
                                               for i, (n, t) in enumerate(self.applets.items())]}

    def list_data_sources(self, plan_id):
        return set()

    def create_applet(self, plan_id, applet_type, name):
        self.writes.append(("create", name, applet_type))
        self.applets[name] = applet_type

    def delete_applet(self, plan_id, name):
        self.writes.append(("delete", name))

    def get_applet_state(self, plan_id, applet):
        # A new applet, as VCC answers the first recorded save (#4).
        return {"appletProperties": {"AppletType": self.applets[applet["name"]], "AppletName": applet["name"],
                                     "Priority": 0.5, "NextApplet": "", "NextAppletGuid": None, "EntityId": 1}}

    def save_applet(self, plan_id, name, props):
        self.writes.append(("save", name, json.loads(json.dumps(props))))

    def set_initial_applet(self, plan_id, name):
        self.writes.append(("initial", name))


def write_doc(tmp_path, *priorities):
    others = [{"name": "ANN TTS", "type": "SetDataSource", "settings": {"DataSources": []}, "next": {}}]
    doc = {"format": "vccflow.plan/1", "plan": {"name": "Source", "initialApplet": "ANN TTS"},
           "applets": [*priorities, *others]}
    (tmp_path / "plan.json").write_text(json.dumps(doc), encoding="utf-8")


def test_import_saves_the_priority_and_the_target_next_applet(tmp_path, monkeypatch):
    monkeypatch.setattr("vccflow.importer.export.export_plan",
                        lambda *a, **k: {"plan": {"initialApplet": "ANN TTS"}, "applets": []})
    write_doc(tmp_path, {"name": "QSP Test", "type": "Priority", "settings": {"Priority": 0.9},
                         "next": {"NextApplet": "ANN TTS"}},
              {"name": "QSP Alone", "type": "Priority", "settings": {"Priority": 0.25}, "next": {}})
    portal = FakePortal()
    import_plan(portal, tmp_path, PLAN_ID, "Scratch", log=lambda _: None)
    assert ("create", "QSP Test", "Priority") in portal.writes
    saved = {w[1]: w[2] for w in portal.writes if w[0] == "save"}
    assert saved["QSP Test"]["Priority"] == 0.9
    assert saved["QSP Test"]["NextApplet"] == "ANN TTS" and saved["QSP Test"]["NextAppletGuid"] == "g-ANN TTS"
    assert saved["QSP Alone"]["Priority"] == 0.25
    assert saved["QSP Alone"]["NextApplet"] == "" and saved["QSP Alone"]["NextAppletGuid"] is None


def test_comparison_shows_the_page_wording_both_ways():
    a = {**exported(), "kind": "Queue - Set Priority"}
    b = json.loads(json.dumps(a))
    b["settings"]["Priority"] = "0.9"
    del b["next"]["NextApplet"]
    doc = lambda x: {"plan": {}, "applets": [x]}
    diffs = compare_docs(doc(a), doc(b))["differences"]
    assert "'QSP Test' setting Priority: source Lowest | target Highest" in diffs
    assert "'QSP Test' path NextApplet is missing in the target (source goes to 'ANN TTS')" in diffs
    back = compare_docs(doc(b), doc(a))["differences"]
    assert "'QSP Test' setting Priority: source Highest | target Lowest" in back
    # The number VCC answers and the string the editor posts are the same choice.
    c = json.loads(json.dumps(a))
    c["settings"]["Priority"] = "0.1"
    assert compare_docs(doc(a), doc(c))["differences"] == []
