"""Data Router: page parsing, the save form, export/import and comparison, from dataRouter.har."""

import html
import json
from unittest.mock import MagicMock
from urllib.parse import parse_qsl

import pytest

from vccflow.compare import compare_docs
from vccflow.config import Account
from vccflow.export import data_sources_needed, data_sources_used, split_data_router
from vccflow.importer import import_plan, unsupported_reason
from vccflow.portal import PortalClient, PortalError, data_router_form, data_router_missing_targets, extract_data_router

PLAN_ID = "b918444e-7c29-41b0-9a65-9a64b8568257"


def rule(source, value, data_type, operator, route):
    return {"LeftHandDataSource": source, "RightHandFixedValue": value, "RightHandFixedBooleanValue": None,
            "DataType": data_type, "OperatorType": operator, "RouteToApplet": route}


# The six rules and default applet saved in dataRouter.har, as the page model returns them.
RULES = [
    rule("CallTopic", "My Reason", 0, "equals", "DC Get Salesforce"),
    rule("CallTopic", "$(CallReason)", 0, "equals", "DC Get Salesforce"),
    rule("CallTopic", "My $(CallReason) My $(CallTopic) $(accessToken)", 0, "not equals", "DC Get Salesforce"),
    rule("VoiceCall|Id", "abc", 0, "does not contain", "ANN Audio"),
    rule("VoiceCall_ID", "2", 1, "is greater than or equal to", "ANN Audio"),
    rule("VoiceCall_ID", "$(callcenterid)", 4, "is or is after", "ANN Audio"),
]
DEFAULT = "LOG Test Claude Failed"
# The recorded post body, exactly as the browser sent it.
RECORDED_POST = (
    "%5Brule0%5D.LeftHandDataSource=CallTopic&index=rule0&%5Brule0%5D.DataType=0&%5Brule0%5D.OperatorType=equals"
    "&%5Brule0%5D.RightHandFixedValue=My+Reason&%5Brule0%5D.RouteToApplet=DC+Get+Salesforce"
    "&%5Brule1%5D.LeftHandDataSource=CallTopic&index=rule1&%5Brule1%5D.DataType=0&%5Brule1%5D.OperatorType=equals"
    "&%5Brule1%5D.RightHandFixedValue=%24%28CallReason%29&%5Brule1%5D.RouteToApplet=DC+Get+Salesforce"
    "&%5Brule2%5D.LeftHandDataSource=CallTopic&index=rule2&%5Brule2%5D.DataType=0&%5Brule2%5D.OperatorType=not+equals"
    "&%5Brule2%5D.RightHandFixedValue=My+%24%28CallReason%29+My+%24%28CallTopic%29+%24%28accessToken%29"
    "&%5Brule2%5D.RouteToApplet=DC+Get+Salesforce"
    "&%5Brule3%5D.LeftHandDataSource=VoiceCall%7CId&index=rule3&%5Brule3%5D.DataType=0"
    "&%5Brule3%5D.OperatorType=does+not+contain&%5Brule3%5D.RightHandFixedValue=abc&%5Brule3%5D.RouteToApplet=ANN+Audio"
    "&%5Brule4%5D.LeftHandDataSource=VoiceCall_ID&index=rule4&%5Brule4%5D.DataType=1"
    "&%5Brule4%5D.OperatorType=is+greater+than+or+equal+to&%5Brule4%5D.RightHandFixedValue=2"
    "&%5Brule4%5D.RouteToApplet=ANN+Audio"
    "&%5Brule5%5D.LeftHandDataSource=VoiceCall_ID&index=rule5&%5Brule5%5D.DataType=4"
    "&%5Brule5%5D.OperatorType=is+or+is+after&%5Brule5%5D.RightHandFixedValue=%24%28callcenterid%29"
    "&%5Brule5%5D.RouteToApplet=ANN+Audio&defaultApplet=LOG+Test+Claude+Failed"
)


