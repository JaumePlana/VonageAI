"""Read-only client for the VCC Media Channels API: the destinations (addresses) of each channel.

Documented by Vonage ("Media Channels API"): https://<region>.api.cc.vonage.com/media-channels,
Accept application/vnd.newvoicemedia.v2+json, scope admin:read for GET. It takes a
client-credentials token, so destinations need no portal login.
"""

from __future__ import annotations

from typing import Any

import requests

from vccflow.auth import TokenProvider

ACCEPT_V2 = "application/vnd.newvoicemedia.v2+json"
READ_SCOPE = "admin:read"
CHANNELS = ("pstn", "api", "webchat", "sms", "whatsapp", "email")


class MediaChannelsError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class MediaChannelsClient:
    def __init__(self, tokens: TokenProvider, session: requests.Session | None = None):
        self.tokens = tokens
        self.base_url = tokens.account.media_channels_url
        self.session = session or tokens.session

    def get(self, path: str) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        resp = self.session.get(
            url,
            headers={"Accept": ACCEPT_V2, "Authorization": f"Bearer {self.tokens.token(READ_SCOPE)}"},
            timeout=60,
        )
        if resp.status_code != 200:
            raise MediaChannelsError(f"GET {url} failed: HTTP {resp.status_code} {resp.text[:500]}", resp.status_code)
        return resp.json()

    def list_destinations(self, channels: tuple[str, ...] = CHANNELS) -> dict[str, list[dict] | None]:
        """Destinations per channel: {"pstn": [{id, address: {name, description, dataSources, ...},
        profileId, mapping, modifiedTime, modifiedBy}], ...}. A channel the account does not
        have (403 "Feature not enabled") maps to None.
        """
        out: dict[str, list[dict] | None] = {}
        for c in channels:
            try:
                out[c] = self.get(f"{c}/addresses").get("addresses", [])
            except MediaChannelsError as e:
                if e.status != 403 or "not enabled" not in str(e).lower():
                    raise
                out[c] = None
        return out
