"""Where data sources are set and read, from a scan by scan_applets.py (offline, read-only).

Usage (from the project root):
    python ds_usage.py <account> "Name 1" "Name 2"   exact names (not case-sensitive, like VCC)
    python ds_usage.py <account> --like "error resp"  every data source whose name contains the text
    python ds_usage.py <account> --all                one line per data source: set/read counts

Roles:
  SET   the applet writes the data source: Set Data Source key, Data Connector response
        mapping or web service <Destination>, Collect Input target, Virtual Assistant output.
  READ  the applet uses its value: $(name) anywhere, Data Router condition, Announcer or Menu
        data source, Salesforce match or field value.
  OTHER the name appears in a field this script doesn't know (e.g. Skill Tagger, Bot Connector
        settings): read the field name and decide.
The editors' dropdown lists (every data source of the account) are ignored, and so are
references to applets (next-applet fields), even when an applet has a data source's name.

Names are kept exactly as stored, spaces included, and shown in quotes: " X" and "X" are
reported apart. A name that differs from a requested one only by leading or trailing spaces
is still found, and flagged: it is most likely a typo for the same data source.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

from vccflow.export import data_sources_created

PLACEHOLDER = re.compile(r"\$\(([^)]+)\)")
XML_DESTINATION = re.compile(r"<(?:Destination|ResponseCodeDestination)>([^<]+)</")
DROPDOWNS = re.compile(r"MetabaseDetails\[|MetabaseEntries|metabaseEntries|options\[|^state\.metadata")
SECRET = re.compile(r"secret|password|token|apikey|api_key|authori|credential", re.I)
VA_OUTPUT = re.compile(r"VirtualAssistantParamsToDataSourcesMapping\[\d+\]\.Destination$")
# Fields that name an applet (where to go next, the applet itself), never a data source.
APPLET_FIELD = re.compile(r"NextApplet|PossibleNextApplets|FormattedNextApplets|AppletName$|currentApplet|"
                          r"Url$|RouteToApplet|(^|\.)Applet\d*$|defaultapplet|DefaultAppletInstanceName", re.I)
PICKERS = re.compile(r"PossibleNextApplets|FormattedNextApplets")  # every applet of the plan, for the editor
DS_FIELD = re.compile(r"DataSource|\.Key$|Destination|Item1$", re.I)
APPLET_LABEL = re.compile(r"^(.*) \([^()]*\)$")  # "Name (Applet Type)" in next-applet pickers


def show(name: str) -> str:
    """The name in quotes, so leading and trailing spaces are visible."""
    return f'"{name}"'


def spaced(name: str) -> bool:
    return name != name.strip()


def walk(v, path=""):
    if isinstance(v, dict):
        for k, x in v.items():
            yield from walk(x, f"{path}.{k}" if path else str(k))
    elif isinstance(v, list):
        for i, x in enumerate(v):
            yield from walk(x, f"{path}[{i}]")
    else:
        yield path, v


def field(path: str) -> str:
    return re.sub(r"^(state\.appletProperties|dc\.model|dc\.fields|form)\.", "", path)


def excerpt(path: str, text: str, needle: str) -> str:
    if SECRET.search(path):
        return "(secret field, not shown)"
    i = text.lower().find(needle.lower())
    start, end = max(0, i - 60), i + len(needle) + 60
    return ("…" if start else "") + " ".join(text[start:end].split()) + ("…" if end < len(text) else "")


def references(applet: dict) -> list[dict]:
    """Every data source the applet sets or reads: {name, role, detail, paths}. Names as stored."""
    body = {k: v for k, v in applet.items() if k in ("state", "dc", "form")}
    props = (body.get("state") or {}).get("appletProperties") or {}
    model = (body.get("dc") or {}).get("model") or {}
    form = body.get("form") or {}
    found = []

    def add(name, role, detail, path):
        if isinstance(name, str) and name.strip():
            found.append({"name": name, "role": role, "detail": detail, "paths": [path]})

    for i, ds in enumerate(props.get("DataSources") or []):
        add(ds.get("Key"), "SET", f"= {ds.get('Value')!r}", f"state.appletProperties.DataSources[{i}].Key")
    for g, tg in enumerate(model.get("TemplateGroups") or []):
        for t, tpl in enumerate(tg.get("Templates") or []):
            for r, item in enumerate(tpl.get("ResponseDataSources") or []):
                add(item.get("Item1"), "SET", f"Data Connector response ({item.get('Item2')})",
                    f"dc.model.TemplateGroups[{g}].Templates[{t}].ResponseDataSources[{r}].Item1")
    if applet["listing"].get("appletType") == "Collect Input" and isinstance(form.get("DataSource"), str):
        add(form["DataSource"], "SET", "collected input stored here", "form.DataSource")
    for key, what in (("DataSource", "announced"), ("AnnouncementDataSource", "announced")):
        add(props.get(key), "READ", what, f"state.appletProperties.{key}")
    if props.get("IsDynamicVoice"):
        add(props.get("VoiceDataSource"), "READ", "voice taken from it", "state.appletProperties.VoiceDataSource")
    for i, rule in enumerate(form.get("RoutingRules") or []):
        right = rule.get("RightHandFixedValue")
        if right is None:
            right = rule.get("RightHandFixedBooleanValue")
        add(rule.get("LeftHandDataSource"), "READ",
            f"Data Router: {rule.get('OperatorType')} {right!r} -> {rule.get('RouteToApplet')}",
            f"form.RoutingRules[{i}].LeftHandDataSource")
    crm = model.get("CrmRetrievalViewModel") or {}
    add(crm.get("MatchDataSource"), "READ", "Salesforce match value", "dc.model.CrmRetrievalViewModel.MatchDataSource")
    for i, p in enumerate((model.get("CrmCreationViewModel") or {}).get("CrmObjectParameters") or []):
        if p.get("DataSourceType") == 0:
            add(p.get("Value"), "READ", f"Salesforce field {p.get('Name') or p.get('Key') or ''}".strip(),
                f"dc.model.CrmCreationViewModel.CrmObjectParameters[{i}].Value")

    for path, v in walk(body):
        if not isinstance(v, str) or DROPDOWNS.search(path):
            continue
        for name in PLACEHOLDER.findall(v):
            if VA_OUTPUT.search(path):
                add(name, "SET", "Virtual Assistant output", path)
            else:
                where = field(path)
                m = re.match(r"DataSources\[(\d+)\]\.Value$", where)
                if m:
                    key = (props.get("DataSources") or [])[int(m.group(1))].get("Key")
                    where = f"value of {show(key or '')}"
                add(name, "READ", f"$({name}) in {where}: {excerpt(path, v, f'$({name})')}", path)
        for name in XML_DESTINATION.findall(v):
            add(name, "SET", "web service response <Destination>", path)
    if model:  # Salesforce get: "Object|Field" per returned field (vccflow's own rule, as in export)
        dc = body["dc"]
        for name in data_sources_created({"settings": {**model, **(dc.get("fields") or {})}}):
            if not any(f["name"].lower() == name.lower() and f["role"] == "SET" for f in found):
                add(name, "SET", "Salesforce response field", "dc.model.ResponseItems")
                found[-1]["paths"].append("dc.model.CrmRetrievalViewModel.Soql")
    # One row per data source and role: an applet can set or read a name in several fields.
    merged: dict[tuple, dict] = {}
    for f in found:
        key = (f["name"].lower(), f["role"])
        if key in merged:
            if f["detail"] not in merged[key]["detail"]:
                merged[key]["detail"] += "; " + f["detail"]
            merged[key]["paths"] += f["paths"]
        else:
            merged[key] = dict(f)
    return list(merged.values())


def applet_reference(path: str, value: str, applet_names: set[str]) -> bool:
    """The field points to an applet, not to a data source."""
    if APPLET_FIELD.search(path):
        return True
    v = value.strip().lower()
    label = APPLET_LABEL.match(value.strip())
    names_applet = v in applet_names or bool(label and label.group(1).strip().lower() in applet_names)
    return names_applet and not DS_FIELD.search(path)


def other_hits(applet: dict, match, known_paths: set[str], applet_names: set[str]) -> list[dict]:
    """The name in a field references() doesn't classify (applet references left out)."""
    body = {k: v for k, v in applet.items() if k in ("state", "dc", "form")}
    hits = []
    for path, v in walk(body):
        if not isinstance(v, str) or DROPDOWNS.search(path) or not match(v):
            continue
        known = any(path == k or path.startswith(k + "[") or path.startswith(k + ".") for k in known_paths)
        if not known and not applet_reference(path, v, applet_names):
            hits.append({"name": v if len(v) < 80 else v[:80], "role": "OTHER",
                         "detail": f"in field {field(path)}: {excerpt(path, v, v.strip()[:40])}", "paths": [path]})
    return hits


