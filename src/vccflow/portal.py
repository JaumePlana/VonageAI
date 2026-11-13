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
from html.parser import HTMLParser
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
# Data Router (DataRouter page, an MVC form): rule data types, all recorded (dataRouter*.har).
# 3 is True/False ("equals" only; its value is RightHandFixedBooleanValue).
DR_RECORDED_TYPES = {0: "text", 1: "number", 2: "decimal", 3: "true/false", 4: "date"}
DR_BOOLEAN = 3
# The plan page (applications.asp) lists every plan; the second cell of the plan's row
# links the initial applet, or says "WARNING initial applet not selected".
INITIAL_APPLET = r'<tr id="{plan_id}">\s*<td\b[^>]*>.*?</td>\s*<td\b[^>]*>(.*?)</td>'
# Fields on applet editor pages that are tokens, the plan's own ID or page navigation.
FORM_NOISE = {
    "__RequestVerificationToken", "__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION",
    "__EVENTTARGET", "__EVENTARGUMENT", "dialnbr", "phonenum", "interactionPlanId", "interactionplanId",
    "action", "application", "myapp", "name", "index", "notlogin", "validation", "addnewapplet", "hasapps",
    "Acd.DialledNumber", "Acd.ApplicationName",
}
PAGE_MODEL = re.compile(r"JSON\.parse\(htmlDecode\('(.*?)'\)\)", re.S)
MODEL_LOOKUPS = ("All", "Possible", "Formatted", "QueryKeys", "DataTypeOperators", "UnsavedChanges", "Show")
SIGNED_IN_USER = re.compile(r"var\s+user\s*=\s*\{(.*?)\};", re.S)
SIGNED_IN_ACCOUNT = re.compile(r"\baccount\s*:\s*'([^']*)'")
SETTINGS_PAGE = "../userAdmin/system-settings.aspx"
CALLBACK_SELECT, CALLBACK_NAME, CALLBACK_FILE, CALLBACK_BUTTON = (
    "SelectedClidNumber", "SelectedClidName", "ClidUpload", "UpdateClidName")
CALLBACK_RENAMED = "The presented name for the callback number selected has been updated"
# Page state, the two callback fields, and a list that changes selection per request.
SETTINGS_NOT_COMPARED = {
    "__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION", "AccountNumbers", "Pin",
    CALLBACK_SELECT, CALLBACK_NAME, CALLBACK_FILE,
}
LINK_TEXT = re.compile(r"<a\b[^>]*>(.*?)</a>", re.S)
# First cell of each plan row: "<name><br> Interaction plan type: <type> ...".
PLAN_ROW = re.compile(r'<tr id="([^"]+)">\s*<td\b[^>]*>\s*(.*?)<br>\s*Interaction plan type:\s*([^<]*?)\s*<', re.S)


class PortalError(Exception):
    pass


class SessionExpired(PortalError):
    pass


