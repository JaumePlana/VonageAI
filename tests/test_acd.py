"""ACD / Queue applets (ACD.har): reading, the multipart save, groups, presence states and audio."""

import hashlib
import json
from unittest.mock import MagicMock

import pytest

from vccflow.compare import compare_docs
from vccflow.config import Account
from vccflow import export
from vccflow.export import data_sources_used, export_applet
from vccflow.importer import import_plan, match_groups, match_presence_states, unsupported_reason
from vccflow.portal import PortalClient, PortalError, acd_form, acd_settings, check_acd, extract_acd

PLAN_ID = "b918444e-7c29-41b0-9a65-9a64b8568257"
ACCOUNT = Account(name="Demo29", region="emea", allow_portal_writes=True, portal_account="NVMDemo29")
APPLETS = ["", "DC JavaScript", "DR Empty", "MNU Example", "DC Get Salesforce"]
DATA_SOURCES = ["", "callcenterid", "CallTopic", "AlwaysOpen"]
GROUPS = [("", ""), ("001", "ACD Service (001)"), ("13102022001", "France (13102022001)"),
          ("13102022006", "Roumanie (13102022006)"), ("1001", "Case_Pop (1001)")]
PRESENCE = [("", "Leave unchanged"), ("6bbbebbd-f160-4d8d-96db-411b755a18a4", "Break"),
            ("734966bb-e646-46c3-a122-617765e92f89", "Ready")]
RECORDING = f"recordings/56303/{PLAN_ID}/77d59069-f7b4-4ca8-9b8f-f8eb208ba38aNVMDemo29.wav"


def select(name, options, value=""):
    opts = "".join(f'<option{" selected=\"selected\"" if v == value else ""} value="{v}">{t}</option>'
                   for v, t in options)
    return f'<select name="Acd.{name}" id="Acd_{name}">{opts}</select>'


def text(name, value=""):
    return f'<input type="text" name="Acd.{name}" id="Acd_{name}" value="{value}" />'


def checkbox(name, on=False):
    return (f'<input type="checkbox" name="Acd.{name}" value="true"{" checked=\"checked\"" if on else ""} />'
            f'<input type="hidden" name="Acd.{name}" value="false" />')


def audio(slot, path="", dynamic=None):
    out = select(f"{slot}Dynamic", [(d, d) for d in DATA_SOURCES], dynamic or "") if dynamic is not None else ""
    return (out + f'<input class="inputfile" type="file" name="Acd.{slot}" id="Acd_{slot}" value="{path}" />'
            f'<input type="hidden" name="Acd.{slot}Delete" value="0" />')


