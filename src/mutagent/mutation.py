"""A small AST-based mutation engine and test runner.

A *mutant* is the module under test with one deliberate bug (a flipped
comparison, a swapped operator, ...). A test suite *kills* a mutant if it fails
on the buggy code. The mutation score is

    score = killed / (killed + survived) * 100

Timeouts count as killed (the mutant broke the program, usually an infinite
loop). Some survivors are *equivalent mutants* that change no observable
behaviour and can never be killed, so 100% is not always reachable.

This engine is deliberately minimal. Swap it for mutmut or Stryker if you need
more operators; the rest of the agent only needs ``Mutant`` objects.
"""

from __future__ import annotations

import ast
import copy
import difflib
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

_CMP = {
    ast.Lt: ast.LtE,
    ast.LtE: ast.Lt,
    ast.Gt: ast.GtE,
    ast.GtE: ast.Gt,
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
}
_BIN = {
    ast.Add: ast.Sub,
    ast.Sub: ast.Add,
    ast.Mult: ast.Div,
    ast.Div: ast.Mult,
    ast.FloorDiv: ast.Mult,
    ast.Mod: ast.Mult,
}
_BOOL = {ast.And: ast.Or, ast.Or: ast.And}

KILLED_STATES = {"killed", "timeout"}

Site = tuple[ast.AST, str, int]


@dataclass(frozen=True)
class Mutant:
    id: int
    lineno: int
    kind: str  # compare | arith | boolop | not | const
    diff: str  # unified diff against the normalised original
    source: str  # full mutated module source


def _sites(tree: ast.AST) -> list[Site]:
    """All places where a mutation can be applied, in a deterministic order."""
    out: list[Site] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for i, op in enumerate(node.ops):
                if type(op) in _CMP:
                    out.append((node, "compare", i))
        elif isinstance(node, ast.BinOp) and type(node.op) in _BIN:
            out.append((node, "arith", 0))
        elif isinstance(node, ast.BoolOp) and type(node.op) in _BOOL:
            out.append((node, "boolop", 0))
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            out.append((node, "not", 0))
        elif isinstance(node, ast.Constant) and isinstance(node.value, (bool, int)):
            out.append((node, "const", 0))
    return out


def _apply(site: Site) -> None:
    node, kind, idx = site
    if kind == "compare":
        node.ops[idx] = _CMP[type(node.ops[idx])]()
    elif kind == "arith":
        node.op = _BIN[type(node.op)]()
    elif kind == "boolop":
        node.op = _BOOL[type(node.op)]()
    elif kind == "not":
        # `not x` -> `not not x`, which has the truthiness of `x` (negation removed)
        node.operand = ast.UnaryOp(op=ast.Not(), operand=node.operand)
    elif kind == "const":
        if isinstance(node.value, bool):
            node.value = not node.value
        else:
            node.value = node.value + 1


def generate_mutants(source: str) -> list[Mutant]:
    """One mutant per mutation site. Diffs are taken between normalised sources."""
    tree = ast.parse(source)
    base_lines = ast.unparse(tree).splitlines()
    mutants: list[Mutant] = []
    for i in range(len(_sites(tree))):
        mutated_tree = copy.deepcopy(tree)
        site = _sites(mutated_tree)[i]
        _apply(site)
        ast.fix_missing_locations(mutated_tree)
        mutated = ast.unparse(mutated_tree)
        diff = "\n".join(
            difflib.unified_diff(
                base_lines, mutated.splitlines(), "original", "mutant", lineterm="", n=2
            )
        )
        mutants.append(
            Mutant(
                id=i,
                lineno=getattr(site[0], "lineno", 0),
                kind=site[1],
                diff=diff,
                source=mutated + "\n",
            )
        )
    return mutants


@dataclass
class TestRun:
    __test__ = False  # not a pytest test class

    passed: bool
    timed_out: bool
    returncode: int | None
    output: str


