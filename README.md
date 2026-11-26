# vccflow

Automation for Vonage Contact Center (VCC) interaction plans. Background and goals are in [CONTEXT.md](CONTEXT.md).

Roadmap:
0. **Explore** (current): authenticate, list plans/mappings, export a plan to JSON. Read-only.
1. POC: create a plan with a single Announcer applet.
2. Copy plans from one account to another (Test → PROD), remapping account-specific IDs.
3. Translate DrawIO/Visio flows into interaction plans.

## Setup

Requires Python 3.11+.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
copy accounts.example.toml accounts.toml
copy .env.example .env
```

Edit `accounts.toml` (one section per customer/environment, with its region) and put each account's client ID and secret in `.env`. Create the API credentials in VCC Admin with the `interaction-plans:read` scope. `.env`, `accounts.toml` and `exports/` are git-ignored.

## Usage

```powershell
vccflow accounts
vccflow list-plans    -a acme-test               # Interaction Manager API; --archived, --source ipm|portal
vccflow list-mappings -a acme-test               # address -> plan, with rules and interaction properties
vccflow list-destinations -a acme-test           # addresses per channel (Media Channels API, scope admin:read); --channel pstn
vccflow get           -a acme-test <any/ipm/path> # raw GET for exploring
vccflow har           exports/HAR/architect.har   # offline: sanitize a browser recording (keep HARs in exports/HAR/)
```

When the API credentials lack a scope or the account has none, the list commands use the token of your portal login instead (`vccflow login`), the way VCC Admin's pages do. It acts as you, is used only for reads, and is kept in memory only.

### Portal (applets)

Applets are not in the public API; these commands use the Architect's own backend through your portal login session, saved under `.sessions/` (git-ignored, contains cookies).

```powershell
vccflow list-callback-numbers   -a JPlana          # API (admin:read): API credentials, else the portal login
vccflow rename-callback-number  -a JPlana 2275226 "New name"   # one entry, by ID, current name or number (admin:write)
vccflow export-callback-numbers -a JPlana --out cb.csv          # Clid,Name,NewName: fill in NewName
vccflow rename-callback-numbers -a JPlana cb.csv --dry-run      # bulk rename; rows only for what changes are enough
vccflow list-custom-configurations -a JPlana [--like RTT]   # Custom Configuration names (alias list-custom-configs), no values
vccflow list-skills       -a JPlana                             # skills, name and ID (User Admin API, users:read)
vccflow list-schedules    -a JPlana --summary                   # schedules (API, admin:read); without --summary: full JSON
vccflow copy-schedule "Jaume Test" --from JPlana --to Demo29 [--name "New name"] [--dry-run]
vccflow copy-schedule "Jaume Test" --from JPlana --name "Jaume Test 2"   # clone in the same account
vccflow compare-schedules "Jaume Test" --from JPlana --to Demo29 [--other "Other name"]
vccflow delete-schedule   -a JPlana "Jaume Test 2" [--dry-run]   # exact name or ID; doesn't know which plans use it
# Schedule names are case-sensitive ("jaume test" and "Jaume Test" can coexist); a schedule needs at least a name and a time zone.
vccflow login         -a JPlana                    # opens Chrome (--browser msedge for Edge); sign in, the window closes by itself
vccflow applets       -a JPlana "zz Claude POC"    # plan name or ID
vccflow applet        -a JPlana "zz Claude POC" "ANN Test"
vccflow poc-announcer -a JPlana "zz Claude POC"    # create, verify, delete (add --keep to keep it)
vccflow export-plan   -a JPlana "DISH Web Services" # -> exports/JPlana/plans/DISH Web Services/plan.json + media/
vccflow initial-applet -a JPlana "zz Claude POC"   # show; add --set "<applet>" to change it
vccflow import-plan   -a JPlana "exports/JPlana/plans/DISH Web Services" "zz Claude POC" --dry-run
vccflow copy-plan     --from JPlana "DISH Web Services" "zz Claude POC" --dry-run   # both steps; --to <account> for another account
vccflow compare-plans --from JPlana --to Demo29 "DISH Web Services" "POC Test"   # read-only: applets, paths, settings, skills, audio, IDs, data sources
vccflow compare-exports "exports/old" "exports/new"   # offline: two saved exports (use export-plan --out to keep a copy)
vccflow session       -a JPlana                    # is the portal login still valid?
```

`import-plan` copies an export into an existing plan of `--account`: it checks everything first (`--dry-run` stops there), creates all applets, saves them with connections by name, uploads audio, sets the initial applet, then exports the target again and compares. On a failed write it deletes the applets it created. Announcer, Set Data Source, Data Connector, Menu, Data Router, Skill Tagger, Route to Interaction Plan, ACD (queue), Schedule Router, Queue - Set Priority, Dial In Config, Collect Input and Call Connect Router applets are supported. A Call Connect Router keeps its delivery numbers in order (each a data source, $(name), or an international number, +<country code><digits>; anything else is refused), distribution type, timeouts, "try agents more than once", whisper option, its two applets (when the number completes, when it does not connect) by name and its three audio files; removed audio is copied as removed. Data sources in the delivery numbers count in the data source check. VCC derives its next applet and TryAgentsMoreThanOnce from the editor's fields, so they are not copied. A Collect Input keeps its input prompt, maximum digits, destination data source, maximum input prompts ("Maximum" or 1-10), confirmation prompt with its playback and confirmation texts, its next applets by name and its three audio files (downloaded, compared by content). Its destination is a data source the applet sets (VCC creates one with a new name); storing the digits in an IVR slot is refused (IVR slots). VCC refuses 1-10 input prompts without the next applet after them, and so does vccflow, before writing. Audio cannot be removed (VCC keeps the file when none is sent); playback and confirmation audio of an applet whose confirmation prompt is off is uploaded with the prompt on, then a second save turns it off, as VCC keeps that audio. A Dial In Config keeps, for each configuration 0-9, its next applet by name, its alternative text and its announcement (downloaded, compared by content), and the selected configuration; selecting an empty configuration is warned about on import and reported by `compare-plans`. An announcement cannot be removed from a configuration (VCC keeps the file when none is sent). A Queue - Set Priority keeps its priority (Lowest, Lower, Normal, Higher, Highest; `compare-plans` shows these words) and its optional next applet by name. A Schedule Router is copied with its schedule by name, its default and failure applets, and one routing per service status. The export keeps the definition of each schedule used (Schedules API, admin:read). In the target account: a schedule with the same name and the same service statuses is used; with more statuses it is used with a warning (a routing may be needed for them); without some of the source's statuses the schedule is cloned as `zz vccflow Aux <name>` (reused next time), with a warning; with no schedule of that name it is cloned with its name, with a warning. Cloned schedules are reported as NEW SCHEDULE and deleted again if the import fails. A Schedule Router without a schedule or default applet is created and not saved (VCC refuses it). An ACD is copied with all its settings, its connections by applet name, its nine audio files (downloaded and uploaded again; compared by content) and its data sources. Its groups are matched in the target account: same ID and name; else the same name (warning: the ID differs); else the same ID (warning: the name differs); else the group is created with the source's ID, name and maximum unexpected and wrap-up times through the User Admin API (`users:write`; the export keeps the definitions of the groups the ACDs use, read with `users:read`; without them, VCC Admin's default of 500 seconds is used, with a warning) and reported as a NEW GROUP, and deleted again if the import fails. Without User Admin API access a missing group stops the import before any write. Presence states are matched by name; a missing one is left as "Leave unchanged" with a warning. Salesforce Omni-Channel routing (algorithm 3, Salesforce transfer, flow settings) is refused: not recorded. A Route to Interaction Plan is copied with its Interaction Plan and applet by name, or its data source (`$(Name)`), and its failure applet. The plan is looked up by name in the target account, then the applet in it; VCC never offers the plan being edited. If the plan or applet is missing there, the applet is created without settings and a warning says so: VCC does not save one with only its failure applet. The "Transfer IVR data / DDI / Agent Id" options are refused (IVR slots). Salesforce Data Connectors can get, update and create records; field values come from data sources (IVR Slots are refused). A Skill Tagger is copied with its static skills (by name), dynamic skills (data sources), necessity (optional, mandatory, mandatory for a time), "Remove all existing skills", personal queue data sources and next applet. Static skills are matched by name without letter case, and the target's spelling is used (VCC refuses "de" when "DE" exists). A skill the target account lacks is created there through the User Admin API (`users:write`) and reported as a NEW SKILL; if the import fails, the skills it created are deleted again. Without User Admin API access, a missing skill stops the import before any write. Data sources of dynamic skills and the personal queue count in the data source check, so missing ones go into the auxiliary Set Data Source as for Data Routers. Data Router rules are kept in order (VCC routes on the first match); every rule data type (text, number, decimal, True/False, date) and operator is supported. A Data Router without rules is created empty and not saved, with a warning: VCC only saves one whose first rule has a data source. `--skip-unsupported` leaves out applets that cannot be imported yet (e.g. ACD) and lists them; connections to them stay unset. A Data Router rule or default whose applet is missing (left out, or already empty in the source) is copied with an empty destination, with a warning naming them. Before writing, it also checks the data sources: those the plan reads but does not set itself (Set Data Source keys and Data Connector outputs count as set) should exist in the target account; the portal saves applets without checking. Missing ones are reported as warnings and the import goes ahead: vccflow first sets them in an auxiliary Set Data Source, `zz vccflow Aux Data Sources`, which stays in the target plan with nothing leading to it. VCC lists a data source only while some applet sets it, and refuses to save a Data Router rule whose data source is not listed; deleting that applet removes the data sources again. It is created only when a data source is missing; a later copy into the same plan adds its keys to the same applet. An auxiliary applet in the source plan is not copied: only its keys that the target account lacks are added. `compare-plans` reports the applet as a note. `$(JavascriptEngineUrl)` is a Vonage keyword (JavaScript Data Connector), not a data source. `--require-data-sources` stops the import before any write instead. Names are not case-sensitive (`LOG` and `Log` are the same data source). `copy-plan` runs the export and the import in one go; the export folder stays under `exports/` for reference.

`compare-plans` and `compare-exports` start with a summary line, then list differences, problems and notes in groups: plan-level lines first, then applets in one plan only, then one block per applet (its name and type, then its lines). Applets compared only through their editor page fields are listed together in the notes, by applet type. Skill Tagger skills are compared one by one by name, e.g. `skill 'External': source mandatory for 15 seconds | target mandatory for 8 hours` or `skill 'Phone' is only in the target (mandatory)`; a name differing only in letter case is a note.

Custom Configurations are account settings that only Vonage creates and sets, on request. Plans read them as `#(Name)`, e.g. in a Data Connector definition. VCC shows their names (not their values) only on the Data Connector editor page, so `list-custom-configurations` reads them from the first Data Connector it finds (`--plan` first) and fails if the account has none.

