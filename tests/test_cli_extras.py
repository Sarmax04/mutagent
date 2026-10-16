import json
from pathlib import Path

from mutagent.cli import main
from mutagent.report import render_markdown

DATA = {
    "target": "m.py", "mutant_count": 3, "initial_score": 33.3, "final_score": 66.7,
    "tokens_used": 100, "mode": "guided", "generated_code": "def test_a():\n    assert True",
    "mutants": [{"id": 1, "line": 4, "kind": "compare"}, {"id": 2, "line": 5, "kind": "arith"}],
    "rounds": [{"round": 0, "score": 33.3, "killed": 1, "survived": 2, "accepted": True,
                "note": "baseline", "tokens_used": 0, "survivors": [1, 2], "triaged": []}],
}


def test_markdown_report_lists_survivors_and_code():
    text = render_markdown(DATA)
    assert "33.3% -> 66.7%" in text and "| #1 | 4 | compare |" in text and "def test_a" in text


def test_report_command_writes_file(tmp_path):
    src = tmp_path / "r.json"
    src.write_text(json.dumps(DATA), encoding="utf-8")
    out = tmp_path / "r.md"
    assert main(["report", str(src), "--out", str(out)]) == 0
    assert "mutagent run" in out.read_text(encoding="utf-8")


def test_min_score_gate(tmp_path):
    (tmp_path / "m.py").write_text("def f(a, b):\n    return a < b\n", encoding="utf-8")
    (tmp_path / "test_m.py").write_text("from m import f\n\ndef test_f():\n    assert f(1, 2)\n", encoding="utf-8")
    args = ["score", "--project", str(tmp_path), "--target", "m.py"]
    assert main(args + ["--min-score", "1"]) == 0
    assert main(args + ["--min-score", "100"]) == 4
