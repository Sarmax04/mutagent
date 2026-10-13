"""Command-line interface: ``mutagent mutants|score|run``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .llm import ConfigError, NebiusChat, Settings, TokenBudget, load_dotenv
from .loop import BaselineFailing, measure, run_loop
from .mutation import counts, generate_mutants, mutation_score


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mutagent",
        description="Write tests with Nemotron on Nebius Token Factory, graded by mutation score.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--project", required=True, help="project directory containing the tests")
        p.add_argument("--target", required=True, help="module under test, relative to --project")

    p = sub.add_parser("mutants", help="list the mutants generated for a module (no tests run)")
    common(p)

    def tests_opt(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--tests",
            nargs="+",
            default=[],
            metavar="PATH",
            help="run only these test files (relative to --project); default: everything pytest finds",
        )

    p = sub.add_parser("score", help="score the project's existing tests (no model calls)")
    common(p)
    tests_opt(p)
    p.add_argument("--test-timeout", type=float, default=20.0)

    p = sub.add_parser("run", help="run the agent loop")
    common(p)
    tests_opt(p)
    p.add_argument("--rounds", type=int, default=4)
    p.add_argument("--budget", type=int, help="token cap; overrides NEBIUS_TOKEN_BUDGET")
    p.add_argument("--triage", action="store_true", help="use the FAST model to skip likely-equivalent mutants")
    p.add_argument("--test-timeout", type=float, default=20.0)
    p.add_argument("--report", default="mutagent_report.json")
    p.add_argument("--no-write-back", action="store_true", help="do not write generated tests into the project")

    p = sub.add_parser("serve", help="start the web demo (needs: pip install -e \".[web]\")")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.cmd == "serve":
        from .web import serve

        serve(args.host, args.port)
        return 0
    project = Path(args.project)
    if not (project / args.target).is_file():
        print(f"error: {project / args.target} not found", file=sys.stderr)
        return 2

    if args.cmd == "mutants":
        mutants = generate_mutants((project / args.target).read_text())
        print(f"{len(mutants)} mutants")
        for m in mutants:
            print(f"  #{m.id:<3} line {m.lineno:<4} {m.kind}")
        return 0

    try:
        if args.cmd == "score":
            mutants, statuses = measure(project, args.target, args.test_timeout, args.tests)
            killed, survived = counts(statuses)
            print(f"mutation score: {mutation_score(statuses):.1f}% ({killed} killed / {survived} survived)")
            for m in mutants:
                if statuses[m.id] == "survived":
                    print(f"  survived #{m.id} line {m.lineno} {m.kind}")
            return 0

        load_dotenv(Path(".env"))
        settings = Settings.from_env()
        budget = TokenBudget(args.budget or settings.token_budget)
        llm = NebiusChat(settings, budget)
        report = run_loop(
            project,
            args.target,
            llm,
            max_rounds=args.rounds,
            test_timeout=args.test_timeout,
            triage=args.triage,
            write_back=not args.no_write_back,
            tests=args.tests,
        )
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except BaselineFailing as exc:
        print("error: the project's own tests fail before any mutation:\n" + str(exc), file=sys.stderr)
        return 1

    data = report.to_dict()
    data["models"] = {"fast": settings.model_fast, "strong": settings.model_strong}
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(data, indent=2))
    print(
        f"\nscore {report.initial_score:.1f}% -> {report.final_score:.1f}% "
        f"using {report.tokens_used} tokens; report: {args.report}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
