"""Data Connector form posts and the initial applet, checked against the recorded HARs."""

import re
from unittest.mock import MagicMock

import pytest

from vccflow.config import Account
from vccflow.portal import PortalClient, PortalError, data_connector_form, extract_initial_applet

PLAN_ID = "e9d0c8ab-741a-4d86-bd98-27f793d2a72e"
NEXT = {"NextApplet": "ANN TTS", "NextAppletFailure": "ANN Test Claude", "NextAppletInterfaceFailure": "LOG Audio Failed"}


def salesforce_get(**crm):
    return {
        "DataProvider": "Salesforce", "Action": "GetSalesforceData", "MatchObjectName": "Case",
        "ResponseItems": ["Case|Origin", "Case|Category__c"],
        "CrmRetrievalViewModel": {"MatchBy": "CLID", "UseRawSoql": False, "MaxRecords": None, **crm},
    }


def test_web_service_form():
    form = data_connector_form(
        {"DataProvider": "WebService", "Action": "CallWebService", "ApiInteractionDefinition": "<ApiInteraction/>"},
        {"NextApplet": "DC Get Client", "NextAppletFailure": "LOG Audio Failed"},
    )
    assert form == [
        ("DataProvider", "WebService"), ("Action", "CallWebService"),
        ("SerialisedTemplatePlaceholders", "[]"), ("ApiInteractionDefinition", "<ApiInteraction/>"),
        ("NextApplet", "DC Get Client"), ("NextAppletFailure", "LOG Audio Failed"), ("NextAppletInterfaceFailure", ""),
    ]


def test_no_next_applets_sends_empty_outcomes():
    # dcwithoutnextapplets.har: the page posts the three outcomes empty.
    form = dict(data_connector_form({"DataProvider": "WebService", "Action": "CallWebService"}, {}))
    assert form["NextApplet"] == form["NextAppletFailure"] == form["NextAppletInterfaceFailure"] == ""
    assert "NextAppletMultipleMatch" not in form


def test_salesforce_get_by_data_source_multiple_records():
    form = dict(data_connector_form(
        salesforce_get(MatchBy="Data Source", MatchDataSource="CallGuid", FieldToMatch="Case|Id", MaxRecords=2), NEXT))
    assert form["MatchBy"] == "Data Source" and form["MatchDataSource"] == "CallGuid" and form["FieldToMatch"] == "Case|Id"
    assert form["RetrieveMultiple"] == "on" and form["MaxRecords"] == "2"
    assert form["SerialisedResponseItems"] == '["Case|Origin","Case|Category__c"]'
    assert form["NextAppletInterfaceFailure"] == "LOG Audio Failed"


def test_salesforce_get_by_clid_single_record():
    form = dict(data_connector_form(salesforce_get(), {**NEXT, "NextAppletMultipleMatch": "ANN Audio"}))
    assert form["MatchBy"] == "CLID" and "MatchDataSource" not in form and "RetrieveMultiple" not in form
    assert form["NextAppletMultipleMatch"] == "ANN Audio"


def test_salesforce_raw_soql():
    soql = "SELECT Status, ID FROM Case WHERE ID = '$(Account ID)' LIMIT 2"
    form = data_connector_form(salesforce_get(UseRawSoql=True, Soql=soql, MatchBy=""), NEXT)
    assert ("UseRawSoql", "true") in form and ("Soql", soql) in form
    assert "MatchObjectName" not in dict(form)


def test_salesforce_update():
    settings = {
        "DataProvider": "Salesforce", "Action": "UpdateSalesforceObject", "MatchObjectName": "VoiceCall",
        "CrmCreationViewModel": {"CrmObjectParameters": [
            {"DataSourceType": 0, "CrmField": "VoiceCall|CallDurationInSeconds", "Value": "Case Country"},
        ]},
    }
    form = dict(data_connector_form(settings, {"NextApplet": "LOG Audio Failed"}))
    assert form["SerialisedResponseItems"] == (
        '[{"crmField":"VoiceCall|CallDurationInSeconds","dataSourceType":0,"value":"Case Country"}]')
    assert form["MatchObjectName"] == "VoiceCall" and form["NextApplet"] == "LOG Audio Failed"


def test_unrecorded_cases_are_refused():
    with pytest.raises(PortalError, match="not been recorded"):
        data_connector_form({"DataProvider": "Zendesk", "Action": "x"}, {})
    with pytest.raises(PortalError, match="IVR Slot"):
        data_connector_form(salesforce_get(MatchBy="IVR Slot"), {})


PLAN_PAGE = f"""
<tr id="{PLAN_ID}">
  <td valign="top">DISH Web Services<br></td>
  <td valign="top" style="border-right: 1px solid #CCCCCC;">
    <a href="Applet?phonenum={PLAN_ID}&amp;myapp=ANN Audio&amp;application=DataAnnouncer">ANN Audio</a>
  </td>
  <td id="Applets"></td>
</tr>
"""


def test_extract_initial_applet():
    assert extract_initial_applet(PLAN_PAGE, PLAN_ID) == "ANN Audio"
    # A plan without one, followed by a plan that has one: must not pick up the next plan's applet.
    empty = re.sub(r"<a .*?</a>", "WARNING initial applet not selected", PLAN_PAGE, flags=re.S)
    assert extract_initial_applet(empty.replace(PLAN_ID, "plan-a") + PLAN_PAGE, "plan-a") is None
    with pytest.raises(PortalError):
        extract_initial_applet(PLAN_PAGE, "other-plan")


def test_set_initial_applet_posts_the_form():
    session = MagicMock()
    resp = MagicMock(status_code=301, is_redirect=True, headers={"Location": "/CallCentre/apps/applications.asp"})
    session.request.return_value = resp
    PortalClient(Account(name="t", region="emea", allow_portal_writes=True), session=session).set_initial_applet(PLAN_ID, "ANN Audio")
    _, kwargs = session.request.call_args
    assert session.request.call_args[0][1].endswith("InteractionArchitect/SetInitialApplet")
    assert kwargs["data"] == {"interactionPlanId": PLAN_ID, "initialApplet": "ANN Audio", "Update": "Set"}
