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
vccflow export-plan   -a acme-test <plan-id>     # -> exports/acme-test/<plan-id>.json
vccflow get           -a acme-test <any/ipm/path> # raw GET for exploring
```

## Tests

```powershell
pytest
```
