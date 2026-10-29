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
vccflow list-plans    -a acme-test
vccflow list-mappings -a acme-test
vccflow get           -a acme-test <any/ipm/path> # raw GET for exploring
vccflow har           exports/architect.har       # offline: sanitize a browser recording
```

### Portal (applets)

Applets are not in the public API; these commands use the Architect's own backend through your portal login session, saved under `.sessions/` (git-ignored, contains cookies).

```powershell
vccflow login         -a JPlana                    # opens Chrome (--browser msedge for Edge); sign in, the window closes by itself
vccflow applets       -a JPlana "zz Claude POC"    # plan name or ID
vccflow applet        -a JPlana "zz Claude POC" "ANN Test"
vccflow poc-announcer -a JPlana "zz Claude POC"    # create, verify, delete (add --keep to keep it)
vccflow export-plan   -a JPlana "DISH Web Services" # -> exports/JPlana/plans/DISH Web Services/plan.json + media/
vccflow initial-applet -a JPlana "zz Claude POC"   # show; add --set "<applet>" to change it
```

The export drops IDs that belong to one account, keeps connections by applet name, records the initial applet, and downloads uploaded audio files. Data Connector definitions are exported as-is and can contain credentials.

Writes only run for accounts with `allow_portal_writes = true` in `accounts.toml`. Commands check that the portal session and the API see the same plan, so a session from the wrong account is refused.

`har` writes `architect.clean.har` (cookies, auth headers and tokens removed; scripts, styles and images dropped) and an `architect.bodies/` folder with every request/response body, and prints the list of calls, with writes marked `*`. Only share the `.clean.har`, and only after checking it.

## Tests

```powershell
pytest
```
