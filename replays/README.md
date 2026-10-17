# Recorded runs

The hosted demo's "Recorded run" mode plays back JSON reports from this folder, one file per preset, plus an optional comparison file:

| Preset | Agent run | No-feedback comparison |
|---|---|---|
| cachetools: func.py | `cachetools-func.json` | `cachetools-func-baseline.json` |
| Toy pricing module | `pricing-toy.json` | `pricing-toy-baseline.json` |
| inflection: word forms | `inflection.json` | `inflection-baseline.json` |
| python-slugify: slugs | `slugify.json` | `slugify-baseline.json` |

The baseline file is the comparison: the same loop, model and acceptance rule, but the model is never told which bugs the current tests miss. The page shows both scores side by side.

Record everything from **real** runs (never hand-edit a recording or fill it from a fake model, because judges will read it as evidence):

```bash
python scripts/record.py                  # all four presets, agent run + baseline (up to 6 rounds)
python scripts/record.py cachetools-func  # just one preset
python scripts/record.py --triage         # also let the small FAST model set aside likely-unkillable bugs
```

Run it from the repository root with `.env` filled in. The slow presets (`inflection`, `slugify`) take several minutes because every round re-scores more than a hundred planted bugs.

Each report holds the score per round, which planted bugs were still alive after each round, the tests the agent kept, token counts, the small model's one-line explanation of each bug that survived, the bugs it set aside as unkillable (only with `--triage`) and the model names.
