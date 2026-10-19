"""Record real runs for the hosted demo's "Recorded run" mode.

For every preset (or the ones you name) this runs, against the real model:

1. the agent loop            -> replays/<preset>.json
2. the baseline              -> replays/<preset>-baseline.json  (same loop and model, never told which bugs survive)

Run it from the repository root, with .env filled in:

    python scripts/record.py                    # all presets
    python scripts/record.py pricing-toy        # just one
    python scripts/record.py --triage           # also let the FAST model set aside likely-unkillable bugs
    python scripts/record.py --sandbox          # run every pytest in Nebius Token Factory Sandboxes (much faster)

Defaults match the published recordings: up to 6 rounds, stop after 3 rounds
without progress, no triage, the FAST model explains the bugs that survive, and the
new tests are trimmed to those that catch something the others miss (``--minimize``).

Nothing is written into the example projects (``--no-write-back``). Recordings must
come from real runs only; never edit them by hand.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mutagent.presets import PRESETS, examples_dir  # noqa: E402


def run(cmd: list[str]) -> int:
    print("\n$ " + " ".join(cmd), flush=True)
    return subprocess.call(cmd, cwd=ROOT)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("presets", nargs="*", help="preset ids (default: all)")
    parser.add_argument("--rounds", type=int, default=6)
    parser.add_argument("--triage", action="store_true", help="let the FAST model set aside likely-unkillable bugs")
    parser.add_argument("--no-triage", action="store_true", help=argparse.SUPPRESS)  # old flag; triage is off by default
    parser.add_argument("--skip-baseline", action="store_true")
    parser.add_argument("--attempts", type=int, default=1, help="best of N for both the agent and the baseline (N x the tokens)")
    parser.add_argument("--docs", action="store_true", help="both runs look up the library's documentation with Tavily (needs TAVILY_API_KEY)")
    parser.add_argument("--sandbox", action="store_true", help="run every pytest in Nebius Token Factory Sandboxes (needs contree-sdk and NEBIUS_PROJECT_ID)")
    args = parser.parse_args()

    known = {p.id: p for p in PRESETS}
    unknown = [name for name in args.presets if name not in known]
    if unknown:
        print(f"unknown preset(s): {', '.join(unknown)}. Choose from: {', '.join(known)}", file=sys.stderr)
        return 2
    chosen = [known[name] for name in args.presets] if args.presets else list(PRESETS)

    failures = 0
    for preset in chosen:
        project = str(examples_dir() / preset.project)
        base = [sys.executable, "-m", "mutagent"]
        common = ["--project", project, "--target", preset.target, "--no-write-back"]
        if preset.tests:
            common += ["--tests", *preset.tests]
        common += ["--attempts", str(args.attempts)]
        if args.docs:
            common.append("--docs")
        if args.sandbox:
            common.append("--sandbox")
            if preset.packages:
                common += ["--sandbox-packages", *preset.packages]
        common.append("--minimize")
        guided = base + ["run", *common, "--rounds", str(args.rounds), "--report", f"replays/{preset.id}.json"]
        if args.triage and not args.no_triage:
            guided.append("--triage")
        guided.append("--explain")
        failures += run(guided) != 0
        if not args.skip_baseline:
            blind = base + ["baseline", *common, "--rounds", str(args.rounds), "--report", f"replays/{preset.id}-baseline.json"]
            failures += run(blind) != 0
    print("\nDone." if not failures else f"\n{failures} command(s) failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
