"""Skill Tagger (skillTagger.har) and Salesforce create Data Connector (dataconnector_createSFRecord.har)."""

import html
import json
from unittest.mock import MagicMock
from urllib.parse import parse_qsl, urlencode

import pytest

from vccflow.compare import compare_docs
from vccflow.config import Account
from vccflow.export import data_sources_created, data_sources_used, export_plan, split_skill_tagger
from vccflow.importer import check_skills, import_plan, unsupported_reason
from vccflow.portal import PortalClient, PortalError, data_connector_form, extract_skill_tagger, skill_tagger_form

PLAN_ID = "b918444e-7c29-41b0-9a65-9a64b8568257"

# The page model after the last recorded save (#18), lookups shortened.
MODEL = {
    "ClearAllSkills": True,
    "Skills": [{"Name": "External", "Necessity": 3, "RequiredDays": 0, "RequiredHours": 0, "RequiredMinutes": 0,
                "RequiredSeconds": 15},
               {"Name": "Chat", "Necessity": 2, "RequiredDays": 0, "RequiredHours": 0, "RequiredMinutes": 0,
                "RequiredSeconds": 0}],
    "DynamicSkills": [{"Name": "CallReason", "Necessity": 2, "RequiredDays": 0, "RequiredHours": 0,
                       "RequiredMinutes": 0, "RequiredSeconds": 0}],
    "SerialisedDynamicSkills": None,
    "AgentIdDataSource": "AgentID", "LoggedInTimeoutDataSource": "", "LoggedOutTimeoutDataSource": "",
    "InterruptAgentWhilstAway": False, "ShowInterruptAgentWhilstAway": False,
    "AllSkills": ["Chat", "DE", "Email", "External", "PT"],
    "AllMetabaseEntries": {"AgentID": "AgentID", "CallReason": "CallReason", "Account|Id": "Account|Account ID"},
    "AllRequiredDays": {"0": "0 d"}, "PossibleNextApplets": [{"AppletName": "DR Decimal", "EntityId": 1}],
    "NextApplet": "DR Decimal", "FormattedNextApplets": [{"Value": "DR Decimal", "AppletId": 1}],
}
# The recorded post (#18), without its form token.
RECORDED_POST = urlencode([
    ("ClearAllSkills", "true"), ("AgentIdDataSource", "AgentID"), ("LoggedInTimeoutDataSource", ""),
    ("LoggedOutTimeoutDataSource", ""), ("InterruptAgentWhilstAway", "False"),
    ("Skills[0].Name", "Chat"), ("Skills[0].Necessity", "2"), ("Skills[0].RequiredDays", "0"),
    ("Skills[0].RequiredHours", "0"), ("Skills[0].RequiredMinutes", "0"), ("Skills[0].RequiredSeconds", "0"),
    ("Skills[1].Name", "External"), ("Skills[1].Necessity", "3"), ("Skills[1].RequiredDays", "0"),
    ("Skills[1].RequiredHours", "0"), ("Skills[1].RequiredMinutes", "0"), ("Skills[1].RequiredSeconds", "15"),
    ("SerialisedDynamicSkills", '[{"Name":"CallReason","Necessity":2,"RequiredDays":0,"RequiredHours":0,'
                                '"RequiredMinutes":0,"RequiredSeconds":0,"FriendlyName":"CallReason",'
                                '"Description":" is mandatory"}]'),
    ("NextApplet", "DR Decimal"),
])


def page(model=None):
    data = html.escape(json.dumps(model or MODEL), quote=True)
    return ('<form id="SkillTaggerForm"><input name="__RequestVerificationToken" type="hidden" value="tok" /></form>'
            f"<script>vm.load(JSON.parse(htmlDecode('{data}')));</script>")


def exported(**over):
    settings, nxt = split_skill_tagger(MODEL)
    settings.update(over)
    return {"name": "ST Sample", "kind": "Skill Tagger", "type": "SkillTagger", "settings": settings, "next": nxt}


def test_export_keeps_skills_by_name_sorted_without_lookups():
    settings, nxt = split_skill_tagger(extract_skill_tagger(page()))
    assert [s["Name"] for s in settings["Skills"]] == ["Chat", "External"]
    assert settings["Skills"][1] == {"Name": "External", "Necessity": 3, "RequiredDays": 0, "RequiredHours": 0,
                                     "RequiredMinutes": 0, "RequiredSeconds": 15}
    assert settings["DynamicSkills"][0]["Name"] == "CallReason" and settings["ClearAllSkills"] is True
    assert settings["AgentIdDataSource"] == "AgentID" and settings["InterruptAgentWhilstAway"] is False
    assert not [k for k in settings if k.startswith(("All", "Possible", "Formatted", "Show", "Serialised"))]
    assert nxt == {"NextApplet": "DR Decimal"}


