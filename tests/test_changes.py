import shutil
import subprocess

import pytest

from mutagent.changes import DiffError, changed_lines, parse_hunks
from mutagent.cli import main
from mutagent.loop import measure
from mutagent.scan import scan

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

MODULE = (
    "def gt(a, b):\n"
    "    return a > b\n"
    "\n"
    "\n"
    "def add(a, b):\n"
    "    return a + b\n"
)


def git(root, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "init.defaultBranch=main", *args],
        cwd=root, check=True, capture_output=True,
    )


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "calc.py").write_text(MODULE, encoding="utf-8")
    (tmp_path / "other.py").write_text("def one():\n    return 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text(
        "from calc import add, gt\nfrom other import one\n\n"
        "def test_it():\n    assert gt(2, 1)\n    assert add(1, 1) == 2\n    assert one() == 1\n",
        encoding="utf-8",
    )
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "start")
    git(tmp_path, "branch", "base")
    return tmp_path


def test_parse_hunks_reads_new_side_lines():
    text = "@@ -1,2 +1,3 @@\n+a\n@@ -10 +12 @@\n-x\n+y\n@@ -20,3 +23,0 @@\n"
    assert parse_hunks(text) == {1, 2, 3, 12}


def test_changed_lines_follow_edits_and_new_files(repo):
    assert changed_lines(repo, "calc.py", "base") == set()
    (repo / "calc.py").write_text(MODULE.replace("return a + b", "return a + b + 0"), encoding="utf-8")
    assert changed_lines(repo, "calc.py", "base") == {6}
    (repo / "new.py").write_text("x = 1\n", encoding="utf-8")
    assert changed_lines(repo, "new.py", "base") is None  # untracked: the whole file is new
    with pytest.raises(DiffError):
        changed_lines(repo, "calc.py", "no-such-branch")


def test_only_changed_lines_get_planted_bugs(repo):
    (repo / "calc.py").write_text(MODULE.replace("return a + b", "return a + b + 0"), encoding="utf-8")
    every, _ = measure(repo, "calc.py")
    lines = changed_lines(repo, "calc.py", "base")
    some, statuses = measure(repo, "calc.py", lines=lines)
    assert 0 < len(some) < len(every) and {m.lineno for m in some} == {6}
    assert set(statuses) == {m.id for m in some}


def test_scan_and_score_with_diff(repo, capsys):
    (repo / "calc.py").write_text(MODULE.replace("return a + b", "return a + b + 0"), encoding="utf-8")
    rows = scan(repo, ["calc.py", "other.py"], log=lambda _m: None, diff="base")
    assert rows[0].mutants > 0 and rows[1].score is None and "changed lines" in rows[1].note
    assert main(["scan", "--project", str(repo), "--discover", "--diff", "base"]) == 0
    out = capsys.readouterr().out
    assert "calc.py" in out and "other.py" not in out
    assert main(["score", "--project", str(repo), "--target", "other.py", "--diff", "base"]) == 0
    assert "nothing to check" in capsys.readouterr().out
    assert main(["score", "--project", str(repo), "--target", "calc.py", "--diff", "base"]) == 0
    assert "lines changed since base" in capsys.readouterr().out
    assert main(["score", "--project", str(repo), "--target", "calc.py", "--diff", "nope"]) == 2


def test_scan_without_changes_is_a_quick_pass(repo, capsys):
    assert main(["scan", "--project", str(repo), "--discover", "--diff", "base"]) == 0
    assert "nothing to check" in capsys.readouterr().out
