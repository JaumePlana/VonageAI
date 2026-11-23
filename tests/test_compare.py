import copy
import json

from vccflow.compare import compare_docs, compare_exports, format_report
from vccflow.export import broken_connection_ids
from vccflow.portal import extract_form_values


def plan(**over):
    doc = {
        "plan": {"initialApplet": "ANN A", "reportingGroupName": "Sales"},
        "applets": [
            {"name": "ANN A", "kind": "Announcer", "type": "DataAnnouncer",
             "settings": {"Text": "Hello", "Timeout": 10, "Voice": None}, "next": {"NextAppletSuccess": "ST B"},
             "media": [{"property": "AnnouncementMedia", "name": "Ringing", "sha256": "aaa"}]},
            {"name": "ST B", "kind": "Skill Tagger", "unsupported": "no initialState",
             "form": {"Skills": [{"Name": "FR", "Necessity": 2}], "NextApplet": "DC C"}},
            {"name": "DC C", "kind": "Data Connector", "type": "DataRetriever",
             "settings": {"ApiInteractionDefinition": "<Value>s3cret</Value>"}, "next": {}},
        ],
    }
    doc.update(over)
    return doc


def by_name(doc, name):
    return next(a for a in doc["applets"] if a["name"] == name)


def test_identical_plans_report_nothing():
    report = compare_docs(plan(), plan())
    assert report["differences"] == [] and report["problems"] == []


def test_auxiliary_data_source_applet_is_a_note():
    b = plan()
    b["applets"].append({"name": "zz vccflow Aux Data Sources", "kind": "Set Data Source", "type": "SetDataSource",
                         "settings": {"DataSources": [{"Key": "AlwaysOpen", "Value": ""}]}, "next": {}})
    report = compare_docs(plan(), b)
    assert report["differences"] == []
    assert any("'zz vccflow Aux Data Sources' is only in the target plan" in n and "AlwaysOpen" in n for n in report["notes"])


def test_applets_missing_on_either_side():
    a, b = plan(), plan()
    a["applets"].append({"name": "Only source", "kind": "Menu"})
    b["applets"].append({"name": "Only target", "kind": "ACD"})
    text = " ".join(compare_docs(a, b)["differences"])
    assert "'Only source' (Menu) is missing in the target plan" in text
    assert "'Only target' (ACD) is only in the target plan" in text


def test_paths_missing_or_different_in_both_directions():
    a, b = plan(), plan()
    by_name(a, "ANN A")["next"]["NextAppletFail"] = "DC C"
    by_name(b, "DC C")["next"]["NextApplet"] = "ANN A"
    by_name(b, "ANN A")["next"]["NextAppletSuccess"] = "DC C"
    text = " ".join(compare_docs(a, b)["differences"])
    assert "'ANN A' path NextAppletFail is missing in the target" in text
    assert "'DC C' path NextApplet is missing in the source" in text
    assert "'ANN A' path NextAppletSuccess is different: source 'ST B' | target 'DC C'" in text


def test_skills_differ_or_go_missing():
    a, b = plan(), plan()
    by_name(b, "ST B")["form"]["Skills"] = [{"Name": "EN", "Necessity": 2}]
    text = " ".join(compare_docs(a, b)["differences"])
    assert "'ST B' field Skills" in text and "FR" in text and "EN" in text
    by_name(b, "ST B")["form"]["Skills"] = []
    assert any("(not set)" in d for d in compare_docs(a, b)["differences"])


def test_same_values_in_another_form_are_not_differences():
    a, b = plan(), plan()
    by_name(b, "ANN A")["settings"].update({"Timeout": "10", "Voice": ""})
    assert compare_docs(a, b)["differences"] == []


def test_letter_case_is_a_note_and_credentials_stay_hidden():
    a, b = plan(), plan()
    by_name(b, "ANN A")["settings"]["Text"] = "HELLO"
    by_name(b, "DC C")["settings"]["ApiInteractionDefinition"] = "<Value>0ther</Value>"
    report = compare_docs(a, b)
    assert any("letter case" in n for n in report["notes"])
    assert any("values hidden" in d for d in report["differences"])
    assert "s3cret" not in format_report(report) and "0ther" not in format_report(report)


