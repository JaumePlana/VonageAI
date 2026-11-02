---
name: copy-interaction-plan
description: Copy the applets of a VCC Interaction Plan into another plan, in the same account or from one account to another, then verify and compare both plans. Use when the user asks to copy an Interaction Plan, e.g. "copy IP1 from Account X to Account Y" or "copy plan A to plan B in Account X". Accounts are names from accounts.toml.
argument-hint: <source plan> [target plan] from <account> [to <account>]
---

# Copy an Interaction Plan

Run everything from the project root with `.venv/Scripts/vccflow.exe` (shown as `vccflow`). Follow the steps in order, and report everything the commands print: warnings and errors must reach the user.

## 1. Work out the request

From the request, get:
- **Source account and plan.** Ask if either is missing.
- **Target account.** The same as the source if none is given.
- **Target plan.** If none is given, use the source plan's name, and say so.

Check the account names with `vccflow accounts`. If one isn't listed, stop and tell the user: it has to be added to `accounts.toml`.

## 2. Check the logins

For the source account and then the target account (once if they're the same):

```
vccflow session -a <account>
```

If the session has expired or is missing, tell the user and ask with AskUserQuestion, naming the account clearly: "The portal login for **<account>** has expired. Do you want to log in to **<account>** now?"

- **Yes:** tell the user a browser will open and to sign in to **<account>**, then run this with a 320000 ms timeout, and run `vccflow session -a <account>` again:
  ```
  vccflow login -a <account>
  ```
- **No:** stop, and say the copy wasn't run.

`portal_account` is mandatory in `accounts.toml`. `login` prints the account it signed in to, and removes the saved login if it's any other account. Commands do the same, so a saved login can disappear for this reason: it isn't an error to retry. If it happens, tell the user which account they signed in to and ask again. If an account has no `portal_account`, tell the user: `login` reports the name and the line to add without keeping the login, so they need to add it and log in once more. vccflow never logs out of VCC. Don't add it yourself unless they confirm the name is right.

Ask separately for each account. Never log in without a yes. A later command can also report an expired session, because logins expire during long runs: ask the same way, then repeat that step.

## 3. Safety checks

- **Writes:** only accounts with `allow_portal_writes = true` in `accounts.toml` can be written to, and the commands refuse the others. If the target isn't allowed, tell the user and stop. Don't change `accounts.toml` unless they ask.
- **Production:** if the target account or plan looks like production (e.g. "prod" in the name), or the user has described it that way, ask for explicit confirmation naming the account and plan, even if the request already asked for the copy.

## 4. Dry run

```
vccflow copy-plan "<source plan>" "<target plan>" --from <source account> --to <target account> --dry-run
```

This exports the source plan to `exports/<source account>/plans/<plan>/` and changes nothing in VCC. Act on what it reports:

| Result | What to do |
|---|---|
| A name "already exists in the target plan" | The target isn't empty. List the conflicts and stop. Don't delete anything unless the user asks. |
| "is not supported for import yet" | List those applets and ask whether to copy the rest with `--skip-unsupported`. Say which paths would be left unset. Mention `/add-applet-type` for adding support. |
| "points to ..., which is not in the plan" | The source has a broken path. Report it and ask how to proceed. |
| "Expected one plan" | The plan name is wrong, or the target plan doesn't exist. vccflow doesn't create plans. |
| WARNING about data sources | Doesn't stop the copy. Keep these for the final report. Add `--require-data-sources` only if the user asked to stop on missing ones. |

## 5. Copy

Run the same command without `--dry-run`, plus any option the user agreed to, with a 600000 ms timeout. Keep the full output.

If it fails, the importer deletes the applets it created. Confirm that with `vccflow applets -a <target account> "<target plan>"`, report the error, and stop. Don't retry without working out the cause.

## 6. Compare both plans

Run this after every copy, even if the copy reported that it matches:

```
vccflow compare-plans "<source plan>" "<target plan>" --from <source account> --to <target account>
```

It is read-only, and exits with an error when it finds differences; that is a result to report, not a failure. It reports:

- **DIFFERENCES:** applets missing in either plan or of another type; paths missing or different, in both directions; settings, including skills; audio files (name and content); initial applet; reporting group.
- **PROBLEMS:** paths to applets that aren't in the plan; GUIDs and IDs that don't resolve inside their own plan; data sources the target lacks, with the applets that use them.
- **NOTES:** letter-case differences, which VCC ignores, and applets compared only through their editor page.

Investigate any difference you can't explain before reporting it, without printing credentials.

## 7. Report to the user

1. **Result:** copied or not, from which account and plan to which, with counts of applets and audio files, and the initial applet.
2. **Errors and warnings from the copy:** all of them, including skipped applets and paths left unset.
3. **Differences between the plans:** all of them, grouped by applet. Say plainly if there are none. Expected ones (e.g. skipped applets) should be labelled as expected.
4. **Problems:** missing data sources with the applets that use them, and broken paths or IDs.
5. **Not checked:** what the comparison couldn't see (below), where it applies to this plan.

### Limits to state when they apply

- Applet types that can't be exported yet are compared through their editor page fields and embedded model. This covers Skill Tagger skills and Data Router rules, but audio on those pages isn't compared, and pages with no readable fields, like Set Whisper, show nothing. Their data sources aren't counted in the data source check.
- Differences in account-specific IDs in those pages (e.g. a skill or queue ID) can be false differences between accounts.
- A plan's data sources being present doesn't prove the flow works: the comparison reads configuration, and no call is placed.

## Rules

- **Credentials:** never print Data Connector definitions, `.env`, `.sessions/` or `accounts.toml` secrets. If the plan's Web Service Data Connectors hold credentials and the copy crosses accounts, say that they were copied too. Exports hold credentials: they stay in `exports/`, out of git.
- **Nothing beyond the request:** don't delete applets, change the source plan, edit `accounts.toml`, or commit.
