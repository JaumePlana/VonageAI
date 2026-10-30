"""Plans from the portal's plan page, for accounts without the Interaction Plan API."""

import pytest

from vccflow.ipm import IpmError
from vccflow.poc import resolve_plan
from vccflow.portal import PortalError, extract_plans


def row(plan_id, name, initial=None):
    link = f'<a href="Applet?phonenum={plan_id}&amp;myapp={initial}">{initial}</a>' if initial else "WARNING initial applet not selected"
    return f"""
<tr id="{plan_id}">
  <td valign="top">
    {name}<br>
    Interaction plan type: Default
    <br /><br /><a href="AppletCopy?sourceInteractionPlanId={plan_id}">Copy To</a>
  </td>
  <td valign="top" style="border-right: 1px solid #CCCCCC;">{link}</td>
  <td id="Applets"></td>
</tr>"""


PAGE = row("3238080946", "3238080946 - Std IVR", "ANN Welcome") + row("NVMDemo29-4", "Main &amp; Co") + \
    row("fc2382c8-9cbc-4172-a219-420976de4e18", "WCB", "ACD TEst")


def test_extract_plans_handles_old_ids_and_missing_initial_applet():
    plans = extract_plans(PAGE)
    assert [p["interactionPlanId"] for p in plans] == ["3238080946", "NVMDemo29-4", "fc2382c8-9cbc-4172-a219-420976de4e18"]
    assert plans[1] == {"interactionPlanId": "NVMDemo29-4", "interactionPlanName": "Main & Co",
                        "type": "Default", "initialApplet": None}
    assert plans[2]["initialApplet"] == "ACD TEst"


class FakePortal:
    def list_plans(self):
        return extract_plans(PAGE)

    def list_applets(self, plan_id):
        return {"name": {p["interactionPlanId"]: p["interactionPlanName"] for p in self.list_plans()}[plan_id]}


class DisabledApi:
    def list_plans(self):
        raise IpmError("Feature not enabled", 403)

    get_plan = list_plans


def test_resolve_plan_falls_back_to_portal_when_api_is_off():
    assert resolve_plan("WCB", FakePortal(), DisabledApi()) == ("fc2382c8-9cbc-4172-a219-420976de4e18", "WCB")
    assert resolve_plan("NVMDemo29-4", FakePortal(), None) == ("NVMDemo29-4", "Main & Co")
    with pytest.raises(PortalError, match="found 0"):
        resolve_plan("Nope", FakePortal(), None)


def test_other_api_errors_are_not_hidden():
    class BrokenApi:
        def list_plans(self):
            raise IpmError("boom", 500)

    with pytest.raises(IpmError):
        resolve_plan("WCB", FakePortal(), BrokenApi())


def test_old_style_id_resolves_through_the_api():
    class Api:
        def list_plans(self):
            return [{"interactionPlanId": "NVMDemo29-4", "interactionPlanName": "Main & Co"}]

    assert resolve_plan("NVMDemo29-4", FakePortal(), Api()) == ("NVMDemo29-4", "Main & Co")