def test_audio_initial_applet_and_reporting_group():
    a, b = plan(), plan()
    by_name(b, "ANN A")["media"][0]["sha256"] = "bbb"
    b["plan"] = {"initialApplet": "DC C", "reportingGroupName": None}
    text = " ".join(compare_docs(a, b)["differences"])
    assert "audio 'Ringing' has different content" in text
    assert "initial applet: source ANN A | target DC C" in text and "reporting group" in text
    c = copy.deepcopy(plan())
    by_name(c, "ANN A")["media"] = []
    assert any("audio AnnouncementMedia is missing in the target" in d for d in compare_docs(plan(), c)["differences"])


def test_paths_to_applets_outside_the_plan_are_problems():
    b = plan()
    by_name(b, "ANN A")["next"]["NextAppletFail"] = "Gone"
    assert any(p.startswith("target:") and "'Gone'" in p for p in compare_docs(plan(), b)["problems"])


def test_broken_connection_ids():
    applets = [{"id": 1, "guid": "g1", "name": "A"}, {"id": 2, "guid": "g2", "name": "B"}]
    good = {"AppletGuid": "other", "NextAppletSuccess": "A", "NextAppletSuccessGuid": "g1",
            "TimeoutAppletName": "B", "TimeoutAppletGuid": "g2", "FailureAppletId": "1"}
    assert broken_connection_ids("X", good, applets) == []
    bad = {"NextAppletSuccess": "A", "NextAppletSuccessGuid": "g2", "NextAppletFailGuid": "elsewhere", "FailureAppletId": 9}
    problems = " ".join(broken_connection_ids("X", bad, applets))
    assert "says 'A' but its GUID is 'B'" in problems and "not in this plan" in problems and "FailureAppletId 9" in problems


def test_form_values_include_the_page_model_without_lookups():
    html = """<form><input name="__RequestVerificationToken" value="t"><input name="dialnbr" value="1">
    <input type="checkbox" name="On" value="x" checked><input type="checkbox" name="Off" value="y">
    <select name="Q"><option value="1">a</option><option value="2" selected>b</option></select>
    <textarea name="T"> hi </textarea></form>
    <script>m.loadCrmModel(JSON.parse(htmlDecode('{&quot;Skills&quot;:[{&quot;Name&quot;:&quot;FR&quot;}],&quot;AllSkills&quot;:[1],&quot;PossibleNextApplets&quot;:[2],&quot;NextApplet&quot;:&quot;B&quot;}')));</script>"""
    assert extract_form_values(html) == {"On": "x", "Q": "2", "T": "hi", "Skills": [{"Name": "FR"}], "NextApplet": "B"}


def save(folder, doc):
    folder.mkdir()
    (folder / "plan.json").write_text(json.dumps({"format": "vccflow.plan/1", **doc}), encoding="utf-8")


def test_compare_exports_finds_differences_including_skills(tmp_path):
    a, b = plan(), plan()
    by_name(b, "ST B")["form"]["Skills"] = [{"Name": "EN"}]
    save(tmp_path / "a", a)
    save(tmp_path / "b", b)
    report = compare_exports(tmp_path / "a", tmp_path / "b")
    assert any("'ST B' field Skills" in d for d in report["differences"])
    assert any("data sources in the target account not checked" in n for n in report["notes"])


def test_older_exports_without_page_fields_or_hashes_are_noted_not_flagged(tmp_path):
    a, b = plan(), plan()
    for doc in (a, b):
        del by_name(doc, "ST B")["form"]
        del by_name(doc, "ANN A")["media"][0]["sha256"]
        by_name(doc, "ANN A")["media"][0]["file"] = "media/x.wav"
    save(tmp_path / "a", a)
    save(tmp_path / "b", b)
    (tmp_path / "a" / "media").mkdir()
    (tmp_path / "a" / "media" / "x.wav").write_bytes(b"one")
    (tmp_path / "b" / "media").mkdir()
    (tmp_path / "b" / "media" / "x.wav").write_bytes(b"two")
    report = compare_exports(tmp_path / "a", tmp_path / "b")
    assert any("settings not compared" in n for n in report["notes"])
    # Hashes come from the files, so the audio difference is still found.
    assert any("different content" in d for d in report["differences"])


def with_clid(account, behaviour, number=None):
    doc = plan()
    doc["source"] = {"account": account}
    doc["plan"].update({"outboundClidBehaviour": behaviour, "outboundClid": number})
    return doc


