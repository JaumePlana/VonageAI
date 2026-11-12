---
name: data-source-usage
description: Find where data sources are initialized (set) and consumed (read) across all the Interaction Plans of a VCC account, read-only. Use when the user asks which applets or plans reference, set, use or read a data source, whether a data source is used at all, or for unused data sources, e.g. "which applets use the data source 'SF Error Response' in Viessmann-test". Also use it to rescan an account after the user changed its plans and show what changed since the previous scan, e.g. "I changed PROD, rescan and compare". The account is a name from accounts.toml.
argument-hint: <account> "<data source>" ["<data source>" ...]
---

# Data source usage

Read-only: this skill never changes anything in VCC. Run commands from the project root.

The VCC API has no applet data, so the skill reads every applet's editor page through the portal login, then analyses that offline. Three scripts in `scripts/`:

- `scan_applets.py <account> [--fresh]` saves every applet of every plan in `exports/<account>/applet-scan/` (one `<plan name>.json` per plan, plus `manifest.json`; plans whose names clash get ` [<plan ID>]` appended, and renamed plans are followed through the ID stored in each file). No audio is downloaded. It resumes where it stopped. `--fresh` starts over and keeps a complete previous scan in `exports/<account>/applet-scan-previous` (replacing the one there); an incomplete scan is discarded, never kept as the previous one. The editor pages' catalogs of possible values (data source lists, next-applet pickers, Salesforce object and field lists, form definitions) are left out: they are the same in every applet and made up ~99% of a scan. The account's data source list is saved once, as `dataSources` in `manifest.json`. `--compact` shrinks scans saved before this, offline.
- `ds_usage.py <account> "Name" ...` reports, for each data source, the applets that SET it, READ it, or mention it in a field it doesn't classify (OTHER). Names aren't case-sensitive, like in VCC. `--like "text"` matches every data source whose name contains the text. `--all` gives one line per data source with set and read counts, then every name with spacing problems (leading, trailing or double spaces, in the plans or only in the account's list), then the data sources that exist in the account but that no applet sets or reads.
- `compare_scans.py <account>` shows what changed between `applet-scan-previous` and `applet-scan`: plans and applets added, removed or renamed, and each changed field with its old and new value, plus the data sources added to or removed from the account. Lists of simple entries (Set Data Source keys, response mappings) are compared as sets, long texts (web service definitions) line by line. The data source and next-applet dropdowns are ignored, and secret values are never shown. `--old DIR --new DIR` compares other scan folders.

## 1. Check the account and the scan

Check the account name with `.venv/Scripts/vccflow.exe accounts`. If the user didn't name one, or it isn't listed, ask. If the user didn't name a data source, ask which one, unless they want the whole account (`--all`).

Then read `exports/<account>/applet-scan/manifest.json`, if it exists:
- `finished` is today: use the scan, and say when it was made.
- `finished` is missing: the last scan stopped. Run `scan_applets.py <account>` without `--fresh`, which resumes it.
- Older than today: ask, giving the date: "The last scan of **<account>** is from <date>. Use it, or scan again (about 10 s per plan)?" A new scan uses `--fresh`.
- The user says they changed the account's plans since the scan: scan again with `--fresh`, whatever the date. Then compare the scans (step 5) unless the user doesn't want it.
- No manifest: scan.

Before scanning, check the login with `.venv/Scripts/vccflow.exe session -a <account>`. If it has expired or is missing, ask, naming the account: "The portal login for **<account>** has expired. Do you want to log in to **<account>** now?" On yes, run `.venv/Scripts/vccflow.exe login -a <account>` with a 320000 ms timeout. On no, stop.

## 2. Scan (only if needed)

```
.venv/Scripts/python.exe .claude/skills/data-source-usage/scripts/scan_applets.py <account> [--fresh] > "<scratchpad>/scan-<account>.log" 2>&1
```

Run it in the background: a large account takes 15 minutes or more. Tell the user how many plans it found (first log line) and roughly how long it will take, then wait for the `DONE` line. If the log ends with `LOGIN EXPIRED`, ask the login question above, then run the same command again without `--fresh`: it resumes.

## 3. Analyse

```
.venv/Scripts/python.exe .claude/skills/data-source-usage/scripts/ds_usage.py <account> "Name 1" "Name 2"
```

- Check the `SCAN` and `APPLETS` header lines: the scan must be complete. List any `UNREADABLE` applets in the report: they weren't searched.
- `NOT FOUND` for a name: run again with `--like` and a distinctive part of the name, to find other spellings, and tell the user which names exist. Do the same when the names suggest variants (e.g. "SF …" and "Salesforce …"). Pass names exactly as the user gave them, leading and trailing spaces included: a leading space usually means the user is after a name that starts with one, so `--like " failure"` finds `" Failure Case Lookup"`.
- Names are printed in quotes and kept exactly as stored, so `" X"`, `"X"` and `"X  Y"` are reported apart. VCC treats a name with leading spaces, trailing spaces or two or more spaces between words as a different data source (confirmed by the user), so `>> SPACES: …` and `>> SAME NAME BUT FOR SPACES` are findings: always report them, quoting the names so the spaces show, and say which problem it is (leading, trailing, double spaces between words). A name set with one spelling and read with another never matches: report it as a bug. A requested name also matches spellings that differ from it only by spaces, and the report flags them.
- Next-applet fields are already left out, so an applet named like a data source doesn't produce OTHER rows.
- `NOT FOUND` also says whether the name exists in the account's data source list (unused by the plans) or not at all (probably misspelt). Without `dataSources` in the manifest it can't tell: run `scan_applets.py <account> --compact` once.
- Use the script's rows and counts exactly. Don't recount or estimate.

**OTHER rows** need a judgement from the field name. Known fields:

| Applet | Field | Meaning |
|---|---|---|
| Skill Tagger | `AgentIdDataSource` | reads the agent ID from it (consumed) |
| Skill Tagger | `LoggedInTimeoutDataSource`, `LoggedOutTimeoutDataSource` | probably reads the timeout from it (consumed) |
| Bot Connector | `AgentForce_*DataSourceName` | probably the bot writes into it (initialized) |

For any other field, say what the field name suggests and that it isn't confirmed. Never treat an OTHER row as proof either way.

**Other data sources the setters copy.** When a SET value is `$(another)`, the value comes from that data source. Say so, and offer to trace that one too. Don't trace it unasked.

## 4. Write the report

**Opening line:** the account, read-only, when the scan was made, and the plans and applets searched. Mention any unreadable applets.

**One section per data source**, headed with its name:

- **Initialized:** a table with Plan | Applet (type) | Value or how it's set | Reached from | Next applet. Use the script's `from` and `next` lines. When `from` is long, give the first few and the count.
- **Consumed:** a table with Plan | Applet (type) | How, e.g. "Data Router: equals 'True' → Transfer". If there are none, say in bold that no plan reads it.
- **Other:** the OTHER rows with your reading, if any.

When several data sources follow the same pattern (e.g. "Set Failure …" applets that copy `$(error)`), explain the pattern once, before the tables.

**Observations:** only what the data shows, most important first:
- Set but never read, or read but never set. For read-but-never-set, it may be a system data source (CallGuid, DialledNumber…), set by a Bot Connector, passed in by a plan transfer, or missing.
- Names with leading spaces, trailing spaces or two or more spaces between words, and the same name under two spellings that differ only by spaces: where each is set and read.
- The same purpose under different names, and which plans use which.
- An applet that behaves differently from the others of the same pattern, e.g. a different next applet.

**Closing:** the scan covers Interaction Plan applets only. Reads outside them aren't visible: agent desktop (ContactPad, Salesforce screen pop), reporting, interaction data exports. Offer one useful next step, such as tracing a data source a setter copies, or running the same check on the account's Test or PROD counterpart.

## 5. Compare with the previous scan

Run it when the user asks what changed, or after a `--fresh` scan the user asked for because they changed the plans:

```
.venv/Scripts/python.exe .claude/skills/data-source-usage/scripts/compare_scans.py <account>
```

Check the `OLD` and `NEW` header lines: both scans must be complete. If there is no previous scan, say so: the first `--fresh` scan after this change creates it.

Report the changes in a table: Plan | Applet | Change, grouped when the same change was made in several plans (e.g. "`Salesforce Error Response` → `SF Error Response`" in seven Set Failure applets). Say plainly what each change means, e.g. "the failure exit, empty before, now goes to …". Use the script's lines exactly; don't add changes it doesn't show. Then run the data source analysis the user asked for on the new scan, and say which findings of earlier reports the changes resolved.

## Rules

- Never print credentials. The scan holds Data Connector settings with secrets; the script hides excerpts from secret fields. Don't open or quote scan files beyond what the script prints.
- `exports/` is git-ignored: the scans (`applet-scan`, `applet-scan-previous`) stay there, private. Don't copy them elsewhere.
- Report plan, applet and data source names as they are.
- Comparing two accounts: run the analysis for each and report where they differ.
