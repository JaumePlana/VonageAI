"""Virtual Assistant and Bot Connector (virtualAssistantAndBot.har): React editors saved through Applet/Save."""

import json
from unittest.mock import MagicMock

import pytest

from vccflow.compare import compare_docs
from vccflow.config import Account
from vccflow.export import VA_KEY_PLACEHOLDER, data_sources_created, data_sources_used, export_applet
from vccflow.importer import import_plan, unsupported_reason

PLAN_ID = "5d3d939c-ffe4-4261-8c0e-4c7ccd1fa82b"
ACCOUNT = Account(name="Demo29", region="emea", allow_portal_writes=True, portal_account="NVMDemo29")
KEY_REF = "0f0f0f0f-1111-2222-3333-444444444444"  # what VCC answers for the key: a GUID, never the key
LISTED = [{"id": 166010870, "guid": "a0f5a730-ea80-11f1-8a44-0ab3d743c8e3", "name": "VA Test"},
          {"id": 166011638, "guid": "f7ef2e5a-ea80-11f1-8a44-0ab3d743c8e3", "name": "SDS Test"},
          {"id": 166011702, "guid": "1e3b3619-ea81-11f1-8a44-0ab3d743c8e3", "name": "BO Test"}]
# VCC's answers to the last saves (#25, #41).
VA = {"DataSourcesToVirtualAssistantParamsMapping": None, "VirtualAssistantParamsToDataSourcesMapping": None,
      "VirtualAgentId": "abcdef", "FailureAppletId": 166011638, "FailureAppletGuid": "f7ef2e5a-ea80-11f1-8a44-0ab3d743c8e3",
      "VonageAiKey": KEY_REF, "VonageAiAgentPhoneNumber": "$(abc)43", "VonageStudioRegion": "", "InteractionType": "Call",
      "VaiResultParameterName": "VIRTUAL_AGENT_RESULT", "DialledNumber": PLAN_ID, "AppletName": "VA Test",
      "DisplayName": "Virtual Assistant", "AppletType": "VirtualAssistant", "NextApplet": "SDS Test",
      "NextAppletGuid": "f7ef2e5a-ea80-11f1-8a44-0ab3d743c8e3", "Url": "Applet/VirtualAssistant",
      "ConfigurationUrl": "Applet", "ApplicationType": 40, "EntityId": 166010870,
      "AppletGuid": "a0f5a730-ea80-11f1-8a44-0ab3d743c8e3"}
BOT = {"InteractionType": "Call", "Provider": "Agentforce", "AgentForce_PstnNumber": "$(Agents_Away)343$(hola)",
       "AgentForce_BotName": "", "AgentForce_SipSupport": True,
       "AgentForce_AgentEscalationApplet": "f7ef2e5a-ea80-11f1-8a44-0ab3d743c8e3",
       "AgentForce_InteractionPlanEscalationApplet": "a0f5a730-ea80-11f1-8a44-0ab3d743c8e3",
       "AgentForce_FailureApplet": "f7ef2e5a-ea80-11f1-8a44-0ab3d743c8e3", "AgentForce_AgentIdDataSourceName": "def",
       "AgentForce_InteractionPlanDataSourceName": "abc", "AgentForce_DigitalFallbackQueue": None, "BotProperties": None,
       "DialledNumber": PLAN_ID, "AppletName": "BO Test", "DisplayName": "Bot Connector", "AppletType": "BotConnector",
       "NextApplet": "", "NextAppletGuid": None, "Url": "Applet/BotConnector", "ConfigurationUrl": "Applet",
       "ApplicationType": 5, "EntityId": 166011702, "AppletGuid": "1e3b3619-ea81-11f1-8a44-0ab3d743c8e3"}


def exported(props, listed=LISTED):
    portal = MagicMock()
    portal.get_applet_state.return_value = {"appletProperties": json.loads(json.dumps(props)), "metadata": {}}
    applet = {"name": props["AppletName"], "appletType": props["DisplayName"],
              "configUrl": f"Applet?application={props['AppletType']}"}
    return export_applet(portal, PLAN_ID, applet, None, listed)


