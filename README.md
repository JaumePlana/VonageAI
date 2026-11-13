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
vccflow list-callback-numbers  -a JPlana           # from the settings page (no API for it)
vccflow rename-callback-number -a JPlana "+442030023771" "New name"   # checks that no other setting moved
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

`import-plan` copies an export into an existing plan of `--account`: it checks everything first (`--dry-run` stops there), creates all applets, saves them with connections by name, uploads audio, sets the initial applet, then exports the target again and compares. On a failed write it deletes the applets it created. Announcer, Set Data Source, Data Connector, Menu and Data Router applets are supported. Data Router rules are kept in order (VCC routes on the first match); every rule data type (text, number, decimal, True/False, date) and operator is supported. A Data Router without rules is created empty and not saved, with a warning: VCC only saves one whose first rule has a data source. `--skip-unsupported` leaves out applets that cannot be imported yet (e.g. ACD) and lists them; connections to them stay unset. A Data Router rule or default whose applet is missing (left out, or already empty in the source) is copied with an empty destination, with a warning naming them. Before writing, it also checks the data sources: those the plan reads but does not set itself (Set Data Source keys and Data Connector outputs count as set) should exist in the target account; the portal saves applets without checking. Missing ones are reported as warnings and the import goes ahead: vccflow first sets them in an auxiliary Set Data Source, `zz vccflow Aux Data Sources`, which stays in the target plan with nothing leading to it. VCC lists a data source only while some applet sets it, and refuses to save a Data Router rule whose data source is not listed; deleting that applet removes the data sources again. It is created only when a data source is missing; a later copy into the same plan adds its keys to the same applet. An auxiliary applet in the source plan is not copied: only its keys that the target account lacks are added. `compare-plans` reports the applet as a note. `$(JavascriptEngineUrl)` is a Vonage keyword (JavaScript Data Connector), not a data source. `--require-data-sources` stops the import before any write instead. Names are not case-sensitive (`LOG` and `Log` are the same data source). `copy-plan` runs the export and the import in one go; the export folder stays under `exports/` for reference.

Plan-level settings (reporting group, Outbound CLID) are exported and compared, but not imported: a copy leaves them as they are on the target plan. A different CLID setting is a difference; a different number between two accounts is a note, because numbers often differ per account by design.

The export drops IDs that belong to one account, keeps connections by applet name, records the initial applet, and downloads uploaded audio files. Data Connector definitions are exported as-is and can contain credentials.

`portal_account` is mandatory in `accounts.toml`: it is the VCC account's name, as `vccflow login` reports it. Without it, commands are refused and any saved login for that account is removed. A login signed in to any other account is removed too, at login and before the first portal call of every command. `vccflow login` still works without it, to find out the name: it tells you which account you signed in to and the line to add, without keeping the login. vccflow does not log out of VCC, because that also ends your other VCC sessions: a removed login stays valid on VCC until it expires, but vccflow can no longer use it. Writes also need `allow_portal_writes = true`. Commands also check that the portal session and the API see the same plan.

`har` writes `architect.clean.har` (cookies, auth headers and tokens removed; scripts, styles and images dropped) and an `architect.bodies/` folder with every request/response body, and prints the list of calls, with writes marked `*`. Only share the `.clean.har`, and only after checking it.

## Tests

```powershell
pytest
```
