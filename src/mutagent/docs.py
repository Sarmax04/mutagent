"""Ground the model's expectations in the library's real documentation (Tavily web search).

The most common reason a model-written test is thrown away is a wrong expectation:
the model guesses what a function returns (``'Ss'`` vs ``'ß'``, ``misses`` vs ``miss``)
instead of knowing it. With ``--docs`` the agent looks up the documentation of the
functions it is about to test, through the Tavily Search API, and puts a short digest
into the prompt next to the source code.

Lookups are best effort: any network or API problem is logged and the run continues
without documentation. Results are cached per run, so each function is looked up once.
"""

from __future__ import annotations

import ast
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

TAVILY_URL = "https://api.tavily.com/search"
MAX_DIGEST_CHARS = 3000


class DocsUnavailable(RuntimeError):
    """No Tavily key configured."""


def enclosing_names(source: str, lines: Sequence[int]) -> list[str]:
    """Public function/class names that contain the given line numbers, most hits first.

    Methods are reported as ``Class.method``. Private helpers (``_name``) are kept,
    because mutants often live there, but public names sort first on ties.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    spans: list[tuple[int, int, str]] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}{child.name}"
                spans.append((child.lineno, child.end_lineno or child.lineno, name))
                visit(child, name + ".")

    visit(tree, "")
    counts: dict[str, int] = {}
    for line in lines:
        inside = [s for s in spans if s[0] <= line <= s[1]]
        if inside:
            innermost = min(inside, key=lambda s: s[1] - s[0])[2]
            counts[innermost] = counts.get(innermost, 0) + 1
    return sorted(counts, key=lambda n: (-counts[n], n.split(".")[-1].startswith("_"), n))


def public_names(names: Sequence[str]) -> list[str]:
    """Names worth searching the web for: private parts are dropped.

    Documentation covers the public API, so ``_irregular`` is skipped and
    ``Cache.__getitem__`` becomes ``Cache``. Order is kept, duplicates removed.
    """
    out: list[str] = []
    for name in names:
        parts = name.split(".")
        while parts and parts[-1].startswith("_"):
            parts.pop()
        public = ".".join(parts)
        if public and not any(p.startswith("_") for p in parts) and public not in out:
            out.append(public)
    return out


def project_name(project: Path, module: str) -> str:
    """The name to search the web for: the distribution name if the project declares one."""
    project = Path(project)
    for meta in ("pyproject.toml", "setup.cfg"):
        path = project / meta
        if path.is_file():
            match = re.search(r'^\s*name\s*=\s*"?([A-Za-z0-9_.\-]+)"?', path.read_text(encoding="utf-8", errors="replace"), re.M)
            if match:
                return match.group(1)
    vendored = project / "VENDORED.md"
    if vendored.is_file():
        match = re.search(r"pypi\.org/project/([A-Za-z0-9_.\-]+)", vendored.read_text(encoding="utf-8", errors="replace"))
        if match:
            return match.group(1)
    return module.split(".")[0]


def tavily_search(query: str, api_key: str, *, max_results: int = 4, timeout: float = 20.0) -> dict:
    """One Tavily search (1 credit with ``basic`` depth). Raises on HTTP or network errors."""
    body = json.dumps(
        {
            "query": query,
            "search_depth": "basic",
            "max_results": max_results,
            "chunks_per_source": 3,
            "include_answer": "basic",
            "include_raw_content": False,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        TAVILY_URL,
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https URL
        return json.loads(response.read().decode("utf-8"))


@dataclass
class DocsLookup:
    """Looks up documentation for function names and keeps a digest per name."""

    package: str
    search: Callable[[str], dict]
    log: Callable[[str], None] = print
    max_names_per_round: int = 2
    cache: dict[str, str] = field(default_factory=dict)
    history: list[dict] = field(default_factory=list)  # for the report: query, urls

    def query_for(self, name: str) -> str:
        readable = name.replace(".", " ")
        return f"python {self.package} {readable} documentation behaviour examples"

    def digest(self, names: Sequence[str]) -> str:
        """Documentation text for ``names`` (looking up at most a few new ones)."""
        new = [n for n in names if n not in self.cache][: self.max_names_per_round]
        for name in new:
            query = self.query_for(name)
            try:
                data = self.search(query)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                self.log(f"docs: lookup for {name} failed ({exc}); continuing without it")
                self.cache[name] = ""
                continue
            self.cache[name] = _format(name, data)
            urls = [r.get("url", "") for r in data.get("results", [])]
            self.history.append({"name": name, "query": query, "urls": urls})
            self.log(f"docs: looked up {name} ({len(urls)} source(s))")
        parts = [self.cache[n] for n in names if self.cache.get(n)]
        text = "\n\n".join(parts)
        return text[:MAX_DIGEST_CHARS]


def _format(name: str, data: dict) -> str:
    lines = [f"### {name}"]
    answer = (data.get("answer") or "").strip()
    if answer:
        lines.append("Summary: " + " ".join(answer.split())[:600])
    for result in data.get("results", [])[:3]:
        content = " ".join((result.get("content") or "").split())[:500]
        if content:
            lines.append(f"- {result.get('url', '')}: {content}")
    return "\n".join(lines) if len(lines) > 1 else ""


def from_env(project: Path, module: str, log: Callable[[str], None] = print, package: str | None = None) -> DocsLookup:
    """A lookup using ``TAVILY_API_KEY``; raises DocsUnavailable when it is not set."""
    key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not key:
        raise DocsUnavailable("--docs needs TAVILY_API_KEY in your environment or .env (get a key at tavily.com)")
    name = package or project_name(project, module)
    return DocsLookup(package=name, search=lambda q: tavily_search(q, key), log=log)
