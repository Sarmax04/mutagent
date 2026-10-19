import shutil
import sys
from pathlib import Path

import pytest

from fake_contree import FakeContree
from mutagent import sandbox as sandbox_module
from mutagent.cli import main
from mutagent.loop import measure, run_loop
from mutagent.mutation import evaluate, generate_mutants, run_tests
from mutagent.sandbox import SandboxError, SandboxRunner, pytest_command, unavailable_reason
from test_loop import STRONG_TESTS, FakeLLM, fenced

DEMO = Path(__file__).resolve().parents[1] / "examples" / "demo_project"

pytestmark = pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="the fake sandbox needs bash")


@pytest.fixture
def project(tmp_path):
    dest = tmp_path / "demo"
    shutil.copytree(DEMO, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return dest


@pytest.fixture
def runner():
    return SandboxRunner(client=FakeContree(), workers=4, log=lambda _m: None)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(sandbox_module.time, "sleep", lambda _s: None)


def test_pytest_command_quotes_and_limits_time():
    line = pytest_command(["tests/test one.py"], 20, True, ["tests/old.py"])
    assert "rm -f /work/tests/old.py;" in line
    assert "timeout -k 2 20 python -m pytest -x -q -p no:cacheprovider 'tests/test one.py'" in line
    assert "-x" not in pytest_command([], 5, False)


def test_unavailable_reason_names_what_is_missing(monkeypatch):
    monkeypatch.delenv("NEBIUS_PROJECT_ID", raising=False)
    monkeypatch.setenv("NEBIUS_API_KEY", "k")
    reason = unavailable_reason()
    assert reason is None or "NEBIUS_PROJECT_ID" in reason or "contree-sdk" in reason
    with pytest.raises(SandboxError):
        SandboxRunner()


def test_run_tests_matches_the_local_result(project, runner):
    local = run_tests(project, 30, [])
    remote = runner.run_tests(project, 30, [])
    assert remote.passed == local.passed and remote.returncode == local.returncode
    assert "passed" in remote.output


def test_failing_tests_and_timeouts_are_reported(project, runner):
    (project / "tests" / "test_extra.py").write_text(
        "import time\n\ndef test_bad():\n    assert 1 == 2\n\ndef test_slow():\n    time.sleep(30)\n", encoding="utf-8"
    )
    bad = runner.run_tests(project, 30, ["tests/test_extra.py"])
    assert not bad.passed and not bad.timed_out and "assert 1 == 2" in bad.output
    (project / "tests" / "test_extra.py").write_text("import time\n\ndef test_slow():\n    time.sleep(30)\n", encoding="utf-8")
    slow = runner.run_tests(project, 1, ["tests/test_extra.py"])
    assert slow.timed_out and slow.returncode is None


def test_only_changed_files_are_uploaded_and_removed_ones_deleted(project, runner):
    runner.run_tests(project, 30, [])
    client = runner.client
    (project / "tests" / "test_new.py").write_text("def test_a():\n    assert True\n", encoding="utf-8")
    runner.run_tests(project, 30, ["tests/test_new.py"])
    assert client.calls[-1]["files"] == ["/work/tests/test_new.py"]
    (project / "tests" / "test_new.py").unlink()
    (project / "tests" / "test_pricing.py").unlink()  # was in the snapshot, so it must be deleted there
    runner.run_tests(project, 30, [])
    assert "rm -f /work/tests/test_pricing.py;" in client.calls[-1]["shell"]
    assert client.calls[-1]["files"] == []


def test_evaluate_gives_the_same_statuses_as_a_local_run(project, runner):
    source = (project / "pricing.py").read_text(encoding="utf-8")
    mutants = generate_mutants(source)[:12]
    local = evaluate(project, "pricing.py", mutants, source, 30, [])
    remote = runner.evaluate(project, "pricing.py", mutants, source, 30, [])
    assert remote == local
    assert (project / "pricing.py").read_text(encoding="utf-8") == source


def test_measure_with_a_runner(project, runner):
    mutants, statuses = measure(project, "pricing.py", 30, [], runner=runner)
    assert len(statuses) == len(mutants) and 0 < sum(s != "survived" for s in statuses.values()) < len(mutants)


def test_loop_runs_entirely_in_the_sandbox(project, runner):
    report = run_loop(project, "pricing.py", FakeLLM([fenced(STRONG_TESTS)]), max_rounds=1, log=lambda _m: None, runner=runner)
    assert report.final_score >= 90 and report.final_score > report.initial_score
    assert runner.runs > 20


def test_coverage_is_measured_in_the_sandbox(project, runner, monkeypatch):
    monkeypatch.delenv("MUTAGENT_NO_COVERAGE", raising=False)
    covered = runner.coverage(project, "pricing.py", [])
    assert covered is not None and 40 < covered <= 100


def test_a_broken_sandbox_is_an_error_not_a_caught_bug(project):
    runner = SandboxRunner(client=FakeContree(fail=True), log=lambda _m: None)
    with pytest.raises(SandboxError):
        runner.run_tests(project, 30, [])
    with pytest.raises(SandboxError):
        measure(project, "pricing.py", 30, [], runner=runner)


def test_check_reports_a_working_sandbox(runner):
    lines = runner.check()
    assert any("passed" in line for line in lines) and any("caught (good)" in line for line in lines)


def test_cli_sandbox_check_and_score(project, monkeypatch, capsys):
    monkeypatch.setattr("mutagent.cli.SandboxRunner", lambda **kw: SandboxRunner(client=FakeContree(), log=lambda _m: None, **kw))
    assert main(["sandbox-check"]) == 0
    assert "Sandboxes work" in capsys.readouterr().out
    assert main(["score", "--project", str(project), "--target", "pricing.py", "--sandbox"]) == 0
    assert "mutation score:" in capsys.readouterr().out


def test_cli_explains_missing_sandbox_setup(project, monkeypatch, capsys):
    monkeypatch.setattr("mutagent.sandbox.unavailable_reason", lambda: "NEBIUS_PROJECT_ID is not set.")
    assert main(["sandbox-check"]) == 3
    assert "NEBIUS_PROJECT_ID" in capsys.readouterr().err


def test_outputs_match_the_local_runs(project, runner):
    from mutagent.mutation import mutant_outputs

    source = (project / "pricing.py").read_text(encoding="utf-8")
    mutants = generate_mutants(source)[:6]
    local = mutant_outputs(project, "pricing.py", mutants, source, 30, [])
    remote = runner.outputs(project, "pricing.py", mutants, source, 30, [])
    assert {k: (v.passed, v.returncode) for k, v in remote.items()} == {k: (v.passed, v.returncode) for k, v in local.items()}
