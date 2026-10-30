from unittest.mock import MagicMock

from vccflow.config import Account
from vccflow.manager import ManagerClient


def client(*bodies):
    tokens = MagicMock()
    tokens.account = Account(name="t", region="emea")
    tokens.token.side_effect = lambda scope: f"tok-{scope}"
    session = MagicMock()
    session.get.side_effect = [MagicMock(status_code=200, json=MagicMock(return_value=b)) for b in bodies]
    return ManagerClient(tokens, session=session), session


def test_plans_hide_archived_unless_asked():
    plans = [{"id": "a", "name": "A", "archived": False}, {"id": "b", "name": "B", "archived": True}]
    c, session = client(plans, plans)
    assert [p["id"] for p in c.list_plans()] == ["a"]
    assert len(c.list_plans(include_archived=True)) == 2
    url = session.get.call_args.args[0]
    assert url == "https://emea.api.newvoicemedia.com/interaction-plan/plans"


def test_destinations_use_the_addresses_scope():
    c, session = client([{"type": "Pstn", "address": "3233001530"}], [])
    assert c.list_destinations(("pstn", "api")) == {"pstn": [{"type": "Pstn", "address": "3233001530"}], "api": []}
    auth = session.get.call_args.kwargs["headers"]["Authorization"]
    assert auth == "Bearer tok-addresses:read"
