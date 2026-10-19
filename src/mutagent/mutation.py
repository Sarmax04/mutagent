"""A small AST-based mutation engine and test runner.

A *mutant* is the module under test with one deliberate bug (a flipped
comparison, a swapped operator, ...). A test suite *kills* a mutant if it fails
on the buggy code. The mutation score is

    score = killed / (killed + survived) * 100

Timeouts count as killed (the mutant broke the program, usually an infinite
loop). Some survivors are *equivalent mutants* that change no observable
behaviour and can never be killed, so 100% is not always reachable.

Operators: flipped comparisons (``<``/``<=``, ``==``/``!=``, ``in``/``not in``,
``is``/``is not``), swapped arithmetic (also ``+=`` and friends), ``and``/``or``
swaps, removed ``not``, negated ``if`` conditions (also ``if`` filters in
comprehensions), ``return x`` -> ``return None``, bumped integer / flipped boolean
constants, and, inside functions: a call statement removed, a ``raise`` removed,
``break``/``continue`` swapped, a unary minus dropped, and an assigned value replaced
by ``None``. Still smaller than mutmut or Stryker, which can replace it; the rest of
the agent only needs ``Mutant`` objects.
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
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
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
    kind: str  # compare | arith | boolop | not | const | negate | return | call | raise | loop | sign | assign
    diff: str  # unified diff against the normalised original
    source: str  # full mutated module source


def _inside_functions(tree: ast.AST) -> set[int]:
    """ids of the nodes that live in a function body (statement mutations stay there:
    removing a module-level statement usually just breaks the import)."""
    inside: set[int] = set()
    for func in ast.walk(tree):
        if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for stmt in func.body:
                inside.update(id(n) for n in ast.walk(stmt))
    return inside


def _sites(tree: ast.AST) -> list[Site]:
    """All places where a mutation can be applied, in a deterministic order."""
    out: list[Site] = []
    inside = _inside_functions(tree)
    for node in ast.walk(tree):
        if id(node) in inside:
            if isinstance(node, ast.Expr) and isinstance(node.value, (ast.Call, ast.Await)):
                out.append((node, "call", 0))
            elif isinstance(node, ast.Raise):
                out.append((node, "raise", 0))
            elif isinstance(node, (ast.Break, ast.Continue)):
                out.append((node, "loop", 0))
            elif (
                isinstance(node, ast.Assign)
                and not isinstance(node.value, ast.Constant)
                and all(isinstance(t, ast.Name) for t in node.targets)
            ):
                out.append((node, "assign", 0))
        if isinstance(node, ast.comprehension):
            for i in range(len(node.ifs)):
                out.append((node, "negate", i))
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub) and not isinstance(node.operand, ast.Constant):
            out.append((node, "sign", 0))
        if isinstance(node, ast.Compare):
            for i, op in enumerate(node.ops):
                if type(op) in _CMP:
                    out.append((node, "compare", i))
        elif isinstance(node, (ast.BinOp, ast.AugAssign)) and type(node.op) in _BIN:
            out.append((node, "arith", 0))
        elif isinstance(node, (ast.If, ast.IfExp)):
            out.append((node, "negate", 0))
        elif (
            isinstance(node, ast.Return)
            and node.value is not None
            and not (isinstance(node.value, ast.Constant) and node.value.value is None)
        ):
            out.append((node, "return", 0))
        elif isinstance(node, ast.BoolOp) and type(node.op) in _BOOL:
            out.append((node, "boolop", 0))
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            out.append((node, "not", 0))
        elif isinstance(node, ast.Constant) and isinstance(node.value, (bool, int)):
            out.append((node, "const", 0))
    return out


def _replace(parents: dict[int, ast.AST], node: ast.AST, new: ast.AST) -> None:
    """Put ``new`` where ``node`` is in its parent's statement list."""
    parent = parents[id(node)]
    for field_name, value in ast.iter_fields(parent):
        if isinstance(value, list) and any(item is node for item in value):
            value[[i for i, item in enumerate(value) if item is node][0]] = ast.copy_location(new, node)
            return
    raise ValueError("statement not found in its parent")


def _apply(site: Site, parents: dict[int, ast.AST] | None = None) -> None:
    node, kind, idx = site
    if kind == "call":
        # `log(x)` -> `None`: the call no longer happens
        node.value = ast.Constant(value=None)
    elif kind == "raise":
        _replace(parents or {}, node, ast.Pass())
    elif kind == "loop":
        _replace(parents or {}, node, ast.Continue() if isinstance(node, ast.Break) else ast.Break())
    elif kind == "sign":
        # `-x` -> `+x`
        node.op = ast.UAdd()
    elif kind == "assign":
        node.value = ast.Constant(value=None)
    elif kind == "negate" and isinstance(node, ast.comprehension):
        node.ifs[idx] = ast.UnaryOp(op=ast.Not(), operand=node.ifs[idx])
    elif kind == "compare":
        node.ops[idx] = _CMP[type(node.ops[idx])]()
    elif kind == "arith":
        node.op = _BIN[type(node.op)]()
    elif kind == "boolop":
        node.op = _BOOL[type(node.op)]()
    elif kind == "not":
        # `not x` -> `not not x`, which has the truthiness of `x` (negation removed)
        node.operand = ast.UnaryOp(op=ast.Not(), operand=node.operand)
    elif kind == "negate":
        # `if x:` -> `if not x:`
        node.test = ast.UnaryOp(op=ast.Not(), operand=node.test)
    elif kind == "return":
        # `return x` -> `return None`
        node.value = ast.Constant(value=None)
    elif kind == "const":
        if isinstance(node.value, bool):
            node.value = not node.value
        else:
            node.value = node.value + 1


def generate_mutants(source: str, lines: set[int] | None = None) -> list[Mutant]:
    """One mutant per mutation site. Diffs are taken between normalised sources.

    ``lines`` restricts the mutants to those source lines (for example the lines a pull
    request changed); ids are then numbered 0..n-1 again.
    """
    tree = ast.parse(source)
    base_lines = ast.unparse(tree).splitlines()
    mutants: list[Mutant] = []
    for i in range(len(_sites(tree))):
        mutated_tree = copy.deepcopy(tree)
        site = _sites(mutated_tree)[i]
        node, kind, idx = site
        anchor = node.ifs[idx] if isinstance(node, ast.comprehension) else node
        lineno = getattr(anchor, "lineno", 0)
        if lines is not None and lineno not in lines:
            continue
        parents = {id(child): parent for parent in ast.walk(mutated_tree) for child in ast.iter_child_nodes(parent)}
        _apply(site, parents)
        ast.fix_missing_locations(mutated_tree)
        mutated = ast.unparse(mutated_tree)
        diff = "\n".join(
            difflib.unified_diff(
                base_lines, mutated.splitlines(), "original", "mutant", lineterm="", n=2
            )
        )
        mutants.append(
            Mutant(
                id=len(mutants),
                lineno=lineno,
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


def mutant_outputs(
    work: Path,
    target: str,
    mutants: list[Mutant],
    original_source: str,
    timeout: float = 20.0,
    tests: Sequence[str] = (),
) -> dict[int, TestRun]:
    """Like ``evaluate`` but without fail-fast, keeping each run's output (to see which tests failed)."""
    target_path = work / target
    runs: dict[int, TestRun] = {}
    try:
        for mutant in mutants:
            target_path.write_text(mutant.source, encoding="utf-8")
            runs[mutant.id] = run_tests(work, timeout, tests, fail_fast=False)
    finally:
        target_path.write_text(original_source, encoding="utf-8")
    return runs


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
