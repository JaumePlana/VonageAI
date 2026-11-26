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
from typing import Any, Callable
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
# Skill Tagger (SkillPersonalQueueTagger page, an MVC form; skillTagger.har).
SKILL_NECESSITY = {1: "optional", 2: "mandatory", 3: "mandatory for a time"}
SKILL_TIME_KEYS = ("RequiredDays", "RequiredHours", "RequiredMinutes", "RequiredSeconds")
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
# Dropdown values that identify something in one account (a plan, presence state...): a GUID or a long number.
# The editor page fields keep such an option by its text, which names the same thing in every account.
ACCOUNT_ID_VALUE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|\d{6,}", re.I)
PAGE_MODEL = re.compile(r"JSON\.parse\(htmlDecode\('(.*?)'\)\)", re.S)
MODEL_LOOKUPS = ("All", "Possible", "Formatted", "QueryKeys", "DataTypeOperators", "UnsavedChanges", "Show")
SIGNED_IN_USER = re.compile(r"var\s+user\s*=\s*\{(.*?)\};", re.S)
SIGNED_IN_ACCOUNT = re.compile(r"\baccount\s*:\s*'([^']*)'")
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
    Salesforce get (match by CLID or data source, raw SOQL, one or several records), update and
    create (field values from data sources; IVR Slots are refused).
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
    elif provider == "Salesforce" and action in ("UpdateSalesforceObject", "CreateSalesforceObject"):
        # Update and create post the same fields (dataconnector.har, dataconnector_createSFRecord.har):
        # the object, and each field with the data source its value comes from.
        if not settings.get("MatchObjectName"):
            raise PortalError("a Salesforce update or create needs an object (MatchObjectName); it cannot be saved")
        params = (settings.get("CrmCreationViewModel") or {}).get("CrmObjectParameters") or []
        slots = [p.get("CrmField") for p in params if p.get("DataSourceType") != 0]
        if slots:
            raise PortalError(f"Salesforce fields set from an IVR Slot are not supported: {', '.join(map(str, slots))}; "
                              "only values from data sources can be saved")
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


def extract_skill_tagger(html: str) -> dict:
    """The Skill Tagger page model (SkillPersonalQueueTagger page): skills, dynamic skills,
    personal queue data sources, NextApplet, and the account's lookups (AllSkills, AllMetabaseEntries)."""
    for raw in PAGE_MODEL.findall(html):
        model = json.loads(html_lib.unescape(raw.replace("\\'", "'")))
        if isinstance(model, dict) and "Skills" in model and "DynamicSkills" in model:
            return model
    raise PortalError("Skill Tagger page has no Skills model; the page format may have changed")


def _skill_requirement(skill: dict, where: str) -> dict:
    """A skill requirement as the page posts it; Necessity 1 optional, 2 mandatory, 3 mandatory for a time."""
    necessity = skill.get("Necessity")
    if necessity not in SKILL_NECESSITY:
        raise PortalError(f"{where}: necessity {necessity!r} is unknown (recorded: "
                          f"{', '.join(f'{k} {v}' for k, v in SKILL_NECESSITY.items())}); it cannot be saved")
    if not skill.get("Name"):
        raise PortalError(f"{where} has no name; it cannot be saved")
    return {"Name": skill["Name"], "Necessity": necessity,
            **{k: int(skill.get(k) or 0) for k in SKILL_TIME_KEYS}}


def skill_description(skill: dict) -> str:
    """The text the page posts with a dynamic skill (Nvm.SkillTagger.SkillDescription in
    SkillPersonalQueueTagger.js): " is optional", " is mandatory", " is mandatory for 1 day 8 minutes "."""
    if skill["Necessity"] == 1:
        return " is optional"
    if skill["Necessity"] == 2:
        return " is mandatory"
    units = zip((skill.get(k) or 0 for k in SKILL_TIME_KEYS), ("day", "hour", "minute", "second"))
    return " is mandatory for " + "".join(f"{n} {unit}{'' if n == 1 else 's'} " for n, unit in units if n > 0)


def skill_tagger_form(settings: dict, nxt: dict, friendly_names: dict | None = None) -> list[tuple[str, str]]:
    """Build the Skill Tagger form post, as its page sends it on save (skillTagger.har).

    Static skills are posted by name (Skills[i].*), dynamic skills (a data source's value is the
    skill) as SerialisedDynamicSkills JSON with the data source's display name (friendly_names,
    the page's AllMetabaseEntries). The logged-in and logged-out timeouts are posted only with
    an agent ID data source: without one the page does not offer them.
    """
    if settings.get("InterruptAgentWhilstAway"):
        raise PortalError("'Interrupt agent whilst away' on has not been recorded yet; it cannot be saved")
    agent = settings.get("AgentIdDataSource") or ""
    timeouts = [settings.get(k) or "" for k in ("LoggedInTimeoutDataSource", "LoggedOutTimeoutDataSource")]
    if not agent and any(timeouts):
        raise PortalError("the logged-in and logged-out timeouts need an agent ID data source; it cannot be saved")
    form = [("ClearAllSkills", "true")] if settings.get("ClearAllSkills") else []
    form.append(("AgentIdDataSource", agent))
    if agent:
        form += [("LoggedInTimeoutDataSource", timeouts[0]), ("LoggedOutTimeoutDataSource", timeouts[1])]
    form.append(("InterruptAgentWhilstAway", "False"))
    for i, skill in enumerate(settings.get("Skills") or []):
        s = _skill_requirement(skill, f"skill {i + 1}")
        form += [(f"Skills[{i}].{k}", str(v)) for k, v in s.items()]
    dynamic = []
    for i, skill in enumerate(settings.get("DynamicSkills") or []):
        s = _skill_requirement(skill, f"dynamic skill {i + 1}")
        s["FriendlyName"] = (friendly_names or {}).get(s["Name"], s["Name"])
        s["Description"] = skill_description(s)
        dynamic.append(s)
    form += [("SerialisedDynamicSkills", _compact(dynamic)), ("NextApplet", nxt.get("NextApplet") or "")]
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


