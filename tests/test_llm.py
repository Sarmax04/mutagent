import pytest

from mutagent.llm import (
    BudgetExceeded,
    ConfigError,
    Settings,
    TokenBudget,
    strip_reasoning,
)

FULL_ENV = {
    "NEBIUS_API_KEY": "k",
    "NEBIUS_BASE_URL": "https://example.invalid/v1/",
    "NEBIUS_MODEL_FAST": "fast-model",
    "NEBIUS_MODEL_STRONG": "strong-model",
}


def test_budget_tracks_and_stops():
    budget = TokenBudget(100)
    budget.check()
    budget.charge(60)
    assert budget.remaining == 40
    budget.charge(60)
    assert budget.remaining == 0
    with pytest.raises(BudgetExceeded):
        budget.check()


def test_budget_rejects_negative_charge():
    with pytest.raises(ValueError):
        TokenBudget(10).charge(-1)


def test_settings_from_env_ok_and_default_budget():
    settings = Settings.from_env(FULL_ENV)
    assert settings.model_strong == "strong-model"
    assert settings.token_budget == 200_000
    assert Settings.from_env({**FULL_ENV, "NEBIUS_TOKEN_BUDGET": "5000"}).token_budget == 5000


def test_settings_lists_every_missing_variable():
    with pytest.raises(ConfigError) as err:
        Settings.from_env({"NEBIUS_API_KEY": "k", "NEBIUS_MODEL_FAST": "  "})
    message = str(err.value)
    assert "NEBIUS_BASE_URL" in message
    assert "NEBIUS_MODEL_FAST" in message
    assert "NEBIUS_MODEL_STRONG" in message
    assert "NEBIUS_API_KEY" not in message


def test_strip_reasoning_removes_think_blocks():
    assert strip_reasoning("<think>plan\n```python\nx\n```</think>answer") == "answer"
    assert strip_reasoning("hidden thoughts</think>answer") == "answer"
    assert strip_reasoning("plain") == "plain"
