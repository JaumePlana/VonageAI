"""Route to Interaction Plan (appletType "Shim", shim.aspx; routeToInteractionPlan.har)."""

import json
from unittest.mock import MagicMock

import pytest

from vccflow.compare import compare_docs
from vccflow.config import Account
from vccflow.export import data_sources_used, export_applet, split_shim
from vccflow.importer import import_plan, unsaved_routes, unsupported_reason
from vccflow.portal import PortalClient, PortalError, check_shim, extract_shim

PLAN_ID = "b918444e-7c29-41b0-9a65-9a64b8568257"
COPY_ID = "3dc82ac3-9734-4d9f-b79d-dd6b8988ae1b"
PLANS = [("", ""), (COPY_ID, "POC Copy Claude"), ("NVMDemo29-5", "AdvDemo - Self Services")]
OWN_APPLETS = ["", "ANN Audio", "DC Authenticate", "DC JavaScript"]
ACCOUNT = Account(name="Demo29", region="emea", allow_portal_writes=True, portal_account="NVMDemo29")


def select(name, options, selected=None):
    opts = "".join(f'<option{" selected=\"selected\"" if v == selected else ""} value="{v}">{t}</option>'
                   for v, t in options)
    return f'<select name="datagrid$ctl02${name}" onchange="__doPostBack()" id="x_{name}">{opts}</select>'


def page(route="Interaction plan", plan="", applets=("",), applet=None, data_source=None, failure="", state="S0",
         message="", ivr=False):
    """A shim.aspx page as VCC renders it (recorded structure, shortened)."""
    type_select = select("ddShimType", [(route, route)], route)
    if route == "Interaction plan":
        fields = (select("ddDialledNbr", PLANS, plan) + select("ddShimApp", [(a, f"{a} (Announcer)" if a else "") for a in applets],
                                                                applet))
    else:
        value = f' value="{data_source}"' if data_source is not None else ""
        fields = f'<input name="datagrid$ctl02$tbDataSourceName" type="text"{value} id="tb" />'
    failure_select = select("ddFailureApplet", [(a, f"{a} (x)" if a else "") for a in OWN_APPLETS], failure)
    box = '<input id="b" type="checkbox" name="datagrid3$ctl02$IVR3" checked="checked" />' if ivr else \
        '<input id="b" type="checkbox" name="datagrid3$ctl02$IVR3" />'
    return (f'<form method="POST" action="./shim.aspx?phonenum={PLAN_ID}" id="Form1">'
            '<input type="hidden" name="__EVENTTARGET" value="" /><input type="hidden" name="__EVENTARGUMENT" value="" />'
            f'<input type="hidden" name="__LASTFOCUS" value="" /><input type="hidden" name="__VIEWSTATE" value="{state}" />'
            f'<input type="hidden" name="__EVENTVALIDATION" value="E{state}" />'
            f'<span id="myLabel1" style="font-size:Small;">{message}</span>'
            f'{type_select}{fields}{failure_select}{box}'
            '<select name="datagrid3$ctl02$shimSourceSlot" disabled="disabled"></select>'
            '<input type="submit" name="Update" value="Update" id="Update" /></form>')


def ok(text):
    return MagicMock(status_code=200, is_redirect=False, headers={}, text=text)


SAVED = MagicMock(status_code=302, is_redirect=True, text="", headers={
    "Location": f"/CallCentre/apps/updated.asp?phonenum={PLAN_ID}&myapp=R&application=Shim&name=shim"
                f"&dd1={COPY_ID}&dd2=ANN Audio&nextAppletFail=DC Authenticate"})
# updated.asp saves what the redirect carries, and says so.
UPDATED = MagicMock(status_code=200, is_redirect=False, headers={},
                    text='<td id="statusMessage" class="subSection"> Your details have been successfully updated </td>')


def client(*responses):
    session = MagicMock()
    session.get.return_value = MagicMock(status_code=200, text="var user = { account: 'NVMDemo29' };")
    session.request.side_effect = list(responses)
    return PortalClient(ACCOUNT, session=session), session


def posts(session):
    return [c.kwargs["data"] for c in session.request.call_args_list if c.args[0] == "POST"]


TO_PLAN = {"RouteType": "Interaction plan", "TargetPlan": "POC Copy Claude", "TargetApplet": "ANN Audio",
           "DataSourceName": "", "TransferIvrData": False, "TransferDdi": False, "TransferAgentId": False}
TO_DATA_SOURCE = {**TO_PLAN, "RouteType": "Data source", "TargetPlan": "", "TargetApplet": "",
                  "DataSourceName": "$(Account_ID)"}


# --- reading ---

