import urllib.error

from mutagent.cli import main
from mutagent.docs import DocsLookup, enclosing_names, project_name
from mutagent.loop import run_loop
from mutagent.llm import Tier

from test_loop import FakeLLM, RecordingLLM, STRONG_TESTS, fenced, project, quiet  # noqa: F401

SOURCE = '''\
class Cache:
    def get(self, key):
        if key in self.data:
            return self.data[key]
        return None


def pluralize(word):
    if word.endswith("s"):
        return word
    return word + "s"


def _helper(x):
    return x + 1
'''


def test_enclosing_names_counts_mutant_lines_per_function():
    assert enclosing_names(SOURCE, [9, 10, 11, 3, 15]) == ["pluralize", "Cache.get", "_helper"]
    assert enclosing_names("not python (", [1]) == []


def test_project_name_prefers_declared_names(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "python-slugify"\n', encoding="utf-8")
    assert project_name(tmp_path, "slugify.slugify") == "python-slugify"
    other = tmp_path / "other"
    other.mkdir()
    assert project_name(other, "cachetools.func") == "cachetools"


def test_lookup_caches_formats_and_survives_errors():
    calls = []

    def search(query):
        calls.append(query)
        if "broken" in query:
            raise urllib.error.URLError("offline")
        return {
            "answer": "pluralize adds an s.",
            "results": [
                {"url": "https://inflection.readthedocs.io/p", "content": "pluralize('cat') == 'cats'"},
                {"url": "https://shop.example/tips", "content": "how to pluralize product names"},
            ],
        }

    docs = DocsLookup(package="inflection", search=search, log=quiet)
    text = docs.digest(["pluralize", "broken"])
    assert "### pluralize" in text and "pluralize('cat') == 'cats'" in text and "broken" not in text
    docs.digest(["pluralize", "broken"])
    assert len(calls) == 2  # cached, including the failure
    assert docs.history == [{"name": "pluralize", "query": calls[0], "urls": ["https://inflection.readthedocs.io/p"]}]
    assert "shop.example" not in text  # unrelated pages are dropped


def test_docs_go_into_the_prompt_and_the_report(project):  # noqa: F811
    docs = DocsLookup(
        package="pricing",
        search=lambda q: {"results": [{"url": "https://pricing.example/d", "content": "apply_discount(100, 10) is 90"}]},
        log=quiet,
    )
    llm = RecordingLLM([fenced(STRONG_TESTS)])
    report = run_loop(project, "pricing.py", llm, max_rounds=1, log=quiet, docs=docs)
    prompt = [p for tier, p in llm.prompts if tier is Tier.STRONG][0]
    assert "Documentation found on the web (Tavily search)" in prompt
    assert "apply_discount(100, 10) is 90" in prompt
    assert report.docs_lookups and report.docs_lookups[0]["urls"] == ["https://pricing.example/d"]


def test_docs_flag_without_key_is_a_clear_error(project, monkeypatch, capsys):  # noqa: F811
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.chdir(project)
    for k, v in {"NEBIUS_API_KEY": "x", "NEBIUS_BASE_URL": "http://x/v1/",
                 "NEBIUS_MODEL_FAST": "f", "NEBIUS_MODEL_STRONG": "s"}.items():
        monkeypatch.setenv(k, v)
    code = main(["run", "--project", str(project), "--target", "pricing.py", "--docs", "--no-write-back"])
    assert code == 2 and "TAVILY_API_KEY" in capsys.readouterr().err


def test_public_names_drop_private_parts():
    from mutagent.docs import public_names

    names = ["_irregular", "Cache.__getitem__", "pluralize", "Cache.get", "_Hidden.method", "Cache"]
    assert public_names(names) == ["Cache", "pluralize", "Cache.get"]


def test_docs_are_skipped_for_unpublished_modules(tmp_path, monkeypatch):
    from mutagent.docs import from_env

    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    messages = []
    assert from_env(tmp_path, "pricing", log=messages.append) is None
    assert "not a published package" in messages[0]
    assert from_env(tmp_path, "pricing", log=messages.append, package="pricing") is not None
    (tmp_path / "VENDORED.md").write_text("From https://pypi.org/project/python-slugify/", encoding="utf-8")
    assert from_env(tmp_path, "slugify.slugify", log=messages.append).package == "python-slugify"
