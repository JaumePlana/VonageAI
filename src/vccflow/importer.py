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
from vccflow.scoped_api import ApiError
from vccflow.skills import GROUP_DEFAULT_MAX_TIME, GroupsClient, SkillsClient
from vccflow.schedules import SchedulesClient
from vccflow.portal import (ACD, ACD_PRESENCE, CI_AUDIO, COLLECT_INPUT, DIAL_IN_CONFIG, MAX_MEDIA_BYTES, SHIM, SHIM_TO_PLAN, PortalClient, PortalError,
                            acd_groups, acd_presence_states, check_acd, check_collect_input, check_dial_in_config, check_shim,
                            data_connector_form, data_router_form, data_router_missing_targets, shim_configured,
                            skill_tagger_form)

DATA_CONNECTOR = "DataRetriever"
DATA_ROUTER = export.DATA_ROUTER
SKILL_TAGGER = export.SKILL_TAGGER
SCHEDULE_ROUTER = export.SCHEDULE_ROUTER
# A schedule cloned because the target has one of that name without the source's service statuses.
AUX_SCHEDULE_PREFIX = "zz vccflow Aux "
# Auxiliary Set Data Source that makes the data sources the target account lacks exist. VCC
# refuses to save a Data Router rule whose data source is not in the account's list ("Rule 1:
# Please select a Data Source from the list"). A data source is in the list only while some
# applet sets it: deleting this applet removes them again (tested 2026-11-13), so it is kept.
# Nothing leads to it, so it never runs. "zz" sorts it last; the same name is reused next time.
# It is created only when a data source is missing. One in the source plan is not copied as an
# applet: its keys that the target account lacks go into the target's own auxiliary applet.
AUX_SDS = "zz vccflow Aux Data Sources"
# Applet types whose editor saves through Applet/Save (the React editor).
EDITOR_TYPES = export.IMPORTABLE_EDITOR_TYPES


def count(n: int, one: str, many: str | None = None) -> str:
    """'1 skill', '3 skills' ('1 Interaction Plan', with many for irregular plurals)."""
    return f"{n} {one if n == 1 else many or one + 's'}"


def are(n: int, dry_run: bool = False) -> str:
    """The verb for count(): 'is' / 'are', or 'would be' in a dry run."""
    return "would be" if dry_run else "is" if n == 1 else "are"


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
    if applet["type"] not in EDITOR_TYPES | {DATA_CONNECTOR, DATA_ROUTER, SKILL_TAGGER, SHIM, ACD, SCHEDULE_ROUTER,
                                               DIAL_IN_CONFIG, COLLECT_INPUT, export.CALL_CONNECT_ROUTER,
                                               export.CALL_RECORDING, export.LOOP_COUNTER, export.CALLBACK}:
        return f"applet type {applet['type']} is not supported for import yet"
    if applet["type"] == DATA_CONNECTOR:
        try:
            data_connector_form(applet["settings"], applet.get("next", {}))
        except PortalError as e:
            return str(e)
    if applet["type"] == SKILL_TAGGER:
        try:
            skill_tagger_form(applet["settings"], applet.get("next", {}))
        except PortalError as e:
            return str(e)
    if applet["type"] == export.PRIORITY:
        extra = sorted(set(applet["settings"]) - {"Priority"})
        if extra:
            return f"Queue - Set Priority settings {', '.join(extra)} have not been recorded yet; they cannot be saved"
        if str(applet["settings"].get("Priority")) not in export.PRIORITY_LABELS:
            return (f"Queue - Set Priority value {applet['settings'].get('Priority')!r} is not one of the editor's "
                    f"choices ({', '.join(export.PRIORITY_LABELS)})")
    if applet["type"] == SCHEDULE_ROUTER and applet["settings"].get("ScheduleNextAppletMappings"):
        return "Schedule Router ScheduleNextAppletMappings has not been recorded yet; it cannot be saved"
    if applet["type"] == ACD:
        try:
            check_acd(applet["settings"])
        except PortalError as e:
            return str(e)
    if applet["type"] == export.CALLBACK and (problems := export.callback_problems(applet)):
        return "; ".join(problems)
    if applet["type"] in (export.CALL_RECORDING, export.LOOP_COUNTER):
        check = export.call_recording_problems if applet["type"] == export.CALL_RECORDING else export.loop_counter_problems
        problems = check(applet["settings"], applet.get("next", {}))
        if problems:
            return "; ".join(problems)
    if applet["type"] == export.CALL_CONNECT_ROUTER:
        problems = export.call_connect_router_problems(applet["settings"])
        extra = sorted(set(applet.get("next", {})) - set(export.CCR_APPLETS))
        if extra:
            problems.append(f"Call Connect Router connections {', '.join(extra)} have not been recorded yet")
        if problems:
            return "; ".join(problems)
    if applet["type"] == COLLECT_INPUT:
        try:
            check_collect_input(applet["settings"], applet.get("next", {}))
        except PortalError as e:
            return str(e)
        wrong = [m["property"] for m in applet.get("media", []) if m["property"] not in CI_AUDIO]
        if wrong:
            return f"Collect Input audio {', '.join(wrong)} is not one of its audio files"
    if applet["type"] == DIAL_IN_CONFIG:
        try:
            check_dial_in_config(applet["settings"], applet.get("next", {}))
        except PortalError as e:
            return str(e)
        slots = [m["property"] for m in applet.get("media", [])]
        wrong = [s for s in slots if s not in {f"Announcement.{i}" for i in range(10)}]
        if wrong:
            return f"Dial In Config audio {', '.join(wrong)} is not a configuration's announcement"
    if applet["type"] == SHIM:
        try:
            check_shim(applet["settings"])
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


