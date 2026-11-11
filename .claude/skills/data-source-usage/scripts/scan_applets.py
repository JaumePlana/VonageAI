"""Read-only: save the raw settings of every applet of every plan of an account.

Usage (from the project root):
    .venv/Scripts/python.exe .claude/skills/data-source-usage/scripts/scan_applets.py <account> [--fresh]

Writes exports/<account>/applet-scan/<plan name>.json (one per plan) and manifest.json.
The plan ID is kept inside each file, so renamed plans are followed.
It resumes: plans already saved are skipped, so a run stopped by an expired login
continues where it left off. --fresh starts over: a complete previous scan is moved to
exports/<account>/applet-scan-previous (replacing the one there), so compare_scans.py can
show what changed; an incomplete one is discarded.

The editor pages' catalogs of possible values (data source lists, next-applet pickers,
Salesforce object and field lists, form definitions) are left out; the account's data
source list is saved once, in manifest.json ("dataSources"). --compact removes the catalogs
from scans saved before this, offline.

No audio is downloaded and nothing is changed in VCC. The files hold Data Connector
settings, credentials included: exports/ is git-ignored, keep it private.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from vccflow.config import load_account
from vccflow.export import DC_SUB_NOT_CONFIG
from vccflow.portal import PortalClient, PortalError, SessionExpired


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_applet(portal: PortalClient, plan_id: str, applet: dict) -> dict:
    url = applet.get("configUrl") or ""
    entry: dict = {"listing": applet}
    try:
        if url.startswith("DataRetriever"):
            entry["dc"] = portal.get_data_connector(applet)
        elif url.startswith("Applet?"):
            entry["state"] = portal.get_applet_state(plan_id, applet)
        else:
            entry["form"] = portal.get_applet_form(applet)
    except SessionExpired:
        raise
    except PortalError as e:
        entry["error"] = str(e)
        try:
            entry["form"] = portal.get_applet_form(applet)
        except SessionExpired:
            raise
        except PortalError as e2:
            entry["formError"] = str(e2)
    return entry


# The editor pages carry catalogs of possible values (every data source of the account,
# every applet of the plan for the next-applet pickers, Salesforce objects and fields, the
# page's form definition). They aren't the applet's configuration, are the same in every
# applet, and made up ~95% of a scan. The applet's own values are all kept. TemplateGroups
# (Data Connector response mappings) is kept: the analysis needs it, unlike export-plan.
STATE_CATALOGS = {"metadata", "dataSourceKeys"}
DC_CATALOGS = {"MetabaseEntries", "PossibleNextApplets", "FormattedNextApplets", "MatchObjectNames",
               "CrmFields", "FilterableCrmFields"}
DC_SUB_CATALOGS = DC_SUB_NOT_CONFIG  # lookup lists inside the Salesforce sub-models, as export-plan


def data_source_list(entry: dict) -> list[str] | None:
    """The account's data source names, as an applet page lists them (None if it doesn't)."""
    entries = ((entry.get("state") or {}).get("dataSourceKeys") or {}).get("metabaseEntries")
    if entries is None:
        entries = ((entry.get("dc") or {}).get("model") or {}).get("MetabaseEntries")
    return sorted(entries, key=lambda n: (n.lower(), n)) if entries is not None else None


def compact(entry: dict) -> dict:
    """The applet without the editor's catalogs of possible values."""
    out = dict(entry)
    if isinstance(out.get("state"), dict):
        out["state"] = {k: v for k, v in out["state"].items() if k not in STATE_CATALOGS}
    if isinstance(out.get("dc"), dict) and isinstance(out["dc"].get("model"), dict):
        model = {}
        for k, v in out["dc"]["model"].items():
            if k in DC_CATALOGS:
                continue
            model[k] = {s: x for s, x in v.items() if s not in DC_SUB_CATALOGS} if isinstance(v, dict) else v
        out["dc"] = {**out["dc"], "model": model}
    return out


def write_json(target: Path, doc: dict) -> None:
    """Indented like export-plan's plan.json; renamed when complete, so no half-written files."""
    tmp = target.with_name(target.name + ".part")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(target)


def compact_saved(folder: Path) -> None:
    """Remove the catalogs from a scan saved before they were left out (offline)."""
    manifest_file = folder / "manifest.json"
    if not manifest_file.exists():
        return
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    before = after = 0
    for f in sorted(folder.glob("*.json")):
        if f.name == "manifest.json":
            continue
        doc = json.loads(f.read_text(encoding="utf-8"))
        if manifest.get("dataSources") is None:
            for a in doc["applets"]:
                if (names := data_source_list(a)) is not None:
                    manifest["dataSources"] = names
                    break
        before += f.stat().st_size
        doc["applets"] = [compact(a) for a in doc["applets"]]
        write_json(f, doc)
        after += f.stat().st_size
    write_json(manifest_file, manifest)
    print(f"{folder}: {before // 2**20} MB -> {after // 2**20} MB", flush=True)


