"""Call Connect Router (callConnectRouter.har): a React editor, appletType CallDivert, saved through Applet/Save."""

import json
from unittest.mock import MagicMock

import pytest

from vccflow.compare import compare_docs
from vccflow.config import Account
from vccflow.export import data_sources_used, export_applet
from vccflow.importer import import_plan, unsupported_reason

PLAN_ID = "b918444e-7c29-41b0-9a65-9a64b8568257"
ACCOUNT = Account(name="Demo29", region="emea", allow_portal_writes=True, portal_account="NVMDemo29")
REC = "recordings/56303/B080/a09bb129-f4b5-469e-b98f-303f97e6e74bNVMDemo29.wav"


def media(media_id):
    return {"AppletMedia": {"Id": media_id, "AccountId": 0, "Path": None, "Name": None, "DefaultLanguage": None,
                            "AvailableMediaMetadata": None}, "TextToSpeech": "", "RuntimeUrl": None}


# VCC's answer to save #27: every field set, the three audio files uploaded.
PROPS = {"AccountId": 56303, "NoAnswerTimeout": 19, "RetryAgents": True, "GroupTimeout": 37,
         "CallCompleteAppletId": 165917878, "UnsuccessfulCallAppletId": 165897398,
         "RingingAudio": media(15717814), "WhisperAudio": media(15717878), "WhisperOption": "Agent",
         "OnHoldAnnouncement": media(15717750),
         "DeliveryNumbers": [{"Value": "$(ANN_Welcome)"}, {"Value": "+34666"}, {"Value": "+34"}],
         "DistributionType": "Sequential", "TryAgentsMoreThanOnce": True, "OnHoldAnnouncementPath": REC,
         "RingingAudioPath": REC, "WhisperAudioPath": REC, "LastPosition": 0, "SendToAllAgents": False,
         "DialledNumber": PLAN_ID, "AppletName": "CCR Test", "DisplayName": "Call Connect Router",
         "AppletType": "CallDivert", "NextApplet": "DIC Test", "NextAppletGuid": "69679167-e997-11f1-8a44-0ab3d743c8e3",
         "Url": "CallConnectRouterInitial", "ConfigurationUrl": "Applet", "ApplicationType": 7, "EntityId": 165939126,
         "AppletGuid": "fae39450-e9b7-11f1-8a44-0ab3d743c8e3"}
LISTED = [{"id": 165939126, "guid": "fae39450-e9b7-11f1-8a44-0ab3d743c8e3", "name": "CCR Test"},
          {"id": 165897398, "guid": "69679167-e997-11f1-8a44-0ab3d743c8e3", "name": "DIC Test"},
          {"id": 165917878, "guid": "eb97596a-e9ac-11f1-8a44-0ab3d743c8e3", "name": "DIC to empty 3"}]
AUDIO = ("OnHoldAnnouncement", "RingingAudio", "WhisperAudio")


def exported(tmp_path, props=None):
    portal = MagicMock()
    portal.get_applet_state.return_value = {"appletProperties": json.loads(json.dumps(props or PROPS)), "metadata": {}}
    portal.get_media_info.side_effect = lambda i: {"url": f"https://x/{i}.wav", "name": f"audio {i}"}
    portal.download_media.side_effect = lambda url: url.encode()
    applet = {"name": "CCR Test", "appletType": "Call Connect Router", "configUrl": "Applet?application=CallDivert"}
    return export_applet(portal, PLAN_ID, applet, tmp_path, LISTED)


def test_export_keeps_the_editor_fields_the_applets_by_name_and_the_audio(tmp_path):
    e = exported(tmp_path)
    assert e["type"] == "CallDivert"
    assert e["next"] == {"CallCompleteApplet": "DIC to empty 3", "UnsuccessfulCallApplet": "DIC Test"}
    s = e["settings"]
    assert s["DeliveryNumbers"] == PROPS["DeliveryNumbers"]
    assert {k: s[k] for k in ("NoAnswerTimeout", "GroupTimeout", "RetryAgents", "DistributionType", "WhisperOption",
                              "SendToAllAgents")} == {"NoAnswerTimeout": 19, "GroupTimeout": 37, "RetryAgents": True,
                                                      "DistributionType": "Sequential", "WhisperOption": "Agent",
                                                      "SendToAllAgents": False}
    # VCC derives these (NextApplet from UnsuccessfulCallAppletId, #12; TryAgentsMoreThanOnce from RetryAgents, #13);
    # LastPosition is not in the editor. IDs, GUIDs and the account's storage paths are left out.
    for gone in ("TryAgentsMoreThanOnce", "LastPosition", "CallCompleteAppletId", "UnsuccessfulCallAppletId",
                 "NextAppletGuid", "AccountId", "RingingAudioPath"):
        assert gone not in s
    assert sorted(m["property"] for m in e["media"]) == list(AUDIO)
    assert "connectionProblems" not in e
    assert unsupported_reason(e) is None


