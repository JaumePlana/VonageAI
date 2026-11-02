"""Facts for an Outbound CLID summary, from `vccflow list-plans --archived` JSON on stdin (read-only).

Usage: vccflow list-plans -a <account> --archived | python clid_summary.py [--archived] [--callback-numbers FILE]

Prints plan counts, each setting's plans, and items worth checking. Only reads the JSON.
--archived also lists archived plans, which are otherwise only counted.
--callback-numbers takes the JSON of `vccflow list-callback-numbers`, and checks that every
InteractionPlanClid number is a callback number, as VCC requires for the setting to be correct.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

DEFAULT = "DefaultAccountClid"
BACKUP = re.compile(r"backup", re.I)
NOT_IN_USE = re.compile(r"not in use|unused|deprecated|\bold\b", re.I)
TEST = re.compile(r"\btest\b", re.I)


def close_numbers(a: str, b: str) -> bool:
    """One number is the other with a single digit added: a likely typo."""
    if len(a) > len(b):
        a, b = b, a
    return len(b) == len(a) + 1 and any(b[:i] + b[i + 1:] == a for i in range(len(b)))


def digits_in_name(name: str) -> str | None:
    m = re.match(r"\s*(\d{6,})", name)
    return m.group(1) if m else None


def checks(active: list[dict]) -> list[str]:
    found = []
    by_number = defaultdict(list)
    for p in active:
        if p.get("outboundClid") and p.get("outboundClidBehaviour") != DEFAULT:
            by_number[p["outboundClid"]].append(p["name"])
    numbers = sorted(by_number)
    for i, a in enumerate(numbers):
        for b in numbers[i + 1:]:
            if close_numbers(a, b):
                found.append(f"LIKELY TYPO: {a} ({', '.join(by_number[a])}) vs {b} ({', '.join(by_number[b])})")
    for p in active:
        name, number = p["name"], p.get("outboundClid")
        kept_by_name = digits_in_name(name)
        if number and p.get("outboundClidBehaviour") == DEFAULT and kept_by_name != number:
            found.append(f"STORED NUMBER NOT ITS OWN: '{name}' stores {number}, setting {DEFAULT}")
        if number and p.get("outboundClidBehaviour") != DEFAULT and kept_by_name and kept_by_name != number:
            found.append(f"NAME VS CLID: '{name}' presents {number}")
        if BACKUP.search(name):
            found.append(f"BACKUP STILL ACTIVE: '{name}' | {p.get('outboundClidBehaviour')} | {number}")
        if NOT_IN_USE.search(name):
            found.append(f"NAMED AS UNUSED BUT ACTIVE: '{name}' | {p.get('outboundClidBehaviour')} | {number}")
        if TEST.search(name) and p.get("outboundClidBehaviour") != DEFAULT:
            found.append(f"TEST PLAN WITH ITS OWN SETTING: '{name}' | {p.get('outboundClidBehaviour')} | {number}")
    return found


def digits(number: str | None) -> str:
    return re.sub(r"\D", "", number or "")


def callback_check(active: list[dict], path: str) -> None:
    entries = json.loads(Path(path).read_text(encoding="utf-8"))
    callback = defaultdict(list)  # a number can have several entries, e.g. one name per language
    for n in entries:
        callback[digits(n["number"])].append(n["name"])
    print(f"\nCALLBACK NUMBERS: {len(callback)} numbers in the account ({len(entries)} entries)")
    used = defaultdict(list)
    for p in active:
        if p.get("outboundClidBehaviour") == "InteractionPlanClid":
            used[digits(p.get("outboundClid"))].append(p["name"])
    for number, names in sorted(used.items(), key=lambda u: (u[0] in callback, -len(u[1]))):
        known = callback.get(number)
        state = "OK, callback number named " + ", ".join(f"'{x}'" for x in known) if known else "NOT A CALLBACK NUMBER"
        print(f"  {number or '(no number)'} | {len(names)} plans | {state}" + ("" if known else f": {'; '.join(names)}"))
    unused = sorted(n for n in callback if n not in used)
    print(f"  Callback numbers no plan uses as InteractionPlanClid: {len(unused)}")


def main() -> None:
    plans = json.load(sys.stdin)
    active = [p for p in plans if not p.get("archived")]
    archived = [p for p in plans if p.get("archived")]
    print(f"PLANS: {len(active)} active, {len(archived)} archived")

    counts = Counter(p.get("outboundClidBehaviour") for p in active)
    print("\nSETTINGS (active):")
    for setting, n in counts.most_common():
        print(f"  {setting}: {n} ({100 * n / len(active):.0f}%)")

    print("\nPLANS BY SETTING (active), except DefaultAccountClid with no number:")
    rows = defaultdict(list)
    for p in active:
        setting, number = p.get("outboundClidBehaviour"), p.get("outboundClid")
        if setting != DEFAULT or number:
            rows[(setting, number)].append(p["name"])
    for (setting, number), names in sorted(rows.items(), key=lambda r: (str(r[0][0]), -len(r[1]), str(r[0][1]))):
        print(f"  {setting} | {number} | {len(names)} plans: {'; '.join(sorted(names, key=str.lower))}")

    default = [p for p in active if p.get("outboundClidBehaviour") == DEFAULT]
    with_number = [p for p in default if p.get("outboundClid")]
    own = [p for p in with_number if digits_in_name(p["name"]) == p["outboundClid"]]
    print(f"\nDEFAULT DETAIL: {len(default)} plans; {len(default) - len(with_number)} with no number; "
          f"{len(with_number)} with a number stored, {len(own)} of them the plan's own number")

    print("\nWORTH CHECKING:")
    for item in checks(active) or ["(none flagged)"]:
        print(f"  {item}")

    if "--callback-numbers" in sys.argv:
        callback_check(active, sys.argv[sys.argv.index("--callback-numbers") + 1])

    if "--archived" in sys.argv:
        print("\nARCHIVED:")
        for p in sorted(archived, key=lambda p: p["name"].lower()):
            print(f"  {p['name']} | {p.get('outboundClidBehaviour')} | {p.get('outboundClid')}")


if __name__ == "__main__":
    main()
