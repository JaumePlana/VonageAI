"""Callback numbers through the Configuration API (callbackNumbers.v2.har). Numbers are made up."""

from unittest.mock import MagicMock

import pytest

from vccflow.auth import AuthError
from vccflow.callback_numbers import CallbackNumbersClient, CallbackNumbersError, parse_csv, write_csv
from vccflow.config import Account

BASE = "https://emea.api.newvoicemedia.com/ccadministration/CallbackNumbers"


def entry(i, name, clid, language="en-gb"):
    return {"id": i, "guid": f"g{i}", "accountId": 1, "withholdClid": False, "name": name, "clid": clid,
            "language": language, "record": "NoOverride"}


class Tokens:
    """A login that grants some scopes and refuses the others, like an API client or the portal."""

    def __init__(self, granted, name="api"):
        self.granted, self.name = granted, name

    def token(self, scope):
        if scope not in self.granted:
            raise AuthError(f"{self.name}: invalid_scope {scope}")
        return f"{self.name}-{scope}"


class FakeApi:
    """The ccadministration API in memory, with the rules seen live on 2026-11-20."""

    def __init__(self, entries):
        self.entries = entries
        self.calls = []

    def request(self, method, url, headers=None, data=None, json=None, timeout=None):
        self.calls.append((method, url, headers, data, json))
        resp = MagicMock(status_code=200)
        if method == "GET" and url == BASE:
            resp.json.return_value = {"callbackNumbers": [dict(e) for e in self.entries]}
        elif method == "GET" and url == f"{BASE}/csv":
            resp.text = write_csv([{"Clid": e["clid"], "Name": e["name"], "NewName": ""} for e in self.entries])
        elif method == "PATCH":
            e = next(e for e in self.entries if str(e["id"]) == url.rsplit("/", 1)[1])
            if any(x["name"] == json["name"] for x in self.entries):
                resp.status_code, resp.text = 400, '{"message":"Name already exists. Try again with a different name."}'
            else:
                e["name"] = json["name"]
        elif method == "PUT":
            rows = parse_csv(data.decode("utf-8"))
            if len(rows) != len(self.entries):
                resp.status_code, resp.text = 400, '{"message":"You have not specified the correct number of callback numbers."}'
                return resp
            n = 0
            for r in rows:
                if r["NewName"]:
                    e = next(e for e in self.entries if e["clid"] == r["Clid"] and e["name"] == r["Name"])
                    e["name"], n = r["NewName"], n + 1
            resp.json.return_value = {"message": f"Updated {n} of {len(rows)} callback numbers."}
        return resp


def client(entries=None, providers=None, writes=True):
    api = FakeApi(entries if entries is not None else [
        entry(1, "349000001", "+349000001", "es-ES"), entry(2, "349000001", "+349000001", "ca-ES"),
        entry(3, "comways OB English", "+449000002")])
    account = Account(name="JPlana", region="emea", allow_portal_writes=writes, portal_account="Partner_JPlana")
    providers = providers or [Tokens({"admin:read"}, "api"), Tokens({"admin:read", "admin:write"}, "portal")]
    return CallbackNumbersClient(account, session=api, providers=providers, log=lambda m: None), api


def test_list_keeps_the_api_fields_and_adds_number():
    c, api = client()
    first = c.list()[0]
    assert first["number"] == "+349000001" and first["language"] == "es-ES" and first["guid"] == "g1"
    method, url, headers, *_ = api.calls[0]
    assert (method, url) == ("GET", BASE)
    assert headers["Accept"] == "application/vnd.newvoicemedia.v1+json" and headers["Authorization"] == "Bearer api-admin:read"


def test_reads_fall_back_to_admin_write_then_to_the_portal_login():
    c, api = client(providers=[Tokens({"admin:write"}, "api")])  # like Viessmann-test's credentials
    c.list()
    assert api.calls[0][2]["Authorization"] == "Bearer api-admin:write"
    c, api = client(providers=[Tokens(set(), "api"), Tokens({"admin:read"}, "portal")])
    c.list()
    assert api.calls[0][2]["Authorization"] == "Bearer portal-admin:read"


def test_rename_one_entry_by_id_patches_its_name():
    c, api = client()
    saved = c.rename("3", "Sales line")
    assert saved["name"] == "Sales line"
    patch = next(x for x in api.calls if x[0] == "PATCH")
    assert patch[1] == f"{BASE}/3" and patch[4] == {"name": "Sales line"}
    assert patch[2]["Authorization"] == "Bearer portal-admin:write"  # the API credentials lack admin:write


def test_rename_refuses_ambiguous_numbers_and_unchanged_names():
    c, api = client()
    with pytest.raises(CallbackNumbersError, match="found 2.*ID 1.*ID 2"):
        c.rename("+349000001", "New")  # two entries, one per language
    with pytest.raises(CallbackNumbersError, match="already named"):
        c.rename("3", "comways OB English")
    assert not any(x[0] == "PATCH" for x in api.calls)


def test_rename_passes_on_the_portal_refusal():
    c, _ = client()
    with pytest.raises(CallbackNumbersError, match="Name already exists.*bulk CSV upload"):
        c.rename("3", "349000001")


def test_writes_need_allow_portal_writes():
    c, api = client(writes=False)
    with pytest.raises(CallbackNumbersError, match="Writes are disabled"):
        c.rename("3", "x")
    assert api.calls == []


def test_bulk_rename_sends_every_entry_and_checks_the_result():
    c, api = client()
    partial = write_csv([{"Clid": "+349000001", "Name": "349000001", "NewName": ""},
                         {"Clid": "+449000002", "Name": "comways OB English", "NewName": "349000001"}])
    message, done = c.rename_bulk(partial)  # the bulk upload accepts a name another entry has
    assert message == "Updated 1 of 3 callback numbers." and [d["entry"]["id"] for d in done] == [3]
    put = next(x for x in api.calls if x[0] == "PUT")
    assert put[1] == f"{BASE}/csv" and put[2]["Content-Type"] == "text/csv"
    assert len(parse_csv(put[3].decode("utf-8"))) == 3  # all entries, not only the rows given


def test_bulk_rows_with_a_shared_number_and_name_are_refused():
    c, _ = client()
    rows = parse_csv(write_csv([{"Clid": "+349000001", "Name": "349000001", "NewName": "Spain"}]))
    with pytest.raises(CallbackNumbersError, match="has 2 entries.*es-ES, ca-ES"):
        c.plan_bulk(rows)
    unknown = parse_csv(write_csv([{"Clid": "+11", "Name": "x", "NewName": "y"}]))
    with pytest.raises(CallbackNumbersError, match="export the CSV again"):
        c.plan_bulk(unknown)


def test_csv_format_matches_the_portal_export():
    text = write_csv([{"Clid": "+449000002", "Name": "comways OB English", "NewName": ""}])
    assert text == 'Clid,Name,NewName\r\n"+449000002","comways OB English",""\r\n'
    assert parse_csv("﻿" + text) == [{"Clid": "+449000002", "Name": "comways OB English", "NewName": ""}]
    with pytest.raises(CallbackNumbersError, match="header Clid,Name,NewName"):
        parse_csv("Number,Name\n1,2\n")


def test_expired_portal_login_is_reported_once():
    from vccflow.portal import SessionExpired

    class Expired:
        def token(self, scope):
            raise SessionExpired("The portal login for 'JPlana' has expired. Run: vccflow login -a JPlana")

    c, _ = client(providers=[Tokens(set(), "api"), Expired()])
    with pytest.raises(SessionExpired, match="has expired"):
        c.list()
