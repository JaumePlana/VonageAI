import base64
import json
from unittest.mock import MagicMock

import pytest

from vccflow.config import Account
from vccflow.poc import resolve_plan, run_announcer_poc
from vccflow.portal import PortalClient, PortalError, SessionExpired, encode_properties, extract_initial_state

PLAN_ID = "e9d0c8ab-741a-4d86-bd98-27f793d2a72e"


def account(writes=True):
    return Account(name="test", region="emea", allow_portal_writes=writes)


def response(status=200, *, json_body=None, text="", location=None):
    r = MagicMock()
    r.status_code = status
    r.is_redirect = status in (301, 302, 303)
    r.headers = {"Location": location} if location else {}
    r.text = text
    r.json.return_value = json_body
    return r


def portal_with(*responses, writes=True):
    session = MagicMock()
    session.request.side_effect = list(responses)
    return PortalClient(account(writes), session=session), session


def test_extract_initial_state():
    html = (
        '<script>$(document).ready(function() { var initialState = {"interactionPlanId":"x",'
        '"appletProperties":{"Text":"a }; b","Voice":"Amy"}}; Bundle.init(initialState); });</script>'
    )
    state = extract_initial_state(html)
    assert state["appletProperties"] == {"Text": "a }; b", "Voice": "Amy"}


def test_encode_properties_roundtrip():
    props = {"Text": "Hola, què tal?", "BargeIn": True}
    assert json.loads(base64.b64decode(encode_properties(props))) == props


def test_redirect_to_login_means_session_expired():
    portal, _ = portal_with(response(302, location="https://emea.newvoicemedia.com/Auth/connect/authorize?x"))
    with pytest.raises(SessionExpired, match="vccflow login"):
        portal.list_applets(PLAN_ID)


def test_writes_require_opt_in():
    portal, session = portal_with(writes=False)
    with pytest.raises(PortalError, match="allow_portal_writes"):
        portal.create_applet(PLAN_ID, "DataAnnouncer", "ANN")
    session.request.assert_not_called()


def test_save_sends_base64_properties():
    portal, session = portal_with(response(json_body={"AppletProperties": {}}))
    portal.save_applet(PLAN_ID, "ANN", {"Text": "hi"})
    body = session.request.call_args.kwargs["json"]
    assert body["interactionPlanId"] == PLAN_ID and body["appletName"] == "ANN"
    assert json.loads(base64.b64decode(body["appletProperties"])) == {"Text": "hi"}


def test_resolve_plan_detects_account_mismatch():
    portal = MagicMock()
    portal.list_applets.return_value = {"name": "Some other plan", "applets": []}
    ipm = MagicMock()
    ipm.get_plan.return_value = {"interactionPlanName": "zz Claude POC"}
    with pytest.raises(PortalError, match="mismatch"):
        resolve_plan(PLAN_ID, portal, ipm)


def test_resolve_plan_by_name():
    portal = MagicMock()
    portal.list_applets.return_value = {"name": "zz Claude POC", "applets": []}
    ipm = MagicMock()
    ipm.list_plans.return_value = [{"interactionPlanId": PLAN_ID, "interactionPlanName": "zz Claude POC"}]
    assert resolve_plan("zz Claude POC", portal, ipm) == (PLAN_ID, "zz Claude POC")


class FakePortal:
    """In-memory stand-in for PortalClient."""

    def __init__(self, drop_text=False):
        self.applets = {}
        self.drop_text = drop_text

    def list_applets(self, plan_id):
        return {"name": "zz Claude POC", "applets": [{"name": n, "configUrl": f"Applet?myapp={n}"} for n in self.applets]}

    def find_applet(self, plan_id, name):
        return {"name": name, "configUrl": f"Applet?myapp={name}"}

    def create_applet(self, plan_id, applet_type, name):
        self.applets[name] = {"AppletName": name, "AppletType": applet_type, "Text": None, "EntityId": 1}

    def get_applet_state(self, plan_id, applet):
        return {"appletProperties": dict(self.applets[applet["name"]])}

    def save_applet(self, plan_id, name, props):
        self.applets[name] = {**props, "Text": None} if self.drop_text else dict(props)
        return {}

    def delete_applet(self, plan_id, name):
        del self.applets[name]


def test_poc_creates_verifies_and_deletes():
    fake = FakePortal()
    saved = run_announcer_poc(fake, PLAN_ID, "ANN POC", "Hello", log=lambda _: None)
    assert saved["Text"] == "Hello" and saved["EntityId"] == 1  # defaults kept
    assert fake.applets == {}


def test_poc_keep_leaves_applet():
    fake = FakePortal()
    run_announcer_poc(fake, PLAN_ID, "ANN POC", "Hello", keep=True, log=lambda _: None)
    assert "ANN POC" in fake.applets


def test_poc_fails_when_readback_differs():
    with pytest.raises(PortalError, match="differ"):
        run_announcer_poc(FakePortal(drop_text=True), PLAN_ID, "ANN POC", "Hello", log=lambda _: None)


def test_poc_refuses_existing_name():
    fake = FakePortal()
    fake.applets["ANN POC"] = {}
    with pytest.raises(PortalError, match="already exists"):
        run_announcer_poc(fake, PLAN_ID, "ANN POC", "Hello", log=lambda _: None)
