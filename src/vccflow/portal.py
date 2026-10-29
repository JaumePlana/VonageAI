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
MAX_MEDIA_BYTES = 10 * 1024 * 1024  # the Announcer editor's upload limit
INITIAL_STATE = re.compile(r"var\s+initialState\s*=\s*")
# Data Connector (DataRetriever) pages are an older MVC form, not the React editor.
DC_MODEL = re.compile(r"\.load\w*Model\(JSON\.parse\(htmlDecode\('(.*?)'\)\)\)", re.S)
DC_FORM = re.compile(r'<form\b[^>]*id="DataRetrieverForm".*?</form>', re.S)
TEXTAREA = r'<textarea\b[^>]*\bname="{name}"[^>]*>(.*?)</textarea>'
INPUT_VALUE = r'<input\b(?=[^>]*\bname="{name}")[^>]*\bvalue="([^"]*)"'
VALIDATION_ERRORS = re.compile(r'class="(?:validation-summary-errors|field-validation-error)"[^>]*>(.*?)</(?:div|span)>', re.S)
DC_NEXT_KEYS = ("NextApplet", "NextAppletFailure", "NextAppletInterfaceFailure", "NextAppletMultipleMatch")
# The plan page (applications.asp) lists every plan; the second cell of the plan's row
# links the initial applet, or says "WARNING initial applet not selected".
INITIAL_APPLET = r'<tr id="{plan_id}">\s*<td\b[^>]*>.*?</td>\s*<td\b[^>]*>(.*?)</td>'
LINK_TEXT = re.compile(r"<a\b[^>]*>(.*?)</a>", re.S)


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


