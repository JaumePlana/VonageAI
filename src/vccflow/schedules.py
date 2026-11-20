"""Schedules (VCC Admin "Schedules"), through the API the page uses (schedules.har, 2026-11-20).

  GET    /schedule/definitions            {"schedules": [schedule, ...]}, each in full
  POST   /schedule/definitions            schedule without id -> 201 {"id": "<guid>"}
  PUT    /schedule/definitions/<id>       the whole schedule (replaces it)
  DELETE /schedule/definitions/<id>       204

A schedule: name, timezone, defaultTarget (the service status outside every slot),
is24HourFormat, targets (its service statuses; a new one has "Open" and "Closed"),
dayDefinitions {name: {periods: [{start, end, target}]}} holding the slots, dayOfWeek
{monday: <dayDefinition>, ...} for the active weekdays (an inactive day is absent and gets
defaultTarget), and customDays [{name, date, dayDefinition, enabled}] for special days.
A special day repeated every year has a date without year ("12-25"); a one-off day has one
("2026-12-24"). "All day" is the slot 00:00:00.0000000 - 23:59:59.9999999. The list also
returns id, createdBy, lastUpdated, lastUpdatedBy and scheduledHolidays, which a save does
not send.

Reads need the scope admin:read (a token with only admin:write gets 403); writes admin:write.
"""

from __future__ import annotations

import copy
import re
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from vccflow.config import Account
from vccflow.scoped_api import ApiError, ScopedApi

PATH = "schedule/definitions"
READ_SCOPES = ("admin:read",)
WRITE_SCOPES = ("admin:write",)
# Returned by the list, not sent by the page when saving.
READ_ONLY = ("id", "createdBy", "lastUpdated", "lastUpdatedBy", "scheduledHolidays")
SAVED = ("name", "timezone", "defaultTarget", "is24HourFormat", "targets", "dayDefinitions", "dayOfWeek", "customDays")
ONE_OFF = re.compile(r"^\d{4}-\d{2}-\d{2}$")
YEARLY = re.compile(r"^\d{2}-\d{2}$")


class SchedulesError(ApiError):
    pass


def today_in(tz_name: str) -> tuple[date, str | None]:
    """Today's date where the schedule is (its time zone), or in UTC with a note if unknown."""
    try:
        return datetime.now(ZoneInfo(tz_name)).date(), None
    except (ZoneInfoNotFoundError, ValueError):
        return datetime.now(timezone.utc).date(), f"time zone '{tz_name}' unknown here: today taken in UTC"


def body_of(schedule: dict) -> dict:
    """What a save sends: the schedule without its ID and the fields only the list returns."""
    return {k: copy.deepcopy(schedule[k]) for k in SAVED if k in schedule}


def check(body: dict) -> list[str]:
    """Problems the portal's rules would refuse, found before any write."""
    problems = []
    for k, label in (("name", "a name"), ("timezone", "a time zone")):
        if not str(body.get(k) or "").strip():
            problems.append(f"it has no {label}: VCC needs at least a name and a time zone")
    targets = set(body.get("targets") or [])
    if body.get("defaultTarget") not in targets:
        problems.append(f"default service status '{body.get('defaultTarget')}' is not one of its statuses")
    defs = body.get("dayDefinitions") or {}
    for name, d in defs.items():
        periods = d.get("periods") or []
        if not periods:
            problems.append(f"day '{name}' has no time slot")
        for p in periods:
            if p.get("target") not in targets:
                problems.append(f"day '{name}' slot {p.get('start')}-{p.get('end')} uses unknown status '{p.get('target')}'")
    for day, ref in (body.get("dayOfWeek") or {}).items():
        if ref not in defs:
            problems.append(f"{day} refers to a missing day definition '{ref}'")
    names, dates = set(), set()
    for c in body.get("customDays") or []:
        if c["name"] in names:  # case-sensitive, like VCC ("Test Repeated" and "test repeated" coexist)
            problems.append(f"special day name '{c['name']}' appears twice")
        if c["date"] in dates:
            problems.append(f"special day date {c['date']} appears twice")
        if not (ONE_OFF.match(c["date"]) or YEARLY.match(c["date"])):
            problems.append(f"special day '{c['name']}' has an unexpected date '{c['date']}'")
        if c.get("dayDefinition") not in defs:
            problems.append(f"special day '{c['name']}' refers to a missing day definition '{c.get('dayDefinition')}'")
        names.add(c["name"])
        dates.add(c["date"])
    return problems


