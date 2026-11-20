"""Schedules: copy rules, checks, the create sequence and comparison (schedules.har)."""

import copy
from datetime import date

import pytest

from vccflow.config import Account
from vccflow.schedules import (
    SchedulesClient, SchedulesError, body_of, check, differences, prepare_copy, show_slots,
)

ALL_DAY = {"start": "00:00:00.0000000", "end": "23:59:59.9999999", "target": "Holidays"}


def schedule(**over):
    s = {
        "id": "2907cb12", "createdBy": "x@example.com", "lastUpdated": "2026-11-20T13:16:09Z", "lastUpdatedBy": "x",
        "name": "Jaume Test", "timezone": "America/Panama", "defaultTarget": "Closed", "is24HourFormat": True,
        "targets": ["Closed", "Open", "Holidays"],
        "dayDefinitions": {
            "_regularDayMonday": {"periods": [{"start": "09:00:00", "end": "14:00:00", "target": "Open"},
                                              {"start": "15:00:00", "end": "16:00:00", "target": "Open"}]},
            "Christmas": {"periods": [ALL_DAY]},
            "Old one-off": {"periods": [{"start": "12:00:00", "end": "13:00:00", "target": "Closed"}]},
            "Future one-off": {"periods": [ALL_DAY]},
        },
        "dayOfWeek": {"monday": "_regularDayMonday"},
        "customDays": [
            {"name": "Christmas", "date": "12-25", "dayDefinition": "Christmas", "enabled": True},
            {"name": "Old one-off", "date": "2026-11-01", "dayDefinition": "Old one-off", "enabled": True},
            {"name": "Future one-off", "date": "2027-01-28", "dayDefinition": "Future one-off", "enabled": True},
        ],
        "scheduledHolidays": [],
    }
    s.update(over)
    return s


TODAY = date(2026, 11, 20)


def test_body_leaves_out_what_only_the_list_returns():
    b = body_of(schedule())
    assert set(b) == {"name", "timezone", "defaultTarget", "is24HourFormat", "targets", "dayDefinitions",
                      "dayOfWeek", "customDays"}


def test_copy_keeps_yearly_and_future_days_and_drops_past_one_offs():
    body, notes = prepare_copy(schedule(), "Copy", TODAY)
    assert body["name"] == "Copy"
    assert [c["name"] for c in body["customDays"]] == ["Christmas", "Future one-off"]
    assert body["customDays"][0]["date"] == "12-25"  # yearly: no year, copied as is
    assert "Old one-off" not in body["dayDefinitions"]  # its slots go with it
    assert notes == ["special day 'Old one-off' (2026-11-01) left out: a one-off date in the past"]
    today_one_off = schedule(customDays=[{"name": "Today", "date": "2026-11-20", "dayDefinition": "Christmas", "enabled": True}])
    assert [c["name"] for c in prepare_copy(today_one_off, "Copy", TODAY)[0]["customDays"]] == ["Today"]


def test_slots_shared_with_a_kept_day_stay():
    s = schedule()
    s["customDays"][1]["dayDefinition"] = "Christmas"  # the past day uses the same slots as Christmas
    body, _ = prepare_copy(s, "Copy", TODAY)
    assert "Christmas" in body["dayDefinitions"]


def test_scheduled_holidays_are_not_copied_and_noted():
    body, notes = prepare_copy(schedule(scheduledHolidays=[{"x": 1}]), "Copy", TODAY)
    assert "scheduledHolidays" not in body and any("scheduledHolidays (1) not copied" in n for n in notes)


def test_check_finds_what_vcc_refuses():
    assert check(body_of(schedule())) == []
    s = body_of(schedule())
    s["customDays"].append({"name": "Christmas", "date": "12-25", "dayDefinition": "Christmas", "enabled": True})
    s["dayDefinitions"]["_regularDayMonday"]["periods"][0]["target"] = "Lunch"
    s["dayOfWeek"]["tuesday"] = "_regularDayTuesday"
    problems = " | ".join(check(s))
    assert "name 'Christmas' appears twice" in problems and "date 12-25 appears twice" in problems
    assert "unknown status 'Lunch'" in problems and "missing day definition '_regularDayTuesday'" in problems
    # names differing only in letter case coexist in VCC
    s = body_of(schedule())
    s["customDays"].append({"name": "christmas", "date": "12-24", "dayDefinition": "Christmas", "enabled": True})
    assert check(s) == []


