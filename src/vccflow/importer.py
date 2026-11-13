"""Import a plan exported by export.py into an existing interaction plan.

Order matters: every applet is created first, so the next-applet names exist
when the settings are saved. Then each applet is saved (connections by name,
with the target's GUIDs), audio files are uploaded, the initial applet is set,
and the target plan is exported again and compared with the source.

Everything that can be checked is checked before the first write. If a write
fails, the applets created by this import are deleted again.
"""

from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path
from typing import Callable

from vccflow import export
from vccflow.portal import MAX_MEDIA_BYTES, PortalClient, PortalError, data_connector_form, data_router_form, data_router_missing_targets

DATA_CONNECTOR = "DataRetriever"
DATA_ROUTER = export.DATA_ROUTER
# Auxiliary Set Data Source that makes the data sources the target account lacks exist. VCC
# refuses to save a Data Router rule whose data source is not in the account's list ("Rule 1:
# Please select a Data Source from the list"). A data source is in the list only while some
# applet sets it: deleting this applet removes them again (tested 2026-11-13), so it is kept.
# Nothing leads to it, so it never runs. "zz" sorts it last; the same name is reused next time.
# It is created only when a data source is missing. One in the source plan is not copied as an
# applet: its keys that the target account lacks go into the target's own auxiliary applet.
AUX_SDS = "zz vccflow Aux Data Sources"
# Applet types whose editor saves through Applet/Save (the React editor).
EDITOR_TYPES = {"DataAnnouncer", "SetDataSource", "Menu"}


def load_plan(plan_dir: Path) -> dict:
    path = plan_dir / "plan.json"
    if not path.exists():
        raise PortalError(f"{path} not found; export the plan first (vccflow export-plan)")
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc.get("format") != export.FORMAT:
        raise PortalError(f"{path}: format {doc.get('format')!r}, expected {export.FORMAT!r}")
    return doc


def unsupported_reason(applet: dict) -> str | None:
    """Why this applet cannot be imported yet, or None if it can."""
    if applet.get("unsupported"):
        return f"was not exported: {applet['unsupported']}"
    if applet["type"] not in EDITOR_TYPES | {DATA_CONNECTOR, DATA_ROUTER}:
        return f"applet type {applet['type']} is not supported for import yet"
    if applet["type"] == DATA_CONNECTOR:
        try:
            data_connector_form(applet["settings"], applet.get("next", {}))
        except PortalError as e:
            return str(e)
    if applet["type"] == DATA_ROUTER and applet["settings"].get("RoutingRules"):  # no rules: created, not saved
        try:
            data_router_form(applet["settings"], applet.get("next", {}))
        except PortalError as e:
            return str(e)
    return None


def drop_unsupported(doc: dict) -> tuple[dict, list[str]]:
    """Return (doc without the applets that cannot be imported, notes on what was left out).

    Connections to a left-out applet, and the initial applet if it is one, are left unset.
    """
    skipped = {a["name"]: r for a in doc["applets"] if (r := unsupported_reason(a))}
    notes = [f"skipped '{name}': {reason}" for name, reason in skipped.items()]
    applets = []
    for a in doc["applets"]:
        if a["name"] in skipped:
            continue
        a = copy.deepcopy(a)
        for key, target in list(a.get("next", {}).items()):
            if target in skipped:
                del a["next"][key]
                notes.append(f"'{a['name']}'.{key} pointed to skipped '{target}': left unset")
        applets.append(a)
    plan = dict(doc["plan"])
    if plan.get("initialApplet") in skipped:
        notes.append(f"initial applet '{plan['initialApplet']}' was skipped: not set")
        plan["initialApplet"] = None
    return {**doc, "plan": plan, "applets": applets}, notes