def test_export_plan_reads_the_skill_tagger_page(tmp_path):
    class Portal:
        account = Account(name="Demo29", region="emea")

        def list_applets(self, plan_id):
            return {"applets": [{"id": 1, "guid": "g", "name": "ST Sample", "appletType": "Skill Tagger",
                                 "configUrl": "SkillPersonalQueueTagger?phonenum=x&myapp=ST&application=SkillTagger"}]}

        def get_skill_tagger(self, applet):
            return extract_skill_tagger(page())

        def get_initial_applet(self, plan_id):
            return "ST Sample"

    a = export_plan(Portal(), PLAN_ID, "P", tmp_path, log=lambda _: None)["applets"][0]
    assert a["type"] == "SkillTagger" and "unsupported" not in a and a["next"] == {"NextApplet": "DR Decimal"}


def test_form_reproduces_the_recorded_save():
    a = exported()
    assert skill_tagger_form(a["settings"], a["next"], MODEL["AllMetabaseEntries"]) == \
        parse_qsl(RECORDED_POST, keep_blank_values=True)


def test_form_without_agent_leaves_out_the_timeouts_and_unticked_clear():
    a = exported(ClearAllSkills=False, AgentIdDataSource="", Skills=[], DynamicSkills=[])
    assert skill_tagger_form(a["settings"], {}) == [
        ("AgentIdDataSource", ""), ("InterruptAgentWhilstAway", "False"), ("SerialisedDynamicSkills", "[]"),
        ("NextApplet", "")]


@pytest.mark.parametrize("over, message", [
    ({"AgentIdDataSource": "", "LoggedInTimeoutDataSource": "calldisposition"}, "need an agent ID"),
    ({"InterruptAgentWhilstAway": True}, "Interrupt agent whilst away"),
    ({"Skills": [{"Name": "Chat", "Necessity": 4}]}, "necessity 4 is unknown"),
    ({"DynamicSkills": [{"Name": "", "Necessity": 2}]}, "dynamic skill 1 has no name"),
])
def test_unrecorded_cases_are_refused(over, message):
    a = exported(**over)
    with pytest.raises(PortalError, match=message):
        skill_tagger_form(a["settings"], a["next"])
    assert message in unsupported_reason(a)


def test_data_sources_of_dynamic_skills_and_the_personal_queue():
    a = exported(LoggedOutTimeoutDataSource="CallTopic")
    assert data_sources_used(a) == {"CallReason", "AgentID", "CallTopic"}
    assert data_sources_created(a) == set()


def test_save_posts_the_form_with_the_page_token_and_reads_back():
    session = MagicMock()
    session.get.return_value = MagicMock(status_code=200, text="var user = { account: 'NVMDemo29' };")
    session.request.side_effect = [
        MagicMock(status_code=200, is_redirect=False, headers={}, text=page()),
        MagicMock(status_code=200, is_redirect=False, headers={}, text=page()),
    ]
    account = Account(name="Demo29", region="emea", allow_portal_writes=True, portal_account="NVMDemo29")
    a = exported()
    saved = PortalClient(account, session=session).save_skill_tagger(PLAN_ID, "ST Sample", a["settings"], a["next"])
    get, post = session.request.call_args_list
    assert get.args[0] == "GET" and "SkillPersonalQueueTagger?phonenum=" in get.args[1]
    assert post.args[1].endswith(f"SkillPersonalQueueTagger?phoneNum={PLAN_ID}&myApp=ST%20Sample")
    assert post.kwargs["data"] == [("__RequestVerificationToken", "tok"), *parse_qsl(RECORDED_POST, keep_blank_values=True)]
    assert saved["NextApplet"] == "DR Decimal"


def test_refused_save_is_an_error():
    session = MagicMock()
    session.get.return_value = MagicMock(status_code=200, text="var user = { account: 'NVMDemo29' };")
    session.request.side_effect = [
        MagicMock(status_code=200, is_redirect=False, headers={}, text=page()),
        MagicMock(status_code=200, is_redirect=False, headers={}, text=(
            '<div class="validation-summary-errors"><ul><li>Please select a valid skill</li></ul></div>')),
    ]
    account = Account(name="Demo29", region="emea", allow_portal_writes=True, portal_account="NVMDemo29")
    a = exported()
    with pytest.raises(PortalError, match="'ST Sample' was refused: Please select a valid skill"):
        PortalClient(account, session=session).save_skill_tagger(PLAN_ID, "ST Sample", a["settings"], a["next"])