def links(plan_applets: list[dict], applet: dict) -> tuple[list[str], list[str]]:
    """Applets that point to this one, and the ones it points to (by name, in the same plan).

    A Data Connector's PossibleNextApplets / FormattedNextApplets list every applet of the
    plan (the editor's picker), not where it goes: they are left out.
    """
    names = {a["listing"]["name"] for a in plan_applets}
    me = applet["listing"]["name"]

    def pointed(a):
        body = {k: v for k, v in a.items() if k in ("state", "dc", "form")}
        return {v for p, v in walk(body) if isinstance(v, str) and v in names and not DROPDOWNS.search(p)
                and not PICKERS.search(p) and not p.endswith("AppletName")}

    nxt = sorted(pointed(applet) - {me})
    prev = sorted(a["listing"]["name"] for a in plan_applets if a is not applet and me in pointed(a))
    return prev, nxt


def spacing_variants(keys) -> list[list[str]]:
    """Groups of names that are equal once leading and trailing spaces are removed."""
    groups = defaultdict(set)
    for key in keys:
        groups[key.strip()].add(key)
    return sorted(sorted(g) for g in groups.values() if len(g) > 1)


def load_scan(account: str) -> tuple[dict, list[dict]]:
    scan = Path("exports") / account / "applet-scan"
    manifest_file = scan / "manifest.json"
    if not manifest_file.exists():
        sys.exit(f"No scan for '{account}'. Run scan_applets.py {account} first.")
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    plans = [json.loads(f.read_text(encoding="utf-8")) for f in sorted(scan.glob("*.json")) if f.name != "manifest.json"]
    return manifest, plans


