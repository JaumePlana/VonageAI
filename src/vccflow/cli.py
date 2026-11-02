"""Command-line entry point: vccflow <command> --account <name>."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from vccflow import compare, export, har, importer, manager, poc
from vccflow.auth import AuthError, TokenProvider
from vccflow.config import ConfigError, list_account_names, load_account
from vccflow.ipm import IpmClient, IpmError
from vccflow.manager import ManagerClient, ManagerError
from vccflow.media_channels import MediaChannelsClient, MediaChannelsError
from vccflow.portal import PortalClient, PortalError, WrongAccount, check_portal_account
from vccflow.portal_token import PortalTokenProvider


def _client(args: argparse.Namespace) -> IpmClient:
    account = load_account(args.account, Path(args.config))
    return IpmClient(TokenProvider(account))


def _manager(args: argparse.Namespace) -> ManagerClient:
    """Interaction Manager API client: API credentials if the account has them, else the portal login."""
    account = load_account(args.account, Path(args.config))
    if account.has_api_credentials:
        return ManagerClient(TokenProvider(account))
    print(f"No API credentials for '{account.name}'; using the portal login.", file=sys.stderr)
    return ManagerClient(PortalTokenProvider(account))


def _with_portal_login_fallback(args: argparse.Namespace, call):
    """Run call(client); if the API credentials lack the scope, retry with the portal login's token."""
    client = _manager(args)
    try:
        return call(client)
    except (AuthError, ManagerError) as e:
        refused = "invalid_scope" in str(e) or getattr(e, "status", None) == 403
        if not refused or isinstance(client.tokens, PortalTokenProvider):
            raise
    print(f"The API credentials of '{args.account}' lack the scope; using the portal login.", file=sys.stderr)
    return call(ManagerClient(PortalTokenProvider(client.tokens.account)))


def _portal(args: argparse.Namespace, account_name: str | None = None) -> tuple[PortalClient, ManagerClient | None]:
    """Portal client, plus the Interaction Manager API client used to look plans up (if credentials exist)."""
    account = load_account(account_name or args.account, Path(args.config))
    api = ManagerClient(TokenProvider(account)) if account.has_api_credentials else None
    return PortalClient(account), api


def _print_json(data) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def cmd_accounts(args: argparse.Namespace) -> None:
    for name in list_account_names(Path(args.config)):
        print(name)


def cmd_list_plans(args: argparse.Namespace) -> None:
    account = load_account(args.account, Path(args.config))
    if args.source == "portal":
        _print_json(PortalClient(account).list_plans())
    elif args.source == "ipm":
        _print_json(IpmClient(TokenProvider(account)).list_plans())
    else:
        _print_json(_with_portal_login_fallback(args, lambda c: c.list_plans(include_archived=args.archived)))


def cmd_list_mappings(args: argparse.Namespace) -> None:
    _print_json(_with_portal_login_fallback(args, lambda c: c.list_mappings()))


def cmd_list_destinations(args: argparse.Namespace) -> None:
    channels = tuple(args.channel) if args.channel else manager.CHANNELS
    account = load_account(args.account, Path(args.config))
    if args.source == "media-channels" and account.has_api_credentials:
        try:
            _print_json(MediaChannelsClient(TokenProvider(account)).list_destinations(channels))
            return
        except (AuthError, MediaChannelsError) as e:
            refused = "invalid_scope" in str(e) or getattr(e, "status", None) == 403
            if not refused:
                raise
            print(f"The API credentials of '{account.name}' cannot use the Media Channels API (scope admin:read); "
                  "using the Interaction Manager instead.", file=sys.stderr)
    _print_json(_with_portal_login_fallback(args, lambda c: c.list_destinations(channels)))


def _plan_metadata(portal: PortalClient, api: ManagerClient | None, plan_id: str) -> dict | None:
    """Reporting group and agent-interface flag: from the public IPM API (it has both), else the
    Interaction Manager API (reporting group only)."""
    if not api:
        return None
    try:
        return IpmClient(api.tokens).get_plan(plan_id)
    except IpmError:
        pass
    plan = next((p for p in api.list_plans(include_archived=True) if p["id"] == plan_id), None)
    return {"reportingGroupName": plan.get("reportingGroup"), "displayInAgentInterface": None} if plan else None


