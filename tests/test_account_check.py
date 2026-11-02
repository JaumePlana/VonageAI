"""The portal login must be signed in to the account configured in portal_account."""

from unittest.mock import MagicMock

import pytest

from vccflow import cli
from vccflow.config import Account, MissingPortalAccount, load_account
from vccflow.portal import PortalClient, PortalError, SessionExpired, WrongAccount, extract_signed_in_account
from vccflow.portal_token import PortalTokenProvider

PAGE = """<script>
    var user = {
        username: 'j.plana@comways.eu',
        account: '{name}',
        accountId: 'b5b14b40',
        Accounts: true
    };
    var account = {other: 'x'};
</script>"""


def page(name):
    return PAGE.replace("{name}", name)


def client(signed_in, portal_account="NVMDemo29", writes=True):
    session = MagicMock()
    session.get.return_value = MagicMock(status_code=200, text=page(signed_in))
    session.request.return_value = MagicMock(status_code=200, is_redirect=False, headers={},
                                             json=MagicMock(return_value={"applets": []}))
    acc = Account(name="Demo29", region="emea", allow_portal_writes=writes, portal_account=portal_account)
    return PortalClient(acc, session=session), session


def test_extract_signed_in_account():
    assert extract_signed_in_account(page("IFM")) == "IFM"
    with pytest.raises(PortalError, match="Cannot tell"):
        extract_signed_in_account("<html></html>")


def test_wrong_account_is_refused_before_any_call():
    portal, session = client("IFM")
    with pytest.raises(WrongAccount, match="signed in to 'IFM', but portal_account is 'NVMDemo29'"):
        portal.list_applets("plan")
    session.request.assert_not_called()


def test_right_account_passes_and_is_checked_once():
    portal, session = client("nvmdemo29")  # names ignore letter case
    portal.list_applets("plan")
    portal.list_applets("plan")
    assert session.get.call_count == 1 and session.request.call_count == 2


def test_wrong_account_is_removed_without_logging_out_of_vcc(isolated_sessions):
    saved = isolated_sessions / "Demo29.json"
    saved.write_text('{"cookies": []}', encoding="utf-8")
    portal, session = client("IFM")
    with pytest.raises(WrongAccount):
        portal.list_applets("plan")
    assert not saved.exists()
    assert not any("logout" in c.args[0].lower() for c in session.get.call_args_list)


def test_expired_login_is_reported_as_expired():
    portal, session = client("IFM")
    session.get.return_value = MagicMock(status_code=302)
    with pytest.raises(SessionExpired):
        portal.list_applets("plan")


def test_portal_login_token_is_refused_for_the_wrong_account():
    cookies = MagicMock()
    cookies.get.return_value = MagicMock(status_code=200, text=page("IFM"))
    tokens = PortalTokenProvider(Account(name="Demo29", region="emea", portal_account="NVMDemo29"), cookies=cookies)
    with pytest.raises(WrongAccount):
        tokens.token("addresses:read")


def login_setup(tmp_path, isolated_sessions, monkeypatch, entry, signed_in):
    config = tmp_path / "accounts.toml"
    config.write_text(f'[accounts.Demo29]\nregion = "emea"\n{entry}', encoding="utf-8")
    saved = isolated_sessions / "Demo29.json"

    def fake_login(account, browser_channel):
        saved.write_text('{"cookies": []}', encoding="utf-8")
        return str(saved)

    monkeypatch.setattr("vccflow.portal_login.login", fake_login)
    monkeypatch.setattr(cli.PortalClient, "signed_in_account", lambda self: signed_in)
    monkeypatch.setattr("vccflow.portal.load_session", lambda account: MagicMock())
    return ["--config", str(config), "login", "-a", "Demo29"], saved


def test_login_to_the_right_account_says_so(tmp_path, isolated_sessions, monkeypatch, capsys):
    args, saved = login_setup(tmp_path, isolated_sessions, monkeypatch, 'portal_account = "NVMDemo29"\n', "NVMDemo29")
    assert cli.main(args) == 0
    assert "You have been logged in to 'NVMDemo29'" in capsys.readouterr().out and saved.exists()


def test_login_to_the_wrong_account_is_not_kept(tmp_path, isolated_sessions, monkeypatch, capsys):
    args, saved = login_setup(tmp_path, isolated_sessions, monkeypatch, 'portal_account = "NVMDemo29"\n', "IFM")
    assert cli.main(args) == 1
    assert "signed in to 'IFM'" in capsys.readouterr().err and not saved.exists()


def test_login_without_portal_account_names_it_and_is_not_kept(tmp_path, isolated_sessions, monkeypatch, capsys):
    args, saved = login_setup(tmp_path, isolated_sessions, monkeypatch, "", "IFM")
    assert cli.main(args) == 1
    assert 'portal_account = "IFM"' in capsys.readouterr().err and not saved.exists()


def test_any_other_command_without_portal_account_is_refused_and_loses_its_login(tmp_path, isolated_sessions, monkeypatch, capsys):
    config = tmp_path / "accounts.toml"
    config.write_text('[accounts.Demo29]\nregion = "emea"\n', encoding="utf-8")
    saved = isolated_sessions / "Demo29.json"
    saved.write_text('{"cookies": []}', encoding="utf-8")
    stored = MagicMock()
    monkeypatch.setattr("vccflow.portal.load_session", lambda account: stored)
    assert cli.main(["--config", str(config), "session", "-a", "Demo29"]) == 1
    err = capsys.readouterr().err
    assert "has no portal_account" in err and "saved login for 'Demo29' was removed" in err and not saved.exists()
    stored.get.assert_not_called()  # nothing sent to VCC


def test_missing_portal_account_is_refused_on_load(tmp_path):
    f = tmp_path / "a.toml"
    f.write_text('[accounts.x]\nregion = "emea"\n', encoding="utf-8")
    with pytest.raises(MissingPortalAccount):
        load_account("x", f, env={})
    assert load_account("x", f, env={}, require_portal_account=False).portal_account is None


def test_config_reads_portal_account(tmp_path):
    f = tmp_path / "a.toml"
    f.write_text('[accounts.x]\nregion = "emea"\nportal_account = "NVMDemo29"\n', encoding="utf-8")
    assert load_account("x", f, env={}).portal_account == "NVMDemo29"
