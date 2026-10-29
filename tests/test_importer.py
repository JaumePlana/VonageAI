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
            {"name": n, "guid": f"guid-{n}", "appletType": a["type"], "configUrl": f"x?myapp={n}"}
            for n, a in self.applets.items()]}

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
