import json

from tianyi_electronics_skill.cli import main


def test_advise_is_offline_and_returns_a_read_only_capture_inspection(capsys):
    result = main(["advise", "--request", "请帮我检查已经采集的离线波形"])

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["mode"] == "offline_rule_baseline"
    assert payload["external_call_count"] == 0
    assert payload["recommendations"][0]["action"] == "analyze"
    assert payload["recommendations"][0]["may_write_device"] is False
