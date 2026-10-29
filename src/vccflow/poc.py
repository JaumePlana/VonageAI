"""Step 1 POC: create an Announcer applet in a plan, verify it, and (by default) delete it."""

from __future__ import annotations

import re
from typing import Callable

from vccflow.ipm import IpmClient
from vccflow.portal import PortalClient, PortalError

UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

ANNOUNCER_TYPE = "DataAnnouncer"


def resolve_plan(plan: str, portal: PortalClient, ipm: IpmClient | None) -> tuple[str, str]:
    """Return (plan_id, plan_name) for a plan ID or name.

    When API credentials exist, the plan is also looked up through the IPM API,
    which is bound to the account. Checking that the portal session sees the same
    plan guards against a session that belongs to a different account.
    """
    api_name = None
    if UUID.match(plan):
        plan_id = plan
        if ipm:
            api_name = ipm.get_plan(plan_id)["interactionPlanName"]
    else:
        if not ipm:
            raise PortalError("Plan names need API credentials for this account; pass the plan ID instead")
        matches = [p for p in ipm.list_plans() if p["interactionPlanName"] == plan]
        if len(matches) != 1:
            raise PortalError(f"Expected one plan named '{plan}', found {len(matches)}")
        plan_id, api_name = matches[0]["interactionPlanId"], plan

    portal_name = portal.list_applets(plan_id).get("name")
    if not portal_name:
        raise PortalError(f"The portal session cannot see plan {plan_id}; is it logged in to the right account?")
    if api_name is not None and portal_name != api_name:
        raise PortalError(f"Account mismatch: API says '{api_name}', portal session says '{portal_name}'")
    return plan_id, portal_name


def run_announcer_poc(
    portal: PortalClient,
    plan_id: str,
    name: str,
    text: str,
    voice: str = "Amy",
    keep: bool = False,
    log: Callable[[str], None] = print,
) -> dict:
    """Create, configure, verify and optionally delete an Announcer. Return its saved properties."""
    if any(a["name"] == name for a in portal.list_applets(plan_id).get("applets", [])):
        raise PortalError(f"An applet named '{name}' already exists in this plan; pick another --name")

    log(f"1. Creating Announcer '{name}'")
    portal.create_applet(plan_id, ANNOUNCER_TYPE, name)
    applet = portal.find_applet(plan_id, name)

    log("2. Reading its default settings")
    props = dict(portal.get_applet_state(plan_id, applet)["appletProperties"])

    wanted = {"AnnouncementType": "Text", "Voice": voice, "Text": text, "BargeIn": True}
    log(f"3. Saving {wanted}")
    portal.save_applet(plan_id, name, {**props, **wanted})

    log("4. Reading it back")
    saved = portal.get_applet_state(plan_id, portal.find_applet(plan_id, name))["appletProperties"]
    diffs = {k: (v, saved.get(k)) for k, v in wanted.items() if saved.get(k) != v}
    if diffs:
        raise PortalError(f"Saved settings differ from what was sent (sent, read back): {diffs}")
    log("   Settings match.")

    if keep:
        log("5. Keeping the applet (--keep)")
    else:
        log("5. Deleting it")
        portal.delete_applet(plan_id, name)
        if any(a["name"] == name for a in portal.list_applets(plan_id).get("applets", [])):
            raise PortalError(f"Applet '{name}' is still there after delete")
        log("   Deleted.")
    return saved
