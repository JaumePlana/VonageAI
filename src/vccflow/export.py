"""Export an interaction plan into the portable vccflow plan format.

The format keeps what an engineer configured and drops what belongs to one
account: applet IDs and GUIDs, the plan ID and media IDs. Connections between
applets are kept by applet name, and uploaded audio files are saved next to
the plan file, so the plan can be rebuilt in another plan or account.

Layout: <out_dir>/plan.json and <out_dir>/media/<applet name>.<ext>
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from vccflow.portal import PortalClient, PortalError

FORMAT = "vccflow.plan/1"

# Applet properties that identify an applet in one account; rebuilt on import.
ACCOUNT_SPECIFIC = {
    "DialledNumber", "EntityId", "AppletGuid", "AppletName", "DisplayName",
    "Url", "ConfigurationUrl", "ApplicationType", "AppletType",
}
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
XML_DESTINATION = re.compile(r"<(?:Destination|ResponseCodeDestination)>([^<]+)</")
SOQL_SELECT = re.compile(r"^\s*SELECT\s+(.+?)\s+FROM\s+(\w+)", re.I | re.S)


def _is_next_key(key: str) -> bool:
    return key.startswith("NextApplet") and not key.endswith(("Guid", "Url"))


def split_connections(props: dict) -> tuple[dict, dict]:
    """Return (settings, next) where next maps NextApplet* properties to applet names."""
    settings, nxt = {}, {}
    for k, v in props.items():
        if _is_next_key(k):
            if v:
                nxt[k] = v
        elif k in ACCOUNT_SPECIFIC or k.endswith("Guid") or k.startswith("NextApplet"):
            continue  # IDs, and NextApplet*Guid / *Url derived from the names
        else:
            settings[k] = v
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
    <ResponseCodeDestination>, and the fields a Salesforce get returns ("Object|Field",
    or "Object(n)|Field" when several records are retrieved; raw SOQL: the selected fields).
    """
    s = applet.get("settings", {})
    found = {ds["Key"] for ds in s.get("DataSources") or [] if ds.get("Key")}
    for text in _strings(s):
        found.update(x.strip() for x in XML_DESTINATION.findall(text))
    crm = s.get("CrmRetrievalViewModel") or {}
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


def data_sources_used(applet: dict) -> set[str]:
    """Data sources the applet reads: $(...) placeholders, an Announcer's data source,
    a Salesforce match data source and the values a Salesforce update writes."""
    s = applet.get("settings", {})
    found = set()
    for text in _strings(s):
        found.update(PLACEHOLDER.findall(text))
    if s.get("DataSource"):
        found.add(s["DataSource"])
    if (s.get("CrmRetrievalViewModel") or {}).get("MatchDataSource"):
        found.add(s["CrmRetrievalViewModel"]["MatchDataSource"])
    for p in (s.get("CrmCreationViewModel") or {}).get("CrmObjectParameters") or []:
        if p.get("DataSourceType") == 0 and p.get("Value"):
            found.add(p["Value"])
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


def export_applet(portal: PortalClient, plan_id: str, applet: dict, media_dir: Path) -> dict:
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

    props = portal.get_applet_state(plan_id, applet)["appletProperties"]
    entry["type"] = props.get("AppletType")
    settings, nxt = split_connections(props)
    entry["settings"] = settings
    entry["next"] = nxt

    media = []
    refs = find_media_refs(settings)
    for path, media_id in refs:
        info = portal.get_media_info(media_id)
        ext = Path(info.get("url") or "").suffix or ".wav"
        stem = _safe_filename(applet["name"] if len(refs) == 1 else f"{applet['name']} {path}")
        filename = f"{stem}{ext}"
        (media_dir / filename).write_bytes(portal.download_media(info["url"]))
        media.append({"property": path, "file": f"media/{filename}", "name": info.get("name")})
        _clear_media_id(settings, path)
    if media:
        entry["media"] = media
    return entry


def export_plan(
    portal: PortalClient,
    plan_id: str,
    plan_name: str,
    out_dir: Path,
    metadata: dict | None = None,
    log: Callable[[str], None] = print,
) -> dict:
    """Export a plan to out_dir/plan.json (+ media/). Return the plan document."""
    media_dir = out_dir / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    listing = portal.list_applets(plan_id)

    applets = []
    for a in sorted(listing.get("applets", []), key=lambda x: x["name"].lower()):
        log(f"  {a['name']} ({a['appletType']})")
        try:
            applets.append(export_applet(portal, plan_id, a, media_dir))
        except PortalError as e:
            log(f"    WARNING: not exported: {e}")
            applets.append({"name": a["name"], "kind": a["appletType"], "unsupported": str(e)})

    names = {a["name"] for a in applets}
    for a in applets:
        for key, target in a.get("next", {}).items():
            if target not in names:
                log(f"    WARNING: {a['name']}.{key} points to '{target}', which is not in this plan")

    initial = portal.get_initial_applet(plan_id)
    if initial is None:
        log("    WARNING: the plan has no initial applet")
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
            **({"reportingGroupName": metadata.get("reportingGroupName"),
                "displayInAgentInterface": metadata.get("displayInAgentInterface")} if metadata else {}),
        },
        "requires": {"dataSources": data_sources, "dataSourcesNeeded": sorted(data_sources_needed(applets))},
        "applets": applets,
    }
    (out_dir / "plan.json").write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    if not any(media_dir.iterdir()):
        media_dir.rmdir()
    return doc
