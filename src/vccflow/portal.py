"""Client for the Interaction Plans Architect backend on the VCC CallCentre portal.

These endpoints are undocumented: they are what the Architect web page calls,
found by recording the browser (see PROGRESS.md). They authenticate with the
portal's cookie session, captured by `vccflow login` (see portal_login.py).
"""

from __future__ import annotations

import base64
import html as html_lib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from vccflow.config import Account

SESSIONS_DIR = Path(".sessions")
INITIAL_STATE = re.compile(r"var\s+initialState\s*=\s*")
# Data Connector (DataRetriever) pages are an older MVC form, not the React editor.
DC_MODEL = re.compile(r"\.loadModel\(JSON\.parse\(htmlDecode\('(.*?)'\)\)\)", re.S)
DC_FORM = re.compile(r'<form\b[^>]*id="DataRetrieverForm".*?</form>', re.S)
TEXTAREA = r'<textarea\b[^>]*\bname="{name}"[^>]*>(.*?)</textarea>'
INPUT_VALUE = r'<input\b(?=[^>]*\bname="{name}")[^>]*\bvalue="([^"]*)"'


class PortalError(Exception):
    pass


class SessionExpired(PortalError):
    pass


def session_file(account: Account) -> Path:
    return SESSIONS_DIR / f"{account.name}.json"


def load_session(account: Account, path: Path | None = None) -> requests.Session:
    """Build a requests session from the cookies saved by `vccflow login`."""
    path = path or session_file(account)
    if not path.exists():
        raise SessionExpired(f"No portal session for '{account.name}'. Run: vccflow login -a {account.name}")
    state = json.loads(path.read_text(encoding="utf-8"))
    session = requests.Session()
    for c in state.get("cookies", []):
        session.cookies.set(c["name"], c["value"], domain=c["domain"], path=c.get("path", "/"))
    return session


def extract_initial_state(html: str) -> dict:
    """Parse the `var initialState = {...};` object the editor page embeds."""
    m = INITIAL_STATE.search(html)
    if not m:
        raise PortalError("Applet page has no initialState; the page format may have changed")
    state, _ = json.JSONDecoder().raw_decode(html, m.end())
    return state


def extract_data_connector(html: str) -> dict:
    """Parse a DataRetriever page into {"model": {...}, "fields": {...}}.

    The page model holds the dropdown choices (provider, action, next applets);
    the request definition itself is only in the form's textarea.
    """
    m = DC_MODEL.search(html)
    if not m:
        raise PortalError("Data Connector page has no loadModel(...); the page format may have changed")
    model = json.loads(html_lib.unescape(m.group(1).replace("\\'", "'")))
    form = DC_FORM.search(html)
    form_html = form.group(0) if form else ""
    fields = {}
    for name in ("ApiInteractionDefinition",):
        t = re.search(TEXTAREA.format(name=name), form_html, re.S)
        if t:
            fields[name] = html_lib.unescape(t.group(1)).strip()
    for name in ("SerialisedTemplatePlaceholders",):
        v = re.search(INPUT_VALUE.format(name=name), form_html)
        if v and v.group(1):
            fields[name] = html_lib.unescape(v.group(1))
    return {"model": model, "fields": fields}


def encode_properties(props: dict) -> str:
    return base64.b64encode(json.dumps(props, separators=(",", ":")).encode("utf-8")).decode("ascii")