class WrongAccount(PortalError):
    """The portal login is signed in to another VCC account than the one configured."""


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
    Covers the cases recorded so far: Web Service calls, JavaScript (dataRouter4.har), and
    Salesforce get (match by CLID or data source, raw SOQL, one or several records) and update.
    """
    provider, action = settings.get("DataProvider"), settings.get("Action")
    form = [("DataProvider", provider), ("Action", action)]
    if provider == "WebService":
        form += [
            ("SerialisedTemplatePlaceholders", settings.get("SerialisedTemplatePlaceholders") or "[]"),
            ("ApiInteractionDefinition", settings.get("ApiInteractionDefinition") or ""),
        ]
    elif provider == "JavaScript" and action == "CallWebService":
        form += [("ApiInteractionDefinition", settings.get("ApiInteractionDefinition") or "")]
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


def extract_data_router(html: str) -> dict:
    """Parse a Data Router page into {"rules": [...], "default": name or None, "operators": {...}}.

    The page model (Nvm.DataRouter.load) holds the rules in order, each routing to an applet
    by name, the default applet ("If rule does not match, route to") and the operators each
    data type offers.
    """
    for raw in PAGE_MODEL.findall(html):
        model = json.loads(html_lib.unescape(raw.replace("\\'", "'")))
        if isinstance(model, dict) and "RoutingRules" in model:
            return {
                "rules": model.get("RoutingRules") or [],
                "default": model.get("DefaultAppletInstanceName") or None,
                "operators": {int(k): [o["Value"] for o in v] for k, v in (model.get("DataTypeOperators") or {}).items()},
            }
    raise PortalError("Data Router page has no RoutingRules model; the page format may have changed")


def data_router_form(settings: dict, nxt: dict) -> list[tuple[str, str]]:
    """Build the DataRouter form post, as the page sends it on save (dataRouter.har).

    settings["RoutingRules"] are the rules without their applet; nxt holds "RoutingRules.<i>"
    and "DefaultApplet" by name. Only the recorded cases are built: text, number and date
    rules with any operator, and True/False rules. A rule or default with no applet in nxt is posted with
    an empty destination, so the rule is kept (user's decision; the portal saves it:
    dataRouter2.har). See data_router_missing_targets.
    """
    rules = settings.get("RoutingRules") or []
    if not rules:
        raise PortalError("a Data Router without rules cannot be saved: VCC needs a data source in the first rule "
                          "(import-plan creates it empty instead)")
    form: list[tuple[str, str]] = []
    for i, rule in enumerate(rules):
        data_type, operator = rule.get("DataType"), rule.get("OperatorType")
        if data_type not in DR_RECORDED_TYPES:
            raise PortalError(f"Data Router rule {i + 1} (RoutingRules.{i}): data type {data_type} has not been recorded yet "
                              f"(recorded: {', '.join(f'{k} {v}' for k, v in DR_RECORDED_TYPES.items())}); it cannot be saved")
        if not rule.get("LeftHandDataSource"):
            raise PortalError(f"Data Router rule {i + 1} (RoutingRules.{i}) has no data source; it cannot be saved")
        p = f"[rule{i}]"
        form += [
            (f"{p}.LeftHandDataSource", rule["LeftHandDataSource"]),
            ("index", f"rule{i}"),
            (f"{p}.DataType", str(data_type)),
            (f"{p}.OperatorType", operator or ""),
            # Always posted, even when unused: "is null"/"is not null" keep an old value (dataRouter4.har).
            (f"{p}.RightHandFixedValue", rule.get("RightHandFixedValue") or ""),
        ]
        if data_type == DR_BOOLEAN:
            form.append((f"{p}.RightHandFixedBooleanValue", rule.get("RightHandFixedBooleanValue") or ""))
        form.append((f"{p}.RouteToApplet", nxt.get(f"RoutingRules.{i}") or ""))
    form.append(("defaultApplet", nxt.get("DefaultApplet") or ""))
    return form


def data_router_missing_targets(settings: dict, nxt: dict) -> list[str]:
    """The rules (and default) of a Data Router that route to no applet, e.g. "rule 3 (RoutingRules.2)"."""
    missing = [f"rule {i + 1} (RoutingRules.{i})" for i in range(len(settings.get("RoutingRules") or []))
               if not nxt.get(f"RoutingRules.{i}")]
    if not nxt.get("DefaultApplet"):
        missing.append("default applet")
    return missing


def extract_initial_applet(html: str, plan_id: str) -> str | None:
    m = re.search(INITIAL_APPLET.format(plan_id=re.escape(plan_id)), html, re.S)
    if not m:
        raise PortalError(f"Plan {plan_id} not found on the plan page; the page format may have changed")
    link = LINK_TEXT.search(m.group(1))
    return html_lib.unescape(link.group(1)).strip() if link else None


def extract_plans(html: str) -> list[dict]:
    """Parse the plan page (applications.asp) into one entry per plan.

    Plan IDs are GUIDs, or older account-style IDs (e.g. "NVMDemo29-4", "3238080946").
    """
    plans = []
    for plan_id, name, plan_type in PLAN_ROW.findall(html):
        plans.append({
            "interactionPlanId": plan_id,
            "interactionPlanName": html_lib.unescape(re.sub(r"\s+", " ", name)).strip(),
            "type": plan_type.strip(),
            "initialApplet": extract_initial_applet(html, plan_id),
        })
    return plans


class _FormFields(HTMLParser):
    """Collect what a browser would submit from a page's inputs, selects and textareas."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.fields: dict[str, list[str]] = {}
        self._select: str | None = None
        self._textarea: str | None = None
        self._text: list[str] = []

    def _add(self, name: str | None, value: str) -> None:
        if name and name not in FORM_NOISE:
            self.fields.setdefault(name, []).append(value)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "input":
            kind = (a.get("type") or "text").lower()
            if kind in ("submit", "button", "image", "reset", "file"):
                return
            if kind in ("checkbox", "radio") and "checked" not in a:
                return
            self._add(a.get("name"), a.get("value") or "")
        elif tag == "select":
            self._select = a.get("name")
        elif tag == "option" and self._select and "selected" in a:
            self._add(self._select, a.get("value") or "")
        elif tag == "textarea":
            self._textarea, self._text = a.get("name"), []

    def handle_endtag(self, tag):
        if tag == "select":
            self._select = None
        elif tag == "textarea" and self._textarea:
            self._add(self._textarea, "".join(self._text).strip())
            self._textarea = None

    def handle_data(self, data):
        if self._textarea:
            self._text.append(data)


