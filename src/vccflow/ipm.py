"""Read-only client for the VCC Interaction Plan Manager (IPM) API."""

from __future__ import annotations

from typing import Any

import requests

from vccflow.auth import TokenProvider

READ_SCOPE = "interaction-plans:read"
ACCEPT_V1 = "application/vnd.newvoicemedia.v1+json"

# Both paths confirmed against a live account (paths are case-insensitive).
PLANS_PATH = "interactionplans"
MAPPINGS_PATH = "mappings"


class IpmError(Exception):
    pass


class IpmClient:
    def __init__(self, tokens: TokenProvider, session: requests.Session | None = None):
        self.tokens = tokens
        self.base_url = tokens.account.ipm_base_url
        self.session = session or tokens.session

    def get(self, path: str) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        resp = self.session.get(
            url,
            headers={
                "Accept": ACCEPT_V1,
                "Authorization": f"Bearer {self.tokens.token(READ_SCOPE)}",
            },
            timeout=60,
        )
        if resp.status_code != 200:
            raise IpmError(f"GET {url} failed: HTTP {resp.status_code} {resp.text[:500]}")
        return resp.json()

    def list_plans(self) -> Any:
        return self.get(PLANS_PATH)

    def get_plan(self, plan_id: str) -> Any:
        return self.get(f"{PLANS_PATH}/{plan_id}")

    def list_mappings(self) -> Any:
        return self.get(MAPPINGS_PATH)
