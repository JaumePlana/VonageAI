"""What changed in an account's plans between two applet scans (offline, read-only).

Usage (from the project root):
    python compare_scans.py <account>                 applet-scan-previous -> applet-scan
    python compare_scans.py <account> --old DIR --new DIR

scan_applets.py --fresh keeps the previous complete scan in exports/<account>/applet-scan-previous.
Plans are matched by plan ID, applets by name (an applet with the same GUID under another name
is reported as renamed). Reports plans and applets added, removed or renamed, and every changed
field with its old and new value. The data source and next-applet dropdowns are ignored, since
they change whenever anything in the account changes; values of secret fields are never shown.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

NOISE = re.compile(r"MetabaseDetails|MetabaseEntries|metabaseEntries|options\[|^state\.metadata|PossibleNextApplets|"
                   r"FormattedNextApplets|RequestVerificationToken|__VIEWSTATE|__EVENTVALIDATION|antiforgery|"
                   r"^listing\.(id|configUrl)$|Url$|currentApplet\.id|AppletId$|\.Id$", re.I)
SECRET = re.compile(r"secret|password|token|apikey|api_key|authori|credential|bearer", re.I)
MULTILINE = 3  # text fields with more lines than this are compared line by line


def walk(v, path=""):
    if isinstance(v, dict):
        for k, x in v.items():
            yield from walk(x, f"{path}.{k}" if path else str(k))
    elif isinstance(v, list):
        if v and all(isinstance(x, dict) and not any(isinstance(y, (dict, list)) for y in x.values()) for x in v):
            yield path, Items(v)  # a list of simple entries: compared as a set, not by position
            return
        if not v:
            yield path, []
        for i, x in enumerate(v):
            yield from walk(x, f"{path}[{i}]")
    else:
        yield path, v


class Items(list):
    """A list of simple entries (e.g. Set Data Source keys, response mappings)."""

    def entries(self) -> list[str]:
        return [json.dumps(x, ensure_ascii=False, sort_keys=True) for x in self]


def flat(applet: dict) -> dict:
    return {p: v for p, v in walk(applet) if not NOISE.search(p)}


def short(path: str, v) -> str:
    if SECRET.search(path):
        return "(secret, not shown)"
    s = json.dumps(v, ensure_ascii=False)
    return s if len(s) <= 160 else s[:157] + "…"


def text_diff(old: str, new: str) -> list[str]:
    """Changed lines of a long text (e.g. a web service definition), secret lines hidden."""
    import difflib
    out = []
    for line in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0):
        if line.startswith(("---", "+++", "@@")):
            continue
        out.append(f"{line[0]} (line with credentials, not shown)" if SECRET.search(line) else line.strip())
    return out


def item_changes(path: str, old, new) -> list[str]:
    from collections import Counter
    o = Counter(old.entries()) if isinstance(old, Items) else Counter()
    n = Counter(new.entries()) if isinstance(new, Items) else Counter()
    hide = SECRET.search(path)
    lines = [f"      {path}:"]
    for e in sorted((o - n).elements()):
        lines.append(f"          - {'(secret, not shown)' if hide else e}")
    for e in sorted((n - o).elements()):
        lines.append(f"          + {'(secret, not shown)' if hide else e}")
    if len(lines) == 1:
        lines[0] += " same entries, order changed"
    return lines


def field_changes(path: str, old, new) -> list[str]:
    if isinstance(old, Items) or isinstance(new, Items):
        return item_changes(path, old, new)
    if (isinstance(old, str) and isinstance(new, str) and not SECRET.search(path)
            and max(old.count("\n"), new.count("\n")) >= MULTILINE):
        return [f"      {path}: text changed"] + [f"          {l}" for l in text_diff(old, new)]
    return [f"      {path}: {short(path, old)} -> {short(path, new)}"]


def load(folder: Path) -> tuple[dict, dict]:
    if not (folder / "manifest.json").exists():
        sys.exit(f"No scan in {folder}.")
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    plans = {}
    for f in folder.glob("*.json"):
        if f.name != "manifest.json":
            doc = json.loads(f.read_text(encoding="utf-8"))
            plans[doc["plan"]["interactionPlanId"]] = doc
    return manifest, plans


def name(doc: dict) -> str:
    return doc["plan"]["interactionPlanName"]


def compare_plan(o: dict, n: dict) -> list[str]:
    lines = []
    if name(o) != name(n):
        lines.append(f"  plan renamed: {name(o)!r} -> {name(n)!r}")
    if o["plan"].get("initialApplet") != n["plan"].get("initialApplet"):
        lines.append(f"  initial applet: {o['plan'].get('initialApplet')!r} -> {n['plan'].get('initialApplet')!r}")
    oa = {a["listing"]["name"]: a for a in o["applets"]}
    na = {a["listing"]["name"]: a for a in n["applets"]}
    guid_of_old = {a["listing"].get("guid"): nm for nm, a in oa.items() if a["listing"].get("guid")}
    renamed = {}
    for nm, a in na.items():
        old_name = guid_of_old.get(a["listing"].get("guid"))
        if nm not in oa and old_name and old_name not in na:
            renamed[old_name] = nm
    for nm in sorted(set(oa) - set(na) - set(renamed), key=str.lower):
        lines.append(f"  - applet removed: {nm} ({oa[nm]['listing']['appletType']})")
    for nm in sorted(set(na) - set(oa) - set(renamed.values()), key=str.lower):
        lines.append(f"  + applet added: {nm} ({na[nm]['listing']['appletType']})")
    pairs = [(nm, nm) for nm in set(oa) & set(na)] + list(renamed.items())
    for onm, nnm in sorted(pairs, key=lambda x: x[1].lower()):
        fo, fn = flat(oa[onm]), flat(na[nnm])
        diffs = [p for p in sorted(set(fo) | set(fn)) if fo.get(p, "<none>") != fn.get(p, "<none>")]
        kind = na[nnm]["listing"]["appletType"]
        if onm != nnm:
            lines.append(f"  ~ applet renamed: {onm} -> {nnm} ({kind})")
        elif diffs:
            lines.append(f"  ~ applet changed: {nnm} ({kind})")
        for p in diffs:
            lines += field_changes(p, fo.get(p, "<none>"), fn.get(p, "<none>"))
    return lines


def main() -> int:
    os.chdir(Path(__file__).resolve().parents[4])  # the project root
    ap = argparse.ArgumentParser()
    ap.add_argument("account")
    ap.add_argument("--old", help="older scan folder (default: exports/<account>/applet-scan-previous)")
    ap.add_argument("--new", help="newer scan folder (default: exports/<account>/applet-scan)")
    args = ap.parse_args()
    base = Path("exports") / args.account
    old_dir = Path(args.old) if args.old else base / "applet-scan-previous"
    new_dir = Path(args.new) if args.new else base / "applet-scan"
    om, old = load(old_dir)
    nm, new = load(new_dir)
    for label, m, plans in (("OLD", om, old), ("NEW", nm, new)):
        status = "complete" if m.get("finished") else "INCOMPLETE (scan did not finish)"
        print(f"{label}: {len(plans)}/{m.get('plans')} plans, {status}, started {m.get('started')}, "
              f"finished {m.get('finished')}")

    for pid in sorted(set(old) - set(new), key=lambda p: name(old[p]).lower()):
        print(f"\nPLAN REMOVED: {name(old[pid])} ({len(old[pid]['applets'])} applets)")
    for pid in sorted(set(new) - set(old), key=lambda p: name(new[p]).lower()):
        print(f"\nPLAN ADDED: {name(new[pid])} ({len(new[pid]['applets'])} applets)")
    changed = 0
    for pid in sorted(set(old) & set(new), key=lambda p: name(new[p]).lower()):
        lines = compare_plan(old[pid], new[pid])
        if lines:
            changed += 1
            print(f"\nPLAN CHANGED: {name(new[pid])}")
            print("\n".join(lines))
    print(f"\nSUMMARY: {len(old)} plans before, {len(new)} after; {len(set(new) - set(old))} added, "
          f"{len(set(old) - set(new))} removed, {changed} changed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