def test_page_is_read_with_the_target_plan_by_name():
    r = extract_shim(page(plan="NVMDemo29-5", applets=["ANN EN Client ID Not Found"], failure="DC Authenticate"))
    assert (r["RouteType"], r["TargetPlan"], r["TargetPlanId"], r["TargetApplet"], r["FailureApplet"]) == (
        "Interaction plan", "AdvDemo - Self Services", "NVMDemo29-5", "ANN EN Client ID Not Found", "DC Authenticate")
    r = extract_shim(page(route="Data source", data_source="$(Account_ID)", failure="DC JavaScript",
                          message="Please provide data source name."))
    assert (r["RouteType"], r["DataSourceName"], r["TargetPlan"], r["Message"]) == (
        "Data source", "$(Account_ID)", "", "Please provide data source name.")
    assert extract_shim(page(ivr=True))["TransferIvrData"] is True


def test_export_keeps_plan_and_applet_as_settings_and_failure_as_a_connection():
    settings, nxt = split_shim(extract_shim(page(plan=COPY_ID, applets=["ANN Audio"], failure="DC Authenticate")))
    assert settings == TO_PLAN and nxt == {"FailureApplet": "DC Authenticate"}
    assert "TargetPlanId" not in settings  # plan IDs differ per account


def test_export_applet_reads_the_shim_page():
    portal = MagicMock()
    portal.get_shim.return_value = extract_shim(page(route="Data source", data_source="$(Account_ID)"))
    applet = {"name": "Route to Somewhere", "appletType": "Route To Interaction Plan",
              "configUrl": f"shim.aspx?phoneNum={PLAN_ID}&myApp=Route to Somewhere&application=Shim"}
    entry = export_applet(portal, PLAN_ID, applet, None)
    assert entry["type"] == "Shim" and entry["settings"] == TO_DATA_SOURCE and entry["next"] == {}


def test_the_data_source_is_read_without_its_notation():
    assert data_sources_used({"type": "Shim", "settings": TO_DATA_SOURCE}) == {"Account_ID"}
    assert data_sources_used({"type": "Shim", "settings": TO_PLAN}) == set()


@pytest.mark.parametrize("change, message", [
    ({"TransferIvrData": True}, "IVR data transfer"),
    ({"RouteType": "Data source", "DataSourceName": "LOG"}, r"\$\(dataSourceName\) notation"),
    ({"RouteType": "Data source", "DataSourceName": "$Log"}, "notation"),
    ({"RouteType": "Queue"}, "has not been recorded"),
])
def test_unrecorded_or_invalid_cases_are_refused(change, message):
    with pytest.raises(PortalError, match=message):
        check_shim({**TO_PLAN, **change})
    assert unsupported_reason({"name": "R", "type": "Shim", "settings": {**TO_PLAN, **change}})


# --- saving ---

def test_save_to_a_plan_chooses_the_plan_then_its_applet_as_the_browser_does():
    portal, session = client(ok(page(state="S1")), ok(page(plan=COPY_ID, applets=["ANN Audio", "LOG Test"], state="S2")),
                             SAVED, UPDATED,
                             ok(page(plan=COPY_ID, applets=["ANN Audio", "LOG Test"], applet="ANN Audio",
                                     failure="DC Authenticate")))
    saved = portal.save_shim(PLAN_ID, "Route to Somewhere", TO_PLAN, {"FailureApplet": "DC Authenticate"})
    choose_plan, save = posts(session)
    assert choose_plan == {"__EVENTTARGET": "datagrid$ctl02$ddDialledNbr", "__EVENTARGUMENT": "", "__LASTFOCUS": "",
                           "__VIEWSTATE": "S1", "__EVENTVALIDATION": "ES1",
                           "datagrid$ctl02$ddShimType": "Interaction plan", "datagrid$ctl02$ddDialledNbr": COPY_ID,
                           "datagrid$ctl02$ddShimApp": "", "datagrid$ctl02$ddFailureApplet": ""}
    assert save == {"__EVENTTARGET": "", "__EVENTARGUMENT": "", "__LASTFOCUS": "", "__VIEWSTATE": "S2",
                    "__EVENTVALIDATION": "ES2", "datagrid$ctl02$ddShimType": "Interaction plan",
                    "datagrid$ctl02$ddDialledNbr": COPY_ID, "datagrid$ctl02$ddShimApp": "ANN Audio",
                    "datagrid$ctl02$ddFailureApplet": "DC Authenticate", "Update": "Update"}
    assert (saved["TargetPlan"], saved["TargetApplet"], saved["FailureApplet"]) == (
        "POC Copy Claude", "ANN Audio", "DC Authenticate")
    opened = session.request.call_args_list[3]
    assert opened.args[0] == "GET" and opened.args[1].endswith(
        f"/apps/updated.asp?phonenum={PLAN_ID}&myapp=R&application=Shim&name=shim&dd1={COPY_ID}&dd2=ANN Audio"
        "&nextAppletFail=DC Authenticate")


