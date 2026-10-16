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

- **Planted bugs:** flipped comparisons (`<`/`<=`, `==`/`!=`, `in`/`not in`, `is`/`is not`), swapped arithmetic (also `+=`), `and`/`or` swaps, removed `not`, negated `if` conditions, `return x` replaced by `return None`, and bumped integer / flipped boolean constants.
- **Two model tiers on purpose.** `STRONG` (a larger Nemotron model) writes tests. `FAST` (a small Nemotron model) optionally triages survivors that look semantically equivalent, so tokens are not wasted on unkillable mutants (`--triage`). Triage only changes what the STRONG model is shown; the score still counts every bug.
- **A comparison baseline.** `mutagent baseline` runs the same loop with the same model and the same acceptance rule, but never shows the model which bugs survive. The difference between the two scores is what the survivor feedback adds.
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

# 2. run the agent (uses Token Factory, up to --rounds rounds); --triage uses the small FAST model too
mutagent run --project examples/demo_project --target pricing.py --rounds 4 --triage

# 3. the comparison baseline: same loop and model, no bug feedback
mutagent baseline --project examples/demo_project --target pricing.py
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

Baseline mutation score of each project's *own* tests, before the agent does anything (measured once with the current set of planted bugs, in a cloud Linux container; timings vary by machine). These are not agent results.

| Target (MIT-licensed) | Mutants | Baseline score | One scoring pass |
|---|---|---|---|
| `examples/demo_project/pricing.py` (toy) | 37 | 37.8% | ~5 s |
| cachetools `func.py` | 55 | 89.1% | ~19 s |
| python-slugify `slugify.py` (two of its test files) | 118 | 54.2% | ~53 s |
| inflection `__init__.py` | 108 | 88.9% | ~57 s |
| humanize `filesize.py` | 36 | 97.2% | ~14 s |
| humanize `lists.py` | 20 | 90.0% | ~8 s |
| humanize `time.py` | 366 | 89.6% | ~6 min |
| cachetools `keys.py` | 19 | 94.7% | ~5 s |

Unmodified copies of cachetools, inflection and python-slugify (all MIT) are vendored in `examples/` so the demo runs out of the box; each folder has a `VENDORED.md` with the source and license. The humanize rows were measured on a separate checkout and are not shipped.

```bash
mutagent score --project examples/cachetools_demo --target src/cachetools/func.py --tests tests/test_func.py
mutagent score --project examples/inflection_demo --target inflection/__init__.py --tests test_inflection.py
mutagent score --project examples/slugify_demo --target slugify/slugify.py --tests tests/test_legacy.py tests/test_modern_word_boundary.py
```

The generated tests are written to `tests/test_mutagent_generated.py` inside the target project, and a JSON report (score per round, tokens used) to `mutagent_report.json`.

## Web demo

```bash
pip install -e ".[web]"
mutagent serve            # http://127.0.0.1:8000
```

The page shows every planted bug as a circle (hollow = slipped through, filled = caught by a test), the score after each round, and the tests the agent kept.

- **Recorded run:** plays back a real run from `replays/<preset>.json`, next to the no-feedback comparison from `replays/<preset>-baseline.json` when it exists. Record them with `python scripts/record.py` (see `replays/README.md`). Recordings come from real runs only.
- **Live run:** starts a capped run against the model. It switches on only when the `NEBIUS_*` values from `.env.example` are set; otherwise the page offers recordings only.
- **Presets only:** visitors pick one of the fixed libraries in `src/mutagent/presets.py`. They never upload code or write prompts. Libraries that take about a minute per scoring pass are recording-only.
- **Limits (see `.env.example`):** one live run at a time, a token budget per run, a cap on live runs per day.

Container image (deployed on Render; the Dockerfile listens on `$PORT`, default 8000): `docker build -t mutagent .`, then `docker run -p 8000:8000 --env-file .env mutagent`. It runs as an unprivileged user with `MUTAGENT_LIMITS=1`.

## Use it in CI

`mutagent score` needs no API key, so it can gate a pull request: it fails (exit code 4) when the tests are too weak.

```yaml
# .github/workflows/mutation-gate.yml
name: mutation-gate
on: [pull_request]
jobs:
  gate:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.13" }
      - run: pip install pytest "git+https://github.com/Sarmax04/mutagent"   # use your repo URL
      - run: mutagent score --project . --target src/yourpkg/module.py --min-score 85
```

To let the agent strengthen the tests, run `mutagent run ... --min-score 85` with `NEBIUS_API_KEY` set as a secret, then turn the saved JSON into a readable report with `mutagent report mutagent_report.json --out report.md`. The web demo also has a "Download test file" button.

### Try it on your own project (web)

```
mutagent serve --local
```

Open http://127.0.0.1:8000 and choose "Your own code". Type the folder of a Python project, press "Find modules", pick a module and the test files that count, and start the run. The page shows the planted bugs, the score after every round, the bugs that got away and the new tests, with buttons to copy or download them. This panel needs your `.env` with a Nebius key, only works on `127.0.0.1`, and never writes into your project. It is not part of the hosted demo.

### Check a whole project

```
mutagent scan --project . --discover --tests tests/test_app.py
mutagent scan --project . --targets src/app/a.py src/app/b.py --min-score 85
mutagent scan --project . --discover --min-score 85 --fix
```

`scan` scores every module (no API key needed) and prints a table with the weakest module last. With `--min-score` it exits with code 4 if any module is below the line. With `--fix` the agent then works on each weak module. When it improves the score it writes `tests/test_mutagent_<module>.py`, and it always saves a JSON report per module in `mutagent_reports/`.

## Results

Real runs against Nebius Token Factory (NVIDIA Nemotron models), 3 rounds at most. Each row compares the survivor-guided agent with the baseline run (same loop, same model, no bug feedback). Recorded in `replays/` and playable in the web demo.

| Target | Mutants | Score before | No bug feedback (same model) | Agent (guided) | Rounds | Tokens (agent) |
|---|---|---|---|---|---|---|
| `examples/demo_project/pricing.py` | 37 | 37.8% | 94.6% (6,241 tokens) | 91.9% | 3 | 17,451 |
| cachetools `func.py` | 55 | 89.1% | 96.4% (27,928 tokens) | 98.2% | 3 | 23,130 |
| python-slugify `slugify.py` | 118 | 54.2% | 74.6% (41,841 tokens) | 88.1% | 3 | 64,871 |
| inflection `__init__.py` | 108 | 88.9% | 88.9% (53,362 tokens) | 95.4% | 3 | 39,214 |

The guided agent beat the baseline on three of four targets (cachetools +1.8 points, inflection +6.5, slugify +13.5). On the small pricing module the baseline won (94.6% vs 91.9%) and used far fewer tokens. Each number is one run of a non-deterministic model, so treat gaps of a few points as noise. The mutants that stay alive are mostly equivalent or hard to reach; I did not verify each one by hand. The baseline on inflection failed because the model invented an import that does not exist, which is a real weakness of working without seeing the code's surroundings.

Failures I hit while getting the first runs working, and what changed:

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
