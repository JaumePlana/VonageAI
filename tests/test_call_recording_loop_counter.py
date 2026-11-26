"""Call Recording (callRecording.har) and Loop Counter (loopCounter.har): React editors saved through Applet/Save."""

import json
from unittest.mock import MagicMock

import pytest

from vccflow.compare import compare_docs
from vccflow.config import Account
from vccflow.export import export_applet
from vccflow.importer import import_plan, unsupported_reason

PLAN_ID = "2b1fa24b-89d4-449f-ab7c-ca86b01cd841"
ACCOUNT = Account(name="Demo29", region="emea", allow_portal_writes=True, portal_account="NVMDemo29")
LISTED = [{"id": 165942582, "guid": "8adc58aa-e9bb-11f1-8a44-0ab3d743c8e3", "name": "CR Recording"},
          {"id": 165945398, "guid": "ba112372-e9bb-11f1-8a44-0ab3d743c8e3", "name": "LC Loop"},
          {"id": 148703030, "guid": "6fdde60c-b586-11f0-a707-0240f276eb0d", "name": "LC test"}]
# VCC's answers to the last saves (#14 and #10).
CR = {"DelayTransfer": "Immediate", "Function": "Resume", "DialledNumber": PLAN_ID, "AppletName": "CR Recording",
      "DisplayName": "Call Recording", "AppletType": "CallRecording", "NextApplet": "LC test",
      "NextAppletGuid": "6fdde60c-b586-11f0-a707-0240f276eb0d", "Url": "Applet/CallRecording",
      "ConfigurationUrl": "Applet", "ApplicationType": 8, "EntityId": 165942582,
      "AppletGuid": "8adc58aa-e9bb-11f1-8a44-0ab3d743c8e3"}
LC = {"MaximumLoopCount": 1, "NextAppletIdOnMaximumReached": 148703030,
      "NextAppletGuidOnMaximumReached": "6fdde60c-b586-11f0-a707-0240f276eb0d", "DialledNumber": PLAN_ID,
      "AppletName": "LC Loop", "DisplayName": "Loop Counter", "AppletType": "LoopCounter", "NextApplet": "CR Recording",
      "NextAppletGuid": "8adc58aa-e9bb-11f1-8a44-0ab3d743c8e3", "Url": "Applet/LoopCounter", "ConfigurationUrl": "Applet",
      "ApplicationType": 19, "EntityId": 165945398, "AppletGuid": "ba112372-e9bb-11f1-8a44-0ab3d743c8e3"}


def exported(props, kind, listed=LISTED):
    portal = MagicMock()
    portal.get_applet_state.return_value = {"appletProperties": json.loads(json.dumps(props)), "metadata": {}}
    applet = {"name": props["AppletName"], "appletType": kind, "configUrl": f"Applet?application={props['AppletType']}"}
    return export_applet(portal, PLAN_ID, applet, None, listed)


# --- Call Recording ---

def test_call_recording_exports_its_two_choices_and_the_next_applet_by_name():
    e = exported(CR, "Call Recording")
    assert e["type"] == "CallRecording"
    assert e["settings"] == {"DelayTransfer": "Immediate", "Function": "Resume"}
    assert e["next"] == {"NextApplet": "LC test"}
    assert unsupported_reason(e) is None


@pytest.mark.parametrize("function", ["Start", "Pause", "Resume"])
@pytest.mark.parametrize("when", ["Immediate", "Transferred", "Manual"])
def test_every_combination_is_accepted(function, when):
    # The editor offers some per action only, yet saved Pause + Immediate (#9); any is valid (user).
    applet = {"name": "CR", "type": "CallRecording", "settings": {"Function": function, "DelayTransfer": when},
              "next": {}}
    assert unsupported_reason(applet) is None


@pytest.mark.parametrize("settings, message", [
    ({"Function": "Stop", "DelayTransfer": "Immediate"}, "Function 'Stop' is not one of Start, Pause, Resume"),
    ({"Function": "Start"}, "DelayTransfer None is not one of Immediate, Transferred, Manual"),
    ({"Function": "Start", "DelayTransfer": "Immediate", "Other": 1}, "settings Other have not been recorded yet"),
])
def test_call_recording_refuses_what_was_not_recorded(settings, message):
    assert message in unsupported_reason({"name": "CR", "type": "CallRecording", "settings": settings, "next": {}})


# --- Loop Counter ---

def test_loop_counter_exports_its_maximum_and_both_applets_by_name():
    e = exported(LC, "Loop Counter")
    assert e["type"] == "LoopCounter"
    assert e["settings"] == {"MaximumLoopCount": 1}
    assert e["next"] == {"NextApplet": "CR Recording", "NextAppletOnMaximumReached": "LC test"}
    assert unsupported_reason(e) is None


def test_a_new_loop_counter_exports_with_no_applets():
    # Save #4: VCC saves it with 1 run and no applets (NextApplet null).
    new = {**LC, "NextAppletIdOnMaximumReached": None, "NextAppletGuidOnMaximumReached": None, "NextApplet": None,
           "NextAppletGuid": None}
    e = exported(new, "Loop Counter")
    assert e["settings"] == {"MaximumLoopCount": 1} and e["next"] == {}
    assert unsupported_reason(e) is None


