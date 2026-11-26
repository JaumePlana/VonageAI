"""Callback (callback.har): a React editor saved through Applet/Save; its initial applet may be in another plan."""

import json
from unittest.mock import MagicMock

import pytest

from vccflow.compare import compare_docs
from vccflow.config import Account
from vccflow.export import data_sources_used, export_applet
from vccflow.importer import import_plan, unsupported_reason

PLAN_ID = "2b1fa24b-89d4-449f-ab7c-ca86b01cd841"  # TestDavid, where the recording was made
POC = "b918444e-7c29-41b0-9a65-9a64b8568257"  # POC Claude
ACCOUNT = Account(name="Demo29", region="emea", allow_portal_writes=True, portal_account="NVMDemo29")
LISTED = [{"id": 120698486, "guid": "ec91b743-aa77-11ee-980a-0267ee5b37ba", "name": "ACD"},
          {"id": 165952310, "guid": "88dce5ee-e9be-11f1-8a44-0ab3d743c8e3", "name": "CB My Callback"},
          {"id": 148702902, "guid": "f4cf0c8c-b585-11f0-a707-0240f276eb0d", "name": "SE test"},
          {"id": 148702966, "guid": "1ce0c55a-b586-11f0-a707-0240f276eb0d", "name": "SW test"}]
POC_APPLETS = [{"id": 164243766, "guid": "8a3fbb9b-d6d7-11f1-ab24-0240f276eb0d", "name": "ANN Audio"}]
PLANS = [{"interactionPlanId": PLAN_ID, "interactionPlanName": "TestDavid"},
         {"interactionPlanId": POC, "interactionPlanName": "POC Claude"}]
# VCC's answer to the last save (#21): the initial applet in its own plan, every routing applet set.
PROPS = {"CallbackInitialAppletId": 120698486, "CallbackInitialAppletGuid": "ec91b743-aa77-11ee-980a-0267ee5b37ba",
         "CallbackInitialInteractionPlanId": PLAN_ID, "CallbackNumberProperty": "$(Log),+3465",
         "PresentedNumberProperty": "$(Log),+34", "FailureAppletGuid": "1ce0c55a-b586-11f0-a707-0240f276eb0d",
         "DuplicateCallbackAppletGuid": "1ce0c55a-b586-11f0-a707-0240f276eb0d", "DialledNumber": PLAN_ID,
         "AppletName": "CB My Callback", "DisplayName": "Callback", "AppletType": "Callback", "NextApplet": "SE test",
         "NextAppletGuid": "f4cf0c8c-b585-11f0-a707-0240f276eb0d", "Url": "Applet/Callback", "ConfigurationUrl": "Applet",
         "ApplicationType": 6, "EntityId": 165952310, "AppletGuid": "88dce5ee-e9be-11f1-8a44-0ab3d743c8e3"}
# #6: an applet of another plan (POC Claude's "ANN Audio").
OTHER_PLAN = {"CallbackInitialAppletId": 164243766, "CallbackInitialAppletGuid": "8a3fbb9b-d6d7-11f1-ab24-0240f276eb0d",
              "CallbackInitialInteractionPlanId": POC}
# #8 / #9: "Return to previous queue".
PREVIOUS = {"CallbackInitialAppletId": None, "CallbackInitialAppletGuid": None, "CallbackInitialInteractionPlanId": None}


def exported(props=None, listed=LISTED, plan_id=PLAN_ID):
    portal = MagicMock()
    portal.get_applet_state.return_value = {"appletProperties": json.loads(json.dumps(props or PROPS)), "metadata": {}}
    portal.list_plans.return_value = PLANS
    portal.list_applets.side_effect = lambda pid: {"applets": POC_APPLETS if pid == POC else LISTED}
    applet = {"name": "CB My Callback", "appletType": "Callback", "configUrl": "Applet?application=Callback"}
    return export_applet(portal, plan_id, applet, None, listed)


def test_export_in_its_own_plan_keeps_every_applet_by_name():
    e = exported()
    assert e["type"] == "Callback"
    assert e["settings"] == {"CallbackNumberProperty": "$(Log),+3465", "PresentedNumberProperty": "$(Log),+34"}
    assert e["next"] == {"CallbackInitialApplet": "ACD", "NextApplet": "SE test", "DuplicateCallbackApplet": "SW test",
                         "FailureApplet": "SW test"}
    assert "connectionProblems" not in e and unsupported_reason(e) is None


def test_export_of_an_initial_applet_in_another_plan_keeps_the_plan_and_applet_by_name():
    e = exported({**PROPS, **OTHER_PLAN})
    assert e["settings"]["CallbackInitialPlan"] == "POC Claude" and e["settings"]["CallbackInitialApplet"] == "ANN Audio"
    assert "CallbackInitialApplet" not in e["next"]
    assert "connectionProblems" not in e and unsupported_reason(e) is None


