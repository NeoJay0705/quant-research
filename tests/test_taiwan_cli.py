import json

from quant_research.taiwan.cli import main
from quant_research.taiwan.storage import SQLiteStore


def test_status_does_not_create_database_and_quotes_reads(tmp_path, capsys):
    root = tmp_path / "data"
    assert main(["--root", str(root), "status"]) == 0
    assert json.loads(capsys.readouterr().out)["initialized"] is False
    SQLiteStore(root).initialize()
    assert main(["--root", str(root), "quotes", "--symbol", "2330", "--source", "finmind"]) == 0
    assert json.loads(capsys.readouterr().out) == []