@pytest.mark.parametrize("value, ok", [(1, True), ("3", True), (10, True), ("10", True),
                                       # #7, #8: refused by VCC; #9: an empty value answers HTTP 500.
                                       (11, False), ("0", False), ("", False), (None, False)])
def test_the_maximum_runs_are_1_to_10(value, ok):
    reason = unsupported_reason({"name": "LC", "type": "LoopCounter", "settings": {"MaximumLoopCount": value}, "next": {}})
    assert (reason is None) if ok else f"maximum runs {value!r} is not 1 to 10" in reason


# --- import and comparison ---

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
        # New applets, as VCC answers the first recorded saves (#4).
        new = {"CallRecording": {"DelayTransfer": "Transferred", "Function": "Start", "NextApplet": "",
                                 "NextAppletGuid": None},
               "LoopCounter": {"MaximumLoopCount": 1, "NextAppletIdOnMaximumReached": None,
                               "NextAppletGuidOnMaximumReached": None, "NextApplet": None, "NextAppletGuid": None}}
        t = self.applets[applet["name"]]
        return {"appletProperties": {"AppletType": t, "AppletName": applet["name"], **new.get(t, {})}}

    def save_applet(self, plan_id, name, props):
        self.writes.append(("save", name, json.loads(json.dumps(props))))

    def set_initial_applet(self, plan_id, name):
        self.writes.append(("initial", name))


def test_import_saves_both_with_the_targets_ids_and_guids(tmp_path, monkeypatch):
    monkeypatch.setattr("vccflow.importer.export.export_plan",
                        lambda *a, **k: {"plan": {"initialApplet": "LC Loop"}, "applets": []})
    lc = exported({**LC, "MaximumLoopCount": 10}, "Loop Counter")
    cr = exported(CR, "Call Recording")
    other = {"name": "LC test", "type": "SetDataSource", "settings": {"DataSources": []}, "next": {}}
    doc = {"format": "vccflow.plan/1", "plan": {"name": "Source", "initialApplet": "LC Loop"}, "applets": [cr, lc, other]}
    (tmp_path / "plan.json").write_text(json.dumps(doc), encoding="utf-8")
    portal = FakePortal()
    import_plan(portal, tmp_path, PLAN_ID, "Scratch", log=lambda _: None)
    target = {a["name"]: a for a in portal.list_applets(PLAN_ID)["applets"]}
    saved = {w[1]: w[2] for w in portal.writes if w[0] == "save"}
    assert saved["CR Recording"]["Function"] == "Resume" and saved["CR Recording"]["DelayTransfer"] == "Immediate"
    assert saved["CR Recording"]["NextApplet"] == "LC test"
    assert saved["CR Recording"]["NextAppletGuid"] == target["LC test"]["guid"]
    lc_saved = saved["LC Loop"]
    assert lc_saved["MaximumLoopCount"] == 10
    assert lc_saved["NextApplet"] == "CR Recording" and lc_saved["NextAppletGuid"] == target["CR Recording"]["guid"]
    assert lc_saved["NextAppletIdOnMaximumReached"] == target["LC test"]["id"]
    assert lc_saved["NextAppletGuidOnMaximumReached"] == target["LC test"]["guid"]
    assert "NextAppletOnMaximumReached" not in lc_saved


def test_comparison_reports_changes_both_ways_and_nothing_between_accounts():
    lc, cr = exported(LC, "Loop Counter"), exported(CR, "Call Recording")
    lc2, cr2 = json.loads(json.dumps(lc)), json.loads(json.dumps(cr))
    lc2["settings"]["MaximumLoopCount"] = 5
    lc2["next"]["NextAppletOnMaximumReached"] = "CR Recording"
    cr2["settings"]["Function"] = "Pause"
    doc = lambda *x: {"plan": {}, "applets": [*x, {"name": "LC test", "type": "SetDataSource", "settings": {},
                                                    "next": {}}]}
    diffs = compare_docs(doc(lc, cr), doc(lc2, cr2))["differences"]
    assert "'LC Loop' setting MaximumLoopCount: source 1 | target 5" in diffs
    assert "'LC Loop' path NextAppletOnMaximumReached is different: source 'LC test' | target 'CR Recording'" in diffs
    assert "'CR Recording' setting Function: source Resume | target Pause" in diffs
    assert "'CR Recording' setting Function: source Pause | target Resume" in compare_docs(doc(lc2, cr2), doc(lc, cr))["differences"]
    # Another account: other IDs and GUIDs, the same export.
    listed = [{"id": 7, "guid": "a1", "name": "CR Recording"}, {"id": 8, "guid": "b2", "name": "LC Loop"},
              {"id": 9, "guid": "c3", "name": "LC test"}]
    lc3 = exported({**LC, "NextAppletIdOnMaximumReached": 9, "NextAppletGuidOnMaximumReached": "c3",
                    "NextAppletGuid": "a1", "EntityId": 8, "AppletGuid": "b2", "DialledNumber": "x"}, "Loop Counter", listed)
    assert compare_docs(doc(lc, cr), doc(lc3, cr))["differences"] == []
