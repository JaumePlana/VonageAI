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
from vccflow.portal import ACD_LABELS, PortalClient, skill_description

# Settings whose values may hold credentials: report that they differ, not their values.
SENSITIVE = re.compile(r"ApiInteractionDefinition|secret|password|token|bearer|authorization|api_?key", re.I)
MAX_SHOWN = 160
PLAN, ONE_SIDE, PAGE_FIELDS = "plan", "one side", "page fields"
# Skill Tagger skill lists: sets of skills by name (VCC ignores their order and letter case).
SKILL_LISTS = {"Skills": "skill", "DynamicSkills": "dynamic skill"}


class Finding(str):
    """A report line that also knows what it is about, so format_report() can group it.

    It is the full line, as before; `applet`, `kind` and `detail` (the line without the
    applet name) say which applet it belongs to, and `section` places lines that belong to
    no single applet (PLAN or ONE_SIDE: applets in one plan only) or that are listed
    together (PAGE_FIELDS).
    """

    applet: str | None
    kind: str | None
    detail: str
    section: str

    def __new__(cls, text: str, applet: str | None = None, kind: str | None = None,
                detail: str | None = None, section: str = PLAN) -> "Finding":
        f = super().__new__(cls, text)
        f.applet, f.kind, f.detail, f.section = applet, kind, detail or text, section
        return f


def _about(name: str, kind: str | None, detail: str, section: str = PLAN) -> Finding:
    """A line about one applet: "'<name>' <detail>"."""
    return Finding(f"'{name}' {detail}", name, kind, detail, section)


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
        if len(a) == len(b) and a and all(isinstance(x, dict) for x in a + b):
            # Same number of entries (e.g. Data Router rules): report each changed field, in order.
            for i, (x, y) in enumerate(zip(a, b)):
                yield from _diff(x, y, f"{path}[{i}]")
        elif json.dumps(a, sort_keys=True, default=str) != json.dumps(b, sort_keys=True, default=str):
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


def _setting_findings(applet: str, kind: str | None, a: Any, b: Any, report: dict, what: str = "setting") -> None:
    for path, x, y in _diff(a, b):
        if SENSITIVE.search(path) or SENSITIVE.search(str(x)) or SENSITIVE.search(str(y)):
            report["differences"].append(_about(applet, kind, f"{what} {path} differs (values hidden: may hold credentials)"))
        elif _case_only(x, y):
            report["notes"].append(_about(applet, kind, f"{what} {path} differs only in letter case: {_show(x)} / {_show(y)}"))
        else:
            report["differences"].append(_about(applet, kind, f"{what} {path}: source {_show(x)} | target {_show(y)}"))


def _requirement(skill: dict) -> str:
    """"optional", "mandatory" or "mandatory for 8 hours", as the Skill Tagger page says it."""
    return skill_description(skill).strip().removeprefix("is ").strip()


def _skill_findings(applet: str, kind: str | None, a: dict, b: dict, report: dict) -> None:
    """Compare the Skill Tagger's skills one by one, by name, rather than as whole lists."""
    for key, what in SKILL_LISTS.items():
        xs = {s["Name"].lower(): s for s in a.get(key) or []}
        ys = {s["Name"].lower(): s for s in b.get(key) or []}
        for k in sorted(set(xs) | set(ys)):
            x, y = xs.get(k), ys.get(k)
            if y is None:
                detail = f"{what} '{x['Name']}' is only in the source ({_requirement(x)})"
            elif x is None:
                detail = f"{what} '{y['Name']}' is only in the target ({_requirement(y)})"
            elif _requirement(x) != _requirement(y):
                detail = f"{what} '{x['Name']}': source {_requirement(x)} | target {_requirement(y)}"
            else:
                if x["Name"] != y["Name"]:
                    report["notes"].append(_about(applet, kind, f"{what} differs only in letter case: "
                                                                f"'{x['Name']}' / '{y['Name']}'"))
                continue
            report["differences"].append(_about(applet, kind, detail))


def _group(g: dict) -> str:
    """"Espagne (13102022002), overflow after 15 s": an ACD group as the report shows it."""
    overflow = g.get("TimedOverflow")
    return f"{g.get('Name')} ({g.get('DisplayId')})" + (f", overflow after {overflow} s" if overflow not in ("", None) else "")


def _acd_group_findings(applet: str, kind: str | None, a: dict, b: dict, report: dict) -> None:
    """Compare an ACD's groups slot by slot (the slot order is the overflow order), rather than as a list."""
    xs = {g["Slot"]: g for g in a.get("Groups") or []}
    ys = {g["Slot"]: g for g in b.get("Groups") or []}
    for slot in sorted(set(xs) | set(ys)):
        x, y = xs.get(slot), ys.get(slot)
        if y is None:
            detail = f"group {slot + 1} {_group(x)} is only in the source"
        elif x is None:
            detail = f"group {slot + 1} {_group(y)} is only in the target"
        elif _group(x) != _group(y):
            detail = f"group {slot + 1}: source {_group(x)} | target {_group(y)}"
        else:
            continue
        report["differences"].append(_about(applet, kind, detail))


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


