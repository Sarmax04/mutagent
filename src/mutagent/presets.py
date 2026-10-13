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
