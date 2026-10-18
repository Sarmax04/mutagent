"""Line coverage of one module, to put next to its mutation score.

Coverage says which lines the tests *run*. The mutation score says whether the tests
would *notice* a bug on those lines. The two often disagree: tests can run every line
of a module and still catch only a third of the planted bugs. mutagent reports both,
so that gap is visible.

Measured with coverage.py. Everything here is best effort: if coverage.py is missing or
fails, the result is ``None`` and the rest of the run is unaffected.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from .mutation import copy_project


def percent_for(report: dict, target_path: Path) -> float | None:
    """Line coverage in percent for ``target_path`` from a ``coverage json`` report."""
    wanted = os.path.realpath(target_path)
    for name, data in report.get("files", {}).items():
        if os.path.realpath(name) == wanted or os.path.realpath(Path(report.get("cwd", ".")) / name) == wanted:
            summary = data.get("summary", {})
            statements = summary.get("num_statements", 0)
            if not statements:
                return None
            return round(100.0 * summary.get("covered_lines", 0) / statements, 1)
    return None


def measure_coverage(work: Path, target: str, tests: Sequence[str] = (), timeout: float = 120.0) -> float | None:
    """Line coverage (percent) of ``target`` when ``tests`` run, inside the prepared copy ``work``.

    Returns None when coverage.py is not installed, the run times out, or the module
    was never imported by the tests.
    """
    if os.environ.get("MUTAGENT_NO_COVERAGE") == "1":
        return None
    work = Path(work)
    data_file = work.parent / "mutagent.coverage"
    json_file = work.parent / "mutagent-coverage.json"
    paths = [str(work), str(work / "src")]
    if os.environ.get("PYTHONPATH"):
        paths.append(os.environ["PYTHONPATH"])
    env = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": os.pathsep.join(paths),
        "COVERAGE_FILE": str(data_file),
    }
    run = [sys.executable, "-m", "coverage", "run", "-m", "pytest", "-q", "-p", "no:cacheprovider", *tests]
    try:
        subprocess.run(run, cwd=work, env=env, capture_output=True, timeout=timeout)
        if not data_file.exists():
            return None
        subprocess.run(
            [sys.executable, "-m", "coverage", "json", "-q", "-o", str(json_file)],
            cwd=work, env=env, capture_output=True, timeout=timeout,
        )
        report = json.loads(json_file.read_text(encoding="utf-8"))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    report["cwd"] = str(work)
    return percent_for(report, work / target)


def project_coverage(project: Path, target: str, tests: Sequence[str] = (), timeout: float = 120.0) -> float | None:
    """Like ``measure_coverage`` but on a throwaway copy of ``project``."""
    work = copy_project(Path(project).resolve())
    try:
        return measure_coverage(work, target, tests, timeout)
    finally:
        shutil.rmtree(work.parent, ignore_errors=True)
