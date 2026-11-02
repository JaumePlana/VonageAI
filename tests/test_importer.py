import json

import pytest

from vccflow.config import Account
from vccflow.importer import check_plan, compare, import_plan
from vccflow.portal import PortalError

TARGET = "da7310e5-10c5-4080-a8b8-34d7e94323f1"


def plan_doc():
    return {
        "format": "vccflow.plan/1",
        "plan": {"name": "Source", "initialApplet": "ANN A"},
        "applets": [
            {"name": "ANN A", "type": "DataAnnouncer", "settings": {
                "AnnouncementType": "InternalFile",
                "AnnouncementMedia": {"AppletMedia": {"Id": None, "AccountId": 0, "Path": None}, "TextToSpeech": ""}},
             "next": {"NextAppletSuccess": "LOG B"},
             "media": [{"property": "AnnouncementMedia", "file": "media/ANN A.wav", "name": "Ringing"}]},
            {"name": "LOG B", "type": "SetDataSource", "settings": {"DataSources": [{"Key": "LOG", "Value": "x"}]},
             "next": {"NextApplet": "DC C"}},
            {"name": "DC C", "type": "DataRetriever",
             "settings": {"DataProvider": "WebService", "Action": "CallWebService", "ApiInteractionDefinition": "<a/>"},
             "next": {}},
        ],
    }


def write_plan(tmp_path, doc):
    (tmp_path / "media").mkdir()
    (tmp_path / "media" / "ANN A.wav").write_bytes(b"RIFF")
    (tmp_path / "plan.json").write_text(json.dumps(doc), encoding="utf-8")


class FakePortal:
    """Keeps applets in memory; records every write."""

    account = Account(name="JPlana", region="emea", allow_portal_writes=True)

    def __init__(self, fail_on_save=None):
        self.applets = {"ANN Claude POC": {"type": "DataAnnouncer", "props": {}}}
        self.initial = None
        self.writes = []
        self.fail_on_save = fail_on_save

    def list_applets(self, plan_id):
        return {"name": "zz Claude POC", "applets": [
            {"id": 1000 + i, "name": n, "guid": f"guid-{n}", "appletType": a["type"], "configUrl": f"x?myapp={n}"}
            for i, (n, a) in enumerate(self.applets.items())]}

    def create_applet(self, plan_id, applet_type, name):
        self.writes.append(("create", name))
        self.applets[name] = {"type": applet_type, "props": {"AppletType": applet_type, "AppletGuid": f"guid-{name}"}}

    def delete_applet(self, plan_id, name):
        self.writes.append(("delete", name))
        del self.applets[name]

    def get_applet_state(self, plan_id, applet):
        return {"appletProperties": dict(self.applets[applet["name"]]["props"])}

    def save_applet(self, plan_id, name, props):
        if name == self.fail_on_save:
            raise PortalError("boom")
        self.writes.append(("save", name))
        self.applets[name]["props"] = props

    def upload_media(self, plan_id, name, path, filename):
        self.writes.append(("upload", name, filename))
        return 42

    def save_data_connector(self, plan_id, name, settings, nxt):
        self.writes.append(("save-dc", name))
        self.applets[name]["dc"] = (settings, nxt)

    def set_initial_applet(self, plan_id, name):
        self.writes.append(("initial", name))
        self.initial = name

    data_sources = None  # None: the account's list cannot be read

    def list_data_sources(self, plan_id):
        return self.data_sources


def test_check_plan_reports_conflicts_and_dangling_connections(tmp_path):
    doc = plan_doc()
    doc["applets"][1]["next"] = {"NextApplet": "Nowhere"}
    problems = check_plan(doc, tmp_path, {"ANN A"})
    assert any("already exists" in p for p in problems)
    assert any("'Nowhere'" in p for p in problems)
    assert any("media file" in p for p in problems)


def test_dry_run_writes_nothing(tmp_path):
    write_plan(tmp_path, plan_doc())
    portal = FakePortal()
    import_plan(portal, tmp_path, TARGET, "zz Claude POC", dry_run=True, log=lambda _: None)
    assert portal.writes == []


