from vccflow.cli import build_parser


def test_parser_builds_and_knows_the_list_commands():
    parser = build_parser()
    args = parser.parse_args(["list-destinations", "-a", "JPlana", "--channel", "pstn"])
    assert args.channel == ["pstn"]
    assert parser.parse_args(["list-plans", "-a", "JPlana", "--archived"]).source == "manager"