def test_skills_are_matched_without_letter_case():
    a = exported(Skills=[{"Name": "de", "Necessity": 2}, {"Name": "Chat", "Necessity": 1}, {"Name": "Nope", "Necessity": 1}])
    spellings, missing, notes = check_skills([a], ["Chat", "DE"])
    assert spellings == {"de": "DE"}
    assert missing == {"Nope": "skill 'Nope' does not exist in the target account (used by ST Sample)"}
    assert notes == ["skill 'de' exists in the target account as 'DE': that spelling is used"]


class FakePortal:
    account = Account(name="Demo29", region="emea", allow_portal_writes=True)

    def __init__(self, skills):
        self.skills, self.applets, self.writes = skills, {}, []

    def list_applets(self, plan_id):
        return {"name": "Scratch", "applets": [
            {"id": i, "name": n, "guid": f"g-{n}", "appletType": t, "configUrl": f"x?{n}"}
            for i, (n, t) in enumerate(self.applets.items())]}

    def list_data_sources(self, plan_id):
        return {"AgentID", "CallReason"}

    def list_skills(self, plan_id):
        return self.skills

    def get_skill_tagger(self, applet):
        return {"AllSkills": ["chat", "External"]}

    def create_applet(self, plan_id, applet_type, name):
        self.writes.append(("create", name, applet_type))
        self.applets[name] = applet_type

    def delete_applet(self, plan_id, name):
        self.writes.append(("delete", name))
        self.applets.pop(name)

    def get_applet_state(self, plan_id, applet):
        return {"appletProperties": {"AppletType": self.applets[applet["name"]]}}

    def save_applet(self, plan_id, name, props):
        self.writes.append(("save", name))

    def save_skill_tagger(self, plan_id, name, settings, nxt):
        self.writes.append(("save-st", name, json.loads(json.dumps(settings)), nxt))

    def set_initial_applet(self, plan_id, name):
        self.writes.append(("initial", name))


def write_doc(tmp_path, *applets):
    target = {"name": "DR Decimal", "type": "SetDataSource", "settings": {"DataSources": []}, "next": {}}
    doc = {"format": "vccflow.plan/1", "plan": {"name": "Source", "initialApplet": "ST Sample"},
           "applets": [*applets, target]}
    (tmp_path / "plan.json").write_text(json.dumps(doc), encoding="utf-8")
    return doc


def test_import_saves_the_skill_tagger_with_the_target_spelling(tmp_path, monkeypatch):
    write_doc(tmp_path, exported())
    portal = FakePortal(["CHAT", "External"])
    monkeypatch.setattr("vccflow.importer.export.export_plan", lambda *a, **k: {"plan": {"initialApplet": "ST Sample"},
                                                                                  "applets": []})
    logs = []
    import_plan(portal, tmp_path, PLAN_ID, "Scratch", log=logs.append)
    assert ("create", "ST Sample", "SkillTagger") in portal.writes
    saved = next(w for w in portal.writes if w[0] == "save-st")
    assert [s["Name"] for s in saved[2]["Skills"]] == ["CHAT", "External"] and saved[3] == {"NextApplet": "DR Decimal"}
    assert any("skill 'Chat' exists in the target account as 'CHAT'" in m for m in logs)


def test_missing_skill_without_the_user_admin_api_stops_before_any_write(tmp_path):
    write_doc(tmp_path, exported())
    portal = FakePortal(["Chat"])
    with pytest.raises(PortalError, match="skill 'External' does not exist in the target account \\(used by ST Sample\\)"):
        import_plan(portal, tmp_path, PLAN_ID, "Scratch", log=lambda _: None)
    assert portal.writes == []


def test_without_a_skill_tagger_in_the_target_skills_are_checked_after_creating(tmp_path):
    write_doc(tmp_path, exported(Skills=[{"Name": "Chat", "Necessity": 2}, {"Name": "PT", "Necessity": 1}]))
    portal = FakePortal(None)  # the created applet's page lists "chat" and "External": PT is missing
    with pytest.raises(PortalError, match="skill 'PT'"):
        import_plan(portal, tmp_path, PLAN_ID, "Scratch", log=lambda _: None)
    assert not any(w[0].startswith("save") for w in portal.writes)
    assert ("delete", "ST Sample") in portal.writes  # what it created is deleted again