def _outbound_clid_findings(source: dict, target: dict, report: dict) -> None:
    """The plan's Outbound CLID. A copy does not change it, so a difference stays after copying."""
    a, b = source["plan"], target["plan"]
    if "outboundClidBehaviour" not in a or "outboundClidBehaviour" not in b:
        report["notes"].append("outbound CLID not compared: not available for both plans")
        return
    x, y = a["outboundClidBehaviour"], b["outboundClidBehaviour"]
    if x != y:
        report["differences"].append(f"outbound CLID setting: source {_show(x)} | target {_show(y)}")
    n, m = a.get("outboundClid"), b.get("outboundClid")
    if not any(_diff(n, m)):
        return
    text = f"outbound CLID number: source {_show(n)} | target {_show(m)}"
    accounts = {(d.get("source") or {}).get("account") for d in (source, target)}
    # Numbers often differ per account by design; within an account a different number is a real difference.
    if len(accounts) > 1:
        report["notes"].append(f"{text} (different accounts: may be by design)")
    else:
        report["differences"].append(text)


def compare_docs(source: dict, target: dict) -> dict:
    """Return {"differences": [...], "problems": [...], "notes": [...]} for two read_plan() documents."""
    report: dict[str, list[str]] = {"differences": [], "problems": [], "notes": []}
    src = {a["name"]: a for a in source["applets"]}
    dst = {a["name"]: a for a in target["applets"]}

    for side, only in (("target", dst.keys() - src.keys()), ("source", src.keys() - dst.keys())):
        if importer.AUX_SDS in only:  # made by import-plan to keep missing data sources in the account
            only.discard(importer.AUX_SDS)
            keys = [d.get("Key") for d in ((dst if side == "target" else src)[importer.AUX_SDS].get("settings") or {})
                    .get("DataSources") or []]
            report["notes"].append(Finding(f"'{importer.AUX_SDS}' is only in the {side} plan: the auxiliary Set Data "
                                           f"Source vccflow keeps so that these data sources exist: "
                                           f"{', '.join(map(str, keys))}", section=ONE_SIDE))
        for name in sorted(only):
            where = "only in the target plan" if side == "target" else "missing in the target plan"
            kind = (dst if side == "target" else src)[name].get("kind")
            report["differences"].append(Finding(f"applet '{name}' ({kind}) is {where}", section=ONE_SIDE))

    for name in sorted(src.keys() & dst.keys()):
        a, b = src[name], dst[name]
        kind = a.get("kind")
        diffs = report["differences"]
        if kind != b.get("kind"):
            diffs.append(_about(name, None, f"applet type: source {kind} | target {b.get('kind')}"))
            continue
        an, bn = a.get("next", {}), b.get("next", {})
        for key in sorted(set(an) | set(bn)):
            if key not in bn:
                diffs.append(_about(name, kind, f"path {key} is missing in the target (source goes to '{an[key]}')"))
            elif key not in an:
                diffs.append(_about(name, kind, f"path {key} is missing in the source (target goes to '{bn[key]}')"))
            elif an[key] != bn[key]:
                diffs.append(_about(name, kind, f"path {key} is different: source '{an[key]}' | target '{bn[key]}'"))
        if a.get("unsupported") or b.get("unsupported"):
            if "form" in a and "form" in b:
                report["notes"].append(_about(name, kind, f"({kind}) compared through its editor page fields only",
                                              PAGE_FIELDS))
                _setting_findings(name, kind, a["form"], b["form"], report, "field")
            else:
                report["notes"].append(_about(name, kind, f"({kind}) settings not compared: its editor page fields "
                                                          "are not in the export (made before they were saved)"))
        else:
            sa, sb = a.get("settings") or {}, b.get("settings") or {}
            if a.get("type") == b.get("type") == export.ACD:
                _acd_group_findings(name, kind, sa, sb, report)
                # Fixed choices as the page words them ("Random", "First Retry"); an unknown code stays as it is.
                sa = {k: ACD_LABELS.get(k, {}).get(str(v), v) for k, v in sa.items() if k != "Groups"}
                sb = {k: ACD_LABELS.get(k, {}).get(str(v), v) for k, v in sb.items() if k != "Groups"}
            if a.get("type") == b.get("type") == export.PRIORITY:
                # As the page words them ("Highest"): VCC sends 0.9 and the editor posts "0.9".
                sa = {k: export.PRIORITY_LABELS.get(str(v), v) if k == "Priority" else v for k, v in sa.items()}
                sb = {k: export.PRIORITY_LABELS.get(str(v), v) if k == "Priority" else v for k, v in sb.items()}
            if a.get("type") == b.get("type") == export.SKILL_TAGGER:
                _skill_findings(name, kind, sa, sb, report)
                sa = {k: v for k, v in sa.items() if k not in SKILL_LISTS}
                sb = {k: v for k, v in sb.items() if k not in SKILL_LISTS}
            _setting_findings(name, kind, sa, sb, report)
        am = {m["property"]: m for m in a.get("media", [])}
        bm = {m["property"]: m for m in b.get("media", [])}
        for prop in sorted(set(am) | set(bm)):
            if prop not in bm or prop not in am:
                side = "target" if prop not in bm else "source"
                diffs.append(_about(name, kind, f"audio {prop} is missing in the {side}"))
            elif am[prop].get("name") != bm[prop].get("name"):
                diffs.append(_about(name, kind, f"audio name: source '{am[prop].get('name')}' | "
                                                f"target '{bm[prop].get('name')}'"))
            elif am[prop].get("sha256") and bm[prop].get("sha256") and am[prop]["sha256"] != bm[prop]["sha256"]:
                # Files VCC keeps under a GUID (ACD, Dial In Config) have no name: the slot says which one.
                what = f"'{am[prop]['name']}'" if am[prop].get("name") else prop
                diffs.append(_about(name, kind, f"audio {what} has different content"))

    for key, label in (("initialApplet", "initial applet"), ("reportingGroupName", "reporting group")):
        x, y = source["plan"].get(key), target["plan"].get(key)
        if x != y and not (_is_empty(x) and _is_empty(y)):
            report["differences"].append(f"{label}: source {_show(x)} | target {_show(y)}")

    _outbound_clid_findings(source, target, report)

    for side, doc in (("source", source), ("target", target)):
        names = {a["name"] for a in doc["applets"]}
        for a in doc["applets"]:
            for key, to in a.get("next", {}).items():
                if to not in names:
                    detail = f"path {key} goes to '{to}', which is not in the plan"
                    report["problems"].append(Finding(f"{side}: '{a['name']}' {detail}", a["name"], a.get("kind"),
                                                      f"{side}: {detail}"))
            report["problems"] += [Finding(f"{side}: {p}", a["name"], a.get("kind"))
                                   for p in a.get("connectionProblems", [])]
            if (selected := export.dial_in_config_empty_selection(a)) is not None:
                detail = f"selects configuration {selected}, which is empty (no applet, alternative text or announcement)"
                report["problems"].append(Finding(f"{side}: '{a['name']}' {detail}", a["name"], a.get("kind"),
                                                  f"{side}: {detail}"))
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


