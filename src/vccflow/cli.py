"""Command-line entry point: vccflow <command> --account <name>."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from vccflow import har
from vccflow.auth import AuthError, TokenProvider
from vccflow.config import ConfigError, list_account_names, load_account
from vccflow.ipm import IpmClient, IpmError


def _client(args: argparse.Namespace) -> IpmClient:
    account = load_account(args.account, Path(args.config))
    return IpmClient(TokenProvider(account))


def _print_json(data) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def cmd_accounts(args: argparse.Namespace) -> None:
    for name in list_account_names(Path(args.config)):
        print(name)


def cmd_list_plans(args: argparse.Namespace) -> None:
    _print_json(_client(args).list_plans())


def cmd_list_mappings(args: argparse.Namespace) -> None:
    _print_json(_client(args).list_mappings())


def cmd_export_plan(args: argparse.Namespace) -> None:
    plan = _client(args).get_plan(args.plan_id)
    out = Path(args.out or f"exports/{args.account}/{args.plan_id}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Saved {out}")


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vccflow", description=__doc__)
    parser.add_argument("--config", default="accounts.toml", help="accounts file (default: accounts.toml)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("accounts", help="list configured accounts").set_defaults(func=cmd_accounts)

    def with_account(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("--account", "-a", required=True, help="account name from accounts.toml")
        return p

    with_account(sub.add_parser("list-plans", help="list interaction plans")).set_defaults(func=cmd_list_plans)
    with_account(sub.add_parser("list-mappings", help="list number/route mappings")).set_defaults(func=cmd_list_mappings)

    p = with_account(sub.add_parser("export-plan", help="save one plan as JSON"))
    p.add_argument("plan_id")
    p.add_argument("--out", help="output file (default: exports/<account>/<plan_id>.json)")
    p.set_defaults(func=cmd_export_plan)

    p = with_account(sub.add_parser("get", help="raw GET on any IPM path, for exploring the API"))
    p.add_argument("path", help="path under /ipm, e.g. mappings")
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("har", help="sanitize a browser HAR recording and list its API calls (offline)")
    p.add_argument("file", help="HAR file, e.g. exports/architect.har")
    p.add_argument("--keep-static", action="store_true", help="keep scripts, styles, images and fonts")
    p.set_defaults(func=cmd_har)

    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except (ConfigError, AuthError, IpmError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0
