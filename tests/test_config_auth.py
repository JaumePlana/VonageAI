from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vccflow.auth import AuthError, TokenProvider
from vccflow.config import ConfigError, load_account

ACCOUNTS = """
[accounts.acme-test]
region = "emea"
client_id_env = "T_ID"
client_secret_env = "T_SECRET"
"""


@pytest.fixture
def accounts_file(tmp_path: Path) -> Path:
    p = tmp_path / "accounts.toml"
    p.write_text(ACCOUNTS)
    return p


def test_load_account_reads_secrets_from_env(accounts_file):
    acc = load_account("acme-test", accounts_file, env={"T_ID": "id", "T_SECRET": "s"})
    assert acc.client_id == "id"
    assert acc.token_url == "https://emea.cc.vonage.com/Auth/connect/token"
    assert acc.ipm_base_url == "https://emea.api.cc.vonage.com/ipm"


def test_missing_env_var_is_reported(accounts_file):
    with pytest.raises(ConfigError, match="T_SECRET"):
        load_account("acme-test", accounts_file, env={"T_ID": "id"})


def test_unknown_account(accounts_file):
    with pytest.raises(ConfigError, match="Unknown account"):
        load_account("nope", accounts_file, env={})


def _provider(accounts_file, status=200, body=None):
    acc = load_account("acme-test", accounts_file, env={"T_ID": "id", "T_SECRET": "s"})
    session = MagicMock()
    session.post.return_value.status_code = status
    session.post.return_value.json.return_value = body or {"access_token": "tok", "expires_in": 900}
    session.post.return_value.text = "denied"
    now = [0.0]
    return TokenProvider(acc, session=session, clock=lambda: now[0]), session, now


def test_token_is_cached_until_near_expiry(accounts_file):
    provider, session, now = _provider(accounts_file)
    assert provider.token("interaction-plans:read") == "tok"
    now[0] = 800
    provider.token("interaction-plans:read")
    assert session.post.call_count == 1
    now[0] = 850
    provider.token("interaction-plans:read")
    assert session.post.call_count == 2


def test_token_request_failure(accounts_file):
    provider, _, _ = _provider(accounts_file, status=401)
    with pytest.raises(AuthError, match="401"):
        provider.token("interaction-plans:read")
