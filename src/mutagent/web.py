"""Hosted demo: pick a preset, watch a recorded run, or start a capped live run.

Safety model (see also ``safety.py``):
  * visitors choose from fixed presets; they never submit code or prompts
  * one live run at a time, a per-run token budget and a per-day run cap
  * model-written tests are screened before they run, and run under resource limits
    when ``MUTAGENT_LIMITS=1`` is set
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .llm import ChatModel, ConfigError, api_error_message, NebiusChat, Settings, TokenBudget, load_dotenv
from .docs import DocsUnavailable
from .loop import BaselineFailing, RoundResult, module_name, run_best_of
from .presets import PRESETS, Preset, get_preset, project_path, replays_dir
from .scan import discover_targets
from .safety import screen_test_code
from .sandbox import SandboxError, SandboxRunner, unavailable_reason as sandbox_unavailable

log = logging.getLogger("mutagent.web")
STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_STORED_RUNS = 20
LOCAL_MAX_ROUNDS = 8
LOCAL_MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class Limits:
    max_rounds: int = 3
    run_token_budget: int = 60_000
    max_runs_per_day: int = 30
    test_timeout: float = 20.0

    @classmethod
    def from_env(cls) -> "Limits":
        def read(name: str, default, cast):
            raw = os.environ.get(name, "").strip()
            return cast(raw) if raw else default

        return cls(
            max_rounds=read("MUTAGENT_MAX_ROUNDS", cls.max_rounds, int),
            run_token_budget=read("MUTAGENT_RUN_TOKEN_BUDGET", cls.run_token_budget, int),
            max_runs_per_day=read("MUTAGENT_MAX_RUNS_PER_DAY", cls.max_runs_per_day, int),
            test_timeout=read("MUTAGENT_TEST_TIMEOUT", cls.test_timeout, float),
        )


class LiveUnavailable(RuntimeError):
    pass


class Busy(RuntimeError):
    pass


class DailyLimit(RuntimeError):
    pass


@dataclass
class RunState:
    id: str
    preset: str
    rounds_requested: int
    status: str = "queued"  # queued | running | done | error
    rounds: list[dict] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    report: dict | None = None
    error: str | None = None

    def add_round(self, result: RoundResult) -> None:
        self.rounds.append(
            {
                "round": result.round,
                "score": result.score,
                "killed": result.killed,
                "survived": result.survived,
                "accepted": result.accepted,
                "note": result.note,
                "tokens_used": result.tokens_used,
                "survivors": list(result.survivors),
            }
        )

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "preset": self.preset,
            "status": self.status,
            "rounds_requested": self.rounds_requested,
            "rounds": list(self.rounds),
            "log": list(self.log),
            "report": self.report,
            "error": self.error,
        }


class RunManager:
    """Owns the live-run slot, the daily counter and recent run states."""

    def __init__(
        self,
        llm_factory: Callable[[], ChatModel] | None,
        limits: Limits,
        models: dict[str, str] | None = None,
        local_factory: Callable[[], ChatModel] | None = None,
        sandbox_factory: Callable[..., SandboxRunner] | None = None,
        sandbox_always: bool = False,
    ) -> None:
        self._factory = llm_factory
        self._local_factory = local_factory
        self._sandbox_factory = sandbox_factory  # packages, log -> SandboxRunner
        self._sandbox_always = sandbox_always and sandbox_factory is not None
        self.limits = limits
        self.models = models or {}
        self._lock = threading.Lock()
        self._runs: "OrderedDict[str, RunState]" = OrderedDict()
        self._active: str | None = None
        self._day = datetime.now(timezone.utc).date()
        self._count = 0

    @property
    def live_enabled(self) -> bool:
        return self._factory is not None

    @property
    def local_enabled(self) -> bool:
        return self._local_factory is not None

    def _roll_day(self) -> None:  # caller holds the lock
        today = datetime.now(timezone.utc).date()
        if today != self._day:
            self._day, self._count = today, 0

    def status(self) -> dict:
        with self._lock:
            self._roll_day()
            return {
                "live": self.live_enabled,
                "busy": self._active is not None,
                "max_rounds": self.limits.max_rounds,
                "runs_left_today": max(self.limits.max_runs_per_day - self._count, 0),
                "run_token_budget": self.limits.run_token_budget,
                "models": self.models,
                "local": self.local_enabled,
                "docs_available": self.local_enabled and bool(os.environ.get("TAVILY_API_KEY", "").strip()),
                "sandbox": self._sandbox_always,
                "sandbox_available": self.local_enabled and self._sandbox_factory is not None,
            }

    def get(self, run_id: str) -> RunState | None:
        with self._lock:
            return self._runs.get(run_id)

    def start(self, preset: Preset, rounds: int) -> RunState:
        if self._factory is None or not preset.live:
            raise LiveUnavailable()
        rounds = max(1, min(rounds, self.limits.max_rounds))
        with self._lock:
            self._roll_day()
            if self._active is not None:
                raise Busy()
            if self._count >= self.limits.max_runs_per_day:
                raise DailyLimit()
            state = RunState(id=uuid4().hex[:12], preset=preset.id, rounds_requested=rounds)
            self._count += 1
            self._active = state.id
            self._runs[state.id] = state
            while len(self._runs) > MAX_STORED_RUNS:
                self._runs.popitem(last=False)
        threading.Thread(target=self._work, args=(state, preset), daemon=True).start()
        return state

    def start_local(
        self,
        project: Path,
        target: str,
        tests: list[str],
        rounds: int,
        docs: bool = False,
        attempts: int = 1,
        sandbox: bool = False,
        packages: list[str] | None = None,
    ) -> RunState:
        """A run on the person's own project. Only offered by ``mutagent serve --local``."""
        if self._local_factory is None:
            raise LiveUnavailable()
        rounds = max(1, min(rounds, LOCAL_MAX_ROUNDS))
        with self._lock:
            if self._active is not None:
                raise Busy()
            state = RunState(id=uuid4().hex[:12], preset="local", rounds_requested=rounds)
            self._active = state.id
            self._runs[state.id] = state
            while len(self._runs) > MAX_STORED_RUNS:
                self._runs.popitem(last=False)
        job = (project, target, tests, docs, max(1, min(attempts, LOCAL_MAX_ATTEMPTS)), sandbox, packages or [])
        threading.Thread(target=self._work, args=(state, None, job), daemon=True).start()
        return state

    def _work(self, state: RunState, preset: Preset | None, job: tuple | None = None) -> None:
        try:
            docs_lookup, attempts, runner = None, 1, None
            want_sandbox, packages = self._sandbox_always, list(getattr(preset, "packages", ()) or ())
            if job is not None:
                make_llm = self._local_factory
                project, target, tests, want_docs, attempts, job_sandbox, job_packages = job
                want_sandbox = want_sandbox or job_sandbox
                packages = job_packages
                if want_docs:
                    from .docs import from_env

                    docs_lookup = from_env(project, module_name(target), log=state.log.append)
            else:
                make_llm = self._factory
                project, target, tests = project_path(preset), preset.target, preset.tests  # type: ignore[union-attr]
            state.status = "running"
            if want_sandbox:
                if self._sandbox_factory is None:
                    raise SandboxError(sandbox_unavailable() or "Sandboxes are not available on this server.")
                runner = self._sandbox_factory(packages, state.log.append)
                state.log.append("Tests run in isolated Nebius Token Factory Sandboxes.")

            def log_line(message: str) -> None:
                state.log.append(message)
                if re.fullmatch(r"attempt \d+ of \d+", message):
                    state.rounds.clear()  # a new attempt starts from the same baseline

            report = run_best_of(
                attempts,
                make_llm,  # type: ignore[arg-type]
                project,
                target,
                max_rounds=state.rounds_requested,
                test_timeout=self.limits.test_timeout,
                write_back=False,
                log=log_line,
                tests=tests,
                on_round=state.add_round,
                code_filter=screen_test_code,
                explain=True,
                docs=docs_lookup,
                runner=runner,
            )
            state.report = report.to_dict()
            state.status = "done"
        except (DocsUnavailable, SandboxError) as exc:
            state.error = str(exc)
            state.status = "error"
        except BaselineFailing:
            log.exception("the project fails its own tests")
            state.error = (
                "Your project's own tests fail before any bug is planted, so it can't be scored. "
                "Fix them first."
                if job is not None
                else "This preset's own tests fail on the server, so it can't be scored."
            )
            state.status = "error"
        except Exception as exc:  # noqa: BLE001 - report any failure to the visitor
            log.exception("live run %s failed", state.id)
            state.error = "The run stopped: " + (
                api_error_message(exc) or f"{type(exc).__name__}. See the server log for details."
            )
            state.status = "error"
        finally:
            with self._lock:
                self._active = None


