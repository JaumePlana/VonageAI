from unittest.mock import MagicMock

import pytest

from vccflow.config import Account
from vccflow.portal import (
    PortalClient, PortalError, callback_rename_form, digits, extract_callback_numbers, settings_snapshot,
)

PAGE = """<form id="mainForm" method="post" enctype="multipart/form-data">
<input type="hidden" name="__VIEWSTATE" value="vs" /><input type="hidden" name="__EVENTVALIDATION" value="ev" />
<select name="AccountNumbers"><option value="a1">x</option><option selected="selected" value="a2">y</option></select>
<input name="Pin" type="password" /><input type="submit" name="UpdatePin" value="Update" />
<select name="TransferLimitList"><option value="3">3</option><option value="5">5</option></select>
<input name="PhoneCapacity" type="text" value="100" /><input type="submit" name="InteractionCapacityButton" value="Set" />
<input type="submit" name="DownloadClidButton" value="Download" />
<input type="file" name="ClidUpload" /><input type="submit" name="UploadClidButton" value="Upload" />
<select name="SelectedClidNumber"><option selected="selected" value="2801974">+442030023771 - +442030023771</option>
<option value="3983450">CH Commercial Service - +41564186778</option><option value="1">Sales - Spain - 34518900000</option></select>
<input name="SelectedClidName" type="text" maxlength="50" /><input type="submit" name="UpdateClidName" value="Update" />
<input name="RateCallFrequency" type="text" value="1" />
</form>"""
SUCCESS = PAGE + '<div class="success">The presented name for the callback number selected has been updated.</div>'


def test_list_reads_name_and_number_from_the_dropdown():
    assert extract_callback_numbers(PAGE) == [
        {"id": "2801974", "name": "+442030023771", "number": "+442030023771"},
        {"id": "3983450", "name": "CH Commercial Service", "number": "+41564186778"},
        {"id": "1", "name": "Sales - Spain", "number": "34518900000"},  # a hyphen in the name
    ]
    with pytest.raises(PortalError):
        extract_callback_numbers("<html></html>")


def test_rename_posts_what_the_browser_posts():
    form = callback_rename_form(PAGE, "3983450", "New name")
    assert form == [
        ("__VIEWSTATE", "vs"), ("__EVENTVALIDATION", "ev"), ("AccountNumbers", "a2"), ("Pin", ""),
        ("TransferLimitList", "3"),  # nothing marked selected: the first option
        ("PhoneCapacity", "100"), ("ClidUpload", ""), ("SelectedClidNumber", "3983450"),
        ("SelectedClidName", "New name"), ("UpdateClidName", "Update"), ("RateCallFrequency", "1"),
    ]


def portal(*pages, writes=True):
    session = MagicMock()
    session.get.return_value = MagicMock(status_code=200, text="var user = { account: 'Acme' };")
    session.request.side_effect = [MagicMock(status_code=200, is_redirect=False, headers={}, text=t) for t in pages]
    return PortalClient(Account(name="t", region="emea", allow_portal_writes=writes, portal_account="Acme"), session=session), session


def renamed_page(old="CH Commercial Service", new="New name"):
    return PAGE.replace(f">{old} - ", f">{new} - ")


def test_rename_checks_the_result():
    p, session = portal(PAGE, SUCCESS, renamed_page())
    assert p.rename_callback_number("+41564186778", "New name")["name"] == "New name"
    post = session.request.call_args_list[1]
    assert post.args[0] == "POST" and post.args[1].endswith("/apps/../userAdmin/system-settings.aspx")
    sent = dict(post.kwargs["files"])
    assert sent["SelectedClidNumber"] == (None, "3983450") and sent["SelectedClidName"] == (None, "New name")
    assert sent["ClidUpload"][0] == "" and "DownloadClidButton" not in sent and "UpdatePin" not in sent


def test_rename_finds_the_number_by_name_id_or_digits():
    for which in ("CH Commercial Service", "3983450", "41564186778"):
        p, _ = portal(PAGE, SUCCESS, renamed_page())
        assert p.rename_callback_number(which, "New name")["number"] == "+41564186778"


def test_rename_reports_other_settings_that_moved():
    moved = renamed_page().replace('name="PhoneCapacity" type="text" value="100"', 'name="PhoneCapacity" type="text" value="1"')
    p, _ = portal(PAGE, SUCCESS, moved)
    with pytest.raises(PortalError, match="PhoneCapacity"):
        p.rename_callback_number("3983450", "New name")


def test_rename_requires_confirmation_from_the_portal():
    p, _ = portal(PAGE, PAGE)
    with pytest.raises(PortalError, match="did not confirm"):
        p.rename_callback_number("3983450", "New name")


def test_rename_refuses_before_posting():
    p, session = portal(PAGE)
    with pytest.raises(PortalError, match="found 0"):
        p.rename_callback_number("999", "New name")
    assert session.request.call_count == 1  # only the read
    p, session = portal(writes=False)
    with pytest.raises(PortalError, match="Writes are disabled"):
        p.rename_callback_number("3983450", "New name")
    session.request.assert_not_called()
    p, session = portal()
    with pytest.raises(PortalError, match="1 to 50"):
        p.rename_callback_number("3983450", "x" * 51)


def test_snapshot_ignores_page_state_and_digits_normalise():
    assert set(settings_snapshot(PAGE)) == {"TransferLimitList", "PhoneCapacity", "RateCallFrequency"}
    assert digits("+44 20-30") == "442030" and digits(None) == ""
