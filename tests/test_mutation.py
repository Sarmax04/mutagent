import ast
import shutil
from pathlib import Path

from mutagent.mutation import (
    copy_project,
    counts,
    evaluate,
    generate_mutants,
    mutation_score,
    run_tests,
)

SRC = (
    "def f(a, b):\n"
    "    if a < b and not b == 3:\n"
    "        return a + b + 1\n"
    "    return True\n"
)


def test_generates_every_operator_kind():
    kinds = {m.kind for m in generate_mutants(SRC)}
    assert kinds == {"compare", "arith", "boolop", "not", "const"}


def test_mutants_are_valid_distinct_and_deterministic():
    first = generate_mutants(SRC)
    second = generate_mutants(SRC)
    assert [m.source for m in first] == [m.source for m in second]
    originals = ast.unparse(ast.parse(SRC)) + "\n"
    for m in first:
        ast.parse(m.source)  # must stay valid Python
        assert m.source != originals
        assert m.diff.startswith("--- original")


def test_not_mutation_removes_the_negation():
    mutant = next(m for m in generate_mutants("def f(x):\n    return not x\n") if m.kind == "not")
    namespace: dict = {}
    exec(mutant.source, namespace)
    assert namespace["f"](True) is True  # original returns False


def test_score_counts_timeouts_as_killed():
    statuses = {0: "killed", 1: "timeout", 2: "survived", 3: "survived"}
    assert counts(statuses) == (2, 2)
    assert mutation_score(statuses) == 50.0
    assert mutation_score({}) == 100.0


def _project(tmp_path: Path, test_body: str) -> Path:
    project = tmp_path / "p"
    (project / "tests").mkdir(parents=True)
    (project / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (project / "tests" / "test_calc.py").write_text(test_body)
    return project


def test_evaluate_separates_strong_and_weak_tests(tmp_path):
    weak = _project(tmp_path / "weak", "from calc import add\n\ndef test_zero():\n    assert add(0, 0) == 0\n")
    strong = _project(tmp_path / "strong", "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    scores = {}
    for name, project in (("weak", weak), ("strong", strong)):
        work = copy_project(project)
        try:
            source = (work / "calc.py").read_text()
            mutants = generate_mutants(source)
            assert run_tests(work).passed
            scores[name] = mutation_score(evaluate(work, "calc.py", mutants, source))
            assert (work / "calc.py").read_text() == source  # always restored
        finally:
            shutil.rmtree(work.parent, ignore_errors=True)
    assert scores["strong"] > scores["weak"]
    assert scores["strong"] == 100.0


def test_infinite_loop_mutant_is_a_timeout(tmp_path):
    project = tmp_path / "p"
    (project / "tests").mkdir(parents=True)
    (project / "loop.py").write_text(
        "def count(n):\n    i = 0\n    while i < n:\n        i = i + 1\n    return i\n"
    )
    (project / "tests" / "test_loop.py").write_text(
        "from loop import count\n\ndef test_count():\n    assert count(3) == 3\n"
    )
    work = copy_project(project)
    try:
        source = (work / "loop.py").read_text()
        mutants = generate_mutants(source)
        statuses = evaluate(work, "loop.py", mutants, source, timeout=3.0)
        assert "timeout" in statuses.values()
    finally:
        shutil.rmtree(work.parent, ignore_errors=True)
