"""Callback numbers through the Configuration page's API (callbackNumbers.v2.har, 2026-11-20).

VCC Admin moved callback numbers from "Configuration (legacy)" (system-settings.aspx, a whole
page posted as a form) to "Configuration", which calls a JSON API on the API host:

  GET   /ccadministration/CallbackNumbers           {"callbackNumbers": [{id, guid, accountId, name,
                                                     clid, language, record, withholdClid}]}
  PATCH /ccadministration/CallbackNumbers/<id>      {"name": "..."}  (renames one entry)
  GET   /ccadministration/CallbackNumbers/csv       "Clid,Name,NewName" lines, NewName empty
  PUT   /ccadministration/CallbackNumbers/csv       the same CSV with NewName filled (text/csv), with
                                                     every entry ("You have not specified the correct
                                                     number of callback numbers" otherwise):
                                                     {"message": "Updated 1 of 5 callback numbers."}

A number can have several entries (one per language). PATCH refuses a name that an entry
already has, even the entry's own ("Name already exists. Try again with a different name."),
while the CSV upload accepts it.

All with Accept: application/vnd.newvoicemedia.v1+json and a bearer token. Reads accept
admin:read; writes need admin:write. API credentials often have only one of the two, so each
call tries the account's API credentials first and then the portal login, which offers both.
"""

from __future__ import annotations

import csv
import io
import re

from typing import Any

from vccflow.config import Account
from vccflow.scoped_api import ApiError, ScopedApi

PATH = "ccadministration/CallbackNumbers"
READ_SCOPES = ("admin:read", "admin:write")
WRITE_SCOPES = ("admin:write",)
CSV_HEADER = ["Clid", "Name", "NewName"]
MAX_NAME = 50  # the legacy page's limit; kept as a guard


class CallbackNumbersError(ApiError):
    pass


def _message(e: Exception) -> str:
    m = re.search(r'"message"\s*:\s*"([^"]*)"', str(e))
    return m.group(1) if m else str(e)


def digits(number: Any) -> str:
    return re.sub(r"\D", "", str(number or ""))


def matching(entries: list[dict], which: str) -> list[dict]:
    """Entries whose ID, current name or number (digits only) is `which`."""
    return [e for e in entries if which in (str(e["id"]), e["name"]) or (digits(which) and digits(which) == digits(e["clid"]))]


def parse_csv(text: str) -> list[dict]:
    """Rows of the bulk CSV as {Clid, Name, NewName}; refuses another header."""
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    if not rows or [c.strip() for c in rows[0]] != CSV_HEADER:
        raise CallbackNumbersError(f"The CSV must start with the header {','.join(CSV_HEADER)} "
                                   "(as written by export-callback-numbers)")
    out = []
    for i, r in enumerate(rows[1:], start=2):
        if not any(c.strip() for c in r):
            continue
        if len(r) != 3:
            raise CallbackNumbersError(f"CSV line {i}: expected 3 columns, found {len(r)}")
        out.append(dict(zip(CSV_HEADER, r)))
    return out


def write_csv(rows: list[dict]) -> str:
    """The bulk CSV as the portal writes it: every value quoted, CRLF line ends."""
    buf = io.StringIO()
    w = csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    buf.write(",".join(CSV_HEADER) + "\r\n")
    for r in rows:
        w.writerow([r["Clid"], r["Name"], r.get("NewName") or ""])
    return buf.getvalue()