def page(saved=False, **v):
    """An ACD page as VCC renders it (recorded structure, shortened). v: field values; music=True: a music file."""
    apps = [(a, f"{a} (x)" if a else "") for a in APPLETS]
    groups = "".join(select(f"Group{i}", GROUPS, v.get(f"Group{i}", "")) +
                     (text(f"TimedOverflow{i}", v.get(f"TimedOverflow{i}", "")) if i and v.get(f"Group{i}") else "")
                     for i in range(10))
    return (
        '<form id="acdForm" method="post" enctype="multipart/form-data">'
        '<input name="__RequestVerificationToken" type="hidden" value="tok" />'
        + (f'<div id="Message" class="normalMessage">This applet has been saved.</div>' if saved else "")
        + f'<input type="hidden" name="Acd.DialledNumber" value="{PLAN_ID}" />'
        '<input type="hidden" name="Acd.ApplicationName" value="My Queue" />'
        + select("CallRouterAlgorithm", [("0", "Longest"), ("1", "Skills FIFO"), ("2", "Skills LIFO")],
                 v.get("CallRouterAlgorithm", "2"))
        + select("AgentTiebreakAlgorithm", [("1", "Random"), ("0", "Time")], v.get("AgentTiebreakAlgorithm", "0"))
        + checkbox("PreferLastHandlingAgent", v.get("PreferLastHandlingAgent")) + checkbox("PlayTargetAnswerTime")
        + checkbox("PlayRecentAnswerTime") + checkbox("PlayTargetAnswerTimeWhenRecentUnavailable")
        + text("MaxEstimatedWaitPeriod") + select("MaxEstimatedWaitApplication", apps)
        + checkbox("ShouldPlayQueuePosition") + text("AgePerMinute", "1")
        + checkbox("UnlimitedQueueLength", v.get("UnlimitedQueueLength", True)) + text("MaxQueueLength", v.get("MaxQueueLength", ""))
        + select("MaxQueueApplet", apps, v.get("MaxQueueApplet", "DC JavaScript"))
        + select("BreakoutAvailable", [("0", "Always"), ("1", "First Retry")], "0") + checkbox("BreakoutReenter")
        + "".join(select(f"App{i}", apps, "MNU Example" if i == 8 else "") for i in range(10))
        + audio("Announcement1", dynamic="") + audio("Announcement2", dynamic="") + audio("Announcement3", dynamic="")
        + audio("SubsequentAnnouncement", dynamic="") + audio("CurPosAnnouncement")
        + audio("MusicFile", RECORDING if v.get("music", True) else "", dynamic="") + text("MusicDuration", v.get("MusicDuration", "7"))
        + text("NoAnswerTimeoutAsSeconds", "9999")
        + select("PostUnexpectedPresenceStateGuid", PRESENCE, "6bbbebbd-f160-4d8d-96db-411b755a18a4")
        + select("AgentHangupApplet", apps, "DR Empty") + select("NoAgentsLoggedInApplet", apps, "DC Get Salesforce")
        + select("AgentRejectApplet", apps) + text("QueueTimeoutAsSeconds") + select("QueueTimeoutApplet", apps, "DC JavaScript")
        + audio("OnHoldAnnouncement") + audio("RingingAnnouncement") + audio("WhisperAnnouncement", dynamic="callcenterid")
        + select("WhisperOption", [("Off", "Off"), ("AgentOnly", "Agent Only")], "AgentOnly")
        + select("ObjectPopDataSource", [(d, d or "Off") for d in DATA_SOURCES])
        + groups + text("WrapupTimeAsSeconds") + select("PostWrapPresenceStateGuid", PRESENCE, "734966bb-e646-46c3-a122-617765e92f89")
        + checkbox("SalesforceTransfer") + text("SalesForceDialledNumber") + text("FlowDevName") + text("FallBackQueue")
        + "</form>")


MY_QUEUE = page(Group0="13102022001", Group1="13102022006", TimedOverflow1="0", Group2="1001", TimedOverflow2="15")


def exported():
    return acd_settings(extract_acd(MY_QUEUE))


# --- reading ---

def test_page_is_read_with_connections_groups_and_presence_states_by_name():
    settings, nxt = exported()
    assert nxt == {"MaxQueueApplet": "DC JavaScript", "App8": "MNU Example", "AgentHangupApplet": "DR Empty",
                   "NoAgentsLoggedInApplet": "DC Get Salesforce", "QueueTimeoutApplet": "DC JavaScript"}
    assert settings["Groups"] == [
        {"Slot": 0, "Name": "France", "DisplayId": "13102022001", "TimedOverflow": ""},
        {"Slot": 1, "Name": "Roumanie", "DisplayId": "13102022006", "TimedOverflow": "0"},
        {"Slot": 2, "Name": "Case_Pop", "DisplayId": "1001", "TimedOverflow": "15"}]
    assert (settings["PostUnexpectedPresenceState"], settings["PostWrapPresenceState"]) == ("Break", "Ready")
    assert not any("Guid" in k for k in settings)  # GUIDs differ per account
    assert settings["UnlimitedQueueLength"] is True and settings["PreferLastHandlingAgent"] is False
    assert settings["WhisperAnnouncementDynamic"] == "callcenterid" and settings["CallRouterAlgorithm"] == "2"
    assert extract_acd(MY_QUEUE)["audio"] == {"MusicFile": RECORDING}


def test_data_sources_of_the_audio_and_the_object_pop():
    settings, _ = exported()
    settings.update(Announcement2Dynamic="CallTopic", ObjectPopDataSource="AlwaysOpen")
    assert data_sources_used({"type": "ACD", "settings": settings}) == {"callcenterid", "CallTopic", "AlwaysOpen"}


