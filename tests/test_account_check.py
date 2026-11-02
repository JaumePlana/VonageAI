"""The portal login must be signed in to the account configured in portal_account."""

from unittest.mock import MagicMock

import pytest

from vccflow import cli
from vccflow.config import Account, load_account
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


def test_writes_need_portal_account():
    portal, session = client("IFM", portal_account=None)
    with pytest.raises(PortalError, match="Writes need portal_account"):
        portal.delete_applet("plan", "ANN")
    session.request.assert_not_called()


def test_reads_without_portal_account_warn(capsys):
    portal, _ = client("IFM", portal_account=None)
    portal.list_applets("plan")
    portal.list_applets("plan")
    assert capsys.readouterr().err.count("no portal_account set") == 1


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


def login_setup(tmp_path, monkeypatch, entry, signed_in):
    config = tmp_path / "accounts.toml"
    config.write_text(f'[accounts.Demo29]\nregion = "emea"\n{entry}', encoding="utf-8")
    saved = tmp_path / "Demo29.json"
    saved.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("vccflow.portal_login.login", lambda account, browser_channel: str(saved))
    monkeypatch.setattr(cli.PortalClient, "signed_in_account", lambda self: signed_in)
    monkeypatch.setattr("vccflow.portal.load_session", lambda account: MagicMock())
    args = ["--config", str(config), "login", "-a", "Demo29"]
    return args, saved


def test_login_to_the_right_account_says_so(tmp_path, monkeypatch, capsys):
    args, saved = login_setup(tmp_path, monkeypatch, 'portal_account = "NVMDemo29"\n', "NVMDemo29")
    assert cli.main(args) == 0
    assert "You have been logged in to 'NVMDemo29'" in capsys.readouterr().out and saved.exists()


def test_login_to_the_wrong_account_is_not_kept(tmp_path, monkeypatch, capsys):
    args, saved = login_setup(tmp_path, monkeypatch, 'portal_account = "NVMDemo29"\n', "IFM")
    assert cli.main(args) == 1
    assert "signed in to 'IFM'" in capsys.readouterr().err and not saved.exists()


def test_login_without_portal_account_suggests_it(tmp_path, monkeypatch, capsys):
    args, saved = login_setup(tmp_path, monkeypatch, "", "IFM")
    assert cli.main(args) == 0
    assert 'portal_account = "IFM"' in capsys.readouterr().out and saved.exists()


def test_config_reads_portal_account(tmp_path):
    f = tmp_path / "a.toml"
    f.write_text('[accounts.x]\nregion = "emea"\nportal_account = "NVMDemo29"\n', encoding="utf-8")
    assert load_account("x", f, env={}).portal_account == "NVMDemo29"
