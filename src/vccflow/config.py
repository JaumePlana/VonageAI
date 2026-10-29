"""Load per-account settings from accounts.toml and credentials from the environment."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

REGIONS = ("emea", "nam", "apac")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Account:
    name: str
    region: str
    # API credentials are optional: portal-only accounts work without them.
    client_id: str | None = None
    client_secret: str | None = None
    # Portal writes (create/save/delete applets) must be enabled per account.
    allow_portal_writes: bool = False

    @property
    def token_url(self) -> str:
        return f"https://{self.region}.cc.vonage.com/Auth/connect/token"

    @property
    def ipm_base_url(self) -> str:
        return f"https://{self.region}.api.cc.vonage.com/ipm"

    @property
    def portal_url(self) -> str:
        return f"https://{self.region}.newvoicemedia.com/CallCentre"

    @property
    def has_api_credentials(self) -> bool:
        return bool(self.client_id and self.client_secret)


def load_accounts_file(path: Path) -> dict:
    if not path.exists():
        raise ConfigError(
            f"{path} not found. Copy accounts.example.toml to accounts.toml and edit it."
        )
    with path.open("rb") as f:
        return tomllib.load(f).get("accounts", {})


def list_account_names(path: Path) -> list[str]:
    return sorted(load_accounts_file(path))


def load_account(name: str, path: Path, env: dict[str, str] | None = None) -> Account:
    env = os.environ if env is None else env
    accounts = load_accounts_file(path)
    if name not in accounts:
        known = ", ".join(sorted(accounts)) or "(none)"
        raise ConfigError(f"Unknown account '{name}'. Known accounts: {known}")

    entry = accounts[name]
    region = entry.get("region", "").lower()
    if region not in REGIONS:
        raise ConfigError(f"Account '{name}': region must be one of {', '.join(REGIONS)}")

    def secret(key: str) -> str | None:
        var = entry.get(key)
        if not var:
            return None
        value = env.get(var)
        if not value:
            raise ConfigError(f"Account '{name}': environment variable {var} is not set")
        return value

    return Account(
        name=name,
        region=region,
        client_id=secret("client_id_env"),
        client_secret=secret("client_secret_env"),
        allow_portal_writes=bool(entry.get("allow_portal_writes", False)),
    )
