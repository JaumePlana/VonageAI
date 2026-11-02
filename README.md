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
vccflow login         -a JPlana                    # opens Chrome (--browser msedge for Edge); sign in, the window closes by itself
vccflow applets       -a JPlana "zz Claude POC"    # plan name or ID
vccflow applet        -a JPlana "zz Claude POC" "ANN Test"
vccflow poc-announcer -a JPlana "zz Claude POC"    # create, verify, delete (add --keep to keep it)
vccflow export-plan   -a JPlana "DISH Web Services" # -> exports/JPlana/plans/DISH Web Services/plan.json + media/
vccflow initial-applet -a JPlana "zz Claude POC"   # show; add --set "<applet>" to change it
vccflow import-plan   -a JPlana "exports/JPlana/plans/DISH Web Services" "zz Claude POC" --dry-run
vccflow copy-plan     --from JPlana "DISH Web Services" "zz Claude POC" --dry-run   # both steps; --to <account> for another account
```

`import-plan` copies an export into an existing plan of `--account`: it checks everything first (`--dry-run` stops there), creates all applets, saves them with connections by name, uploads audio, sets the initial applet, then exports the target again and compares. On a failed write it deletes the applets it created. Announcer, Set Data Source, Data Connector and Menu applets are supported. `--skip-unsupported` leaves out applets that cannot be imported yet (e.g. ACD) and lists them; connections to them stay unset. Before writing, it also checks the data sources: those the plan reads but does not set itself (Set Data Source keys and Data Connector outputs count as set) should exist in the target account; the portal saves applets without checking. Missing ones are reported as warnings and the import goes ahead; `--require-data-sources` stops it before any write instead. Names are not case-sensitive (`LOG` and `Log` are the same data source). `copy-plan` runs the export and the import in one go; the export folder stays under `exports/` for reference.

The export drops IDs that belong to one account, keeps connections by applet name, records the initial applet, and downloads uploaded audio files. Data Connector definitions are exported as-is and can contain credentials.

Writes only run for accounts with `allow_portal_writes = true` in `accounts.toml`. Commands check that the portal session and the API see the same plan, so a session from the wrong account is refused.

`har` writes `architect.clean.har` (cookies, auth headers and tokens removed; scripts, styles and images dropped) and an `architect.bodies/` folder with every request/response body, and prints the list of calls, with writes marked `*`. Only share the `.clean.har`, and only after checking it.

## Tests

```powershell
pytest
```
