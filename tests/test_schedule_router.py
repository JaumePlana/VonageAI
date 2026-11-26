"""Schedule Router (scheduleRouter.har): the schedule by name, its service statuses, and cloning schedules."""

import json
from unittest.mock import MagicMock

import pytest

from vccflow.compare import compare_docs
from vccflow.config import Account
from vccflow.export import _used_schedules, export_applet, schedules_in_metadata
from vccflow.importer import import_plan, plan_schedules, unsupported_reason
from vccflow.portal import PortalClient, PortalError

PLAN_ID = "b918444e-7c29-41b0-9a65-9a64b8568257"
FAB, TEST = "b5434940-67c7-41c2-b6dd-59c894a893d0", "a1f287dd-42ba-43c1-a9e2-9aaf12a489af"
ACCOUNT = Account(name="Demo29", region="emea", allow_portal_writes=True, portal_account="NVMDemo29")


def mapping(guid, status):
    return {"prop": f"ScheduleMappings[{guid}:{status}]", "label": status, "type": "appletSelector",
            "includeCondition": {"prop": "Schedule", "values": [guid]}}


# The editor's metadata after the last recorded save (#25), shortened.
METADATA = {"container": {"type": "appletContainer", "children": [
    {"type": "group", "label": "Schedule", "children": [
        {"prop": "Schedule", "type": "enumeration",
         "options": [{"value": FAB, "label": "Fab'n'Fit schedule"}, {"value": TEST, "label": "Test Schedule"}]}]},
    {"type": "group", "label": "Mappings", "children": [
        {"prop": "DefaultAppletId", "type": "appletSelector"},
        *(mapping(TEST, s) for s in ("Open", "Closed", "Public Holidays")),
        *(mapping(FAB, s) for s in ("Closed", "Open", "Test", "Test 2"))]}]}}
PROPS = {"Schedule": FAB, "DefaultAppletId": 2, "DefaultAppletGuid": None,
         "ScheduleMappings": {f"{FAB}:Test": 3, f"{FAB}:Test 2": 4}, "ScheduleNextAppletMappings": {},
         "FailureAppletId": 4, "FailureAppletGuid": None, "DialledNumber": PLAN_ID, "AppletName": "Schedule Router",
         "DisplayName": "Schedule Router", "AppletType": "ScheduleRouter", "NextApplet": "", "NextAppletGuid": None,
         "Url": "Applet/ScheduleRouter", "ConfigurationUrl": "Applet", "ApplicationType": 26, "EntityId": 1,
         "AppletGuid": "691ae8db-e982-11f1-8a44-0ab3d743c8e3"}
LISTED = [{"id": 1, "guid": "g1", "name": "Schedule Router"}, {"id": 2, "guid": "g2", "name": "DC JavaScript"},
          {"id": 3, "guid": "g3", "name": "DR Capture Empty"}, {"id": 4, "guid": "g4", "name": "DR No Default"}]


def exported():
    portal = MagicMock()
    portal.get_applet_state.return_value = {"appletProperties": dict(PROPS), "metadata": METADATA}
    applet = {"name": "Schedule Router", "appletType": "Schedule Router", "configUrl": "Applet?application=ScheduleRouter"}
    return export_applet(portal, PLAN_ID, applet, None, LISTED)


# --- reading ---

def test_metadata_lists_each_schedule_with_its_service_statuses():
    assert schedules_in_metadata(METADATA) == [
        {"id": FAB, "name": "Fab'n'Fit schedule", "targets": ["Closed", "Open", "Test", "Test 2"]},
        {"id": TEST, "name": "Test Schedule", "targets": ["Open", "Closed", "Public Holidays"]}]


def test_export_keeps_the_schedule_and_applets_by_name_and_the_schedule_statuses():
    e = exported()
    assert e["type"] == "ScheduleRouter"
    assert e["settings"] == {"Schedule": "Fab'n'Fit schedule", "ScheduleNextAppletMappings": {}}
    assert e["next"] == {"DefaultApplet": "DC JavaScript", "ScheduleMappings.Test": "DR Capture Empty",
                         "ScheduleMappings.Test 2": "DR No Default", "FailureApplet": "DR No Default"}
    assert e["scheduleStatuses"] == ["Closed", "Open", "Test", "Test 2"]


def test_export_saves_the_definitions_of_the_schedules_used():
    client = MagicMock()
    client.list.return_value = [{"id": "x", "name": "Fab'n'Fit schedule", "timezone": "Europe/Madrid", "targets": ["Open"],
                                 "lastUpdated": "now"}, {"id": "y", "name": "Other", "targets": []}]
    found = _used_schedules(client, [exported()], lambda _: None)
    assert found == [{"name": "Fab'n'Fit schedule", "timezone": "Europe/Madrid", "targets": ["Open"]}]
    assert _used_schedules(None, [exported()], lambda _: None) == []


def test_unrecorded_next_applet_mappings_are_refused():
    e = exported()
    e["settings"]["ScheduleNextAppletMappings"] = {"x": "y"}
    assert "has not been recorded" in unsupported_reason(e)
    assert unsupported_reason(exported()) is None


