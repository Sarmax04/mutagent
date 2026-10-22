# Vendored third-party code

This folder contains an **unmodified** copy of parts of [cachetools](https://github.com/tkem/cachetools)
(MIT License, see `LICENSE` in this folder), used only as a demonstration target for mutagent.

- Source: https://github.com/tkem/cachetools (published on PyPI as https://pypi.org/project/cachetools/)
- Commit: be68aedd0e5ae7f803d541d189636cdfbc532d95
- Copied: `src/cachetools/` (the package), `tests/__init__.py`, `tests/test_func.py`
- Not part of mutagent's own code; the MIT license of the mutagent repo does not replace the one in this folder.

Try it:

    mutagent score --project examples/cachetools_demo --target src/cachetools/func.py --tests tests/test_func.py
