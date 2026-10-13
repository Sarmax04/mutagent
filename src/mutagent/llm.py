"""Nebius Token Factory client with two model tiers and a hard token budget.

Token Factory exposes an OpenAI-compatible chat API (verify this in the Nebius
docs), so the standard ``openai`` SDK is used with a custom ``base_url``.
Nothing here hard-codes a URL or model ID: copy both from the Token Factory
docs and model catalog into ``.env``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Mapping, Protocol


class ConfigError(RuntimeError):
    """Required configuration is missing."""


class BudgetExceeded(RuntimeError):
    """The token budget is used up."""


class Tier(str, Enum):
    """Which kind of model a call needs.

    FAST   - routine, cheap calls (for example a Nemotron Nano model).
    STRONG - hard reasoning and test writing (for example Nemotron Super/Ultra).
    """

    FAST = "fast"
    STRONG = "strong"


@dataclass
class TokenBudget:
    """Tracks total tokens spent so a demo cannot drain the credits."""

    max_tokens: int
    used: int = 0

    def charge(self, tokens: int) -> None:
        if tokens < 0:
            raise ValueError("tokens must be non-negative")
        self.used += tokens

    @property
    def remaining(self) -> int:
        return max(self.max_tokens - self.used, 0)

    def check(self) -> None:
        if self.used >= self.max_tokens:
            raise BudgetExceeded(
                f"token budget exhausted ({self.used}/{self.max_tokens})"
            )


class ChatModel(Protocol):
    """Anything the agent loop can talk to (real client or a test fake)."""

    budget: TokenBudget

    def complete(self, tier: Tier, messages: list[dict[str, str]]) -> str: ...


def strip_reasoning(text: str) -> str:
    """Remove ``<think>...</think>`` blocks that reasoning models may emit."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    if "</think>" in text:  # some models emit only the closing tag
        text = text.split("</think>")[-1]
    return text.strip()


def load_dotenv(path: Path) -> None:
    """Tiny .env reader (KEY=VALUE lines); existing environment wins."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    api_key: str
    base_url: str
    model_fast: str
    model_strong: str
    token_budget: int = 200_000

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        required = {
            "NEBIUS_API_KEY": "api_key",
            "NEBIUS_BASE_URL": "base_url",
            "NEBIUS_MODEL_FAST": "model_fast",
            "NEBIUS_MODEL_STRONG": "model_strong",
        }
        missing = [name for name in required if not env.get(name, "").strip()]
        if missing:
            raise ConfigError(
                "Missing configuration: "
                + ", ".join(missing)
                + ". Copy .env.example to .env and fill it in."
            )
        values = {field: env[name].strip() for name, field in required.items()}
        budget = env.get("NEBIUS_TOKEN_BUDGET", "").strip()
        return cls(token_budget=int(budget) if budget else 200_000, **values)


class NebiusChat:
    """Chat client for Nebius Token Factory with per-tier models and a budget."""

    def __init__(self, settings: Settings, budget: TokenBudget | None = None) -> None:
        from openai import OpenAI  # imported lazily so unit tests need no SDK setup

        self._client = OpenAI(api_key=settings.api_key, base_url=settings.base_url)
        self._models = {Tier.FAST: settings.model_fast, Tier.STRONG: settings.model_strong}
        self.budget = budget or TokenBudget(settings.token_budget)

    def complete(
        self,
        tier: Tier,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> str:
        self.budget.check()
        response = self._client.chat.completions.create(
            model=self._models[tier],
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if response.usage is not None:
            self.budget.charge(response.usage.total_tokens)
        return strip_reasoning(response.choices[0].message.content or "")
