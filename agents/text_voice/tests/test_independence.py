"""The standalone contract: agents/text_voice imports nothing from the repo.

From the lqabr-agent-scaffold skill. Parses the AST rather than grepping raw
text — an earlier version grepped file contents and fired on its own
docstrings, which *describe* the rule. A test that fires on the documentation
of a rule instead of a breach of it trains people to ignore it.

The only platform coupling permitted is a runtime URL (the MCP endpoint),
never an import.
"""

from __future__ import annotations

import ast
from pathlib import Path

FORBIDDEN_ROOTS = {"lqabr_core", "agents", "packages"}
AGENT = Path(__file__).resolve().parents[1]
DIRECT_HUBSPOT_EXEMPTION = "hubspot_direct.py"   # a FILENAME, not a pattern


def _sources():
    for folder in ("packages", "src"):
        root = AGENT / folder
        if root.exists():
            yield from root.rglob("*.py")


def _module_roots(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:      # level>0 is internal
                yield node.module.split(".")[0]


def _docstring_nodes(tree):
    seen = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef,
                          ast.AsyncFunctionDef)):
            b = getattr(n, "body", None)
            if b and isinstance(b[0], ast.Expr) and \
               isinstance(b[0].value, ast.Constant) and \
               isinstance(b[0].value.value, str):
                seen.add(id(b[0].value))
    return seen


def test_no_repo_imports():
    bad = [f"{p.relative_to(AGENT)}: imports {r}"
           for p in _sources()
           for r in _module_roots(ast.parse(p.read_text(encoding="utf-8")))
           if r in FORBIDDEN_ROOTS]
    assert not bad, f"agent must stay standalone; found: {bad}"


def test_no_direct_hubspot_calls_outside_the_one_exemption():
    bad = []
    for p in _sources():
        if p.name == DIRECT_HUBSPOT_EXEMPTION:
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        docs = _docstring_nodes(tree)
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) \
               and id(n) not in docs and "api.hubapi.com" in n.value:
                bad.append(f"{p.relative_to(AGENT)}:{n.lineno}")
    assert not bad, f"HubSpot only through the MCP; found {bad}"


def test_the_exemption_is_read_only():
    p = AGENT / "packages" / "text_voice_core" / DIRECT_HUBSPOT_EXEMPTION
    if not p.exists():
        return
    src = p.read_text(encoding="utf-8")
    for bad in ('"PATCH"', "'PATCH'", '"PUT"', "'PUT'", '"DELETE"', "'DELETE'",
                "/batch/update"):
        assert bad not in src, f"exemption is read-only: found {bad}"


def test_settings_is_the_only_module_reading_the_environment():
    """settings.py owns os.environ; everything else takes a value from it, so
    a rename out there is a config change and never a code edit."""
    # secrets.py is the one other module permitted to read the environment:
    # it resolves a credential by NAME at run time (get_secret("lqabr-x")
    # falls back to LQABR_X), which a module of static constants cannot
    # express. It reads only names it was handed, and never logs a value.
    allowed = {"settings.py", "secrets.py"}
    bad = []
    for p in _sources():
        if p.name in allowed:
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if isinstance(n, ast.Attribute) and n.attr in ("environ", "getenv") \
                    and isinstance(n.value, ast.Name) and n.value.id == "os":
                bad.append(f"{p.relative_to(AGENT)}:{n.lineno}")
            elif isinstance(n, ast.Name) and n.id == "getenv":
                bad.append(f"{p.relative_to(AGENT)}:{n.lineno}")
    assert not bad, f"only settings.py may read os.environ; found {bad}"