def extract_form_values(html: str) -> dict:
    """Settings of an applet editor page: its form fields plus its embedded JSON model.

    Older pages fill their form from a model with script (Skill Tagger skills, Data Router
    rules, next applets), so the fields alone miss them. Tokens, account-specific IDs and
    lookup lists are left out.
    """
    parser = _FormFields()
    parser.feed(html)
    values: dict[str, Any] = {k: v[0] if len(v) == 1 else v for k, v in parser.fields.items()}
    for raw in PAGE_MODEL.findall(html):
        model = json.loads(html_lib.unescape(raw.replace("\\'", "'")))
        if not isinstance(model, dict) or (len(model) > 20 and all(isinstance(v, str) for v in model.values())):
            continue  # a bare lookup list, e.g. the account's data sources
        values.update({k: v for k, v in model.items() if not k.startswith(MODEL_LOOKUPS)})
    return values


def extract_signed_in_account(html: str) -> str:
    """Name of the VCC account a portal page is signed in to (var user = {account: '...'})."""
    user = SIGNED_IN_USER.search(html)
    name = SIGNED_IN_ACCOUNT.search(user.group(1)) if user else None
    if not name:
        raise PortalError("Cannot tell which account the portal is signed in to; the page format may have changed")
    return html_lib.unescape(name.group(1))


def remove_session(account: Account) -> bool:
    """Delete the saved login. Return True if there was one.

    It does not log out of VCC: /Auth/logout ended the user's other VCC sessions too
    (other saved logins, and possibly the browser). The login stays valid on VCC until
    it expires, but vccflow can no longer use it.
    """
    path = session_file(account)
    existed = path.exists()
    path.unlink(missing_ok=True)
    return existed


def check_portal_account(account: Account, signed_in: str) -> None:
    """Refuse a login that is not signed in to the configured portal_account, and remove it."""
    if signed_in.casefold() == (account.portal_account or "").casefold():
        return
    remove_session(account)
    if account.portal_account:
        raise WrongAccount(
            f"The portal login for '{account.name}' is signed in to '{signed_in}', "
            f"but portal_account is '{account.portal_account}'. The saved login was removed. "
            f"Run: vccflow login -a {account.name} and sign in to '{account.portal_account}'"
        )
    raise WrongAccount(
        f"The portal login for '{account.name}' is signed in to '{signed_in}', and the account has no portal_account. "
        f"The saved login was removed. If '{signed_in}' is the right account, add this to "
        f"[accounts.{account.name}] in accounts.toml and log in again:\nportal_account = \"{signed_in}\""
    )


