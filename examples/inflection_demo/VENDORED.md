# Vendored third-party code

This folder contains an **unmodified** copy of parts of [inflection](https://github.com/jpvanhal/inflection)
(MIT License, see `LICENSE` in this folder), used only as a demonstration target for mutagent.

- Source: PyPI source distribution `inflection` 0.5.1 (https://pypi.org/project/inflection/0.5.1/)
- Copied: `inflection/__init__.py`, `test_inflection.py`
- Not part of mutagent's own code; the MIT license of the mutagent repo does not replace the one in this folder.

Try it:

    mutagent score --project examples/inflection_demo --target inflection/__init__.py --tests test_inflection.py