def _place(item: str) -> str:
    """Where a line goes: an applet name, or PLAN / ONE_SIDE / PAGE_FIELDS. Plain strings are plan-level."""
    section = getattr(item, "section", PLAN)
    applet = getattr(item, "applet", None)
    return section if section == PAGE_FIELDS or applet is None else applet


def _section(items: list[str], lines: list[str]) -> None:
    """Plan-level lines first, then applets in one plan only, then one block per applet."""
    groups: dict[str, list[str]] = {}
    for x in items:
        groups.setdefault(_place(x), []).append(x)
    for place, title in ((PLAN, "Plan"), (ONE_SIDE, "Applets in one plan only")):
        if place in groups:
            lines.append(f"  {title}")
            lines += [f"    {x}" for x in groups.pop(place)]
    if PAGE_FIELDS in groups:
        found = groups.pop(PAGE_FIELDS)
        by_kind: dict[str, list[str]] = {}
        for x in found:
            by_kind.setdefault(x.kind or "?", []).append(f"'{x.applet}'")
        lines.append(f"  Compared through their editor page fields only ({importer.count(len(found), 'applet')})")
        lines += [f"    {kind}: {', '.join(names)}" for kind, names in sorted(by_kind.items())]
    for name in sorted(groups, key=str.lower):
        found = groups[name]
        kind = next((x.kind for x in found if x.kind), None)
        lines.append(f"  '{name}'" + (f" ({kind})" if kind else "") + f": {len(found)}")
        lines += [f"    {x.detail}" for x in found]


def format_report(report: dict) -> str:
    """The report grouped: a summary line, then per section the plan-level lines and one block per applet."""
    diffs = report["differences"]
    places = [_place(x) for x in diffs]
    plan, one_side = places.count(PLAN), places.count(ONE_SIDE)
    applets = len(set(places) - {PLAN, ONE_SIDE})
    n = importer.count
    lines = [f"SUMMARY: {n(len(diffs), 'difference')} ({plan} plan-level, {n(one_side, 'applet')} in one plan only, "
             f"{len(diffs) - plan - one_side} in {n(applets, 'applet')}), "
             f"{n(len(report['problems']), 'problem')}, {n(len(report['notes']), 'note')}", ""]
    for key, title in (("differences", "DIFFERENCES"), ("problems", "PROBLEMS"), ("notes", "NOTES")):
        lines.append(f"{title} ({len(report[key])})")
        _section(report[key], lines)
    return "\n".join(lines)
