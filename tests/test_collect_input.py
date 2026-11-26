"""Collect Input (collectInput.har): an MVC page, CollectDtmfTones, saved with one multipart post; digits to a data source."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vccflow.compare import compare_docs
from vccflow.config import Account
from vccflow.export import data_sources_created, data_sources_used, export_applet
from vccflow.importer import import_plan, unsupported_reason
from vccflow.portal import (CI_SAVED, PortalClient, PortalError, collect_input_form, collect_input_settings,
                            extract_collect_input)

PLAN_ID = "b918444e-7c29-41b0-9a65-9a64b8568257"
ACCOUNT = Account(name="Demo29", region="emea", allow_portal_writes=True, portal_account="NVMDemo29")
APPLETS = ["DIC Empty", "DIC to empty 3", "DR Decimal"]
REC = "recordings/56303/b918444e-7c29-41b0-9a65-9a64b8568257/{}NVMDemo29.wav"
INITIAL, PLAYBACK, CONFIRMATION = (REC.format(g) for g in ("b9fc13d7-cedf-48dd-a16f-6360f6f58fc8",
                                                           "e7055c83-932b-4bc0-8c9d-1238267e854d",
                                                           "2828607d-bece-4f83-8c0e-601fe3191b9d"))
REFUSED = "Next applet after maximum input prompts: Must be set if 'Maximum input prompts' is not 'Maximum'"


def page(alt="", ds="", nxt="", count="0", timeout="", collect=True, confirm=False, playback="", confirmation="",
         audio=(), message=""):
    """A CollectDtmfTones page as VCC writes it (structure of the recorded responses)."""
    def file_row(slot):
        link = f'<a href="../{audio[slot]}" target="_blank">Play Existing</a>' if slot in audio else ""
        return (f'<tr><td class="propertyName shrink"><label for="{slot}">x</label></td>\n<td class="shrink">\n'
                f'<input class="selectfile" name="{slot}" id="{slot}" type="file" accept="audio/wav,wav" '
                f"onchange=\"DisplayChosenFile('{slot}', '{slot}label')\">\n<label class=\"fileChooseLabel\" "
                f'for="{slot}">Choose file</label>\n</td>\n<td>\n{link}\n</td>\n</tr>\n')

    def select(name, current, values):
        opts = "".join(f'<option{" selected=\"selected\"" if v == current else ""} value="{v}">{t}</option>\n'
                       for v, t in values)
        return f'<select id="{name}" name="{name}">{opts}</select>\n'

    def box(name, on):
        return (f'<input{" checked=\"checked\"" if on else ""} id="{name}" name="{name}" type="checkbox" value="true" />'
                f'<input name="{name}" type="hidden" value="false" />\n')

    applets = [("", "")] + [(a, f"{a} (Data Router)") for a in APPLETS]
    return (f'<form action="/CallCentre/apps/CollectDtmfTones?phoneNum={PLAN_ID}&amp;myApp=CI%20Input" '
            'enctype="multipart/form-data" id="CollectDtmfTonesForm" method="post">'
            '<input name="__RequestVerificationToken" type="hidden" value="tok" />'
            f'<div class="validation-summary-valid" data-valmsg-summary="true"><ul><li>{message}</li>\n</ul></div>'
            '<table>' + file_row("InitialAudioFile")
            + f'<input id="InitialTextToSpeak" name="InitialTextToSpeak" type="text" value="{alt}" />\n'
            '<input id="MaxDigitCount" maxlength="2" name="MaxDigitCount" size="4" type="text" value="11" />\n'
            + box("CollectToDataSource", collect) + select("IvrSlot", "", [("", "")])
            + f'<input id="DataSource" name="DataSource" type="text" value="{ds}" />\n'
            + select("NextApplet", nxt, applets)
            + select("TimeoutCount", count, [("0", "Maximum")] + [(str(i), str(i)) for i in range(1, 11)])
            + select("TimeoutAppletName", timeout, applets) + box("ConfirmInput", confirm)
            + file_row("PlaybackAudioFile")
            + f'<input id="PlaybackTextToSpeak" name="PlaybackTextToSpeak" type="text" value="{playback}" />\n'
            + file_row("ConfirmationAudioFile")
            + f'<input id="ConfirmationTextToSpeak" name="ConfirmationTextToSpeak" type="text" value="{confirmation}" />\n'
            '</table><input id="submitButton" type="submit" value="Update"/></form>')


ALL_AUDIO = {"InitialAudioFile": INITIAL, "PlaybackAudioFile": PLAYBACK, "ConfirmationAudioFile": CONFIRMATION}
# The last recorded page (#32).
LAST = dict(alt="My alternative text", ds="My Second Destination", nxt="DR Decimal", count="8", timeout="DIC Empty",
            confirm=True, playback="Playback", confirmation="Confirmation", audio=ALL_AUDIO)
LAST_SETTINGS = {"InitialTextToSpeak": "My alternative text", "MaxDigitCount": "11", "CollectToDataSource": True,
                 "DataSource": "My Second Destination", "TimeoutCount": "8", "ConfirmInput": True,
                 "PlaybackTextToSpeak": "Playback", "ConfirmationTextToSpeak": "Confirmation"}
LAST_NEXT = {"NextApplet": "DR Decimal", "TimeoutApplet": "DIC Empty"}


# --- reading ---

def test_the_page_gives_every_setting_the_next_applets_by_name_and_the_audio():
    p = extract_collect_input(page(**LAST))
    assert collect_input_settings(p) == (LAST_SETTINGS, LAST_NEXT)
    assert p["audio"] == ALL_AUDIO


def test_unticking_the_confirmation_clears_its_texts_and_keeps_its_audio():
    # #24: the page after unticking "Confirmation prompt?".
    p = extract_collect_input(page(alt="My alternative text", ds="MyDestination", nxt="DR Decimal", count="10",
                                   timeout="DIC to empty 3", audio=ALL_AUDIO))
    settings, nxt = collect_input_settings(p)
    assert settings["ConfirmInput"] is False and settings["PlaybackTextToSpeak"] == settings["ConfirmationTextToSpeak"] == ""
    assert set(p["audio"]) == set(ALL_AUDIO)


def test_another_page_is_refused():
    with pytest.raises(PortalError, match="Not a Collect Input page"):
        extract_collect_input("<form><input name='x' value='1'/></form>")


# --- the post, as the browser sent it ---

EMPTY = ("", b"", "application/octet-stream")


def recorded(alt="My alternative text", ds="MyDestination", nxt="", count="0", timeout="", initial=EMPTY,
             confirm=None):
    """A recorded post's fields in order, token left out (collectInput.har). confirm: None (unticked), or
    (playback file, playback text, confirmation file, confirmation text)."""
    out = [("InitialAudioFile", initial), ("InitialTextToSpeak", alt), ("MaxDigitCount", "11"),
           ("CollectToDataSource", "true"), ("CollectToDataSource", "false"), ("IvrSlot", ""), ("DataSource", ds),
           ("NextApplet", nxt), ("TimeoutCount", count), ("TimeoutAppletName", timeout)]
    if confirm is None:
        return out + [("ConfirmInput", "false")]
    pf, pt, cf, ct = confirm
    return out + [("ConfirmInput", "true"), ("ConfirmInput", "false"), ("PlaybackAudioFile", pf),
                  ("PlaybackTextToSpeak", pt), ("ConfirmationAudioFile", cf), ("ConfirmationTextToSpeak", ct)]


def settings_of(**page_args):
    return collect_input_settings(extract_collect_input(page(**page_args)))


WAV = ("Announcer Team Emergency.wav", b"RIFF")
UK = ("UK_CallRecording_Test.wav", b"RIFF2")


@pytest.mark.parametrize("state, uploads, expected", [
    # #3: the first save, a data source only.
    (dict(ds="MyDestination"), {}, recorded(alt="")),
    # #14: the input prompt audio.
    (dict(alt="My alternative text", ds="MyDestination"), {"InitialAudioFile": WAV},
     recorded(initial=(*WAV, "audio/wav"))),
    # #15, #19, #26: next applets, maximum input prompts.
    (dict(alt="My alternative text", ds="MyDestination", nxt="DR Decimal"), {}, recorded(nxt="DR Decimal")),
    (dict(alt="My alternative text", ds="MyDestination", nxt="DR Decimal", count="1", timeout="DIC to empty 3"), {},
     recorded(nxt="DR Decimal", count="1", timeout="DIC to empty 3")),
    (dict(alt="My alternative text", ds="MyDestination", nxt="DR Decimal", timeout="DIC to empty 3"), {},
     recorded(nxt="DR Decimal", timeout="DIC to empty 3")),
    # #22: the confirmation ticked, nothing in it.
    (dict(alt="My alternative text", ds="MyDestination", nxt="DR Decimal", count="10", timeout="DIC to empty 3",
          confirm=True), {},
     recorded(nxt="DR Decimal", count="10", timeout="DIC to empty 3", confirm=(EMPTY, "", EMPTY, ""))),
    # #23: its audio files and texts.
    (dict(alt="My alternative text", ds="MyDestination", nxt="DR Decimal", count="10", timeout="DIC to empty 3",
          confirm=True, playback="Playback", confirmation="Confirmation"),
     {"PlaybackAudioFile": WAV, "ConfirmationAudioFile": UK},
     recorded(nxt="DR Decimal", count="10", timeout="DIC to empty 3",
              confirm=((*WAV, "audio/wav"), "Playback", (*UK, "audio/wav"), "Confirmation"))),
    # #24: unticked again; its fields are not posted.
    (dict(alt="My alternative text", ds="MyDestination", nxt="DR Decimal", count="10", timeout="DIC to empty 3",
          audio=ALL_AUDIO), {}, recorded(nxt="DR Decimal", count="10", timeout="DIC to empty 3")),
    # #32: the last save.
    (LAST, {}, recorded(ds="My Second Destination", nxt="DR Decimal", count="8", timeout="DIC Empty",
                        confirm=(EMPTY, "Playback", EMPTY, "Confirmation"))),
])
def test_the_form_rebuilds_the_recorded_posts_from_the_page_saved(state, uploads, expected):
    settings, nxt = settings_of(**state)
    assert collect_input_form(settings, nxt, uploads) == expected


@pytest.mark.parametrize("change, message", [
    # #18 and #31, which VCC refused.
    (lambda s, n: (s.update(TimeoutCount="8"), n.pop("TimeoutApplet")), "maximum input prompts 8 and no next applet"),
    (lambda s, n: s.update(CollectToDataSource=False, IvrSlot="Slot 1"), "IVR slot is not supported"),
    (lambda s, n: s.update(DataSource=""), "no destination data source"),
    (lambda s, n: s.update(TimeoutCount="11"), "is not 'Maximum' or 1 to 10"),
    (lambda s, n: s.update(ConfirmInput=False), "only kept with the confirmation prompt"),
    (lambda s, n: s.update(Other="x"), "settings Other have not been recorded yet"),
])
def test_what_vcc_refuses_or_was_not_recorded_is_refused(change, message):
    settings, nxt = dict(LAST_SETTINGS), dict(LAST_NEXT)
    change(settings, nxt)
    with pytest.raises(PortalError, match=message):
        collect_input_form(settings, nxt)
    assert message in unsupported_reason({"name": "CI", "type": "DigitStringCollection", "settings": settings,
                                          "next": nxt})


def test_maximum_input_prompts_maximum_needs_no_next_applet():
    settings = {**LAST_SETTINGS, "TimeoutCount": "0"}
    assert unsupported_reason({"name": "CI", "type": "DigitStringCollection", "settings": settings,
                               "next": {"NextApplet": "DR Decimal"}}) is None


# --- saving ---

def client(*responses):
    session = MagicMock()
    session.get.return_value = MagicMock(status_code=200, text="var user = { account: 'NVMDemo29' };")
    session.request.side_effect = [MagicMock(status_code=200, is_redirect=False, headers={}, text=t) for t in responses]
    return PortalClient(ACCOUNT, session=session), session


def posted(call):
    """The fields posted, a file as (file name, content type)."""
    return [(k, v[1] if v[0] is None else (v[0], v[2])) for k, v in call.kwargs["files"]]


def as_posted(fields):
    return [(k, (v[0], v[2]) if isinstance(v, tuple) else v) for k, v in fields]


def test_save_posts_the_form_with_the_token_and_checks_the_page_read_back(tmp_path):
    wav = tmp_path / "CI Input InitialAudioFile.wav"
    wav.write_bytes(b"RIFF")
    portal, session = client(page(), page(**LAST, message=CI_SAVED))
    portal.save_collect_input(PLAN_ID, "CI Input", LAST_SETTINGS, LAST_NEXT,
                              {k: wav for k in ("InitialAudioFile", "PlaybackAudioFile", "ConfirmationAudioFile")})
    get, post = session.request.call_args_list
    assert get.args[0] == "GET" and get.args[1].endswith(f"CollectDtmfTones?phoneNum={PLAN_ID}&myApp=CI%20Input"
                                                         "&application=DigitStringCollection")
    assert post.args[0] == "POST" and post.args[1].endswith(f"CollectDtmfTones?phoneNum={PLAN_ID}&myApp=CI%20Input")
    fields = posted(post)
    assert fields[0] == ("__RequestVerificationToken", "tok")
    file = (wav.name, b"", "audio/wav")
    assert fields[1:] == as_posted(recorded(ds="My Second Destination", nxt="DR Decimal", count="8",
                                            timeout="DIC Empty", initial=file,
                                            confirm=(file, "Playback", file, "Confirmation")))


def test_a_refused_save_gives_vccs_message():
    settings = {**LAST_SETTINGS, "TimeoutCount": "0"}
    portal, _ = client(page(), page(message=REFUSED))
    with pytest.raises(PortalError, match="was refused: Next applet after maximum input prompts: Must be set"):
        portal.save_collect_input(PLAN_ID, "CI Input", settings, LAST_NEXT)


def test_a_save_reading_back_otherwise_fails():
    portal, _ = client(page(), page(**{**LAST, "timeout": ""}, message=CI_SAVED))
    with pytest.raises(PortalError, match="reads back with TimeoutApplet ''"):
        portal.save_collect_input(PLAN_ID, "CI Input", LAST_SETTINGS, LAST_NEXT)
    # Audio already there that the source does not have (none can be removed) is reported too.
    portal, _ = client(page(), page(**{**LAST, "audio": {"InitialAudioFile": INITIAL}}, message=CI_SAVED))
    with pytest.raises(PortalError, match="InitialAudioFile present"):
        portal.save_collect_input(PLAN_ID, "CI Input", LAST_SETTINGS, LAST_NEXT)


def test_an_applet_the_page_does_not_offer_is_refused_before_posting():
    portal, session = client(page())
    with pytest.raises(PortalError, match="applets not offered by the page: Elsewhere"):
        portal.save_collect_input(PLAN_ID, "CI Input", LAST_SETTINGS, {**LAST_NEXT, "NextApplet": "Elsewhere"})
    assert len(session.request.call_args_list) == 1


def test_confirmation_audio_with_the_confirmation_off_is_uploaded_with_it_on_then_turned_off(tmp_path):
    # As #23 then #24: VCC keeps the audio when the prompt is unticked.
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFF")
    off = {**LAST_SETTINGS, "ConfirmInput": False, "PlaybackTextToSpeak": "", "ConfirmationTextToSpeak": ""}
    after = {**LAST, "confirm": False, "playback": "", "confirmation": "", "audio": {"PlaybackAudioFile": PLAYBACK}}
    portal, session = client(page(), page(**{**after, "confirm": True}, message=CI_SAVED), page(**after, message=CI_SAVED))
    portal.save_collect_input(PLAN_ID, "CI Input", off, LAST_NEXT, {"PlaybackAudioFile": wav})
    _, first, second = session.request.call_args_list
    base = dict(ds="My Second Destination", nxt="DR Decimal", count="8", timeout="DIC Empty")
    assert posted(first)[1:] == as_posted(recorded(**base, confirm=(("a.wav", b"", "audio/wav"), "", EMPTY, "")))
    assert posted(second)[1:] == as_posted(recorded(**base))


# --- export, data sources, import, comparison ---

def test_export_keeps_the_settings_the_next_applets_by_name_and_the_audio(tmp_path):
    portal = MagicMock()
    portal.get_collect_input.return_value = extract_collect_input(page(**LAST))
    portal.download_recording.side_effect = lambda path: path.encode()
    applet = {"name": "CI Input", "appletType": "Collect Input",
              "configUrl": f"CollectDtmfTones?phonenum={PLAN_ID}&myapp=CI Input&application=DigitStringCollection"}
    e = export_applet(portal, PLAN_ID, applet, tmp_path)
    assert e["type"] == "DigitStringCollection" and e["settings"] == LAST_SETTINGS and e["next"] == LAST_NEXT
    assert [m["property"] for m in e["media"]] == ["ConfirmationAudioFile", "InitialAudioFile", "PlaybackAudioFile"]
    assert (tmp_path / "CI Input InitialAudioFile.wav").read_bytes() == INITIAL.encode()


def test_the_destination_is_a_data_source_the_applet_sets():
    a = {"name": "CI", "type": "DigitStringCollection", "settings": LAST_SETTINGS, "next": LAST_NEXT}
    assert data_sources_created(a) == {"My Second Destination"}
    assert "My Second Destination" not in data_sources_used(a)


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

    def save_collect_input(self, plan_id, name, settings, nxt, audio):
        self.writes.append(("ci", name, settings, nxt, audio))

    def save_applet(self, plan_id, name, props):
        self.writes.append(("save", name))

    def get_applet_state(self, plan_id, applet):
        return {"appletProperties": {}}

    def set_initial_applet(self, plan_id, name):
        self.writes.append(("initial", name))


def ci(**settings):
    return {"name": "CI Input", "type": "DigitStringCollection", "settings": {**LAST_SETTINGS, **settings},
            "next": {"NextApplet": "DR Decimal", "TimeoutApplet": "DR Decimal"},
            "media": [{"property": "InitialAudioFile", "file": "media/a.wav", "name": None, "sha256": "x"}]}


def test_import_saves_with_the_audio(tmp_path, monkeypatch):
    monkeypatch.setattr("vccflow.importer.export.export_plan",
                        lambda *a, **k: {"plan": {"initialApplet": "CI Input"}, "applets": []})
    (tmp_path / "media").mkdir()
    (tmp_path / "media" / "a.wav").write_bytes(b"RIFF")
    others = [{"name": "DR Decimal", "type": "SetDataSource", "settings": {"DataSources": []}, "next": {}}]
    doc = {"format": "vccflow.plan/1", "plan": {"name": "Source", "initialApplet": "CI Input"},
           "applets": [ci(), *others]}
    (tmp_path / "plan.json").write_text(json.dumps(doc), encoding="utf-8")
    portal, logs = FakePortal(), []
    import_plan(portal, tmp_path, PLAN_ID, "Scratch", log=logs.append)
    assert ("create", "CI Input", "DigitStringCollection") in portal.writes
    _, _, settings, nxt, audio = next(w for w in portal.writes if w[0] == "ci")
    assert settings["DataSource"] == "My Second Destination" and nxt["TimeoutApplet"] == "DR Decimal"
    assert audio == {"InitialAudioFile": tmp_path / "media" / "a.wav"}
    assert "   uploaded 1 audio file: InitialAudioFile" in logs


def test_comparison_reports_changes_with_maximum_as_the_page_words_it():
    a = ci(TimeoutCount="0")
    b = json.loads(json.dumps(a))
    b["settings"].update(TimeoutCount="3", DataSource="Other")
    b["next"]["TimeoutApplet"] = "CI Input"
    b["media"][0]["sha256"] = "y"
    doc = lambda x: {"plan": {}, "applets": [x, {"name": "DR Decimal", "type": "SetDataSource", "settings": {},
                                                 "next": {}}]}
    diffs = compare_docs(doc(a), doc(b))["differences"]
    assert "'CI Input' setting TimeoutCount: source Maximum | target 3" in diffs
    assert "'CI Input' setting DataSource: source My Second Destination | target Other" in diffs
    assert "'CI Input' path TimeoutApplet is different: source 'DR Decimal' | target 'CI Input'" in diffs
    assert "'CI Input' audio InitialAudioFile has different content" in diffs
    assert "'CI Input' setting TimeoutCount: source 3 | target Maximum" in compare_docs(doc(b), doc(a))["differences"]
    assert compare_docs(doc(a), doc(json.loads(json.dumps(a))))["differences"] == []