# --- Virtual Assistant ---

def test_va_export_keeps_whether_a_key_is_set_never_its_reference():
    e = exported(VA)
    assert e["type"] == "VirtualAssistant"
    assert e["settings"]["VonageAiKeySet"] is True and "VonageAiKey" not in e["settings"]
    assert KEY_REF not in json.dumps(e)
    assert e["settings"]["VirtualAgentId"] == "abcdef" and e["settings"]["VonageAiAgentPhoneNumber"] == "$(abc)43"
    assert e["next"] == {"NextApplet": "SDS Test", "FailureApplet": "SDS Test"}
    assert "connectionProblems" not in e and unsupported_reason(e) is None


def test_the_va_number_reads_its_data_sources():
    assert data_sources_used(exported(VA)) == {"abc"}


@pytest.mark.parametrize("change, message", [
    ({"VirtualAgentId": ""}, "ID '' is not 1 to 50 characters"),
    ({"VonageAiAgentPhoneNumber": None}, "Number None is not 1 to 50 characters"),
    ({"VonageAiAgentPhoneNumber": "+" + "1" * 50}, "is not 1 to 50 characters"),
    ({"DataSourcesToVirtualAssistantParamsMapping": [{"x": 1}]}, "DataSourcesToVirtualAssistantParamsMapping has not been recorded"),
    ({"Other": 1}, "settings Other have not been recorded yet"),
])
def test_va_refusals(change, message):
    e = exported(VA)
    e["settings"].update(change)
    assert message in unsupported_reason(e)


# --- Bot Connector ---

def test_bot_export_keeps_its_settings_and_the_routing_applets_by_name():
    e = exported(BOT)
    assert e["type"] == "BotConnector"
    assert e["next"] == {"AgentForce_AgentEscalationApplet": "SDS Test",
                         "AgentForce_InteractionPlanEscalationApplet": "VA Test", "AgentForce_FailureApplet": "SDS Test"}
    for k in ("AgentForce_AgentEscalationApplet", "AgentForce_InteractionPlanEscalationApplet", "AgentForce_FailureApplet"):
        assert k not in e["settings"]
    assert e["settings"]["AgentForce_SipSupport"] is True and e["settings"]["Provider"] == "Agentforce"
    assert "connectionProblems" not in e and unsupported_reason(e) is None


def test_a_bot_guid_outside_the_plan_is_a_connection_problem():
    e = exported({**BOT, "AgentForce_FailureApplet": "99999999-9999-9999-9999-999999999999"})
    assert e["connectionProblems"] == ["'BO Test' AgentForce_FailureApplet 99999999-9999-9999-9999-999999999999 "
                                       "is not in this plan"]


def test_the_bot_data_source_names_are_set_and_the_number_reads_its_own():
    e = exported(BOT)
    assert data_sources_created(e) == {"abc", "def"}
    assert data_sources_used(e) == {"Agents_Away", "hola"}


@pytest.mark.parametrize("change, message", [
    ({"AgentForce_PstnNumber": ""}, "phone number '' is not 1 to 80 characters"),
    ({"Provider": "Other"}, "provider 'Other' has not been recorded yet"),
    ({"BotProperties": {"a": 1}}, "BotProperties has not been recorded yet"),
])
def test_bot_refusals(change, message):
    e = exported(BOT)
    e["settings"].update(change)
    assert message in unsupported_reason(e)


# --- import and comparison ---

class FakePortal:
    account = ACCOUNT

    def __init__(self):
        self.applets, self.writes = {}, []

    def list_applets(self, plan_id):
        return {"name": "Scratch", "applets": [{"id": 100 + i, "guid": f"0000000{i}-0000-0000-0000-000000000000",
                                                "name": n, "appletType": t}
                                               for i, (n, t) in enumerate(self.applets.items())]}

    def list_data_sources(self, plan_id):
        return {"abc", "Agents_Away", "hola"}

    def create_applet(self, plan_id, applet_type, name):
        self.writes.append(("create", name, applet_type))
        self.applets[name] = applet_type

    def delete_applet(self, plan_id, name):
        self.writes.append(("delete", name))

    def get_applet_state(self, plan_id, applet):
        return {"appletProperties": {"AppletType": self.applets[applet["name"]], "AppletName": applet["name"],
                                     "VonageAiKey": "", "NextApplet": None, "NextAppletGuid": None}}

    def save_applet(self, plan_id, name, props):
        self.writes.append(("save", name, json.loads(json.dumps(props))))

    def set_initial_applet(self, plan_id, name):
        self.writes.append(("initial", name))