class _BrowserForm(HTMLParser):
    """Collect a page's form exactly as a browser would submit it, without pressing any button."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.fields: list[list] = []  # [name, value], in page order
        self.options: dict[str, list[tuple[str, str]]] = {}  # select name -> [(value, text)]
        self._select: list | None = None
        self._option: list | None = None
        self._has_selected = False
        self._textarea: list | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        name = a.get("name")
        if tag == "input" and name and "disabled" not in a:
            kind = (a.get("type") or "text").lower()
            if kind in ("submit", "button", "image", "reset") or (kind in ("checkbox", "radio") and "checked" not in a):
                return
            self.fields.append([name, "" if kind == "file" else a.get("value") or ""])
        elif tag == "select" and name and "disabled" not in a:
            self._select, self._has_selected = [name, None], False
            self.fields.append(self._select)
            self.options[name] = []
        elif tag == "option" and self._select:
            value = a.get("value")
            self._option = [value, ""]
            self.options[self._select[0]].append(self._option)
            # A select with nothing marked submits its first option.
            if "selected" in a or self._select[1] is None:
                self._select[1] = value
        elif tag == "textarea" and name:
            self._textarea = [name, ""]
            self.fields.append(self._textarea)

    def handle_endtag(self, tag):
        if tag == "select":
            self._select = self._option = None
        elif tag == "option":
            self._option = None
        elif tag == "textarea":
            self._textarea = None

    def handle_data(self, data):
        if self._option:
            self._option[1] += data
        elif self._textarea:
            self._textarea[1] += data


def extract_callback_numbers(html: str) -> list[dict]:
    """Callback numbers from the settings page: the dropdown shows "name - number".

    The name can hold a hyphen too, so the last one separates them, as the page's own script does.
    """
    parser = _BrowserForm()
    parser.feed(html)
    options = parser.options.get(CALLBACK_SELECT)
    if options is None:
        raise PortalError("The settings page has no callback number list; the page format may have changed")
    numbers = []
    for value, text in options:
        name, _, number = html_lib.unescape(text).strip().rpartition("-")
        numbers.append({"id": value, "name": name.strip(), "number": number.strip()})
    return numbers


def digits(number: Any) -> str:
    return re.sub(r"\D", "", str(number or ""))


def callback_rename_form(html: str, number_id: str, new_name: str) -> list[tuple]:
    """The multipart post for the page's "Update" button of a callback number.

    The page posts all its settings with any button, so everything but the number and
    its name stays exactly as the page shows it.
    """
    parser = _BrowserForm()
    parser.feed(html)
    form = []
    for name, value in parser.fields:
        if name == CALLBACK_SELECT:
            value = number_id
        elif name == CALLBACK_NAME:
            value = new_name
        form.append((name, value))
        if name == CALLBACK_NAME:
            form.append((CALLBACK_BUTTON, "Update"))  # the pressed button, in its page position
    names = {n for n, _ in form}
    if not {CALLBACK_SELECT, CALLBACK_NAME, CALLBACK_FILE} <= names:
        raise PortalError("The settings page has no callback number form; the page format may have changed")
    return form


def settings_snapshot(html: str) -> dict[str, Any]:
    """The page's settings, to compare before and after a change."""
    parser = _BrowserForm()
    parser.feed(html)
    return {n: v for n, v in parser.fields if n not in SETTINGS_NOT_COMPARED}


def encode_properties(props: dict) -> str:
    return base64.b64encode(json.dumps(props, separators=(",", ":")).encode("utf-8")).decode("ascii")


