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
    assert audio["media"][0].pop("sha256")
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


def test_created_versus_used_data_sources():
    from vccflow.export import data_sources_created, data_sources_needed, data_sources_used

    sds = {"settings": {"DataSources": [{"Key": "LOG", "Value": "x $(Case Country)"}]}}
    ws = {"settings": {"ApiInteractionDefinition": "<Url>$(JavascriptEngineUrl)</Url><Destination>Language</Destination>"}}
    sf = {"settings": {"Action": "GetSalesforceData", "ResponseItems": ["Case|Origin"],
                       "CrmRetrievalViewModel": {"MatchDataSource": "CallGuid", "MaxRecords": 2}}}
    soql = {"settings": {"Action": "GetSalesforceData",
                         "CrmRetrievalViewModel": {"UseRawSoql": True, "Soql": "SELECT Status, Id FROM Case LIMIT 1"}}}
    assert data_sources_created(sds) == {"LOG"} and data_sources_used(sds) == {"Case Country"}
    # $(JavascriptEngineUrl) is a Vonage keyword (JavaScript Data Connector), not a data source.
    assert data_sources_created(ws) == {"Language"} and data_sources_used(ws) == set()
    assert data_sources_created(sf) == {"Case|Origin", "Case(1)|Origin", "Case(2)|Origin"}
    assert data_sources_created(soql) == {"Case|Status", "Case|Id"}
    reads_log = {"settings": {"Text": "$(LOG) $(Case|Origin)"}}
    assert data_sources_needed([sds, ws, sf, reads_log]) == {"Case Country", "CallGuid"}


def test_menu_connections_go_into_next():
    menu = {"Choices": {"0": "DC Get Salesforce", "*": "ANN Test Claude", "9": ""},
            "TimeoutAppletName": "LOG Audio Failed", "TimeoutAppletGuid": "g1",
            "FailureAppletId": 164192310, "FailureAppletGuid": "g2", "AccountId": 56303,
            "Text": "Hello", "AppletType": "Menu"}
    settings, nxt = split_connections(menu, {164192310: "LOG Test Claude Failed"})
    assert nxt == {"Choices.0": "DC Get Salesforce", "Choices.*": "ANN Test Claude",
                   "TimeoutApplet": "LOG Audio Failed", "FailureApplet": "LOG Test Claude Failed"}
    assert settings == {"Text": "Hello"}


def test_menu_data_sources():
    from vccflow.export import data_sources_used

    dynamic = {"settings": {"AnnouncementDataSource": "ANN_Case", "IsDynamicVoice": True, "VoiceDataSource": "vendortype"}}
    assert data_sources_used(dynamic) == {"ANN_Case", "vendortype"}
    # VoiceDataSource only counts when the voice is dynamic.
    fixed = {"settings": {"IsDynamicVoice": False, "VoiceDataSource": "vendortype"}}
    assert data_sources_used(fixed) == set()


def test_applets_that_cannot_be_exported_keep_their_page_fields(tmp_path):
    from vccflow.portal import PortalError

    class Portal(FakePortal):
        states = {}

        def list_applets(self, plan_id):
            return {"applets": [{"id": 1, "guid": "g", "name": "ST B", "appletType": "Skill Tagger", "configUrl": "Skill?x"}]}

        def get_applet_state(self, plan_id, applet):
            raise PortalError("no initialState")

        def get_applet_form(self, applet):
            return {"Skills": [{"Name": "FR"}]}

    doc = export_plan(Portal(), PLAN_ID, "P", tmp_path, log=lambda _: None)
    assert doc["applets"][0]["form"] == {"Skills": [{"Name": "FR"}]} and doc["applets"][0]["unsupported"]
    assert json.loads((tmp_path / "plan.json").read_text(encoding="utf-8"))["applets"][0]["form"]


def test_export_records_connection_ids_that_do_not_resolve(tmp_path):
    class Portal(FakePortal):
        states = {"ANN A": {"AppletType": "DataAnnouncer", "NextAppletSuccess": "ANN A", "NextAppletSuccessGuid": "elsewhere"}}

        def list_applets(self, plan_id):
            return {"applets": [{"id": 1, "guid": "g1", "name": "ANN A", "appletType": "Announcer", "configUrl": "Applet?x"}]}

    doc = export_plan(Portal(), PLAN_ID, "P", tmp_path, log=lambda _: None)
    assert "not in this plan" in doc["applets"][0]["connectionProblems"][0]


SCHED = "b5434940-67c7-41c2-b6dd-59c894a893d0"
OTHER_SCHED = "a1f287dd-42ba-43c1-a9e2-9aaf12a489af"
GUID_LC_TARGET = "ec91b743-aa77-11ee-980a-0267ee5b37ba"