def test_a_refused_react_save_is_an_error():
    session = MagicMock()
    session.get.return_value = MagicMock(status_code=200, text="var user = { account: 'NVMDemo29' };")
    session.request.return_value = MagicMock(status_code=200, is_redirect=False, headers={},
                                             json=lambda: {"errors": ["'Default applet' field is required."]})
    with pytest.raises(PortalError, match="'Schedule Router' was refused: 'Default applet' field is required."):
        PortalClient(ACCOUNT, session=session).save_applet(PLAN_ID, "Schedule Router", PROPS)


# --- choosing the target schedule ---

SOURCE = [{"name": "Fab'n'Fit schedule", "timezone": "Europe/Madrid", "targets": ["Closed", "Open", "Test", "Test 2"]}]


def router(name="Schedule Router"):
    return {**exported(), "name": name}


@pytest.mark.parametrize("target, chosen, warning, created", [
    ([{"name": "Fab'n'Fit schedule", "targets": ["Open", "Test 2", "Closed", "Test"]}], "Fab'n'Fit schedule", None, None),
    ([{"name": "Fab'n'Fit schedule", "targets": ["Open", "Closed", "Test", "Test 2", "Holidays"]}], "Fab'n'Fit schedule",
     "schedule 'Fab'n'Fit schedule' in the target account has more service statuses than in the source: Holidays. "
     "Maybe a routing should be defined for them (they go to the default applet)", None),
    ([{"name": "Fab'n'Fit schedule", "targets": ["Open", "Closed"]}], "zz vccflow Aux Fab'n'Fit schedule",
     "schedule 'Fab'n'Fit schedule' exists in the target account without the service statuses Test, Test 2: it is cloned "
     "as 'zz vccflow Aux Fab'n'Fit schedule' (used by Schedule Router, Second)", "zz vccflow Aux Fab'n'Fit schedule"),
    ([{"name": "fab'n'fit schedule", "targets": ["Open"]}], "Fab'n'Fit schedule",
     "schedule 'Fab'n'Fit schedule' does not exist in the target account (used by Schedule Router, Second)",
     "Fab'n'Fit schedule"),
    ([{"name": "Fab'n'Fit schedule", "targets": ["Open"]},
      {"name": "zz vccflow Aux Fab'n'Fit schedule", "targets": ["Closed", "Open", "Test", "Test 2"]}],
     "zz vccflow Aux Fab'n'Fit schedule", "the auxiliary schedule 'zz vccflow Aux Fab'n'Fit schedule' already there is used", None),
])
def test_the_schedule_is_matched_by_name_and_service_statuses(target, chosen, warning, created):
    picked, warnings, create = plan_schedules([router(), router("Second")], SOURCE, target)
    assert picked == {"Fab'n'Fit schedule": chosen}
    assert (warnings == []) if warning is None else any(warning in w for w in warnings)
    assert [c["name"] for c in create] == ([created] if created else [])
    if created:
        assert create[0]["body"] == SOURCE[0]


def test_a_clone_needs_the_definition_and_a_usable_auxiliary():
    with pytest.raises(PortalError, match="no definition of it to clone"):
        plan_schedules([router()], [], [])
    with pytest.raises(PortalError, match="auxiliary schedule 'zz vccflow Aux Fab'n'Fit schedule' already in the target"):
        plan_schedules([router()], SOURCE, [{"name": "Fab'n'Fit schedule", "targets": ["Open"]},
                                            {"name": "zz vccflow Aux Fab'n'Fit schedule", "targets": ["Open"]}])


# --- copying ---

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
        return {"appletProperties": {"AppletType": self.applets[applet["name"]], "AppletName": applet["name"],
                                     "Schedule": None, "DefaultAppletId": None, "ScheduleMappings": {}}}

    def save_applet(self, plan_id, name, props):
        self.writes.append(("save", name, json.loads(json.dumps(props))))

    def set_initial_applet(self, plan_id, name):
        self.writes.append(("initial", name))


class FakeSchedules:
    def __init__(self, schedules):
        self.schedules, self.created, self.deleted = list(schedules), [], []

    def list(self):
        return self.schedules

    def copy_to(self, source, new_name):
        saved = {**source, "id": f"new-{len(self.created)}", "name": new_name}
        self.schedules.append(saved)
        self.created.append(new_name)
        return saved, source, []

    def delete(self, schedule_id):
        self.deleted.append(schedule_id)


def write_doc(tmp_path, *routers):
    others = [{"name": n, "type": "SetDataSource", "settings": {"DataSources": []}, "next": {}}
              for n in ("DC JavaScript", "DR Capture Empty", "DR No Default")]
    doc = {"format": "vccflow.plan/1", "plan": {"name": "Source", "initialApplet": "DC JavaScript"},
           "requires": {"schedules": SOURCE}, "applets": [*routers, *others]}
    (tmp_path / "plan.json").write_text(json.dumps(doc), encoding="utf-8")