def check_plan(doc: dict, plan_dir: Path, existing: set[str]) -> list[str]:
    """Return the problems that would stop an import into a plan with these applet names."""
    problems = []
    names = {a["name"] for a in doc["applets"]}
    for name in sorted(names & existing):
        problems.append(f"'{name}' already exists in the target plan")
    for a in doc["applets"]:
        reason = unsupported_reason(a)
        if reason:
            problems.append(f"'{a['name']}': {reason}")
            if a.get("unsupported"):
                continue
        for key, target in a.get("next", {}).items():
            if target not in names:
                problems.append(f"'{a['name']}'.{key} points to '{target}', which is not in the plan")
        for m in a.get("media", []):
            f = plan_dir / m["file"]
            if not f.exists():
                problems.append(f"'{a['name']}': media file {f} is missing")
            elif f.stat().st_size > MAX_MEDIA_BYTES:
                problems.append(f"'{a['name']}': media file {f} is larger than 10 MB")
    initial = doc["plan"].get("initialApplet")
    if initial and initial not in names:
        problems.append(f"Initial applet '{initial}' is not in the plan")
    return problems


def check_data_sources(applets: list[dict], existing: set[str]) -> tuple[list[str], list[str]]:
    """Return (missing, notes) for the data sources the plan reads but does not set itself.

    Names are not case-sensitive: one that exists with other letter case counts as
    existing, and the portal keeps the account's spelling (e.g. "LOG" becomes "Log").
    """
    lower = {e.lower(): e for e in existing}
    users: dict[str, list[str]] = {}
    for a in applets:
        for n in export.data_sources_used(a):
            users.setdefault(n.lower(), []).append(a["name"])
    missing, notes = [], []
    for name in sorted(export.data_sources_needed(applets)):
        found = lower.get(name.lower())
        if found is None:
            used_by = ", ".join(sorted(set(users.get(name.lower(), []))))
            missing.append(f"data source '{name}' is used by {used_by} but does not exist in the target account")
        elif found != name:
            notes.append(f"data source '{name}' exists in the target account as '{found}'")
    for name in sorted({n for a in applets for n in export.data_sources_created(a)}):
        found = lower.get(name.lower())
        if found is not None and found != name:
            notes.append(f"data source '{name}' set by the plan exists in the target account as '{found}'")
    return missing, notes


def _set_path(props: dict, path: str, media_id: int) -> None:
    node = props
    for part in path.split("."):
        node = node[part]
    node["AppletMedia"] = {**(node.get("AppletMedia") or {}), "Id": media_id}


def _comparable(applet: dict) -> dict:
    """What must match between source and copy: type, settings, connections, media names."""
    settings = json.loads(json.dumps(applet.get("settings", {})).replace("\\r\\n", "\\n"))
    return {
        "type": applet.get("type"),
        "settings": settings,
        "next": applet.get("next", {}),
        "media": [(m["property"], m.get("name")) for m in applet.get("media", [])],
    }


def compare(source: dict, copy: dict, notes: list[str] | None = None) -> list[str]:
    diffs = []
    src = {a["name"]: _comparable(a) for a in source["applets"]}
    dst = {a["name"]: _comparable(a) for a in copy["applets"]}
    for name, want in src.items():
        got = dst.get(name)
        if got is None:
            diffs.append(f"'{name}' is missing in the copy")
            continue
        for part in ("type", "next", "media"):
            if got[part] != want[part]:
                diffs.append(f"'{name}' {part}: source {want[part]}, copy {got[part]}")
        for k in sorted(set(want["settings"]) | set(got["settings"])):
            w, g = want["settings"].get(k), got["settings"].get(k)
            if w == g:
                continue
            if notes is not None and json.dumps(w).lower() == json.dumps(g).lower():
                notes.append(f"'{name}' setting {k} differs only in letter case "
                             "(the portal uses the account's spelling of data source names)")
            else:
                diffs.append(f"'{name}' setting {k} differs")
    if source["plan"].get("initialApplet") != copy["plan"].get("initialApplet"):
        diffs.append(f"initial applet: source {source['plan'].get('initialApplet')!r}, "
                     f"copy {copy['plan'].get('initialApplet')!r}")
    return diffs