def test_a_new_applet_exports_and_imports_as_it_is_created(tmp_path):
    # Save #4: VCC saves the applet empty.
    new = {**PROPS, "NoAnswerTimeout": 15, "RetryAgents": False, "GroupTimeout": 30, "CallCompleteAppletId": None,
           "UnsuccessfulCallAppletId": None, "RingingAudio": None, "WhisperAudio": None, "WhisperOption": "Off",
           "OnHoldAnnouncement": None, "DeliveryNumbers": [], "TryAgentsMoreThanOnce": False,
           "OnHoldAnnouncementPath": "", "RingingAudioPath": "", "WhisperAudioPath": "", "NextApplet": "",
           "NextAppletGuid": None}
    e = exported(tmp_path, new)
    assert e["next"] == {} and "media" not in e and e["settings"]["DeliveryNumbers"] == []
    assert unsupported_reason(e) is None


def test_removed_audio_is_copied_as_removed(tmp_path):
    # Save #36: the three audio files removed (AppletMedia null), the timeouts 0.
    gone = {"AppletMedia": None, "TextToSpeech": "", "RuntimeUrl": None}
    e = exported(tmp_path, {**PROPS, **{k: gone for k in AUDIO}, "WhisperOption": "Off", "NoAnswerTimeout": 0,
                            "GroupTimeout": 0, "OnHoldAnnouncementPath": "", "RingingAudioPath": "",
                            "WhisperAudioPath": ""})
    assert "media" not in e and e["settings"]["RingingAudio"] == gone
    assert unsupported_reason(e) is None


@pytest.mark.parametrize("change, message", [
    ({"DeliveryNumbers": [{"Value": "0034666"}]}, "delivery number '0034666' is not a data source"),
    ({"DeliveryNumbers": [{"Value": "+34 666"}]}, "delivery number '+34 666'"),
    ({"DeliveryNumbers": [{"Value": "$(Open"}]}, "delivery number '$(Open'"),
    ({"DistributionType": "Simultaneous"}, "DistributionType 'Simultaneous' is not one of Sequential, Rotary"),
    ({"WhisperOption": "Caller"}, "WhisperOption 'Caller' is not one of Off, Agent, AgentAndCaller"),
    # #31-#35: an empty timeout, which VCC answers with HTTP 500.
    ({"NoAnswerTimeout": ""}, "NoAnswerTimeout '' is not 0 to 9999 seconds"),
    ({"GroupTimeout": 10000}, "GroupTimeout 10000 is not 0 to 9999 seconds"),
    ({"Other": 1}, "settings Other have not been recorded yet"),
])
def test_what_was_not_recorded_or_vcc_refuses_is_refused(tmp_path, change, message):
    e = exported(tmp_path)
    e["settings"].update(change)
    assert message in unsupported_reason(e)


def test_a_next_applet_other_than_vccs_copy_is_kept_and_refused(tmp_path):
    e = exported(tmp_path, {**PROPS, "NextApplet": "DIC to empty 3",
                            "NextAppletGuid": "eb97596a-e9ac-11f1-8a44-0ab3d743c8e3"})
    assert e["next"]["NextApplet"] == "DIC to empty 3"
    assert "connections NextApplet have not been recorded yet" in unsupported_reason(e)


def test_data_sources_in_the_delivery_numbers_are_read():
    a = {"name": "CCR", "type": "CallDivert", "settings": {"DeliveryNumbers": [{"Value": "$(ANN_Welcome)"},
                                                                                {"Value": "+34666"}]}}
    assert data_sources_used(a) == {"ANN_Welcome"}


class FakePortal:
    account = ACCOUNT

    def __init__(self):
        self.applets, self.writes = {}, []

    def list_applets(self, plan_id):
        return {"name": "Scratch", "applets": [{"id": 100 + i, "guid": f"g-{n}", "name": n, "appletType": t}
                                               for i, (n, t) in enumerate(self.applets.items())]}

    def list_data_sources(self, plan_id):
        return {"ANN_Welcome"}

    def create_applet(self, plan_id, applet_type, name):
        self.writes.append(("create", name, applet_type))
        self.applets[name] = applet_type

    def delete_applet(self, plan_id, name):
        self.writes.append(("delete", name))

    def get_applet_state(self, plan_id, applet):
        # A new applet, as VCC answers the first recorded save (#4).
        return {"appletProperties": {"AppletType": self.applets[applet["name"]], "AppletName": applet["name"],
                                     "CallCompleteAppletId": None, "UnsuccessfulCallAppletId": None, "NextApplet": "",
                                     "NextAppletGuid": None, "LastPosition": 0, "TryAgentsMoreThanOnce": False,
                                     "RingingAudio": None, "WhisperAudio": None, "OnHoldAnnouncement": None,
                                     "EntityId": 1}}

    def upload_media(self, plan_id, name, path, filename):
        self.writes.append(("upload", name, filename))
        return 500 + len(self.writes)

    def save_applet(self, plan_id, name, props):
        self.writes.append(("save", name, json.loads(json.dumps(props))))

    def set_initial_applet(self, plan_id, name):
        self.writes.append(("initial", name))