def page(rules=RULES, default=DEFAULT):
    """A Data Router page as the portal renders it: a data source list, then the page model."""
    lookup = {"CallTopic": "CallTopic", "VoiceCall|Id": "VoiceCall|Voice Call ID"}
    model = {"QueryKeys": [{"KeyName": "CallTopic", "DataType": 0}], "DefaultAppletInstanceName": default,
             "UnsavedChanges": False, "RoutingRules": rules,
             "DataTypeOperators": {"0": [{"Key": "Equals", "Value": "equals"}], "4": [{"Key": "Is", "Value": "is"}]},
             "PossibleNextApplets": [], "NextApplet": None, "FormattedNextApplets": []}
    enc = lambda m: html.escape(json.dumps(m), quote=True)  # noqa: E731
    return (f"<script>Nvm.InitDataRouterCodeMirror(JSON.parse(htmlDecode('{enc(lookup)}')));"
            f"Nvm.DataRouter.load(JSON.parse(htmlDecode('{enc(model)}')));</script>")


def exported():
    settings, nxt = split_data_router(RULES, DEFAULT)
    return {"name": "DR Sample", "kind": "Data Router", "type": "DataRouter", "settings": settings, "next": nxt}


def test_extract_data_router_reads_rules_default_and_operators():
    dr = extract_data_router(page())
    assert dr["rules"] == RULES and dr["default"] == DEFAULT
    assert dr["operators"] == {0: ["equals"], 4: ["is"]}
    with pytest.raises(PortalError, match="RoutingRules"):
        extract_data_router("<html>no model</html>")


def test_export_keeps_rules_in_order_and_connections_by_name():
    a = exported()
    assert [r["OperatorType"] for r in a["settings"]["RoutingRules"]] == [r["OperatorType"] for r in RULES]
    assert all("RouteToApplet" not in r for r in a["settings"]["RoutingRules"])
    assert a["next"] == {**{f"RoutingRules.{i}": r["RouteToApplet"] for i, r in enumerate(RULES)}, "DefaultApplet": DEFAULT}


def test_form_reproduces_the_recorded_post():
    a = exported()
    assert data_router_form(a["settings"], a["next"]) == parse_qsl(RECORDED_POST, keep_blank_values=True)


@pytest.mark.parametrize("change, match", [
    (lambda a: a["settings"]["RoutingRules"][1].update(DataType=5), "data type 5 has not been recorded"),
])
def test_unrecorded_cases_are_refused(change, match):
    a = exported()
    change(a)
    with pytest.raises(PortalError, match=match):
        data_router_form(a["settings"], a["next"])
    assert unsupported_reason(a)  # so import-plan refuses it before any write


def test_true_false_and_null_rules_reproduce_the_recorded_post():
    # dataRouter4.har: "DR Test" as the page model returns it after the save, and the post that saved it.
    rules = [
        rule("AlwaysOpen", None, 3, "equals", "ANN Audio") | {"RightHandFixedBooleanValue": "False"},
        rule("AlwaysOpen", None, 3, "equals", "ANN Audio") | {"RightHandFixedBooleanValue": "True"},
        rule("AlwaysOpen", "", 0, "is null", "ANN Audio"),
        rule("AlwaysOpen", "xxxxxxxxxx", 0, "is not null", "ANN Audio"),  # an old value the page keeps
    ]
    settings, nxt = split_data_router(rules, "ANN New Audio")
    recorded = []
    for i, r in enumerate(rules):
        recorded += [(f"[rule{i}].LeftHandDataSource", "AlwaysOpen"), ("index", f"rule{i}"),
                     (f"[rule{i}].DataType", str(r["DataType"])), (f"[rule{i}].OperatorType", r["OperatorType"]),
                     (f"[rule{i}].RightHandFixedValue", r["RightHandFixedValue"] or "")]
        if r["DataType"] == 3:
            recorded.append((f"[rule{i}].RightHandFixedBooleanValue", r["RightHandFixedBooleanValue"]))
        recorded.append((f"[rule{i}].RouteToApplet", "ANN Audio"))
    recorded.append(("defaultApplet", "ANN New Audio"))
    assert data_router_form(settings, nxt) == recorded
    assert ("[rule1].RightHandFixedBooleanValue", "True") in recorded and ("[rule3].RightHandFixedValue", "xxxxxxxxxx") in recorded