def doc_with(applet):
    return {"plan": {}, "applets": [applet, {"name": "DR Decimal", "kind": "Data Router", "settings": {}, "next": {}}]}


def test_comparison_reports_each_skill_by_name_both_ways_and_case_as_a_note():
    a = exported()
    chat, external = a["settings"]["Skills"]
    phone = {**chat, "Name": "Phone"}
    # As changed live in "POC Copy Claude": External 15 s -> 8 h, Phone added, a dynamic skill removed.
    b = exported(Skills=[chat, {**external, "RequiredSeconds": 0, "RequiredHours": 8}, phone], DynamicSkills=[])
    assert compare_docs(doc_with(a), doc_with(b))["differences"] == [
        "'ST Sample' skill 'External': source mandatory for 15 seconds | target mandatory for 8 hours",
        "'ST Sample' skill 'Phone' is only in the target (mandatory)",
        "'ST Sample' dynamic skill 'CallReason' is only in the source (mandatory)"]
    assert compare_docs(doc_with(b), doc_with(a))["differences"] == [
        "'ST Sample' skill 'External': source mandatory for 8 hours | target mandatory for 15 seconds",
        "'ST Sample' skill 'Phone' is only in the source (mandatory)",
        "'ST Sample' dynamic skill 'CallReason' is only in the target (mandatory)"]
    c = exported(Skills=[{**s, "Name": s["Name"].upper()} for s in a["settings"]["Skills"]])
    report = compare_docs(doc_with(a), doc_with(c))
    assert report["differences"] == [] and "'ST Sample' skill differs only in letter case: 'Chat' / 'CHAT'" in report["notes"]
    assert compare_docs(doc_with(a), doc_with(exported(ClearAllSkills=False)))["differences"] == [
        "'ST Sample' setting ClearAllSkills: source true | target false"]
    assert compare_docs(doc_with(a), doc_with(exported()))["differences"] == []


# --- Salesforce create (dataconnector_createSFRecord.har) ---

SF_CREATE = {
    "DataProvider": "Salesforce", "Action": "CreateSalesforceObject", "MatchObjectName": "Account",
    "ResponseItems": ["Account|Id"],
    "CrmCreationViewModel": {"CrmObjectParameters": [
        {"DataSourceType": 0, "CrmField": "Account|Name", "Value": "Lead"},
        {"DataSourceType": 0, "CrmField": "Account|Phone", "Value": "CLID"}]},
}


def test_salesforce_create_reproduces_the_recorded_save():
    form = data_connector_form(SF_CREATE, {"NextApplet": "DR Sample", "NextAppletInterfaceFailure": "ANN New Audio"})
    assert form == [
        ("DataProvider", "Salesforce"), ("Action", "CreateSalesforceObject"), ("MatchObjectName", "Account"),
        ("fieldToPopulateSelectBox", ""),
        ("SerialisedResponseItems", '[{"crmField":"Account|Name","dataSourceType":0,"value":"Lead"},'
                                    '{"crmField":"Account|Phone","dataSourceType":0,"value":"CLID"}]'),
        ("NextApplet", "DR Sample"), ("NextAppletFailure", ""), ("NextAppletInterfaceFailure", "ANN New Audio")]


def test_salesforce_create_sets_the_record_id_and_reads_its_values():
    a = {"settings": SF_CREATE}
    assert data_sources_created(a) == {"Account|Id"} and data_sources_used(a) == {"Lead", "CLID"}


@pytest.mark.parametrize("change, message", [
    (lambda s: s["CrmCreationViewModel"]["CrmObjectParameters"].append(
        {"DataSourceType": 1, "CrmField": "Account|Site", "Value": "Slot1"}), "IVR Slot are not supported: Account|Site"),
    (lambda s: s.update(MatchObjectName=""), "needs an object"),
])
def test_salesforce_create_refuses_ivr_slots_and_a_missing_object(change, message):
    settings = json.loads(json.dumps(SF_CREATE))
    change(settings)
    with pytest.raises(PortalError, match=message):
        data_connector_form(settings, {})


def test_dynamic_skill_descriptions_as_the_page_builds_them():
    from vccflow.portal import skill_description

    # skillTagger_DynamicSkills.har
    assert skill_description({"Necessity": 1}) == " is optional"
    assert skill_description({"Necessity": 2}) == " is mandatory"
    assert skill_description({"Necessity": 3, "RequiredMinutes": 8}) == " is mandatory for 8 minutes "
    assert skill_description({"Necessity": 3, "RequiredDays": 1, "RequiredHours": 2, "RequiredSeconds": 1}) == \
        " is mandatory for 1 day 2 hours 1 second "


