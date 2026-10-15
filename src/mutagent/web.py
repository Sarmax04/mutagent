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
from .loop import BaselineFailing, RoundResult, run_loop
from .presets import PRESETS, Preset, get_preset, project_path, replays_dir
from .safety import screen_test_code

log = logging.getLogger("mutagent.web")
STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_STORED_RUNS = 20


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
    ) -> None:
        self._factory = llm_factory
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

    def _work(self, state: RunState, preset: Preset) -> None:
        try:
            llm = self._factory()  # type: ignore[misc]
            state.status = "running"
            report = run_loop(
                project_path(preset),
                preset.target,
                llm,
                max_rounds=state.rounds_requested,
                test_timeout=self.limits.test_timeout,
                write_back=False,
                log=state.log.append,
                tests=preset.tests,
                on_round=state.add_round,
                code_filter=screen_test_code,
            )
            state.report = report.to_dict()
            state.status = "done"
        except BaselineFailing:
            log.exception("preset %s fails its own tests on this server", preset.id)
            state.error = "This preset's own tests fail on the server, so it can't be scored."
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


class RunRequest(BaseModel):
    preset: str
    rounds: int = Field(default=2, ge=1, le=20)


def _live_from_env(limits: Limits) -> tuple[Callable[[], ChatModel] | None, dict[str, str]]:
    load_dotenv(Path(".env"))
    try:
        settings = Settings.from_env()
    except ConfigError:
        return None, {}
    budget = min(limits.run_token_budget, settings.token_budget)

    def factory() -> ChatModel:
        return NebiusChat(settings, TokenBudget(budget))

    return factory, {"fast": settings.model_fast, "strong": settings.model_strong}


def create_app(
    llm_factory: Callable[[], ChatModel] | None = None,
    limits: Limits | None = None,
    models: dict[str, str] | None = None,
    auto_configure: bool = True,
) -> FastAPI:
    """Build the app. Live runs are enabled when ``llm_factory`` is given, or (with
    ``auto_configure``) when the Nebius settings are present in the environment/.env."""
    limits = limits or Limits.from_env()
    if llm_factory is None and auto_configure:
        llm_factory, models = _live_from_env(limits)
    manager = RunManager(llm_factory, limits, models)

    app = FastAPI(title="MutAgent", docs_url=None, redoc_url=None)
    app.state.manager = manager

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

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

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict:
        state = manager.get(run_id)
        if state is None:
            raise HTTPException(404, "That run no longer exists.")
        return state.snapshot()

    return app


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    try:
        import uvicorn
    except ImportError:
        raise SystemExit('The web demo needs extra packages. Run: pip install -e ".[web]"')
    uvicorn.run(create_app(), host=host, port=port)
