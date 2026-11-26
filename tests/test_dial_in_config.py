"""Dial In Config (dialInConfig.har): ten configurations, each saved alone through updated.asp, and the chosen one."""

import json
from unittest.mock import MagicMock

import pytest

from vccflow.compare import compare_docs
from vccflow.config import Account
from vccflow.export import dial_in_config_empty_selection, export_applet
from vccflow.importer import import_plan, unsupported_reason
from vccflow.portal import PortalClient, PortalError, dial_in_config_settings, extract_dial_in_config

PLAN_ID = "b918444e-7c29-41b0-9a65-9a64b8568257"
ACCOUNT = Account(name="Demo29", region="emea", allow_portal_writes=True, portal_account="NVMDemo29")
APPLETS = ["ANN TTS", "DR Empty2", "DR Only Default"]
RECORDING = "recordings/56303/b918444e-7c29-41b0-9a65-9a64b8568257/4bcda4b3-d5b6-4d15-9925-7b85a1765068NVMDemo29.wav"
HIDDEN = ('<input type="hidden" name="validation" value="true" />\n<input type="hidden" name="name" value="dialinconfig"/>\n'
          '<input type="hidden" name="optype" value="{optype}"/>\n'
          f'<input type="hidden" name="phonenum" value="{PLAN_ID}" />\n<input type="hidden" name="myapp" value="DIC Test" />\n'
          '<input type="hidden" name="application" value="DialInConfig" />\n')


def config_form(i, applet="", alt="", audio=None):
    """One configuration's form, as dialinconfig.asp writes it: the current applet first, not marked selected."""
    others = [a for a in APPLETS if a != applet]
    options = ([f'<option value="{applet}">{applet} (Data Router)</option>'] if applet else []) + ['<option value=""></option>']
    options += [f'<option value="{a}">{a} (Data Router)</option>' for a in others]
    play = (f'<td class="subSection" height="30" align="right"><a href="../{audio}">Play</a></td>' if audio
            else '<td class="subSection" height="30" colspan="2">&nbsp;</td>')
    return ('<form action="updated.asp" enctype="multipart/form-data" method="post" id="form2" name="form1">\n'
            + HIDDEN.format(optype="announcementconfig")
            + f'<input type="hidden" name="TargetURL" value="../uploads/" size="20"/>\n<input type="hidden" name="index" value="{i}"/>\n'
            '<input type="hidden" name="announcement"/>\n'
            f'<tr>\n<td class="subSection" width="10" height="30">&nbsp;{i}&nbsp</td>\n<td class="subSection" height="30">\n'
            f'<select name="nextapplet{i}" onchange="notifyChange()" size="1" style="width: 150;">\n' + "\n".join(options)
            + '\n</select>\n</td>\n<td class="subSection" height="30" name="mytext">\n'
            f'<input onchange="notifyChange()" type="text" size="20" name="alttext" value="{alt}" maxlength="255"/>\n</td>\n'
            f'<td class="subSection" height="30">\n<input class="selectfile" name="myfile" id="fileupload{i}" type="file" value="">\n'
            f'</td>\n<td class="subSection" height="30">\n<input type="submit" id="update{i}" name="B2" value="Update" />\n</td>\n'
            + play + '\n</tr>\n</form>\n')


def page(configs=None, selected=0):
    """The editor page; configs: {i: (applet, alt text, audio path)}."""
    configs = configs or {}
    forms = "".join(config_form(i, *configs.get(i, ())) for i in range(10))
    choices = "".join(f'<option value="Selected{i}"{" selected=\"selected\"" if i == selected else ""}>{i}</option>'
                      for i in range(10))
    return ("<html><body><h1>DIC Test (Dial In Config), POC Claude</h1><table>" + forms
            + '<form action="updated.asp" method="post" id="form1" name="form1">\n' + HIDDEN.format(optype="configNumber")
            + f'<tr><td>Select your configuration</td><td><select name="option">{choices}</select></td>'
            '<td><input type="submit" name="B3" value="Update" /></td></tr>\n</form></table></body></html>')


