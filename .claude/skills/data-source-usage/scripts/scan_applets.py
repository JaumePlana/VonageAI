"""Read-only: save the raw settings of every applet of every plan of an account.

Usage (from the project root):
    .venv/Scripts/python.exe .claude/skills/data-source-usage/scripts/scan_applets.py <account> [--fresh]

Writes exports/<account>/applet-scan/<plan name>.json (one per plan) and manifest.json.
The plan ID is kept inside each file, so renamed plans are followed.
It resumes: plans already saved are skipped, so a run stopped by an expired login
continues where it left off. --fresh deletes the previous scan first.

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


def main() -> int:
    os.chdir(Path(__file__).resolve().parents[4])  # the project root
    ap = argparse.ArgumentParser()
    ap.add_argument("account")
    ap.add_argument("--fresh", action="store_true", help="delete the previous scan first")
    args = ap.parse_args()

    load_dotenv(".env")
    account = load_account(args.account, Path("accounts.toml"))
    out = Path("exports") / args.account / "applet-scan"
    if args.fresh and out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    manifest_file = out / "manifest.json"
    manifest = json.loads(manifest_file.read_text(encoding="utf-8")) if manifest_file.exists() else {}
    manifest.setdefault("account", args.account)
    manifest.setdefault("started", now())
    manifest.pop("finished", None)

    portal = PortalClient(account)
    plans = portal.list_plans()
    manifest["plans"] = len(plans)
    manifest_file.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
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
        tmp = target.with_name(target.name + ".part")  # renamed when complete: no half-written plan files
        tmp.write_text(json.dumps({"plan": plan, "scannedAt": now(), "applets": applets},
                                  ensure_ascii=False), encoding="utf-8")
        tmp.replace(target)
        note = f", {bad} unreadable" if bad else ""
        print(f"[{i}/{len(plans)}] {plan['interactionPlanName']}: {len(applets)} applets{note}", flush=True)

    manifest["finished"] = now()
    manifest_file.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"DONE: {len(plans)} plans saved in {out}" + (f", {unreadable} unreadable applets" if unreadable else ""))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SessionExpired as e:
        print(f"LOGIN EXPIRED: {e}\nThe plans saved so far are kept; run again after logging in to resume.")
        sys.exit(2)
