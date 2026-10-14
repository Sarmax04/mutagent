# mutagent

> A test-writing agent that proves its tests are good. It is graded by **mutation score**, not by how many tests it writes.

Built for the **Nebius x NVIDIA Global AI Hackathon** (track: *Coding and Agentic Engineering*), using NVIDIA Nemotron models served through **Nebius Token Factory**.

License: MIT (see `LICENSE`).

**Live demo:** https://mutagent.onrender.com (free hosting: after 15 idle minutes the first visit takes about a minute to wake up).

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


| Component | What it does |
|---|---|
| NVIDIA Nemotron (STRONG tier) | `nvidia/nemotron-3-super-120b-a12b`. Writes tests targeting surviving mutants. |
| NVIDIA Nemotron (FAST tier) | `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B`. Optional triage of likely-equivalent mutants (`--triage`); not used in the runs below. |
| Nebius Token Factory | All inference calls (`src/mutagent/llm.py`). |
| Web demo hosting | The demo page runs on Render's free tier (Docker). Only the model calls go to Nebius, which the hackathon FAQ allows. |

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

An unmodified copy of cachetools (MIT) is vendored in `examples/cachetools_demo/` so the demo runs out of the box; see its `VENDORED.md` for the source commit and license:

```bash
mutagent score --project examples/cachetools_demo --target src/cachetools/func.py --tests tests/test_func.py
```

The generated tests are written to `tests/test_mutagent_generated.py` inside the target project, and a JSON report (score per round, tokens used) to `mutagent_report.json`.

## Web demo

```bash
pip install -e ".[web]"
mutagent serve            # http://127.0.0.1:8000
```

The page shows every planted bug as a circle (hollow = slipped through, filled = caught by a test), the score after each round, and the tests the agent kept.

- **Recorded run:** plays back a real run from `replays/<preset>.json`. Record one with the commands in `replays/README.md`. Recordings come from real runs only.
- **Live run:** starts a capped run against the model. It switches on only when the `NEBIUS_*` values from `.env.example` are set; otherwise the page offers recordings only.
- **Presets only:** visitors pick one of the fixed libraries in `src/mutagent/presets.py`. They never upload code or write prompts.
- **Limits (see `.env.example`):** one live run at a time, a token budget per run, a cap on live runs per day.

Container image (deployed on Render; the Dockerfile listens on `$PORT`, default 8000): `docker build -t mutagent .`, then `docker run -p 8000:8000 --env-file .env mutagent`. It runs as an unprivileged user with `MUTAGENT_LIMITS=1`.

## Results

Real runs against Nebius Token Factory (Nemotron 3 Super as the STRONG model), 3 rounds at most. Both runs are recorded in `replays/` and play back in the web demo.

| Target | Score before | Score after | Rounds | Tokens |
|---|---|---|---|---|
| cachetools `func.py` (22 mutants) | 72.7% | 100.0% | 1 | 6,608 |
| `examples/demo_project/pricing.py` (29 mutants) | 24.1% | 96.6% | 3 | 9,745 |

The remaining pricing mutant survives because it is equivalent: changing `<= 5` to `< 5` gives the same result at exactly 5 kg, so no test can tell them apart.

Failures I hit while getting these runs, and what changed:

- The reasoning model spent its whole reply budget thinking and returned no code. Fixes: a higher reply limit (`NEBIUS_MAX_TOKENS`), a stricter prompt, and an option to switch thinking off (`NEBIUS_EXTRA_BODY`).
- One wrong expectation made the agent reject a whole file of otherwise good tests. It now removes only the failing test functions and re-checks the rest.
- On Windows the generated tests were saved in a legacy encoding and crashed on a non-ASCII character. All files are now read and written as UTF-8.

## Safety

The agent executes model-written test code with pytest, so treat it as untrusted code.

- Each test run has a timeout.
- In the web demo, every model-written test file is screened first (`src/mutagent/safety.py`) and is rejected before running if it imports modules such as `os`, `subprocess`, `socket` or `io`, or calls `exec`, `open` and similar. This is a denylist and **not a sandbox**; a determined attacker could get around it.
- `MUTAGENT_LIMITS=1` (Linux) starts each pytest process under CPU, memory and file-size limits.
- Real isolation has to come from where you run it: a container or a Nebius Serverless Job, as an unprivileged user, without secrets and ideally without network access. Don't run it on a machine with data you care about.

## Limitations

- The built-in mutation engine is intentionally small (comparison, arithmetic, boolean, negation and integer-constant mutations). It is isolated in `mutation.py` and can be replaced by mutmut or Stryker.
- Some surviving mutants are *equivalent* (they change no behaviour), so 100% is not always reachable.
- Module paths follow a flat or `src/` layout; the project's own test dependencies must be installed in the environment that runs `mutagent`.

## Feedback

Notes on Nebius Token Factory, AI Cloud and the NVIDIA tools used are collected in `FEEDBACK.md`.