def check_skills(applets: list[dict], existing: list[str]) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """Return (spellings, missing, notes) for the static skills of the plan's Skill Taggers.

    Skill names are compared without letter case, as VCC does ("DE" exists, so "de" cannot be
    created): spellings maps a source name to the target account's spelling where they differ.
    missing maps each skill the target lacks to a message naming the applets that use it.
    """
    lower = {e.lower(): e for e in existing}
    users: dict[str, set[str]] = {}
    for a in applets:
        if a.get("type") == SKILL_TAGGER:
            for s in a["settings"].get("Skills") or []:
                users.setdefault(s["Name"], set()).add(a["name"])
    spellings, missing, notes = {}, {}, []
    for name in sorted(users, key=str.lower):
        found = lower.get(name.lower())
        if found is None:
            missing[name] = (f"skill '{name}' does not exist in the target account "
                             f"(used by {', '.join(sorted(users[name]))})")
        elif found != name:
            spellings[name] = found
            notes.append(f"skill '{name}' exists in the target account as '{found}': that spelling is used")
    return spellings, missing, notes


def use_skill_spellings(applets: list[dict], spellings: dict[str, str]) -> None:
    """Rename the Skill Taggers' static skills to the target account's spelling."""
    for a in applets:
        if a.get("type") == SKILL_TAGGER:
            for s in a["settings"].get("Skills") or []:
                s["Name"] = spellings.get(s["Name"], s["Name"])


def _resolve_skills(applets: list[dict], existing: list[str], log: Callable[[str], None],
                    can_create: bool) -> list[str]:
    """Use the target's spelling of existing skills; return the skills to create.

    Without the User Admin API (can_create False), a missing skill stops the import.
    """
    spellings, missing, notes = check_skills(applets, existing)
    for n in notes:
        log(f"NOTE: {n}")
    if missing and not can_create:
        raise PortalError("Cannot import:\n  " + "\n  ".join(missing.values()) + "\n  They cannot be created: "
                          "the User Admin API is not available with users:write for this account")
    for m in missing.values():
        log(f"WARNING: {m}")
    use_skill_spellings(applets, spellings)
    return list(missing)


def callback_initial_targets(applets: list[dict], portal: PortalClient, log: Callable[[str], None],
                             verb: str) -> dict[str, int | None]:
    """Callback name -> the target account's ID of its initial applet in another plan, looked up by plan and
    applet name (None, with a warning, when not found: it is saved as "Return to previous queue")."""
    if not applets:
        return {}
    plans: dict[str, list[dict]] = {}
    for p in portal.list_plans():
        plans.setdefault(p["interactionPlanName"], []).append(p)
    found: dict[str, int | None] = {}
    for a in applets:
        plan, applet = a["settings"]["CallbackInitialPlan"], a["settings"]["CallbackInitialApplet"]
        matches = plans.get(plan, [])
        why = None
        if not matches:
            why = f"Interaction Plan '{plan}' does not exist in the target account"
        elif len(matches) > 1:
            why = f"the target account has {count(len(matches), 'Interaction Plan')} named '{plan}'"
        else:
            listed = portal.list_applets(matches[0]["interactionPlanId"]).get("applets", [])
            found[a["name"]] = next((x.get("id") for x in listed if x["name"] == applet), None)
            if found[a["name"]] is None:
                why = f"applet '{applet}' does not exist in Interaction Plan '{plan}' of the target account"
        if why:
            found[a["name"]] = None
            log(f"WARNING: Callback '{a['name']}' {verb} copied with 'Return to previous queue' as its initial "
                f"applet: {why}. Set it in the portal.")
    return found


