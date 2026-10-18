import json
import shutil
from pathlib import Path

import pytest

from mutagent.cli import main
from mutagent.linecov import measure_coverage, percent_for, project_coverage
from mutagent.loop import run_loop
from mutagent.report import render_markdown
from mutagent.scan import ScanRow, format_table, scan

from test_loop import STRONG_TESTS, FakeLLM, fenced

DEMO = Path(__file__).resolve().parents[1] / "examples" / "demo_project"


@pytest.fixture(autouse=True)
def coverage_on(monkeypatch):
    monkeypatch.delenv("MUTAGENT_NO_COVERAGE", raising=False)


@pytest.fixture
def project(tmp_path):
    dest = tmp_path / "demo"
    shutil.copytree(DEMO, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return dest


def test_percent_for_picks_the_target_file(tmp_path):
    target = tmp_path / "pkg" / "m.py"
    target.parent.mkdir()
    target.write_text("x = 1\n", encoding="utf-8")
    report = {
        "cwd": str(tmp_path),
        "files": {
            "pkg/other.py": {"summary": {"num_statements": 4, "covered_lines": 4}},
            "pkg/m.py": {"summary": {"num_statements": 8, "covered_lines": 6}},
        },
    }
    assert percent_for(report, target) == 75.0
    assert percent_for(report, tmp_path / "missing.py") is None
    empty = {"cwd": str(tmp_path), "files": {"pkg/m.py": {"summary": {"num_statements": 0, "covered_lines": 0}}}}
    assert percent_for(empty, target) is None


def test_weak_tests_cover_less_than_everything(project):
    covered = project_coverage(project, "pricing.py")
    assert covered is not None and 40 < covered < 100


def test_switch_turns_measuring_off(project, monkeypatch):
    monkeypatch.setenv("MUTAGENT_NO_COVERAGE", "1")
    assert project_coverage(project, "pricing.py") is None


def test_untouched_module_has_no_figure(project, tmp_path):
    (project / "lonely.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    assert project_coverage(project, "lonely.py") is None
    assert measure_coverage(project, "nothing/here.py") is None


def test_loop_reports_coverage_before_and_after(project):
    report = run_loop(project, "pricing.py", FakeLLM([fenced(STRONG_TESTS)]), max_rounds=1, log=lambda _m: None)
    assert report.initial_coverage is not None and report.final_coverage is not None
    assert report.final_coverage > report.initial_coverage
    assert report.to_dict()["final_coverage"] == report.final_coverage


def test_loop_without_gain_keeps_the_same_coverage(project):
    report = run_loop(project, "pricing.py", FakeLLM([""]), max_rounds=1, log=lambda _m: None)
    assert report.final_coverage == report.initial_coverage


def test_scan_table_has_a_coverage_column(project):
    rows = scan(project, ["pricing.py"], log=lambda _m: None)
    assert rows[0].coverage is not None
    table = format_table(rows)
    assert "coverage" in table.splitlines()[0]
    assert f"{rows[0].coverage:5.1f}%" in table
    assert "-" in format_table([ScanRow("a.py", 0, None, note="x")]).splitlines()[1]


def test_score_command_prints_coverage(project, capsys):
    assert main(["score", "--project", str(project), "--target", "pricing.py"]) == 0
    out = capsys.readouterr().out
    assert "mutation score:" in out and "line coverage:" in out


def test_report_shows_coverage_only_when_known():
    base = {"target": "m.py", "mutant_count": 1, "initial_score": 10.0, "final_score": 20.0, "tokens_used": 1, "rounds": []}
    assert "Line coverage" not in render_markdown(base)
    text = render_markdown({**base, "initial_coverage": 64.7, "final_coverage": 100.0})
    assert "Line coverage: 64.7% -> 100.0%" in text