def test_refuses_before_any_write(tmp_path):
    doc = plan_doc()
    doc["applets"][2]["settings"]["DataProvider"] = "Zendesk"
    write_plan(tmp_path, doc)
    portal = FakePortal()
    with pytest.raises(PortalError, match="not been recorded"):
        import_plan(portal, tmp_path, TARGET, "zz Claude POC", log=lambda _: None)
    assert portal.writes == []


def test_import_creates_first_then_wires_by_name(tmp_path, monkeypatch):
    write_plan(tmp_path, plan_doc())
    portal = FakePortal()
    monkeypatch.setattr("vccflow.importer.export.export_plan", lambda *a, **k: plan_doc())
    diffs = import_plan(portal, tmp_path, TARGET, "zz Claude POC", log=lambda _: None)
    assert diffs == []
    kinds = [w[0] for w in portal.writes]
    assert kinds[:3] == ["create"] * 3 and kinds[-1] == "initial"
    ann = portal.applets["ANN A"]["props"]
    assert ann["NextAppletSuccess"] == "LOG B" and ann["NextAppletSuccessGuid"] == "guid-LOG B"
    assert ann["AnnouncementMedia"]["AppletMedia"]["Id"] == 42
    assert ("upload", "ANN A", "Ringing.wav") in portal.writes
    assert portal.applets["DC C"]["dc"][0]["DataProvider"] == "WebService"
    assert portal.initial == "ANN A"


def test_failed_import_deletes_what_it_created(tmp_path):
    write_plan(tmp_path, plan_doc())
    portal = FakePortal(fail_on_save="LOG B")
    with pytest.raises(PortalError, match="boom"):
        import_plan(portal, tmp_path, TARGET, "zz Claude POC", log=lambda _: None)
    assert set(portal.applets) == {"ANN Claude POC"}


def test_compare_ignores_line_endings_and_spots_differences():
    a = plan_doc()
    b = json.loads(json.dumps(a))
    b["applets"][2]["settings"]["ApiInteractionDefinition"] = "<a/>"
    a["applets"][2]["settings"]["ApiInteractionDefinition"] = "<a/>"
    assert compare(a, b) == []
    b["applets"][1]["next"] = {}
    b["plan"]["initialApplet"] = None
    assert len(compare(a, b)) == 2


def with_menu():
    doc = plan_doc()
    doc["applets"].append({"name": "MNU M", "type": "ACD", "settings": {}, "next": {"NextApplet": "ANN A"}})
    doc["applets"][1]["next"] = {"NextApplet": "MNU M"}  # LOG B -> MNU M
    return doc


def test_drop_unsupported_unsets_connections_to_skipped_applets():
    from vccflow.importer import drop_unsupported

    doc, notes = drop_unsupported(with_menu())
    assert [a["name"] for a in doc["applets"]] == ["ANN A", "LOG B", "DC C"]
    assert doc["applets"][1]["next"] == {}
    assert any("skipped 'MNU M'" in n for n in notes) and any("LOG B" in n and "left unset" in n for n in notes)


def test_skip_unsupported_imports_the_rest(tmp_path, monkeypatch):
    write_plan(tmp_path, with_menu())
    portal = FakePortal()
    with pytest.raises(PortalError, match="ACD is not supported"):
        import_plan(portal, tmp_path, TARGET, "zz Claude POC", log=lambda _: None)
    assert portal.writes == []

    expected = plan_doc()
    expected["applets"][1]["next"] = {}
    monkeypatch.setattr("vccflow.importer.export.export_plan", lambda *a, **k: expected)
    diffs = import_plan(portal, tmp_path, TARGET, "zz Claude POC", skip_unsupported=True, log=lambda _: None)
    assert diffs == []
    assert "MNU M" not in portal.applets and "NextApplet" not in portal.applets["LOG B"]["props"]


def doc_reading(*names):
    doc = plan_doc()
    doc["applets"][1]["settings"]["DataSources"] = [{"Key": "LOG", "Value": " ".join(f"$({n})" for n in names)}]
    return doc