def prepare_copy(schedule: dict, new_name: str, today: date) -> tuple[dict, list[str]]:
    """The body to create a copy named new_name, and notes on what the copy leaves out.

    Special days repeated every year (date "MM-DD") are kept: they have no year to move.
    One-off special days are kept if they are today or later (the portal accepts no past
    date) and left out otherwise, with their slots when nothing else uses them.
    """
    body = body_of(schedule)
    body["name"] = new_name
    notes = []
    if schedule.get("scheduledHolidays"):
        notes.append(f"scheduledHolidays ({len(schedule['scheduledHolidays'])}) not copied: their format has not been recorded")
    kept, dropped = [], []
    for c in body.get("customDays") or []:
        if ONE_OFF.match(c["date"]) and date.fromisoformat(c["date"]) < today:
            dropped.append(c)
        else:
            kept.append(c)
    body["customDays"] = kept
    used = set((body.get("dayOfWeek") or {}).values()) | {c["dayDefinition"] for c in kept}
    for c in dropped:
        notes.append(f"special day '{c['name']}' ({c['date']}) left out: a one-off date in the past")
        if c["dayDefinition"] not in used:
            body["dayDefinitions"].pop(c["dayDefinition"], None)
    return body, notes


def comparable(schedule: dict) -> dict:
    """A schedule for comparison: saved fields only, special days by name, times as HH:MM:SS(.fff)."""
    b = body_of(schedule)
    b["customDays"] = sorted(b.get("customDays") or [], key=lambda c: (c["name"].lower(), c["name"]))
    for d in (b.get("dayDefinitions") or {}).values():
        for p in d.get("periods") or []:  # the API returns 09:00:00.0000000 where the page sends 09:00:00
            p["start"], p["end"] = (re.sub(r"\.0+$", "", p[k]) for k in ("start", "end"))
    return b


def describe(schedule: dict) -> str:
    """One line per schedule for --summary."""
    days = ", ".join(d[:3] for d in (schedule.get("dayOfWeek") or {}))
    special = schedule.get("customDays") or []
    yearly = sum(1 for c in special if YEARLY.match(c["date"]))
    return (f"{schedule['name']} | {schedule.get('timezone')} | default {schedule.get('defaultTarget')} | "
            f"statuses: {', '.join(schedule.get('targets') or [])} | active days: {days or 'none'} | "
            f"special days: {len(special)} ({yearly} yearly) | id {schedule.get('id')}")