def import_plan(
    portal: PortalClient,
    plan_dir: Path,
    target_plan_id: str,
    target_plan_name: str,
    dry_run: bool = False,
    skip_unsupported: bool = False,
    require_data_sources: bool = False,
    log: Callable[[str], None] = print,
) -> list[str]:
    """Copy the exported plan in plan_dir into the target plan. Return the read-back differences.

    With skip_unsupported, applets that cannot be imported yet are left out (see drop_unsupported).
    Data sources the plan reads but does not set should exist in the target account. Missing
    ones are a warning (the portal saves such applets anyway); with require_data_sources
    they stop the import before any write.
    """
    doc = load_plan(plan_dir)
    # The source's auxiliary applet is not copied: it would count as setting its keys and hide them
    # from the check below. Its keys are checked against the target account instead.
    source_aux = next((a for a in doc["applets"] if a["name"] == AUX_SDS), None)
    aux_keys = [d["Key"] for d in (source_aux or {}).get("settings", {}).get("DataSources") or [] if d.get("Key")]
    if source_aux:
        doc = {**doc, "applets": [a for a in doc["applets"] if a["name"] != AUX_SDS]}
        log(f"NOTE: '{AUX_SDS}' in the source plan is not copied as an applet; its {len(aux_keys)} data sources "
            "are checked against the target account")
    if skip_unsupported:
        doc, notes = drop_unsupported(doc)
        for n in notes:
            log(f"NOTE: {n}")
    listing = portal.list_applets(target_plan_id)
    if listing.get("name") != target_plan_name:
        raise PortalError(f"Target plan is '{listing.get('name')}', expected '{target_plan_name}'")
    problems = check_plan(doc, plan_dir, {a["name"] for a in listing.get("applets", [])})
    existing = portal.list_data_sources(target_plan_id)
    to_create: list[str] = []
    if existing is None:
        log("WARNING: could not read the target account's data sources (no applet to read them from); not checked")
    else:
        missing, notes = check_data_sources(doc["applets"], existing)
        for n in notes:
            log(f"NOTE: {n}")
        if require_data_sources:
            problems += missing
        else:
            for m in missing:
                log(f"WARNING: {m}")
            lower = {e.lower() for e in existing}
            wanted = {n.lower(): n for n in [*aux_keys, *export.data_sources_needed(doc["applets"])]}
            to_create = sorted((n for k, n in wanted.items() if k not in lower), key=str.lower)
            already = [k for k in aux_keys if k.lower() in lower]
            if already:
                log(f"NOTE: {len(already)} data sources of the source's '{AUX_SDS}' already exist in the target "
                    f"account, not added: {', '.join(already)}")
    aux_in_target = any(a["name"] == AUX_SDS for a in listing.get("applets", []))
    if problems:
        raise PortalError("Cannot import:\n  " + "\n  ".join(problems))
    applets = doc["applets"]
    log(f"Checks passed: {len(applets)} applets from '{doc['plan']['name']}' into '{target_plan_name}'")
    missing_targets = {a["name"]: m for a in applets if a["type"] == DATA_ROUTER
                       and (m := data_router_missing_targets(a["settings"], a.get("next", {})))}
    verb = "would be" if dry_run else "is"
    empty_routers = {a["name"] for a in applets if a["type"] == DATA_ROUTER and not a["settings"].get("RoutingRules")}
    for name in sorted(empty_routers):
        missing_targets.pop(name, None)
        log(f"WARNING: Data Router '{name}' has no rules: it {verb} created empty and not saved "
            "(VCC only saves a Data Router whose first rule has a data source)."
            + (f" Its default applet '{nxt}' is not set." if (nxt := next(a for a in applets if a['name'] == name)
                                                             .get("next", {}).get("DefaultApplet")) else ""))
    for name, m in missing_targets.items():
        log(f"WARNING: Data Router '{name}' {verb} copied with no destination applet for: {', '.join(m)}. "
            "Set them in the portal.")
    if to_create:
        where = "added to the existing" if aux_in_target else "set in a new"
        log(f"NOTE: the {len(to_create)} missing data sources {'would be' if dry_run else 'are'} {where} auxiliary "
            f"Set Data Source '{AUX_SDS}', which stays in the plan (VCC keeps a data source only while an applet sets "
            f"it; nothing leads to this applet): {', '.join(to_create)}")
    if dry_run:
        for a in applets:
            log(f"  would create {a['name']} ({a['type']})")
        log(f"  would set the initial applet to {doc['plan'].get('initialApplet')!r}")
        return []

    created: list[str] = []
    try:
        if to_create:
            log(f"0. Making the data sources exist through '{AUX_SDS}'")
            if not aux_in_target:
                portal.create_applet(target_plan_id, "SetDataSource", AUX_SDS)
                created.append(AUX_SDS)
            _save_aux_sds(portal, target_plan_id, to_create)
        log("1. Creating applets")
        for a in applets:
            portal.create_applet(target_plan_id, a["type"], a["name"])
            created.append(a["name"])
            log(f"   {a['name']} ({a['type']})")
        by_name = {a["name"]: a for a in portal.list_applets(target_plan_id)["applets"]}

        log("2. Saving settings and connections")
        for a in applets:
            nxt = a.get("next", {})
            if a["type"] == DATA_CONNECTOR:
                portal.save_data_connector(target_plan_id, a["name"], a["settings"], nxt)
            elif a["type"] == DATA_ROUTER and a["name"] in empty_routers:
                log(f"   {a['name']}: empty Data Router, created and not saved")
                continue
            elif a["type"] == DATA_ROUTER:
                portal.save_data_router(target_plan_id, a["name"], a["settings"], nxt)
                if a["name"] in missing_targets:
                    log(f"   WARNING: '{a['name']}' has been copied with missing targets: "
                        f"{', '.join(missing_targets[a['name']])}")
            else:
                props = dict(portal.get_applet_state(target_plan_id, by_name[a["name"]])["appletProperties"])
                props.update(json.loads(json.dumps(a["settings"])))
                for m in a.get("media", []):
                    path = plan_dir / m["file"]
                    filename = f"{m['name']}{path.suffix}" if m.get("name") else path.name
                    media_id = portal.upload_media(target_plan_id, a["name"], path, filename)
                    _set_path(props, m["property"], media_id)
                    log(f"   uploaded {path.name} as '{filename}'")
                if any(k.startswith("Choices.") for k in nxt):
                    props["Choices"] = {}
                for key, target in nxt.items():
                    t = by_name[target]
                    if key.startswith("Choices."):
                        props["Choices"][key[len("Choices."):]] = target
                    elif key == "TimeoutApplet":
                        props["TimeoutAppletName"], props["TimeoutAppletGuid"] = target, t.get("guid")
                    elif key == "FailureApplet":
                        props["FailureAppletId"], props["FailureAppletGuid"] = t.get("id"), t.get("guid")
                    else:
                        props[key] = target
                        props[f"{key}Guid"] = t.get("guid")
                portal.save_applet(target_plan_id, a["name"], props)
            log(f"   {a['name']}")

        initial = doc["plan"].get("initialApplet")
        if initial:
            log(f"3. Setting the initial applet to '{initial}'")
            portal.set_initial_applet(target_plan_id, initial)
    except Exception:
        log(f"Import failed; deleting the {len(created)} applets it created")
        for name in reversed(created):
            try:
                portal.delete_applet(target_plan_id, name)
            except PortalError as e:
                log(f"   could not delete '{name}': {e}")
        raise

    log("4. Reading the copy back and comparing")
    with tempfile.TemporaryDirectory() as tmp:
        copy = export.export_plan(portal, target_plan_id, target_plan_name, Path(tmp), log=lambda _: None)
    # The copy holds the target's other applets too; compare only the imported ones.
    copy["applets"] = [a for a in copy["applets"] if a["name"] in {x["name"] for x in applets}]
    notes: list[str] = []
    diffs = compare(doc, copy, notes)
    for n in notes:
        log(f"NOTE: {n}")
    return diffs


def _save_aux_sds(portal: PortalClient, plan_id: str, names: list[str]) -> None:
    """Save the auxiliary Set Data Source with a key for each name, keeping the keys it already has."""
    applet = portal.find_applet(plan_id, AUX_SDS)
    props = dict(portal.get_applet_state(plan_id, applet)["appletProperties"])
    current = [d for d in props.get("DataSources") or [] if d.get("Key")]
    have = {d["Key"].lower() for d in current}
    props["DataSources"] = current + [{"Key": n, "Value": ""} for n in names if n.lower() not in have]
    portal.save_applet(plan_id, AUX_SDS, props)