class PortalClient:
    def __init__(self, account: Account, session: requests.Session | None = None):
        self.account = account
        self.base = f"{account.portal_url}/apps"
        self.session = session or load_session(account)

    # --- transport ---

    def _request(self, method: str, path: str, *, expect_redirect: bool = False, **kwargs) -> requests.Response:
        url = f"{self.base}/{path}"
        headers = {"Origin": self.account.portal_url.rsplit("/CallCentre", 1)[0], **kwargs.pop("headers", {})}
        resp = self.session.request(method, url, headers=headers, allow_redirects=False, timeout=60, **kwargs)
        location = resp.headers.get("Location", "")
        if resp.status_code in (401, 403) or (resp.is_redirect and "/Auth/" in location):
            raise SessionExpired(
                f"Portal session for '{self.account.name}' has expired. Run: vccflow login -a {self.account.name}"
            )
        if expect_redirect:
            if not resp.is_redirect:
                raise PortalError(f"{method} {path}: expected a redirect, got HTTP {resp.status_code} {resp.text[:300]}")
        elif resp.status_code != 200:
            raise PortalError(f"{method} {path} failed: HTTP {resp.status_code} {resp.text[:300]}")
        return resp

    def _json(self, path: str, body: dict) -> Any:
        resp = self._request("POST", path, json=body, headers={"X-Requested-With": "XMLHttpRequest"})
        try:
            return resp.json()
        except ValueError as e:
            raise PortalError(f"POST {path}: response is not JSON ({resp.text[:200]!r})") from e

    def _require_writes(self) -> None:
        if not self.account.allow_portal_writes:
            raise PortalError(
                f"Writes are disabled for '{self.account.name}'. "
                "Set allow_portal_writes = true for it in accounts.toml."
            )

    # --- reads ---

    def check_session(self) -> None:
        self._request("GET", "applications.asp")

    def list_applets(self, plan_id: str) -> dict:
        """Return {"id", "name", "applets": [{id, guid, name, appletType, configUrl}]}."""
        return self._json("Applet/GetAppletsForInteractionPlan", {"interactionPlanId": plan_id})

    def get_applet_state(self, plan_id: str, applet: dict) -> dict:
        """Return the editor page's initialState for an applet from list_applets()."""
        if not applet.get("configUrl"):
            raise PortalError(f"Applet '{applet.get('name')}' has no configUrl")
        return extract_initial_state(self._request("GET", applet["configUrl"]).text)

    def get_data_connector(self, applet: dict) -> dict:
        """Return extract_data_connector() for a Data Connector from list_applets()."""
        return extract_data_connector(self._request("GET", applet["configUrl"]).text)

    def get_media_info(self, media_id: int) -> dict:
        """Return {"id", "name", "url"} for an uploaded applet audio file."""
        return self._json("Applet/GetAppletMediaInfo", {"appletMediaId": media_id})["appletMediaInfo"]

    def download_media(self, url: str) -> bytes:
        """Download a media file by the site-relative url from get_media_info()."""
        host = self.account.portal_url.rsplit("/CallCentre", 1)[0]
        resp = self.session.get(host + url, allow_redirects=False, timeout=120)
        if resp.status_code != 200:
            raise PortalError(f"Downloading {url} failed: HTTP {resp.status_code}")
        return resp.content

    def find_applet(self, plan_id: str, name: str) -> dict:
        for a in self.list_applets(plan_id).get("applets", []):
            if a["name"] == name:
                return a
        raise PortalError(f"No applet named '{name}' in plan {plan_id}")

    # --- writes ---

    def create_applet(self, plan_id: str, applet_type: str, name: str) -> None:
        """Create an empty applet, e.g. applet_type='DataAnnouncer'."""
        self._require_writes()
        resp = self._request(
            "POST", "AppletCreate/Create", expect_redirect=True,
            data={"interactionPlanId": plan_id, "appletType": applet_type, "appletName": name, "Update": "Add"},
        )
        if "Applet" not in resp.headers.get("Location", ""):
            raise PortalError(f"Creating '{name}' did not open the applet editor: {resp.headers.get('Location')}")

    def save_applet(self, plan_id: str, name: str, properties: dict) -> dict:
        self._require_writes()
        return self._json(
            "Applet/Save",
            {"interactionPlanId": plan_id, "appletName": name, "appletProperties": encode_properties(properties)},
        )

    def delete_applet(self, plan_id: str, name: str) -> None:
        self._require_writes()
        self._request(
            "POST", f"appletdelete/delete?interactionPlanId={quote(plan_id)}",
            expect_redirect=True, data={name: "true"},
        )
