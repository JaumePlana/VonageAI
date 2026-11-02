from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest

from vccflow.config import Account
from vccflow.manager import ManagerClient, ManagerError
from vccflow.portal import PortalError, SessionExpired
from vccflow.portal_token import PortalTokenProvider, extract_oidc_settings, token_from_redirect

PAGE = """<script>
    var user = { account: 'Acme' };
    var oidcSettings = {
        authority: 'https://emea.newvoicemedia.com/Auth/',
        client_id: 'contactworld_oidc_client',
        silent_redirect_uri: 'https://emea.newvoicemedia.com/CallCentre/Oidc/SilentRenew',
        response_type: 'id_token token',
        scope: 'openid users:read addresses:write interaction-plans:write',
        automaticSilentRenew: true
    };
</script>"""
REDIRECT = "https://emea.newvoicemedia.com/CallCentre/Oidc/SilentRenew#id_token=x&access_token=TOKEN&expires_in=3600&state=s"


def test_extract_oidc_settings():
    s = extract_oidc_settings(PAGE)
    assert s["client_id"] == "contactworld_oidc_client" and "addresses:write" in s["scope"].split()


def test_token_from_redirect():
    assert token_from_redirect(REDIRECT) == ("TOKEN", 3600)
    with pytest.raises(SessionExpired):
        token_from_redirect("https://x/SilentRenew#error=login_required&state=s")


def provider():
    cookies = MagicMock()
    cookies.get.side_effect = [
        MagicMock(status_code=200, text=PAGE),
        MagicMock(status_code=302, is_redirect=True, headers={"Location": REDIRECT}),
    ]
    return PortalTokenProvider(Account(name="t", region="emea", portal_account="Acme"), cookies=cookies, clock=lambda: 0), cookies


def test_asks_only_for_the_needed_scope_and_caches_it():
    p, cookies = provider()
    assert p.token("addresses:read") == "TOKEN"
    assert p.token("addresses:read") == "TOKEN"  # cached: no third request
    assert cookies.get.call_count == 2
    query = parse_qs(urlsplit(cookies.get.call_args.args[0]).query)
    assert query["scope"] == ["openid addresses:write"] and query["prompt"] == ["none"]
    assert query["client_id"] == ["contactworld_oidc_client"]


def test_refuses_scopes_the_portal_client_does_not_have():
    p, _ = provider()
    with pytest.raises(PortalError, match="cannot get scope"):
        p.token("users:write")


def test_channels_not_enabled_map_to_none():
    tokens = MagicMock()
    tokens.account = Account(name="t", region="emea")
    session = MagicMock()
    session.get.side_effect = [
        MagicMock(status_code=200, json=MagicMock(return_value=[{"address": "1"}])),
        MagicMock(status_code=403, text='{"title":"Feature not enabled."}'),
    ]
    c = ManagerClient(tokens, session=session)
    assert c.list_destinations(("pstn", "webchat")) == {"pstn": [{"address": "1"}], "webchat": None}
    session.get.side_effect = [MagicMock(status_code=403, text='{"title":"Authorization failed."}')]
    with pytest.raises(ManagerError):
        c.list_destinations(("pstn",))