# The last recorded page (#19) and the one after choosing configuration 6 (#17).
LAST = {0: ("DR Only Default", "Normal", RECORDING), 1: ("", "Test"), 9: ("DR Empty2", "Help")}
SAVED = "<html><body><td id=\"statusMessage\" class=\"subSection\">\nYour details have been successfully updated\n</td></body></html>"
# The choice answers otherwise (#16, #18).
CHOSEN = "<html><body><td id=\"statusMessage\" class=\"subSection\">\nYour applet number {}  have been selected\n</td></body></html>"


# --- reading ---

def test_the_page_gives_each_configuration_and_the_chosen_one():
    p = extract_dial_in_config(page(LAST, selected=6))
    assert p["selected"] == 6 and p["applets"] == set(APPLETS)
    assert p["configs"][0] == {"NextApplet": "DR Only Default", "AltText": "Normal", "Audio": RECORDING}
    assert p["configs"][1] == {"NextApplet": "", "AltText": "Test", "Audio": None}
    assert p["configs"][5] == {"NextApplet": "", "AltText": "", "Audio": None}
    assert dial_in_config_settings(p) == (
        {"SelectedConfiguration": 6, "AltText.0": "Normal", "AltText.1": "Test", "AltText.9": "Help"},
        {"NextApplet.0": "DR Only Default", "NextApplet.9": "DR Empty2"})


def test_another_page_is_refused():
    with pytest.raises(PortalError, match="Not a Dial In Config page"):
        extract_dial_in_config("<html><form action=\"updated.asp\"></form></html>")


def test_export_downloads_the_announcements():
    portal = MagicMock()
    portal.get_dial_in_config.return_value = extract_dial_in_config(page(LAST))
    portal.download_recording.return_value = b"RIFF audio"
    applet = {"name": "DIC Test", "appletType": "Dial In Config",
              "configUrl": f"dialinconfig.asp?phonenum={PLAN_ID}&myapp=DIC Test&application=DialInConfig"}
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        e = export_applet(portal, PLAN_ID, applet, Path(d), [])
        assert (Path(d) / "DIC Test announcement 0.wav").read_bytes() == b"RIFF audio"
    portal.download_recording.assert_called_once_with(RECORDING)
    assert e["type"] == "DialInConfig"
    assert e["settings"] == {"SelectedConfiguration": 0, "AltText.0": "Normal", "AltText.1": "Test", "AltText.9": "Help"}
    assert e["next"] == {"NextApplet.0": "DR Only Default", "NextApplet.9": "DR Empty2"}
    assert [m["property"] for m in e["media"]] == ["Announcement.0"] and e["media"][0]["file"] == "media/DIC Test announcement 0.wav"


# --- saving ---

def client(*responses):
    session = MagicMock()
    session.get.return_value = MagicMock(status_code=200, text="var user = { account: 'NVMDemo29' };")
    session.request.side_effect = [MagicMock(status_code=200, is_redirect=False, headers={}, text=t) for t in responses]
    return PortalClient(ACCOUNT, session=session), session


def posted(call):
    """(path, [(name, value or (filename, content type))]) of a recorded request."""
    method, url = call.args[:2]
    if "files" in call.kwargs:
        fields = [(k, v[1] if v[0] is None else (v[0], v[2])) for k, v in call.kwargs["files"]]
    else:
        fields = list(call.kwargs.get("data") or [])
    return url.rsplit("/", 1)[1], fields


def recorded(index, applet, alt, file=("", "application/octet-stream")):
    """A configuration post as the browser sent it (#3, #5, #7, #12, #14)."""
    return [("validation", "true"), ("name", "dialinconfig"), ("optype", "announcementconfig"), ("phonenum", PLAN_ID),
            ("myapp", "DIC Test"), ("application", "DialInConfig"), ("TargetURL", "../uploads/"), ("index", str(index)),
            ("announcement", ""), (f"nextapplet{index}", applet), ("alttext", alt), ("myfile", file), ("B2", "Update")]


