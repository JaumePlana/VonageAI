"""Import a plan exported by export.py into an existing interaction plan.

Order matters: every applet is created first, so the next-applet names exist
when the settings are saved. Then each applet is saved (connections by name,
with the target's GUIDs), audio files are uploaded, the initial applet is set,
and the target plan is exported again and compared with the source.

Everything that can be checked is checked before the first write. If a write
fails, the applets created by this import are deleted again.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Callable

from vccflow import export
from vccflow.portal import MAX_MEDIA_BYTES, PortalClient, PortalError, data_connector_form

DATA_CONNECTOR = "DataRetriever"
# Applet types whose editor saves through Applet/Save (the React editor).
EDITOR_TYPES = {"DataAnnouncer", "SetDataSource"}


def load_plan(plan_dir: Path) -> dict:
    path = plan_dir / "plan.json"
    if not path.exists():
        raise PortalError(f"{path} not found; export the plan first (vccflow export-plan)")
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc.get("format") != export.FORMAT:
        raise PortalError(f"{path}: format {doc.get('format')!r}, expected {export.FORMAT!r}")
    return doc


def check_plan(doc: dict, plan_dir: Path, existing: set[str]) -> list[str]:
    """Return the problems that would stop an import into a plan with these applet names."""
    problems = []
    names = {a["name"] for a in doc["applets"]}
    for name in sorted(names & existing):
        problems.append(f"'{name}' already exists in the target plan")
    for a in doc["applets"]:
        if a.get("unsupported"):
            problems.append(f"'{a['name']}' was not exported: {a['unsupported']}")
            continue
        if a["type"] not in EDITOR_TYPES | {DATA_CONNECTOR}:
            problems.append(f"'{a['name']}': applet type {a['type']} is not supported for import yet")
        for key, target in a.get("next", {}).items():
            if target not in names:
                problems.append(f"'{a['name']}'.{key} points to '{target}', which is not in the plan")
        if a["type"] == DATA_CONNECTOR:
            try:
                data_connector_form(a["settings"], a.get("next", {}))
            except PortalError as e:
                problems.append(f"'{a['name']}': {e}")
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


def compare(source: dict, copy: dict) -> list[str]:
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
            if want["settings"].get(k) != got["settings"].get(k):
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
    log: Callable[[str], None] = print,
) -> list[str]:
    """Copy the exported plan in plan_dir into the target plan. Return the read-back differences."""
    doc = load_plan(plan_dir)
    listing = portal.list_applets(target_plan_id)
    if listing.get("name") != target_plan_name:
        raise PortalError(f"Target plan is '{listing.get('name')}', expected '{target_plan_name}'")
    problems = check_plan(doc, plan_dir, {a["name"] for a in listing.get("applets", [])})
    if problems:
        raise PortalError("Cannot import:\n  " + "\n  ".join(problems))
    applets = doc["applets"]
    log(f"Checks passed: {len(applets)} applets from '{doc['plan']['name']}' into '{target_plan_name}'")
    if dry_run:
        for a in applets:
            log(f"  would create {a['name']} ({a['type']})")
        log(f"  would set the initial applet to {doc['plan'].get('initialApplet')!r}")
        return []

    created: list[str] = []
    try:
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
            else:
                props = dict(portal.get_applet_state(target_plan_id, by_name[a["name"]])["appletProperties"])
                props.update(json.loads(json.dumps(a["settings"])))
                for m in a.get("media", []):
                    path = plan_dir / m["file"]
                    filename = f"{m['name']}{path.suffix}" if m.get("name") else path.name
                    media_id = portal.upload_media(target_plan_id, a["name"], path, filename)
                    _set_path(props, m["property"], media_id)
                    log(f"   uploaded {path.name} as '{filename}'")
                for key, target in nxt.items():
                    props[key] = target
                    props[f"{key}Guid"] = by_name[target].get("guid")
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
    return compare(doc, copy)