def _export(portal: PortalClient, api: ManagerClient | None, plan: str, out: str | None) -> tuple[Path, str]:
    """Export a plan; return (export folder, plan ID)."""
    account = portal.account.name
    plan_id, plan_name = poc.resolve_plan(plan, portal, api)
    metadata = _plan_metadata(portal, api, plan_id)
    folder = Path(out or f"exports/{account}/plans/{export._safe_filename(plan_name)}")
    print(f"Exporting '{plan_name}' from '{account}' to {folder}")
    doc = export.export_plan(portal, plan_id, plan_name, folder, metadata=metadata)
    n_media = sum(len(a.get("media", [])) for a in doc["applets"])
    print(f"Saved {folder / 'plan.json'}: {len(doc['applets'])} applets, {n_media} media files")
    if doc["requires"]["dataSources"]:
        print(f"Data sources used: {', '.join(doc['requires']['dataSources'])}")
    if doc["requires"]["dataSourcesNeeded"]:
        print(f"Of these, not set by the plan itself (must exist in a target account): "
              f"{', '.join(doc['requires']['dataSourcesNeeded'])}")
    if any(a.get("type") == "DataRetriever" for a in doc["applets"]):
        print("NOTE: Data Connector definitions are exported as-is and may contain credentials. Keep exports private.")
    return folder, plan_id


def _import(portal: PortalClient, source: Path, plan_id: str, plan_name: str, args: argparse.Namespace) -> None:
    dry_run = args.dry_run
    diffs = importer.import_plan(portal, source, plan_id, plan_name, dry_run=dry_run,
                                 skip_unsupported=args.skip_unsupported,
                                 require_data_sources=args.require_data_sources)
    if dry_run:
        print("Dry run: nothing was changed.")
    elif diffs:
        print("WARNING: the copy differs from the source:")
        for d in diffs:
            print(f"  {d}")
        raise PortalError(f"{len(diffs)} differences after import; check the plan in the Architect")
    else:
        print(f"Imported into '{plan_name}' ({portal.account.name}); the copy matches the source.")


def cmd_export_plan(args: argparse.Namespace) -> None:
    portal, api = _portal(args)
    _export(portal, api, args.plan, args.out)


def cmd_import_plan(args: argparse.Namespace) -> None:
    portal, api = _portal(args)
    plan_id, plan_name = poc.resolve_plan(args.target, portal, api)
    _import(portal, Path(args.source), plan_id, plan_name, args)


def cmd_copy_plan(args: argparse.Namespace) -> None:
    to_account = args.to_account or args.from_account
    # Resolve the target before exporting, so a wrong name or session fails fast.
    target, target_api = _portal(args, to_account)
    target_id, target_name = poc.resolve_plan(args.target, target, target_api)
    source, source_api = _portal(args, args.from_account)
    folder, source_id = _export(source, source_api, args.plan, args.out)
    if to_account == args.from_account and source_id == target_id:
        raise PortalError("Source and target are the same plan")
    print()
    _import(target, folder, target_id, target_name, args)


def cmd_compare_plans(args: argparse.Namespace) -> None:
    to_account = args.to_account or args.from_account
    source, source_api = _portal(args, args.from_account)
    target, target_api = _portal(args, to_account)
    source_id, source_name = poc.resolve_plan(args.plan, source, source_api)
    target_id, target_name = poc.resolve_plan(args.target, target, target_api)
    if to_account == args.from_account and source_id == target_id:
        raise PortalError("Source and target are the same plan")
    print(f"Comparing '{source_name}' ({args.from_account}) with '{target_name}' ({to_account})")
    report = compare.compare_plans(
        source, source_id, source_name, target, target_id, target_name,
        _plan_metadata(source, source_api, source_id), _plan_metadata(target, target_api, target_id),
    )
    print(compare.format_report(report))
    found = len(report["differences"]) + len(report["problems"])
    if found:
        raise PortalError(f"{found} differences or problems found")


def cmd_compare_exports(args: argparse.Namespace) -> None:
    print(f"Comparing saved exports {args.source} and {args.target}")
    report = compare.compare_exports(Path(args.source), Path(args.target))
    print(compare.format_report(report))
    found = len(report["differences"]) + len(report["problems"])
    if found:
        raise PortalError(f"{found} differences or problems found")


def cmd_session(args: argparse.Namespace) -> None:
    account = load_account(args.account, Path(args.config))
    signed_in = PortalClient(account).signed_in_account()
    check_portal_account(account, signed_in)
    note = "" if account.portal_account else " (no portal_account set: not checked)"
    print(f"Portal session for '{account.name}' is valid: logged in to '{signed_in}'{note}.")


def cmd_get(args: argparse.Namespace) -> None:
    _print_json(_client(args).get(args.path))


