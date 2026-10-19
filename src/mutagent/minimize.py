"""Trim the generated test file to the tests that matter.

The agent keeps whole rounds of tests as long as the score rises, so the final file
often contains tests that catch nothing the others don't already catch. This step

1. runs the generated tests alone against every planted bug that only they catch,
   and records which tests fail on which bug,
2. keeps the smallest set of tests that still catches all of those bugs (greedy set
   cover, in file order on ties), and
3. checks the trimmed file: it must pass on the real code and reach the same
   mutation score. Otherwise the full file is kept.

It costs test runs but no model tokens.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from .mutation import Mutant, mutation_score


@dataclass
class Trim:
    code: str
    before: int
    after: int
    note: str


def test_names(code: str) -> list[str]:
    """Test names in file order: ``test_x`` for functions, ``TestClass.test_x`` for methods."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            names.append(node.name)
        elif isinstance(node, ast.ClassDef):
            names += [
                f"{node.name}.{b.name}"
                for b in node.body
                if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef)) and b.name.startswith("test")
            ]
    return names


def cover(names: Sequence[str], kills: dict[int, set[str]]) -> list[str]:
    """Smallest (greedy) list of ``names`` that together fail on every mutant in ``kills``."""
    left = {mid: hit for mid, hit in kills.items() if hit}
    keep: list[str] = []
    while left:
        best = max(names, key=lambda n: (sum(n in hit for hit in left.values()), -names.index(n)))
        if not any(best in hit for hit in left.values()):
            break
        keep.append(best)
        left = {mid: hit for mid, hit in left.items() if best not in hit}
    return [n for n in names if n in keep]


def trim_tests(
    *,
    work: Path,
    target: str,
    source: str,
    mutants: list[Mutant],
    code: str,
    generated_rel: str,
    initial: dict[int, str],
    final: dict[int, str],
    chosen: Sequence[str],
    timeout: float,
    outputs: Callable,
    run_tests: Callable,
    evaluate: Callable,
    failing_tests: Callable,
    prune_tests: Callable,
    log: Callable[[str], None],
) -> Trim:
    names = test_names(code)
    if len(names) < 2:
        return Trim(code, len(names), len(names), "nothing to trim")
    needed = [m for m in mutants if final.get(m.id) != "survived" and initial.get(m.id) == "survived"]
    if not needed:
        return Trim(code, len(names), len(names), "the new tests catch nothing on their own")
    generated = Path(work) / generated_rel
    generated.write_text(code, encoding="utf-8")
    runs = outputs(work, target, needed, source, timeout, [generated_rel])
    kills: dict[int, set[str]] = {}
    for mutant in needed:
        run = runs[mutant.id]
        if run.timed_out:
            continue  # cannot tell which test caught it; the final check below covers this case
        failed, _reason = failing_tests(run.output, generated_rel)
        if failed:
            kills[mutant.id] = {n for n in failed if n in names}
    keep = cover(names, kills)
    if not keep or len(keep) == len(names):
        return Trim(code, len(names), len(names), "every test catches something the others miss")
    trimmed = prune_tests(code, [n for n in names if n not in keep])
    if trimmed is None:
        return Trim(code, len(names), len(names), "could not remove the redundant tests")
    generated.write_text(trimmed, encoding="utf-8")
    check = run_tests(work, timeout, chosen)
    statuses = evaluate(work, target, mutants, source, timeout, chosen) if check.passed else {}
    if not check.passed or mutation_score(statuses) < mutation_score(final):
        generated.write_text(code, encoding="utf-8")
        return Trim(code, len(names), len(names), "the trimmed file scored lower, so the full file is kept")
    log(f"trimmed the new tests from {len(names)} to {len(keep)}: the others caught nothing extra")
    return Trim(trimmed, len(names), len(keep), "trimmed")
