"""The agent loop: write tests -> measure mutation score -> target survivors.

Each round the STRONG model sees the module, the surviving mutants (as small
diffs) and its previous test file, and returns the complete next test file. A
candidate is accepted only if

  1. it passes on the original, unmutated code (otherwise the tests are wrong), and
  2. it strictly raises the mutation score.

Otherwise it is rolled back and the failure is fed into the next prompt.
Generated code is executed with pytest, so run this in a container or a Nebius
Serverless Job, never on a machine you care about.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from .llm import BudgetExceeded, ChatModel, Tier, strip_reasoning
from .mutation import (
    Mutant,
    copy_project,
    counts,
    evaluate,
    generate_mutants,
    mutation_score,
    run_tests,
)

GENERATED_REL = "tests/test_mutagent_generated.py"

SYSTEM_PROMPT = (
    "You are an expert Python test engineer. You write deterministic pytest tests "
    "that detect specific bugs. Never use the network, randomness, sleeping or "
    "file-system side effects. Assert exact expected values and exact exception "
    "types, and test boundary values on both sides of every comparison."
)


class BaselineFailing(RuntimeError):
    """The project's own tests fail on the unmutated code."""


@dataclass
class RoundResult:
    round: int
    score: float
    killed: int
    survived: int
    accepted: bool
    note: str
    tokens_used: int


@dataclass
class LoopReport:
    target: str
    mutant_count: int
    initial_score: float = 0.0
    final_score: float = 0.0
    tokens_used: int = 0
    generated_path: str | None = None
    rounds: list[RoundResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def module_name(target: str) -> str:
    """Import name for a target path: ``src/pkg/mod.py`` -> ``pkg.mod``."""
    parts = list(Path(target).with_suffix("").parts)
    if parts and parts[0] == "src":
        parts = parts[1:]
    return ".".join(parts)


_FENCE = re.compile(r"```(?:python|py)?[ \t]*\n(.*?)```", re.DOTALL)


def extract_code(reply: str) -> str | None:
    """First fenced block that contains a test function, else None."""
    for block in _FENCE.findall(strip_reasoning(reply)):
        if "def test" in block:
            return block.strip() + "\n"
    return None


def build_messages(
    module: str,
    source: str,
    existing_tests: str,
    current_generated: str,
    survivors: list[Mutant],
    last_error: str | None,
    max_diffs: int = 12,
) -> list[dict[str, str]]:
    diffs = "\n\n".join(
        f"### Mutant {m.id} (line {m.lineno}, {m.kind})\n{m.diff}"
        for m in survivors[:max_diffs]
    )
    parts = [
        f"Module under test: `{module}` (import it with `from {module} import ...`).",
        "",
        "## Source",
        "```python",
        source.rstrip(),
        "```",
    ]
    if existing_tests:
        parts += [
            "",
            "## Existing hand-written tests (they stay in place; do not duplicate them)",
            "```python",
            existing_tests.rstrip(),
            "```",
        ]
    if current_generated:
        parts += [
            "",
            "## Your current generated test file (keep its tests and extend it)",
            "```python",
            current_generated.rstrip(),
            "```",
        ]
    parts += [
        "",
        "## Surviving mutants",
        "Each diff is a small bug that NO current test detects. Write tests that "
        "pass on the original source and fail on these mutants.",
        "",
        diffs,
    ]
    if last_error:
        parts += [
            "",
            "## Your previous attempt was rejected",
            "It failed against the original, unmutated code, so an expectation was "
            "wrong. Fix it. Pytest output (tail):",
            "```",
            last_error[-1500:],
            "```",
        ]
    parts += [
        "",
        f"Return the COMPLETE contents of `{GENERATED_REL}` in a single ```python "
        "block and nothing else of substance.",
    ]
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(parts)},
    ]


def triage_equivalent(llm: ChatModel, survivors: list[Mutant]) -> set[int]:
    """Ask the FAST model which survivors are probably equivalent (unkillable).

    Cheap, optional, and conservative: when the reply is unclear nothing is skipped.
    """
    if not survivors:
        return set()
    listing = "\n\n".join(f"### Mutant {m.id}\n{m.diff}" for m in survivors[:40])
    messages = [
        {
            "role": "system",
            "content": "You judge whether a code mutation changes observable behaviour.",
        },
        {
            "role": "user",
            "content": (
                "Return a JSON array with the ids of mutants that are semantically "
                "EQUIVALENT to the original (no input can tell them apart). If you are "
                "unsure about a mutant, leave it out. Reply with only the JSON array.\n\n"
                + listing
            ),
        },
    ]
    reply = llm.complete(Tier.FAST, messages)
    match = re.search(r"\[[\d,\s]*\]", reply)
    if not match:
        return set()
    valid = {m.id for m in survivors}
    try:
        return {int(i) for i in json.loads(match.group(0)) if int(i) in valid}
    except (ValueError, TypeError):
        return set()


def selection(work: Path, tests: Sequence[str], generated_rel: str) -> list[str]:
    """Pytest arguments: the chosen test files plus the generated file once it exists.

    Empty ``tests`` means "run everything pytest discovers".
    """
    if not tests:
        return []
    chosen = list(tests)
    if (work / generated_rel).exists():
        chosen.append(generated_rel)
    return chosen