def test_import_saves_the_va_with_the_placeholder_key_and_the_bot_with_the_targets_guids(tmp_path, monkeypatch):
    monkeypatch.setattr("vccflow.importer.export.export_plan",
                        lambda *a, **k: {"plan": {"initialApplet": "VA Test"}, "applets": []})
    sds = {"name": "SDS Test", "type": "SetDataSource", "settings": {"DataSources": []}, "next": {}}
    doc = {"format": "vccflow.plan/1", "plan": {"name": "Source", "initialApplet": "VA Test"},
           "applets": [exported(VA), exported(BOT), sds]}
    (tmp_path / "plan.json").write_text(json.dumps(doc), encoding="utf-8")
    portal, logs = FakePortal(), []
    import_plan(portal, tmp_path, PLAN_ID, "Scratch", log=logs.append)
    target = {a["name"]: a for a in portal.list_applets(PLAN_ID)["applets"]}
    saved = {w[1]: w[2] for w in portal.writes if w[0] == "save"}
    va = saved["VA Test"]
    assert va["VonageAiKey"] == VA_KEY_PLACEHOLDER and "VonageAiKeySet" not in va
    assert va["FailureAppletId"] == target["SDS Test"]["id"] and va["FailureAppletGuid"] == target["SDS Test"]["guid"]
    assert va["NextApplet"] == "SDS Test" and va["VirtualAgentId"] == "abcdef"
    assert ("WARNING: Virtual Assistant 'VA Test' is copied with the API key 'SET-IN-PORTAL': VCC never returns the "
            "key, so it cannot be copied. Enter it in the portal.") in logs
    bot = saved["BO Test"]
    assert bot["AgentForce_AgentEscalationApplet"] == target["SDS Test"]["guid"]
    assert bot["AgentForce_InteractionPlanEscalationApplet"] == target["VA Test"]["guid"]
    assert bot["AgentForce_FailureApplet"] == target["SDS Test"]["guid"]
    assert bot["AgentForce_PstnNumber"] == "$(Agents_Away)343$(hola)" and bot["AgentForce_SipSupport"] is True


def test_comparison_sees_changes_but_not_the_key_reference():
    doc = lambda *x: {"plan": {}, "applets": [*x, {"name": "SDS Test", "type": "SetDataSource", "settings": {},
                                                    "next": {}}]}
    va, bot = exported(VA), exported(BOT)
    # Another applet's key reference (every applet has its own): no difference.
    other_ref = exported({**VA, "VonageAiKey": "99999999-0000-0000-0000-000000000000"})
    assert compare_docs(doc(va, bot), doc(other_ref, bot))["differences"] == []
    va2, bot2 = json.loads(json.dumps(va)), json.loads(json.dumps(bot))
    va2["settings"]["VirtualAgentId"] = "other"
    bot2["settings"]["AgentForce_SipSupport"] = False
    bot2["next"]["AgentForce_FailureApplet"] = "VA Test"
    diffs = compare_docs(doc(va, bot), doc(va2, bot2))["differences"]
    assert "'VA Test' setting VirtualAgentId: source abcdef | target other" in diffs
    assert "'BO Test' setting AgentForce_SipSupport: source true | target false" in diffs
    assert "'BO Test' path AgentForce_FailureApplet is different: source 'SDS Test' | target 'VA Test'" in diffs
    no_key = json.loads(json.dumps(va))
    no_key["settings"]["VonageAiKeySet"] = False
    assert "'VA Test' setting VonageAiKeySet: source true | target false" in compare_docs(doc(va, bot), doc(no_key, bot))["differences"]
