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


def test_src_layout_scores_the_mutated_copy(tmp_path):
    project = tmp_path / "p"
    (project / "src" / "pkg").mkdir(parents=True)
    (project / "tests").mkdir()
    (project / "src" / "pkg" / "__init__.py").write_text("")
    (project / "src" / "pkg" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (project / "tests" / "test_calc.py").write_text(
        "from pkg.calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    )
    mutants, statuses = measure(project, "src/pkg/calc.py")
    assert len(mutants) == 2  # `a + b` -> `a - b`, and `return ...` -> `return None`
    assert mutation_score(statuses) == 100.0  # 0% would mean the original was imported


def test_tests_option_selects_files(tmp_path):
    project = tmp_path / "p"
    (project / "tests").mkdir(parents=True)
    (project / "m.py").write_text("def f(a):\n    return a + 1\n")
    (project / "tests" / "test_good.py").write_text("from m import f\n\ndef test_f():\n    assert f(1) == 2\n")
    (project / "tests" / "test_broken.py").write_text("def test_broken():\n    assert False\n")
    with pytest.raises(BaselineFailing):
        measure(project, "m.py")  # everything pytest finds, including the broken file
    _, statuses = measure(project, "m.py", tests=["tests/test_good.py"])
    assert mutation_score(statuses) == 100.0


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


def test_non_ascii_test_code_is_saved_as_utf8(project):
    # Windows defaults to a legacy encoding; the file must be written as UTF-8.
    code = STRONG_TESTS + "\n# approx ≈ and é\n"
    llm = FakeLLM([fenced(code)])
    run_loop(project, "pricing.py", llm, max_rounds=1, log=quiet)
    raw = (project / GENERATED_REL).read_bytes()
    assert "≈".encode("utf-8") in raw


def test_one_wrong_test_is_dropped_and_the_rest_kept(project):
    wrong = "\n\ndef test_wrong_expectation():\n    assert apply_discount(100, 0) == 1\n"
    llm = FakeLLM([fenced(STRONG_TESTS + wrong)])
    report = run_loop(project, "pricing.py", llm, max_rounds=1, log=quiet)
    assert report.rounds[1].accepted
    assert "dropped" in report.rounds[1].note
    saved = (project / GENERATED_REL).read_text()
    assert "test_wrong_expectation" not in saved
    assert "test_discount_values" in saved
    assert report.final_score >= 90


def test_prune_gives_up_when_nothing_would_remain():
    from mutagent.loop import failing_test_names, prune_tests

    code = "def test_a():\n    assert False\n"
    assert prune_tests(code, ["test_a"]) is None
    assert prune_tests(code, ["missing"]) is None
    assert failing_test_names("FAILED t.py::TestX::test_a - boom") is None
    assert failing_test_names("FAILED t.py::test_a[1] - boom") == ["test_a"]


class RecordingLLM(FakeLLM):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.prompts: list[tuple[Tier, str]] = []

    def complete(self, tier, messages):
        self.prompts.append((tier, messages[-1]["content"]))
        return super().complete(tier, messages)


def test_baseline_hides_the_surviving_mutants(project):
    llm = RecordingLLM([fenced(STRONG_TESTS)])
    report = run_loop(project, "pricing.py", llm, max_rounds=1, log=quiet, guided=False)
    assert report.mode == "baseline"
    assert report.final_score > report.initial_score
    (_, prompt), = llm.prompts
    assert "Surviving mutants" not in prompt and "### Mutant" not in prompt
    assert "not told which bugs" in prompt


def test_baseline_ignores_tests_left_by_an_earlier_run(project):
    (project / GENERATED_REL).write_text(fenced(STRONG_TESTS).split("```python\n")[1].split("```")[0])
    llm = RecordingLLM([""])
    report = run_loop(project, "pricing.py", llm, max_rounds=1, log=quiet, guided=False)
    # only the project's own weak tests count, not the strong file left behind
    _, own = measure(DEMO, "pricing.py")  # the pristine demo project
    assert report.initial_score == mutation_score(own)
    assert report.initial_score < 60


def test_guided_prompt_shows_the_mutants_and_triage_is_recorded(project):
    _, own = measure(project, "pricing.py")
    first, second = [i for i, status in own.items() if status == "survived"][:2]
    llm = RecordingLLM([fenced(STRONG_TESTS)], triage_reply=f"[{first}, {second}]")
    report = run_loop(project, "pricing.py", llm, max_rounds=1, log=quiet, triage=True)
    assert report.mode == "guided"
    assert report.rounds[1].triaged == sorted([first, second])
    strong_prompt = [p for tier, p in llm.prompts if tier is Tier.STRONG][0]
    assert "### Mutant" in strong_prompt
    assert f"### Mutant {first} " not in strong_prompt
    assert f"### Mutant {second} " not in strong_prompt


def test_syntax_error_gets_one_repair_attempt(project):
    broken = STRONG_TESTS + "\ndef test_broken(:\n    pass\n"
    llm = RecordingLLM([fenced(broken), fenced(STRONG_TESTS)])
    report = run_loop(project, "pricing.py", llm, max_rounds=1, log=quiet)
    assert report.rounds[1].accepted
    assert "after one repair" in report.rounds[1].note
    assert len(llm.prompts) == 2 and "could not be loaded" in llm.prompts[1][1]
    assert report.tokens_used == 200  # the repair call is counted


def test_collection_error_gets_one_repair_attempt(project):
    bad_import = "from pricing_does_not_exist import apply_discount\n\n" + STRONG_TESTS
    llm = RecordingLLM([fenced(bad_import), fenced(STRONG_TESTS)])
    report = run_loop(project, "pricing.py", llm, max_rounds=1, log=quiet)
    assert report.rounds[1].accepted and "after one repair" in report.rounds[1].note


def test_only_one_repair_per_round(project):
    still_broken = "def test_x(:\n    pass\n"
    llm = RecordingLLM([fenced(still_broken), fenced(still_broken)])
    report = run_loop(project, "pricing.py", llm, max_rounds=1, log=quiet)
    assert not report.rounds[1].accepted
    assert len(llm.prompts) == 2


def test_baseline_runs_several_rounds_without_ever_showing_mutants(project):
    llm = RecordingLLM(["no code here", fenced(STRONG_TESTS)])
    report = run_loop(project, "pricing.py", llm, max_rounds=3, log=quiet, guided=False)
    assert report.mode == "baseline" and report.final_score > report.initial_score
    assert all("### Mutant" not in prompt and "Surviving mutants" not in prompt for _, prompt in llm.prompts)


def test_triage_ignores_implausible_verdict():
    from mutagent.loop import triage_equivalent
    from mutagent.mutation import Mutant

    class AllLLM:
        def complete(self, tier, messages):
            return "[1, 2, 3, 4]"

    ms = [Mutant(id=i, lineno=i, kind="compare", diff="d", source="x") for i in (1, 2, 3, 4)]
    assert triage_equivalent(AllLLM(), ms) == set()


def test_explain_survivors_parses_and_filters():
    from mutagent.loop import explain_survivors
    from mutagent.mutation import Mutant

    class L:
        def complete(self, tier, messages):
            return 'Sure: {"1": "No test uses a negative value.", "9": "unknown id", "2": ""}'

    ms = [Mutant(id=i, lineno=i, kind="compare", diff="d", source="x") for i in (1, 2)]
    assert explain_survivors(L(), ms) == {"1": "No test uses a negative value."}


def test_run_loop_stores_explanations(project):
    llm = FakeLLM([fenced(STRONG_TESTS)], triage_reply='{"1": "x"}')
    report = run_loop(project, "pricing.py", llm, max_rounds=1, log=quiet, explain=True)
    assert Tier.FAST in llm.calls and isinstance(report.explanations, dict)


def test_dropped_test_is_repaired_from_the_real_output(project):
    wrong = "\n\ndef test_wrong_expectation():\n    assert apply_discount(100, 0) == 1\n"
    right = "\n\ndef test_wrong_expectation():\n    assert apply_discount(100, 0) == 100\n"
    llm = RecordingLLM([fenced(STRONG_TESTS + wrong), fenced(STRONG_TESTS + right)])
    report = run_loop(project, "pricing.py", llm, max_rounds=1, log=quiet)
    assert report.rounds[1].accepted
    assert "repaired from real output" in report.rounds[1].note
    assert "== 100" in (project / GENERATED_REL).read_text()
    assert "FAILED" in llm.prompts[-1][1] or "assert" in llm.prompts[-1][1]


def test_failed_repair_keeps_the_pruned_file(project):
    wrong = "\n\ndef test_wrong_expectation():\n    assert apply_discount(100, 0) == 1\n"
    llm = FakeLLM([fenced(STRONG_TESTS + wrong), fenced(STRONG_TESTS + wrong)])
    report = run_loop(project, "pricing.py", llm, max_rounds=1, log=quiet)
    assert report.rounds[1].accepted and "dropped" in report.rounds[1].note
    assert "test_wrong_expectation" not in (project / GENERATED_REL).read_text()


def test_no_gain_round_gives_the_model_a_pointed_hint(project):
    useless = "def test_noop():\n    assert True\n"
    llm = RecordingLLM([fenced(useless), fenced(STRONG_TESTS)])
    report = run_loop(project, "pricing.py", llm, max_rounds=2, log=quiet)
    assert not report.rounds[1].accepted and report.rounds[2].accepted
    second_prompt = llm.prompts[1][1]
    assert "did not catch any additional surviving mutant" in second_prompt
    assert "NO_GAIN" not in second_prompt


def test_dropped_tests_become_lessons_for_the_next_round(project):
    wrong = "\n\ndef test_wrong_expectation():\n    assert apply_discount(100, 0) == 1\n"
    llm = RecordingLLM([fenced(STRONG_TESTS + wrong), "", fenced(STRONG_TESTS)])
    run_loop(project, "pricing.py", llm, max_rounds=2, log=quiet)
    strong_prompts = [p for tier, p in llm.prompts if tier is Tier.STRONG]
    assert any("Wrong expectations from the last round" in p for p in strong_prompts[1:])