Plan-level settings (reporting group, Outbound CLID) are exported and compared, but not imported: a copy leaves them as they are on the target plan. A different CLID setting is a difference; a different number between two accounts is a note, because numbers often differ per account by design.

The export drops IDs that belong to one account, keeps connections by applet name, records the initial applet, and downloads uploaded audio files. Applet types that `import-plan` does not write yet keep every account-specific reference by name, so that plans in two accounts compare by what they point to: applets referenced by ID or GUID become connections (`DefaultApplet`, `UnsuccessfulCallApplet`, `NextAppletOnMaximumReached`, a Schedule Router's `ScheduleMappings.<status>`), GUIDs the editor page labels become the label (a Schedule Router's schedule name), and an uploaded audio file's storage path is dropped (the audio is compared by name and content). Their editor page fields keep a dropdown option whose value is an ID by its text (the plan a Route To Interaction Plan goes to, an ACD's presence states). Exports saved before 2026-11-23 still hold those IDs: compare them only with exports made the same way. Data Connector definitions are exported as-is and can contain credentials.

`portal_account` is mandatory in `accounts.toml`: it is the VCC account's name, as `vccflow login` reports it. Without it, commands are refused and any saved login for that account is removed. A login signed in to any other account is removed too, at login and before the first portal call of every command. `vccflow login` still works without it, to find out the name: it tells you which account you signed in to and the line to add, without keeping the login. vccflow does not log out of VCC, because that also ends your other VCC sessions: a removed login stays valid on VCC until it expires, but vccflow can no longer use it. Writes also need `allow_portal_writes = true`. Commands also check that the portal session and the API see the same plan.

`har` writes `architect.clean.har` (cookies, auth headers and tokens removed; scripts, styles and images dropped) and an `architect.bodies/` folder with every request/response body, and prints the list of calls, with writes marked `*`. Only share the `.clean.har`, and only after checking it.

## Tests

```powershell
pytest
```