# --- Route to Interaction Plan (appletType "Shim"; shim.aspx, an ASP.NET WebForms page) ---

SHIM = "Shim"
SHIM_TO_PLAN, SHIM_TO_DATA_SOURCE = "Interaction plan", "Data source"
SHIM_TYPE, SHIM_PLAN, SHIM_APPLET, SHIM_DATA_SOURCE, SHIM_FAILURE = (
    f"datagrid$ctl02${n}" for n in ("ddShimType", "ddDialledNbr", "ddShimApp", "tbDataSourceName", "ddFailureApplet"))
# "Transfer IVR data / DDI / Agent Id" check boxes: they copy IVR slots, not recorded (out of the usual scope).
SHIM_IVR_BOXES = {"TransferIvrData": "datagrid3$ctl02$IVR3", "TransferDdi": "datagrid1$ctl02$IVR",
                  "TransferAgentId": "datagrid2$ctl02$IVR2"}
# VCC refuses any other form ("Please provide data source name in correct notation => $(dataSourceName)"),
# but does not check that the data source exists ("$(SomethignNew)" was saved).
SHIM_DATA_SOURCE_NOTATION = re.compile(r"\$\([^()]+\)")
SHIM_MESSAGE = re.compile(r'<span id="myLabel1"[^>]*>(.*?)</span>', re.S)