class LocalRunRequest(BaseModel):
    project: str = Field(min_length=1, max_length=500)
    target: str = Field(min_length=1, max_length=300)
    tests: list[str] = Field(default_factory=list, max_length=20)
    rounds: int = Field(default=4, ge=1, le=20)
    docs: bool = False
    attempts: int = Field(default=1, ge=1, le=3)
    sandbox: bool = False
    packages: list[str] = Field(default_factory=list, max_length=10)


class LocalProjectRequest(BaseModel):
    project: str = Field(min_length=1, max_length=500)


def _inside(root: Path, relative: str) -> Path | None:
    """``root / relative`` as a resolved file path, or None if it escapes ``root``."""
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


class RunRequest(BaseModel):
    preset: str
    rounds: int = Field(default=2, ge=1, le=20)


def _live_from_env(
    limits: Limits,
) -> tuple[Callable[[], ChatModel] | None, dict[str, str], Callable[[], ChatModel] | None]:
    load_dotenv(Path(".env"))
    try:
        settings = Settings.from_env()
    except ConfigError:
        return None, {}, None
    budget = min(limits.run_token_budget, settings.token_budget)

    def factory() -> ChatModel:
        return NebiusChat(settings, TokenBudget(budget))

    def local_factory() -> ChatModel:  # your own key, your own machine: the .env budget applies
        return NebiusChat(settings, TokenBudget(settings.token_budget))

    return factory, {"fast": settings.model_fast, "strong": settings.model_strong}, local_factory