def test_return_to_previous_queue_has_no_initial_applet():
    e = exported({**PROPS, **PREVIOUS})
    assert "CallbackInitialApplet" not in e["next"] and "CallbackInitialPlan" not in e["settings"]


def test_a_new_callback_exports_and_imports_as_it_is_created():
    # Save #4: every field null.
    new = {**PROPS, **PREVIOUS, "CallbackNumberProperty": None, "PresentedNumberProperty": None,
           "FailureAppletGuid": None, "DuplicateCallbackAppletGuid": None, "NextApplet": None, "NextAppletGuid": None}
    e = exported(new)
    assert e["next"] == {} and unsupported_reason(e) is None


def test_the_numbers_are_free_text_and_their_data_sources_are_read():
    e = exported()
    assert data_sources_used(e) == {"Log"}
    e["settings"]["CallbackNumberProperty"] = "anything, abc"  # #11: any text is saved
    assert unsupported_reason(e) is None


@pytest.mark.parametrize("change, message", [
    (lambda e: e["settings"].update(CallbackNumberProperty="1" * 101), "not text of up to 100 characters"),
    (lambda e: e["settings"].update(Other=1), "settings Other have not been recorded yet"),
    (lambda e: e["settings"].update(CallbackInitialPlan="POC Claude"), "needs both the plan and the applet"),
    (lambda e: e["next"].update(CallbackInitialApplet="CB My Callback"), "the applet itself"),
])
def test_what_was_not_recorded_is_refused(change, message):
    e = exported()
    change(e)
    assert message in unsupported_reason(e)


# --- import ---

class FakePortal:
    account = ACCOUNT

    def __init__(self, plans=PLANS, other_applets=POC_APPLETS):
        self.applets, self.writes, self.plans, self.other_applets = {}, [], plans, other_applets

    def list_plans(self):
        return self.plans

    def list_applets(self, plan_id):
        if plan_id == POC:
            return {"applets": self.other_applets}
        return {"name": "Scratch", "applets": [{"id": 100 + i, "guid": f"g-{n}", "name": n, "appletType": t}
                                               for i, (n, t) in enumerate(self.applets.items())]}

    def list_data_sources(self, plan_id):
        return {"Log"}

    def create_applet(self, plan_id, applet_type, name):
        self.writes.append(("create", name, applet_type))
        self.applets[name] = applet_type

    def delete_applet(self, plan_id, name):
        self.writes.append(("delete", name))

    def get_applet_state(self, plan_id, applet):
        # A new callback, as VCC answers the first recorded save (#4).
        return {"appletProperties": {"AppletType": self.applets[applet["name"]], "AppletName": applet["name"],
                                     **PREVIOUS, "CallbackNumberProperty": None, "PresentedNumberProperty": None,
                                     "FailureAppletGuid": None, "DuplicateCallbackAppletGuid": None,
                                     "NextApplet": None, "NextAppletGuid": None}}

    def save_applet(self, plan_id, name, props):
        self.writes.append(("save", name, json.loads(json.dumps(props))))

    def set_initial_applet(self, plan_id, name):
        self.writes.append(("initial", name))


def run_import(tmp_path, monkeypatch, callback, portal):
    monkeypatch.setattr("vccflow.importer.export.export_plan",
                        lambda *a, **k: {"plan": {"initialApplet": "CB My Callback"}, "applets": []})
    others = [{"name": n, "type": "SetDataSource", "settings": {"DataSources": []}, "next": {}}
              for n in ("ACD", "SE test", "SW test")]
    doc = {"format": "vccflow.plan/1", "plan": {"name": "Source", "initialApplet": "CB My Callback"},
           "applets": [callback, *others]}
    (tmp_path / "plan.json").write_text(json.dumps(doc), encoding="utf-8")
    logs = []
    import_plan(portal, tmp_path, "scratch-plan", "Scratch", log=logs.append)
    return next(w[2] for w in portal.writes if w[0] == "save" and w[1] == "CB My Callback"), logs


def test_import_in_the_same_plan_posts_the_targets_id_and_guids_as_the_editor_does(tmp_path, monkeypatch):
    portal = FakePortal()
    props, _ = run_import(tmp_path, monkeypatch, exported(), portal)
    target = {a["name"]: a for a in portal.list_applets("scratch-plan")["applets"]}
    # #21: the ID only; VCC fills in the GUID and the plan.
    assert props["CallbackInitialAppletId"] == target["ACD"]["id"]
    assert props["CallbackInitialAppletGuid"] is None and props["CallbackInitialInteractionPlanId"] is None
    assert props["NextApplet"] == "SE test" and props["NextAppletGuid"] == target["SE test"]["guid"]
    assert props["DuplicateCallbackAppletGuid"] == props["FailureAppletGuid"] == target["SW test"]["guid"]
    assert props["CallbackNumberProperty"] == "$(Log),+3465" and props["PresentedNumberProperty"] == "$(Log),+34"
    for extra in ("CallbackInitialApplet", "DuplicateCallbackApplet", "FailureApplet", "FailureAppletId"):
        assert extra not in props


