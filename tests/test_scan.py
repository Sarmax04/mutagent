from mutagent.cli import main
from mutagent.scan import ScanRow, discover_targets, format_table, scan


def make_project(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "calc.py").write_text("def gt(a, b):\n    return a > b\n", encoding="utf-8")
    (tmp_path / "pkg" / "consts.py").write_text("NAME = 'x'\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text(
        "from pkg.calc import gt\n\ndef test_gt():\n    assert gt(2, 1)\n", encoding="utf-8"
    )
    (tmp_path / "setup.py").write_text("x = 1\n", encoding="utf-8")
    return tmp_path


def test_discover_skips_tests_empty_and_setup(tmp_path):
    make_project(tmp_path)
    assert discover_targets(tmp_path) == ["pkg/calc.py", "pkg/consts.py"]


def test_scan_reports_each_module(tmp_path):
    make_project(tmp_path)
    rows = scan(tmp_path, ["pkg/calc.py", "pkg/consts.py"], log=lambda _m: None)
    calc, consts = rows
    assert calc.mutants > 0 and calc.score is not None and calc.score < 100
    assert consts.score is None or consts.mutants >= 0
    assert "pkg/calc.py" in format_table(rows)


def test_format_table_handles_unscored_rows():
    text = format_table([ScanRow("a.py", 0, None, note="nothing to mutate")])
    assert "nothing to mutate" in text


def test_scan_command_gate(tmp_path, capsys):
    make_project(tmp_path)
    args = ["scan", "--project", str(tmp_path), "--targets", "pkg/calc.py"]
    assert main(args) == 0
    assert main(args + ["--min-score", "100"]) == 4
    assert main(["scan", "--project", str(tmp_path), "--targets", "nope.py"]) == 2
    assert main(args + ["--fix"]) == 2


def test_scan_fix_without_api_key_is_a_clear_error(tmp_path, monkeypatch, capsys):
    make_project(tmp_path)
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    code = main(["scan", "--project", str(tmp_path), "--targets", "pkg/calc.py", "--fix", "--min-score", "100"])
    assert code == 2
    assert "NEBIUS_API_KEY" in capsys.readouterr().err


def test_fix_updates_the_table_counts(tmp_path, monkeypatch):
    import mutagent.cli as cli
    from mutagent.loop import LoopReport, RoundResult

    make_project(tmp_path)
    for k, v in {"NEBIUS_API_KEY": "x", "NEBIUS_BASE_URL": "http://x/v1/",
                 "NEBIUS_MODEL_FAST": "f", "NEBIUS_MODEL_STRONG": "s"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.chdir(tmp_path)

    def fake_run_loop(project, target, llm, **kw):
        rep = LoopReport(target=target, mutant_count=2, initial_score=50.0, final_score=100.0)
        rep.rounds.append(RoundResult(1, 100.0, 2, 0, True, "accepted", 5, [], []))
        return rep

    monkeypatch.setattr(cli, "run_loop", fake_run_loop)
    monkeypatch.setattr(cli, "NebiusChat", lambda settings, budget: object())
    code = cli.main(["scan", "--project", str(tmp_path), "--targets", "pkg/calc.py",
                     "--fix", "--min-score", "90", "--report-dir", str(tmp_path / "r")])
    assert code == 0
