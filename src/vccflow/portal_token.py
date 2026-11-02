"""Access tokens from the portal login, as the VCC Admin pages get them.

The portal (CallCentre/portal) signs the user in with OIDC (implicit flow,
client "contactworld_oidc_client") and renews the access token silently:
GET <portal host>/Auth/connect/authorize?...&prompt=none with the login cookies,
which answers with a redirect whose URL fragment carries the token. vccflow does
the same with the cookies saved by `vccflow login`.

That token acts as the logged-in user, with the user's permissions, and can carry
scopes the account's API credentials lack (e.g. addresses, for destinations).
vccflow uses it only for reads, requests only the scope a call needs, and keeps
it in memory; it is never written to disk.
"""

from __future__ import annotations

import re
import secrets
import time
from typing import Callable
from urllib.parse import parse_qs, urlencode, urlsplit

import requests

from vccflow.config import Account
from vccflow.portal import PortalError, SessionExpired, check_portal_account, extract_signed_in_account, load_session

OIDC_SETTINGS = re.compile(r"var\s+oidcSettings\s*=\s*\{(.*?)\};", re.S)
SETTING = r"\b{name}\s*:\s*'([^']*)'"
EXPIRY_MARGIN = 60
# Read scope an API asks for -> the scope the portal client may request instead.
# The portal client lists only the :write variants; the APIs accept them for reads.
PORTAL_SCOPE = {"addresses:read": "addresses:write", "interaction-plans:read": "interaction-plans:write"}


def extract_oidc_settings(html: str) -> dict:
    m = OIDC_SETTINGS.search(html)
    if not m:
        raise PortalError("The portal page has no oidcSettings; the page format may have changed")
    out = {}
    for name in ("authority", "client_id", "silent_redirect_uri", "response_type", "scope"):
        v = re.search(SETTING.format(name=name), m.group(1))
        if not v:
            raise PortalError(f"oidcSettings has no {name}")
        out[name] = v.group(1)
    return out


def token_from_redirect(location: str) -> tuple[str, int]:
    """Read (access_token, expires_in) from the silent-renew redirect, or raise."""
    frag = parse_qs(urlsplit(location).fragment)
    if "access_token" in frag:
        return frag["access_token"][0], int(frag.get("expires_in", ["300"])[0])
    error = (frag.get("error") or parse_qs(urlsplit(location).query).get("error") or ["no token"])[0]
    if error in ("login_required", "interaction_required", "consent_required"):
        raise SessionExpired("The portal login has expired. Run: vccflow login -a <account>")
    raise PortalError(f"The portal did not issue a token: {error}")


class PortalTokenProvider:
    """Same interface as auth.TokenProvider (account, session, token(scope)), backed by the portal login."""

    def __init__(self, account: Account, cookies: requests.Session | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.account = account
        self.cookies = cookies or load_session(account)
        self.session = requests.Session()  # for the API calls: no portal cookies sent there
        self.clock = clock
        self._settings: dict | None = None
        self._cache: dict[str, tuple[str, float]] = {}

    def _oidc(self) -> dict:
        if self._settings is None:
            resp = self.cookies.get(f"{self.account.portal_url}/portal", allow_redirects=False, timeout=60)
            if resp.status_code != 200:
                raise SessionExpired(f"The portal login for '{self.account.name}' has expired. "
                                     f"Run: vccflow login -a {self.account.name}")
            check_portal_account(self.account, extract_signed_in_account(resp.text))
            self._settings = extract_oidc_settings(resp.text)
        return self._settings

    def token(self, scope: str) -> str:
        cached = self._cache.get(scope)
        if cached and cached[1] > self.clock():
            return cached[0]
        oidc = self._oidc()
        wanted = PORTAL_SCOPE.get(scope, scope)
        if wanted not in oidc["scope"].split():
            raise PortalError(f"The portal login cannot get scope {scope}")
        params = {
            "client_id": oidc["client_id"],
            "redirect_uri": oidc["silent_redirect_uri"],
            "response_type": oidc["response_type"],
            "scope": f"openid {wanted}",
            "state": secrets.token_hex(16),
            "nonce": secrets.token_hex(16),
            "prompt": "none",
        }
        authorize = f"{oidc['authority'].rstrip('/')}/connect/authorize?{urlencode(params)}"
        resp = self.cookies.get(authorize, allow_redirects=False, timeout=60)
        if not resp.is_redirect:
            raise PortalError(f"Token request to the portal answered HTTP {resp.status_code}")
        token, expires_in = token_from_redirect(resp.headers.get("Location", ""))
        self._cache[scope] = (token, self.clock() + expires_in - EXPIRY_MARGIN)
        return token