class CallbackNumbersClient(ScopedApi):
    """Callback numbers of one account. Tries each (login, scope) in order until one is allowed."""

    error = CallbackNumbersError
    what = "callback numbers"

    def __init__(self, account: Account, *args, **kwargs):
        super().__init__(account, *args, **kwargs)
        self.base = f"{account.manager_api_url}/{PATH}"

    # --- reads ---

    def list(self) -> list[dict]:
        """The account's callback numbers, as the API returns them (id, name, clid, language, ...)."""
        body = self._call("GET", self.base, READ_SCOPES).json()
        return body.get("callbackNumbers", [])

    def export_csv(self) -> str:
        """The bulk CSV (Clid,Name,NewName with NewName empty), as the portal's export gives it."""
        return self._call("GET", f"{self.base}/csv", READ_SCOPES).text

    # --- writes ---

    def rename(self, which: str, new_name: str) -> dict:
        """Rename one entry, given its number, ID or current name. Return it as read back."""
        self._require_writes()
        if not new_name or len(new_name) > MAX_NAME:
            raise CallbackNumbersError(f"A callback number's name must be 1 to {MAX_NAME} characters")
        found = matching(self.list(), which)
        if len(found) != 1:
            detail = "" if not found else ": " + ", ".join(f"ID {e['id']} ({e['name']}, {e.get('language')})" for e in found)
            raise CallbackNumbersError(f"Expected one callback number matching '{which}', found {len(found)}{detail}. "
                                       "Give its ID to choose one.")
        entry = found[0]
        if entry["name"] == new_name:
            raise CallbackNumbersError(f"{entry['clid']} ({entry.get('language')}) is already named '{new_name}'")
        try:
            self._call("PATCH", f"{self.base}/{entry['id']}", WRITE_SCOPES, json={"name": new_name})
        except CallbackNumbersError as e:
            # e.g. 400 "Name already exists. Try again with a different name.": a single rename
            # refuses a name another entry has; the bulk CSV upload accepts it (callbackNumbers.v2.har).
            if e.status == 400:
                raise CallbackNumbersError(f"VCC refused renaming {entry['clid']} ({entry.get('language')}) to "
                                           f"'{new_name}': {_message(e)} The bulk CSV upload (rename-callback-numbers) "
                                           "accepts a name another entry has.", 400) from e
            raise
        saved = next((e for e in self.list() if e["id"] == entry["id"]), None)
        if saved is None or saved["name"] != new_name:
            raise CallbackNumbersError(f"{entry['clid']} is still named '{(saved or entry)['name']}' after the rename")
        return saved

    def plan_bulk(self, rows: list[dict]) -> list[dict]:
        """The renames a CSV asks for, each with the entry it changes ("entry").

        A number can have several entries (one per language), so a row is identified by its
        number and current name, as the portal's upload does (callbackNumbers.v2.har).
        Refuses rows that match no entry or several, and names that are too long.
        """
        entries = self.list()
        changes = []
        for r in rows:
            new = r["NewName"].strip()
            if not new or new == r["Name"]:
                continue
            found = [e for e in entries if digits(e["clid"]) == digits(r["Clid"]) and e["name"] == r["Name"]]
            if not found:
                raise CallbackNumbersError(f"{r['Clid']} named '{r['Name']}' is not a callback number of "
                                           f"'{self.account.name}' (export the CSV again: names may have changed)")
            if len(found) > 1:
                raise CallbackNumbersError(f"{r['Clid']} named '{r['Name']}' has {len(found)} entries "
                                           f"(languages {', '.join(str(e.get('language')) for e in found)}): rename "
                                           "them one by one with rename-callback-number and the entry's ID")
            if len(new) > MAX_NAME:
                raise CallbackNumbersError(f"{r['Clid']}: the new name is longer than {MAX_NAME} characters")
            changes.append({**r, "NewName": new, "entry": found[0]})
        return changes

    def rename_bulk(self, text: str) -> tuple[str, list[dict]]:
        """Send a bulk CSV, as the portal's upload does. Return (the portal's message, changes made)."""
        self._require_writes()
        changes = self.plan_bulk(parse_csv(text))
        # VCC wants every entry in the upload ("You have not specified the correct number of callback
        # numbers"), so send a fresh export with NewName set on the rows to change.
        full = parse_csv(self.export_csv())
        for c in changes:
            row = next(r for r in full if digits(r["Clid"]) == digits(c["Clid"]) and r["Name"] == c["Name"])
            row["NewName"] = c["NewName"]
        message = self._call("PUT", f"{self.base}/csv", WRITE_SCOPES, data=write_csv(full).encode("utf-8"),
                             headers={"Content-Type": "text/csv"}).json().get("message", "")
        after = {e["id"]: e["name"] for e in self.list()}
        failed = [c for c in changes if after.get(c["entry"]["id"]) != c["NewName"]]
        if failed:
            raise CallbackNumbersError(f"The portal answered '{message}', but these names did not change: "
                                       + ", ".join(f"{c['Clid']} '{c['Name']}' -> '{c['NewName']}'" for c in failed))
        return message, changes