def test_export_downloads_each_audio_file():
    portal = MagicMock()
    portal.get_acd.return_value = extract_acd(MY_QUEUE)
    portal.download_recording.return_value = b"RIFF-music"
    applet = {"name": "My Queue", "appletType": "ACD", "configUrl": f"ACD?phoneNum={PLAN_ID}&myApp=My Queue&application=ACD"}
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        entry = export_applet(portal, PLAN_ID, applet, Path(tmp))
        assert (Path(tmp) / "My Queue MusicFile.wav").read_bytes() == b"RIFF-music"
    portal.download_recording.assert_called_once_with(RECORDING)
    assert entry["type"] == "ACD" and entry["media"] == [
        {"property": "MusicFile", "file": "media/My Queue MusicFile.wav", "name": None,
         "sha256": hashlib.sha256(b"RIFF-music").hexdigest()}]


# --- the form ---

IDENTITY = {"DialledNumber": PLAN_ID, "ApplicationName": "My Queue"}
PRESENCE_IDS = {t: v for v, t in PRESENCE if v}


def names(form):
    return [k for k, _ in form]


def test_form_is_posted_in_the_recorded_order_with_disabled_fields_left_out():
    settings, nxt = exported()
    form = dict((k, v) for k, v in acd_form(settings, nxt, IDENTITY, PRESENCE_IDS, existing_audio={"MusicFile"}))
    order = names(acd_form(settings, nxt, IDENTITY, PRESENCE_IDS, existing_audio={"MusicFile"}))
    assert order[:4] == ["Acd.DialledNumber", "Acd.ApplicationName", "Acd.CallRouterAlgorithm", "Acd.AgentTiebreakAlgorithm"]
    assert order.count("Acd.UnlimitedQueueLength") == 2 and "Acd.MaxQueueLength" not in order  # checked: true + false
    assert order.count("Acd.PreferLastHandlingAgent") == 1
    assert order[-4:] == ["Acd.SalesforceTransfer", "Acd.SalesForceDialledNumber", "Acd.FlowDevName", "Acd.FallBackQueue"]
    assert order.index("Acd.MusicDuration") == order.index("Acd.MusicFileDelete") + 1
    assert [n for n in order if n.startswith("Acd.TimedOverflow")] == ["Acd.TimedOverflow1", "Acd.TimedOverflow2"]
    assert form["Acd.Group2"] == "1001" and form["Acd.Group3"] == "" and form["Acd.TimedOverflow2"] == "15"
    assert form["Acd.PostUnexpectedPresenceStateGuid"] == "6bbbebbd-f160-4d8d-96db-411b755a18a4"
    assert form["Acd.MusicFile"] == ("", b"", "application/octet-stream")  # keeps the file already there
    assert "Acd.WrapupTimeAsSeconds" in form and form["Acd.WhisperOption"] == "AgentOnly"


def test_conditional_fields():
    settings, nxt = exported()
    settings.update(UnlimitedQueueLength=False, MaxQueueLength="4", CallRouterAlgorithm="0", WhisperAnnouncementDynamic="")
    order = names(acd_form(settings, nxt, IDENTITY, PRESENCE_IDS))  # no audio at all
    assert order[order.index("Acd.UnlimitedQueueLength") + 1] == "Acd.MaxQueueLength"
    assert "Acd.WrapupTimeAsSeconds" not in order  # "longest waiting" has no wrap-up time
    assert "Acd.MusicDuration" not in order and "Acd.WhisperOption" not in order
    upload = acd_form(settings, nxt, IDENTITY, PRESENCE_IDS, uploads={"WhisperAnnouncement": ("w.wav", b"RIFF")})
    assert dict(upload)["Acd.WhisperAnnouncement"] == ("w.wav", b"RIFF", "audio/wav") and "Acd.WhisperOption" in names(upload)


@pytest.mark.parametrize("change, message", [
    ({"CallRouterAlgorithm": "3"}, "algorithm '3' has not been recorded"),
    ({"SalesforceTransfer": True}, "Salesforce transfer"),
    ({"FlowDevName": "MyFlow"}, "Omni-Channel"),
    ({"Groups": [{"Slot": 0, "Name": "A", "DisplayId": "1"}, {"Slot": 1, "Name": "B", "DisplayId": "1"}]}, "same group twice"),
])
def test_unrecorded_cases_are_refused(change, message):
    settings, _ = exported()
    with pytest.raises(PortalError, match=message):
        check_acd({**settings, **change})
    assert unsupported_reason({"name": "Q", "type": "ACD", "settings": {**settings, **change}})


