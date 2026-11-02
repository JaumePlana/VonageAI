"""Compare two interaction plans, in the same account or in two accounts (read-only).

Reports, in both directions: applets missing on either side, applet types, paths
(connections), settings, audio files, the initial applet, the reporting group, connection
IDs that do not resolve inside their own plan, and data sources the target lacks.

Applet types vccflow cannot export yet are compared through the fields of their
editor page, so differences there (e.g. Skill Tagger skills) are still reported.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from pathlib import Path
from typing import Any, Iterator

from vccflow import export, importer
from vccflow.portal import PortalClient

# Settings whose values may hold credentials: report that they differ, not their values.
SENSITIVE = re.compile(r"ApiInteractionDefinition|secret|password|token|bearer|authorization|api_?key", re.I)
MAX_SHOWN = 160


def _is_empty(v: Any) -> bool:
    return v is None or v == "" or v == [] or v == {}


def _norm(v: Any) -> str:
    """Scalar as text: the portal returns 10 or "10" for the same setting, and CRLF or LF."""
    return str(v).replace("\r\n", "\n").strip()


def _diff(a: Any, b: Any, path: str = "") -> Iterator[tuple[str, Any, Any]]:
    """Yield (path, a, b) for each difference; empty values all count as "not set"."""
    if _is_empty(a) and _is_empty(b):
        return
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b), key=str):
            yield from _diff(a.get(k), b.get(k), f"{path}.{k}" if path else str(k))
    elif isinstance(a, list) and isinstance(b, list):
        if json.dumps(a, sort_keys=True, default=str) != json.dumps(b, sort_keys=True, default=str):
            yield path, a, b
    elif isinstance(a, (dict, list)) or isinstance(b, (dict, list)) or _norm(a) != _norm(b):
        yield path, a, b


def _show(v: Any) -> str:
    if _is_empty(v):
        return "(not set)"
    text = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    return text if len(text) <= MAX_SHOWN else f"{text[:MAX_SHOWN]}..."


def _case_only(a: Any, b: Any) -> bool:
    return json.dumps(a, default=str).lower() == json.dumps(b, default=str).lower()


def _setting_findings(applet: str, a: Any, b: Any, report: dict, what: str = "setting") -> None:
    for path, x, y in _diff(a, b):
        if SENSITIVE.search(path) or SENSITIVE.search(str(x)) or SENSITIVE.search(str(y)):
            report["differences"].append(f"'{applet}' {what} {path} differs (values hidden: may hold credentials)")
        elif _case_only(x, y):
            report["notes"].append(f"'{applet}' {what} {path} differs only in letter case: {_show(x)} / {_show(y)}")
        else:
            report["differences"].append(f"'{applet}' {what} {path}: source {_show(x)} | target {_show(y)}")


def read_plan(portal: PortalClient, plan_id: str, plan_name: str, metadata: dict | None = None) -> dict:
    """Export a plan from VCC to a temporary folder and return the document."""
    with tempfile.TemporaryDirectory() as tmp:
        return export.export_plan(portal, plan_id, plan_name, Path(tmp), metadata=metadata, log=lambda _: None)


def load_export(folder: Path) -> dict:
    """Read a saved export. Exports made before audio hashes existed get them from their files."""
    doc = importer.load_plan(folder)
    for a in doc["applets"]:
        for m in a.get("media", []):
            if "sha256" not in m and (folder / m["file"]).exists():
                m["sha256"] = hashlib.sha256((folder / m["file"]).read_bytes()).hexdigest()
    return doc


def compare_docs(source: dict, target: dict) -> dict:
    """Return {"differences": [...], "problems": [...], "notes": [...]} for two read_plan() documents."""
    report: dict[str, list[str]] = {"differences": [], "problems": [], "notes": []}
    src = {a["name"]: a for a in source["applets"]}
    dst = {a["name"]: a for a in target["applets"]}

    for name in sorted(src.keys() - dst.keys()):
        report["differences"].append(f"applet '{name}' ({src[name].get('kind')}) is missing in the target plan")
    for name in sorted(dst.keys() - src.keys()):
        report["differences"].append(f"applet '{name}' ({dst[name].get('kind')}) is only in the target plan")

    for name in sorted(src.keys() & dst.keys()):
        a, b = src[name], dst[name]
        if a.get("kind") != b.get("kind"):
            report["differences"].append(f"'{name}' applet type: source {a.get('kind')} | target {b.get('kind')}")
            continue
        an, bn = a.get("next", {}), b.get("next", {})
        for key in sorted(set(an) | set(bn)):
            if key not in bn:
                report["differences"].append(f"'{name}' path {key} is missing in the target (source goes to '{an[key]}')")
            elif key not in an:
                report["differences"].append(f"'{name}' path {key} is missing in the source (target goes to '{bn[key]}')")
            elif an[key] != bn[key]:
                report["differences"].append(f"'{name}' path {key} is different: source '{an[key]}' | target '{bn[key]}'")
        if a.get("unsupported") or b.get("unsupported"):
            if "form" in a and "form" in b:
                report["notes"].append(f"'{name}' ({a.get('kind')}) compared through its editor page fields only")
                _setting_findings(name, a["form"], b["form"], report, "field")
            else:
                report["notes"].append(f"'{name}' ({a.get('kind')}) settings not compared: "
                                       "its editor page fields are not in the export (made before they were saved)")
        else:
            _setting_findings(name, a.get("settings"), b.get("settings"), report)
        am = {m["property"]: m for m in a.get("media", [])}
        bm = {m["property"]: m for m in b.get("media", [])}
        for prop in sorted(set(am) | set(bm)):
            if prop not in bm or prop not in am:
                side = "target" if prop not in bm else "source"
                report["differences"].append(f"'{name}' audio {prop} is missing in the {side}")
            elif am[prop].get("name") != bm[prop].get("name"):
                report["differences"].append(f"'{name}' audio name: source '{am[prop].get('name')}' | target '{bm[prop].get('name')}'")
            elif am[prop].get("sha256") and bm[prop].get("sha256") and am[prop]["sha256"] != bm[prop]["sha256"]:
                report["differences"].append(f"'{name}' audio '{am[prop].get('name')}' has different content")

    for key, label in (("initialApplet", "initial applet"), ("reportingGroupName", "reporting group")):
        x, y = source["plan"].get(key), target["plan"].get(key)
        if x != y and not (_is_empty(x) and _is_empty(y)):
            report["differences"].append(f"{label}: source {_show(x)} | target {_show(y)}")

    for side, doc in (("source", source), ("target", target)):
        names = {a["name"] for a in doc["applets"]}
        for a in doc["applets"]:
            for key, to in a.get("next", {}).items():
                if to not in names:
                    report["problems"].append(f"{side}: '{a['name']}' path {key} goes to '{to}', which is not in the plan")
            report["problems"] += [f"{side}: {p}" for p in a.get("connectionProblems", [])]
    return report


def compare_plans(source_portal: PortalClient, source_id: str, source_name: str,
                  target_portal: PortalClient, target_id: str, target_name: str,
                  source_metadata: dict | None = None, target_metadata: dict | None = None) -> dict:
    source = read_plan(source_portal, source_id, source_name, source_metadata)
    target = read_plan(target_portal, target_id, target_name, target_metadata)
    report = compare_docs(source, target)
    existing = target_portal.list_data_sources(target_id)
    if existing is None:
        report["notes"].append("could not read the target account's data sources: not checked")
    else:
        readable = [a for a in source["applets"] if not a.get("unsupported")]
        missing, notes = importer.check_data_sources(readable, existing)
        report["problems"] += missing
        report["notes"] += notes
    return report


def compare_exports(source_dir: Path, target_dir: Path) -> dict:
    """Compare two saved exports. The account's data source list is not in an export, so it is not checked."""
    report = compare_docs(load_export(source_dir), load_export(target_dir))
    report["notes"].append("compared saved exports: data sources in the target account not checked")
    return report


def format_report(report: dict) -> str:
    lines = []
    for key, title in (("differences", "DIFFERENCES"), ("problems", "PROBLEMS"), ("notes", "NOTES")):
        lines.append(f"{title} ({len(report[key])})")
        lines += [f"  {x}" for x in report[key]]
    return "\n".join(lines)
