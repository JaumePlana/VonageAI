"""Client-credentials bearer tokens for the VCC Authentication API, cached per scope."""

from __future__ import annotations

import time
from typing import Callable

import requests

from vccflow.config import Account

# Refresh this many seconds before the token actually expires.
EXPIRY_MARGIN = 60


class AuthError(Exception):
    pass


class TokenProvider:
    def __init__(
        self,
        account: Account,
        session: requests.Session | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.account = account
        self.session = session or requests.Session()
        self.clock = clock
        self._cache: dict[str, tuple[str, float]] = {}

    def token(self, scope: str) -> str:
        cached = self._cache.get(scope)
        if cached and cached[1] > self.clock():
            return cached[0]
        if not self.account.has_api_credentials:
            raise AuthError(
                f"Account '{self.account.name}' has no API credentials "
                "(client_id_env/client_secret_env in accounts.toml)"
            )

        resp = self.session.post(
            self.account.token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self.account.client_id,
                "client_secret": self.account.client_secret,
                "scope": scope,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        if resp.status_code != 200:
            raise AuthError(
                f"Token request for '{self.account.name}' (scope {scope}) failed: "
                f"HTTP {resp.status_code} {resp.text[:300]}"
            )
        body = resp.json()
        expires_in = int(body.get("expires_in", 900))
        token = body["access_token"]
        self._cache[scope] = (token, self.clock() + expires_in - EXPIRY_MARGIN)
        return token