def cmd_har(args: argparse.Namespace) -> None:
    har_path = Path(args.file)
    if not har_path.exists():
        raise ConfigError(f"{har_path} not found")
    clean_path, bodies_dir, calls, leftovers, n_bodies = har.process(har_path, keep_static=args.keep_static)
    print(har.format_summary(calls))
    print(f"\nSanitized HAR: {clean_path}")
    print(f"Bodies ({n_bodies} files): {bodies_dir}")
    if leftovers:
        print("\nWARNING: possible secrets still present, check before sharing:", file=sys.stderr)
        for s in leftovers:
            print(f"  {s}", file=sys.stderr)


def cmd_login(args: argparse.Namespace) -> None:
    from vccflow.portal_login import login

    account = load_account(args.account, Path(args.config))
    path = login(account, browser_channel=args.browser)
    signed_in = PortalClient(account).signed_in_account()
    try:
        check_portal_account(account, signed_in)
    except WrongAccount:
        Path(path).unlink()  # never keep a login to the wrong account under this name
        raise
    print(f"You have been logged in to '{signed_in}'. Session saved to {path}")
    if not account.portal_account:
        print(f"Add this to [accounts.{account.name}] in accounts.toml to check it from now on, if it is the right account:\n"
              f'portal_account = "{signed_in}"')


def cmd_applets(args: argparse.Namespace) -> None:
    portal, api = _portal(args)
    plan_id, _ = poc.resolve_plan(args.plan, portal, api)
    _print_json(portal.list_applets(plan_id))


def cmd_applet(args: argparse.Namespace) -> None:
    portal, api = _portal(args)
    plan_id, _ = poc.resolve_plan(args.plan, portal, api)
    state = portal.get_applet_state(plan_id, portal.find_applet(plan_id, args.name))
    _print_json(state["appletProperties"])


def cmd_initial_applet(args: argparse.Namespace) -> None:
    portal, api = _portal(args)
    plan_id, plan_name = poc.resolve_plan(args.plan, portal, api)
    if args.set:
        portal.find_applet(plan_id, args.set)  # fail early on a typo
        portal.set_initial_applet(plan_id, args.set)
    current = portal.get_initial_applet(plan_id)
    if args.set and current != args.set:
        raise PortalError(f"Initial applet is still '{current}' after setting '{args.set}'")
    print(f"{plan_name}: initial applet {current!r}" if current else f"{plan_name}: no initial applet set")