def test_unknown_presence_state_is_refused_when_building_the_form():
    settings, nxt = exported()
    with pytest.raises(PortalError, match="presence state 'Break' does not exist"):
        acd_form(settings, nxt, IDENTITY, {"Ready": "x"})


# --- saving ---

def client(*responses):
    session = MagicMock()
    session.get.return_value = MagicMock(status_code=200, text="var user = { account: 'NVMDemo29' };")
    session.request.side_effect = [MagicMock(status_code=200, is_redirect=False, headers={}, text=t) for t in responses]
    return PortalClient(ACCOUNT, session=session), session


def test_save_posts_one_multipart_form_with_the_token_first_and_the_audio(tmp_path):
    settings, nxt = exported()
    wav = tmp_path / "My Queue MusicFile.wav"
    wav.write_bytes(b"RIFF-music")
    portal, session = client(page(music=False), MY_QUEUE.replace("<form", '<div id="Message">This applet has been saved.</div><form'))
    portal.save_acd(PLAN_ID, "My Queue", settings, nxt, {"MusicFile": wav})
    get, post = session.request.call_args_list
    assert get.args[1].endswith(f"ACD?phoneNum={PLAN_ID}&myApp=My%20Queue&application=ACD")
    assert post.args[1].endswith(f"ACD?phoneNum={PLAN_ID}&myApp=My%20Queue") and "data" not in post.kwargs
    files = post.kwargs["files"]
    assert files[0] == ("__RequestVerificationToken", (None, "tok"))
    assert dict(files)["Acd.MusicFile"] == ("My Queue MusicFile.wav", b"RIFF-music", "audio/wav")
    assert dict(files)["Acd.MusicDuration"] == (None, "7")


def test_save_refuses_a_group_the_page_does_not_offer():
    settings, nxt = exported()
    settings["Groups"].append({"Slot": 3, "Name": "Gone", "DisplayId": "999", "TimedOverflow": "0"})
    portal, session = client(page())
    with pytest.raises(PortalError, match=r"groups not offered by the page: Gone \(999\)"):
        portal.save_acd(PLAN_ID, "My Queue", settings, nxt)
    assert len(session.request.call_args_list) == 1  # no post


def test_save_without_confirmation_is_an_error():
    settings, nxt = exported()
    portal, _ = client(page(), page())
    with pytest.raises(PortalError, match="was not confirmed"):
        portal.save_acd(PLAN_ID, "My Queue", settings, nxt)


# --- groups and presence states ---

def acd_with(*groups, **presence):
    settings, nxt = exported()
    settings["Groups"] = [{"Slot": i, "Name": n, "DisplayId": d, "TimedOverflow": ""} for i, (n, d) in enumerate(groups)]
    settings.update(presence)
    return {"name": "My Queue", "type": "ACD", "settings": settings, "next": nxt}


TARGET_GROUPS = [{"groupDisplayId": "001", "groupName": "ACD Service"}, {"groupDisplayId": "5001", "groupName": "France"},
                 {"groupDisplayId": "1001", "groupName": "Cases"}]


def test_groups_match_by_name_then_by_id_else_are_created():
    a = acd_with(("ACD Service", "001"), ("France", "13102022001"), ("Case_Pop", "1001"), ("Roumanie", "13102022006"))
    warnings, create = match_groups([a], TARGET_GROUPS)
    assert [(g["Name"], g["DisplayId"]) for g in a["settings"]["Groups"]] == [
        ("ACD Service", "001"), ("France", "5001"), ("Cases", "1001"), ("Roumanie", "13102022006")]
    assert warnings == [
        "group 'France' has ID 13102022001 in the source but 5001 in the target account: matched by name (used by My Queue)",
        "group 1001 is named 'Case_Pop' in the source but 'Cases' in the target account: matched by ID (used by My Queue)",
        "group 'Roumanie' (13102022006) does not exist in the target account by ID or name (used by My Queue)"]
    assert create == [{"groupDisplayId": "13102022006", "groupName": "Roumanie"}]


