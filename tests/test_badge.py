import json
import xml.etree.ElementTree as ET

from mutagent.badge import color_for, render_badge
from mutagent.cli import main


def test_badge_is_valid_svg_with_the_score():
    svg = render_badge(87.5)
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg")
    assert "87.5%" in svg and "mutation score" in svg
    assert "unknown" in render_badge(None)
    assert "&lt;b&gt;" in render_badge(50, "<b>")


def test_badge_colors_follow_the_score():
    assert color_for(95) != color_for(80) != color_for(65) != color_for(10)
    assert color_for(90) == color_for(100)


def test_badge_command_from_score_and_report(tmp_path):
    out = tmp_path / "b.svg"
    assert main(["badge", "--score", "72", "--out", str(out)]) == 0
    assert "72.0%" in out.read_text(encoding="utf-8")
    report = tmp_path / "r.json"
    report.write_text(json.dumps({"final_score": 96.25}), encoding="utf-8")
    assert main(["badge", "--report", str(report), "--out", str(out)]) == 0
    assert "96.2%" in out.read_text(encoding="utf-8") or "96.3%" in out.read_text(encoding="utf-8")
    report.write_text("{}", encoding="utf-8")
    assert main(["badge", "--report", str(report), "--out", str(out)]) == 2


def test_score_writes_a_badge(tmp_path):
    (tmp_path / "m.py").write_text("def gt(a, b):\n    return a > b\n", encoding="utf-8")
    (tmp_path / "test_m.py").write_text("from m import gt\n\ndef test_gt():\n    assert gt(2, 1)\n", encoding="utf-8")
    out = tmp_path / "badge.svg"
    assert main(["score", "--project", str(tmp_path), "--target", "m.py", "--badge", str(out)]) == 0
    assert "%" in out.read_text(encoding="utf-8")