def test_decimal_rules_reproduce_the_recorded_post():
    # dataRouter5.har: "DR Type2", three decimal rules and no default applet.
    ops = ["equals", "is less than", "is greater than"]
    settings, nxt = split_data_router([rule("CallGuid", "1", 2, op, "ANN Audio") for op in ops], None)
    recorded = []
    for i, op in enumerate(ops):
        recorded += [(f"[rule{i}].LeftHandDataSource", "CallGuid"), ("index", f"rule{i}"), (f"[rule{i}].DataType", "2"),
                     (f"[rule{i}].OperatorType", op), (f"[rule{i}].RightHandFixedValue", "1"),
                     (f"[rule{i}].RouteToApplet", "ANN Audio")]
    assert data_router_form(settings, nxt) == recorded + [("defaultApplet", "")]


def test_missing_targets_are_posted_empty_and_listed():
    a = exported()
    del a["next"]["RoutingRules.2"], a["next"]["DefaultApplet"]
    form = data_router_form(a["settings"], a["next"])
    assert ("[rule2].RouteToApplet", "") in form and ("defaultApplet", "") in form
    assert ("[rule2].OperatorType", "not equals") in form  # the rule itself is kept
    assert data_router_missing_targets(a["settings"], a["next"]) == ["rule 3 (RoutingRules.2)", "default applet"]
    assert unsupported_reason(a) is None
    assert data_router_missing_targets(exported()["settings"], exported()["next"]) == []


def test_data_sources_read_by_rules():
    a = exported()
    assert data_sources_used(a) == {"CallTopic", "VoiceCall|Id", "VoiceCall_ID", "CallReason", "accessToken", "callcenterid"}
    assert "VoiceCall_ID" in data_sources_needed([a])


def test_save_posts_the_form_without_token_and_reads_back():
    session = MagicMock()
    session.get.return_value = MagicMock(status_code=200, text="var user = { account: 'NVMDemo29' };")
    saved = f"/CallCentre/apps/DataRouter?phoneNum={PLAN_ID}&myApp=DR%20Sample&appletSaved=True"
    session.request.side_effect = [
        MagicMock(status_code=302, is_redirect=True, headers={"Location": saved}),
        MagicMock(status_code=200, is_redirect=False, headers={}, text=page()),
    ]
    account = Account(name="Demo29", region="emea", allow_portal_writes=True, portal_account="NVMDemo29")
    a = exported()
    result = PortalClient(account, session=session).save_data_router(PLAN_ID, "DR Sample", a["settings"], a["next"])
    post, get = session.request.call_args_list
    assert post.args[0] == "POST" and post.args[1].endswith(f"DataRouter?phoneNum={PLAN_ID}&myApp=DR%20Sample")
    assert post.kwargs["data"] == parse_qsl(RECORDED_POST, keep_blank_values=True)
    assert not any(k == "__RequestVerificationToken" for k, _ in post.kwargs["data"])
    assert get.args[1].endswith("&appletSaved=True") and result["rules"] == RULES


def test_save_without_confirmation_is_an_error():
    session = MagicMock()
    session.get.return_value = MagicMock(status_code=200, text="var user = { account: 'NVMDemo29' };")
    session.request.return_value = MagicMock(status_code=200, is_redirect=False, headers={}, text=(
        '<div class="validation-summary-errors" data-valmsg-summary="true"><ul>'
        '<li>Rule 1: Please select a Data Source from the list</li> </ul></div>'))
    account = Account(name="Demo29", region="emea", allow_portal_writes=True, portal_account="NVMDemo29")
    a = exported()
    with pytest.raises(PortalError, match="'DR Sample' was refused: Rule 1: Please select a Data Source from the list"):
        PortalClient(account, session=session).save_data_router(PLAN_ID, "DR Sample", a["settings"], a["next"])