def test_name_match_wins_over_an_id_used_by_another_group():
    a = acd_with(("France", "001"))  # 001 is "ACD Service" in the target, "France" is 5001
    match_groups([a], TARGET_GROUPS)
    assert a["settings"]["Groups"][0]["DisplayId"] == "5001"


def test_presence_states_match_by_name_else_are_left_unchanged():
    a = acd_with(PostUnexpectedPresenceState="break", PostWrapPresenceState="Lunch")
    warnings = match_presence_states([a], {"Break": "g1", "Ready": "g2"})
    assert (a["settings"]["PostUnexpectedPresenceState"], a["settings"]["PostWrapPresenceState"]) == ("Break", "")
    assert warnings == ["ACD 'My Queue': presence state 'Lunch' (after wrap-up presence) does not exist in the target "
                        "account: left as 'Leave unchanged'"]


# --- copying ---

class FakePortal:
    account = ACCOUNT

    def __init__(self, lookups=None):
        self.applets, self.writes, self.lookups = {}, [], lookups

    def list_applets(self, plan_id):
        return {"name": "Scratch", "applets": [{"id": i, "name": n, "guid": f"g-{n}", "appletType": t, "configUrl": "x"}
                                               for i, (n, t) in enumerate(self.applets.items())]}

    def list_data_sources(self, plan_id):
        return {"callcenterid"}

    def acd_lookups(self, plan_id):
        return self.lookups

    def get_acd(self, applet):
        return extract_acd(MY_QUEUE)

    def create_applet(self, plan_id, applet_type, name):
        self.writes.append(("create", name, applet_type))
        self.applets[name] = applet_type

    def delete_applet(self, plan_id, name):
        self.writes.append(("delete", name))

    def get_applet_state(self, plan_id, applet):
        return {"appletProperties": {"AppletType": self.applets[applet["name"]]}}

    def save_applet(self, plan_id, name, props):
        self.writes.append(("save", name))

    def save_acd(self, plan_id, name, settings, nxt, audio):
        self.writes.append(("save-acd", name, json.loads(json.dumps(settings)), nxt, sorted(audio)))

    def set_initial_applet(self, plan_id, name):
        self.writes.append(("initial", name))


class FakeGroups:
    def __init__(self, groups):
        self.groups, self.created, self.deleted = list(groups), [], []

    def list(self):
        return self.groups

    def create(self, display_id, name, max_unexpected=500, max_wrap_up=500):
        g = {"groupId": f"id-{display_id}", "groupDisplayId": display_id, "groupName": name,
             "groupMaxUnexpectedTime": max_unexpected, "groupMaxWrapUpTime": max_wrap_up}
        self.created.append(g)
        return g

    def delete(self, group_id):
        self.deleted.append(group_id)


def write_doc(tmp_path, acd, media=True, groups=None):
    others = [{"name": n, "type": "SetDataSource", "settings": {"DataSources": []}, "next": {}}
              for n in ("DC JavaScript", "DR Empty", "MNU Example", "DC Get Salesforce")]
    if media:
        (tmp_path / "media").mkdir()
        (tmp_path / "media" / "My Queue MusicFile.wav").write_bytes(b"RIFF")
        acd["media"] = [{"property": "MusicFile", "file": "media/My Queue MusicFile.wav", "name": None, "sha256": "x"}]
    doc = {"format": "vccflow.plan/1", "plan": {"name": "Source", "initialApplet": "My Queue"}, "applets": [acd, *others]}
    if groups is not None:
        doc["requires"] = {"groups": groups}
    (tmp_path / "plan.json").write_text(json.dumps(doc), encoding="utf-8")


LOOKUPS = {"groups": TARGET_GROUPS, "presence": {"Break": "g1", "Ready": "g2"}}


