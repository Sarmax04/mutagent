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

import ast
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
NO_GAIN = "NO_GAIN"  # marker for last_error: tests passed but killed nothing new

SYSTEM_PROMPT = (
    "You are an expert Python test engineer. You write deterministic pytest tests "
    "that detect specific bugs. Never use the network, randomness, sleeping or "
    "file-system side effects. Assert exact expected values and exact exception "
    "types, and test boundary values on both sides of every comparison. Before "
    "asserting a value, read the source to see exactly what it returns or raises "
    "at that input (an input that looks like an edge case may be valid). Tests run "
    "only against the real code; never write a test that tries to reproduce a mutant. Keep any "
    "reasoning short. Your reply must be ONE ```python block with the complete "
    "test file; do not explain, and do not think out loud in the reply."
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
    survivors: list[int] = field(default_factory=list)  # ids of mutants still alive
    triaged: list[int] = field(default_factory=list)  # survivors the FAST model judged equivalent


@dataclass
class LoopReport:
    target: str
    mutant_count: int
    initial_score: float = 0.0
    final_score: float = 0.0
    tokens_used: int = 0
    generated_path: str | None = None
    generated_code: str = ""
    mutants: list[dict] = field(default_factory=list)  # {"id", "line", "kind"}
    mode: str = "guided"  # guided (survivor feedback loop) | baseline (same loop, no mutant feedback)
    rounds: list[RoundResult] = field(default_factory=list)
    explanations: dict[str, str] = field(default_factory=dict)  # surviving mutant id -> one line

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
    max_diffs: int = 30,
    guided: bool = True,
    lessons: str | None = None,
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
    if guided:
        parts += [
            "",
            "## Surviving mutants",
            "Each diff is a small bug that NO current test detects. Write tests that "
            "pass on the original source and fail on these mutants.",
            "",
            diffs,
        ]
    else:
        parts += [
            "",
            "## Task",
            "Write a thorough pytest test file for this module: exact expected values, "
            "exact exception types, and boundary values on both sides of every "
            "comparison. You are not told which bugs the current tests miss.",
        ]
    if last_error and last_error.startswith(NO_GAIN):
        parts += [
            "",
            "## Your previous attempt was rejected",
            "Its tests passed, but they did not catch any additional surviving mutant above. "
            "Do not repeat that style of test. For each mutant, find an input where the "
            "original and the mutated line return DIFFERENT results, then assert the "
            "original's result for that input. Tests only ever call the ORIGINAL code: the "
            "mutants do not exist while the tests run, so never try to call, copy or compare "
            "against a mutant inside a test.",
        ]
    elif last_error:
        parts += [
            "",
            "## Your previous attempt was rejected",
            "It failed against the original, unmutated code, so an expectation was "
            "wrong. Fix it. Pytest output (tail):",
            "```",
            last_error[-1500:],
            "```",
        ]
    if lessons:
        parts += [
            "",
            "## Wrong expectations from the last round (real pytest output)",
            "Some tests you wrote failed on the real code, so the code does NOT behave the "
            "way those tests assumed. Do not repeat these assumptions; use the real values shown:",
            "```",
            lessons[-1800:],
            "```",
        ]
    parts += [
        "",
        f"Return the COMPLETE contents of `{GENERATED_REL}` in a single ```python "
        "block and nothing else of substance. Do not copy the existing hand-written "
        "tests into it, and keep it to the tests that matter (at most about 15 test functions).",
    ]
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(parts)},
    ]


_FAILED_LINE = re.compile(r"^FAILED\s+(\S+)", re.MULTILINE)


def failing_test_names(output: str) -> list[str] | None:
    """Names of top-level test functions that failed, from pytest's short summary.

    None when the failures cannot be pinned on whole functions (collection errors,
    test classes), in which case nothing is pruned.
    """
    if re.search(r"^ERROR\b", output, re.MULTILINE):
        return None
    names: list[str] = []
    for node_id in _FAILED_LINE.findall(output):
        parts = node_id.split("::")
        if len(parts) != 2:
            return None
        names.append(parts[1].split("[")[0])
    return sorted(set(names)) or None