def create_app(
    llm_factory: Callable[[], ChatModel] | None = None,
    limits: Limits | None = None,
    models: dict[str, str] | None = None,
    auto_configure: bool = True,
    local: bool = False,
    local_factory: Callable[[], ChatModel] | None = None,
) -> FastAPI:
    """Build the app. Live runs are enabled when ``llm_factory`` is given, or (with
    ``auto_configure``) when the Nebius settings are present in the environment/.env."""
    limits = limits or Limits.from_env()
    if llm_factory is None and auto_configure:
        llm_factory, models, env_local = _live_from_env(limits)
        local_factory = local_factory or env_local
    sandbox_factory = None
    sandbox_always = os.environ.get("MUTAGENT_SANDBOX", "").strip() == "1"
    if sandbox_unavailable() is None:
        def sandbox_factory(packages, log_line):
            return SandboxRunner(packages=packages, log=log_line)
    elif sandbox_always:
        # The operator asked for isolated test runs. Without Sandboxes, run nothing.
        log.error("MUTAGENT_SANDBOX=1 but Sandboxes are not usable (%s): live runs are disabled", sandbox_unavailable())
        llm_factory, local_factory = None, None
    # The "your own code" panel exists only when asked for (serve --local), never on a hosted server.
    manager = RunManager(
        llm_factory, limits, models, local_factory if local else None,
        sandbox_factory=sandbox_factory, sandbox_always=sandbox_always,
    )

    app = FastAPI(title="MutAgent", docs_url=None, redoc_url=None)
    app.state.manager = manager

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        # no-cache: browsers must re-check the page after every deploy instead of showing an old copy
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/presets")
    def presets() -> list[dict]:
        return [
            {
                "id": p.id,
                "title": p.title,
                "blurb": p.blurb,
                "target": p.target,
                "has_replay": (replays_dir() / f"{p.id}.json").is_file(),
                "live": p.live,
            }
            for p in PRESETS
        ]

    @app.get("/api/summary")
    def summary() -> list[dict]:
        """Final scores of every recorded run, for the overview table."""
        rows = []
        for p in PRESETS:
            def load(name: str):
                path = replays_dir() / name
                try:
                    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
                except (OSError, ValueError):
                    return None

            agent, base = load(f"{p.id}.json"), load(f"{p.id}-baseline.json")
            if not agent:
                continue
            rows.append(
                {
                    "id": p.id,
                    "title": p.title,
                    "mutants": agent.get("mutant_count"),
                    "before": agent.get("initial_score"),
                    "agent": agent.get("final_score"),
                    "baseline": base.get("final_score") if base else None,
                    "tokens": agent.get("tokens_used"),
                    "cover_before": agent.get("initial_coverage"),
                    "cover_after": agent.get("final_coverage"),
                }
            )
        return rows

    @app.get("/api/status")
    def status() -> dict:
        return manager.status()

    @app.get("/api/replay/{preset_id}")
    def replay(preset_id: str) -> dict:
        if get_preset(preset_id) is None:
            raise HTTPException(404, "Unknown preset.")
        path = replays_dir() / f"{preset_id}.json"
        if not path.is_file():
            raise HTTPException(404, "There is no recording for this preset yet.")
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise HTTPException(500, "The recording file could not be read.")
        baseline = None
        try:  # optional comparison: the same loop without mutant feedback
            baseline_path = replays_dir() / f"{preset_id}-baseline.json"
            if baseline_path.is_file():
                baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            baseline = None
        return {"preset": preset_id, "report": report, "baseline": baseline}

    @app.post("/api/runs", status_code=202)
    def start_run(body: RunRequest) -> dict:
        preset = get_preset(body.preset)
        if preset is None:
            raise HTTPException(404, "Unknown preset.")
        try:
            state = manager.start(preset, body.rounds)
        except LiveUnavailable:
            raise HTTPException(
                503, "Live runs are switched off on this server. Watch the recording instead."
            )
        except Busy:
            raise HTTPException(429, "Another run is in progress. Try again in a minute.")
        except DailyLimit:
            raise HTTPException(429, "The daily limit for live runs is used up. Watch the recording instead.")
        return {"id": state.id}

    @app.post("/api/local/targets")
    def local_targets(body: LocalProjectRequest) -> dict:
        if not manager.local_enabled:
            raise HTTPException(404, "Not available on this server.")
        root = Path(body.project).expanduser().resolve()
        if not root.is_dir():
            raise HTTPException(400, f"{body.project} is not a folder on this computer.")
        targets = discover_targets(root)
        if not targets:
            raise HTTPException(400, "No Python modules were found in that folder.")
        tests = sorted(
            p.relative_to(root).as_posix()
            for p in root.rglob("test_*.py")
            if not any(part.startswith(".") or part in {"node_modules", "site-packages"} for part in p.parts)
        )
        return {"project": str(root), "targets": targets[:200], "tests": tests[:200]}

    @app.post("/api/local/runs", status_code=202)
    def start_local_run(body: LocalRunRequest) -> dict:
        if not manager.local_enabled:
            raise HTTPException(
                503, "Runs on your own code need `mutagent serve --local` and a configured .env."
            )
        root = Path(body.project).expanduser().resolve()
        if not root.is_dir():
            raise HTTPException(400, f"{body.project} is not a folder on this computer.")
        target = _inside(root, body.target)
        if target is None or not target.is_file() or target.suffix != ".py":
            raise HTTPException(400, "The module must be a .py file inside the project folder.")
        tests: list[str] = []
        for name in body.tests:
            found = _inside(root, name)
            if found is None or not found.is_file():
                raise HTTPException(400, f"Test file not found inside the project: {name}")
            tests.append(found.relative_to(root).as_posix())
        if body.docs and not os.environ.get("TAVILY_API_KEY", "").strip():
            raise HTTPException(400, "Looking up docs needs TAVILY_API_KEY in your .env.")
        if body.sandbox and not manager.status()["sandbox_available"]:
            raise HTTPException(400, "Sandboxes are not set up: " + (sandbox_unavailable() or "unknown reason") + ".")
        if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._\-\[\],<>=!~]{0,80}", p) for p in body.packages):
            raise HTTPException(400, "A package name for the sandbox has characters that are not allowed.")
        try:
            state = manager.start_local(
                root, target.relative_to(root).as_posix(), tests, body.rounds, body.docs, body.attempts,
                body.sandbox, body.packages,
            )
        except Busy:
            raise HTTPException(429, "Another run is in progress. Try again in a minute.")
        except LiveUnavailable:
            raise HTTPException(503, "Local runs are switched off.")
        return {"id": state.id}

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict:
        state = manager.get(run_id)
        if state is None:
            raise HTTPException(404, "That run no longer exists.")
        return state.snapshot()

    return app


def serve(host: str = "127.0.0.1", port: int = 8000, local: bool = False) -> None:
    try:
        import uvicorn
    except ImportError:
        raise SystemExit('The web demo needs extra packages. Run: pip install -e ".[web]"')
    if local and host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("--local lets the page read folders on this computer, so it only runs on 127.0.0.1.")
    uvicorn.run(create_app(local=local), host=host, port=port)
