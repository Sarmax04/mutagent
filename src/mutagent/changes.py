"""Which lines of a module changed: for mutating only what a pull request touched.

``--diff origin/main`` plants bugs only on the lines that differ from the merge base
with ``origin/main`` (uncommitted edits included). A CI gate then checks the new code
in seconds instead of re-scoring whole modules. A file git does not track yet counts
as entirely new.

Only the first line of a statement counts: a change in the continuation line of a
long expression does not select that expression's mutants.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)


class DiffError(RuntimeError):
    """git could not compare against the given reference."""


def _git(project: Path, *args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", "-C", str(project), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DiffError(f"could not run git: {exc}") from exc


def parse_hunks(diff_text: str) -> set[int]:
    """New-side line numbers covered by the hunks of a ``--unified=0`` diff."""
    lines: set[int] = set()
    for match in _HUNK.finditer(diff_text):
        start = int(match.group(1))
        count = int(match.group(2)) if match.group(2) is not None else 1
        lines.update(range(start, start + count))
    return lines


def changed_lines(project: Path, target: str, ref: str) -> set[int] | None:
    """Changed line numbers of ``target`` compared with ``ref``; None means "the whole file is new"."""
    project = Path(project)
    tracked = _git(project, "ls-files", "--error-unmatch", "--", target)
    if tracked.returncode != 0:
        inside = _git(project, "rev-parse", "--is-inside-work-tree")
        if inside.returncode != 0:
            raise DiffError(f"{project} is not inside a git repository, so --diff has nothing to compare")
        return None
    result = _git(project, "diff", "--unified=0", "--no-color", "--no-ext-diff", "--merge-base", ref, "--", target)
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip().splitlines()
        hint = " (in GitHub Actions, check out with fetch-depth: 0)" if "unknown revision" in " ".join(message) or "bad revision" in " ".join(message) else ""
        raise DiffError(f"git diff against {ref} failed: {message[-1] if message else 'unknown error'}{hint}")
    return parse_hunks(result.stdout)