def test_a_save_that_does_not_read_back_is_an_error():
    # As found live: without updated.asp the applet stayed empty.
    portal, _ = client(ok(page()), ok(page(route="Data source")), SAVED, UPDATED, ok(page()))
    with pytest.raises(PortalError, match="reads back with RouteType 'Interaction plan' instead of 'Data source'"):
        portal.save_shim(PLAN_ID, "R", TO_DATA_SOURCE, {})


def test_updated_page_without_success_is_an_error():
    portal, _ = client(ok(page()), ok(page(route="Data source")), SAVED,
                       ok('<td id="statusMessage"> Something went wrong </td>'))
    with pytest.raises(PortalError, match="not confirmed: Something went wrong"):
        portal.save_shim(PLAN_ID, "R", TO_DATA_SOURCE, {})


def test_save_to_a_data_source_switches_the_type_first():
    portal, session = client(ok(page(state="S1")), ok(page(route="Data source", state="S2")), SAVED, UPDATED,
                             ok(page(route="Data source", data_source="$(Account_ID)")))
    portal.save_shim(PLAN_ID, "Route to Somewhere", TO_DATA_SOURCE, {})
    switch, save = posts(session)
    assert switch["__EVENTTARGET"] == "datagrid$ctl02$ddShimType" and switch["datagrid$ctl02$ddShimType"] == "Data source"
    assert save == {"__EVENTTARGET": "", "__EVENTARGUMENT": "", "__LASTFOCUS": "", "__VIEWSTATE": "S2",
                    "__EVENTVALIDATION": "ES2", "datagrid$ctl02$ddShimType": "Data source",
                    "datagrid$ctl02$tbDataSourceName": "$(Account_ID)", "datagrid$ctl02$ddFailureApplet": "",
                    "Update": "Update"}


def test_refused_save_reports_the_page_message():
    portal, _ = client(ok(page(route="Data source")),
                       ok(page(route="Data source", message="Please provide data source name in correct notation")))
    with pytest.raises(PortalError, match="was refused: Please provide data source name in correct notation"):
        portal.save_shim(PLAN_ID, "R", TO_DATA_SOURCE, {})


@pytest.mark.parametrize("settings, message", [
    ({**TO_PLAN, "TargetPlan": "Elsewhere"}, "Interaction Plan 'Elsewhere' is not offered"),
    ({**TO_PLAN, "TargetApplet": "Nope"}, "applet 'Nope' is not in Interaction Plan 'POC Copy Claude'"),
])
def test_a_missing_plan_or_applet_stops_before_saving(settings, message):
    portal, session = client(ok(page()), ok(page(plan=COPY_ID, applets=["ANN Audio"])))
    with pytest.raises(PortalError, match=message):
        portal.save_shim(PLAN_ID, "R", settings, {})
    assert not any("Update" in p for p in posts(session))


def test_failure_applet_alone_is_not_saved():
    with pytest.raises(PortalError, match="VCC does not save it"):
        client()[0].save_shim(PLAN_ID, "R", {**TO_PLAN, "TargetPlan": ""}, {"FailureApplet": "ANN Audio"})


# --- copying ---

TARGET_PLANS = [{"interactionPlanId": "p-copy", "interactionPlanName": "POC Copy Claude"},
                {"interactionPlanId": "p-scratch", "interactionPlanName": "Scratch"},
                {"interactionPlanId": "p-a", "interactionPlanName": "Twice"},
                {"interactionPlanId": "p-b", "interactionPlanName": "Twice"}]


def route(name, **settings):
    return {"name": name, "type": "Shim", "settings": {**TO_PLAN, **settings}, "next": {"FailureApplet": "DR Decimal"}}


def test_routes_that_cannot_be_saved_in_the_target_are_found_by_name():
    applets = [route("ok"), route("no plan", TargetPlan="Gone"), route("no applet", TargetApplet="Gone"),
               route("itself", TargetPlan="Scratch"), route("twice", TargetPlan="Twice"),
               route("empty", TargetPlan="", TargetApplet=""), route("ds", **{k: v for k, v in TO_DATA_SOURCE.items()})]
    found = unsaved_routes(applets, TARGET_PLANS, lambda pid: ["ANN Audio"] if pid == "p-copy" else [], "p-scratch")
    assert found == {
        "no plan": "Interaction Plan 'Gone' does not exist in the target account",
        "no applet": "applet 'Gone' does not exist in Interaction Plan 'POC Copy Claude' of the target account",
        "itself": "it routes to Interaction Plan 'Scratch', the plan it is copied into, which VCC does not allow",
        "twice": "the target account has 2 Interaction Plans named 'Twice'",
        "empty": "it is not configured in the source either",
    }


