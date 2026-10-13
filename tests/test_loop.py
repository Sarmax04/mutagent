import shutil
from pathlib import Path

import pytest

from mutagent.llm import BudgetExceeded, Tier, TokenBudget
from mutagent.loop import (
    BaselineFailing,
    GENERATED_REL,
    extract_code,
    measure,
    module_name,
    run_loop,
    triage_equivalent,
)
from mutagent.mutation import generate_mutants, mutation_score

DEMO = Path(__file__).resolve().parents[1] / "examples" / "demo_project"

STRONG_TESTS = '''\
import pytest
from pricing import apply_discount, is_free_shipping, shipping_cost


def test_discount_values():
    assert apply_discount(100, 10) == 90
    assert apply_discount(200, 0) == 200
    assert apply_discount(200, 100) == 0
    assert apply_discount(80, 25) == 60


def test_discount_bounds():
    with pytest.raises(ValueError):
        apply_discount(100, -1)
    with pytest.raises(ValueError):
        apply_discount(100, 101)


def test_shipping_tiers_and_boundaries():
    assert shipping_cost(0.5) == 4.0
    assert shipping_cost(1) == 4.0
    assert shipping_cost(1.01) == 7.5
    assert shipping_cost(5) == 7.5
    assert shipping_cost(5.01) == 7.51
    assert shipping_cost(10) == 13.5


def test_shipping_express():
    assert shipping_cost(0.5, express=True) == 8.0
    assert shipping_cost(10, express=True) == 27.0


def test_shipping_rejects_non_positive_weight():
    with pytest.raises(ValueError):
        shipping_cost(0)
    with pytest.raises(ValueError):
        shipping_cost(-1)


def test_free_shipping_rules():
    assert is_free_shipping(50, False) is True
    assert is_free_shipping(60, False) is True
    assert is_free_shipping(49.99, False) is False
    assert is_free_shipping(30, False) is False
    assert is_free_shipping(25, True) is True
    assert is_free_shipping(24.99, True) is False
    assert is_free_shipping(100, True) is True
'''


def fenced(code: str) -> str:
    return f"Here you go.\n```python\n{code}```\n"


class FakeLLM:
    """Stands in for Nebius: canned replies, no network."""

    def __init__(self, replies, triage_reply="[]"):
        self.budget = TokenBudget(10**9)
        self.replies = list(replies)
        self.triage_reply = triage_reply
        self.calls: list[Tier] = []

    def complete(self, tier, messages):
        self.calls.append(tier)
        if tier is Tier.FAST:
            self.budget.charge(10)
            return self.triage_reply
        self.budget.charge(100)
        return self.replies.pop(0) if self.replies else ""


@pytest.fixture
def project(tmp_path):
    dest = tmp_path / "demo"
    shutil.copytree(DEMO, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return dest


def quiet(_msg):
    pass


def test_extract_code_and_module_name():
    assert extract_code("no code here") is None
    assert extract_code("```python\nprint(1)\n```") is None  # no test function
    assert extract_code(fenced("def test_a():\n    assert True\n")).startswith("def test_a")
    assert extract_code("<think>```python\ndef test_x(): pass\n```</think>nothing") is None
    assert module_name("pricing.py") == "pricing"
    assert module_name("src/pkg/mod.py") == "pkg.mod"


def test_demo_tests_start_weak(project):
    mutants, statuses = measure(project, "pricing.py")
    assert len(mutants) > 20
    assert 0 < mutation_score(statuses) < 60


def test_loop_raises_score_and_writes_tests_back(project):
    llm = FakeLLM([fenced(STRONG_TESTS)])
    report = run_loop(project, "pricing.py", llm, max_rounds=1, log=quiet)
    assert report.final_score > report.initial_score
    assert report.final_score >= 90  # one equivalent mutant (<= 5 vs < 5) is unkillable
    assert report.rounds[1].accepted
    assert report.generated_path == GENERATED_REL
    assert (project / GENERATED_REL).read_text() == STRONG_TESTS
    assert report.tokens_used == 100
    # the project's original module and tests are untouched
    assert (project / "pricing.py").read_text() == (DEMO / "pricing.py").read_text()


def test_wrong_tests_are_rejected_and_not_written(project):
    bad = fenced("def test_wrong():\n    assert 1 == 2\n")
    report = run_loop(project, "pricing.py", FakeLLM([bad]), max_rounds=1, log=quiet)
    assert report.final_score == report.initial_score
    assert not report.rounds[1].accepted
    assert "fail on the original" in report.rounds[1].note
    assert not (project / GENERATED_REL).exists()
    assert report.generated_path is None


def test_reply_without_code_is_rejected(project):
    report = run_loop(project, "pricing.py", FakeLLM(["sorry, no"]), max_rounds=1, log=quiet)
    assert not report.rounds[1].accepted
    assert "no test code" in report.rounds[1].note


def test_stops_when_budget_is_exhausted(project):
    class Broke(FakeLLM):
        def complete(self, tier, messages):
            raise BudgetExceeded("token budget exhausted (1/1)")

    report = run_loop(project, "pricing.py", Broke([]), max_rounds=3, log=quiet)
    assert len(report.rounds) == 2  # baseline + the stopped round
    assert report.rounds[1].note.startswith("stopped")


def test_baseline_failure_is_reported(tmp_path):
    project = tmp_path / "p"
    (project / "tests").mkdir(parents=True)
    (project / "m.py").write_text("def f():\n    return 1\n")
    (project / "tests" / "test_m.py").write_text("from m import f\n\ndef test_f():\n    assert f() == 2\n")
    with pytest.raises(BaselineFailing):
        run_loop(project, "m.py", FakeLLM([]), log=quiet)


def test_triage_parses_ids_and_ignores_garbage():
    survivors = generate_mutants("def f(a):\n    return a + 1\n")
    ids = {m.id for m in survivors}
    pick = next(iter(ids))
    assert triage_equivalent(FakeLLM([], f"[{pick}, 999]"), survivors) == {pick}
    assert triage_equivalent(FakeLLM([], "I think none"), survivors) == set()
    assert triage_equivalent(FakeLLM([], "[]"), []) == set()


def test_triage_is_optional_and_uses_the_fast_tier(project):
    llm = FakeLLM([fenced(STRONG_TESTS)], triage_reply="[]")
    run_loop(project, "pricing.py", llm, max_rounds=1, triage=True, log=quiet)
    assert llm.calls[0] is Tier.FAST
    assert Tier.STRONG in llm.calls
    plain = FakeLLM([fenced(STRONG_TESTS)])
    run_loop(project, "pricing.py", plain, max_rounds=1, log=quiet)
    assert Tier.FAST not in plain.calls