def test_outbound_clid_setting_difference_is_reported():
    report = compare_docs(with_clid("A", "DefaultAccountClid"), with_clid("B", "CustomerClid"))
    assert "outbound CLID setting: source DefaultAccountClid | target CustomerClid" in report["differences"]


def test_outbound_clid_same_is_silent():
    report = compare_docs(with_clid("A", "InteractionPlanClid", "1"), with_clid("B", "InteractionPlanClid", "1"))
    assert not [x for x in report["differences"] + report["notes"] if "CLID" in x]


def test_outbound_clid_number_is_a_note_across_accounts_and_a_difference_within_one():
    across = compare_docs(with_clid("A", "InteractionPlanClid", "1"), with_clid("B", "InteractionPlanClid", "2"))
    assert not across["differences"] and any("number: source 1 | target 2" in n for n in across["notes"])
    within = compare_docs(with_clid("A", "InteractionPlanClid", "1"), with_clid("A", "InteractionPlanClid", "2"))
    assert any("number: source 1 | target 2" in d for d in within["differences"])


def test_exports_without_outbound_clid_are_noted():
    report = compare_docs(plan(), with_clid("B", "CustomerClid"))
    assert report["differences"] == [] and any("outbound CLID not compared" in n for n in report["notes"])


def test_report_groups_plan_level_first_then_one_block_per_applet():
    a, b = plan(), plan()
    b["plan"]["reportingGroupName"] = "Service"
    b["applets"].append({"name": "Only target", "kind": "ACD"})
    by_name(b, "ANN A")["settings"]["Text"] = "Hi"
    by_name(b, "ANN A")["next"]["NextAppletSuccess"] = "DC C"
    by_name(b, "ST B")["form"]["Skills"] = [{"Name": "DE", "Necessity": 2}]
    lines = format_report(compare_docs(a, b)).splitlines()
    assert lines[0] == ("SUMMARY: 5 differences (1 plan-level, 1 applets in one plan only, 3 in 2 applets), "
                        "0 problems, 2 notes")
    body = lines[lines.index("DIFFERENCES (5)") + 1:lines.index("PROBLEMS (0)")]
    assert body[0] == "  Plan" and body[1].startswith("    reporting group: source Sales")
    assert body[2] == "  Applets in one plan only"
    assert body[4] == "  'ANN A' (Announcer): 2"
    # Inside an applet block the applet name is not repeated.
    assert body[5] == "    path NextAppletSuccess is different: source 'ST B' | target 'DC C'"
    assert body[6] == "    setting Text: source Hello | target Hi"
    assert body[7] == "  'ST B' (Skill Tagger): 1" and body[8] == "    field Skills[0].Name: source FR | target DE"


def test_page_field_notes_are_listed_together_by_applet_type():
    a = plan()
    a["applets"].append({"name": "ST Z", "kind": "Skill Tagger", "unsupported": "x", "form": {}})
    text = format_report(compare_docs(a, copy.deepcopy(a)))
    assert "  Compared through their editor page fields only (2 applets)\n    Skill Tagger: 'ST B', 'ST Z'" in text
    assert "compared through its editor page fields only" not in text


def test_report_findings_are_still_plain_lines():
    b = plan()
    by_name(b, "ANN A")["settings"]["Text"] = "Hi"
    assert compare_docs(plan(), b)["differences"] == ["'ANN A' setting Text: source Hello | target Hi"]


def test_form_values_keep_account_specific_options_by_their_text():
    html = """<form><select name="datagrid$ctl02$ddDialledNbr"><option value=""></option>
    <option selected="selected" value="842eb571-b2d6-4819-b7b5-f4f22c5d799f">1012 - Shipment   Validation</option>
    <option value="Partner_JPlana-5">1020 - Welcome</option></select>
    <select name="Acd.PostUnexpectedPresenceStateGuid"><option value="">Leave unchanged</option>
    <option value="83869ea1-e93a-11ef-9b45-0267ee5b37ba" selected>Away
    <option value="8546640c-e93a-11ef-9b45-0267ee5b37ba">Comfort Break</select>
    <select name="Id"><option value="1234567" selected>Queue A</option></select>
    <select name="Mode"><option value="Sequential" selected>Sequential Routing</option></select></form>"""
    assert extract_form_values(html) == {"datagrid$ctl02$ddDialledNbr": "1012 - Shipment Validation",
                                         "Acd.PostUnexpectedPresenceStateGuid": "Away", "Id": "Queue A",
                                         "Mode": "Sequential"}