def test_import_creates_missing_groups_and_saves_with_the_target_groups_and_audio(tmp_path, monkeypatch):
    write_doc(tmp_path, acd_with(("France", "13102022001"), ("Roumanie", "13102022006")))
    portal, groups, logs = FakePortal(LOOKUPS), FakeGroups(TARGET_GROUPS), []
    monkeypatch.setattr("vccflow.importer.export.export_plan", lambda *a, **k: {"plan": {"initialApplet": "My Queue"},
                                                                                  "applets": []})
    import_plan(portal, tmp_path, PLAN_ID, "Scratch", log=logs.append, groups=groups)
    assert [g["groupName"] for g in groups.created] == ["Roumanie"]
    assert any("NEW GROUP created in the target account: 'Roumanie' (13102022006, " in m for m in logs)
    assert any("matched by name" in m for m in logs)
    saved = next(w for w in portal.writes if w[0] == "save-acd")
    assert [g["DisplayId"] for g in saved[2]["Groups"]] == ["5001", "13102022006"] and saved[4] == ["MusicFile"]


def test_missing_group_without_the_user_admin_api_stops_before_any_write(tmp_path):
    write_doc(tmp_path, acd_with(("Roumanie", "13102022006")))
    portal = FakePortal(LOOKUPS)
    with pytest.raises(PortalError, match=r"cannot be created without the User Admin API.*'Roumanie' \(13102022006\)"):
        import_plan(portal, tmp_path, PLAN_ID, "Scratch", log=lambda _: None)
    assert portal.writes == []


def test_failed_import_deletes_the_groups_it_created(tmp_path):
    write_doc(tmp_path, acd_with(("Roumanie", "13102022006")))

    class Failing(FakePortal):
        def save_acd(self, *a):
            raise PortalError("refused")

    groups = FakeGroups(TARGET_GROUPS)
    with pytest.raises(PortalError, match="refused"):
        import_plan(Failing(LOOKUPS), tmp_path, PLAN_ID, "Scratch", log=lambda _: None, groups=groups)
    assert groups.deleted == ["id-13102022006"]


def test_without_an_acd_in_the_target_lookups_come_from_the_created_one(tmp_path, monkeypatch):
    write_doc(tmp_path, acd_with(("France", "13102022001"), PostWrapPresenceState="Lunch"), media=False)
    portal, logs = FakePortal(None), []
    monkeypatch.setattr("vccflow.importer.export.export_plan", lambda *a, **k: {"plan": {"initialApplet": "My Queue"},
                                                                                  "applets": []})
    import_plan(portal, tmp_path, PLAN_ID, "Scratch", log=logs.append)
    saved = next(w for w in portal.writes if w[0] == "save-acd")
    assert saved[2]["PostWrapPresenceState"] == "" and saved[2]["Groups"][0]["DisplayId"] == "13102022001"
    assert any("presence state 'Lunch'" in m and "Leave unchanged" in m for m in logs)


def test_dry_run_reports_groups_to_create(tmp_path):
    write_doc(tmp_path, acd_with(("Roumanie", "13102022006")))
    portal, groups, logs = FakePortal(LOOKUPS), FakeGroups(TARGET_GROUPS), []
    import_plan(portal, tmp_path, PLAN_ID, "Scratch", dry_run=True, log=logs.append, groups=groups)
    assert portal.writes == [] and groups.created == []
    assert any("1 group would be created" in m and "'Roumanie' (13102022006" in m for m in logs)


# The source's definition, as the User Admin API lists it (userAdminAPI_GroupsMax.har #4, #8).
ROUMANIE = {"groupDisplayId": "13102022006", "groupName": "Roumanie", "groupMaxUnexpectedTime": 60,
            "groupMaxWrapUpTime": 23}


def test_export_keeps_the_definitions_of_the_groups_the_acds_use():
    listed = [{**ROUMANIE, "groupId": "g-1"}, {"groupId": "g-2", "groupDisplayId": "1", "groupName": "Other",
                                               "groupMaxUnexpectedTime": 5, "groupMaxWrapUpTime": 5}]
    applets = [acd_with(("Roumanie", "13102022006"), ("Gone", "777"))]
    logs = []
    assert export._used_groups(FakeGroups(listed), applets, logs.append) == [ROUMANIE]
    assert logs == ["    WARNING: group 'Gone' (777) used by an ACD is not in the account's group list"]
    assert export._used_groups(None, applets, logs.append) == []