class FakePortal:
    account = Account(name="Demo29", region="emea", allow_portal_writes=True)

    def __init__(self):
        self.applets, self.writes = {}, []

    def list_applets(self, plan_id):
        return {"name": "Scratch", "applets": [{"id": i, "name": n, "guid": f"g-{n}", "appletType": t, "configUrl": "x"}
                                               for i, (n, t) in enumerate(self.applets.items())]}

    def list_data_sources(self, plan_id):
        return None

    def create_applet(self, plan_id, applet_type, name):
        self.writes.append(("create", name, applet_type))
        self.applets[name] = applet_type

    def get_applet_state(self, plan_id, applet):
        return {"appletProperties": {"AppletType": self.applets[applet["name"]]}}

    def save_applet(self, plan_id, name, props):
        self.writes.append(("save", name))

    def save_data_router(self, plan_id, name, settings, nxt):
        self.writes.append(("save-dr", name, settings, nxt))

    def set_initial_applet(self, plan_id, name):
        self.writes.append(("initial", name))


def plan_with_router():
    targets = [{"name": n, "type": "SetDataSource", "settings": {"DataSources": []}, "next": {}}
               for n in ("DC Get Salesforce", "ANN Audio", DEFAULT)]
    return {"format": "vccflow.plan/1", "plan": {"name": "Source", "initialApplet": "DR Sample"},
            "applets": [exported(), *targets]}


def test_import_creates_and_saves_the_router_with_names(tmp_path, monkeypatch):
    doc = plan_with_router()
    (tmp_path / "plan.json").write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setattr("vccflow.importer.export.export_plan", lambda *a, **k: plan_with_router())
    portal = FakePortal()
    assert import_plan(portal, tmp_path, PLAN_ID, "Scratch", log=lambda _: None) == []
    assert ("create", "DR Sample", "DataRouter") in portal.writes
    saved = next(w for w in portal.writes if w[0] == "save-dr")
    assert saved[2] == exported()["settings"] and saved[3] == exported()["next"]
    assert portal.writes.index(saved) > max(i for i, w in enumerate(portal.writes) if w[0] == "create")


def test_import_copies_a_router_whose_target_was_skipped_and_warns(tmp_path, monkeypatch):
    doc = plan_with_router()
    doc["applets"][2] = {"name": "ANN Audio", "kind": "ACD", "unsupported": "not recorded"}  # rules 4-6 route to it
    (tmp_path / "plan.json").write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setattr("vccflow.importer.export.export_plan", lambda *a, **k: {**doc, "applets": []})
    portal, logs = FakePortal(), []
    import_plan(portal, tmp_path, PLAN_ID, "Scratch", skip_unsupported=True, log=logs.append)
    saved = next(w for w in portal.writes if w[0] == "save-dr")
    assert len(saved[2]["RoutingRules"]) == 6 and "RoutingRules.3" not in saved[3]
    text = "\n".join(logs)
    assert ("WARNING: Data Router 'DR Sample' is copied with no destination applet for: "
            "rule 4 (RoutingRules.3), rule 5 (RoutingRules.4), rule 6 (RoutingRules.5)") in text
    assert "'DR Sample' has been copied with missing targets" in text


def test_import_warns_about_targets_already_missing_in_the_source(tmp_path, monkeypatch):
    # dataRouter2.har: one rule with no destination and no default applet, saved by the portal.
    settings, nxt = split_data_router([rule("AlwaysOpen", "1", 1, "equals", "")], None)
    router = {"name": "DR Capture Empty", "kind": "Data Router", "type": "DataRouter", "settings": settings, "next": nxt}
    assert nxt == {}
    assert data_router_form(settings, nxt) == [
        ("[rule0].LeftHandDataSource", "AlwaysOpen"), ("index", "rule0"), ("[rule0].DataType", "1"),
        ("[rule0].OperatorType", "equals"), ("[rule0].RightHandFixedValue", "1"), ("[rule0].RouteToApplet", ""),
        ("defaultApplet", "")]
    doc = {"format": "vccflow.plan/1", "plan": {"name": "Source", "initialApplet": None}, "applets": [router]}
    (tmp_path / "plan.json").write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setattr("vccflow.importer.export.export_plan", lambda *a, **k: doc)
    for dry_run, verb in ((True, "would be"), (False, "is")):
        logs = []
        import_plan(FakePortal(), tmp_path, PLAN_ID, "Scratch", dry_run=dry_run, log=logs.append)
        assert (f"WARNING: Data Router 'DR Capture Empty' {verb} copied with no destination applet for: "
                "rule 1 (RoutingRules.0), default applet") in "\n".join(logs)