def unsaved_routes(applets: list[dict], plans: list[dict], applets_of: Callable[[str], list[str]],
                   target_plan_id: str) -> dict[str, str]:
    """Route to Interaction Plan applets that cannot be saved in the target: name -> why.

    VCC saves one only with its Interaction Plan and applet (or its data source): not with the
    failure applet alone ("Please select an Interaction Plan."). The plan is looked up by name
    (plans: list_plans()), then the applet by name in it (applets_of(plan ID)). The plan being
    copied into is never offered as a target. A data source is not checked by VCC: always saved.
    """
    by_name: dict[str, list[dict]] = {}
    for p in plans:
        by_name.setdefault(p["interactionPlanName"], []).append(p)
    unsaved = {}
    for a in applets:
        if a.get("type") != SHIM:
            continue
        s = a["settings"]
        if not shim_configured(s):
            unsaved[a["name"]] = "it is not configured in the source either"
            continue
        if s["RouteType"] != SHIM_TO_PLAN:
            continue
        found = by_name.get(s["TargetPlan"], [])
        if not found:
            unsaved[a["name"]] = f"Interaction Plan '{s['TargetPlan']}' does not exist in the target account"
        elif len(found) > 1:
            unsaved[a["name"]] = f"the target account has {count(len(found), 'Interaction Plan')} named '{s['TargetPlan']}'"
        elif found[0]["interactionPlanId"] == target_plan_id:
            unsaved[a["name"]] = (f"it routes to Interaction Plan '{s['TargetPlan']}', the plan it is copied into, "
                                  "which VCC does not allow")
        elif s["TargetApplet"] not in applets_of(found[0]["interactionPlanId"]):
            unsaved[a["name"]] = (f"applet '{s['TargetApplet']}' does not exist in Interaction Plan "
                                  f"'{s['TargetPlan']}' of the target account")
    return unsaved


def match_groups(applets: list[dict], target: list[dict]) -> tuple[list[str], list[dict]]:
    """Point the ACDs' groups at the target account's groups. Return (warnings, groups to create).

    target: [{"groupDisplayId", "groupName"}]. A group is the same when display ID and name both match.
    Otherwise, as the user asked (2026-11-25): a group with the same name is used, whatever its ID; then
    one with the same ID, whatever its name; else it is to be created with the source's ID and name. Each
    case is a warning. The applets' settings are changed to the target's group, so the read-back matches.
    """
    by_id = {g["groupDisplayId"]: g for g in target}
    by_name: dict[str, dict] = {}
    for g in target:
        by_name.setdefault(g["groupName"], g)
    warnings: dict[tuple, list[str]] = {}
    create: dict[str, dict] = {}
    for a in applets:
        if a.get("type") != ACD:
            continue
        for g in a["settings"].get("Groups") or []:
            src_id, src_name = g["DisplayId"], g["Name"]
            same = by_id.get(src_id)
            if same and same["groupName"] == src_name:
                continue
            named = by_name.get(src_name)
            if named:
                g["DisplayId"] = named["groupDisplayId"]
                key = ("name", src_id, src_name, named["groupDisplayId"])
            elif same:
                g["Name"] = same["groupName"]
                key = ("id", src_id, src_name, same["groupName"])
            else:
                create.setdefault(src_id, {"groupDisplayId": src_id, "groupName": src_name})
                key = ("new", src_id, src_name, None)
            warnings.setdefault(key, []).append(a["name"])
    out = []
    for (kind, src_id, src_name, other), users in warnings.items():
        used = f" (used by {', '.join(sorted(set(users)))})"
        if kind == "name":
            out.append(f"group '{src_name}' has ID {src_id} in the source but {other} in the target account: "
                       f"matched by name{used}")
        elif kind == "id":
            out.append(f"group {src_id} is named '{src_name}' in the source but '{other}' in the target account: "
                       f"matched by ID{used}")
        else:
            out.append(f"group '{src_name}' ({src_id}) does not exist in the target account by ID or name{used}")
    return out, list(create.values())


def match_presence_states(applets: list[dict], target: set[str] | dict) -> list[str]:
    """Point the ACDs' presence states at the target's by name (letter case aside). One the target lacks
    is left as "Leave unchanged", with a warning. Return the warnings."""
    lower = {n.lower(): n for n in target}
    warnings = []
    for a in applets:
        if a.get("type") != ACD:
            continue
        s = a["settings"]
        for field in ACD_PRESENCE.values():
            name = s.get(field)
            if not name or name in target:
                continue
            if name.lower() in lower:
                s[field] = lower[name.lower()]
                continue
            label = "after an unexpected" if "Unexpected" in field else "after wrap-up"
            warnings.append(f"ACD '{a['name']}': presence state '{name}' ({label} presence) does not exist in the "
                            "target account: left as 'Leave unchanged'")
            s[field] = ""
    return warnings


