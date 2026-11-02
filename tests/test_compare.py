import copy

from vccflow.compare import broken_connection_ids, compare_docs, format_report
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