def test_import_of_another_plans_applet_finds_it_by_name_in_the_target_account(tmp_path, monkeypatch):
    portal = FakePortal(other_applets=[{"id": 777, "guid": "x", "name": "ANN Audio"}])
    props, logs = run_import(tmp_path, monkeypatch, exported({**PROPS, **OTHER_PLAN}), portal)
    assert props["CallbackInitialAppletId"] == 777
    assert "CallbackInitialPlan" not in props and "CallbackInitialApplet" not in props
    assert not any("Return to previous queue" in m for m in logs)


def test_an_initial_applet_in_a_deleted_plan_is_reported_and_not_copied(tmp_path, monkeypatch):
    # Demo29 "TestDavid" "CB": its initial applet is in a plan that no longer exists.
    e = exported({**PROPS, **OTHER_PLAN, "CallbackInitialInteractionPlanId": "deleted-plan"})
    assert e["connectionProblems"] == ["'CB My Callback' callback initial applet 164243766 is not in Interaction Plan "
                                       "deleted-plan"]
    props, logs = run_import(tmp_path, monkeypatch, e, FakePortal())
    assert props["CallbackInitialAppletId"] is None
    assert ("WARNING: 'CB My Callback' has broken references in the source, not copied: 'CB My Callback' "
            "callback initial applet 164243766 is not in Interaction Plan deleted-plan") in logs


@pytest.mark.parametrize("plans, applets, why", [
    ([], POC_APPLETS, "Interaction Plan 'POC Claude' does not exist in the target account"),
    (PLANS, [], "applet 'ANN Audio' does not exist in Interaction Plan 'POC Claude' of the target account"),
    (PLANS + [{"interactionPlanId": "p2", "interactionPlanName": "POC Claude"}], POC_APPLETS,
     "the target account has 2 Interaction Plans named 'POC Claude'"),
])
def test_an_initial_applet_missing_in_the_target_is_saved_as_return_to_previous_with_a_warning(
        tmp_path, monkeypatch, plans, applets, why):
    portal = FakePortal(plans=plans, other_applets=applets)
    props, logs = run_import(tmp_path, monkeypatch, exported({**PROPS, **OTHER_PLAN}), portal)
    assert props["CallbackInitialAppletId"] is None
    assert (f"WARNING: Callback 'CB My Callback' is copied with 'Return to previous queue' as its initial applet: "
            f"{why}. Set it in the portal.") in logs


def test_comparison_reports_changes_both_ways_and_nothing_between_accounts():
    a = {**exported(), "kind": "Callback"}
    b = json.loads(json.dumps(a))
    b["settings"]["PresentedNumberProperty"] = "+44"
    b["next"]["FailureApplet"] = "ACD"
    doc = lambda x: {"plan": {}, "applets": [x, *({"name": n, "type": "SetDataSource", "settings": {}, "next": {}}
                                                  for n in ("ACD", "SE test", "SW test"))]}
    diffs = compare_docs(doc(a), doc(b))["differences"]
    assert "'CB My Callback' setting PresentedNumberProperty: source $(Log),+34 | target +44" in diffs
    assert "'CB My Callback' path FailureApplet is different: source 'SW test' | target 'ACD'" in diffs
    assert ("'CB My Callback' setting PresentedNumberProperty: source +44 | target $(Log),+34"
            in compare_docs(doc(b), doc(a))["differences"])
    # Another plan in another account: other IDs, GUIDs and plan ID, the same export.
    listed = [{"id": 1, "guid": "00000000-0000-0000-0000-000000000001", "name": "ACD"},
              {"id": 2, "guid": "00000000-0000-0000-0000-000000000002", "name": "SE test"},
              {"id": 3, "guid": "00000000-0000-0000-0000-000000000003", "name": "SW test"}]
    other = {**PROPS, "CallbackInitialAppletId": 1, "CallbackInitialAppletGuid": listed[0]["guid"],
             "CallbackInitialInteractionPlanId": "other-plan", "NextAppletGuid": listed[1]["guid"],
             "FailureAppletGuid": listed[2]["guid"], "DuplicateCallbackAppletGuid": listed[2]["guid"]}
    c = {**exported(other, listed, plan_id="other-plan"), "kind": "Callback"}
    assert compare_docs(doc(a), doc(c))["differences"] == []