def test_missing_data_sources_warn_by_default(tmp_path, monkeypatch):
    doc = doc_reading("Case Country", "LOG", "VoiceCall|ID")
    write_plan(tmp_path, doc)
    portal = FakePortal()
    portal.data_sources = {"VoiceCall|Id"}
    monkeypatch.setattr("vccflow.importer.export.export_plan", lambda *a, **k: doc)
    logs = []
    assert import_plan(portal, tmp_path, TARGET, "zz Claude POC", log=logs.append) == []
    warnings = [m for m in logs if m.startswith("WARNING")]
    # LOG is set by the plan; VoiceCall|ID exists as VoiceCall|Id (names ignore case)
    assert len(warnings) == 1 and "'Case Country' is used by LOG B but" in warnings[0]
    assert any("exists in the target account as 'VoiceCall|Id'" in m for m in logs)
    assert portal.writes  # imported anyway


def test_require_data_sources_stops_before_any_write(tmp_path):
    write_plan(tmp_path, doc_reading("Case Country"))
    portal = FakePortal()
    portal.data_sources = {"log"}
    with pytest.raises(PortalError, match="'Case Country'"):
        import_plan(portal, tmp_path, TARGET, "zz Claude POC", require_data_sources=True, log=lambda _: None)
    assert portal.writes == []


def test_compare_treats_letter_case_as_a_note():
    a = plan_doc()
    b = json.loads(json.dumps(a))
    b["applets"][1]["settings"]["DataSources"][0]["Key"] = "Log"
    notes = []
    assert compare(a, b, notes) == [] and "letter case" in notes[0]


def doc_with_menu():
    doc = plan_doc()
    doc["applets"].append({
        "name": "MNU M", "type": "Menu",
        "settings": {"AnnouncementType": "InternalFile", "Timeout": 10,
                     "AnnouncementMedia": {"AppletMedia": {"Id": None}, "TextToSpeech": ""}},
        "next": {"Choices.0": "LOG B", "Choices.*": "DC C", "TimeoutApplet": "LOG B", "FailureApplet": "DC C"},
        "media": [{"property": "AnnouncementMedia", "file": "media/ANN A.wav", "name": "Menu prompt"}],
    })
    return doc


def test_menu_connections_are_rebuilt_for_the_target(tmp_path, monkeypatch):
    doc = doc_with_menu()
    write_plan(tmp_path, doc)
    portal = FakePortal()
    monkeypatch.setattr("vccflow.importer.export.export_plan", lambda *a, **k: doc)
    assert import_plan(portal, tmp_path, TARGET, "zz Claude POC", log=lambda _: None) == []

    assert portal.applets["MNU M"]["type"] == "Menu"
    props = portal.applets["MNU M"]["props"]
    assert props["Choices"] == {"0": "LOG B", "*": "DC C"}
    assert props["TimeoutAppletName"] == "LOG B" and props["TimeoutAppletGuid"] == "guid-LOG B"
    listed = {a["name"]: a for a in portal.list_applets(TARGET)["applets"]}
    assert props["FailureAppletId"] == listed["DC C"]["id"] and props["FailureAppletGuid"] == "guid-DC C"
    assert props["AnnouncementMedia"]["AppletMedia"]["Id"] == 42
    assert ("upload", "MNU M", "Menu prompt.wav") in portal.writes
    # Connection keys belong to the export format, not to the applet's properties.
    assert not any(k.startswith("Choices.") or k in ("TimeoutApplet", "FailureApplet") for k in props)


def test_menu_choice_to_an_applet_outside_the_plan_is_refused(tmp_path):
    doc = doc_with_menu()
    doc["applets"][-1]["next"]["Choices.1"] = "Nowhere"
    write_plan(tmp_path, doc)
    portal = FakePortal()
    with pytest.raises(PortalError, match="Choices.1 points to 'Nowhere'"):
        import_plan(portal, tmp_path, TARGET, "zz Claude POC", log=lambda _: None)
    assert portal.writes == []


def test_skipping_an_unsupported_applet_removes_it_from_menu_choices():
    from vccflow.importer import drop_unsupported

    doc = doc_with_menu()
    doc["applets"].append({"name": "ACD Q", "type": "ACD", "settings": {}, "next": {}})
    doc["applets"][-2]["next"]["Choices.2"] = "ACD Q"
    kept, notes = drop_unsupported(doc)
    menu = next(a for a in kept["applets"] if a["name"] == "MNU M")
    assert "Choices.2" not in menu["next"] and "Choices.0" in menu["next"]
    assert any("Choices.2" in n for n in notes)
