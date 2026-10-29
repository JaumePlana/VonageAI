import json

from vccflow.config import Account
from vccflow.export import collect_data_sources, export_plan, find_media_refs, split_connections
from vccflow.portal import extract_data_connector

PLAN_ID = "e9d0c8ab-741a-4d86-bd98-27f793d2a72e"

AUDIO = {
    "NextAppletSuccess": "ANN TTS", "NextAppletSuccessGuid": "g1",
    "NextAppletFail": "LOG Audio Failed", "NextAppletFailGuid": "g2",
    "NextApplet": None, "NextAppletGuid": None,
    "AnnouncementType": "InternalFile",
    "AnnouncementMedia": {"AppletMedia": {"Id": 15200090, "AccountId": 0, "Path": None}, "TextToSpeech": ""},
    "DialledNumber": PLAN_ID, "AppletName": "ANN Audio", "DisplayName": "Announcer",
    "AppletType": "DataAnnouncer", "Url": "Applet/DataAnnouncer", "ConfigurationUrl": "Applet",
    "ApplicationType": 10, "EntityId": 1, "AppletGuid": "g0",
}
LOG = {
    "DataSources": [{"Key": "LOG", "Value": "Failed to play file [$(ANN_Welcome)]"}],
    "AppletName": "LOG Audio Failed", "AppletType": "SetDataSource", "NextApplet": "ANN TTS",
    "NextAppletGuid": "g3", "EntityId": 2, "AppletGuid": "g2",
}
TTS = {"AnnouncementType": "Text", "Voice": "Amy", "Text": "Hello", "AppletType": "DataAnnouncer", "AppletGuid": "g1"}

DC_HTML = """
<form action="/CallCentre/apps/DataRetriever?x" id="DataRetrieverForm" method="post">
<input name="__RequestVerificationToken" type="hidden" value="csrf" />
<textarea name="ApiInteractionDefinition">&lt;ApiInteraction&gt;&lt;Url&gt;https://x/$(CustomerReference)&lt;/Url&gt;
&lt;ResponseCodeDestination&gt;ResponseCode&lt;/ResponseCodeDestination&gt;&lt;Destination&gt;accessToken&lt;/Destination&gt;&lt;/ApiInteraction&gt;</textarea>
</form>
<script>NEWVOICEMEDIA.dataRetriever('p', 'DC Authenticate').loadModel(JSON.parse(htmlDecode('{&quot;DataProvider&quot;:&quot;WebService&quot;,&quot;Action&quot;:&quot;CallWebService&quot;,&quot;NextApplet&quot;:&quot;ANN TTS&quot;,&quot;NextAppletUrl&quot;:&quot;u&quot;,&quot;NextAppletFailure&quot;:null,&quot;MetabaseEntries&quot;:{&quot;a&quot;:&quot;a&quot;},&quot;DataProviders&quot;:{}}')));</script>
"""


def test_split_connections_drops_ids_and_keeps_names():
    settings, nxt = split_connections(AUDIO)
    assert nxt == {"NextAppletSuccess": "ANN TTS", "NextAppletFail": "LOG Audio Failed"}
    assert settings["AnnouncementType"] == "InternalFile"
    for gone in ("EntityId", "AppletGuid", "DialledNumber", "NextAppletSuccessGuid", "NextApplet", "AppletName"):
        assert gone not in settings


def test_find_media_refs():
    assert find_media_refs({"AnnouncementMedia": AUDIO["AnnouncementMedia"]}) == [("AnnouncementMedia", 15200090)]
    assert find_media_refs({"AnnouncementMedia": {"AppletMedia": {"Id": None}}}) == []


def test_extract_data_connector():
    dc = extract_data_connector(DC_HTML)
    assert dc["model"]["DataProvider"] == "WebService"
    assert dc["fields"]["ApiInteractionDefinition"].startswith("<ApiInteraction><Url>https://x/$(CustomerReference)")


def test_collect_data_sources():
    assert collect_data_sources({"settings": {"DataSources": LOG["DataSources"]}}) == {"LOG", "ANN_Welcome"}
    assert collect_data_sources({"settings": {"DataSource": "ANN_Welcome"}}) == {"ANN_Welcome"}


class FakePortal:
    account = Account(name="JPlana", region="emea")
    states = {"ANN Audio": AUDIO, "LOG Audio Failed": LOG, "ANN TTS": TTS}

    def list_applets(self, plan_id):
        applets = [{"name": n, "appletType": "x", "configUrl": f"Applet?myApp={n}"} for n in self.states]
        applets.append({"name": "DC Authenticate", "appletType": "Data Connector", "configUrl": "DataRetriever?myApp=DC"})
        return {"name": "DISH Web Services", "applets": applets}

    def get_applet_state(self, plan_id, applet):
        return {"appletProperties": dict(self.states[applet["name"]])}

    def get_data_connector(self, applet):
        return extract_data_connector(DC_HTML)

    def get_initial_applet(self, plan_id):
        return "ANN Audio"

    def get_media_info(self, media_id):
        assert media_id == 15200090
        return {"id": media_id, "name": "Ringing", "url": "/CallCentre/recordings/1/x.wav"}

    def download_media(self, url):
        return b"RIFF....WAVE"


def test_export_plan_writes_portable_document(tmp_path):
    doc = export_plan(FakePortal(), PLAN_ID, "DISH Web Services", tmp_path,
                      metadata={"reportingGroupName": None, "displayInAgentInterface": True}, log=lambda _: None)
    saved = json.loads((tmp_path / "plan.json").read_text(encoding="utf-8"))
    assert saved == doc and doc["format"] == "vccflow.plan/1"
    by_name = {a["name"]: a for a in doc["applets"]}

    audio = by_name["ANN Audio"]
    assert audio["type"] == "DataAnnouncer"
    assert audio["media"] == [{"property": "AnnouncementMedia", "file": "media/ANN Audio.wav", "name": "Ringing"}]
    assert audio["settings"]["AnnouncementMedia"]["AppletMedia"]["Id"] is None
    assert (tmp_path / "media" / "ANN Audio.wav").read_bytes() == b"RIFF....WAVE"

    dc = by_name["DC Authenticate"]
    assert dc["type"] == "DataRetriever"
    assert dc["next"] == {"NextApplet": "ANN TTS"}
    assert "MetabaseEntries" not in dc["settings"] and "NextAppletUrl" not in dc["settings"]
    assert "ApiInteractionDefinition" in dc["settings"]

    assert by_name["LOG Audio Failed"]["next"] == {"NextApplet": "ANN TTS"}
    assert doc["plan"]["initialApplet"] == "ANN Audio"
    assert set(doc["requires"]["dataSources"]) >= {"LOG", "ANN_Welcome", "CustomerReference", "ResponseCode", "accessToken"}
    assert PLAN_ID not in json.dumps(doc["applets"])  # nothing tied to the source plan