class FakeSkills:
    """The target account's User Admin API skills."""

    def __init__(self, names, fail=False):
        self.items = [{"skillId": f"id-{n}", "skillName": n} for n in names]
        self.fail, self.created, self.deleted = fail, [], []

    def names(self):
        if self.fail:
            from vccflow.skills import SkillsError
            raise SkillsError("HTTP 403")
        return [s["skillName"] for s in self.items]

    def create(self, name, description=""):
        s = {"skillId": f"id-{name}", "skillName": name}
        self.items.append(s)
        self.created.append(name)
        return s

    def delete(self, skill_id):
        self.deleted.append(skill_id)


def test_missing_skills_are_created_and_reported(tmp_path, monkeypatch):
    write_doc(tmp_path, exported())
    portal, skills = FakePortal(None), FakeSkills(["chat"])
    monkeypatch.setattr("vccflow.importer.export.export_plan", lambda *a, **k: {"plan": {"initialApplet": "ST Sample"},
                                                                                  "applets": []})
    logs = []
    import_plan(portal, tmp_path, PLAN_ID, "Scratch", log=logs.append, skills=skills)
    assert skills.created == ["External"]
    assert any("NEW SKILL created in the target account: 'External'" in m for m in logs)
    # Warned about before any write, as a missing group or schedule is.
    assert "WARNING: skill 'External' does not exist in the target account (used by ST Sample)" in logs
    assert "NOTE: 1 skill is created in the target account: 'External'" in logs
    saved = next(w for w in portal.writes if w[0] == "save-st")
    assert [s["Name"] for s in saved[2]["Skills"]] == ["chat", "External"]  # the target's spelling


def test_dry_run_only_says_which_skills_would_be_created(tmp_path):
    write_doc(tmp_path, exported())
    portal, skills = FakePortal(None), FakeSkills(["Chat"])
    logs = []
    import_plan(portal, tmp_path, PLAN_ID, "Scratch", dry_run=True, log=logs.append, skills=skills)
    assert skills.created == [] and portal.writes == []
    assert any("NOTE: 1 skill would be created in the target account: 'External'" in m for m in logs)


def test_failed_import_deletes_the_skills_it_created(tmp_path):
    write_doc(tmp_path, exported())

    class Failing(FakePortal):
        def save_skill_tagger(self, *a):
            raise PortalError("refused")

    skills = FakeSkills(["Chat"])
    with pytest.raises(PortalError, match="refused"):
        import_plan(Failing(None), tmp_path, PLAN_ID, "Scratch", log=lambda _: None, skills=skills)
    assert skills.created == ["External"] and skills.deleted == ["id-External"]


def test_without_user_admin_access_the_page_list_is_used(tmp_path):
    write_doc(tmp_path, exported())
    logs = []
    with pytest.raises(PortalError, match="cannot be created"):
        import_plan(FakePortal(["Chat"]), tmp_path, PLAN_ID, "Scratch", log=logs.append, skills=FakeSkills([], fail=True))
    assert any("cannot list the target account's skills" in m for m in logs)


def test_skills_client_pages_uses_version_4_and_refuses_a_name_in_other_case():
    from vccflow.skills import SkillsClient, SkillsError

    class Provider:
        def token(self, scope):
            return "t"

    def response(body):
        return MagicMock(status_code=200, json=MagicMock(return_value=body))

    session = MagicMock()
    session.request.side_effect = [
        response({"skills": [{"skillId": "1", "skillName": "DE"}], "meta": {"page": 1, "pageCount": 2}}),
        response({"skills": [{"skillId": "2", "skillName": "Chat"}], "meta": {"page": 2, "pageCount": 2}}),
    ]
    account = Account(name="Demo29", region="emea", allow_portal_writes=True)
    client = SkillsClient(account, session=session, providers=[Provider()], log=lambda _: None)
    assert client.names() == ["DE", "Chat"]
    first, second = session.request.call_args_list
    assert first.args == ("GET", "https://emea.api.newvoicemedia.com/useradmin/skills")
    assert first.kwargs["params"] == {"page": 1, "limit": 500} and second.kwargs["params"]["page"] == 2
    assert first.kwargs["headers"]["Accept"] == "application/vnd.newvoicemedia.v4+json"

    session.request.side_effect = [response({"skills": [{"skillId": "1", "skillName": "DE"}], "meta": {"pageCount": 1}})]
    with pytest.raises(SkillsError, match="'DE' already exists"):
        client.create("de")