def test_empty_router_is_created_but_not_saved(tmp_path, monkeypatch):
    with pytest.raises(PortalError, match="without rules cannot be saved"):
        data_router_form({"RoutingRules": []}, {})
    empty = {"name": "DR Empty", "kind": "Data Router", "type": "DataRouter", "settings": {"RoutingRules": []},
             "next": {"DefaultApplet": "LOG B"}}
    assert unsupported_reason(empty) is None
    target = {"name": "LOG B", "type": "SetDataSource", "settings": {"DataSources": []}, "next": {}}
    doc = {"format": "vccflow.plan/1", "plan": {"name": "Source", "initialApplet": None}, "applets": [empty, target]}
    (tmp_path / "plan.json").write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setattr("vccflow.importer.export.export_plan", lambda *a, **k: doc)
    portal, logs = FakePortal(), []
    import_plan(portal, tmp_path, PLAN_ID, "Scratch", log=logs.append)
    assert ("create", "DR Empty", "DataRouter") in portal.writes
    assert not any(w[0] == "save-dr" for w in portal.writes)
    text = "\n".join(logs)
    assert "WARNING: Data Router 'DR Empty' has no rules: it is created empty and not saved" in text
    assert "Its default applet 'LOG B' is not set." in text and "no destination applet" not in text


def test_import_refuses_an_unrecorded_rule_before_any_write(tmp_path):
    doc = plan_with_router()
    doc["applets"][0]["settings"]["RoutingRules"][0]["DataType"] = 5
    (tmp_path / "plan.json").write_text(json.dumps(doc), encoding="utf-8")
    portal = FakePortal()
    with pytest.raises(PortalError, match="data type 5"):
        import_plan(portal, tmp_path, PLAN_ID, "Scratch", log=lambda _: None)
    assert portal.writes == []


def doc_with(router, account="A"):
    return {"source": {"account": account}, "plan": {"initialApplet": "DR Sample"},
            "applets": [router, *plan_with_router()["applets"][1:]]}


def test_compare_reports_changed_rules_and_paths_in_both_directions():
    a, b = exported(), exported()
    b["settings"]["RoutingRules"][2]["OperatorType"] = "equals"
    b["next"]["RoutingRules.1"] = "ANN Audio"
    del b["next"]["RoutingRules.5"]
    report = compare_docs(doc_with(a), doc_with(b))
    text = "\n".join(report["differences"])
    assert "'DR Sample' setting RoutingRules[2].OperatorType: source not equals | target equals" in text
    assert "'DR Sample' path RoutingRules.1 is different: source 'DC Get Salesforce' | target 'ANN Audio'" in text
    assert "'DR Sample' path RoutingRules.5 is missing in the target" in text
    back = "\n".join(compare_docs(doc_with(b), doc_with(a))["differences"])
    assert "RoutingRules[2].OperatorType: source equals | target not equals" in back
    assert "path RoutingRules.5 is missing in the source" in back
    assert not any("editor page fields" in n for n in report["notes"])


def test_compare_same_router_in_two_accounts_reports_nothing():
    report = compare_docs(doc_with(exported(), "A"), doc_with(exported(), "B"))
    assert report["differences"] == [] and report["problems"] == []


def test_compare_reports_an_added_rule():
    b = exported()
    b["settings"]["RoutingRules"].append({**b["settings"]["RoutingRules"][0], "RightHandFixedValue": "Other"})
    b["next"]["RoutingRules.6"] = "ANN Audio"
    text = "\n".join(compare_docs(doc_with(exported()), doc_with(b))["differences"])
    # A rule refers to $(accessToken), so the changed list's values are hidden as possible credentials.
    assert "setting RoutingRules differs (values hidden" in text and "path RoutingRules.6 is missing in the source" in text
