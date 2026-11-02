---
name: outbound-clid-summary
description: Summarise the Outbound CLID configuration of the Interaction Plans in a VCC account, read-only. Use when the user asks about outbound CLID, presented numbers or caller ID settings of an account's plans, e.g. "summary of the Outbound CLID of ifm-prod". The account is a name from accounts.toml.
argument-hint: <account>
---

# Outbound CLID summary

Read-only: this skill never changes anything in VCC. Run from the project root.

## 1. Get the facts

Check the account name with `.venv/Scripts/vccflow.exe accounts`. If the user didn't name one, or it isn't listed, ask. Then:

```
CB="$(mktemp)"; .venv/Scripts/vccflow.exe list-callback-numbers -a <account> > "$CB"; .venv/Scripts/vccflow.exe list-plans -a <account> --archived | .venv/Scripts/python.exe .claude/skills/outbound-clid-summary/scripts/clid_summary.py --callback-numbers "$CB"; rm -f "$CB"
```

Add `--archived` after `clid_summary.py` only if the user asks about archived plans.

`list-plans` uses the account's API credentials, or its portal login if it has none, and prints a note when it does. Say which one was used. `list-callback-numbers` always needs the portal login: there's no API for it. If it fails for a reason other than the login, run the summary without `--callback-numbers` and state plainly that the callback check wasn't done.

If the login has expired or is missing, tell the user and ask, naming the account: "The portal login for **<account>** has expired. Do you want to log in to **<account>** now?" On yes, run `.venv/Scripts/vccflow.exe login -a <account>` with a 320000 ms timeout, then repeat the command. On no, stop. If the account has no `portal_account`, tell the user: the command refuses to run, and `login` reports the name to add.

Use the script's numbers exactly. Don't recount or estimate.

## 2. Write the summary

Use this structure, in this order. Leave out a section only where the note says so.

**Opening line:** the account, that it's read-only, which login was used, and the active plan count in bold, with the archived count "not counted below".

**Settings table:**

| Outbound CLID setting | Active plans | Share |
|---|---|---|
| `DefaultAccountClid` | n | n% |

List every setting, most plans first, including `InteractionPlanClid` at 0 if no plan uses it. If none does, say in bold under the table that no plan sets its own outbound number.

**`InteractionPlanClid`: a number set on the plan (n).** A table with one row per number, most plans first:

| Country | CLID | Plans | Which ones |
|---|---|---|---|
| Germany | 4920149869200 | 18 | 2000 Specialities and Queues, German Customer Service, its backup, 15 GER Transfers |

Add a column "Callback number?" from the script's CALLBACK NUMBERS section: ✓ if it is one, **✗** if not. An `InteractionPlanClid` number must be a callback number for the setting to be correct, so state how many numbers and plans fail in a bold line under the table, or that all pass.

- Work out the country from the plan names and the number's country code. Where the names don't tell, leave the country as "Other".
- Group similar plans ("15 GER Transfers") and name the rest. The counts must add up to the setting's total.
- Plans whose CLID is their own number share one row: "Own number". Leave this section out if no plan uses the setting.

**`CustomerClid` (n).** A bullet list grouped by country, then functional groups (e.g. "CORE Function plans (9)") and "Test and other". Name the plans, shortened where a prefix repeats. Leave this section out if no plan uses the setting.

**`DefaultAccountClid` (n).** Say how many have no number set, and how many have a number stored, and whether those are the plan's own number. If there are 10 or fewer, list them in a table of plan and stored number. If more, describe them. Add that with this setting the stored number is probably ignored, but this hasn't been confirmed.

**Worth checking.** A numbered list, most important first, built from the script's WORTH CHECKING section and from what you see while grouping:

- InteractionPlanClid numbers that aren't callback numbers come first: these are misconfigurations, with the plans affected. To correct one, the number has to be added as a callback number, or the plan pointed at one that is.
- Likely typos: explain why, including the country code the wrong number points to.
- Countries or groups that differ from the rest, such as a few countries using `CustomerClid` where most use a fixed number. Say it may be deliberate.
- Backup, unused or test plans that are active, and when they matter: for instance before a Test to PROD copy.
- Plan names that don't match their number's country.

Each item should say what's odd and why it matters. Apart from the callback check, treat them as questions, not errors: this data can't prove a misconfiguration. Don't list anything just to fill the section, and say so if nothing stands out.

**Closing paragraph:** this shows only the plan-level setting. Applets that set a CLID themselves aren't included, and neither is the account's default number, which this data doesn't show. Offer one useful next step, such as listing the archived plans, exporting to a file, or comparing with the account's Test or PROD counterpart if there is one.

## Rules

- Report plan names and numbers as they are. Numbers are customer data: don't send them anywhere or save them in the repo.
- Comparing two accounts: run the script for each and report where they differ.