def read_existing_tests(
    work: Path, generated_rel: str, limit: int = 6000, tests: Sequence[str] = ()
) -> str:
    if tests:
        paths = [work / t for t in tests]
    else:
        tests_dir = work / "tests"
        paths = sorted(tests_dir.glob("test_*.py")) if tests_dir.is_dir() else []
    chunks: list[str] = []
    for path in paths:
        if not path.is_file() or path.relative_to(work).as_posix() == generated_rel:
            continue
        chunks.append(f"# {path.name}\n{path.read_text()}")
    return "\n\n".join(chunks)[:limit]


def measure(
    project: Path,
    target: str,
    test_timeout: float = 20.0,
    tests: Sequence[str] = (),
) -> tuple[list[Mutant], dict[int, str]]:
    """Score the project's current tests without calling any model."""
    work = copy_project(Path(project).resolve())
    try:
        source = (work / target).read_text()
        mutants = generate_mutants(source)
        chosen = selection(work, tests, GENERATED_REL)
        base = run_tests(work, test_timeout, chosen)
        if base.returncode not in (0, 5):  # 5 = no tests yet, which is fine
            raise BaselineFailing(base.output)
        return mutants, evaluate(work, target, mutants, source, test_timeout, chosen)
    finally:
        shutil.rmtree(work.parent, ignore_errors=True)


def run_loop(
    project: Path,
    target: str,
    llm: ChatModel,
    *,
    max_rounds: int = 4,
    patience: int = 2,
    test_timeout: float = 20.0,
    triage: bool = False,
    generated_rel: str = GENERATED_REL,
    write_back: bool = True,
    log: Callable[[str], None] = print,
    tests: Sequence[str] = (),
) -> LoopReport:
    project = Path(project).resolve()
    work = copy_project(project)
    try:
        return _run(
            project, work, target, llm, max_rounds, patience, test_timeout,
            triage, generated_rel, write_back, log, tests,
        )
    finally:
        shutil.rmtree(work.parent, ignore_errors=True)


def _run(
    project: Path,
    work: Path,
    target: str,
    llm: ChatModel,
    max_rounds: int,
    patience: int,
    test_timeout: float,
    triage: bool,
    generated_rel: str,
    write_back: bool,
    log: Callable[[str], None],
    tests: Sequence[str],
) -> LoopReport:
    source = (work / target).read_text()
    mutants = generate_mutants(source)
    if not mutants:
        raise ValueError(f"{target} has no mutation sites; nothing to test against")

    base = run_tests(work, test_timeout, selection(work, tests, generated_rel))
    if base.returncode not in (0, 5):
        raise BaselineFailing(base.output)

    generated = work / generated_rel
    generated.parent.mkdir(parents=True, exist_ok=True)
    current = generated.read_text() if generated.exists() else ""
    module = module_name(target)

    def spent() -> int:
        budget = getattr(llm, "budget", None)
        return budget.used if budget is not None else 0

    def restore() -> None:
        if current:
            generated.write_text(current)
        elif generated.exists():
            generated.unlink()

    statuses = evaluate(
        work, target, mutants, source, test_timeout, selection(work, tests, generated_rel)
    )
    best = mutation_score(statuses)
    killed, survived = counts(statuses)
    report = LoopReport(
        target=target, mutant_count=len(mutants), initial_score=best, final_score=best
    )
    report.rounds.append(RoundResult(0, best, killed, survived, True, "baseline", 0))
    log(f"round 0: score {best:.1f}% ({killed} killed / {survived} survived) baseline")

    start_tokens = spent()
    last_error: str | None = None
    stale = 0
    for rnd in range(1, max_rounds + 1):
        survivors = [m for m in mutants if statuses[m.id] == "survived"]
        if not survivors:
            log("all mutants killed")
            break

        before = spent()
        try:
            skip = triage_equivalent(llm, survivors) if triage else set()
            shown = [m for m in survivors if m.id not in skip] or survivors
            messages = build_messages(
                module,
                source,
                read_existing_tests(work, generated_rel, tests=tests),
                current,
                shown,
                last_error,
            )
            reply = llm.complete(Tier.STRONG, messages)
        except BudgetExceeded as exc:
            report.rounds.append(
                RoundResult(rnd, best, killed, survived, False, f"stopped: {exc}", spent() - before)
            )
            log(f"round {rnd}: stopped, {exc}")
            break

        used = spent() - before
        code = extract_code(reply)
        if code is None:
            note, accepted = "rejected: no test code in reply", False
            stale += 1
        else:
            generated.write_text(code)
            check = run_tests(work, test_timeout, selection(work, tests, generated_rel))
            if not check.passed:
                restore()
                last_error = check.output
                note, accepted = "rejected: tests fail on the original code", False
                stale += 1
            else:
                new_statuses = evaluate(
                    work, target, mutants, source, test_timeout,
                    selection(work, tests, generated_rel),
                )
                new_score = mutation_score(new_statuses)
                if new_score > best:
                    statuses, best, current = new_statuses, new_score, code
                    killed, survived = counts(statuses)
                    last_error, stale = None, 0
                    note, accepted = "accepted: score improved", True
                else:
                    restore()
                    note, accepted = "rejected: no score improvement", False
                    stale += 1

        report.rounds.append(RoundResult(rnd, best, killed, survived, accepted, note, used))
        log(f"round {rnd}: score {best:.1f}% ({killed} killed / {survived} survived) {note}")
        if stale >= patience:
            log(f"stopping: no progress for {patience} rounds")
            break

    report.final_score = best
    report.tokens_used = spent() - start_tokens
    if write_back and current:
        out = project / generated_rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(current)
        report.generated_path = generated_rel
    return report
