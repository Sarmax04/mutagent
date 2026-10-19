"""Run the test suites inside Nebius Token Factory Sandboxes instead of on this machine.

The agent executes model-written test code. Locally that happens in a subprocess with a
timeout, which is not real isolation. With ``--sandbox`` every pytest run (the baseline,
the new tests and one run per planted bug) happens in an isolated, on-demand environment
in Token Factory, away from your own machine, and the mutants run in parallel.

How it works:

1. One image is prepared per run: a Python base image, ``pip install pytest coverage``
   (plus any extra packages the project needs), and a snapshot of the project in ``/work``.
2. Every later run starts from that image and uploads only the files that differ from the
   snapshot (the mutated module, the generated tests), then runs pytest.
3. An infrastructure problem (network, quota, a failed operation) raises ``SandboxError``.
   It is never counted as a caught bug, so a broken sandbox cannot inflate a score.

Needs ``pip install contree-sdk`` and ``NEBIUS_API_KEY`` + ``NEBIUS_PROJECT_ID``. Sandboxes
is a Token Factory beta feature that must be activated for the project first.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shlex
import tarfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from typing import Callable, Sequence

from .linecov import percent_for
from .mutation import Mutant, TestRun

DEFAULT_IMAGE = "python:3.12-slim"
WORK = "/work"
MAX_PROJECT_BYTES = 25 * 1024 * 1024
MAX_DIFF_FILES = 25  # more changed files than this: take a fresh snapshot instead
MAX_WORKERS = 40  # the beta allows 50 concurrent operations
SKIP_DIRS = {"__pycache__", ".git", ".venv", "venv", ".pytest_cache", ".mypy_cache", "node_modules"}
SKIP_SUFFIXES = (".pyc", ".pyo")
SKIP_NAMES = {".coverage", "mutagent.coverage"}
COVERAGE_MARKER = "MUTAGENT_COVERAGE_JSON"
_ENV = "PYTHONPATH=/work:/work/src PYTHONDONTWRITEBYTECODE=1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8"


class SandboxError(RuntimeError):
    """The sandbox could not run something (not a test result)."""


def unavailable_reason() -> str | None:
    """Why sandboxes cannot be used right now, or None when everything needed is present."""
    try:
        import contree_sdk  # noqa: F401
    except ImportError:
        return 'the contree-sdk package is missing. Install it with: pip install contree-sdk (or pip install -e ".[sandbox]")'
    missing = [name for name in ("NEBIUS_API_KEY", "NEBIUS_PROJECT_ID") if not os.environ.get(name, "").strip()]
    if missing:
        return (
            f"{' and '.join(missing)} {'is' if len(missing) == 1 else 'are'} not set. Put them in .env "
            "(the project ID is on https://tokenfactory.nebius.com/project/api-keys)."
        )
    return None


def _collect(work: Path) -> dict[str, bytes]:
    """Every file of the project copy, by posix path relative to ``work``."""
    files: dict[str, bytes] = {}
    total = 0
    for path in sorted(Path(work).rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(work)
        if any(p in SKIP_DIRS or p.endswith(".egg-info") for p in rel.parts[:-1]):
            continue
        if path.name in SKIP_NAMES or path.name.endswith(SKIP_SUFFIXES):
            continue
        data = path.read_bytes()
        total += len(data)
        if total > MAX_PROJECT_BYTES:
            raise SandboxError(f"the project is larger than {MAX_PROJECT_BYTES // (1024 * 1024)} MB; point --project at a smaller folder")
        files[rel.as_posix()] = data
    return files


def _tarball(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pytest_command(tests: Sequence[str], timeout: float, fail_fast: bool, removed: Sequence[str] = ()) -> str:
    """The shell line that runs pytest in ``/work`` with a hard time limit."""
    rm = ("rm -f " + " ".join(shlex.quote(f"{WORK}/{p}") for p in removed) + "; ") if removed else ""
    flags = "-x " if fail_fast else ""
    chosen = (" " + " ".join(shlex.quote(t) for t in tests)) if tests else ""
    return f"cd {WORK} && {rm}{_ENV} timeout -k 2 {max(1, int(timeout))} python -m pytest {flags}-q -p no:cacheprovider{chosen}"


class SandboxRunner:
    """Runs pytest for the agent loop inside Token Factory Sandboxes.

    ``run_tests``, ``evaluate`` and ``coverage`` mirror the local functions, so the loop
    can use either. ``client`` is a ``contree_sdk.ContreeSync`` (a stand-in in tests).
    """

    def __init__(
        self,
        client=None,
        *,
        image: str | None = None,
        packages: Sequence[str] = (),
        workers: int | None = None,
        log: Callable[[str], None] = print,
    ) -> None:
        if client is None:
            reason = unavailable_reason()
            if reason:
                raise SandboxError(reason)
            from contree_sdk import ContreeSync

            client = ContreeSync()
        self.client = client
        self.image = image or os.environ.get("MUTAGENT_SANDBOX_IMAGE", "").strip() or DEFAULT_IMAGE
        self.packages = tuple(packages)
        wanted = workers or int(os.environ.get("MUTAGENT_SANDBOX_WORKERS", "").strip() or 16)
        self.workers = max(1, min(wanted, MAX_WORKERS))
        self.log = log
        self.runs = 0
        self.cost = 0.0
        self._lock = threading.RLock()
        self._prepared = None  # image with pytest installed
        self._snapshot = None  # image with pytest and the project
        self._manifest: dict[str, str] = {}

    # ---- image preparation ----

    def _run(self, image, shell: str, *, files=None, disposable: bool, timeout: float):
        """One sandbox run with a retry; infrastructure errors become SandboxError."""
        last: Exception | None = None
        for attempt in range(3):
            try:
                done = image.run(shell=shell, files=files or None, disposable=disposable, timeout=timeout).wait()
                self.runs += 1
                try:
                    self.cost += float(done.result.cost)
                except Exception:  # noqa: BLE001 - cost reporting is optional
                    pass
                return done
            except Exception as exc:  # noqa: BLE001 - the SDK raises its own error types
                last = exc
                time.sleep(1.5 * (attempt + 1))
        raise SandboxError(f"the sandbox failed: {type(last).__name__}: {last}") from last

    def _prepare(self):
        with self._lock:
            if self._prepared is None:
                self.log(f"sandbox: preparing {self.image} with pytest ...")
                base = self.client.images.use(self.image)
                packages = " ".join(shlex.quote(p) for p in ("pytest", "coverage", *self.packages))
                done = self._run(
                    base, f"pip install -q --disable-pip-version-check {packages}", disposable=False, timeout=900
                )
                if done.exit_code != 0:
                    raise SandboxError("installing pytest in the sandbox failed:\n" + (done.stderr or done.stdout or "")[-1500:])
                self._prepared = done
            return self._prepared

    def _take_snapshot(self, files: dict[str, bytes]) -> None:
        with self._lock:
            prepared = self._prepare()
            self.log(f"sandbox: uploading the project ({len(files)} files) ...")
            done = self._run(
                prepared,
                f"mkdir -p {WORK} && tar xzf /tmp/project.tgz -C {WORK}",
                files={"/tmp/project.tgz": _tarball(files)},
                disposable=False,
                timeout=300,
            )
            if done.exit_code != 0:
                raise SandboxError("unpacking the project in the sandbox failed:\n" + (done.stderr or done.stdout or "")[-1500:])
            self._snapshot = done
            self._manifest = {name: _digest(data) for name, data in files.items()}

    def _plan(self, work: Path) -> tuple[dict[str, bytes], list[str]]:
        """Files that differ from the snapshot (to upload) and files that vanished (to delete)."""
        files = _collect(work)
        with self._lock:
            if self._snapshot is None:
                self._take_snapshot(files)
            changed = {n: d for n, d in files.items() if self._manifest.get(n) != _digest(d)}
            removed = [n for n in self._manifest if n not in files]
            if len(changed) + len(removed) > MAX_DIFF_FILES:
                self._take_snapshot(files)
                changed, removed = {}, []
        return changed, removed

    # ---- the three operations the loop needs ----

    def _pytest(self, changed: dict[str, bytes], removed: list[str], tests, timeout: float, fail_fast: bool) -> TestRun:
        shell = pytest_command(tests, timeout, fail_fast, removed)
        done = self._run(
            self._snapshot,
            shell,
            files={f"{WORK}/{name}": data for name, data in changed.items()},
            disposable=True,
            timeout=timeout + 240,
        )
        code = done.exit_code
        if code in (124, 137):  # killed by `timeout`
            return TestRun(passed=False, timed_out=True, returncode=None, output="timeout")
        output = ((done.stdout or "") + (done.stderr or ""))[-4000:]
        return TestRun(passed=code == 0, timed_out=False, returncode=code, output=output)

    def run_tests(self, workdir: Path, timeout: float = 30.0, tests: Sequence[str] = (), fail_fast: bool = True) -> TestRun:
        changed, removed = self._plan(Path(workdir))
        return self._pytest(changed, removed, tests, timeout, fail_fast)

    def evaluate(
        self,
        work: Path,
        target: str,
        mutants: list[Mutant],
        original_source: str,
        timeout: float = 20.0,
        tests: Sequence[str] = (),
    ) -> dict[int, str]:
        """Run the suite against every mutant, in parallel. Returns id -> killed|survived|timeout."""
        changed, removed = self._plan(Path(work))
        target_name = Path(target).as_posix()
        done_count = 0
        lock = threading.Lock()

        def one(mutant: Mutant) -> tuple[int, str]:
            nonlocal done_count
            files = {**changed, target_name: mutant.source.encode("utf-8")}
            result = self._pytest(files, removed, tests, timeout, True)
            if result.timed_out:
                status = "timeout"
            elif result.passed or result.returncode == 5:  # 5 = no tests collected
                status = "survived"
            else:
                status = "killed"
            with lock:
                done_count += 1
                if done_count % 20 == 0 or done_count == len(mutants):
                    self.log(f"sandbox: {done_count} of {len(mutants)} mutants tested")
            return mutant.id, status

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            return dict(pool.map(one, mutants))

    def coverage(self, work: Path, target: str, tests: Sequence[str] = (), timeout: float = 120.0) -> float | None:
        """Line coverage (percent) of ``target``, measured in the sandbox. None if unavailable."""
        if os.environ.get("MUTAGENT_NO_COVERAGE") == "1":
            return None
        try:
            changed, removed = self._plan(Path(work))
            rm = ("rm -f " + " ".join(shlex.quote(f"{WORK}/{p}") for p in removed) + "; ") if removed else ""
            chosen = (" " + " ".join(shlex.quote(t) for t in tests)) if tests else ""
            summary = (
                "import json; d = json.load(open('/tmp/cov.json')); "
                "print(json.dumps({k: {'summary': v['summary']} for k, v in d['files'].items()}))"
            )
            shell = (
                f"cd {WORK} && {rm}export COVERAGE_FILE=/tmp/mutagent.coverage && "
                f"{_ENV} timeout -k 2 {int(timeout)} python -m coverage run -m pytest -q -p no:cacheprovider{chosen} >/dev/null 2>&1; "
                f"python -m coverage json -q -o /tmp/cov.json >/dev/null 2>&1 && echo {COVERAGE_MARKER} && python -c {shlex.quote(summary)}"
            )
            done = self._run(
                self._snapshot, shell,
                files={f"{WORK}/{n}": d for n, d in changed.items()}, disposable=True, timeout=timeout + 240,
            )
            text = done.stdout or ""
            if COVERAGE_MARKER not in text:
                return None
            data = json.loads(text.split(COVERAGE_MARKER, 1)[1].strip().splitlines()[0])
        except (SandboxError, ValueError, IndexError):
            return None
        return percent_for({"cwd": WORK, "files": data}, PurePosixPath(WORK) / Path(target).as_posix())

    # ---- smoke test ----

    def check(self) -> list[str]:
        """A tiny end-to-end check for ``mutagent sandbox-check``. Returns report lines."""
        started = time.time()
        lines: list[str] = []
        files = {
            "calc.py": b"def add(a, b):\n    return a + b\n",
            "tests/test_calc.py": b"from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        }
        with self._lock:
            self._take_snapshot(files)
        lines.append(f"image ready ({time.time() - started:.0f}s): {self.image} with pytest and coverage")
        passing = self._pytest({}, [], ["tests/test_calc.py"], 30, True)
        lines.append(f"tests on the real code: {'passed' if passing.passed else 'FAILED'}")
        broken = self._pytest({"calc.py": b"def add(a, b):\n    return a - b\n"}, [], ["tests/test_calc.py"], 30, True)
        lines.append(f"tests on a planted bug: {'caught (good)' if not broken.passed else 'NOT caught (bad)'}")
        if not passing.passed:
            lines.append(passing.output.strip()[-600:])
        lines.append(f"{self.runs} sandbox runs, {time.time() - started:.0f}s in total")
        if self.cost:
            lines.append(f"reported cost: {self.cost:.4f} (the unit is whatever the sandbox API reports)")
        return lines
