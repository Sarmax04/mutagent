# Vendored third-party code

This folder contains an **unmodified** copy of parts of [python-slugify](https://github.com/un33k/python-slugify)
(MIT License, see `LICENSE` in this folder), used only as a demonstration target for mutagent.

- Source: PyPI source distribution `python-slugify` 9.1.3 (https://pypi.org/project/python-slugify/9.1.3/)
- Copied: the `slugify/` package, `tests/__init__.py`, `tests/test_legacy.py`, `tests/test_modern_word_boundary.py`
- Only those two test files are used, on purpose: the other upstream test files check the command line and the release, not the slug logic. Scores here therefore describe those two files, not the whole upstream suite.
- Needs `text-unidecode` (installed with `pip install -e ".[dev]"` or `".[examples]"`).
- Not part of mutagent's own code; the MIT license of the mutagent repo does not replace the one in this folder.

Try it:

    mutagent score --project examples/slugify_demo --target slugify/slugify.py --tests tests/test_legacy.py tests/test_modern_word_boundary.py