class _WebForm(HTMLParser):
    """What a browser posts from a WebForms page (hidden state included, disabled fields not),
    and each select's options as (value, text)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.fields: dict[str, str] = {}
        self.options: dict[str, list[tuple[str, str]]] = {}
        self.checked: set[str] = set()  # checked check boxes (ASP.NET MVC adds a hidden "false" of the same name)
        self.files: dict[str, str] = {}  # file inputs: name -> the value the page shows (an existing file's path)
        self._select: str | None = None
        self._selected: str | None = None
        self._option: list | None = None  # [value, text parts]

    def _end_option(self) -> None:
        if self._option is not None and self._select:
            self.options[self._select].append((self._option[0], " ".join("".join(self._option[1]).split())))
            self._option = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "input" and "disabled" not in a and a.get("name"):
            kind = (a.get("type") or "text").lower()
            if kind == "file":
                self.files[a["name"]] = a.get("value") or ""
                return
            if kind in ("submit", "button", "image", "reset"):
                return
            if kind in ("checkbox", "radio") and "checked" not in a:
                return
            if kind == "checkbox":
                self.checked.add(a["name"])
            self.fields[a["name"]] = a.get("value") if a.get("value") is not None else ("on" if kind == "checkbox" else "")
        elif tag == "select":
            self._select = a.get("name") if "disabled" not in a else None
            self._selected = None
            if self._select:
                self.options[self._select] = []
        elif tag == "option" and self._select:
            self._end_option()
            self._option = [a.get("value") or "", []]
            if "selected" in a:
                self._selected = a.get("value") or ""

    def handle_endtag(self, tag):
        if tag == "option":
            self._end_option()
        elif tag == "select" and self._select:
            self._end_option()
            opts = self.options[self._select]
            if self._selected is not None:
                self.fields[self._select] = self._selected
            elif opts:  # a browser posts the first option when none is selected
                self.fields[self._select] = opts[0][0]
            self._select = None

    def handle_data(self, data):
        if self._option is not None:
            self._option[1].append(data)


def _web_form(html: str) -> _WebForm:
    form = _WebForm()
    form.feed(html)
    form.close()
    return form


def extract_shim(html: str) -> dict:
    """A Route to Interaction Plan page: its settings, with the target plan by name (option text).

    Returns {"RouteType", "TargetPlan", "TargetPlanId", "TargetApplet", "DataSourceName",
    "FailureApplet", "Message", and each SHIM_IVR_BOXES key (True when checked)}.
    """
    form = _web_form(html)
    if SHIM_TYPE not in form.fields:
        raise PortalError("Not a Route to Interaction Plan page (no route type field); the page format may have changed")
    f = form.fields
    plan_id = f.get(SHIM_PLAN, "")
    plan = next((t for v, t in form.options.get(SHIM_PLAN, []) if v and v == plan_id), "")
    message = SHIM_MESSAGE.search(html)
    return {
        "RouteType": f[SHIM_TYPE],
        "TargetPlan": plan,
        "TargetPlanId": plan_id if plan else "",
        "TargetApplet": f.get(SHIM_APPLET, "") if plan else "",
        "DataSourceName": f.get(SHIM_DATA_SOURCE, ""),
        "FailureApplet": f.get(SHIM_FAILURE, ""),
        "Message": html_lib.unescape(re.sub(r"<[^>]+>", " ", message.group(1))).strip() if message else "",
        **{k: box in f for k, box in SHIM_IVR_BOXES.items()},
    }


def shim_configured(settings: dict) -> bool:
    """Whether a Route to Interaction Plan has what VCC needs to save it: a plan and applet, or a data source.
    The failure applet alone is not enough ("Please select an Interaction Plan.")."""
    if settings.get("RouteType") == SHIM_TO_DATA_SOURCE:
        return bool(settings.get("DataSourceName"))
    return bool(settings.get("TargetPlan") and settings.get("TargetApplet"))


def check_shim(settings: dict) -> None:
    """Refuse what was not recorded or what VCC would refuse, before any request."""
    route = settings.get("RouteType")
    if route not in (SHIM_TO_PLAN, SHIM_TO_DATA_SOURCE):
        raise PortalError(f"Route to Interaction Plan type {route!r} has not been recorded yet; it cannot be saved")
    on = [k for k in SHIM_IVR_BOXES if settings.get(k)]
    if on:
        raise PortalError(f"Route to Interaction Plan with IVR data transfer ({', '.join(on)}) is not supported: "
                          "it copies IVR slots")
    name = settings.get("DataSourceName") or ""
    if route == SHIM_TO_DATA_SOURCE and name and not SHIM_DATA_SOURCE_NOTATION.fullmatch(name):
        raise PortalError(f"Route to Interaction Plan data source {name!r} is not in the $(dataSourceName) notation "
                          "VCC requires")


# --- ACD / Queue (appletType "ACD"; ACD?phoneNum=..&myApp=..: an MVC page posting a multipart form, ACD.har) ---

ACD = "ACD"
ACD_PREFIX = "Acd."
# Audio slots: each posts a file part (an upload, or empty) and "<slot>Delete"; the page shows an existing
# file's storage path ("recordings/<account>/<plan>/<GUID>.wav", played at ../<path>). Those with a data
# source alternative also post "<slot>Dynamic" (a data source naming the audio file).
ACD_AUDIO = ("Announcement1", "Announcement2", "Announcement3", "SubsequentAnnouncement", "CurPosAnnouncement",
             "MusicFile", "OnHoldAnnouncement", "RingingAnnouncement", "WhisperAnnouncement")
ACD_DYNAMIC_AUDIO = ("Announcement1", "Announcement2", "Announcement3", "SubsequentAnnouncement", "MusicFile",
                     "WhisperAnnouncement")
ACD_DATA_SOURCES = tuple(f"{a}Dynamic" for a in ACD_DYNAMIC_AUDIO) + ("ObjectPopDataSource",)
# Dropdowns of this plan's applets, by name: exported as connections.
ACD_CONNECTIONS = ("MaxEstimatedWaitApplication", "MaxQueueApplet", *(f"App{i}" for i in range(10)),
                   "AgentHangupApplet", "NoAgentsLoggedInApplet", "AgentRejectApplet", "QueueTimeoutApplet")
ACD_CHECKBOXES = ("PreferLastHandlingAgent", "PlayTargetAnswerTime", "PlayRecentAnswerTime",
                  "PlayTargetAnswerTimeWhenRecentUnavailable", "ShouldPlayQueuePosition", "UnlimitedQueueLength",
                  "BreakoutReenter", "SalesforceTransfer")
# Presence states are posted by GUID, which differs per account: exported by name ("" = "Leave unchanged").
ACD_PRESENCE = {"PostUnexpectedPresenceStateGuid": "PostUnexpectedPresenceState",
                "PostWrapPresenceStateGuid": "PostWrapPresenceState"}
ACD_GROUPS = 10  # Group0..Group9 (display IDs); TimedOverflow1..9 (seconds) once their group is chosen
ACD_TEXT = ("CallRouterAlgorithm", "AgentTiebreakAlgorithm", "MaxEstimatedWaitPeriod", "AgePerMinute",
            "MaxQueueLength", "BreakoutAvailable", "MusicDuration", "NoAnswerTimeoutAsSeconds",
            "QueueTimeoutAsSeconds", "WhisperOption", "WrapupTimeAsSeconds")
# 0 longest waiting, 1 skills based FIFO, 2 skills based LIFO (the page's acd_types). 3 (Salesforce
# Omni-Channel, with SalesForceDialledNumber, FlowDevName, FallBackQueue, FlowInputParameters) is not recorded.
ACD_ALGORITHMS = {"0", "1", "2"}
ACD_WRAPUP_ALGORITHMS = {"1", "2"}  # HasWrapupTime: the wrap-up time is posted only for these
ACD_SALESFORCE = ("SalesForceDialledNumber", "FlowDevName", "FallBackQueue")
ACD_SAVED = "This applet has been saved."
# The page's wording of the fixed choices (ACD.har): the form posts the code, the report shows the wording.
ACD_LABELS = {
    "CallRouterAlgorithm": {"0": "Longest waiting (first in, first out)", "1": "Skills based (first in, first out)",
                            "2": "Skills based (last in, first out)"},
    "AgentTiebreakAlgorithm": {"1": "Random", "2": "Time since answering last inbound call (descending)",
                               "0": "Time since handling last interaction or changing presence (descending)",
                               "3": "Time since handling last interaction (descending)"},
    "BreakoutAvailable": {"0": "Always", "1": "First Retry", "2": "Second Retry", "3": "Third Retry"},
    "WhisperOption": {"Off": "Off", "AgentOnly": "Agent Only"},
}
GROUP_LABEL = re.compile(r"^(.*) \(([^()]*)\)$")  # a group option: "France (13102022001)"


def extract_acd(html: str) -> dict:
    """An ACD page: {"form": _WebForm, "audio": {slot: existing file path}}; refuses another page."""
    form = _web_form(html)
    if f"{ACD_PREFIX}CallRouterAlgorithm" not in form.fields:
        raise PortalError("Not an ACD page (no call router algorithm field); the page format may have changed")
    audio = {slot: form.files.get(ACD_PREFIX + slot, "") for slot in ACD_AUDIO}
    return {"form": form, "audio": {k: v for k, v in audio.items() if v}}


def _option_text(form: _WebForm, field: str, value: str) -> str:
    return next((t for v, t in form.options.get(field, []) if v == value), "")


def acd_settings(page: dict) -> tuple[dict, dict]:
    """Return (settings, next) for extract_acd(): connections, groups and presence states by name."""
    form = page["form"]
    f = {k[len(ACD_PREFIX):]: v for k, v in form.fields.items() if k.startswith(ACD_PREFIX)}
    settings: dict[str, Any] = {k: f.get(k, "") for k in ACD_TEXT}
    settings.update({k: ACD_PREFIX + k in form.checked for k in ACD_CHECKBOXES})
    settings.update({k: f.get(k, "") for k in ACD_DATA_SOURCES})
    for guid_field, name in ACD_PRESENCE.items():
        settings[name] = _option_text(form, ACD_PREFIX + guid_field, f.get(guid_field, "")) if f.get(guid_field) else ""
    groups = []
    for i in range(ACD_GROUPS):
        display_id = f.get(f"Group{i}", "")
        if display_id:
            label = _option_text(form, f"{ACD_PREFIX}Group{i}", display_id)
            m = GROUP_LABEL.match(label)
            groups.append({"Slot": i, "Name": m.group(1) if m else label, "DisplayId": display_id,
                           "TimedOverflow": f.get(f"TimedOverflow{i}", "") if i else ""})
    settings["Groups"] = groups
    settings.update({k: f.get(k, "") for k in ACD_SALESFORCE})
    settings["FlowInputParameters"] = sorted(k for k in f if k.startswith("FlowInputParameters"))
    nxt = {k: f[k] for k in ACD_CONNECTIONS if f.get(k)}
    return settings, nxt


def check_acd(settings: dict) -> None:
    """Refuse what was not recorded, before any request."""
    if str(settings.get("CallRouterAlgorithm")) not in ACD_ALGORITHMS:
        raise PortalError(f"ACD call router algorithm {settings.get('CallRouterAlgorithm')!r} has not been recorded "
                          "yet (3 is Salesforce Omni-Channel); it cannot be saved")
    if settings.get("SalesforceTransfer") or any(settings.get(k) for k in ACD_SALESFORCE) \
            or settings.get("FlowInputParameters"):
        raise PortalError("ACD Salesforce transfer / Omni-Channel flow settings have not been recorded yet; "
                          "they cannot be saved")
    slots = [g["Slot"] for g in settings.get("Groups") or []]
    if len(set(slots)) != len(slots) or any(not 0 <= s < ACD_GROUPS for s in slots):
        raise PortalError(f"ACD group slots {slots} are not valid")
    ids = [g["DisplayId"] for g in settings.get("Groups") or []]
    if len(set(ids)) != len(ids):
        raise PortalError("An ACD cannot use the same group twice (the page refuses it)")


def acd_form(settings: dict, nxt: dict, identity: dict, presence_ids: dict[str, str],
             uploads: dict[str, tuple[str, bytes]] | None = None,
             existing_audio: set[str] | frozenset = frozenset()) -> list[tuple[str, Any]]:
    """The ACD form post, in the order the page posts it (ACD.har).

    identity: the page's DialledNumber and ApplicationName. presence_ids: presence state name -> GUID in
    the target. uploads: audio slot -> (file name, content); other slots post an empty file part, which keeps
    a file already there (existing_audio). Each value is a string, or (file name, content, content type).
    Fields the page disables are not posted: the maximum queue length when it is unlimited, a group's
    overflow time when no group is chosen, the music duration and whisper option without audio, the
    wrap-up time for "longest waiting".
    """
    check_acd(settings)
    uploads = uploads or {}
    s, out = settings, []

    def text(name: str, value: Any) -> None:
        out.append((ACD_PREFIX + name, "" if value is None else str(value)))

    def checkbox(name: str) -> None:
        if s.get(name):
            text(name, "true")
        text(name, "false")

    def audio(slot: str) -> None:
        if slot in ACD_DYNAMIC_AUDIO:
            text(f"{slot}Dynamic", s.get(f"{slot}Dynamic"))
        filename, content = uploads.get(slot, ("", b""))
        out.append((ACD_PREFIX + slot, (filename, content, "audio/wav" if filename else "application/octet-stream")))
        text(f"{slot}Delete", "0")

    def has_audio(slot: str) -> bool:
        return slot in uploads or slot in existing_audio

    def presence(guid_field: str) -> None:
        name = s.get(ACD_PRESENCE[guid_field]) or ""
        if name and name not in presence_ids:
            raise PortalError(f"ACD presence state '{name}' does not exist in the target account")
        text(guid_field, presence_ids.get(name, "") if name else "")

    text("DialledNumber", identity["DialledNumber"])
    text("ApplicationName", identity["ApplicationName"])
    text("CallRouterAlgorithm", s.get("CallRouterAlgorithm"))
    text("AgentTiebreakAlgorithm", s.get("AgentTiebreakAlgorithm"))
    for name in ("PreferLastHandlingAgent", "PlayTargetAnswerTime", "PlayRecentAnswerTime",
                 "PlayTargetAnswerTimeWhenRecentUnavailable"):
        checkbox(name)
    text("MaxEstimatedWaitPeriod", s.get("MaxEstimatedWaitPeriod"))
    text("MaxEstimatedWaitApplication", nxt.get("MaxEstimatedWaitApplication"))
    checkbox("ShouldPlayQueuePosition")
    text("AgePerMinute", s.get("AgePerMinute"))
    checkbox("UnlimitedQueueLength")
    if not s.get("UnlimitedQueueLength"):
        text("MaxQueueLength", s.get("MaxQueueLength"))
    text("MaxQueueApplet", nxt.get("MaxQueueApplet"))
    text("BreakoutAvailable", s.get("BreakoutAvailable"))
    checkbox("BreakoutReenter")
    for i in range(10):
        text(f"App{i}", nxt.get(f"App{i}"))
    for slot in ("Announcement1", "Announcement2", "Announcement3", "SubsequentAnnouncement", "CurPosAnnouncement",
                 "MusicFile"):
        audio(slot)
    if has_audio("MusicFile"):
        text("MusicDuration", s.get("MusicDuration"))
    text("NoAnswerTimeoutAsSeconds", s.get("NoAnswerTimeoutAsSeconds"))
    presence("PostUnexpectedPresenceStateGuid")
    for name in ("AgentHangupApplet", "NoAgentsLoggedInApplet", "AgentRejectApplet"):
        text(name, nxt.get(name))
    text("QueueTimeoutAsSeconds", s.get("QueueTimeoutAsSeconds"))
    text("QueueTimeoutApplet", nxt.get("QueueTimeoutApplet"))
    for slot in ("OnHoldAnnouncement", "RingingAnnouncement", "WhisperAnnouncement"):
        audio(slot)
    if s.get("WhisperAnnouncementDynamic") or has_audio("WhisperAnnouncement"):
        text("WhisperOption", s.get("WhisperOption"))
    text("ObjectPopDataSource", s.get("ObjectPopDataSource"))
    groups = {g["Slot"]: g for g in s.get("Groups") or []}
    for i in range(ACD_GROUPS):
        g = groups.get(i)
        text(f"Group{i}", g["DisplayId"] if g else "")
        if g and i:
            text(f"TimedOverflow{i}", g.get("TimedOverflow"))
    if str(s.get("CallRouterAlgorithm")) in ACD_WRAPUP_ALGORITHMS:
        text("WrapupTimeAsSeconds", s.get("WrapupTimeAsSeconds"))
    presence("PostWrapPresenceStateGuid")
    checkbox("SalesforceTransfer")
    for name in ACD_SALESFORCE:
        text(name, s.get(name))
    return out


def acd_presence_states(form: _WebForm) -> dict[str, str]:
    """Presence state name -> GUID, as an ACD page offers them ("Leave unchanged" left out)."""
    return {t: v for v, t in form.options.get(f"{ACD_PREFIX}PostWrapPresenceStateGuid", []) if v}


def acd_groups(form: _WebForm) -> list[dict]:
    """The account's groups as an ACD page offers them: [{"groupDisplayId", "groupName"}]."""
    groups = []
    for v, t in form.options.get(f"{ACD_PREFIX}Group0", []):
        if v:
            m = GROUP_LABEL.match(t)
            groups.append({"groupDisplayId": v, "groupName": m.group(1) if m else t})
    return groups