def test_save_posts_each_configuration_as_the_browser_does_then_the_chosen_one(tmp_path):
    wav = tmp_path / "Announcer Team Emergency.wav"
    wav.write_bytes(b"RIFF")
    after = page(LAST, selected=6)
    portal, session = client(page(), SAVED, SAVED, SAVED, CHOSEN.format(6), after)
    settings = {"SelectedConfiguration": 6, "AltText.0": "Normal", "AltText.1": "Test", "AltText.9": "Help"}
    nxt = {"NextApplet.0": "DR Only Default", "NextApplet.9": "DR Empty2"}
    portal.save_dial_in_config(PLAN_ID, "DIC Test", settings, nxt, {0: wav})
    calls = session.request.call_args_list
    assert calls[0].args[0] == "GET" and "dialinconfig.asp?phonenum=" in calls[0].args[1]
    assert posted(calls[1]) == ("updated.asp", recorded(0, "DR Only Default", "Normal", (wav.name, "audio/wav")))
    assert posted(calls[2]) == ("updated.asp", recorded(1, "", "Test"))
    assert posted(calls[3]) == ("updated.asp", recorded(9, "DR Empty2", "Help"))
    # #16: the chosen configuration, a plain form post.
    assert posted(calls[4]) == ("updated.asp", [
        ("validation", "true"), ("name", "dialinconfig"), ("optype", "configNumber"), ("phonenum", PLAN_ID),
        ("myapp", "DIC Test"), ("application", "DialInConfig"), ("option", "Selected6"), ("B3", "Update")])
    assert len(calls) == 6  # empty configurations are not posted


def test_configuration_zero_needs_no_choice_post():
    portal, session = client(page(), SAVED, page({2: ("ANN TTS", "")}))
    portal.save_dial_in_config(PLAN_ID, "DIC Test", {"SelectedConfiguration": 0}, {"NextApplet.2": "ANN TTS"})
    assert len(session.request.call_args_list) == 3


def test_a_save_not_confirmed_or_reading_back_otherwise_fails():
    portal, _ = client(page(), "<td id=\"statusMessage\">Something went wrong</td>")
    with pytest.raises(PortalError, match="configuration 2 was not confirmed: Something went wrong"):
        portal.save_dial_in_config(PLAN_ID, "DIC Test", {"SelectedConfiguration": 0}, {"NextApplet.2": "ANN TTS"})
    portal, _ = client(page(), SAVED, page())
    with pytest.raises(PortalError, match="reads back with configuration 2 applet ''"):
        portal.save_dial_in_config(PLAN_ID, "DIC Test", {"SelectedConfiguration": 0}, {"NextApplet.2": "ANN TTS"})
    # The choice must confirm the number chosen, not only a save.
    for answer in (SAVED, CHOSEN.format(5)):
        portal, _ = client(page(), SAVED, answer)
        with pytest.raises(PortalError, match="selected configuration was not confirmed"):
            portal.save_dial_in_config(PLAN_ID, "DIC Test", {"SelectedConfiguration": 6}, {"NextApplet.2": "ANN TTS"})


def test_an_applet_the_page_does_not_offer_is_refused_before_posting():
    portal, session = client(page())
    with pytest.raises(PortalError, match="applet 'Elsewhere' \\(configuration 3\\) is not offered"):
        portal.save_dial_in_config(PLAN_ID, "DIC Test", {"SelectedConfiguration": 0}, {"NextApplet.3": "Elsewhere"})
    assert len(session.request.call_args_list) == 1


@pytest.mark.parametrize("settings, nxt, media, message", [
    ({"SelectedConfiguration": 10}, {}, [], "selected configuration 10 is not 0 to 9"),
    ({"SelectedConfiguration": 0, "Other": 1}, {}, [], "settings Other have not been recorded yet"),
    ({"SelectedConfiguration": 0}, {"NextApplet.10": "X"}, [], "settings NextApplet.10 have not been recorded yet"),
    ({"SelectedConfiguration": 0}, {}, [{"property": "Music"}], "audio Music is not a configuration's announcement"),
])
def test_unrecorded_settings_are_refused(settings, nxt, media, message):
    reason = unsupported_reason({"name": "D", "type": "DialInConfig", "settings": settings, "next": nxt, "media": media})
    assert reason and message in reason


# --- the empty selected configuration ---

def dic(selected, **extra):
    return {"name": "DIC Test", "kind": "Dial In Config", "type": "DialInConfig",
            "settings": {"SelectedConfiguration": selected, "AltText.1": "Test"},
            "next": {"NextApplet.9": "DR Empty2"}, "media": [{"property": "Announcement.0", "file": "media/a.wav",
                                                              "name": None, "sha256": "x"}], **extra}