class SchedulesClient(ScopedApi):
    error = SchedulesError
    what = "schedules"

    def __init__(self, account: Account, *args, **kwargs):
        super().__init__(account, *args, **kwargs)
        self.base = f"{account.manager_api_url}/{PATH}"

    def list(self) -> list[dict]:
        return self._call("GET", self.base, READ_SCOPES).json().get("schedules", [])

    def find(self, name_or_id: str) -> dict:
        found = [s for s in self.list() if name_or_id in (s.get("id"), s.get("name"))]
        if len(found) != 1:
            raise SchedulesError(f"Expected one schedule named or with ID '{name_or_id}' in '{self.account.name}', "
                                 f"found {len(found)}")
        return found[0]

    def create(self, body: dict) -> dict:
        """Create a schedule as the page does: an empty one first, then the whole definition.

        Returns it as read back. If the second step fails, the empty schedule is deleted again.
        """
        self._require_writes()
        problems = check(body)
        if problems:
            raise SchedulesError(f"Schedule '{body['name']}' cannot be saved: " + "; ".join(problems))
        if any(s["name"] == body["name"] for s in self.list()):  # names are case-sensitive
            raise SchedulesError(f"A schedule named '{body['name']}' already exists in '{self.account.name}'")
        skeleton = {**{k: body[k] for k in ("name", "timezone", "defaultTarget", "is24HourFormat", "targets")},
                    "dayDefinitions": {}, "dayOfWeek": {}, "customDays": []}
        new_id = self._call("POST", self.base, WRITE_SCOPES, json=skeleton).json()["id"]
        if body == skeleton:  # an empty schedule: the first step already created all of it
            return next(s for s in self.list() if s.get("id") == new_id)
        try:
            self._call("PUT", f"{self.base}/{new_id}", WRITE_SCOPES, json=body)
        except Exception:
            self._call("DELETE", f"{self.base}/{new_id}", WRITE_SCOPES)
            raise
        return next(s for s in self.list() if s.get("id") == new_id)

    def delete(self, name_or_id: str) -> dict:
        """Delete one schedule, given its exact name or ID. Return it as it was; check it is gone.

        VCC does not say whether an interaction plan uses the schedule (Schedule Router applets).
        """
        self._require_writes()
        schedule = self.find(name_or_id)
        self._call("DELETE", f"{self.base}/{schedule['id']}", WRITE_SCOPES)
        if any(s.get("id") == schedule["id"] for s in self.list()):
            raise SchedulesError(f"Schedule '{schedule['name']}' is still listed after the delete")
        return schedule

    def copy_to(self, source: dict, new_name: str, target: "SchedulesClient | None" = None,
                dry_run: bool = False) -> tuple[dict | None, dict, list[str]]:
        """Copy a schedule into this account (target None) or another one, as new_name.

        Returns (the copy as read back, or None on a dry run; the body sent; notes)."""
        target = target or self
        today, tz_note = today_in(source.get("timezone") or "UTC")
        body, notes = prepare_copy(source, new_name, today)
        if tz_note:
            notes.insert(0, tz_note)
        problems = check(body)
        if any(s["name"] == new_name for s in target.list()):  # "jaume test" and "Jaume Test" coexist
            problems.append(f"a schedule named '{new_name}' already exists in '{target.account.name}'")
        if problems:
            raise SchedulesError(f"Cannot copy '{source['name']}' as '{new_name}': " + "; ".join(problems))
        if dry_run:
            return None, body, notes
        saved = target.create(body)
        if comparable(saved) != comparable(body):
            diff = [k for k in SAVED if comparable(saved).get(k) != comparable(body).get(k)]
            notes.append(f"read back differs from what was sent in: {', '.join(diff)}")
        return saved, body, notes


ALL_DAY = ("00:00:00", "23:59:59")


def _hhmm(t: str) -> str:
    return t[:5]


def show_slots(slots: list | None) -> str:
    """Slots as "09:00-14:00 Open, 15:00-16:00 Open"; the all-day slot as "all day Holidays"."""
    if slots is None:
        return "inactive"
    return ", ".join(f"all day {t}" if (s[:8], e[:8]) == ALL_DAY else f"{_hhmm(s)}-{_hhmm(e)} {t}" for s, e, t in slots)


def differences(a: dict, b: dict) -> list[str]:
    """Readable differences between two schedules (names aside), for compare-schedules."""
    out = []
    x, y = comparable(a), comparable(b)
    for k in ("timezone", "defaultTarget", "is24HourFormat"):
        if x.get(k) != y.get(k):
            out.append(f"{k}: {x.get(k)} | {y.get(k)}")
    if sorted(x.get("targets") or []) != sorted(y.get("targets") or []):
        out.append(f"service statuses: {', '.join(x.get('targets') or [])} | {', '.join(y.get('targets') or [])}")

    def slots(s: dict, ref: str | None) -> list | None:
        periods = ((s.get("dayDefinitions") or {}).get(ref) or {}).get("periods", []) if ref else None
        return [(p["start"], p["end"], p["target"]) for p in periods] if periods is not None else None

    for day in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"):
        sa, sb = slots(x, (x.get("dayOfWeek") or {}).get(day)), slots(y, (y.get("dayOfWeek") or {}).get(day))
        if sa != sb:
            out.append(f"{day}: {show_slots(sa)} | {show_slots(sb)}")
    ca = {c["name"]: c for c in x.get("customDays") or []}
    cb = {c["name"]: c for c in y.get("customDays") or []}
    for n in sorted(ca.keys() | cb.keys(), key=lambda k: (k.lower(), k)):
        if n not in cb or n not in ca:
            out.append(f"special day '{n}' only in the {'first' if n in ca else 'second'}")
            continue
        for label, va, vb in (("date", ca[n]["date"], cb[n]["date"]), ("enabled", ca[n].get("enabled"), cb[n].get("enabled")),
                              ("slots", slots(x, ca[n]["dayDefinition"]), slots(y, cb[n]["dayDefinition"]))):
            if va != vb:
                shown = (show_slots(va), show_slots(vb)) if label == "slots" else (va, vb)
                out.append(f"special day '{n}' {label}: {shown[0]} | {shown[1]}")
    return out
