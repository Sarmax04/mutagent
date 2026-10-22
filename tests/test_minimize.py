import shutil
from pathlib import Path

import pytest

from mutagent.loop import run_loop
from mutagent.minimize import cover, test_names as names_in
from test_loop import STRONG_TESTS, FakeLLM, fenced

DEMO = Path(__file__).resolve().parents[1] / "examples" / "demo_project"

REDUNDANT = STRONG_TESTS + '''

def test_discount_again():
    assert apply_discount(100, 10) == 90


class TestShippingAgain:
    def test_one_kilo(self):
        assert shipping_cost(1) == 4.0
'''


@pytest.fixture
def project(tmp_path):
    dest = tmp_path / "demo"
    shutil.copytree(DEMO, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return dest


def test_names_cover_functions_and_methods():
    assert names_in("def test_a():\n    pass\n\nclass TestB:\n    def test_c(self):\n        pass\n\ndef helper():\n    pass\n") == [
        "test_a",
        "TestB.test_c",
    ]
    assert names_in("def broken(:\n") == []


def test_cover_picks_few_tests_in_file_order():
    names = ["a", "b", "c", "d"]
    kills = {1: {"a", "b"}, 2: {"b"}, 3: {"c", "b"}, 4: set()}
    assert cover(names, kills) == ["b"]
    assert cover(names, {1: {"a"}, 2: {"d"}}) == ["a", "d"]


def test_loop_trims_redundant_tests_without_losing_score(project):
    full = run_loop(project, "pricing.py", FakeLLM([fenced(REDUNDANT)]), max_rounds=1, log=lambda _m: None, write_back=False)
    trimmed = run_loop(project, "pricing.py", FakeLLM([fenced(REDUNDANT)]), max_rounds=1, log=lambda _m: None,
                       write_back=False, minimize=True)
    assert trimmed.final_score == full.final_score
    assert trimmed.trimmed["after"] < trimmed.trimmed["before"] == len(names_in(REDUNDANT))
    assert "test_discount_again" not in trimmed.generated_code or "TestShippingAgain" not in trimmed.generated_code
    assert len(names_in(trimmed.generated_code)) == trimmed.trimmed["after"]
    assert full.trimmed == {}


def test_tidy_code_drops_think_aloud_but_keeps_tests():
    from mutagent.loop import tidy_code

    code = (
        "import pytest\n\n\n\n\ndef test_a():\n    # one short note stays\n    assert 1 == 1\n\n"
        "# Let me re-read the mutant...\n# Actually, wait.\n# Indeed the behaviour is identical.\n\n\n\n"
        "def test_b():\n    assert 2 == 2\n"
    )
    tidied = tidy_code(code)
    assert "Actually" not in tidied and "one short note stays" in tidied
    assert "\n\n\n\n" not in tidied
    assert "def test_a" in tidied and "def test_b" in tidied
    assert tidy_code("def broken(:\n") == "def broken(:\n"
    strings = 'X = """\n# a\n# b\n# c\n"""\n\ndef test_x():\n    assert X\n'
    assert tidy_code(strings) == strings  # comment-looking lines inside a string are kept
