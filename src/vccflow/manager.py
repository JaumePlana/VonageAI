"""Read-only client for the API behind VCC Admin's Interaction Manager.

Found by recording the Interaction Manager page (exports/HAR/interactionManager.har).
It lives on https://<region>.api.newvoicemedia.com and takes the same client-credentials
token as the public IPM API, but returns more: archived plans, reporting group, initial
applet, interaction properties; full mapping rules; and the destinations (addresses) of
each channel. It also answers on accounts where the public IPM API is not enabled.

Scopes: plans and mappings need interaction-plans:read; destinations need
addresses:read, which must be granted to the API credentials in VCC Admin.
"""

from __future__ import annotations

from typing import Any

import requests

from vccflow.auth import TokenProvider

ACCEPT_V1 = "application/vnd.newvoicemedia.v1+json"
PLANS_SCOPE = "interaction-plans:read"
ADDRESSES_SCOPE = "addresses:read"
PLANS_PATH = "interaction-plan/plans"
MAPPINGS_PATH = "interaction-plan/mappings"
# Channels the Interaction Manager lists destinations for (address/<channel>).
CHANNELS = ("pstn", "api", "webchat", "sms", "whatsapp", "email")


class ManagerError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class ManagerClient:
    def __init__(self, tokens: TokenProvider, session: requests.Session | None = None):
        self.tokens = tokens
        self.base_url = tokens.account.manager_api_url
        self.session = session or tokens.session

    def get(self, path: str, scope: str = PLANS_SCOPE) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        resp = self.session.get(
            url,
            headers={"Accept": ACCEPT_V1, "Authorization": f"Bearer {self.tokens.token(scope)}"},
            timeout=60,
        )
        if resp.status_code != 200:
            raise ManagerError(f"GET {url} failed: HTTP {resp.status_code} {resp.text[:500]}", resp.status_code)
        return resp.json()

    def list_plans(self, include_archived: bool = False) -> list[dict]:
        """Plans as {id, name, reportingGroup, initialApplet, archived, ...}."""
        plans = self.get(PLANS_PATH)
        return plans if include_archived else [p for p in plans if not p.get("archived")]

    def list_mappings(self) -> list[dict]:
        """Mappings as {id, name, interactionPlan (plan ID), priority, rules, interactionProperties, ...}."""
        return self.get(MAPPINGS_PATH)

    def list_destinations(self, channels: tuple[str, ...] = CHANNELS) -> dict[str, list[dict] | None]:
        """Destinations per channel, e.g. {"pstn": [{address, name, active, ...}], ...}.

        A channel the account does not have (403 "Feature not enabled") maps to None.
        """
        out: dict[str, list[dict] | None] = {}
        for c in channels:
            try:
                out[c] = self.get(f"address/{c}", scope=ADDRESSES_SCOPE)
            except ManagerError as e:
                if e.status != 403 or "Feature not enabled" not in str(e):
                    raise
                out[c] = None
        return out