class FakeApi:
    def __init__(self, schedules, fail_put=False):
        self.schedules, self.calls, self.fail_put = schedules, [], fail_put

    def request(self, method, url, headers=None, json=None, timeout=None):
        from unittest.mock import MagicMock
        self.calls.append((method, url.rsplit("definitions", 1)[1], json))
        r = MagicMock(status_code=200)
        if method == "GET":
            r.json.return_value = {"schedules": copy.deepcopy(self.schedules)}
        elif method == "POST":
            self.schedules.append({**json, "id": "new-id"})
            r.status_code, r.json.return_value = 201, {"id": "new-id"}
        elif method == "PUT":
            if self.fail_put:
                r.status_code, r.text = 400, '{"message":"bad"}'
                return r
            s = next(s for s in self.schedules if s["id"] == url.rsplit("/", 1)[1])
            s.update(copy.deepcopy(json))
            for d in s["dayDefinitions"].values():  # the API answers with seven decimals
                for p in d["periods"]:
                    p["start"] = p["start"] if "." in p["start"] else p["start"] + ".0000000"
        elif method == "DELETE":
            self.schedules = [s for s in self.schedules if s["id"] != url.rsplit("/", 1)[1]]
            r.status_code = 204
        return r


class Tokens:
    def token(self, scope):
        return scope


def client(api, writes=True):
    account = Account(name="JPlana", region="emea", allow_portal_writes=writes, portal_account="Partner_JPlana")
    return SchedulesClient(account, session=api, providers=[Tokens()], log=lambda m: None)


def test_clone_creates_empty_then_saves_the_whole_schedule():
    api = FakeApi([schedule()])
    c = client(api)
    saved, body, notes = c.copy_to(c.find("Jaume Test"), "Clone")
    methods = [m for m, *_ in api.calls if m != "GET"]
    assert methods == ["POST", "PUT"]
    post = next(j for m, _, j in api.calls if m == "POST")
    assert post["dayDefinitions"] == {} and post["customDays"] == [] and post["targets"] == ["Closed", "Open", "Holidays"]
    assert saved["name"] == "Clone" and not any("differs" in n for n in notes)
    assert differences(schedule(name="x"), saved) == ["special day 'Old one-off' only in the first"]


def test_existing_name_or_disabled_writes_stop_before_writing():
    api = FakeApi([schedule(), schedule(id="2", name="Clone")])
    with pytest.raises(SchedulesError, match="already exists"):
        client(api).copy_to(api.schedules[0], "clone")
    with pytest.raises(SchedulesError, match="Writes are disabled"):
        client(FakeApi([schedule()]), writes=False).copy_to(schedule(), "Other")
    assert not [m for m, *_ in api.calls if m != "GET"]


def test_failed_save_deletes_the_new_schedule():
    api = FakeApi([schedule()], fail_put=True)
    with pytest.raises(SchedulesError, match="HTTP 400"):
        client(api).copy_to(schedule(), "Clone")
    assert [m for m, *_ in api.calls if m != "GET"] == ["POST", "PUT", "DELETE"]
    assert [s["name"] for s in api.schedules] == ["Jaume Test"]


def test_empty_schedule_is_created_without_a_second_save():
    empty = schedule(dayDefinitions={}, dayOfWeek={}, customDays=[])
    api = FakeApi([empty])
    saved, _, _ = client(api).copy_to(empty, "Empty copy")
    assert [m for m, *_ in api.calls if m != "GET"] == ["POST"] and saved["name"] == "Empty copy"


def test_delete_by_exact_name_or_id_and_check_it_is_gone():
    api = FakeApi([schedule(), schedule(id="2", name="Jaume Test (clone)")])
    gone = client(api).delete("Jaume Test (clone)")
    assert gone["id"] == "2" and [s["name"] for s in api.schedules] == ["Jaume Test"]
    with pytest.raises(SchedulesError, match="found 0"):
        client(api).delete("jaume test (clone)")  # exact name only
    with pytest.raises(SchedulesError, match="Writes are disabled"):
        client(api, writes=False).delete("2907cb12")


def test_dry_run_writes_nothing():
    api = FakeApi([schedule()])
    saved, body, _ = client(api).copy_to(schedule(), "Clone", dry_run=True)
    assert saved is None and body["name"] == "Clone" and all(m == "GET" for m, *_ in api.calls)


def test_differences_are_readable():
    a, b = schedule(), schedule()
    b["dayDefinitions"]["_regularDayMonday"]["periods"].pop()
    b["customDays"][0]["date"] = "12-26"
    b["dayOfWeek"]["saturday"] = "Christmas"
    out = differences(a, b)
    assert "monday: 09:00-14:00 Open, 15:00-16:00 Open | 09:00-14:00 Open" in out
    assert "saturday: inactive | all day Holidays" in out
    assert "special day 'Christmas' date: 12-25 | 12-26" in out
    assert show_slots([("09:00:00.0000000", "18:00:00.0000000", "Open")]) == "09:00-18:00 Open"