def read_back(monkeypatch):
    monkeypatch.setattr("vccflow.importer.export.export_plan", lambda *a, **k: {"plan": {"initialApplet": "DC JavaScript"},
                                                                                  "applets": []})


def test_import_saves_with_the_target_schedule_guid_and_applet_ids(tmp_path, monkeypatch):
    write_doc(tmp_path, router())
    read_back(monkeypatch)
    portal = FakePortal()
    schedules = FakeSchedules([{"id": "T-GUID", "name": "Fab'n'Fit schedule", "targets": ["Open", "Closed", "Test", "Test 2"]}])
    import_plan(portal, tmp_path, PLAN_ID, "Scratch", log=lambda _: None, schedules=schedules)
    props = next(w[2] for w in portal.writes if w[0] == "save" and w[1] == "Schedule Router")
    ids = {n: 100 + i for i, n in enumerate(portal.applets)}
    assert props["Schedule"] == "T-GUID" and props["DefaultAppletId"] == ids["DC JavaScript"]
    assert props["FailureAppletId"] == ids["DR No Default"]
    assert props["ScheduleMappings"] == {"T-GUID:Test": ids["DR Capture Empty"], "T-GUID:Test 2": ids["DR No Default"]}
    assert props["ScheduleNextAppletMappings"] == {} and schedules.created == []


def test_import_clones_a_missing_schedule_and_reports_it(tmp_path, monkeypatch):
    write_doc(tmp_path, router())
    read_back(monkeypatch)
    portal, schedules, logs = FakePortal(), FakeSchedules([]), []
    import_plan(portal, tmp_path, PLAN_ID, "Scratch", log=logs.append, schedules=schedules)
    assert schedules.created == ["Fab'n'Fit schedule"]
    assert any("NEW SCHEDULE created in the target account: 'Fab'n'Fit schedule'" in m for m in logs)
    props = next(w[2] for w in portal.writes if w[0] == "save" and w[1] == "Schedule Router")
    assert props["Schedule"] == "new-0"


def test_failed_import_deletes_the_schedules_it_created(tmp_path):
    write_doc(tmp_path, router())

    class Failing(FakePortal):
        def save_applet(self, *a):
            raise PortalError("refused")

    schedules = FakeSchedules([])
    with pytest.raises(PortalError, match="refused"):
        import_plan(Failing(), tmp_path, PLAN_ID, "Scratch", log=lambda _: None, schedules=schedules)
    assert schedules.deleted == ["new-0"]


def test_a_router_without_schedule_or_default_is_created_and_not_saved(tmp_path, monkeypatch):
    empty = {**router(), "settings": {"Schedule": None, "ScheduleNextAppletMappings": {}}, "next": {}}
    write_doc(tmp_path, empty)
    read_back(monkeypatch)
    portal, logs = FakePortal(), []
    import_plan(portal, tmp_path, PLAN_ID, "Scratch", log=logs.append)  # no schedules API needed
    assert ("create", "Schedule Router", "ScheduleRouter") in portal.writes
    assert not any(w[0] == "save" and w[1] == "Schedule Router" for w in portal.writes)
    assert any("has no schedule or default applet" in m for m in logs)


def test_without_the_schedules_api_a_router_stops_the_import_before_any_write(tmp_path):
    write_doc(tmp_path, router())
    portal = FakePortal()
    with pytest.raises(PortalError, match="Cannot import the Schedule Routers"):
        import_plan(portal, tmp_path, PLAN_ID, "Scratch", log=lambda _: None)
    assert portal.writes == []


def test_dry_run_reports_the_clone_without_writing(tmp_path):
    write_doc(tmp_path, router())
    portal, schedules, logs = FakePortal(), FakeSchedules([{"name": "Fab'n'Fit schedule", "targets": ["Open"]}]), []
    import_plan(portal, tmp_path, PLAN_ID, "Scratch", dry_run=True, log=logs.append, schedules=schedules)
    assert portal.writes == [] and schedules.created == []
    assert any("1 schedule would be created in the target account: \"zz vccflow Aux Fab'n'Fit schedule\"" in m for m in logs)


# --- comparing ---

def test_comparison_by_schedule_name_and_status_paths_both_ways():
    a = {**exported(), "kind": "Schedule Router"}
    b = json.loads(json.dumps(a))
    b["settings"]["Schedule"] = "Test Schedule"
    b["next"]["ScheduleMappings.Test"] = "DC JavaScript"
    del b["next"]["FailureApplet"]
    doc = lambda x: {"plan": {}, "applets": [x]}
    diffs = compare_docs(doc(a), doc(b))["differences"]
    assert "'Schedule Router' setting Schedule: source Fab'n'Fit schedule | target Test Schedule" in diffs
    assert "'Schedule Router' path ScheduleMappings.Test is different: source 'DR Capture Empty' | target 'DC JavaScript'" in diffs
    assert "'Schedule Router' path FailureApplet is missing in the target (source goes to 'DR No Default')" in diffs
    assert compare_docs(doc(a), doc(json.loads(json.dumps(a))))["differences"] == []
