"""Calls to VCC Admin APIs on the API host (https://<region>.api.newvoicemedia.com) that accept
either the account's API credentials or the portal login's token, each with its own scopes.

API credentials often have only some scopes (e.g. JPlana admin:read only, Viessmann-test
admin:write only), while the portal login offers them all. So each call tries the API
credentials first, then the portal login, scope by scope, until one is allowed, and says so
when it falls back to the portal login.
"""

from __future__ import annotations

import sys
from typing import Callable

import requests

from vccflow.auth import AuthError, TokenProvider
from vccflow.config import Account
from vccflow.portal import PortalError, SessionExpired
from vccflow.portal_token import PortalTokenProvider

ACCEPT_V1 = "application/vnd.newvoicemedia.v1+json"


class ApiError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class ScopedApi:
    """One account's access to an admin API. `what` names it in messages ("schedules")."""

    error = ApiError
    what = "this API"

    def __init__(self, account: Account, session: requests.Session | None = None,
                 providers: list | None = None, log: Callable[[str], None] | None = None):
        self.account = account
        self.session = session or requests.Session()
        if providers is None:
            providers = ([TokenProvider(account)] if account.has_api_credentials else []) + [PortalTokenProvider(account)]
        self.providers = providers
        self.log = log or (lambda m: print(m, file=sys.stderr))
        self.used: str | None = None  # which login served the last call, for reporting

    def _call(self, method: str, url: str, scopes: tuple[str, ...], **kwargs) -> requests.Response:
        refused = []
        extra = kwargs.pop("headers", {})
        for provider in self.providers:
            label = "portal login" if isinstance(provider, PortalTokenProvider) else "API credentials"
            for scope in scopes:
                try:
                    token = provider.token(scope)
                except SessionExpired:
                    raise  # "The portal login ... has expired. Run: vccflow login ..." as it is
                except (AuthError, PortalError) as e:
                    refused.append(f"{label} {scope}: {str(e)[:80]}")
                    continue
                headers = {"Accept": ACCEPT_V1, "Authorization": f"Bearer {token}", **extra}
                resp = self.session.request(method, url, headers=headers, timeout=60, **kwargs)
                if resp.status_code in (401, 403):
                    refused.append(f"{label} {scope}: HTTP {resp.status_code} {resp.text[:80]}")
                    continue
                if resp.status_code not in (200, 201, 204):
                    raise self.error(f"{method} {url}: HTTP {resp.status_code} {resp.text[:300]}", resp.status_code)
                if self.used != label:
                    self.used = label
                    if label == "portal login":
                        reason = "the API credentials lack the scope" if len(self.providers) > 1 else "no API credentials"
                        self.log(f"{self.what.capitalize()} of '{self.account.name}': using the portal login ({reason}).")
                return resp
        raise self.error(f"No login of '{self.account.name}' may {method} {self.what}: " + "; ".join(refused))

    def _require_writes(self) -> None:
        if not self.account.allow_portal_writes:
            raise self.error(f"Writes are disabled for '{self.account.name}'. "
                             "Set allow_portal_writes = true for it in accounts.toml.")
