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
    assert kinds == {"compare", "arith", "boolop", "not", "const", "negate", "return"}  # no statements to remove


def _mutated_behaviour(source: str, kind: str, call):
    """Run `call(namespace)` against every mutant of the given kind."""
    results = []
    for mutant in generate_mutants(source):
        if mutant.kind == kind:
            namespace: dict = {}
            exec(mutant.source, namespace)
            results.append(call(namespace))
    return results


def test_membership_identity_and_augassign_are_mutated():
    assert _mutated_behaviour(
        "def f(x, xs):\n    return x in xs\n", "compare", lambda ns: ns["f"](1, [1])
    ) == [False]
    assert _mutated_behaviour(
        "def f(x):\n    return x is None\n", "compare", lambda ns: ns["f"](None)
    ) == [False]

    def total(ns):
        return ns["f"](5)

    assert _mutated_behaviour("def f(x):\n    x += 2\n    return x\n", "arith", total) == [3]


def test_negate_and_return_mutations_change_behaviour():
    src = "def f(x):\n    if x:\n        return 1\n    return 2\n"
    assert _mutated_behaviour(src, "negate", lambda ns: ns["f"](True)) == [2]
    returns = _mutated_behaviour(src, "return", lambda ns: (ns["f"](True), ns["f"](False)))
    assert sorted(map(str, returns)) == ["(1, None)", "(None, 2)"]


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


STATEMENTS = (
    "def f(xs, n):\n"
    "    out = []\n"
    "    for x in xs:\n"
    "        if x < 0:\n"
    "            continue\n"
    "        if x > 100:\n"
    "            break\n"
    "        out.append(-x)\n"
    "    if n < 0:\n"
    "        raise ValueError('n')\n"
    "    return [y for y in out if y != n]\n"
)


def test_statement_operators_inside_functions():
    kinds = {m.kind for m in generate_mutants(STATEMENTS)}
    assert {"call", "raise", "loop", "sign", "assign", "negate"} <= kinds

    def run(n):
        def call(ns):
            try:
                return ns["f"]([-2, 1, 200, 3], n)
            except Exception as exc:  # noqa: BLE001
                return type(exc).__name__
        return call

    assert run(-5)(_exec(STATEMENTS)) == "ValueError"
    assert run(0)(_exec(STATEMENTS)) == [-1]
    assert _mutated_behaviour(STATEMENTS, "raise", run(-5)) == [[-1]]
    assert _mutated_behaviour(STATEMENTS, "call", run(0)) == [[]]
    assert _mutated_behaviour(STATEMENTS, "sign", run(0)) == [[1]]
    assert sorted(map(str, _mutated_behaviour(STATEMENTS, "loop", run(0)))) == ["[-1, -3]", "[]"]
    assert "AttributeError" in _mutated_behaviour(STATEMENTS, "assign", run(0))


def _exec(source):
    namespace: dict = {}
    exec(source, namespace)
    return namespace


def test_comprehension_filters_are_negated():
    comprehension = next(m for m in generate_mutants(STATEMENTS) if m.kind == "negate" and "if not y != n" in m.diff)
    assert comprehension.lineno == 11
    assert _exec(comprehension.source)["f"]([1], 0) == []


def test_module_level_statements_are_left_alone():
    src = "import os\nTABLE = dict(a=1)\nos.getcwd()\n\ndef f():\n    return TABLE['a']\n"
    kinds = [m.kind for m in generate_mutants(src)]
    assert "assign" not in kinds and "call" not in kinds


def test_mutants_can_be_limited_to_some_lines():
    every = generate_mutants(STATEMENTS)
    some = generate_mutants(STATEMENTS, lines={10, 11})
    assert some and len(some) < len(every)
    assert {m.lineno for m in some} <= {10, 11}
    assert [m.id for m in some] == list(range(len(some)))
