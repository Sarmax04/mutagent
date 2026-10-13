import pytest

from mutagent.llm import Tier, TokenBudget
from mutagent.loop import run_loop
from mutagent.safety import screen_test_code


def test_ordinary_test_file_passes():
    code = (
        "import pytest\nfrom math import isclose\nfrom pricing import apply_discount\n"
        "from . import helpers\n\n"
        "def test_a():\n    assert isclose(apply_discount(100, 10), 90)\n"
        "    with pytest.raises(ValueError):\n        apply_discount(1, -1)\n"
        "    assert getattr(helpers, 'value', 1) == 1\n"
    )
    assert screen_test_code(code) == []


@pytest.mark.parametrize(
    "code, fragment",
    [
        ("import os\n", "os"),
        ("import os.path\n", "os"),
        ("import subprocess\n", "subprocess"),
        ("from socket import socket\n", "socket"),
        ("import importlib\n", "importlib"),
        ("import io\n\ndef test_a():\n    io.open('/etc/passwd')\n", "io"),
        ("def test_a():\n    exec('1')\n", "exec"),
        ("def test_a():\n    open('/etc/passwd')\n", "open"),
        ("def test_a():\n    __import__('os')\n", "__import__"),
        ("import pytest\n\ndef test_a():\n    pytest.importorskip('os')\n", "importorskip"),
        ("def test_a():\n    (1).__class__.__base__.__subclasses__()\n", "__subclasses__"),
        ("def test_a():\n    getattr(int, '__subclasses__')()\n", "__subclasses__"),
        ("def test_a(f):\n    f.__globals__\n", "__globals__"),
        ("def test_a():\n    __builtins__\n", "__builtins__"),
        ("def test_a(:\n", "syntax error"),
    ],
)
def test_risky_code_is_rejected_with_a_reason(code, fragment):
    problems = screen_test_code(code)
    assert problems, code
    assert any(fragment in p for p in problems)


class OneReply:
    def __init__(self, reply):
        self.budget = TokenBudget(10**9)
        self.reply = reply

    def complete(self, tier, messages):
        assert tier is Tier.STRONG
        self.budget.charge(50)
        return self.reply


def test_loop_rejects_screened_code_before_running_it(tmp_path):
    project = tmp_path / "p"
    (project / "tests").mkdir(parents=True)
    (project / "m.py").write_text("def inc(a):\n    return a + 1\n")
    (project / "tests" / "test_m.py").write_text("from m import inc\n\ndef test_inc():\n    assert inc(0) > 0\n")
    marker = tmp_path / "ran"
    reply = (
        "```python\nimport os\nfrom m import inc\n\n"
        f"def test_inc_exact():\n    os.mkdir({str(marker)!r})\n    assert inc(1) == 2\n```"
    )
    seen = []
    report = run_loop(
        project, "m.py", OneReply(reply), max_rounds=1, write_back=False,
        log=lambda _: None, code_filter=screen_test_code, on_round=seen.append,
    )
    assert not marker.exists()  # the file never ran
    assert report.rounds[1].note.startswith("rejected: not allowed")
    assert "os" in report.rounds[1].note
    assert [r.round for r in seen] == [0, 1]
    assert seen[0].survivors  # baseline lists the mutants still alive
