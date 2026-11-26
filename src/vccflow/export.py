"""Export an interaction plan into the portable vccflow plan format.

The format keeps what an engineer configured and drops what belongs to one
account: applet IDs and GUIDs, the plan ID and media IDs. Connections between
applets are kept by applet name, and uploaded audio files are saved next to
the plan file, so the plan can be rebuilt in another plan or account.

Applet types import-plan does not write yet keep account-specific references by name too, so
that plans in two accounts compare by what they point to: other applets (as connections), and
GUIDs their editor page labels (a Schedule Router's schedule). Their editor page fields keep a
dropdown option that is an ID by its text (a plan, a presence state).

Layout: <out_dir>/plan.json and <out_dir>/media/<applet name>.<ext>
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from vccflow.portal import (ACD, ACD_DATA_SOURCES, DIAL_IN_CONFIG, DIC_PAGE, SHIM, SHIM_IVR_BOXES, SKILL_TIME_KEYS,
                            PortalClient, PortalError, acd_settings, dial_in_config_empty,
                            dial_in_config_settings)

FORMAT = "vccflow.plan/1"
SCHEDULE_ROUTER = "ScheduleRouter"
# A Schedule Router editor lists one mapping field per service status of each schedule (scheduleRouter.har).
SCHEDULE_MAPPING_PROP = re.compile(r"^ScheduleMappings\[([^:\]]+):(.*)\]$")
DATA_ROUTER = "DataRouter"
SKILL_TAGGER = "SkillTagger"
# Skill Tagger personal queue settings: each names a data source.
SKILL_TAGGER_DATA_SOURCES = ("AgentIdDataSource", "LoggedInTimeoutDataSource", "LoggedOutTimeoutDataSource")
# Plan-level settings saved in the export. They are reported, not imported: a copy leaves them as they are.
PLAN_SETTINGS = ("reportingGroupName", "displayInAgentInterface", "outboundClidBehaviour", "outboundClid")

# Applet properties that identify an applet in one account; rebuilt on import.
ACCOUNT_SPECIFIC = {
    "DialledNumber", "EntityId", "AppletGuid", "AppletName", "DisplayName",
    "Url", "ConfigurationUrl", "ApplicationType", "AppletType", "AccountId",
}
# Menu connections (exported into "next"): Choices {key: applet name}, the timeout
# applet by name + GUID, and the failure applet by numeric ID + GUID (no name).
MENU_CONNECTION_KEYS = {"Choices", "TimeoutAppletName", "FailureAppletId"}
# Data Connector page-model keys that are lookup lists or derived values, not configuration.
DC_NOT_CONFIG = {
    "DataProviders", "Actions", "MetabaseEntries", "PossibleNextApplets", "FormattedNextApplets",
    "TemplateGroups", "CustomAccountConfigs", "IvrSlotsAvailable", "MatchObjectNames",
    "UserCanApprove", "PartialView", "IsVgipDataProvider", "MultipleDataProviders",
    "SerialisedResponseItems", "CrmFields", "FilterableCrmFields",
}
# Lookup lists inside the Data Connector's Salesforce sub-models.
DC_SUB_NOT_CONFIG = {"MatchBys", "MetabaseDetails", "DataSourceTypes", "IvrSlotDetails"}
PLACEHOLDER = re.compile(r"\$\(([^)]+)\)")
# Vonage keywords written like data sources but not data sources: $(JavascriptEngineUrl) as a
# Data Connector's URL marks its definition as JavaScript code, not a REST request (user, 2026-11-13).
RESERVED = {"javascriptengineurl"}
# Editor-page applet types import-plan writes back: their settings stay exactly as VCC sends them.
IMPORTABLE_EDITOR_TYPES = {"DataAnnouncer", "SetDataSource", "Menu", "Priority"}
# Queue - Set Priority (setPriority.har): the editor's choices, value -> the page's wording. VCC answers
# with a number (0.5) and the editor posts a string ("0.9"); both are saved.
PRIORITY = "Priority"
PRIORITY_LABELS = {"0.9": "Highest", "0.75": "Higher", "0.5": "Normal", "0.25": "Lower", "0.1": "Lowest"}
GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
XML_DESTINATION = re.compile(r"<(?:Destination|ResponseCodeDestination)>([^<]+)</")
SOQL_SELECT = re.compile(r"^\s*SELECT\s+(.+?)\s+FROM\s+(\w+)", re.I | re.S)


def _is_next_key(key: str) -> bool:
    return key.startswith("NextApplet") and not key.endswith(("Guid", "Url"))


def split_connections(props: dict, names_by_id: dict | None = None) -> tuple[dict, dict]:
    """Return (settings, next) where next maps connection keys to applet names.

    NextApplet* properties keep their names; a Menu's connections become
    "Choices.<key>", "TimeoutApplet" and "FailureApplet" (names_by_id maps the
    failure applet's numeric ID to its name).
    """
    settings, nxt = {}, {}
    if isinstance(props.get("Choices"), dict):
        nxt.update({f"Choices.{k}": v for k, v in props["Choices"].items() if v})
    if props.get("TimeoutAppletName"):
        nxt["TimeoutApplet"] = props["TimeoutAppletName"]
    if props.get("FailureAppletId") and names_by_id:
        name = names_by_id.get(int(props["FailureAppletId"]))
        if name:
            nxt["FailureApplet"] = name
    for k, v in props.items():
        if k in MENU_CONNECTION_KEYS:
            continue
        if _is_next_key(k):
            if v:
                nxt[k] = v
        elif k in ACCOUNT_SPECIFIC or k.endswith("Guid") or k.startswith("NextApplet"):
            continue  # IDs, and NextApplet*Guid / *Url derived from the names
        else:
            settings[k] = v
    return settings, nxt


def split_skill_tagger(model: dict) -> tuple[dict, dict]:
    """Return (settings, next) for a Skill Tagger page model.

    Skills are kept by name (VCC posts them by name; skill IDs are not on the page), dynamic
    skills by data source, each with its necessity and time; both sorted by name, since the
    order means nothing to VCC. The account's lookups (AllSkills, AllMetabaseEntries...) are left out.
    """
    def requirements(items: list | None) -> list[dict]:
        kept = [{"Name": s.get("Name"), "Necessity": s.get("Necessity"),
                 **{k: int(s.get(k) or 0) for k in SKILL_TIME_KEYS}} for s in items or []]
        return sorted(kept, key=lambda s: (str(s["Name"]).lower(), str(s["Name"])))

    settings = {
        "ClearAllSkills": bool(model.get("ClearAllSkills")),
        "Skills": requirements(model.get("Skills")),
        "DynamicSkills": requirements(model.get("DynamicSkills")),
        **{k: model.get(k) or "" for k in SKILL_TAGGER_DATA_SOURCES},
        "InterruptAgentWhilstAway": bool(model.get("InterruptAgentWhilstAway")),
    }
    return settings, ({"NextApplet": model["NextApplet"]} if model.get("NextApplet") else {})


def split_shim(page: dict) -> tuple[dict, dict]:
    """Return (settings, next) for a Route to Interaction Plan page (extract_shim()).

    The target plan and its applet are kept by name: plan IDs differ per account, and the applet is
    in the other plan, so it is a setting, not a connection. The data source is kept as written,
    "$(Name)". The failure applet is in this plan: next["FailureApplet"].
    """
    settings = {
        "RouteType": page["RouteType"],
        "TargetPlan": page["TargetPlan"],
        "TargetApplet": page["TargetApplet"],
        "DataSourceName": page["DataSourceName"],
        **{k: bool(page.get(k)) for k in SHIM_IVR_BOXES},
    }
    return settings, ({"FailureApplet": page["FailureApplet"]} if page.get("FailureApplet") else {})


def schedules_in_metadata(metadata: Any) -> list[dict]:
    """The schedules a Schedule Router editor offers: [{"id", "name", "targets"}], targets being the
    service statuses it lists a mapping field for (in the editor's order)."""
    names = {}
    statuses: dict[str, list[str]] = {}

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            prop = x.get("prop")
            if prop == "Schedule" and isinstance(x.get("options"), list):
                names.update({o["value"]: o.get("label") for o in x["options"] if isinstance(o, dict) and o.get("value")})
            m = SCHEDULE_MAPPING_PROP.match(prop) if isinstance(prop, str) else None
            if m:
                statuses.setdefault(m.group(1), []).append(m.group(2))
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(metadata)
    return [{"id": guid, "name": name, "targets": statuses.get(guid, [])} for guid, name in names.items()]


def option_labels(metadata: Any) -> dict[str, str]:
    """GUID -> label of every dropdown option in an editor page's metadata (e.g. schedule GUID -> name)."""
    labels: dict[str, str] = {}
    if isinstance(metadata, dict):
        for o in metadata.get("options") or []:
            if isinstance(o, dict) and isinstance(o.get("value"), str) and GUID.fullmatch(o["value"]) and o.get("label"):
                labels[o["value"].lower()] = o["label"]
        for v in metadata.values():
            labels.update(option_labels(v))
    elif isinstance(metadata, list):
        for v in metadata:
            labels.update(option_labels(v))
    return labels


def relabel(value: Any, labels: dict[str, str]) -> Any:
    """Replace the GUIDs that labels knows, in values and in dictionary keys, by their labels."""
    if isinstance(value, str):
        return GUID.sub(lambda m: labels.get(m.group(0).lower(), m.group(0)), value)
    if isinstance(value, dict):
        return {relabel(k, labels): relabel(v, labels) for k, v in value.items()}
    if isinstance(value, list):
        return [relabel(v, labels) for v in value]
    return value


def _empty_ref(v: Any) -> bool:
    return v is None or v == "" or v == 0


def resolve_applet_references(applet: str, props: dict, listed: list[dict]) -> tuple[dict, dict, list[str]]:
    """Move applet references held as IDs or GUIDs into connections by applet name.

    For editor-page types import-plan does not write. Return (the other properties, next, problems):
    - a property whose key holds "AppletId" or "AppletGuid" (DefaultAppletId, UnsuccessfulCallAppletId,
      NextAppletIdOnMaximumReached + NextAppletGuidOnMaximumReached) becomes next[key without Id/Guid];
    - a mapping whose values are all applet IDs or GUIDs (Schedule Router ScheduleMappings) becomes
      next["<property>.<key>"].
    References to applets that are not in the plan are reported, unless broken_connection_ids() does.
    """
    by_id = {int(a["id"]): a["name"] for a in listed if a.get("id")}
    by_guid = {str(a["guid"]).lower(): a["name"] for a in listed if a.get("guid")}

    def name_of(v: Any) -> str | None:
        if isinstance(v, int) or (isinstance(v, str) and v.isdigit()):
            return by_id.get(int(v))
        if isinstance(v, str) and GUID.fullmatch(v):
            return by_guid.get(v.lower())
        return None

    rest, nxt, problems = {}, {}, []
    for k, v in props.items():
        if k in ACCOUNT_SPECIFIC:
            rest[k] = v
        elif "AppletId" in k or "AppletGuid" in k:
            if _empty_ref(v):
                continue
            base, name = k.replace("AppletId", "Applet").replace("AppletGuid", "Applet"), name_of(v)
            if name is None:
                if not (k.endswith("Guid") or k == "FailureAppletId"):  # those are in broken_connection_ids()
                    problems.append(f"'{applet}' {k} {v} is not in this plan")
            elif nxt.get(base, name) != name:
                problems.append(f"'{applet}' {base} points to both '{nxt[base]}' and '{name}'")
            else:
                nxt[base] = name
        elif isinstance(v, dict) and any(not _empty_ref(x) for x in v.values()) \
                and all(_empty_ref(x) or name_of(x) for x in v.values()):
            nxt.update({f"{k}.{sub}": name_of(x) for sub, x in v.items() if not _empty_ref(x)})
        else:
            rest[k] = v
    return rest, nxt, problems


def split_data_router(rules: list[dict], default: str | None) -> tuple[dict, dict]:
    """Return (settings, next) for a Data Router: the rules in order without their applet,
    and "RoutingRules.<i>" and "DefaultApplet" by applet name. Rule order matters: VCC
    routes on the first rule that matches."""
    settings = {"RoutingRules": [{k: v for k, v in r.items() if k != "RouteToApplet"} for r in rules]}
    nxt = {f"RoutingRules.{i}": r["RouteToApplet"] for i, r in enumerate(rules) if r.get("RouteToApplet")}
    if default:
        nxt["DefaultApplet"] = default
    return settings, nxt


def find_media_refs(value: Any, path: str = "") -> list[tuple[str, int]]:
    """Find uploaded-media references ({"AppletMedia": {"Id": n}}) and their property paths."""
    refs = []
    if isinstance(value, dict):
        media = value.get("AppletMedia")
        if isinstance(media, dict) and media.get("Id"):
            refs.append((path, media["Id"]))
        for k, v in value.items():
            if k != "AppletMedia":
                refs += find_media_refs(v, f"{path}.{k}" if path else k)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            refs += find_media_refs(v, f"{path}[{i}]")
    return refs


def _clear_media_id(settings: dict, path: str) -> None:
    node: Any = settings
    for part in re.findall(r"[^.\[\]]+", path):
        node = node[int(part)] if isinstance(node, list) else node[part]
    node["AppletMedia"] = {**node["AppletMedia"], "Id": None, "AccountId": 0}


def _strings(v: Any):
    if isinstance(v, str):
        yield v
    elif isinstance(v, dict):
        for x in v.values():
            yield from _strings(x)
    elif isinstance(v, list):
        for x in v:
            yield from _strings(x)


def data_sources_created(applet: dict) -> set[str]:
    """Data sources the applet sets, so they need not exist in the target account beforehand.

    Set Data Source keys; Data Connector outputs: Web Service <Destination> and
    <ResponseCodeDestination>, the fields a Salesforce get returns ("Object|Field",
    or "Object(n)|Field" when several records are retrieved; raw SOQL: the selected fields),
    and the ID of the record a Salesforce create makes ("Object|Id").
    """
    s = applet.get("settings", {})
    found = {ds["Key"] for ds in s.get("DataSources") or [] if ds.get("Key")}
    for text in _strings(s):
        found.update(x.strip() for x in XML_DESTINATION.findall(text))
    crm = s.get("CrmRetrievalViewModel") or {}
    if s.get("Action") == "CreateSalesforceObject":  # the new record's ID ("Account|Id")
        found.update(f for f in s.get("ResponseItems") or [] if f)
    if s.get("Action") == "GetSalesforceData":
        fields = list(s.get("ResponseItems") or [])
        soql = SOQL_SELECT.search(crm.get("Soql") or "") if crm.get("UseRawSoql") else None
        if soql:
            obj = soql.group(2)
            fields = [f"{obj}|{f.strip()}" for f in soql.group(1).split(",")]
        records = int(crm.get("MaxRecords") or 1) if not soql else 1
        for f in fields:
            found.add(f)
            if records > 1 and "|" in f:
                obj, field = f.split("|", 1)
                found.update(f"{obj}({i})|{field}" for i in range(1, records + 1))
    return found


def dial_in_config_empty_selection(applet: dict) -> int | None:
    """The configuration a Dial In Config selects, if that configuration is empty (no applet, alternative
    text or announcement): VCC accepts it (user, 2026-11-26), the copy and the comparison warn."""
    if applet.get("type") != DIAL_IN_CONFIG:
        return None
    selected = applet.get("settings", {}).get("SelectedConfiguration")
    audio = {int(m["property"].split(".")[1]) for m in applet.get("media", [])}
    return selected if selected in dial_in_config_empty(applet["settings"], applet.get("next", {}), audio) else None


def data_sources_used(applet: dict) -> set[str]:
    """Data sources the applet reads: $(...) placeholders, an Announcer's data source,
    a Data Router rule's data source, a Salesforce match data source, the values a
    Salesforce update or create writes, and a Skill Tagger's dynamic skills and personal queue."""
    s = applet.get("settings", {})
    found = set()
    for text in _strings(s):
        found.update(PLACEHOLDER.findall(text))
    for rule in s.get("RoutingRules") or []:
        if rule.get("LeftHandDataSource"):
            found.add(rule["LeftHandDataSource"])
    found = {n for n in found if n.lower() not in RESERVED}
    for key in ("DataSource", "AnnouncementDataSource"):
        if s.get(key):
            found.add(s[key])
    if s.get("IsDynamicVoice") and s.get("VoiceDataSource"):  # Menu voice taken from a data source
        found.add(s["VoiceDataSource"])
    if (s.get("CrmRetrievalViewModel") or {}).get("MatchDataSource"):
        found.add(s["CrmRetrievalViewModel"]["MatchDataSource"])
    for p in (s.get("CrmCreationViewModel") or {}).get("CrmObjectParameters") or []:
        if p.get("DataSourceType") == 0 and p.get("Value"):
            found.add(p["Value"])
    if applet.get("type") == ACD:  # audio named by a data source, and the object pop data source
        found.update(s[k] for k in ACD_DATA_SOURCES if s.get(k))
    if applet.get("type") == SKILL_TAGGER:  # dynamic skills and the personal queue read data sources
        found.update(d["Name"] for d in s.get("DynamicSkills") or [] if d.get("Name"))
        found.update(s[k] for k in SKILL_TAGGER_DATA_SOURCES if s.get(k))
    return found


def collect_data_sources(applet: dict) -> set[str]:
    """All data source names an applet sets or reads."""
    return data_sources_created(applet) | data_sources_used(applet)


def data_sources_needed(applets: list[dict]) -> set[str]:
    """Data sources the plan reads but never sets itself: they should exist in the target account.

    Data source names are not case-sensitive in VCC ("LOG" and "Log" are the same).
    """
    created = {n.lower() for a in applets for n in data_sources_created(a)}
    used = set().union(*(data_sources_used(a) for a in applets)) if applets else set()
    return {n for n in used if n.lower() not in created}


def _safe_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]+', "_", name).strip() or "applet"


def broken_connection_ids(applet: str, props: dict, applets: list[dict]) -> list[str]:
    """Connection GUIDs and IDs that do not point to the applet named, inside this plan."""
    by_guid = {a.get("guid"): a["name"] for a in applets}
    ids = {a.get("id") for a in applets}
    problems = []
    for k, v in props.items():
        if not k.endswith("Guid") or k == "AppletGuid" or not v:
            continue
        base = k[:-4]
        named = props.get(base) or props.get(f"{base}Name")
        if v not in by_guid:
            suffix = f" (should be '{named}')" if named else ""
            problems.append(f"'{applet}' {k} points to a GUID that is not in this plan{suffix}")
        elif isinstance(named, str) and named and named != by_guid[v]:
            problems.append(f"'{applet}' {base} says '{named}' but its GUID is '{by_guid[v]}'")
    failure = props.get("FailureAppletId")
    if failure:
        try:
            known = int(failure) in ids
        except (TypeError, ValueError):
            known = False
        if not known:
            problems.append(f"'{applet}' FailureAppletId {failure} is not in this plan")
    return problems


def export_applet(portal: PortalClient, plan_id: str, applet: dict, media_dir: Path,
                  listed: list[dict] | None = None) -> dict:
    entry: dict[str, Any] = {"name": applet["name"], "kind": applet["appletType"]}
    config_url = applet.get("configUrl") or ""

    if config_url.startswith("DataRetriever"):
        dc = portal.get_data_connector(applet)
        model = dc["model"]
        entry["type"] = "DataRetriever"
        settings = {}
        for k, v in model.items():
            if k in DC_NOT_CONFIG or _is_next_key(k) or k.endswith("Url"):
                continue
            settings[k] = {s: x for s, x in v.items() if s not in DC_SUB_NOT_CONFIG} if isinstance(v, dict) else v
        settings.update(dc["fields"])
        entry["settings"] = settings
        entry["next"] = {k: v for k, v in model.items() if _is_next_key(k) and v}
        return entry

    if config_url.startswith("SkillPersonalQueueTagger"):
        entry["type"] = SKILL_TAGGER
        entry["settings"], entry["next"] = split_skill_tagger(portal.get_skill_tagger(applet))
        return entry

    if config_url.startswith("ACD"):
        page = portal.get_acd(applet)
        entry["type"] = ACD
        entry["settings"], entry["next"] = acd_settings(page)
        media = []
        for slot, path in page["audio"].items():
            content = portal.download_recording(path)
            filename = f"{_safe_filename(applet['name'] + ' ' + slot)}{Path(path).suffix or '.wav'}"
            (media_dir / filename).write_bytes(content)
            # VCC stores the file under a GUID: the uploaded file's name is not kept, so it is compared by content.
            media.append({"property": slot, "file": f"media/{filename}", "name": None,
                          "sha256": hashlib.sha256(content).hexdigest()})
        if media:
            entry["media"] = media
        return entry

    if config_url.lower().startswith(DIC_PAGE):
        page = portal.get_dial_in_config(applet)
        entry["type"] = DIAL_IN_CONFIG
        entry["settings"], entry["next"] = dial_in_config_settings(page)
        media = []
        for i, c in sorted(page["configs"].items()):
            if not c["Audio"]:
                continue
            content = portal.download_recording(c["Audio"])
            filename = f"{_safe_filename(applet['name'] + ' announcement ' + str(i))}{Path(c['Audio']).suffix or '.wav'}"
            (media_dir / filename).write_bytes(content)
            # Stored under a GUID, as for an ACD: the uploaded file's name is not kept; compared by content.
            media.append({"property": f"Announcement.{i}", "file": f"media/{filename}", "name": None,
                          "sha256": hashlib.sha256(content).hexdigest()})
        if media:
            entry["media"] = media
        return entry

    if config_url.lower().startswith("shim.aspx"):
        entry["type"] = SHIM
        entry["settings"], entry["next"] = split_shim(portal.get_shim(applet))
        return entry

    if config_url.startswith("DataRouter"):
        dr = portal.get_data_router(applet)
        entry["type"] = DATA_ROUTER
        entry["settings"], entry["next"] = split_data_router(dr["rules"], dr["default"])
        return entry

    state = portal.get_applet_state(plan_id, applet)
    props = state["appletProperties"]
    entry["type"] = props.get("AppletType")
    if entry["type"] == SCHEDULE_ROUTER:
        # The schedule's service statuses, as the editor lists them: what a copy must find in the target.
        own = next((x for x in schedules_in_metadata(state.get("metadata")) if x["id"] == props.get("Schedule")), None)
        entry["scheduleStatuses"] = own["targets"] if own else []
    listed = listed or []
    problems = broken_connection_ids(applet["name"], props, listed) if listed else []
    # Types import-plan writes keep their settings as VCC sends them. The others are only exported
    # and compared, so their account-specific references are kept by name: applets, and GUIDs the
    # page's dropdowns label (e.g. a Schedule Router's schedule).
    by_name = entry["type"] not in IMPORTABLE_EDITOR_TYPES
    resolved: dict = {}
    if by_name:
        props, resolved, unresolved = resolve_applet_references(applet["name"], props, listed)
        problems += unresolved
        # A Schedule Router maps "<schedule GUID>:<status>"; its own schedule is a setting of its own,
        # so its statuses are kept as "ScheduleMappings.<status>" and compare across schedules.
        own = f"ScheduleMappings.{props.get('Schedule')}:"
        resolved = {("ScheduleMappings." + k[len(own):]) if k.startswith(own) else k: v for k, v in resolved.items()}
    settings, nxt = split_connections(props, {a["id"]: a["name"] for a in listed if a.get("id")})
    nxt.update(resolved)
    if by_name:
        labels = option_labels(state.get("metadata"))
        settings, nxt = relabel(settings, labels), relabel(nxt, labels)
    if problems:
        entry["connectionProblems"] = problems
    entry["settings"] = settings
    entry["next"] = nxt

    media = []
    refs = find_media_refs(settings)
    for path, media_id in refs:
        info = portal.get_media_info(media_id)
        ext = Path(info.get("url") or "").suffix or ".wav"
        stem = _safe_filename(applet["name"] if len(refs) == 1 else f"{applet['name']} {path}")
        filename = f"{stem}{ext}"
        content = portal.download_media(info["url"])
        (media_dir / filename).write_bytes(content)
        media.append({"property": path, "file": f"media/{filename}", "name": info.get("name"),
                      "sha256": hashlib.sha256(content).hexdigest()})
        _clear_media_id(settings, path)
        if by_name:
            # The file's storage path holds the account number; the audio is compared by name and content.
            settings.pop(f"{path}Path", None)
    if media:
        entry["media"] = media
    return entry


def _used_schedules(schedules: Any, applets: list[dict], log: Callable[[str], None]) -> list[dict]:
    """The definitions of the schedules the plan's Schedule Routers use (schedules: a SchedulesClient for
    the account, admin:read), so a copy can clone one the target account lacks. None without the API."""
    names = {a["settings"].get("Schedule") for a in applets if a.get("type") == SCHEDULE_ROUTER} - {None, ""}
    if not names or schedules is None:
        return []
    from vccflow.schedules import body_of  # schedules imports portal, as this module does
    from vccflow.scoped_api import ApiError
    try:
        listed = schedules.list()
    except ApiError as e:
        log(f"    WARNING: schedule definitions not exported ({e}): a copy cannot clone a schedule the target lacks")
        return []
    found = [body_of(x) for x in listed if x.get("name") in names]
    for n in sorted(names - {x["name"] for x in found}):
        log(f"    WARNING: schedule '{n}' used by a Schedule Router is not in the account's schedule list")
    return sorted(found, key=lambda x: x["name"])


def export_plan(
    portal: PortalClient,
    plan_id: str,
    plan_name: str,
    out_dir: Path,
    metadata: dict | None = None,
    log: Callable[[str], None] = print,
    schedules: Any = None,
) -> dict:
    """Export a plan to out_dir/plan.json (+ media/). Return the plan document.

    schedules (a SchedulesClient for the account): also save the definitions of the schedules the
    plan's Schedule Routers use, in requires.schedules, for a copy to clone where they are missing."""
    media_dir = out_dir / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    listing = portal.list_applets(plan_id)
    listed = listing.get("applets", [])

    applets = []
    for a in sorted(listing.get("applets", []), key=lambda x: x["name"].lower()):
        log(f"  {a['name']} ({a['appletType']})")
        try:
            applets.append(export_applet(portal, plan_id, a, media_dir, listed))
        except PortalError as e:
            log(f"    NOTE: cannot be imported yet, saving its editor page fields: {e}")
            entry = {"name": a["name"], "kind": a["appletType"], "unsupported": str(e)}
            try:
                entry["form"] = portal.get_applet_form(a)  # still lets exports be compared
            except PortalError as form_error:
                log(f"    WARNING: could not read its editor page: {form_error}")
            applets.append(entry)

    names = {a["name"] for a in applets}
    for a in applets:
        for key, target in a.get("next", {}).items():
            if target not in names:
                log(f"    WARNING: {a['name']}.{key} points to '{target}', which is not in this plan")

    initial = portal.get_initial_applet(plan_id)
    if initial is None:
        log("    WARNING: the plan has no initial applet")
    used_schedules = _used_schedules(schedules, applets, log)
    data_sources = sorted(set().union(*(collect_data_sources(a) for a in applets)) if applets else set())
    doc = {
        "format": FORMAT,
        "source": {
            "account": portal.account.name,
            "planId": plan_id,
            "planName": plan_name,
            "exportedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
        "plan": {
            "name": plan_name,
            "initialApplet": initial,
            **({k: metadata.get(k) for k in PLAN_SETTINGS if k in metadata} if metadata else {}),
        },
        "requires": {"dataSources": data_sources, "dataSourcesNeeded": sorted(data_sources_needed(applets)),
                     **({"schedules": used_schedules} if used_schedules else {})},
        "applets": applets,
    }
    (out_dir / "plan.json").write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    if not any(media_dir.iterdir()):
        media_dir.rmdir()
    return doc