def plan_filenames(plans: list[dict]) -> dict[str, str]:
    """File name per plan ID: the plan name, made safe for Windows.

    Plans whose names clash once made safe (Windows ignores case), or that would be
    called "manifest", get their ID appended: "<name> [<id>].json".
    """
    def safe(name: str) -> str:
        return re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip(" .") or "plan"

    stems = {p["interactionPlanId"]: safe(p["interactionPlanName"]) for p in plans}
    count = Counter(s.lower() for s in stems.values())
    return {pid: (f"{s} [{safe(pid)}]" if count[s.lower()] > 1 or s.lower() == "manifest" else s) + ".json"
            for pid, s in stems.items()}


def tidy(out: Path, files: dict[str, str]) -> None:
    """Match the saved files to the current plans by the plan ID inside each file.

    Files of renamed plans (and scans saved under the plan ID) are renamed; files of
    deleted plans are removed. Then a resumed scan skips exactly the plans already saved.
    """
    saved = {}
    for f in out.glob("*.json"):
        if f.name == "manifest.json":
            continue
        try:
            pid = json.loads(f.read_text(encoding="utf-8"))["plan"]["interactionPlanId"]
        except (ValueError, KeyError, TypeError):
            f.unlink()  # a file cut short by an interrupted run: scan that plan again
            print(f"Removed {f.name}: unreadable, the plan will be scanned again", flush=True)
            continue
        if pid not in files:
            f.unlink()
            print(f"Removed {f.name}: the plan no longer exists", flush=True)
        else:
            saved[pid] = f
    # Two steps, so a rename never overwrites a file that is itself about to be renamed.
    moving = {pid: f.rename(f.with_name(f"{f.name}.tmp")) for pid, f in saved.items() if f.name != files[pid]}
    for pid, tmp in moving.items():
        tmp.rename(out / files[pid])


def empty(folder: Path) -> None:
    """Empty the folder rather than delete it: on Windows, OneDrive or an open editor
    can hold the folder itself and refuse its removal."""
    for f in folder.iterdir():
        shutil.rmtree(f) if f.is_dir() else f.unlink()


def scan_finished(folder: Path) -> bool:
    try:
        return bool(json.loads((folder / "manifest.json").read_text(encoding="utf-8")).get("finished"))
    except (OSError, ValueError):
        return False


def keep_previous(out: Path) -> None:
    """Before a fresh scan: move a complete scan to applet-scan-previous, for compare_scans.py.

    An incomplete scan is just discarded, so it never replaces a complete previous one.
    """
    previous = out.with_name("applet-scan-previous")
    if scan_finished(out):
        previous.mkdir(exist_ok=True)
        empty(previous)
        for f in out.iterdir():
            shutil.move(str(f), str(previous / f.name))
        print(f"Previous scan kept in {previous}", flush=True)
    else:
        empty(out)


def main() -> int:
    os.chdir(Path(__file__).resolve().parents[4])  # the project root
    ap = argparse.ArgumentParser()
    ap.add_argument("account")
    ap.add_argument("--fresh", action="store_true", help="start over, keeping a complete previous scan in applet-scan-previous")
    ap.add_argument("--compact", action="store_true",
                    help="offline: remove the editor catalogs from scans saved before they were left out")
    args = ap.parse_args()

    if args.compact:
        for folder in ("applet-scan", "applet-scan-previous"):
            compact_saved(Path("exports") / args.account / folder)
        return 0

    load_dotenv(".env")
    account = load_account(args.account, Path("accounts.toml"))
    out = Path("exports") / args.account / "applet-scan"
    out.mkdir(parents=True, exist_ok=True)
    if args.fresh:
        keep_previous(out)
    manifest_file = out / "manifest.json"
    manifest = json.loads(manifest_file.read_text(encoding="utf-8")) if manifest_file.exists() else {}
    manifest.setdefault("account", args.account)
    manifest.setdefault("started", now())
    manifest.pop("finished", None)

    portal = PortalClient(account)
    plans = portal.list_plans()
    manifest["plans"] = len(plans)
    manifest_file.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"{len(plans)} plans in '{args.account}'", flush=True)

    files = plan_filenames(plans)
    tidy(out, files)

    unreadable = 0
    for i, plan in enumerate(plans, 1):
        pid = plan["interactionPlanId"]
        target = out / files[pid]
        if target.exists():
            continue
        applets = [read_applet(portal, pid, a) for a in portal.list_applets(pid).get("applets", [])]
        bad = sum(1 for a in applets if "formError" in a)
        unreadable += bad
        if manifest.get("dataSources") is None:  # the account's list, saved once instead of per applet
            for a in applets:
                if (names := data_source_list(a)) is not None:
                    manifest["dataSources"] = names
                    write_json(manifest_file, manifest)
                    break
        write_json(target, {"plan": plan, "scannedAt": now(), "applets": [compact(a) for a in applets]})
        note = f", {bad} unreadable" if bad else ""
        print(f"[{i}/{len(plans)}] {plan['interactionPlanName']}: {len(applets)} applets{note}", flush=True)

    manifest["finished"] = now()
    manifest_file.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"DONE: {len(plans)} plans saved in {out}" + (f", {unreadable} unreadable applets" if unreadable else ""))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SessionExpired as e:
        print(f"LOGIN EXPIRED: {e}\nThe plans saved so far are kept; run again after logging in to resume.")
        sys.exit(2)