def group_times(create: list[dict], source: list[dict]) -> list[str]:
    """Give each group to create the source's maximum unexpected and wrap-up times (source: the export's
    requires.groups, userAdminAPI_GroupsMax.har); return the groups without a definition there, which keep
    VCC Admin's default."""
    by_key = {(g["groupDisplayId"], g["groupName"]): g for g in source}
    unknown = []
    for g in create:
        src = by_key.get((g["groupDisplayId"], g["groupName"]))
        if src is None:
            unknown.append(f"'{g['groupName']}' ({g['groupDisplayId']})")
        for key in ("groupMaxUnexpectedTime", "groupMaxWrapUpTime"):
            g[key] = src[key] if src and src.get(key) is not None else GROUP_DEFAULT_MAX_TIME
    return unknown


def _times(g: dict) -> str:
    return f"maximum unexpected time {g['groupMaxUnexpectedTime']} s, wrap-up time {g['groupMaxWrapUpTime']} s"


def _resolve_acd_lookups(applets: list[dict], target_groups: list[dict], presence: dict, log: Callable[[str], None],
                        dry_run: bool, can_create: bool, source_groups: list[dict]) -> list[dict]:
    """Match the ACDs' groups and presence states (warnings logged); return the groups to create, with
    the source's maximum times. Without the User Admin API (can_create False), a group to create stops
    the import."""
    warnings, create = match_groups(applets, target_groups)
    for w in warnings:
        log(f"WARNING: {w}")
    for w in match_presence_states(applets, presence):
        log(f"WARNING: {w}")
    if create and not can_create:
        raise PortalError("Cannot import: groups missing in the target account cannot be created without the "
                          "User Admin API (users:write): " + ", ".join(f"'{g['groupName']}' ({g['groupDisplayId']})"
                                                                        for g in create))
    if create:
        unknown = group_times(create, source_groups)
        if unknown:
            log(f"WARNING: the export has no definition of {count(len(unknown), 'group')} ({', '.join(unknown)}; "
                f"exported without the User Admin API, users:read): {'it is' if len(unknown) == 1 else 'they are'} "
                f"created with VCC Admin's default maximum times, {GROUP_DEFAULT_MAX_TIME} seconds")
        log(f"NOTE: {count(len(create), 'group')} {are(len(create), dry_run)} created in the target account: "
            + ", ".join(f"'{g['groupName']}' ({g['groupDisplayId']}, {_times(g)})" for g in create)
            + f" ({'its' if len(create) == 1 else 'their'} source ID, name and maximum times; agents and presence "
            "states are not copied)")
    return create


def _create_groups(groups: GroupsClient, create: list[dict], log: Callable[[str], None]) -> list[dict]:
    log("0. Creating the missing groups")
    made = []
    for g in create:
        made.append(groups.create(g["groupDisplayId"], g["groupName"], g["groupMaxUnexpectedTime"],
                                  g["groupMaxWrapUpTime"]))
        log(f"   NEW GROUP created in the target account: '{g['groupName']}' ({g['groupDisplayId']}, {_times(g)})")
    return made


def schedule_router_saved(applet: dict) -> bool:
    """VCC saves a Schedule Router only with a schedule and a default applet ("'Schedule' field is required.",
    "'Default applet' field is required."): one without them is created and left empty."""
    return bool(applet["settings"].get("Schedule") and applet.get("next", {}).get("DefaultApplet"))


