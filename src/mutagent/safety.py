"""Cheap defence in depth for running model-written test code.

``screen_test_code`` statically rejects test files that import risky modules or call
risky builtins *before* anything is executed. This is NOT a sandbox: a determined
attacker can get around a denylist. Real isolation comes from running mutagent in a
container (or a Nebius Serverless Job) with no secrets and, ideally, no network for the
test processes, plus the resource limits enabled by ``MUTAGENT_LIMITS=1``.

For the hosted demo the risk is further reduced because users never supply code or
prompts: only fixed presets can be run.
"""

from __future__ import annotations

import ast

FORBIDDEN_MODULES = frozenset(
    {
        # processes, files and the interpreter itself
        "os", "sys", "pathlib", "shutil", "tempfile", "subprocess", "multiprocessing",
        "signal", "io", "glob", "fileinput", "zipfile", "tarfile", "sqlite3", "pty",
        "runpy", "code", "importlib", "builtins", "gc", "inspect", "types", "ctypes",
        "site", "sysconfig", "pkgutil", "platform", "pdb", "asyncio", "concurrent",
        "pickle", "marshal",
        # network
        "socket", "ssl", "urllib", "http", "httpx", "requests", "ftplib", "smtplib",
        "webbrowser",
    }
)
FORBIDDEN_CALLS = frozenset(
    {"eval", "exec", "compile", "open", "__import__", "input", "breakpoint", "globals", "vars"}
)
# method calls that reach the system even through an allowed object
FORBIDDEN_METHOD_CALLS = frozenset({"importorskip", "system", "popen", "fork"})
FORBIDDEN_ATTRIBUTES = frozenset(
    {
        "__subclasses__", "__globals__", "__builtins__", "__code__", "__closure__",
        "__import__", "f_globals", "f_builtins", "f_locals", "f_back", "gi_frame", "tb_frame",
    }
)


def screen_test_code(code: str) -> list[str]:
    """Return a list of problems; an empty list means the file may be run."""
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [f"syntax error on line {exc.lineno}"]

    problems: list[str] = []

    def flag(message: str) -> None:
        if message not in problems:
            problems.append(message)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in FORBIDDEN_MODULES:
                    flag(f"imports '{root}'")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if node.level == 0 and root in FORBIDDEN_MODULES:
                flag(f"imports '{root}'")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in FORBIDDEN_CALLS:
                flag(f"calls '{func.id}'")
            elif isinstance(func, ast.Attribute) and func.attr in FORBIDDEN_METHOD_CALLS:
                flag(f"calls '{func.attr}'")
        elif isinstance(node, ast.Attribute):
            if node.attr in FORBIDDEN_ATTRIBUTES:
                flag(f"uses '{node.attr}'")
        elif isinstance(node, ast.Name) and node.id in ("__builtins__", "__import__"):
            flag(f"uses '{node.id}'")
        elif isinstance(node, ast.Constant) and node.value in FORBIDDEN_ATTRIBUTES:
            # getattr(obj, "__subclasses__") and similar string-based lookups
            flag(f"uses '{node.value}'")
    return problems