def _compact(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def data_connector_form(settings: dict, nxt: dict) -> list[tuple[str, str]]:
    """Build the DataRetriever form post for a Data Connector, as the page sends it on "Update".

    settings are the exported page-model values (see export.py), nxt the NextApplet* names.
    Covers the cases recorded so far: Web Service calls, and Salesforce get (match by CLID
    or data source, raw SOQL, one or several records) and update.
    """
    provider, action = settings.get("DataProvider"), settings.get("Action")
    form = [("DataProvider", provider), ("Action", action)]
    if provider == "WebService":
        form += [
            ("SerialisedTemplatePlaceholders", settings.get("SerialisedTemplatePlaceholders") or "[]"),
            ("ApiInteractionDefinition", settings.get("ApiInteractionDefinition") or ""),
        ]
    elif provider == "Salesforce" and action == "GetSalesforceData":
        crm = settings.get("CrmRetrievalViewModel") or {}
        if crm.get("UseRawSoql"):
            form += [("UseRawSoql", "true"), ("CrmRetrievalViewModel.UseRawSoql", "false"), ("Soql", crm.get("Soql") or "")]
        else:
            match_by = crm.get("MatchBy") or "CLID"
            form += [("CrmRetrievalViewModel.UseRawSoql", "false"),
                     ("MatchObjectName", settings.get("MatchObjectName") or ""), ("MatchBy", match_by)]
            if match_by == "Data Source":
                form += [("MatchDataSource", crm.get("MatchDataSource") or ""), ("FieldToMatch", crm.get("FieldToMatch") or "")]
            elif match_by != "CLID":
                raise PortalError(f"Salesforce match by '{match_by}' has not been recorded yet; it cannot be saved")
            if crm.get("MaxRecords"):
                form += [("RetrieveMultiple", "on"), ("MaxRecords", str(crm["MaxRecords"]))]
            form += [("fieldToRetrieveSelectBox", ""), ("SerialisedResponseItems", _compact(settings.get("ResponseItems") or []))]
    elif provider == "Salesforce" and action == "UpdateSalesforceObject":
        params = (settings.get("CrmCreationViewModel") or {}).get("CrmObjectParameters") or []
        items = [{"crmField": p["CrmField"], "dataSourceType": p["DataSourceType"], "value": p["Value"]} for p in params]
        form += [("MatchObjectName", settings.get("MatchObjectName") or ""),
                 ("fieldToPopulateSelectBox", ""), ("SerialisedResponseItems", _compact(items))]
    else:
        raise PortalError(f"Data Connector {provider}/{action} has not been recorded yet; it cannot be saved")
    # The page always sends the three outcome fields, empty when nothing follows;
    # leaving them out is refused ("Please select a valid next applet").
    form += [(k, nxt.get(k) or "") for k in DC_NEXT_KEYS if k != "NextAppletMultipleMatch"]
    if nxt.get("NextAppletMultipleMatch"):
        form.append(("NextAppletMultipleMatch", nxt["NextAppletMultipleMatch"]))
    return form


def extract_initial_applet(html: str, plan_id: str) -> str | None:
    m = re.search(INITIAL_APPLET.format(plan_id=re.escape(plan_id)), html, re.S)
    if not m:
        raise PortalError(f"Plan {plan_id} not found on the plan page; the page format may have changed")
    link = LINK_TEXT.search(m.group(1))
    return html_lib.unescape(link.group(1)).strip() if link else None


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

    def get_initial_applet(self, plan_id: str) -> str | None:
        """Name of the applet the plan starts with, or None if it is not set."""
        html = self._request("GET", f"applications.asp?phonenum={quote(plan_id)}").text
        return extract_initial_applet(html, plan_id)

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
        # Redirects to the new applet's editor: Applet?... or, for a Data Connector, DataRetriever?...
        if "myapp=" not in resp.headers.get("Location", "").lower():
            raise PortalError(f"Creating '{name}' did not open the applet editor: {resp.headers.get('Location')}")

    def save_applet(self, plan_id: str, name: str, properties: dict) -> dict:
        self._require_writes()
        return self._json(
            "Applet/Save",
            {"interactionPlanId": plan_id, "appletName": name, "appletProperties": encode_properties(properties)},
        )

    def upload_media(self, plan_id: str, applet_name: str, path: Path, filename: str | None = None) -> int:
        """Upload an audio file for an applet, as the Announcer editor does. Return its appletMediaId."""
        self._require_writes()
        if path.stat().st_size > MAX_MEDIA_BYTES:
            raise PortalError(f"{path} is larger than 10 MB, which the portal refuses")
        with path.open("rb") as f:
            resp = self._request(
                "POST", "Applet/AppletMediaUpload", headers={"X-Requested-With": "XMLHttpRequest"},
                data={"interactionPlanId": plan_id, "appletName": applet_name},
                files={"file": (filename or path.name, f, "audio/wav")},
            )
        body = resp.json()
        if body.get("errorMessage") or not body.get("appletMediaId"):
            raise PortalError(f"Uploading {path.name} for '{applet_name}' failed: {body.get('errorMessage') or body}")
        return body["appletMediaId"]

    def save_data_connector(self, plan_id: str, name: str, settings: dict, nxt: dict) -> dict:
        """Save a Data Connector's settings, as clicking "Update" on its page does.

        Returns the saved page parsed by extract_data_connector(), for reading back.
        """
        self._require_writes()
        form = data_connector_form(settings, nxt)  # fails before any request if unsupported
        page_url = f"DataRetriever?phonenum={quote(plan_id)}&myapp={quote(name)}&application=DataRetriever"
        page = self._request("GET", page_url).text
        token = re.search(INPUT_VALUE.format(name="__RequestVerificationToken"), page)
        if not token:
            raise PortalError(f"Data Connector page for '{name}' has no form token")
        # The page loads the provider (and object) panels before saving; do the same.
        # Those two are AJAX-only actions: without X-Requested-With they answer 405.
        referer = {"Referer": f"{self.base}/{page_url}"}
        ajax = {**referer, "X-Requested-With": "XMLHttpRequest"}
        provider = settings["DataProvider"]
        self._request("POST", "DataRetriever/EditDataProvider", headers=ajax,
                      data={"phoneNumber": plan_id, "appletInstanceName": name, "dataProviderName": provider})
        if provider == "Salesforce" and settings.get("MatchObjectName"):
            self._request("POST", "DataRetriever/EditMatchObject", headers=ajax,
                          data={"dataProviderName": provider, "matchObject": settings["MatchObjectName"],
                                "action": settings["Action"]})
        resp = self._request("POST", f"DataRetriever?phoneNum={quote(plan_id)}&myApp={quote(name)}", headers=referer,
                             data=[("__RequestVerificationToken", html_lib.unescape(token.group(1))), *form])
        errors = [re.sub(r"<[^>]+>", " ", e).strip() for e in VALIDATION_ERRORS.findall(resp.text)]
        if any(errors):
            raise PortalError(f"Saving Data Connector '{name}' was refused: {'; '.join(e for e in errors if e)}")
        return extract_data_connector(resp.text)

    def set_initial_applet(self, plan_id: str, name: str) -> None:
        """Set the applet the plan starts with.

        It cannot be cleared: the portal ignores an empty value. It is cleared only
        when the applet itself is deleted.
        """
        if not name:
            raise PortalError("The initial applet cannot be cleared, only set to another applet")
        self._require_writes()
        self._request("POST", "InteractionArchitect/SetInitialApplet", expect_redirect=True,
                      data={"interactionPlanId": plan_id, "initialApplet": name, "Update": "Set"})

    def delete_applet(self, plan_id: str, name: str) -> None:
        self._require_writes()
        self._request(
            "POST", f"appletdelete/delete?interactionPlanId={quote(plan_id)}",
            expect_redirect=True, data={name: "true"},
        )
