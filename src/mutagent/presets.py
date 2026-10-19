"""Fixed demo targets for the hosted app. Visitors choose a preset; they never upload code."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Preset:
    id: str
    title: str
    blurb: str
    project: str  # folder under the examples directory
    target: str  # module under test, relative to the project folder
    tests: tuple[str, ...]  # test files to run ("" = everything pytest finds)
    live: bool = True  # False: too slow to score on a small server, so recording only
    packages: tuple[str, ...] = ()  # extra pip packages the tests need inside a sandbox


PRESETS: tuple[Preset, ...] = (
    Preset(
        id="cachetools-func",
        title="cachetools: func.py",
        blurb=(
            "The memoizing decorators of a popular MIT-licensed library. "
            "Its own tests never check the default settings."
        ),
        project="cachetools_demo",
        target="src/cachetools/func.py",
        tests=("tests/test_func.py",),
    ),
    Preset(
        id="pricing-toy",
        title="Toy pricing module",
        blurb="A tiny module with deliberately weak tests. Quick to run, so it suits a first look.",
        project="demo_project",
        target="pricing.py",
        tests=(),
    ),
    Preset(
        id="inflection",
        title="inflection: word forms",
        blurb=(
            "Plural, singular and case conversion helpers from a small MIT-licensed library. "
            "A real library whose tests still miss bugs. Recording only: scoring it takes about a minute per round."
        ),
        project="inflection_demo",
        target="inflection/__init__.py",
        tests=("test_inflection.py",),
        live=False,
    ),
    Preset(
        id="slugify",
        title="python-slugify: slugs",
        blurb=(
            "Turns text into URL slugs. Uses two of the library's own test files, so there is room to improve. "
            "Recording only: scoring it takes about a minute per round."
        ),
        project="slugify_demo",
        target="slugify/slugify.py",
        tests=("tests/test_legacy.py", "tests/test_modern_word_boundary.py"),
        live=False,
        packages=("text-unidecode",),
    ),
)

_BY_ID = {preset.id: preset for preset in PRESETS}


def get_preset(preset_id: str) -> Preset | None:
    return _BY_ID.get(preset_id)


def repo_root() -> Path:
    """The repository folder (this file lives in <repo>/src/mutagent/)."""
    return Path(__file__).resolve().parents[2]


def examples_dir() -> Path:
    return Path(os.environ.get("MUTAGENT_EXAMPLES_DIR") or repo_root() / "examples")


def replays_dir() -> Path:
    return Path(os.environ.get("MUTAGENT_REPLAYS_DIR") or repo_root() / "replays")


def project_path(preset: Preset) -> Path:
    return examples_dir() / preset.project
