import json
import threading
import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from mutagent import web  # noqa: E402
from mutagent.llm import TokenBudget  # noqa: E402
from mutagent.presets import PRESETS, Preset, project_path  # noqa: E402
from mutagent.web import Limits, create_app  # noqa: E402

TINY = Preset(
    id="tiny",
    title="Tiny",
    blurb="One function, one weak test.",
    project="tiny",
    target="m.py",
    tests=("tests/test_m.py",),
)
GOOD_REPLY = "```python\nfrom m import inc\n\n\ndef test_inc_exact():\n    assert inc(1) == 2\n```"


class ScriptedModel:
    def __init__(self, reply=GOOD_REPLY, gate=None):
        self.budget = TokenBudget(10**9)
        self.reply = reply
        self.gate = gate

    def complete(self, tier, messages):
        if self.gate is not None:
            self.gate.wait(30)
        self.budget.charge(123)
        return self.reply


@pytest.fixture
def tiny(tmp_path, monkeypatch):
    """A one-function project served as the only preset."""
    examples = tmp_path / "examples"
    project = examples / "tiny"
    (project / "tests").mkdir(parents=True)
    (project / "m.py").write_text("def inc(a):\n    return a + 1\n")
    (project / "tests" / "test_m.py").write_text("from m import inc\n\ndef test_inc():\n    assert inc(0) > 0\n")
    monkeypatch.setenv("MUTAGENT_EXAMPLES_DIR", str(examples))
    monkeypatch.setenv("MUTAGENT_REPLAYS_DIR", str(tmp_path / "replays"))
    monkeypatch.setattr(web, "PRESETS", (TINY,))
    monkeypatch.setattr(web, "get_preset", lambda pid: TINY if pid == "tiny" else None)
    return tmp_path


def make_client(factory=None, **limits):
    app = create_app(
        llm_factory=factory,
        limits=Limits(**limits),
        models={"fast": "fast-model", "strong": "strong-model"},
        auto_configure=False,
    )
    return TestClient(app)


def wait_done(client, run_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] in ("done", "error"):
            return run
        time.sleep(0.2)
    raise AssertionError("run did not finish")


def test_real_presets_point_at_existing_files():
    for preset in PRESETS:
        root = project_path(preset)
        assert (root / preset.target).is_file(), preset.id
        for test_file in preset.tests:
            assert (root / test_file).is_file(), (preset.id, test_file)


def test_index_page_is_served():
    response = make_client().get("/")
    assert response.status_code == 200
    assert "<title>MutAgent</title>" in response.text


def test_status_reports_live_off_without_a_model():
    status = make_client().get("/api/status").json()
    assert status["live"] is False


def test_live_run_end_to_end(tiny):
    client = make_client(lambda: ScriptedModel(), max_rounds=2)
    assert client.get("/api/status").json()["live"] is True
    started = client.post("/api/runs", json={"preset": "tiny", "rounds": 5})
    assert started.status_code == 202
    run = wait_done(client, started.json()["id"])
    assert run["status"] == "done", run
    assert run["rounds_requested"] == 2  # clamped to the server limit
    scores = [r["score"] for r in run["rounds"]]
    assert scores[0] == pytest.approx(66.67, abs=0.1) and scores[-1] == 100.0
    assert run["rounds"][-1]["survivors"] == []
    assert "def test_inc_exact" in run["report"]["generated_code"]
    assert run["report"]["mutants"] and run["log"]
    # the preset's own files are never modified
    assert not (project_path(TINY) / "tests" / "test_mutagent_generated.py").exists()


def test_screened_code_is_rejected_in_a_live_run(tiny):
    reply = "```python\nimport os\nfrom m import inc\n\ndef test_x():\n    assert inc(1) == 2\n```"
    client = make_client(lambda: ScriptedModel(reply), max_rounds=1)
    run = wait_done(client, client.post("/api/runs", json={"preset": "tiny"}).json()["id"])
    assert run["rounds"][-1]["note"].startswith("rejected: not allowed")
    assert run["rounds"][-1]["score"] == pytest.approx(66.67, abs=0.1)


def test_live_off_returns_503(tiny):
    response = make_client().post("/api/runs", json={"preset": "tiny"})
    assert response.status_code == 503


def test_unknown_preset_returns_404(tiny):
    client = make_client(lambda: ScriptedModel())
    assert client.post("/api/runs", json={"preset": "nope"}).status_code == 404
    assert client.get("/api/replay/nope").status_code == 404
    assert client.get("/api/runs/doesnotexist").status_code == 404


def test_one_run_at_a_time_then_daily_cap(tiny):
    gate = threading.Event()
    client = make_client(lambda: ScriptedModel(gate=gate), max_rounds=1, max_runs_per_day=2)
    first = client.post("/api/runs", json={"preset": "tiny"})
    assert first.status_code == 202
    busy = client.post("/api/runs", json={"preset": "tiny"})
    assert busy.status_code == 429 and "in progress" in busy.json()["detail"]
    gate.set()
    wait_done(client, first.json()["id"])
    second = client.post("/api/runs", json={"preset": "tiny"})
    assert second.status_code == 202
    wait_done(client, second.json()["id"])
    capped = client.post("/api/runs", json={"preset": "tiny"})
    assert capped.status_code == 429 and "daily limit" in capped.json()["detail"]
    assert client.get("/api/status").json()["runs_left_today"] == 0


def test_failing_model_is_reported_not_crashing(tiny):
    class Broken(ScriptedModel):
        def complete(self, tier, messages):
            raise ConnectionError("network down")

    client = make_client(lambda: Broken(), max_rounds=1)
    run = wait_done(client, client.post("/api/runs", json={"preset": "tiny"}).json()["id"])
    assert run["status"] == "error"
    assert "ConnectionError" in run["error"]
    # the slot is free again
    assert client.get("/api/status").json()["busy"] is False


def test_replay_is_served_when_a_recording_exists(tiny):
    client = make_client()
    assert client.get("/api/presets").json()[0]["has_replay"] is False
    assert client.get("/api/replay/tiny").status_code == 404
    recording = {"target": "m.py", "rounds": [{"round": 0, "score": 50.0}], "models": {"strong": "x"}}
    (tiny / "replays").mkdir()
    (tiny / "replays" / "tiny.json").write_text(json.dumps(recording))
    assert client.get("/api/presets").json()[0]["has_replay"] is True
    assert client.get("/api/replay/tiny").json()["report"] == recording
    (tiny / "replays" / "tiny.json").write_text("{not json")
    assert client.get("/api/replay/tiny").status_code == 500


def test_recording_only_presets_refuse_live_runs(tiny, monkeypatch):
    from dataclasses import replace

    slow = replace(TINY, live=False)
    monkeypatch.setattr(web, "get_preset", lambda pid: slow if pid == "tiny" else None)
    monkeypatch.setattr(web, "PRESETS", (slow,))
    client = make_client(factory=lambda: ScriptedModel("x"))
    listed = client.get("/api/presets").json()
    assert listed[0]["live"] is False
    assert client.post("/api/runs", json={"preset": "tiny"}).status_code == 503


def test_real_presets_pass_their_own_tests():
    from mutagent.mutation import run_tests, copy_project
    import shutil

    for preset in PRESETS:
        work = copy_project(project_path(preset))
        try:
            result = run_tests(work, 60, preset.tests)
            assert result.passed, (preset.id, result.output)
        finally:
            shutil.rmtree(work.parent, ignore_errors=True)
