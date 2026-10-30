from unittest.mock import MagicMock

import pytest

from vccflow.config import Account
from vccflow.media_channels import MediaChannelsClient, MediaChannelsError


def client(*responses):
    tokens = MagicMock()
    tokens.account = Account(name="t", region="emea")
    tokens.token.side_effect = lambda scope: f"tok-{scope}"
    session = MagicMock()
    session.get.side_effect = list(responses)
    return MediaChannelsClient(tokens, session=session), session


def ok(body):
    return MagicMock(status_code=200, json=MagicMock(return_value=body))


def test_destinations_per_channel_with_admin_read():
    c, session = client(ok({"addresses": [{"id": "3233001530"}]}),
                        MagicMock(status_code=403, text='{"title":"feature.notenabled","detail":"Feature not enabled"}'))
    assert c.list_destinations(("pstn", "webchat")) == {"pstn": [{"id": "3233001530"}], "webchat": None}
    first = session.get.call_args_list[0]
    assert first.args[0] == "https://emea.api.cc.vonage.com/media-channels/pstn/addresses"
    assert first.kwargs["headers"]["Authorization"] == "Bearer tok-admin:read"
    assert first.kwargs["headers"]["Accept"] == "application/vnd.newvoicemedia.v2+json"


def test_authorization_failures_are_not_hidden():
    c, _ = client(MagicMock(status_code=403, text='{"title":"Authorization failed."}'))
    with pytest.raises(MediaChannelsError):
        c.list_destinations(("pstn",))