class PortalClient:
    def __init__(self, account: Account, session: requests.Session | None = None):
        self.account = account
        self.base = f"{account.portal_url}/apps"
        self.session = session or load_session(account)
        self._account_checked = False

    # --- transport ---

    def signed_in_account(self) -> str:
        """Name of the VCC account this login is signed in to, from the portal page."""
        resp = self.session.get(f"{self.account.portal_url}/portal", allow_redirects=False, timeout=60)
        if resp.status_code != 200:
            raise SessionExpired(
                f"Portal session for '{self.account.name}' has expired. Run: vccflow login -a {self.account.name}"
            )
        return extract_signed_in_account(resp.text)

    def _check_account(self) -> None:
        """Before the first call: make sure the login belongs to the configured account."""
        if self._account_checked:
            return
        self._account_checked = True
        check_portal_account(self.account, self.signed_in_account())

    def _request(self, method: str, path: str, *, expect_redirect: bool | None = False, **kwargs) -> requests.Response:
        """expect_redirect: True needs a redirect, False needs HTTP 200, None accepts either."""
        self._check_account()
        url = f"{self.base}/{path}"
        headers = {"Origin": self.account.portal_url.rsplit("/CallCentre", 1)[0], **kwargs.pop("headers", {})}
        resp = self.session.request(method, url, headers=headers, allow_redirects=False, timeout=60, **kwargs)
        location = resp.headers.get("Location", "")
        if resp.status_code in (401, 403) or (resp.is_redirect and "/Auth/" in location):
            raise SessionExpired(
                f"Portal session for '{self.account.name}' has expired. Run: vccflow login -a {self.account.name}"
            )
        if expect_redirect is None:
            if not resp.is_redirect and resp.status_code != 200:
                raise PortalError(f"{method} {path} failed: HTTP {resp.status_code} {resp.text[:300]}")
        elif expect_redirect:
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

    def get_data_router(self, applet: dict) -> dict:
        """Return extract_data_router() for a Data Router from list_applets()."""
        return extract_data_router(self._request("GET", applet["configUrl"]).text)

    def get_applet_form(self, applet: dict) -> dict:
        """Editor page fields of an applet from list_applets(), for types without a parsed format."""
        return extract_form_values(self._request("GET", applet["configUrl"]).text)

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

    def list_plans(self) -> list[dict]:
        """All plans of the account, read from the Architect's plan page (no API needed)."""
        return extract_plans(self._request("GET", "applications.asp").text)

    def list_callback_numbers(self) -> list[dict]:
        """The account's callback numbers as {id, name, number}, read from the settings page."""
        return extract_callback_numbers(self._request("GET", SETTINGS_PAGE).text)

    def get_initial_applet(self, plan_id: str) -> str | None:
        """Name of the applet the plan starts with, or None if it is not set."""
        html = self._request("GET", f"applications.asp?phonenum={quote(plan_id)}").text
        return extract_initial_applet(html, plan_id)

    def list_data_sources(self, plan_id: str | None = None) -> set[str] | None:
        """The account's data source names, as the applet editors list them.

        Every Announcer, Set Data Source and Data Connector page carries the account's
        list (metabase entries). It is read from the first such applet found, in plan_id
        first, then in the account's other plans. None if the account has no such applet.
        """
        plan_ids = [plan_id] if plan_id else []
        plan_ids += [p["interactionPlanId"] for p in self.list_plans() if p["interactionPlanId"] != plan_id]
        for pid in plan_ids:
            for applet in self.list_applets(pid).get("applets", []):
                url = applet.get("configUrl") or ""
                try:
                    if url.startswith("DataRetriever"):
                        entries = self.get_data_connector(applet)["model"].get("MetabaseEntries")
                    elif url.startswith("Applet?"):
                        entries = (self.get_applet_state(pid, applet).get("dataSourceKeys") or {}).get("metabaseEntries")
                    else:
                        continue
                except PortalError:
                    continue
                if entries is not None:
                    return set(entries)
        return None

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

    def save_data_router(self, plan_id: str, name: str, settings: dict, nxt: dict) -> dict:
        """Save a Data Router's rules and default applet, as its page's save does.

        The page posts a plain form (no form token) and redirects with appletSaved=True.
        Returns the saved page parsed by extract_data_router(), for reading back.
        """
        self._require_writes()
        form = data_router_form(settings, nxt)  # fails before any request if unsupported
        page_url = f"DataRouter?phoneNum={quote(plan_id)}&myApp={quote(name)}"
        resp = self._request("POST", page_url, expect_redirect=None,
                             headers={"Referer": f"{self.base}/{page_url}"}, data=form)
        if not resp.is_redirect:
            # Refused: the page comes back with its validation summary, e.g. "Rule 1: Please select
            # a Data Source from the list" when the rule's data source does not exist in the account.
            errors = [re.sub(r"<[^>]+>|\s+", " ", e).strip() for e in VALIDATION_ERRORS.findall(resp.text)]
            reason = "; ".join(e for e in errors if e) or "no reason shown on the page"
            raise PortalError(f"Saving Data Router '{name}' was refused: {reason}")
        location = resp.headers.get("Location", "")
        if "appletsaved=true" not in location.lower():
            raise PortalError(f"Saving Data Router '{name}' was not confirmed: redirected to {location}")
        return extract_data_router(self._request("GET", f"{page_url}&appletSaved=True").text)

    def rename_callback_number(self, which: str, new_name: str) -> dict:
        """Rename a callback number, given its number, ID or current name. Return it as read back.

        Checks afterwards that the name changed and that no other setting on the page moved.
        """
        self._require_writes()
        if not new_name or len(new_name) > 50:
            raise PortalError("A callback number's name must be 1 to 50 characters")
        page = self._request("GET", SETTINGS_PAGE).text
        numbers = extract_callback_numbers(page)
        matches = [n for n in numbers if which in (n["id"], n["name"]) or (digits(which) and digits(which) == digits(n["number"]))]
        if len(matches) != 1:
            raise PortalError(f"Expected one callback number matching '{which}', found {len(matches)}")
        before = settings_snapshot(page)
        fields = callback_rename_form(page, matches[0]["id"], new_name)
        files = [(n, ("", b"", "application/octet-stream") if n == CALLBACK_FILE else (None, v)) for n, v in fields]
        resp = self._request("POST", SETTINGS_PAGE, files=files)
        if CALLBACK_RENAMED not in resp.text:
            raise PortalError(f"The portal did not confirm renaming {matches[0]['number']}; check the settings page")
        after_page = self._request("GET", SETTINGS_PAGE).text
        moved = {k for k in before.keys() | settings_snapshot(after_page).keys()
                 if before.get(k) != settings_snapshot(after_page).get(k)}
        if moved:
            raise PortalError(f"The rename changed other settings on the page, check them now: {', '.join(sorted(moved))}")
        saved = next(n for n in extract_callback_numbers(after_page) if n["id"] == matches[0]["id"])
        if saved["name"] != new_name:
            raise PortalError(f"{saved['number']} is still named '{saved['name']}' after the rename")
        return saved

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