def test_a_missing_group_is_created_with_the_source_maximum_times(tmp_path, monkeypatch):
    write_doc(tmp_path, acd_with(("Roumanie", "13102022006")), groups=[ROUMANIE])
    portal, groups, logs = FakePortal(LOOKUPS), FakeGroups(TARGET_GROUPS), []
    monkeypatch.setattr("vccflow.importer.export.export_plan", lambda *a, **k: {"plan": {"initialApplet": "My Queue"},
                                                                                  "applets": []})
    import_plan(portal, tmp_path, PLAN_ID, "Scratch", log=logs.append, groups=groups)
    assert [(g["groupMaxUnexpectedTime"], g["groupMaxWrapUpTime"]) for g in groups.created] == [(60, 23)]
    assert ("   NEW GROUP created in the target account: 'Roumanie' (13102022006, maximum unexpected time 60 s, "
            "wrap-up time 23 s)") in logs
    assert not any("no definition" in m for m in logs)


def test_without_the_source_definition_a_group_gets_vcc_admins_default(tmp_path):
    write_doc(tmp_path, acd_with(("Roumanie", "13102022006")))
    portal, groups, logs = FakePortal(LOOKUPS), FakeGroups(TARGET_GROUPS), []
    import_plan(portal, tmp_path, PLAN_ID, "Scratch", dry_run=True, log=logs.append, groups=groups)
    assert ("WARNING: the export has no definition of 1 group ('Roumanie' (13102022006); exported without the "
            "User Admin API, users:read): it is created with VCC Admin's default maximum times, 500 seconds") in logs
    assert any("'Roumanie' (13102022006, maximum unexpected time 500 s, wrap-up time 500 s)" in m for m in logs)


# --- comparing ---

def test_comparison_reports_groups_presence_and_paths_both_ways():
    a = {**acd_with(("France", "13102022001")), "kind": "ACD"}
    b = json.loads(json.dumps(a))
    b["settings"]["Groups"][0]["Name"] = "Spain"
    b["settings"]["PostWrapPresenceState"] = "Break"
    del b["next"]["App8"]
    doc = lambda x: {"plan": {}, "applets": [x]}
    diffs = compare_docs(doc(a), doc(b))["differences"]
    assert "'My Queue' group 1: source France (13102022001) | target Spain (13102022001)" in diffs
    assert "'My Queue' setting PostWrapPresenceState: source Ready | target Break" in diffs
    assert "'My Queue' path App8 is missing in the target (source goes to 'MNU Example')" in diffs
    back = compare_docs(doc(b), doc(a))["differences"]
    assert "'My Queue' path App8 is missing in the source (target goes to 'MNU Example')" in back
    assert compare_docs(doc(a), doc(json.loads(json.dumps(a))))["differences"] == []


def test_groups_are_compared_slot_by_slot():
    a = {**acd_with(("France", "13102022001"), ("Portugal", "13102022005")), "kind": "ACD"}
    a["settings"]["Groups"][1]["TimedOverflow"] = "10"
    b = {**acd_with(("France", "13102022001"), ("Espagne", "13102022002"), ("Case_Pop", "1001")), "kind": "ACD"}
    b["settings"]["Groups"][1]["TimedOverflow"] = "15"
    doc = lambda x: {"plan": {}, "applets": [x]}
    diffs = [d for d in compare_docs(doc(a), doc(b))["differences"] if "group" in d]
    assert diffs == [
        "'My Queue' group 2: source Portugal (13102022005), overflow after 10 s | target Espagne (13102022002), overflow after 15 s",
        "'My Queue' group 3 Case_Pop (1001) is only in the target"]


def test_fixed_choices_are_reported_as_the_page_words_them():
    a = {**acd_with(("France", "13102022001")), "kind": "ACD"}
    b = json.loads(json.dumps(a))
    b["settings"].update(AgentTiebreakAlgorithm="1", BreakoutAvailable="1", WhisperOption="Off", CallRouterAlgorithm="9")
    doc = lambda x: {"plan": {}, "applets": [x]}
    diffs = compare_docs(doc(a), doc(b))["differences"]
    assert "'My Queue' setting AgentTiebreakAlgorithm: source Time since handling last interaction or changing presence "            "(descending) | target Random" in diffs
    assert "'My Queue' setting BreakoutAvailable: source Always | target First Retry" in diffs
    assert "'My Queue' setting WhisperOption: source Agent Only | target Off" in diffs
    assert "'My Queue' setting CallRouterAlgorithm: source Skills based (last in, first out) | target 9" in diffs