def cmd_poc_announcer(args: argparse.Namespace) -> None:
    portal, api = _portal(args)
    plan_id, plan_name = poc.resolve_plan(args.plan, portal, api)
    print(f"Plan: {plan_name} ({plan_id}) in account '{args.account}'")
    poc.run_announcer_poc(portal, plan_id, args.name, args.text, voice=args.voice, keep=args.keep)
    print("POC passed.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vccflow", description=__doc__)
    parser.add_argument("--config", default="accounts.toml", help="accounts file (default: accounts.toml)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("accounts", help="list configured accounts").set_defaults(func=cmd_accounts)

    def with_account(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("--account", "-a", required=True, help="account name from accounts.toml")
        return p

    p = with_account(sub.add_parser("list-plans", help="list interaction plans"))
    p.add_argument("--source", choices=("manager", "ipm", "portal"), default="manager",
                   help="manager: Interaction Manager API (default, richest); ipm: public IPM API; portal: plan page")
    p.add_argument("--archived", action="store_true", help="include archived plans (manager source)")
    p.set_defaults(func=cmd_list_plans)
    with_account(sub.add_parser("list-mappings", help="list mappings (address -> plan, with rules)")).set_defaults(
        func=cmd_list_mappings)
    p = with_account(sub.add_parser("list-destinations", help="list destinations (addresses) per channel"))
    p.add_argument("--channel", action="append", choices=manager.CHANNELS, help="only this channel (repeatable)")
    p.add_argument("--source", choices=("media-channels", "manager"), default="media-channels",
                   help="media-channels: Media Channels API (default, scope admin:read); "
                        "manager: Interaction Manager API (falls back to the portal login)")
    p.set_defaults(func=cmd_list_destinations)

    p = with_account(sub.add_parser("export-plan", help="export a plan with all applets and audio files (portal)"))
    p.add_argument("plan", help="plan ID or name")
    p.add_argument("--out", help="output folder (default: exports/<account>/plans/<plan name>)")
    p.set_defaults(func=cmd_export_plan)

    p = with_account(sub.add_parser("import-plan", help="copy an exported plan into an existing plan (portal, writes)"))
    p.add_argument("source", help="export folder with plan.json, e.g. \"exports/JPlana/plans/DISH Web Services\"")
    p.add_argument("target", help="target plan ID or name (in --account)")
    p.add_argument("--dry-run", action="store_true", help="only run the checks")
    p.add_argument("--skip-unsupported", action="store_true",
                   help="leave out applets that cannot be imported yet; connections to them stay unset")
    p.add_argument("--require-data-sources", action="store_true",
                   help="stop before writing if the target account lacks data sources the plan reads "
                        "(default: warn and import anyway)")
    p.set_defaults(func=cmd_import_plan)

    p = sub.add_parser("copy-plan", help="export a plan and import it into another plan (portal, writes)")
    p.add_argument("plan", help="source plan ID or name")
    p.add_argument("target", help="target plan ID or name; it must exist")
    p.add_argument("--from", dest="from_account", required=True, help="source account from accounts.toml")
    p.add_argument("--to", dest="to_account", help="target account (default: the --from account)")
    p.add_argument("--out", help="export folder (default: exports/<from account>/plans/<plan name>)")
    p.add_argument("--dry-run", action="store_true", help="export, then only run the import checks")
    p.add_argument("--skip-unsupported", action="store_true",
                   help="leave out applets that cannot be imported yet; connections to them stay unset")
    p.add_argument("--require-data-sources", action="store_true",
                   help="stop before writing if the target account lacks data sources the plan reads "
                        "(default: warn and import anyway)")
    p.set_defaults(func=cmd_copy_plan)

    p = sub.add_parser("compare-plans", help="compare two plans, also across accounts (portal, read-only)")
    p.add_argument("plan", help="source plan ID or name")
    p.add_argument("target", help="target plan ID or name")
    p.add_argument("--from", dest="from_account", required=True, help="source account from accounts.toml")
    p.add_argument("--to", dest="to_account", help="target account (default: the --from account)")
    p.set_defaults(func=cmd_compare_plans)

    p = sub.add_parser("compare-exports", help="compare two saved export folders (offline)")
    p.add_argument("source", help="export folder with plan.json")
    p.add_argument("target", help="export folder with plan.json")
    p.set_defaults(func=cmd_compare_exports)

    with_account(sub.add_parser("session", help="check that the account's portal login is still valid")).set_defaults(
        func=cmd_session)

    p = with_account(sub.add_parser("get", help="raw GET on any IPM path, for exploring the API"))
    p.add_argument("path", help="path under /ipm, e.g. mappings")
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("har", help="sanitize a browser HAR recording and list its API calls (offline)")
    p.add_argument("file", help="HAR file, e.g. exports/HAR/architect.har")
    p.add_argument("--keep-static", action="store_true", help="keep scripts, styles, images and fonts")
    p.set_defaults(func=cmd_har)

    p = with_account(sub.add_parser("login", help="sign in to the portal in a browser and save the session"))
    p.add_argument("--browser", default="chrome", help="browser channel: chrome (default) or msedge")
    p.set_defaults(func=cmd_login)

    p = with_account(sub.add_parser("applets", help="list the applets of a plan (portal)"))
    p.add_argument("plan", help="plan ID, or plan name if the account has API credentials")
    p.set_defaults(func=cmd_applets)

    p = with_account(sub.add_parser("applet", help="show one applet's settings (portal)"))
    p.add_argument("plan", help="plan ID or name")
    p.add_argument("name", help="applet name")
    p.set_defaults(func=cmd_applet)

    p = with_account(sub.add_parser("initial-applet", help="show or set the applet a plan starts with (portal)"))
    p.add_argument("plan", help="plan ID or name")
    p.add_argument("--set", metavar="APPLET", help="set it to this applet (writes)")
    p.set_defaults(func=cmd_initial_applet)

    p = with_account(sub.add_parser("poc-announcer", help="create, verify and delete an Announcer (portal, writes)"))
    p.add_argument("plan", help="plan ID or name")
    p.add_argument("--name", default="ANN Claude POC", help="applet name")
    p.add_argument("--text", default="This is a test announcement created by vccflow.")
    p.add_argument("--voice", default="Amy")
    p.add_argument("--keep", action="store_true", help="keep the applet instead of deleting it")
    p.set_defaults(func=cmd_poc_announcer)

    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except (ConfigError, AuthError, IpmError, ManagerError, MediaChannelsError, PortalError, TimeoutError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0