# Dial In Config (dialInConfig.har): a classic ASP page, dialinconfig.asp, with one multipart form per
# configuration (0-9: next applet, alternative text, announcement) and one form choosing the configuration.
# Each form posts to updated.asp, which saves that configuration alone ("Your details have been
# successfully updated"; the choice answers "Your applet number <i> have been selected"). A configuration's current applet is the first option of its list (not marked
# selected); its announcement is a "Play" link to recordings/...; a save without a file keeps the file.
DIAL_IN_CONFIG = "DialInConfig"
DIC_CONFIGS = range(10)
DIC_PAGE = "dialinconfig.asp"
DIC_FORM = re.compile(r'<form action="updated\.asp"(.*?)</form>', re.S | re.I)
DIC_PLAY = re.compile(r'href="\.\./(recordings/[^"]+)"[^>]*>\s*Play\s*</a>', re.I)
DIC_SAVED = "successfully updated"
DIC_CHOSEN = "Your applet number {} have been selected"
DIC_KEYS = re.compile(r"^(AltText|NextApplet)\.([0-9])$")


def extract_dial_in_config(html: str) -> dict:
    """A Dial In Config page: {"configs": {i: {"NextApplet", "AltText", "Audio"}}, "selected": i,
    "applets": the applets offered}. Audio is the existing file's path ("recordings/..."), or None."""
    configs, selected, applets = {}, None, set()
    for body in DIC_FORM.findall(html):
        form = _web_form("<form" + body + "</form>")
        if form.fields.get("optype") == "announcementconfig":
            i = int(form.fields.get("index", "-1"))
            play = DIC_PLAY.search(body)
            configs[i] = {"NextApplet": form.fields.get(f"nextapplet{i}", ""), "AltText": form.fields.get("alttext", ""),
                          "Audio": html_lib.unescape(play.group(1)) if play else None}
            applets |= {v for v, _ in form.options.get(f"nextapplet{i}", []) if v}
        elif form.fields.get("optype") == "configNumber":
            m = re.fullmatch(r"Selected([0-9])", form.fields.get("option", ""))
            selected = int(m.group(1)) if m else None
    if sorted(configs) != list(DIC_CONFIGS) or selected is None:
        raise PortalError("Not a Dial In Config page (configurations 0-9 and the chosen one expected); "
                          "the page format may have changed")
    return {"configs": configs, "selected": selected, "applets": applets}