class FakePortal:
    account = ACCOUNT

    def __init__(self):
        self.applets, self.writes = {}, []

    def list_applets(self, plan_id):
        if plan_id == "p-copy":
            return {"name": "POC Copy Claude", "applets": [{"name": "ANN Audio"}]}
        return {"name": "Scratch", "applets": [{"id": i, "name": n, "guid": f"g-{n}", "appletType": t}
                                               for i, (n, t) in enumerate(self.applets.items())]}

    def list_plans(self):
        return TARGET_PLANS

    def list_data_sources(self, plan_id):
        return {"Account_ID"}

    def create_applet(self, plan_id, applet_type, name):
        self.writes.append(("create", name, applet_type))
        self.applets[name] = applet_type

    def get_applet_state(self, plan_id, applet):
        return {"appletProperties": {"AppletType": self.applets[applet["name"]]}}

    def save_applet(self, plan_id, name, props):
        self.writes.append(("save", name))

    def save_shim(self, plan_id, name, settings, nxt):
        self.writes.append(("save-shim", name, settings["TargetPlan"], nxt))

    def set_initial_applet(self, plan_id, name):
        self.writes.append(("initial", name))


def write_doc(tmp_path, *applets):
    target = {"name": "DR Decimal", "type": "SetDataSource", "settings": {"DataSources": []}, "next": {}}
    doc = {"format": "vccflow.plan/1", "plan": {"name": "Source", "initialApplet": "DR Decimal"},
           "applets": [*applets, target]}
    (tmp_path / "plan.json").write_text(json.dumps(doc), encoding="utf-8")


def test_import_saves_a_route_whose_plan_exists_and_leaves_the_others_empty_with_a_warning(tmp_path, monkeypatch):
    write_doc(tmp_path, route("R ok"), route("R gone", TargetPlan="Gone"))
    monkeypatch.setattr("vccflow.importer.export.export_plan", lambda *a, **k: {
        "plan": {"initialApplet": "DR Decimal"},
        "applets": [route("R ok"), {**route("R gone", TargetPlan="", TargetApplet=""), "next": {}},
                    {"name": "DR Decimal", "type": "SetDataSource", "settings": {"DataSources": []}, "next": {}}]})
    portal, logs = FakePortal(), []
    diffs = import_plan(portal, tmp_path, "p-scratch", "Scratch", log=logs.append)
    assert ("create", "R gone", "Shim") in portal.writes and ("create", "R ok", "Shim") in portal.writes
    assert ("save-shim", "R ok", "POC Copy Claude", {"FailureApplet": "DR Decimal"}) in portal.writes
    assert not any(w[0] == "save-shim" and w[1] == "R gone" for w in portal.writes)
    assert any("WARNING: Route to Interaction Plan 'R gone' is copied without its settings: Interaction Plan 'Gone' "
               "does not exist in the target account" in m and "failure applet 'DR Decimal' is not set either" in m
               for m in logs)
    assert diffs == []  # the empty route was warned about, not reported again
    assert any("comparison leaves out the 1 Route to Interaction Plan applets copied without their settings" in m
               and m.endswith(": R gone") for m in logs)


def test_dry_run_warns_without_writing(tmp_path):
    write_doc(tmp_path, route("R gone", TargetPlan="Gone"))
    portal, logs = FakePortal(), []
    import_plan(portal, tmp_path, "p-scratch", "Scratch", dry_run=True, log=logs.append)
    assert portal.writes == [] and any("'R gone' would be copied without its settings" in m for m in logs)


# --- comparing ---

def doc_with(applet):
    return {"plan": {}, "applets": [applet, {"name": "DR Decimal", "kind": "Data Router", "settings": {}, "next": {}}]}


def test_comparison_by_plan_and_applet_name_both_ways():
    a = {**route("R"), "kind": "Route To Interaction Plan"}
    b = {**a, "settings": {**a["settings"], "TargetApplet": "LOG Test"}, "next": {}}
    assert compare_docs(doc_with(a), doc_with(b))["differences"] == [
        "'R' path FailureApplet is missing in the target (source goes to 'DR Decimal')",
        "'R' setting TargetApplet: source ANN Audio | target LOG Test"]
    assert compare_docs(doc_with(b), doc_with(a))["differences"] == [
        "'R' path FailureApplet is missing in the source (target goes to 'DR Decimal')",
        "'R' setting TargetApplet: source LOG Test | target ANN Audio"]
    c = {**a, "settings": {**TO_DATA_SOURCE}}
    assert "'R' setting RouteType: source Interaction plan | target Data source" in \
        compare_docs(doc_with(a), doc_with(c))["differences"]
    assert compare_docs(doc_with(a), doc_with(json.loads(json.dumps(a))))["differences"] == []