def plan_schedules(applets: list[dict], source: list[dict], target: list[dict]) -> tuple[dict, list[str], list[dict]]:
    """Choose the target schedule of each Schedule Router, by name, as the user asked (2026-11-26).

    source: the definitions in the export (requires.schedules); target: the target account's schedules
    ([{"id", "name", "targets"}]). A schedule's service statuses are its "targets". Return (source schedule
    name -> target schedule name, warnings, schedules to create [{"name", "body"}]):
    - same name and the same statuses: used as it is;
    - same name with all the source's statuses and more: used, with a warning (more routings may be needed);
    - same name without some of the source's statuses: the source schedule is cloned as an auxiliary
      schedule, "zz vccflow Aux <name>" (reused if it is already there with those statuses), with a warning;
    - no schedule of that name: the source schedule is cloned with its name, with a warning.
    Schedule names are case-sensitive in VCC. Without the source definition a clone is impossible: an error.
    """
    by_source = {x["name"]: x for x in source}
    by_target = {x["name"]: x for x in target}
    chosen, warnings, create = {}, [], []
    statuses_of, users = {}, {}
    for a in applets:
        if a.get("type") == SCHEDULE_ROUTER and a["settings"].get("Schedule"):
            name = a["settings"]["Schedule"]
            users.setdefault(name, set()).add(a["name"])
            own = by_source.get(name, {}).get("targets") or a.get("scheduleStatuses") or                 [k.split(".", 1)[1] for k in a.get("next", {}) if k.startswith("ScheduleMappings.")]
            statuses_of.setdefault(name, set()).update(own)
    for name, wanted in sorted(statuses_of.items()):
        used = f" (used by {', '.join(sorted(users[name]))})"

        def clone(as_name: str, why: str) -> None:
            if name not in by_source:
                raise PortalError(f"Cannot import: schedule '{name}' {why}, and the export has no definition of it to "
                                  "clone (it was exported without Schedules API access, admin:read)")
            create.append({"name": as_name, "body": by_source[name]})
            chosen[name] = as_name
            warnings.append(f"schedule '{name}' {why}{used}" if as_name == name else
                            f"schedule '{name}' {why}: it is cloned as '{as_name}'{used}")

        there = by_target.get(name)
        if there is None:
            clone(name, "does not exist in the target account")
            continue
        have = set(there.get("targets") or [])
        missing, extra = sorted(wanted - have), sorted(have - wanted)
        if not missing:
            chosen[name] = name
            if extra:
                warnings.append(f"schedule '{name}' in the target account has more service statuses than in the source: "
                                f"{', '.join(extra)}. Maybe a routing should be defined for them (they go to the "
                                f"default applet){used}")
            continue
        aux = AUX_SCHEDULE_PREFIX + name
        why = f"exists in the target account without the service statuses {', '.join(missing)}"
        old = by_target.get(aux)
        if old is not None and wanted <= set(old.get("targets") or []):
            chosen[name] = aux
            warnings.append(f"schedule '{name}' {why}: the auxiliary schedule '{aux}' already there is used{used}")
        elif old is not None:
            raise PortalError(f"Cannot import: schedule '{name}' {why}, and the auxiliary schedule '{aux}' already in "
                              "the target account lacks them too")
        else:
            clone(aux, why)
    return chosen, warnings, create


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
    skills: SkillsClient | None = None,
    groups: GroupsClient | None = None,
    schedules: SchedulesClient | None = None,
) -> list[str]:
    """Copy the exported plan in plan_dir into the target plan. Return the read-back differences.

    skills (the target account's User Admin API) lists its skills and creates those the plan's
    Skill Taggers need; created skills are reported, and deleted again if the import fails.
    groups (the same API) does the same for the groups of the plan's ACDs (see match_groups); their
    presence states are matched by name (see match_presence_states). schedules (the target's Schedules
    API) chooses or clones the schedules of the plan's Schedule Routers (see plan_schedules); cloned
    schedules are reported, and deleted again if the import fails.

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
        log(f"NOTE: '{AUX_SDS}' in the source plan is not copied as an applet; its {count(len(aux_keys), 'data source')} "
            f"{are(len(aux_keys))} checked against the target account")
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
                log(f"NOTE: {count(len(already), 'data source')} of the source's '{AUX_SDS}' already "
                    f"{'exists' if len(already) == 1 else 'exist'} in the target "
                    f"account, not added: {', '.join(already)}")
    aux_in_target = any(a["name"] == AUX_SDS for a in listing.get("applets", []))
    if problems:
        raise PortalError("Cannot import:\n  " + "\n  ".join(problems))
    applets = doc["applets"]
    # Static skills must exist in the target account: missing ones are created through the User Admin
    # API. Without it, the skill list is read from any Skill Tagger page (or from the first Skill
    # Tagger this import creates) and a missing skill stops the import.
    taggers = [a["name"] for a in applets if a["type"] == SKILL_TAGGER and a["settings"].get("Skills")]
    skills_unchecked, skills_to_create = False, []
    if taggers:
        existing_skills = None
        if skills is not None:
            try:
                existing_skills = skills.names()
            except ApiError as e:
                log(f"WARNING: cannot list the target account's skills through the User Admin API ({e}); "
                    "missing skills cannot be created")
                skills = None
        if existing_skills is None:
            existing_skills = portal.list_skills(target_plan_id)
        if existing_skills is None:
            skills_unchecked = True
            log("NOTE: the target account has no Skill Tagger to read its skills from; they are checked "
                "after creating the applets, before saving any")
        else:
            skills_to_create = _resolve_skills(applets, existing_skills, log, skills is not None)
        if skills_to_create:
            log(f"NOTE: {count(len(skills_to_create), 'skill')} {are(len(skills_to_create), dry_run)} created in "
                f"the target account: {', '.join(repr(s) for s in skills_to_create)}")
    # ACD groups and presence states: by name in the target account. The groups come from the User Admin
    # API (users:read), else from an ACD page; presence states from an ACD page. Without an ACD in the target
    # account, both are read from the first ACD this import creates, before saving any.
    acds = [a for a in applets if a["type"] == ACD]
    acd_unchecked, groups_to_create = False, []
    if acds:
        target_groups = None
        if groups is not None:
            try:
                target_groups = groups.list()
            except ApiError as e:
                log(f"WARNING: cannot list the target account's groups through the User Admin API ({e}); "
                    "missing groups cannot be created")
                groups = None
        lookups = portal.acd_lookups(target_plan_id)
        if target_groups is None and lookups is not None:
            target_groups = lookups["groups"]
        if lookups is None:
            acd_unchecked = True
            log("NOTE: the target account has no ACD to read its presence states (and groups) from; they are "
                "checked after creating the applets, before saving any")
        else:
            groups_to_create = _resolve_acd_lookups(applets, target_groups, lookups["presence"], log, dry_run,
                                                    groups is not None, doc.get("requires", {}).get("groups") or [])
    # Schedule Routers: the schedule by name in the target account (Schedules API, admin:read), cloned
    # where the target lacks it or its service statuses.
    routers = [a for a in applets if a["type"] == SCHEDULE_ROUTER]
    empty_schedule_routers = {a["name"] for a in routers if not schedule_router_saved(a)}
    chosen_schedules, schedules_to_create = {}, []
    if len(routers) > len(empty_schedule_routers):
        target_schedules = None
        if schedules is not None:
            try:
                target_schedules = schedules.list()
            except ApiError as e:
                problems_sr = f"the target account's schedules cannot be listed ({e})"
        if target_schedules is None:
            raise PortalError("Cannot import the Schedule Routers: "
                              + (problems_sr if schedules is not None else "no Schedules API client was given"))
        chosen_schedules, warnings, schedules_to_create = plan_schedules(
            [a for a in routers if a["name"] not in empty_schedule_routers], doc["requires"].get("schedules") or [],
            target_schedules)
        for w in warnings:
            log(f"WARNING: {w}")
        if schedules_to_create:
            log(f"NOTE: {count(len(schedules_to_create), 'schedule')} {are(len(schedules_to_create), dry_run)} "
                "created in the target "
                f"account: {', '.join(repr(c['name']) for c in schedules_to_create)}")
    for name in sorted(empty_schedule_routers):
        log(f"WARNING: Schedule Router '{name}' has no schedule or default applet: it {'would be' if dry_run else 'is'} "
            "created empty and not saved (VCC needs both to save it)")
    log(f"Checks passed: {count(len(applets), 'applet')} from '{doc['plan']['name']}' into '{target_plan_name}'")
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
    for a in applets:
        if a.get("connectionProblems"):  # found by the export: references that point nowhere are not copied
            log(f"WARNING: '{a['name']}' has broken references in the source, not copied: "
                + "; ".join(a["connectionProblems"]))
        if (selected := export.dial_in_config_empty_selection(a)) is not None:
            log(f"WARNING: Dial In Config '{a['name']}' selects configuration {selected}, which is empty "
                "(no applet, alternative text or announcement)")
    callback_targets = callback_initial_targets(
        [a for a in applets if a["type"] == export.CALLBACK and a["settings"].get("CallbackInitialPlan")],
        portal, log, verb)
    routes = [a for a in applets if a["type"] == SHIM]
    unsaved = {}
    if routes:
        unsaved = unsaved_routes(routes, portal.list_plans(),
                                 lambda pid: [x["name"] for x in portal.list_applets(pid).get("applets", [])],
                                 target_plan_id)
    for a in routes:
        if a["name"] in unsaved:
            failure = a.get("next", {}).get("FailureApplet")
            log(f"WARNING: Route to Interaction Plan '{a['name']}' {verb} copied without its settings: "
                f"{unsaved[a['name']]}. VCC does not save it without its Interaction Plan and applet (or data source)"
                + (f", so its failure applet '{failure}' is not set either" if failure else "")
                + ". Set them in the portal.")
    if to_create:
        where = "added to the existing" if aux_in_target else "set in a new"
        log(f"NOTE: the {count(len(to_create), 'missing data source')} {are(len(to_create), dry_run)} {where} auxiliary "
            f"Set Data Source '{AUX_SDS}', which stays in the plan (VCC keeps a data source only while an applet sets "
            f"it; nothing leads to this applet): {', '.join(to_create)}")
    if dry_run:
        for a in applets:
            log(f"  would create {a['name']} ({a['type']})")
        log(f"  would set the initial applet to {doc['plan'].get('initialApplet')!r}")
        return []

    created: list[str] = []
    created_skills: list[dict] = []
    created_groups: list[dict] = []
    created_schedules: list[dict] = []
    try:
        if skills_to_create:
            log("0. Creating the missing skills")
            for name in skills_to_create:
                created_skills.append(skills.create(name))
                log(f"   NEW SKILL created in the target account: '{name}'")
        if groups_to_create:
            created_groups += _create_groups(groups, groups_to_create, log)
        if schedules_to_create:
            log("0. Creating the missing schedules")
            for c in schedules_to_create:
                saved, _, notes = schedules.copy_to(c["body"], c["name"])
                created_schedules.append(saved)
                log(f"   NEW SCHEDULE created in the target account: '{c['name']}' "
                    f"(service statuses: {', '.join(saved.get('targets') or [])})")
                for n in notes:
                    log(f"   NOTE: {n}")
        schedule_ids = {x["name"]: x["id"] for x in schedules.list()} if chosen_schedules else {}
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
        if skills_unchecked:
            _resolve_skills(applets, portal.get_skill_tagger(by_name[taggers[0]]).get("AllSkills") or [], log, False)
        if acd_unchecked:
            form = portal.get_acd(by_name[acds[0]["name"]])["form"]
            target_groups = target_groups if target_groups is not None else acd_groups(form)
            late = _resolve_acd_lookups(applets, target_groups, acd_presence_states(form), log, False, groups is not None,
                                        doc.get("requires", {}).get("groups") or [])
            if late:
                created_groups += _create_groups(groups, late, log)

        log("2. Saving settings and connections")
        for a in applets:
            nxt = a.get("next", {})
            if a["type"] == DATA_CONNECTOR:
                portal.save_data_connector(target_plan_id, a["name"], a["settings"], nxt)
            elif a["type"] == SKILL_TAGGER:
                portal.save_skill_tagger(target_plan_id, a["name"], a["settings"], nxt)
            elif a["type"] == ACD:
                portal.save_acd(target_plan_id, a["name"], a["settings"], nxt,
                                {m["property"]: plan_dir / m["file"] for m in a.get("media", [])})
                if a.get("media"):
                    log(f"   uploaded {count(len(a['media']), 'audio file')}: {', '.join(m['property'] for m in a['media'])}")
            elif a["type"] == SCHEDULE_ROUTER and a["name"] in empty_schedule_routers:
                log(f"   {a['name']}: Schedule Router created empty and not saved (see the warning above)")
                continue
            elif a["type"] == SCHEDULE_ROUTER:
                schedule = chosen_schedules[a["settings"]["Schedule"]]
                guid = schedule_ids[schedule]
                props = dict(portal.get_applet_state(target_plan_id, by_name[a["name"]])["appletProperties"])
                props.update({k: v for k, v in a["settings"].items() if k != "Schedule"})
                # As the editor saves it (scheduleRouter.har): the schedule GUID, applets by numeric ID, and
                # one mapping per routed service status, keyed "<schedule GUID>:<status>".
                props["Schedule"] = guid
                props["DefaultAppletId"] = by_name[nxt["DefaultApplet"]]["id"]
                props["FailureAppletId"] = by_name[nxt["FailureApplet"]]["id"] if nxt.get("FailureApplet") else None
                props["ScheduleMappings"] = {f"{guid}:{k.split('.', 1)[1]}": by_name[t]["id"]
                                             for k, t in nxt.items() if k.startswith("ScheduleMappings.")}
                portal.save_applet(target_plan_id, a["name"], props)
                a["settings"]["Schedule"] = schedule  # the read-back shows the target's schedule
            elif a["type"] == COLLECT_INPUT:
                portal.save_collect_input(target_plan_id, a["name"], a["settings"], nxt,
                                          {m["property"]: plan_dir / m["file"] for m in a.get("media", [])})
                if a.get("media"):
                    log(f"   uploaded {count(len(a['media']), 'audio file')}: {', '.join(m['property'] for m in a['media'])}")
            elif a["type"] == DIAL_IN_CONFIG:
                audio = {int(m["property"].split(".")[1]): plan_dir / m["file"] for m in a.get("media", [])}
                portal.save_dial_in_config(target_plan_id, a["name"], a["settings"], nxt, audio)
                if audio:
                    n = len(audio)
                    log(f"   uploaded {count(n, 'announcement')}: configuration{'' if n == 1 else 's'} "
                        f"{', '.join(map(str, sorted(audio)))}")
            elif a["type"] == SHIM and a["name"] in unsaved:
                log(f"   {a['name']}: Route to Interaction Plan created without its settings (see the warning above)")
                continue
            elif a["type"] == SHIM:
                portal.save_shim(target_plan_id, a["name"], a["settings"], nxt)
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
                if a["type"] == export.CALLBACK:
                    plan_target = a["settings"].get("CallbackInitialPlan")
                    for k in export.CB_OTHER_PLAN:
                        props.pop(k, None)
                    # As the editor posts it: the applet's ID only; VCC fills in its GUID and plan.
                    props.update(CallbackInitialAppletId=callback_targets.get(a["name"]) if plan_target else None,
                                 CallbackInitialAppletGuid=None, CallbackInitialInteractionPlanId=None)
                for key, target in nxt.items():
                    t = by_name[target]
                    if a["type"] == export.CALLBACK and key == export.CB_INITIAL:
                        props["CallbackInitialAppletId"] = t.get("id")
                        continue
                    if a["type"] == export.CALLBACK and key in export.CB_GUID_APPLETS:
                        props[f"{key}Guid"] = t.get("guid")
                        continue
                    if key.startswith("Choices."):
                        props["Choices"][key[len("Choices."):]] = target
                    elif key == "TimeoutApplet":
                        props["TimeoutAppletName"], props["TimeoutAppletGuid"] = target, t.get("guid")
                    elif key == "FailureApplet":
                        props["FailureAppletId"], props["FailureAppletGuid"] = t.get("id"), t.get("guid")
                    elif a["type"] == export.LOOP_COUNTER and key == export.LC_MAXIMUM:
                        props["NextAppletIdOnMaximumReached"] = t.get("id")
                        props["NextAppletGuidOnMaximumReached"] = t.get("guid")
                    elif a["type"] == export.CALL_CONNECT_ROUTER and key in export.CCR_APPLETS:
                        props[f"{key}Id"] = t.get("id")  # by numeric ID only, as the editor saves it
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
        log(f"Import failed; deleting the {count(len(created), 'applet')} it created")
        for name in reversed(created):
            try:
                portal.delete_applet(target_plan_id, name)
            except PortalError as e:
                log(f"   could not delete '{name}': {e}")
        for c in created_schedules:
            try:
                schedules.delete(c["id"])
                log(f"   deleted the schedule it created: '{c['name']}'")
            except ApiError as e:
                log(f"   could not delete the schedule it created, '{c.get('name')}': {e}")
        for g in created_groups:
            try:
                groups.delete(g["groupId"])
                log(f"   deleted the group it created: '{g['groupName']}' ({g['groupDisplayId']})")
            except ApiError as e:
                log(f"   could not delete the group it created, '{g.get('groupName')}': {e}")
        for s in created_skills:
            try:
                skills.delete(s["skillId"])
                log(f"   deleted the skill it created: '{s['skillName']}'")
            except ApiError as e:
                log(f"   could not delete the skill it created, '{s.get('skillName')}': {e}")
        raise

    log("4. Reading the copy back and comparing")
    with tempfile.TemporaryDirectory() as tmp:
        copy = export.export_plan(portal, target_plan_id, target_plan_name, Path(tmp), log=lambda _: None)
    # The copy holds the target's other applets too; compare only the imported ones.
    copy["applets"] = [a for a in copy["applets"] if a["name"] in {x["name"] for x in applets}]
    notes: list[str] = []
    diffs = compare(doc, copy, notes)
    # Routes left without their settings were warned about above; their difference is expected.
    diffs = [d for d in diffs if not any(d.startswith(f"'{n}' ") for n in unsaved)]
    unset = sorted(n for n, i in callback_targets.items() if i is None)
    diffs = [d for d in diffs if not any(d.startswith(f"'{n}' setting CallbackInitial") for n in unset)]
    if unset:
        log(f"NOTE: the comparison leaves out the initial applet of {count(len(unset), 'Callback')} copied with "
            f"'Return to previous queue' (see the warnings above): {', '.join(unset)}")
    if unsaved:
        log(f"NOTE: the comparison leaves out the {count(len(unsaved), 'Route to Interaction Plan applet')} copied without "
            f"their settings (see the warnings above): {', '.join(sorted(unsaved))}")
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
