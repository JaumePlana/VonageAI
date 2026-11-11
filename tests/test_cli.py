from vccflow.cli import build_parser


def test_parser_builds_and_knows_the_list_commands():
    parser = build_parser()
    args = parser.parse_args(["list-destinations", "-a", "JPlana", "--channel", "pstn"])
    assert args.channel == ["pstn"]
    assert parser.parse_args(["list-plans", "-a", "JPlana", "--archived"]).source == "manager"


def test_plan_metadata_reads_the_outbound_clid(monkeypatch):
    from unittest.mock import MagicMock

    from vccflow import cli
    from vccflow.ipm import IpmError

    api = MagicMock()
    api.list_plans.return_value = [{"id": "p1", "reportingGroup": "Sales", "outboundClidBehaviour": "InteractionPlanClid",
                                    "outboundClid": "441"}]
    monkeypatch.setattr(cli, "IpmClient", lambda tokens: MagicMock(get_plan=MagicMock(side_effect=IpmError("off", 403))))
    assert cli._plan_metadata(MagicMock(), api, "p1") == {
        "reportingGroupName": "Sales", "displayInAgentInterface": None,
        "outboundClidBehaviour": "InteractionPlanClid", "outboundClid": "441",
    }

    monkeypatch.setattr(cli, "IpmClient", lambda tokens: MagicMock(get_plan=MagicMock(return_value={"displayInAgentInterface": True})))
    assert cli._plan_metadata(MagicMock(), api, "p1")["displayInAgentInterface"] is True


def test_list_data_sources_sorts_and_filters(monkeypatch, capsys):
    import json
    from unittest.mock import MagicMock

    from vccflow import cli

    portal = MagicMock()
    portal.list_data_sources.return_value = {"SF Error Response", "CallGuid", "Salesforce Error Response", "agentid"}
    monkeypatch.setattr(cli, "load_account", lambda name, path: MagicMock())
    monkeypatch.setattr(cli, "PortalClient", lambda account: portal)
    parser = build_parser()

    parser.parse_args(["list-datasources", "-a", "X"]).func(parser.parse_args(["list-datasources", "-a", "X"]))
    assert json.loads(capsys.readouterr().out) == ["agentid", "CallGuid", "Salesforce Error Response", "SF Error Response"]

    args = parser.parse_args(["list-data-sources", "-a", "X", "--like", "ERROR resp", "--plan", "p1"])
    args.func(args)
    assert json.loads(capsys.readouterr().out) == ["Salesforce Error Response", "SF Error Response"]
    portal.list_data_sources.assert_called_with("p1")


def test_list_data_sources_without_a_source_applet(monkeypatch):
    import pytest
    from unittest.mock import MagicMock

    from vccflow import cli

    monkeypatch.setattr(cli, "load_account", lambda name, path: MagicMock())
    monkeypatch.setattr(cli, "PortalClient", lambda account: MagicMock(list_data_sources=MagicMock(return_value=None)))
    args = build_parser().parse_args(["list-data-sources", "-a", "X"])
    with pytest.raises(SystemExit, match="to read the list from"):
        args.func(args)