def test_import_saves_the_applets_by_the_targets_ids_and_uploads_the_audio(tmp_path, monkeypatch):
    monkeypatch.setattr("vccflow.importer.export.export_plan",
                        lambda *a, **k: {"plan": {"initialApplet": "CCR Test"}, "applets": []})
    (tmp_path / "export").mkdir()
    e = exported(tmp_path / "export")
    (tmp_path / "media").mkdir()
    for m in e["media"]:
        (tmp_path / m["file"]).write_bytes(b"RIFF")
    others = [{"name": n, "type": "SetDataSource", "settings": {"DataSources": []}, "next": {}}
              for n in ("DIC Test", "DIC to empty 3")]
    doc = {"format": "vccflow.plan/1", "plan": {"name": "Source", "initialApplet": "CCR Test"}, "applets": [e, *others]}
    (tmp_path / "plan.json").write_text(json.dumps(doc), encoding="utf-8")
    portal = FakePortal()
    import_plan(portal, tmp_path, PLAN_ID, "Scratch", log=lambda _: None)
    assert ("create", "CCR Test", "CallDivert") in portal.writes
    props = next(w[2] for w in portal.writes if w[0] == "save" and w[1] == "CCR Test")
    ids = {n: 100 + i for i, n in enumerate(portal.applets)}
    assert props["CallCompleteAppletId"] == ids["DIC to empty 3"]
    assert props["UnsuccessfulCallAppletId"] == ids["DIC Test"]
    assert "CallCompleteApplet" not in props and "UnsuccessfulCallApplet" not in props
    assert props["NextApplet"] == "" and props["TryAgentsMoreThanOnce"] is False  # VCC sets them
    assert props["DeliveryNumbers"] == PROPS["DeliveryNumbers"] and props["WhisperOption"] == "Agent"
    uploads = [w for w in portal.writes if w[0] == "upload"]
    assert len(uploads) == 3
    assert all(props[k]["AppletMedia"]["Id"] for k in AUDIO)


def test_comparison_reports_changes_both_ways_and_nothing_between_accounts(tmp_path):
    a = {**exported(tmp_path), "kind": "Call Connect Router"}
    b = json.loads(json.dumps(a))
    b["settings"]["DeliveryNumbers"] = [{"Value": "+34666"}, {"Value": "$(ANN_Welcome)"}, {"Value": "+34"}]
    b["settings"]["DistributionType"] = "Rotary"
    b["next"]["UnsuccessfulCallApplet"] = "DIC to empty 3"
    doc = lambda x: {"plan": {}, "applets": [x, *({"name": n, "type": "SetDataSource", "settings": {}, "next": {}}
                                                  for n in ("DIC Test", "DIC to empty 3"))]}
    diffs = compare_docs(doc(a), doc(b))["differences"]
    assert any(d.startswith("'CCR Test' setting DeliveryNumbers") for d in diffs)  # the order matters
    assert "'CCR Test' setting DistributionType: source Sequential | target Rotary" in diffs
    assert "'CCR Test' path UnsuccessfulCallApplet is different: source 'DIC Test' | target 'DIC to empty 3'" in diffs
    assert "'CCR Test' setting DistributionType: source Rotary | target Sequential" in compare_docs(doc(b), doc(a))["differences"]
    # Another account: other IDs and storage paths, the same export.
    other = {**PROPS, "CallCompleteAppletId": 7, "UnsuccessfulCallAppletId": 8, "AccountId": 1, "EntityId": 9,
             "RingingAudioPath": "recordings/1/x.wav"}
    listed = [{"id": 7, "guid": "a", "name": "DIC to empty 3"}, {"id": 8, "guid": "b", "name": "DIC Test"}]
    portal = MagicMock()
    portal.get_applet_state.return_value = {"appletProperties": other, "metadata": {}}
    portal.get_media_info.side_effect = lambda i: {"url": f"https://x/{i}.wav", "name": f"audio {i}"}
    portal.download_media.side_effect = lambda url: url.encode()
    c = export_applet(portal, PLAN_ID, {"name": "CCR Test", "appletType": "Call Connect Router",
                                        "configUrl": "Applet?application=CallDivert"}, tmp_path, listed)
    assert compare_docs(doc(a), doc({**c, "kind": "Call Connect Router"}))["differences"] == []
