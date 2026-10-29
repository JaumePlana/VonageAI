# Progress log

Goals and background: [CONTEXT.md](CONTEXT.md). Setup and usage: [README.md](README.md).

## Status (2026-10-28)

### Done
- Python project `vccflow` set up (`.venv`, Python 3.15). 41 unit tests pass.
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
- [x] Claude: Playwright login (`vccflow login`, session in `.sessions/`), portal client (`portal.py`), and the `applets`, `applet` and `poc-announcer` commands (2026-10-29). The recorded account is **JPlana** (not DISH; "DISH Web Services" is one of its plans). The POC plan is "zz Claude POC" (`da7310e5-10c5-4080-a8b8-34d7e94323f1`). Writes are enabled only for JPlana.
- [x] POC: create an Announcer in a scratch plan, read it back, delete it. **Passed on 2026-10-29** in JPlana / "zz Claude POC". The applet "ANN Claude POC" was kept with `--keep`. Deleting applets works too: ANN Test 1 and 2 were deleted through the client.
- [x] Step 2a, export (2026-10-29): `vccflow export-plan -a <account> <plan>` writes `exports/<account>/plans/<plan>/plan.json` plus `media/`. Tested on DISH Web Services (JPlana): 8 applets and 1 WAV file.
  - Format `vccflow.plan/1`: per applet `name`, `type`, `settings` (IDs/GUIDs removed), `next` (connections by name), `media` (file path). Also `requires.dataSources`.
  - Announcer and Set Data Source use the React editor (`initialState`). Data Connector (DataRetriever) is an older MVC form: page model plus an `ApiInteractionDefinition` textarea, with a CSRF token (`__RequestVerificationToken`).
  - Media: `Applet/GetAppletMediaInfo {appletMediaId}` returns name + url, and the url downloads the WAV. Upload goes through `Applet/AppletMediaUpload` (multipart: interactionPlanId, appletName, file; max 10 MB), which returns `appletMediaId`.
  - Data Connector definitions can contain credentials (they do in DISH). Exports are git-ignored; keep them private.
- [x] Data Connector save and the initial applet (2026-10-29), from the user's `dataconnector.har` and `initialapplet.har`:
  - **Initial applet:** `POST InteractionArchitect/SetInitialApplet`, form `interactionPlanId, initialApplet=<name>, Update=Set` (301). Read it from `applications.asp?phonenum=<id>`: that page lists *every* plan, and the second cell of the plan's row links the initial applet or says "WARNING initial applet not selected". It **cannot be cleared** (an empty value is ignored); deleting the applet clears it. Export writes it to `plan.initialApplet`; CLI `vccflow initial-applet`.
  - **Data Connector save:** GET the page (`DataRetriever?phonenum=&myapp=&application=DataRetriever`) for `__RequestVerificationToken`, then `POST DataRetriever/EditDataProvider` (and `EditMatchObject` for Salesforce), both AJAX-only (need `X-Requested-With`, otherwise 405), then the form `POST DataRetriever?phoneNum=&myApp=`. It answers 200 with the page; refusals are in `validation-summary-errors` (e.g. the XML is validated, SOQL needs `LIMIT 1..10`, a Salesforce update needs the object's `Id` field mapped).
  - Salesforce pages use `loadCrmModel(...)`; the page model holds the full configuration (`CrmRetrievalViewModel`: MatchBy/MatchDataSource/FieldToMatch/MaxRecords/UseRawSoql/Soql; `ResponseItems`; `CrmCreationViewModel.CrmObjectParameters` for updates). `portal.data_connector_form()` rebuilds the post from the exported settings.
  - Supported: Web Service; Salesforce get by CLID or data source, one or several records, raw SOQL; Salesforce update. Not recorded yet (refused): match by IVR Slot, Salesforce create, other providers (MS Dynamics, Zendesk, Google, VGIP...).
  - **Live round-trip passed** in JPlana / "zz Claude POC": every variant saved and read back, initial applet set; scratch applets deleted, plan back to "no initial applet".
  - The plan page has a built-in **"Copy To"** (`docopy.asp?source=<planId>`), not explored yet; probably same-account only.
- [x] Step 2b, import (2026-10-29): `vccflow import-plan -a <account> <export folder> <target plan> [--dry-run]` (`importer.py`). **Copied DISH Web Services into zz Claude POC (JPlana): 11 applets, the WAV and the initial applet; the read-back matches, and the connection GUIDs point to the new applets.**
  - Order: checks (name conflicts, dangling connections, unsupported Data Connectors, media ≤ 10 MB) → create all → save each (React applets: target defaults + exported settings + `NextApplet*` names with the target's GUIDs; Data Connectors: form post) → upload media (`Applet/AppletMediaUpload`, file named after the VCC media name) → initial applet → export the target and compare. A failed write deletes the created applets.
  - A Data Connector with nothing next must post `NextApplet`, `NextAppletFailure` and `NextAppletInterfaceFailure` as empty values (`dcwithoutnextapplets.har`); leaving them out is refused.
- [x] `vccflow copy-plan <plan> <target> --from <account> [--to <account>] [--dry-run]` (2026-10-29): resolves the target first, exports to `exports/<from>/plans/<plan>/`, then imports. Refuses the same plan as source and target.
- [ ] Next: copy across accounts (Test → PROD). Check first that the target has the data sources, the Salesforce/Web Service connections and the other referenced items; nothing on PROD before a Test → Test run between two accounts.
