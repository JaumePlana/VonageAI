---
name: add-applet-type
description: Add support for a new VCC applet type to vccflow from a browser HAR recording, so export-plan, import-plan and copy-plan can handle it. Use when the user adds a HAR for an applet type (e.g. ACD, Router, Data Router, Collect Input) or asks to support copying one. Arguments are the HAR name and the applet type, e.g. "exports/HAR/acd.har ACD".
argument-hint: <har file> [applet type]
---

# Add an applet type to vccflow

Turn a HAR of creating and saving an applet into export and import support, with tests and a live check. Base the code on what the recording shows, never on guesses about the portal.

Read `PROGRESS.md` ("Resume here") first, then `src/vccflow/export.py`, `src/vccflow/importer.py` and `src/vccflow/portal.py`.

## Rules

- **Secrets:** read only the sanitized output. Run `vccflow har` first, and use `scripts/inspect_har.py`, which masks credentials. Never print `.env`, `.sessions/`, a raw `.har`, or Data Connector definitions.
- **Writes:** live writes only to a plan the user names as a scratch plan, in an account with `allow_portal_writes = true`. Never PROD. Don't empty a plan or delete applets you didn't create without asking.
- **Git:** commit or push only when the user asks. `exports/` and the user's notes file stay out.
- **No guessing:** if the recording doesn't show a case, refuse it in code with a clear error (see `data_connector_form`), and tell the user what to record.

## 1. Read the recording

```
.venv/Scripts/vccflow.exe har exports/HAR/<name>.har
.venv/Scripts/python.exe .claude/skills/add-applet-type/scripts/inspect_har.py exports/HAR/<name>.clean.har --full
```

Find:
- The `appletType` value in `AppletCreate/Create`, and the editor page it redirects to.
- How saving works, which decides the family below.
- Each setting, and which saves changed it. A good recording changes every option; if important options never change, ask for another recording.

## 2. Identify the editor family

| Family | Editor page | Read | Save | Example |
|---|---|---|---|---|
| **React** | `Applet?` | `initialState` | `Applet/Save` (base64 JSON) | Announcer, Menu |
| **MVC form** | `<Name>?` (`ACD`, `DataRouter`...) | page model (`load...Model`) and form fields | form post with `__RequestVerificationToken` | Data Connector |
| **Classic ASP** | `*.asp`, `*.aspx` | HTML form | form post | not supported yet: expect new parsing |

- **React:** add the type to `EDITOR_TYPES` in `importer.py`; the save path already exists.
- **MVC form or Classic ASP:** `export_applet` only knows `DataRetriever` besides React, so add a branch for reading. Add `portal.save_<type>()` and a `<type>_form()` builder modelled on `save_data_connector` and `data_connector_form`. Check the recorded headers too: AJAX-only calls answer 405 without `X-Requested-With`. In `import_plan`, route the type to its save method. `export_applet` must set `type` to the `appletType` value that `AppletCreate/Create` takes.

## 3. Map every connection and ID

This is where copies go wrong. Search the properties for applet names, GUIDs and numeric IDs. Connections must be exported **by name** in `next`, because IDs and GUIDs differ per account:

| Shape in the applet | Example | In `next` | Rebuilt on import |
|---|---|---|---|
| `NextApplet*` + `*Guid` | Announcer | same key | name and target GUID (already handled) |
| Map of names | Menu `Choices` | `Choices.<key>` | the map |
| Name + GUID | Menu `TimeoutAppletName` | `TimeoutApplet` | name and target GUID |
| Numeric ID + GUID | Menu `FailureAppletId` | `FailureApplet` | target ID and GUID |
| List of branches | routers (likely) | `<Field>.<index or key>` | the list, keeping order and conditions |

- `split_connections` converts on export, using `names_by_id` for numeric IDs. The loop over `nxt` in `import_plan` converts back.
- Add account-specific properties to `ACCOUNT_SPECIFIC` or the new branch.
- Also look for references to things outside the plan: skills, groups, queues, transfer strings, schedules, other plans. These differ per account too. Keep them by **name** when the recording gives a name. If they are only IDs and you can't resolve the name, say so to the user: don't copy an ID across accounts silently. Consider a check like `check_data_sources` that warns when the target lacks one.

## 4. Media and data sources

- Uploaded audio with `{"AppletMedia": {"Id": n}}` is found by `find_media_refs`. Anything else needs its own handling.
- Add the type's data source fields to `data_sources_used` (reads) or `data_sources_created` (sets), in `export.py`. Names ignore letter case.

## 5. Tests

Add unit tests from the recorded values, as in `tests/test_importer.py` and `tests/test_data_connector.py`:
- Export: connections by name, account-specific values gone.
- Import: connections rebuilt with the target's IDs and GUIDs, and no export-only keys left in the saved properties.
- Form builders (if any) reproduce the recorded post, and unrecorded cases are refused.
- Data source fields.

For a form-based type, also rebuild each recorded save from its response page and compare it with the recorded post.

Run `.venv/Scripts/python.exe -m pytest -q`. All must pass.

## 6. Live check

If the portal login has expired, ask the user to run `! .venv/Scripts/vccflow.exe login -a <account>`.

1. Ask which plan contains the type and which empty scratch plan to copy into, unless the user said.
2. `vccflow copy-plan "<plan>" "<scratch>" --from <a> --to <b> --dry-run`, then without `--dry-run`.
3. The read-back must match. A difference is a bug or a new finding: investigate, don't dismiss it. Compare the new applet's connections directly as well, resolving its IDs and GUIDs against the target's applet list.

## 7. Finish

- Add the type to the supported list in `README.md`.
- Add the findings to `PROGRESS.md`: endpoints, connection fields, what was tested live, and what wasn't. Update the test count and "Resume here".
- Report plainly what works, what was tested live, what was only unit-tested, and which options the recording didn't cover. Don't commit unless asked.
