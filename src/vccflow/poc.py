"""Step 1 POC: create an Announcer applet in a plan, verify it, and (by default) delete it."""

from __future__ import annotations

from typing import Any, Callable

from vccflow.ipm import IpmError
from vccflow.manager import ManagerError
from vccflow.portal import PortalClient, PortalError

ANNOUNCER_TYPE = "DataAnnouncer"


def _id_and_name(plan: dict) -> tuple[str, str]:
    """Plan ID and name from an Interaction Manager, IPM or portal plan record."""
    return plan.get("id") or plan["interactionPlanId"], plan.get("name") or plan["interactionPlanName"]


def resolve_plan(plan: str, portal: PortalClient, api: Any | None) -> tuple[str, str]:
    """Return (plan_id, plan_name) for a plan ID or name.

    api is a ManagerClient (or IpmClient): any client whose list_plans() returns the
    account's plans. That API is bound to the account through its credentials, so
    checking that the portal session sees the same plan catches a session from a
    different account. Without it (no credentials, or HTTP 403), the plan is looked
    up on the portal's plan page only, and that check is not possible.
    """
    api_name = None
    plans = None
    if api:
        try:
            plans = [_id_and_name(p) for p in api.list_plans()]
        except (IpmError, ManagerError) as e:
            if e.status != 403:
                raise
    from_api = plans is not None
    if not from_api:
        plans = [_id_and_name(p) for p in portal.list_plans()]
    by_id = [p for p in plans if p[0] == plan]
    matches = by_id or [p for p in plans if p[1] == plan]
    if len(matches) != 1:
        where = "the API" if from_api else "the portal"
        raise PortalError(f"Expected one plan with ID or name '{plan}' in {where}, found {len(matches)}")
    plan_id, name = matches[0]
    if from_api:
        api_name = name

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