def main() -> int:
    os.chdir(Path(__file__).resolve().parents[4])  # the project root
    ap = argparse.ArgumentParser()
    ap.add_argument("account")
    ap.add_argument("names", nargs="*")
    ap.add_argument("--like", action="append", default=[], help="name contains this text (repeatable)")
    ap.add_argument("--all", action="store_true", help="summary of every data source")
    args = ap.parse_args()
    if not (args.names or args.like or args.all):
        ap.error("give names, --like or --all")

    manifest, plans = load_scan(args.account)
    unreadable = [(p["plan"]["interactionPlanName"], a["listing"]["name"]) for p in plans
                  for a in p["applets"] if "formError" in a]
    status = "complete" if manifest.get("finished") else "INCOMPLETE (scan did not finish)"
    print(f"SCAN: {args.account}, {len(plans)}/{manifest.get('plans')} plans, {status}, "
          f"started {manifest.get('started')}, finished {manifest.get('finished')}")
    print(f"APPLETS: {sum(len(p['applets']) for p in plans)}, unreadable: {len(unreadable)}")
    for pn, an in unreadable:
        print(f"  UNREADABLE: {pn} | {an}")

    exact = {n.lower() for n in args.names}
    trimmed = {n.strip().lower() for n in args.names}
    likes = [s.lower() for s in args.like]

    def match(name: str) -> bool:
        """Exact (spaces count), equal once trimmed (flagged in the report), or containing a --like text."""
        n = name.lower()
        return n in exact or n.strip() in trimmed or any(s in n for s in likes)

    rows = defaultdict(list)  # data source (lower case, spaces kept) -> rows
    spellings = defaultdict(set)
    for p in plans:
        pn = p["plan"]["interactionPlanName"]
        applet_names = {b["listing"]["name"].strip().lower() for b in p["applets"]}
        for a in p["applets"]:
            refs = references(a)
            hits = [r for r in refs if args.all or match(r["name"])]
            if not args.all:
                hits += other_hits(a, match, {x for r in refs for x in r["paths"]}, applet_names)
            if not hits:
                continue
            prev, nxt = ([], []) if args.all else links(p["applets"], a)
            for r in hits:
                key = r["name"].lower()
                spellings[key].add(r["name"])
                rows[key].append({**r, "plan": pn, "applet": a["listing"]["name"],
                                  "type": a["listing"]["appletType"], "from": prev, "next": nxt})

    if args.all:
        print("\nDATA SOURCE | set by applets | read by applets | plans")
        for key in sorted(rows, key=lambda k: (k.strip(), k)):
            rs = rows[key]
            s = len({(r["plan"], r["applet"]) for r in rs if r["role"] == "SET"})
            g = len({(r["plan"], r["applet"]) for r in rs if r["role"] == "READ"})
            flag = "  SET-NEVER-READ" if s and not g else "  READ-NEVER-SET" if g and not s else ""
            if spaced(key):
                flag += "  SPACES"
            print(f"{' / '.join(show(n) for n in sorted(spellings[key]))} | {s} | {g} | "
                  f"{len({r['plan'] for r in rs})}{flag}")
        variants = spacing_variants(rows)
        if variants:
            print("\nSAME NAME BUT FOR SPACES (probably one data source under two spellings):")
            for v in variants:
                print("  " + " / ".join(show(s) for k in v for s in sorted(spellings[k])))
        return 0

    for n in sorted(args.names, key=str.lower):
        if not any(k.strip() == n.strip().lower() for k in rows):
            print(f"\nNOT FOUND: no applet sets or reads {show(n)}")
    for key in sorted(rows, key=lambda k: (k.strip(), k)):
        rs = rows[key]
        print(f"\n=== {' / '.join(show(n) for n in sorted(spellings[key]))}")
        if spaced(key):
            print("  >> the name has leading or trailing spaces")
        if key not in exact and key.strip() in trimmed:
            asked = ", ".join(show(n) for n in args.names if n.strip().lower() == key.strip())
            print(f"  >> differs from the requested {asked} only by leading or trailing spaces")
        for role in ("SET", "READ", "OTHER"):
            group = [r for r in rs if r["role"] == role]
            applets = {(r["plan"], r["applet"]) for r in group}
            print(f"--- {role}: {len(applets)} applets in {len({r['plan'] for r in group})} plans")
            for r in sorted(group, key=lambda r: (r["plan"].lower(), r["applet"].lower())):
                print(f"  {r['plan']} | {r['applet']} ({r['type']}) | {r['detail']}")
                print(f"      from: {', '.join(r['from']) or '-'}")
                print(f"      next: {', '.join(r['next']) or '-'}")
        if not any(r["role"] == "READ" for r in rs):
            print("  >> SET but never READ in any plan")
        if not any(r["role"] == "SET" for r in rs):
            print("  >> READ but never SET in any plan (system data source, set elsewhere, or missing)")
    for v in spacing_variants(rows):
        print(f"\n>> SAME NAME BUT FOR SPACES: {' / '.join(show(s) for k in v for s in sorted(spellings[k]))}: probably one data source "
              f"under two spellings; compare where each is set and read")
    return 0


if __name__ == "__main__":
    sys.exit(main())