@pytest.mark.parametrize("selected, empty", [(0, None), (1, None), (9, None), (6, 6)])
def test_selecting_an_empty_configuration_is_found(selected, empty):
    # 0 has only an announcement, 1 only a text, 9 only an applet: none of them is empty.
    assert dial_in_config_empty_selection(dic(selected)) == empty


class FakePortal:
    account = ACCOUNT

    def __init__(self):
        self.applets, self.writes = {}, []

    def list_applets(self, plan_id):
        return {"name": "Scratch", "applets": [{"id": 100 + i, "guid": f"g-{n}", "name": n, "appletType": t}
                                               for i, (n, t) in enumerate(self.applets.items())]}

    def list_data_sources(self, plan_id):
        return set()

    def create_applet(self, plan_id, applet_type, name):
        self.writes.append(("create", name, applet_type))
        self.applets[name] = applet_type

    def delete_applet(self, plan_id, name):
        self.writes.append(("delete", name))

    def save_dial_in_config(self, plan_id, name, settings, nxt, audio):
        self.writes.append(("dic", name, settings, nxt, audio))

    def save_applet(self, plan_id, name, props):
        self.writes.append(("save", name))

    def get_applet_state(self, plan_id, applet):
        return {"appletProperties": {}}

    def set_initial_applet(self, plan_id, name):
        self.writes.append(("initial", name))


def write_doc(tmp_path, applet):
    (tmp_path / "media").mkdir()
    (tmp_path / "media" / "a.wav").write_bytes(b"RIFF")
    others = [{"name": "DR Empty2", "type": "SetDataSource", "settings": {"DataSources": []}, "next": {}}]
    doc = {"format": "vccflow.plan/1", "plan": {"name": "Source", "initialApplet": "DIC Test"}, "applets": [applet, *others]}
    (tmp_path / "plan.json").write_text(json.dumps(doc), encoding="utf-8")


def test_import_saves_with_the_announcements_and_warns_about_an_empty_selection(tmp_path, monkeypatch):
    monkeypatch.setattr("vccflow.importer.export.export_plan",
                        lambda *a, **k: {"plan": {"initialApplet": "DIC Test"}, "applets": []})
    write_doc(tmp_path, dic(6))
    portal, logs = FakePortal(), []
    import_plan(portal, tmp_path, PLAN_ID, "Scratch", log=logs.append)
    assert ("create", "DIC Test", "DialInConfig") in portal.writes
    _, _, settings, nxt, audio = next(w for w in portal.writes if w[0] == "dic")
    assert settings["SelectedConfiguration"] == 6 and nxt == {"NextApplet.9": "DR Empty2"}
    assert audio == {0: tmp_path / "media" / "a.wav"}
    assert ("WARNING: Dial In Config 'DIC Test' selects configuration 6, which is empty "
            "(no applet, alternative text or announcement)") in logs


def test_comparison_reports_each_configuration_and_an_empty_selection():
    a = dic(0)
    b = json.loads(json.dumps(a))
    b["settings"]["AltText.1"] = "Other"
    b["settings"]["SelectedConfiguration"] = 6
    b["next"]["NextApplet.9"] = "DIC Test"
    b["media"][0]["sha256"] = "y"
    doc = lambda x: {"plan": {}, "applets": [x, {"name": "DR Empty2", "type": "SetDataSource", "settings": {}, "next": {}}]}
    report = compare_docs(doc(a), doc(b))
    diffs = report["differences"]
    assert "'DIC Test' setting AltText.1: source Test | target Other" in diffs
    assert "'DIC Test' setting SelectedConfiguration: source 0 | target 6" in diffs
    assert "'DIC Test' path NextApplet.9 is different: source 'DR Empty2' | target 'DIC Test'" in diffs
    assert "'DIC Test' audio Announcement.0 has different content" in diffs
    assert ("target: 'DIC Test' selects configuration 6, which is empty (no applet, alternative text or announcement)"
            in report["problems"])
    assert compare_docs(doc(a), doc(json.loads(json.dumps(a))))["differences"] == []
