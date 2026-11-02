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