def copy_project(project: Path) -> Path:
    """Copy a project into a temp dir; the caller removes ``result.parent``."""
    work = Path(tempfile.mkdtemp(prefix="mutagent-")) / "proj"
    shutil.copytree(
        project,
        work,
        ignore=shutil.ignore_patterns(
            "__pycache__", "*.pyc", ".git", ".venv", ".pytest_cache", "*.egg-info"
        ),
    )
    return work


# With MUTAGENT_LIMITS=1 (POSIX only) pytest starts under CPU, memory and file-size
# limits. Set before pytest starts instead of via preexec_fn, which is unsafe when the
# parent process has threads (for example a web server).
_LIMITED_BOOTSTRAP = (
    "import resource, runpy, sys\n"
    "for name, value in (('RLIMIT_CPU', 60), ('RLIMIT_AS', 2 << 30), ('RLIMIT_FSIZE', 10 << 20)):\n"
    "    resource.setrlimit(getattr(resource, name), (value, value))\n"
    "sys.argv = ['pytest'] + sys.argv[1:]\n"
    "runpy.run_module('pytest', run_name='__main__', alter_sys=True)\n"
)


def _pytest_argv(tests: Sequence[str], fail_fast: bool = True) -> list[str]:
    args = [*(["-x"] if fail_fast else []), "-q", "-p", "no:cacheprovider", *tests]
    if os.name == "posix" and os.environ.get("MUTAGENT_LIMITS") == "1":
        return [sys.executable, "-c", _LIMITED_BOOTSTRAP, *args]
    return [sys.executable, "-m", "pytest", *args]


def run_tests(
    workdir: Path,
    timeout: float = 30.0,
    tests: Sequence[str] = (),
    fail_fast: bool = True,
) -> TestRun:
    """Run pytest in ``workdir``, optionally only the given test files.

    ``workdir`` and ``workdir/src`` go first on ``PYTHONPATH`` so the *mutated copy*
    is what gets imported (flat and ``src/`` layouts), never an installed copy.
    Bytecode is disabled so edits are never stale.
    """
    paths = [str(workdir), str(workdir / "src")]
    if os.environ.get("PYTHONPATH"):
        paths.append(os.environ["PYTHONPATH"])
    env = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": os.pathsep.join(paths),
    }
    try:
        proc = subprocess.run(
            _pytest_argv(tests, fail_fast),
            cwd=workdir,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return TestRun(passed=False, timed_out=True, returncode=None, output="timeout")
    output = (proc.stdout + proc.stderr)[-4000:]
    return TestRun(
        passed=proc.returncode == 0,
        timed_out=False,
        returncode=proc.returncode,
        output=output,
    )


def evaluate(
    work: Path,
    target: str,
    mutants: list[Mutant],
    original_source: str,
    timeout: float = 20.0,
    tests: Sequence[str] = (),
) -> dict[int, str]:
    """Run the suite against every mutant. Returns id -> killed|survived|timeout."""
    target_path = work / target
    statuses: dict[int, str] = {}
    try:
        for mutant in mutants:
            target_path.write_text(mutant.source, encoding="utf-8")
            result = run_tests(work, timeout, tests)
            if result.timed_out:
                statuses[mutant.id] = "timeout"
            elif result.passed or result.returncode == 5:  # 5 = no tests collected
                statuses[mutant.id] = "survived"
            else:
                statuses[mutant.id] = "killed"
    finally:
        target_path.write_text(original_source, encoding="utf-8")
    return statuses


def counts(statuses: dict[int, str]) -> tuple[int, int]:
    """(killed, survived); timeouts count as killed."""
    killed = sum(1 for s in statuses.values() if s in KILLED_STATES)
    survived = sum(1 for s in statuses.values() if s == "survived")
    return killed, survived


def mutation_score(statuses: dict[int, str]) -> float:
    """killed / (killed + survived) * 100; vacuously 100.0 when there are no mutants."""
    killed, survived = counts(statuses)
    total = killed + survived
    return 100.0 if total == 0 else 100.0 * killed / total