def dial_in_config_settings(page: dict) -> tuple[dict, dict]:
    """(settings, next) of a Dial In Config: the chosen configuration, each configuration's alternative text
    ("AltText.<i>") and next applet ("NextApplet.<i>"), empty ones left out."""
    settings: dict = {"SelectedConfiguration": page["selected"]}
    nxt = {}
    for i, c in sorted(page["configs"].items()):
        if c["AltText"]:
            settings[f"AltText.{i}"] = c["AltText"]
        if c["NextApplet"]:
            nxt[f"NextApplet.{i}"] = c["NextApplet"]
    return settings, nxt


def check_dial_in_config(settings: dict, nxt: dict) -> None:
    """Refuse what the recording does not show."""
    keys = [k for k in [*settings, *nxt] if k != "SelectedConfiguration" and not DIC_KEYS.match(k)]
    if keys:
        raise PortalError(f"Dial In Config settings {', '.join(keys)} have not been recorded yet; they cannot be saved")
    if settings.get("SelectedConfiguration") not in DIC_CONFIGS:
        raise PortalError(f"Dial In Config selected configuration {settings.get('SelectedConfiguration')!r} "
                          "is not 0 to 9")


def dial_in_config_empty(settings: dict, nxt: dict, audio: set) -> list[int]:
    """The configurations with no applet, alternative text or announcement (audio: their numbers)."""
    return [i for i in DIC_CONFIGS if not (settings.get(f"AltText.{i}") or nxt.get(f"NextApplet.{i}") or i in audio)]


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
    """Collect what a browser would submit from a page's inputs, selects and textareas.

    A selected option whose value is an account-specific ID (ACCOUNT_ID_VALUE) is kept by its text.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.fields: dict[str, list[str]] = {}
        self._select: str | None = None
        self._option: tuple[str, list[str]] | None = None  # selected option being read: (value, text)
        self._textarea: str | None = None
        self._text: list[str] = []

    def _add(self, name: str | None, value: str) -> None:
        if name and name not in FORM_NOISE:
            self.fields.setdefault(name, []).append(value)

    def _end_option(self) -> None:
        if self._option is not None:
            value, text = self._option
            label = " ".join("".join(text).split())
            self._add(self._select, label if label and ACCOUNT_ID_VALUE.fullmatch(value) else value)
            self._option = None

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
        elif tag == "option" and self._select:
            self._end_option()  # </option> is optional in HTML
            if "selected" in a:
                self._option = (a.get("value") or "", [])
        elif tag == "textarea":
            self._textarea, self._text = a.get("name"), []

    def handle_endtag(self, tag):
        if tag == "option":
            self._end_option()
        elif tag == "select":
            self._end_option()
            self._select = None
        elif tag == "textarea" and self._textarea:
            self._add(self._textarea, "".join(self._text).strip())
            self._textarea = None

    def handle_data(self, data):
        if self._textarea:
            self._text.append(data)
        elif self._option is not None:
            self._option[1].append(data)


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

    def get_skill_tagger(self, applet: dict) -> dict:
        """Return extract_skill_tagger() for a Skill Tagger from list_applets()."""
        return extract_skill_tagger(self._request("GET", applet["configUrl"]).text)

    def get_shim(self, applet: dict) -> dict:
        """Return extract_shim() for a Route to Interaction Plan from list_applets()."""
        return extract_shim(self._request("GET", applet["configUrl"]).text)

    def get_acd(self, applet: dict) -> dict:
        """Return extract_acd() for an ACD from list_applets()."""
        return extract_acd(self._request("GET", applet["configUrl"]).text)

    def download_recording(self, path: str) -> bytes:
        """Download an ACD audio file by the path its page shows ("recordings/..."), as "Play Existing" does."""
        resp = self.session.get(f"{self.account.portal_url}/{path}", allow_redirects=False, timeout=120)
        if resp.status_code != 200 or not resp.content:
            raise PortalError(f"Downloading {path} failed: HTTP {resp.status_code}")
        return resp.content

    def acd_lookups(self, plan_id: str | None = None) -> dict | None:
        """{"groups": acd_groups(), "presence": acd_presence_states()} from the first ACD page found
        (plan_id first). None if the account has no ACD."""
        def read(_pid: str, applet: dict) -> Any:
            if not (applet.get("configUrl") or "").startswith("ACD"):
                return None
            form = self.get_acd(applet)["form"]
            return {"groups": acd_groups(form), "presence": acd_presence_states(form)}
        return self._first_from_applets(plan_id, read)

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
        def read(pid: str, applet: dict) -> Any:
            url = applet.get("configUrl") or ""
            if url.startswith("DataRetriever"):
                return self.get_data_connector(applet)["model"].get("MetabaseEntries")
            if url.startswith("Applet?"):
                return (self.get_applet_state(pid, applet).get("dataSourceKeys") or {}).get("metabaseEntries")
            return None

        entries = self._first_from_applets(plan_id, read)
        return None if entries is None else set(entries)

    def list_custom_configurations(self, plan_id: str | None = None) -> list[str] | None:
        """The account's Custom Configuration names (CustomAccountConfigs), without their values.

        Vonage sets them up on request; plans read them as #(Name), e.g. in a Data Connector
        definition. Only the Data Connector editor page carries the list, so it is read from
        the first Data Connector found, in plan_id first. None if the account has none.
        """
        def read(_pid: str, applet: dict) -> Any:
            if not (applet.get("configUrl") or "").startswith("DataRetriever"):
                return None
            return self.get_data_connector(applet)["model"].get("CustomAccountConfigs")

        names = self._first_from_applets(plan_id, read)
        return None if names is None else list(names)

    def list_skills(self, plan_id: str | None = None) -> list[str] | None:
        """The account's skill names, as the Skill Tagger page lists them (AllSkills).

        Read from the first Skill Tagger found, in plan_id first. None if the account has none.
        """
        def read(_pid: str, applet: dict) -> Any:
            if not (applet.get("configUrl") or "").startswith("SkillPersonalQueueTagger"):
                return None
            return self.get_skill_tagger(applet).get("AllSkills")

        names = self._first_from_applets(plan_id, read)
        return None if names is None else list(names)

    def _first_from_applets(self, plan_id: str | None, read: Callable[[str, dict], Any]) -> Any:
        """First value read(plan_id, applet) returns that is not None, over the account's applets
        (plan_id's first). Applets whose page fails to load are skipped."""
        plan_ids = [plan_id] if plan_id else []
        plan_ids += [p["interactionPlanId"] for p in self.list_plans() if p["interactionPlanId"] != plan_id]
        for pid in plan_ids:
            for applet in self.list_applets(pid).get("applets", []):
                try:
                    value = read(pid, applet)
                except PortalError:
                    continue
                if value is not None:
                    return value
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
        """Save a React editor's applet. VCC refuses with {"errors": [...]}, e.g. "'Schedule' field is required."
        (scheduleRouter.har): that is an error here, not a silent success."""
        self._require_writes()
        body = self._json(
            "Applet/Save",
            {"interactionPlanId": plan_id, "appletName": name, "appletProperties": encode_properties(properties)},
        )
        if isinstance(body, dict) and body.get("errors"):
            raise PortalError(f"Saving '{name}' was refused: {'; '.join(map(str, body['errors']))}")
        return body

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

    def save_skill_tagger(self, plan_id: str, name: str, settings: dict, nxt: dict) -> dict:
        """Save a Skill Tagger's skills, personal queue and next applet, as its page's save does.

        Returns the saved page parsed by extract_skill_tagger(), for reading back.
        """
        self._require_writes()
        skill_tagger_form(settings, nxt)  # fails before any request if unsupported
        page_url = f"SkillPersonalQueueTagger?phonenum={quote(plan_id)}&myapp={quote(name)}&application=SkillTagger"
        page = self._request("GET", page_url).text
        token = re.search(INPUT_VALUE.format(name="__RequestVerificationToken"), page)
        if not token:
            raise PortalError(f"Skill Tagger page for '{name}' has no form token")
        friendly = extract_skill_tagger(page).get("AllMetabaseEntries") or {}
        form = skill_tagger_form(settings, nxt, friendly)
        resp = self._request("POST", f"SkillPersonalQueueTagger?phoneNum={quote(plan_id)}&myApp={quote(name)}",
                             headers={"Referer": f"{self.base}/{page_url}"},
                             data=[("__RequestVerificationToken", html_lib.unescape(token.group(1))), *form])
        errors = [re.sub(r"<[^>]+>|\s+", " ", e).strip() for e in VALIDATION_ERRORS.findall(resp.text)]
        if any(errors):
            raise PortalError(f"Saving Skill Tagger '{name}' was refused: {'; '.join(e for e in errors if e)}")
        return extract_skill_tagger(resp.text)

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

    def save_shim(self, plan_id: str, name: str, settings: dict, nxt: dict) -> dict:
        """Save a Route to Interaction Plan as its page does (routeToInteractionPlan.har).

        A WebForms page: every change posts the whole form back (with its __VIEWSTATE) and the
        page answers with the new state. As in the browser, the route type is chosen first, then the
        target plan, whose applets the answer lists; "Update" saves, redirecting to updated.asp. A
        refused save comes back as the page with its message ("Please select an Interaction Plan.").
        The plan is chosen by name; the plan being edited is never offered. Returns extract_shim()
        of the saved page.
        """
        self._require_writes()
        check_shim(settings)
        if not shim_configured(settings):
            raise PortalError(f"Route to Interaction Plan '{name}' has no Interaction Plan and applet, or data "
                              "source: VCC does not save it")
        url = f"shim.aspx?phonenum={quote(plan_id)}&myapp={quote(name)}&application=Shim"
        headers = {"Referer": f"{self.base}/{url}"}

        def post(form: _WebForm, target: str = "", save: bool = False) -> requests.Response:
            data = {**form.fields, "__EVENTTARGET": target, "__EVENTARGUMENT": ""}
            if save:
                data["Update"] = "Update"
            resp = self._request("POST", url, expect_redirect=None, headers=headers, data=data)
            if resp.is_redirect and not save:
                raise PortalError(f"Route to Interaction Plan '{name}': changing {target} left the page "
                                  f"({resp.headers.get('Location')})")
            return resp

        def choose(form: _WebForm, field: str, value: str) -> _WebForm:
            form.fields[field] = value
            return _web_form(post(form, field).text)

        form = _web_form(self._request("GET", url).text)
        if SHIM_TYPE not in form.fields:
            raise PortalError(f"Route to Interaction Plan page for '{name}' has no route type field")
        route = settings["RouteType"]
        if form.fields[SHIM_TYPE] != route:
            form = choose(form, SHIM_TYPE, route)
        if route == SHIM_TO_PLAN:
            plans = [v for v, t in form.options.get(SHIM_PLAN, []) if v and t == settings["TargetPlan"]]
            if len(plans) != 1:
                why = "is not offered (it is missing, or it is the plan being edited)" if not plans else "is ambiguous"
                raise PortalError(f"Route to Interaction Plan '{name}': Interaction Plan '{settings['TargetPlan']}' {why}")
            form = choose(form, SHIM_PLAN, plans[0])
            if settings["TargetApplet"] not in [v for v, _ in form.options.get(SHIM_APPLET, [])]:
                raise PortalError(f"Route to Interaction Plan '{name}': applet '{settings['TargetApplet']}' is not in "
                                  f"Interaction Plan '{settings['TargetPlan']}'")
            form.fields[SHIM_APPLET] = settings["TargetApplet"]
        else:
            form.fields[SHIM_DATA_SOURCE] = settings["DataSourceName"]
        failure = nxt.get("FailureApplet") or ""
        if failure not in [v for v, _ in form.options.get(SHIM_FAILURE, [])]:
            raise PortalError(f"Route to Interaction Plan '{name}': failure applet '{failure}' is not offered")
        form.fields[SHIM_FAILURE] = failure
        resp = post(form, save=True)
        if not resp.is_redirect:
            reason = extract_shim(resp.text)["Message"] or "no reason shown on the page"
            raise PortalError(f"Saving Route to Interaction Plan '{name}' was refused: {reason}")
        location = resp.headers.get("Location", "")
        if "/apps/updated.asp" not in location.lower():
            raise PortalError(f"Saving Route to Interaction Plan '{name}' was not confirmed: redirected to {location}")
        # The page only validates: the redirect carries the values (name=shim&dd1=<plan ID or $(data
        # source)>&dd2=<applet>&nextAppletFail=<applet>) and updated.asp, a classic ASP page, saves them
        # (found live 2026-11-25: without opening it, nothing was saved).
        done = self._request("GET", location.split("/apps/", 1)[1]).text
        if "successfully updated" not in done:
            status = re.search(r'id="statusMessage"[^>]*>(.*?)</td>', done, re.S)
            message = re.sub(r"<[^>]+>|\s+", " ", status.group(1)).strip() if status else "no message"
            raise PortalError(f"Saving Route to Interaction Plan '{name}' was not confirmed: {message}")
        saved = extract_shim(self._request("GET", url).text)
        want = {"RouteType": route, "FailureApplet": failure, **(
            {"TargetPlan": settings["TargetPlan"], "TargetApplet": settings["TargetApplet"]} if route == SHIM_TO_PLAN
            else {"DataSourceName": settings["DataSourceName"]})}
        wrong = [f"{k} {saved[k]!r} instead of {v!r}" for k, v in want.items() if saved[k] != v]
        if wrong:
            raise PortalError(f"Route to Interaction Plan '{name}' reads back with {', '.join(wrong)}")
        return saved

    def save_acd(self, plan_id: str, name: str, settings: dict, nxt: dict,
                 audio: dict[str, Path] | None = None) -> dict:
        """Save an ACD as its page does: one multipart post with every field and the audio files (ACD.har).

        Groups are posted by display ID and must be offered by the page; presence states are posted by the
        GUID the target's page gives their name. VCC answers with the page and "This applet has been saved."
        Returns extract_acd() of that page.
        """
        self._require_writes()
        check_acd(settings)
        page_url = f"ACD?phoneNum={quote(plan_id)}&myApp={quote(name)}"
        page = extract_acd(self._request("GET", f"{page_url}&application=ACD").text)
        form = page["form"]
        token = form.fields.get("__RequestVerificationToken")
        if not token:
            raise PortalError(f"ACD page for '{name}' has no form token")
        offered = {v for v, _ in form.options.get(f"{ACD_PREFIX}Group0", []) if v}
        missing = [f"{g['Name']} ({g['DisplayId']})" for g in settings.get("Groups") or [] if g["DisplayId"] not in offered]
        if missing:
            raise PortalError(f"ACD '{name}': groups not offered by the page: {', '.join(missing)}")
        uploads = {}
        for slot, path in (audio or {}).items():
            if slot not in ACD_AUDIO:
                raise PortalError(f"ACD '{name}': {slot} is not an audio slot")
            if path.stat().st_size > MAX_MEDIA_BYTES:
                raise PortalError(f"{path} is larger than 10 MB, which the portal refuses")
            uploads[slot] = (path.name, path.read_bytes())
        identity = {k: form.fields[f"{ACD_PREFIX}{k}"] for k in ("DialledNumber", "ApplicationName")}
        parts = acd_form(settings, nxt, identity, acd_presence_states(form), uploads, set(page["audio"]))
        files = [("__RequestVerificationToken", (None, token))]
        files += [(k, (None, v) if isinstance(v, str) else v) for k, v in parts]
        resp = self._request("POST", page_url, headers={"Referer": f"{self.base}/{page_url}"}, files=files)
        errors = [re.sub(r"<[^>]+>|\s+", " ", e).strip() for e in VALIDATION_ERRORS.findall(resp.text)]
        if any(errors):
            raise PortalError(f"Saving ACD '{name}' was refused: {'; '.join(e for e in errors if e)}")
        if ACD_SAVED not in resp.text:
            raise PortalError(f"Saving ACD '{name}' was not confirmed (no \"{ACD_SAVED}\")")
        return extract_acd(resp.text)

    def get_dial_in_config(self, applet: dict) -> dict:
        """Return extract_dial_in_config() for a Dial In Config from list_applets()."""
        return extract_dial_in_config(self._request("GET", applet["configUrl"]).text)

    def save_dial_in_config(self, plan_id: str, name: str, settings: dict, nxt: dict,
                            audio: dict[int, Path] | None = None) -> dict:
        """Save a Dial In Config as its page does (dialInConfig.har): one multipart post to updated.asp per
        configuration that has an applet, an alternative text or an announcement, then the chosen
        configuration. Empty configurations are not posted (a new applet has none). An announcement
        cannot be removed: a post without a file keeps the existing one. Returns the page read back,
        after checking every configuration and the chosen one."""
        self._require_writes()
        check_dial_in_config(settings, nxt)
        audio = audio or {}
        url = f"{DIC_PAGE}?phonenum={quote(plan_id)}&myapp={quote(name)}&application={DIAL_IN_CONFIG}"
        headers = {"Referer": f"{self.base}/{url}"}
        page = extract_dial_in_config(self._request("GET", url).text)
        identity = [("validation", "true"), ("name", "dialinconfig")]
        where = [("phonenum", plan_id), ("myapp", name), ("application", DIAL_IN_CONFIG)]

        def done(resp: requests.Response, what: str, expected: str = DIC_SAVED) -> None:
            if expected not in re.sub(r"\s+", " ", resp.text):
                status = re.search(r'id="statusMessage"[^>]*>(.*?)</td>', resp.text, re.S)
                message = re.sub(r"<[^>]+>|\s+", " ", status.group(1)).strip() if status else "no message"
                raise PortalError(f"Saving Dial In Config '{name}' {what} was not confirmed: {message}")

        for i in DIC_CONFIGS:
            applet, alt, path = nxt.get(f"NextApplet.{i}", ""), settings.get(f"AltText.{i}", ""), audio.get(i)
            if not (applet or alt or path):
                continue
            if applet and applet not in page["applets"]:
                raise PortalError(f"Dial In Config '{name}': applet '{applet}' (configuration {i}) is not offered")
            if path and path.stat().st_size > MAX_MEDIA_BYTES:
                raise PortalError(f"{path} is larger than 10 MB, which the portal refuses")
            upload = (path.name, path.read_bytes(), "audio/wav") if path else ("", b"", "application/octet-stream")
            parts = [*identity, ("optype", "announcementconfig"), *where, ("TargetURL", "../uploads/"),
                     ("index", str(i)), ("announcement", ""), (f"nextapplet{i}", applet), ("alttext", alt)]
            files = [(k, (None, v)) for k, v in parts] + [("myfile", upload), ("B2", (None, "Update"))]
            done(self._request("POST", "updated.asp", headers=headers, files=files), f"configuration {i}")
        selected = settings["SelectedConfiguration"]
        if selected != page["selected"]:
            data = [*identity, ("optype", "configNumber"), *where, ("option", f"Selected{selected}"), ("B3", "Update")]
            done(self._request("POST", "updated.asp", headers=headers, data=data), "selected configuration",
                 DIC_CHOSEN.format(selected))
        saved = extract_dial_in_config(self._request("GET", url).text)
        wrong = [] if saved["selected"] == selected else [f"selected configuration {saved['selected']}"]
        for i, c in saved["configs"].items():
            if c["NextApplet"] != nxt.get(f"NextApplet.{i}", ""):
                wrong.append(f"configuration {i} applet {c['NextApplet']!r}")
            if c["AltText"] != settings.get(f"AltText.{i}", ""):
                wrong.append(f"configuration {i} alternative text {c['AltText']!r}")
            if bool(c["Audio"]) != (i in audio):
                wrong.append(f"configuration {i} {'with' if c['Audio'] else 'without'} an announcement")
        if wrong:
            raise PortalError(f"Dial In Config '{name}' reads back with {', '.join(wrong)}")
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
