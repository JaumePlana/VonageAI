# Progress log

Goals and background: [CONTEXT.md](CONTEXT.md). Setup and usage: [README.md](README.md).

## Status (2026-10-28)

### Done
- Python project `vccflow` set up (`.venv`, Python 3.15). 5 unit tests pass.
- Per-account config: `accounts.toml` holds region and variable names; `.env` holds secrets. Configured: `Viessmann-test`, `Viessmann-prod` (emea).
- Login (client credentials, `interaction-plans:read`) works against Viessmann Test.
- Read-only commands work: `accounts`, `list-plans` (82 plans in Test), `list-mappings`, `export-plan`, `get`.

### Findings
- Interaction Plan Manager API: root `https://emea.api.cc.vonage.com/ipm`, header `Accept: application/vnd.newvoicemedia.v1+json`. Token from `https://emea.cc.vonage.com/Auth/connect/token`.
- Working paths: `interactionplans`, `interactionplans/{id}`, `mappings`. `interaction-plans` returns 404.
- **The API returns plan metadata only, no applets.** `GET interactionplans/{id}` gives the ID, name, reporting group, display flag and created/updated info. Sub-paths (`/applets`, `/definition`, `/content`, `/versions`, `/export`, `/flow`) return 404, and Accept v2/v3 returns 400.
- Vonage's official cross-account copy goes through support, and needs manual fixes afterwards (transfer strings, groups, skills, DNR rules, ACD/UCD targets).
- The user expects Vonage won't provide an applet API (a draft request to Vonage was written, but probably won't be sent).

### Agreed approach
1. **Discovery:** in the Test account, record a HAR in the browser dev tools while opening a plan in the Interaction Plans Architect, adding an Announcer, changing a setting and saving. Save it under `exports/` (git-ignored). It contains session cookies, so strip them before analysing.
2. **Decide on the method:**
   - **A.** The Architect uses a JSON backend: call those endpoints from Python, with the session from a Playwright persistent login. Safeguards: dry run, read back and compare after each write, Test → Test before any PROD write.
   - **B.** Otherwise: automate the Architect page with Playwright.
3. **Common plan format:** applets, settings and connections, with groups, skills and transfer strings referenced by name. Copy Test → PROD and DrawIO/Visio import both use it.
4. Then the original order: Announcer POC → copy Test → PROD → DrawIO/Visio import.

### HAR findings (2026-10-29): case A, a JSON backend
Recorded in the "DISH Web Services" plan (`e9d0c8ab-…`). That plan is **not** in Viessmann Test, so the recording was made in another account.
- The Architect is the legacy portal `https://emea.newvoicemedia.com/CallCentre/apps/...`, logged in through OIDC with a **cookie session**. The save and create calls carry no CSRF header.
- **List a plan's applets:** `POST Applet/GetAppletsForInteractionPlan`, body `{"interactionPlanId"}`. Returns id, guid, name, appletType and configUrl per applet.
- **Create an applet:** `POST AppletCreate/Create`, form `interactionPlanId, appletType=DataAnnouncer, appletName, Update=Add`. Redirects (302) to the editor.
- **Read an applet's settings:** `GET Applet?phonenum=<planId>&myapp=<name>&application=<type>`. HTML with embedded JSON `appletProperties` and `metadata`.
- **Save an applet:** `POST Applet/Save`, JSON `{interactionPlanId, appletName, appletProperties: base64(JSON)}`. Returns `AppletProperties, AppletMetadata, DataSourceKeys`.
- **Delete an applet:** `POST appletdelete/delete?interactionPlanId=<id>`, form `<AppletName>=true`.
- **Connections between applets:** `NextAppletSuccess` / `NextAppletFail` (by applet name), plus `*Guid`. The page first saved with the GUIDs empty, then saved again with them filled in. GUIDs are per account, so names are the key to copy by.
- Announcer property fields: `AnnouncementType` (Text/ExternalFile/...), `Voice`, `Text`, `BargeIn`, `DataSource`, `AnnouncementMedia`, template fields, `EntityId`, `AppletGuid`.

## Next steps
- [x] Claude: write the HAR clean-up script: `vccflow har <file>` (2026-10-29).
- [x] User: record the HAR (done, in the DISH account).
- [x] Together: analyse the HAR. Result: **A**.
- [ ] Claude: Playwright login that saves the portal session, plus a portal client (list applets, read settings, create, save).
- [ ] POC: create an Announcer in a scratch plan in a Test account, read it back, delete it.