def prune_tests(code: str, names: Sequence[str]) -> str | None:
    """``code`` without the named top-level test functions, or None if impossible."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    funcs = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    if any(name not in funcs for name in names):
        return None
    remaining = [n for n in funcs if n.startswith("test") and n not in names]
    if not remaining:
        return None
    lines = code.splitlines(keepends=True)
    for name in sorted(names, key=lambda n: funcs[n].lineno, reverse=True):
        node = funcs[name]
        start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
        del lines[start - 1 : node.end_lineno]
    return "".join(lines).rstrip() + "\n"


_COLLECTION = re.compile(r"errors? during collection|ERROR collecting|^ERROR\s+\S+\.py", re.MULTILINE)


def collection_failed(output: str) -> bool:
    """True when pytest could not even import the test files (bad import, syntax error, ...)."""
    return bool(_COLLECTION.search(output))


def syntax_problem(code: str) -> str | None:
    """A one-line description of a syntax error in ``code``, or None if it parses."""
    try:
        ast.parse(code)
    except SyntaxError as exc:
        return f"{type(exc).__name__}: {exc.msg} (line {exc.lineno})"
    return None


@dataclass
class Attempt:
    code: str  # the file as kept (failing tests removed)
    check: object  # RunResult of the kept file
    dropped: list  # names of removed test functions
    failure: str  # pytest output that showed why they failed
    full_code: str  # the file before any removal
    repaired: bool = False


def repair_failures(llm: ChatModel, messages: list[dict[str, str]], code: str, failure: str) -> str | None:
    """Ask the STRONG model once to fix wrong expectations, using the real pytest output."""
    follow_up = [
        *messages,
        {"role": "assistant", "content": "```python\n" + code.rstrip() + "\n```"},
        {
            "role": "user",
            "content": (
                "Some of those tests FAILED on the original, unmodified code:\n```\n"
                + failure.strip()[-3000:]
                + "\n```\nThe original code is correct, so each failing expectation is wrong. "
                "Read the real values in the output, fix the expectations (or delete a test you "
                "cannot make correct), and keep the tests that already pass. "
                "Return the COMPLETE file in one ```python block."
            ),
        },
    ]
    try:
        return extract_code(llm.complete(Tier.STRONG, follow_up))
    except BudgetExceeded:
        return None


def repair(llm: ChatModel, messages: list[dict[str, str]], code: str, error: str) -> str | None:
    """Ask the STRONG model once to fix a test file that could not even be loaded."""
    follow_up = [
        *messages,
        {"role": "assistant", "content": "```python\n" + code.rstrip() + "\n```"},
        {
            "role": "user",
            "content": (
                "That file could not be loaded:\n```\n" + error.strip()[-1200:] + "\n```\n"
                "Return the COMPLETE corrected file in one ```python block. Change only what is needed."
            ),
        },
    ]
    try:
        return extract_code(llm.complete(Tier.STRONG, follow_up))
    except BudgetExceeded:
        return None


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
        ids = {int(i) for i in json.loads(match.group(0)) if int(i) in valid}
    except (ValueError, TypeError):
        return set()
    if len(ids) > max(1, len(survivors) // 2):
        # Implausible: most survivors are real bugs. Ignore the small model's verdict.
        return set()
    return ids


def explain_survivors(
    llm: ChatModel, survivors: list[Mutant], limit: int = 12
) -> dict[str, str]:
    """Ask the FAST model for one plain sentence per surviving bug: why no test caught it.

    Best effort: a reply that cannot be parsed gives no explanations.
    """
    shown = survivors[:limit]
    if not shown:
        return {}
    listing = "\n\n".join(f"### Mutant {m.id} (line {m.lineno}, {m.kind})\n{m.diff}" for m in shown)
    messages = [
        {"role": "system", "content": "You explain gaps in a test suite to a developer, briefly and concretely."},
        {
            "role": "user",
            "content": (
                "Each mutant below is a small planted bug that the tests did NOT catch. "
                "For each one, write ONE short sentence saying what input or check the tests "
                "are missing, or that the change cannot alter behaviour at all (equivalent). "
                'Reply with only a JSON object mapping the mutant id to its sentence, e.g. {"3": "..."}.\n\n'
                + listing
            ),
        },
    ]
    reply = llm.complete(Tier.FAST, messages)
    match = re.search(r"\{.*\}", reply, re.DOTALL)
    if not match:
        return {}
    try:
        raw = json.loads(match.group(0))
    except ValueError:
        return {}
    valid = {str(m.id) for m in shown}
    return {
        str(k): " ".join(str(v).split())[:240]
        for k, v in raw.items()
        if str(k) in valid and isinstance(v, str) and v.strip()
    }


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
        chunks.append(f"# {path.name}\n" + path.read_text(encoding="utf-8"))
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
        source = (work / target).read_text(encoding="utf-8")
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
    on_round: Callable[[RoundResult], None] | None = None,
    code_filter: Callable[[str], list[str]] | None = None,
    guided: bool = True,
    explain: bool = False,
) -> LoopReport:
    """Run the agent loop.

    ``guided=False`` is the comparison baseline: the same loop and the same acceptance
    rule, but the model is never shown which mutants survive. It only sees the
    project, its own earlier tests and any failure output.

    ``on_round`` is called after the baseline and after every round (for live UIs).
    ``code_filter`` receives each candidate test file and returns a list of problems;
    any problem rejects the candidate before it is executed (see ``safety.py``).
    """
    project = Path(project).resolve()
    work = copy_project(project)
    try:
        return _run(
            project, work, target, llm, max_rounds, patience, test_timeout,
            triage, generated_rel, write_back, log, tests, on_round, code_filter, guided, explain,
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
    on_round: Callable[[RoundResult], None] | None,
    code_filter: Callable[[str], list[str]] | None,
    guided: bool = True,
    explain: bool = False,
) -> LoopReport:
    source = (work / target).read_text(encoding="utf-8")
    mutants = generate_mutants(source)
    if not mutants:
        raise ValueError(f"{target} has no mutation sites; nothing to test against")

    base = run_tests(work, test_timeout, selection(work, tests, generated_rel))
    if base.returncode not in (0, 5):
        raise BaselineFailing(base.output)

    generated = work / generated_rel
    generated.parent.mkdir(parents=True, exist_ok=True)
    if not guided and generated.exists():
        generated.unlink()  # the baseline starts from the project's own tests only
    current = generated.read_text(encoding="utf-8") if generated.exists() else ""
    module = module_name(target)

    def spent() -> int:
        budget = getattr(llm, "budget", None)
        return budget.used if budget is not None else 0

    def restore() -> None:
        if current:
            generated.write_text(current, encoding="utf-8")
        elif generated.exists():
            generated.unlink()

    statuses = evaluate(
        work, target, mutants, source, test_timeout, selection(work, tests, generated_rel)
    )
    best = mutation_score(statuses)
    killed, survived = counts(statuses)
    report = LoopReport(
        target=target,
        mutant_count=len(mutants),
        initial_score=best,
        final_score=best,
        mutants=[{"id": m.id, "line": m.lineno, "kind": m.kind} for m in mutants],
        mode="guided" if guided else "baseline",
    )

    def alive() -> list[int]:
        return [m.id for m in mutants if statuses[m.id] == "survived"]

    def add(result: RoundResult) -> None:
        report.rounds.append(result)
        if on_round is not None:
            on_round(result)

    add(RoundResult(0, best, killed, survived, True, "baseline", 0, alive()))
    log(f"round 0: score {best:.1f}% ({killed} killed / {survived} survived) baseline")

    def check_and_prune(code: str, rnd: int, messages: list[dict[str, str]], allow_repair: bool) -> Attempt:
        """Write ``code``, run it, repair a load error once, then drop failing test functions."""
        generated.parent.mkdir(parents=True, exist_ok=True)
        generated.write_text(code, encoding="utf-8")
        chosen = selection(work, tests, generated_rel)
        check = run_tests(work, test_timeout, chosen)
        did_repair = False
        if allow_repair and not check.passed and not check.timed_out and collection_failed(check.output):
            log(f"round {rnd}: pytest could not load the new tests; asking the model to fix it once")
            did_repair = True
            fixed = repair(llm, messages, code, check.output)
            if fixed and not syntax_problem(fixed) and not (code_filter and code_filter(fixed)):
                code = fixed
                generated.write_text(code, encoding="utf-8")
                check = run_tests(work, test_timeout, chosen)
        full, failure, dropped = code, "", []
        if not check.passed:
            every = run_tests(work, test_timeout, [generated_rel], fail_fast=False)
            names = None if every.timed_out else failing_test_names(every.output)
            pruned = prune_tests(code, names) if names else None
            failure = every.output
            if pruned is not None:
                generated.write_text(pruned, encoding="utf-8")
                recheck = run_tests(work, test_timeout, chosen)
                if recheck.passed:
                    log(f"round {rnd}: dropped {len(names)} failing test(s): {', '.join(names)}")
                    code, check, dropped = pruned, recheck, list(names)
                else:
                    log(f"round {rnd}: dropping {', '.join(names)} was not enough. Output (last part):\n{recheck.output.strip()[-800:]}")
            elif names is None:
                log(f"round {rnd}: the failures could not be pinned on single test functions, so nothing was dropped")
        return Attempt(code, check, dropped, failure, full, did_repair)

    start_tokens = spent()
    last_error: str | None = None
    lessons: str | None = None
    stale = 0
    for rnd in range(1, max_rounds + 1):
        survivors = [m for m in mutants if statuses[m.id] == "survived"]
        if not survivors:
            log("all mutants killed")
            break

        before = spent()
        try:
            skip = triage_equivalent(llm, survivors) if (triage and guided) else set()
            if skip:
                log(f"round {rnd}: the FAST model judged {len(skip)} survivor(s) equivalent: {sorted(skip)}")
            shown = [m for m in survivors if m.id not in skip] or survivors
            if not guided:
                shown = []
            messages = build_messages(
                module,
                source,
                read_existing_tests(work, generated_rel, tests=tests),
                current,
                shown,
                last_error,
                guided=guided,
                lessons=lessons,
            )
            reply = llm.complete(Tier.STRONG, messages)
        except BudgetExceeded as exc:
            add(
                RoundResult(
                    rnd, best, killed, survived, False, f"stopped: {exc}", spent() - before, alive()
                )
            )
            log(f"round {rnd}: stopped, {exc}")
            break

        code = extract_code(reply)
        repaired = False
        if code is not None:
            problem = syntax_problem(code)
            if problem:
                log(f"round {rnd}: the reply has a {problem}; asking the model to fix it once")
                repaired = True
                code = repair(llm, messages, code, problem) or code
        problems = code_filter(code) if (code is not None and code_filter) else []
        if code is None:
            info = getattr(llm, "last_info", "")
            note, accepted = "rejected: no test code in reply", False
            if info:
                note += f" ({info})"
            log(f"round {rnd}: reply started with {reply[:200]!r}")
            stale += 1
        elif problems:
            shown_problems = "; ".join(problems[:3])
            last_error = "Your test file was rejected before it ran: " + shown_problems
            note, accepted = f"rejected: not allowed ({shown_problems})", False
            stale += 1
        else:
            attempt = check_and_prune(code, rnd, messages, allow_repair=not repaired)
            code, check, dropped = attempt.code, attempt.check, len(attempt.dropped)
            repaired = repaired or attempt.repaired
            lessons = attempt.failure if attempt.dropped else None
            saved = 0
            if dropped and check.passed:
                # Those tests failed because of wrong expectations. Show the model the real
                # outputs once and keep its fixed file only if it saves some of them.
                log(f"round {rnd}: asking the model to fix {dropped} dropped test(s) using the real failure output")
                candidate = repair_failures(llm, messages, attempt.full_code, attempt.failure)
                if candidate and not syntax_problem(candidate) and not (code_filter and code_filter(candidate)):
                    second = check_and_prune(candidate, rnd, messages, allow_repair=False)
                    if second.check.passed and len(second.dropped) < dropped:
                        saved = dropped - len(second.dropped)
                        log(f"round {rnd}: the fix saved {saved} test(s)")
                        code, check, dropped = second.code, second.check, len(second.dropped)
                    else:
                        generated.write_text(code, encoding="utf-8")
                else:
                    generated.write_text(code, encoding="utf-8")
            if not check.passed:
                restore()
                last_error = check.output
                tail = check.output.strip()[-1500:]
                log(f"round {rnd}: the new tests failed on the original code. Output (last part):\n{tail}")
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
                    if dropped:
                        note += f" ({dropped} failing test(s) dropped)"
                    if saved:
                        note += f" ({saved} repaired from real output)"
                    if repaired:
                        note += " (after one repair)"
                else:
                    restore()
                    note, accepted = "rejected: no score improvement", False
                    stale += 1
                    if guided:
                        last_error = NO_GAIN

        add(RoundResult(rnd, best, killed, survived, accepted, note, spent() - before, alive(), sorted(skip)))
        log(f"round {rnd}: score {best:.1f}% ({killed} killed / {survived} survived) {note}")
        if stale >= patience:
            log(f"stopping: no progress for {patience} rounds")
            break

    report.final_score = best
    report.generated_code = current
    if explain and alive():
        try:
            left = [m for m in mutants if m.id in set(alive())]
            report.explanations = explain_survivors(llm, left)
            log(f"explained {len(report.explanations)} surviving bug(s)")
        except BudgetExceeded:
            log("no budget left to explain the surviving bugs")
    report.tokens_used = spent() - start_tokens
    if write_back and current:
        out = project / generated_rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(current, encoding="utf-8")
        report.generated_path = generated_rel
    return report
