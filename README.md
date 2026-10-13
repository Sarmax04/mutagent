# mutagent

> A test-writing agent that proves its tests are good. It is graded by **mutation score**, not by how many tests it writes.

Built for the **Nebius x NVIDIA Global AI Hackathon** (track: *Coding and Agentic Engineering*), using NVIDIA Nemotron models served through **Nebius Token Factory**.

License: MIT (see `LICENSE`).

## The problem

LLM-generated tests often pass without testing anything. Coverage and "number of tests" are easy to game. Mutation testing asks the real question: if I plant a small bug in the code, does a test fail?

## How it works

```
 module + existing tests
          |
          v
  generate mutants (small planted bugs)          score = killed / (killed + survived) x 100
          |
          v
  run the suite against every mutant  ------>  survivors = bugs no test detects
          ^                                              |
          |                                              v
  accept only if the new tests                  STRONG Nemotron model writes tests aimed
  (1) pass on the ORIGINAL code and             at the surviving mutants (shown as diffs)
  (2) strictly raise the score  <---------------------- /
```

- **Two model tiers on purpose.** `STRONG` (a larger Nemotron model) writes tests. `FAST` (a small Nemotron model) optionally triages survivors that look semantically equivalent, so tokens are not wasted on unkillable mutants (`--triage`).
- **Hard token budget** per run (`NEBIUS_TOKEN_BUDGET`), so a public demo cannot drain credits.
- **Honest acceptance rule.** Tests that fail on the original code, or that do not improve the score, are rolled back. The failure output is fed into the next prompt.

## Where NVIDIA and Nebius are used

<!-- TODO before submitting: fill in the exact model IDs and which Nebius services you ended up using. -->

| Component | What it does |
|---|---|
| NVIDIA Nemotron (STRONG tier) | TODO: model ID. Writes tests targeting surviving mutants. |
| NVIDIA Nemotron (FAST tier) | TODO: model ID. Optional triage of likely-equivalent mutants. |
| Nebius Token Factory | All inference calls (`src/mutagent/llm.py`). |
| Nebius Serverless (Endpoints / Jobs) | TODO: describe how the demo is hosted, or delete this row. |

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env        # fill in the key, base URL and model IDs from the Token Factory docs

# 1. no model needed: score the demo project's deliberately weak tests
mutagent score --project examples/demo_project --target pricing.py

# 2. run the agent (uses Token Factory, up to --rounds rounds)
mutagent run --project examples/demo_project --target pricing.py --rounds 4
```

Other commands: `mutagent mutants ...` lists the mutants for a module without running anything. `python -m pytest` runs this repo's own tests, which use a fake model and need no API key.

### Real projects

```bash
# only run the test file(s) that cover the module (much faster, avoids unrelated test dependencies)
mutagent score --project path/to/lib --target src/pkg/mod.py --tests tests/test_mod.py
```

- `src/` and flat layouts both work: the mutated copy is put first on `PYTHONPATH`, so an installed copy of the library is never tested by mistake.
- Libraries that generate files at build time (for example a `_version.py` from setuptools-scm or hatch-vcs) need a placeholder in the checkout you point `--project` at.
- Cost grows with `mutants x test-run time`; every round re-scores all mutants.

### Targets measured so far

Baseline mutation score of each project's *own* tests, before the agent does anything (measured once, in a cloud Linux container; timings vary by machine). These are not agent results.

| Target (MIT-licensed) | Mutants | Baseline score | One scoring pass |
|---|---|---|---|
| cachetools `func.py` | 22 | 72.7% | ~8 s |
| humanize `number.py` | 140 | 86.4% | ~70 s |
| humanize `filesize.py` | 24 | 95.8% | ~10 s |
| humanize `lists.py` | 12 | 91.7% | ~4 s |
| cachetools `keys.py` | 4 | 100% | ~2 s |

The generated tests are written to `tests/test_mutagent_generated.py` inside the target project, and a JSON report (score per round, tokens used) to `mutagent_report.json`.

## Results

<!-- TODO: fill in from real runs; keep failures in, they make the numbers credible. -->

| Target | Score before | Score after | Rounds | Tokens |
|---|---|---|---|---|
| `examples/demo_project/pricing.py` | TODO | TODO | TODO | TODO |

## Safety

The agent executes model-written test code with pytest. Run it in a container or a Nebius Serverless Job, not on a machine with data you care about. Each test run has a timeout.

## Limitations

- The built-in mutation engine is intentionally small (comparison, arithmetic, boolean, negation and integer-constant mutations). It is isolated in `mutation.py` and can be replaced by mutmut or Stryker.
- Some surviving mutants are *equivalent* (they change no behaviour), so 100% is not always reachable.
- Module paths follow a flat or `src/` layout; the project's own test dependencies must be installed in the environment that runs `mutagent`.

## Feedback

Notes on Nebius Token Factory, AI Cloud and the NVIDIA tools used are collected in `FEEDBACK.md`.
