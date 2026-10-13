# Recorded runs

The hosted demo's "Recorded run" mode plays back JSON reports from this folder, one file per preset:

| Preset | File |
|---|---|
| cachetools: func.py | `cachetools-func.json` |
| Toy pricing module | `pricing-toy.json` |

Record one from a **real** run (never hand-edit a recording or fill it from a fake model, because judges will read it as evidence):

```bash
mutagent run --project examples/cachetools_demo --target src/cachetools/func.py \
  --tests tests/test_func.py --rounds 3 --report replays/cachetools-func.json

mutagent run --project examples/demo_project --target pricing.py \
  --rounds 3 --report replays/pricing-toy.json
```

Each report holds the score per round, which planted bugs were still alive after each round, the tests the agent kept, token counts and the model names.