class ByNamePortal(FakePortal):
    """A Schedule Router, a Loop Counter and a Call Connect Router, as VCC sends them (values made up)."""

    listed = [
        {"id": 101, "guid": "aaaaaaaa-0000-0000-0000-000000000001", "name": "SR Opening hours",
         "appletType": "Schedule Router", "configUrl": "Applet?myApp=SR"},
        {"id": 102, "guid": GUID_LC_TARGET, "name": "ANN Open", "appletType": "Announcer", "configUrl": "Applet?myApp=A"},
        {"id": 103, "guid": "aaaaaaaa-0000-0000-0000-000000000003", "name": "ANN Closed",
         "appletType": "Announcer", "configUrl": "Applet?myApp=C"},
        {"id": 104, "guid": "aaaaaaaa-0000-0000-0000-000000000004", "name": "LC test",
         "appletType": "Loop Counter", "configUrl": "Applet?myApp=LC"},
        {"id": 105, "guid": "aaaaaaaa-0000-0000-0000-000000000005", "name": "CCR",
         "appletType": "Call Connect Router", "configUrl": "Applet?myApp=CCR"},
    ]
    states = {
        "SR Opening hours": {
            "Schedule": SCHED, "DefaultAppletId": 102, "DefaultAppletGuid": None,
            "ScheduleMappings": {f"{SCHED}:Closed": 103, f"{SCHED}:Open": 102, f"{OTHER_SCHED}:Open": 103}, "ScheduleNextAppletMappings": {},
            "FailureAppletId": 102, "FailureAppletGuid": None, "AppletType": "ScheduleRouter", "NextApplet": "",
            "EntityId": 101, "AppletGuid": "aaaaaaaa-0000-0000-0000-000000000001",
        },
        "ANN Open": {"AppletType": "DataAnnouncer", "Text": "Open"},
        "ANN Closed": {"AppletType": "DataAnnouncer", "Text": "Closed"},
        "LC test": {
            "MaximumLoopCount": 2, "NextAppletIdOnMaximumReached": 102,
            "NextAppletGuidOnMaximumReached": GUID_LC_TARGET, "AppletType": "LoopCounter",
            "NextApplet": "ANN Closed", "NextAppletGuid": "aaaaaaaa-0000-0000-0000-000000000003",
        },
        "CCR": {
            "AppletType": "CallDivert", "CallCompleteAppletId": None, "UnsuccessfulCallAppletId": 999,
            "RingingAudio": {"AppletMedia": {"Id": 15200090, "AccountId": 0}, "TextToSpeech": ""},
            "RingingAudioPath": "recordings/133338/B053/x.wav", "NextApplet": "ANN Open",
        },
    }
    metadata = {"container": {"children": [{"type": "group", "children": [{"prop": "Schedule", "options": [
        {"value": SCHED, "label": "Fab'n'Fit schedule"}, {"value": OTHER_SCHED, "label": "Test Schedule"}]}]}]}}

    def list_applets(self, plan_id):
        return {"applets": self.listed}

    def get_applet_state(self, plan_id, applet):
        return {"appletProperties": dict(self.states[applet["name"]]), "metadata": self.metadata}


def test_types_not_imported_keep_applets_and_schedules_by_name(tmp_path):
    doc = export_plan(ByNamePortal(), PLAN_ID, "P", tmp_path, log=lambda _: None)
    by_name = {a["name"]: a for a in doc["applets"]}

    sr = by_name["SR Opening hours"]
    assert sr["settings"] == {"Schedule": "Fab'n'Fit schedule", "ScheduleNextAppletMappings": {}}
    assert sr["next"] == {"DefaultApplet": "ANN Open", "FailureApplet": "ANN Open",
                          "ScheduleMappings.Closed": "ANN Closed",
                          "ScheduleMappings.Open": "ANN Open",
                          # a mapping left from another schedule keeps that schedule's name
                          "ScheduleMappings.Test Schedule:Open": "ANN Closed"}

    lc = by_name["LC test"]
    assert lc["settings"] == {"MaximumLoopCount": 2}
    assert lc["next"] == {"NextApplet": "ANN Closed", "NextAppletOnMaximumReached": "ANN Open"}

    ccr = by_name["CCR"]
    assert "RingingAudioPath" not in ccr["settings"] and ccr["media"][0]["property"] == "RingingAudio"
    assert ccr["next"] == {"NextApplet": "ANN Open"}
    assert ccr["connectionProblems"] == ["'CCR' UnsuccessfulCallAppletId 999 is not in this plan"]


def test_types_import_plan_writes_keep_their_settings_as_sent(tmp_path):
    class Portal(ByNamePortal):
        states = {**ByNamePortal.states, "ANN Open": {"AppletType": "DataAnnouncer", "Voice": SCHED, "DefaultAppletId": 103}}

    doc = export_plan(Portal(), PLAN_ID, "P", tmp_path, log=lambda _: None)
    ann = next(a for a in doc["applets"] if a["name"] == "ANN Open")
    assert ann["settings"] == {"Voice": SCHED, "DefaultAppletId": 103} and ann["next"] == {}


def test_resolve_applet_references_reports_two_targets():
    from vccflow.export import resolve_applet_references

    listed = [{"id": 1, "guid": "aaaaaaaa-0000-0000-0000-000000000001", "name": "A"},
              {"id": 2, "guid": "aaaaaaaa-0000-0000-0000-000000000002", "name": "B"}]
    rest, nxt, problems = resolve_applet_references(
        "X", {"NextAppletIdOnMax": 1, "NextAppletGuidOnMax": "aaaaaaaa-0000-0000-0000-000000000002", "Count": 3,
              "Plain": {"k": 3}}, listed)
    assert rest == {"Count": 3, "Plain": {"k": 3}} and nxt == {"NextAppletOnMax": "A"}
    assert problems == ["'X' NextAppletOnMax points to both 'A' and 'B'"]
