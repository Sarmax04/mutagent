"""Score several modules in one go, and optionally let the agent fix the weak ones."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from .linecov import project_coverage
from .loop import BaselineFailing, measure
from .mutation import counts, generate_mutants, mutation_score

SKIP_DIRS = {"tests", "test", "docs", "examples", "build", "dist", "node_modules", "site-packages"}
SKIP_FILES = {"setup.py", "conftest.py", "__main__.py", "_version.py"}


@dataclass
class ScanRow:
    target: str
    mutants: int
    score: float | None  # None when the module has no mutants or its tests fail
    killed: int = 0
    survived: int = 0
    note: str = ""
    coverage: float | None = None  # line coverage of the module, next to its mutation score


def discover_targets(project: Path) -> list[str]:
    """Python modules worth mutating: not tests, not hidden or vendored folders."""
    project = Path(project)
    found: list[str] = []
    for path in sorted(project.rglob("*.py")):
        rel = path.relative_to(project)
        parts = rel.parts
        if any(p.startswith(".") or p in SKIP_DIRS or p.endswith(".egg-info") or p == "__pycache__" for p in parts[:-1]):
            continue
        if path.name in SKIP_FILES or path.name.startswith("test_") or path.name.endswith("_test.py"):
            continue
        if not path.read_text(encoding="utf-8", errors="replace").strip():
            continue
        found.append(rel.as_posix())
    return found


def scan(
    project: Path,
    targets: Sequence[str],
    *,
    tests: Sequence[str] = (),
    test_timeout: float = 20.0,
    log: Callable[[str], None] = print,
    runner=None,
) -> list[ScanRow]:
    rows: list[ScanRow] = []
    for target in targets:
        source = (Path(project) / target).read_text(encoding="utf-8")
        if not generate_mutants(source):
            rows.append(ScanRow(target, 0, None, note="nothing to mutate"))
            continue
        log(f"scoring {target} ...")
        try:
            mutants, statuses = measure(Path(project), target, test_timeout, tests, runner=runner)
        except BaselineFailing:
            rows.append(ScanRow(target, 0, None, note="the project's own tests fail"))
            continue
        killed, survived = counts(statuses)
        coverage = project_coverage(Path(project), target, tests, runner=runner)
        rows.append(ScanRow(target, len(mutants), mutation_score(statuses), killed, survived, coverage=coverage))
    return rows


def format_table(rows: Sequence[ScanRow]) -> str:
    width = max([len(r.target) for r in rows] + [6])
    lines = [f"{'module':<{width}}  mutants    score  coverage  survived"]
    for r in rows:
        score = "-" if r.score is None else f"{r.score:5.1f}%"
        cover = "-" if r.coverage is None else f"{r.coverage:5.1f}%"
        extra = f"  ({r.note})" if r.note else ""
        lines.append(f"{r.target:<{width}}  {r.mutants:>7}  {score:>7}  {cover:>8}  {r.survived:>8}{extra}")
    scored = [r.score for r in rows if r.score is not None]
    if scored:
        lines.append(f"{'lowest':<{width}}  {'':>7}  {min(scored):5.1f}%")
    return "\n".join(lines)
