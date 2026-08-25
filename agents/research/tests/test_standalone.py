"""The isolation guarantees, checked against CODE rather than prose.

Two invariants this agent must never lose:

1. it imports nothing from the rest of the repo, and
2. it reaches HubSpot only through the MCP.

Both are checked by parsing the source, not by scanning text: an earlier
version grepped raw file contents and failed on its own docstrings, which
say "no lqabr_core" and "never calls api.hubapi.com". A test that fires on
the documentation of a rule instead of a breach of it is worse than no test —
it trains people to ignore it.
"""

from __future__ import annotations

import ast
from pathlib import Path

#: Repo modules this agent must never import.
FORBIDDEN_ROOTS = {"lqabr_core", "summary_core", "mcp", "agents", "packages"}

AGENT = Path(__file__).resolve().parents[1]


def _sources():
    for folder in ("packages", "src"):
        for path in (AGENT / folder).rglob("*.py"):
            yield path


def _module_roots(tree: ast.AST):
    """Top-level package of every absolute import in the file."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            # level > 0 is a relative import — inside this agent, always fine.
            if node.level == 0 and node.module:
                yield node.module.split(".")[0]


def _docstring_nodes(tree: ast.AST):
    """Every string node that is a docstring, so it can be excluded."""
    seen = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef,
                             ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and \
                    isinstance(body[0].value.value, str):
                seen.add(id(body[0].value))
    return seen


def test_no_repo_imports():
    """The agent's library lives inside the agent. If this fails, someone
    reached for shared code and the agent stopped being deployable alone."""
    offenders = []
    for path in _sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for root in _module_roots(tree):
            if root in FORBIDDEN_ROOTS:
                offenders.append(f"{path.relative_to(AGENT)}: imports {root}")
    assert not offenders, ("the research agent must stay standalone; found: "
                           + "; ".join(offenders))


#: The ONE file allowed to name HubSpot directly (2026-08-24, user decision):
#: the MCP exposes no lead-listing tool, so "which leads are in this industry"
#: is read straight from HubSpot. The exemption is deliberately a single
#: filename, not a pattern — a second direct caller must fail this test and be
#: argued for on its own merits. Delete the exemption when the MCP grows the
#: tool; see research_core/hubspot_direct.py.
DIRECT_HUBSPOT_EXEMPTION = "hubspot_direct.py"


def test_no_direct_hubspot_calls_outside_the_one_exemption():
    """Every HubSpot read and write goes through the MCP, with one scoped
    exception. A HubSpot hostname anywhere else would mean a second, unaudited
    path to the CRM — a second copy of the token and writes that skip schema
    validation."""
    offenders = []
    for path in _sources():
        if path.name == DIRECT_HUBSPOT_EXEMPTION:
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        docstrings = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and id(node) not in docstrings and "api.hubapi.com" in node.value:
                offenders.append(f"{path.relative_to(AGENT)}:{node.lineno}")
    assert not offenders, ("the research agent must reach HubSpot only through the "
                           f"MCP (except {DIRECT_HUBSPOT_EXEMPTION}); a HubSpot URL "
                           f"appears in code at: {offenders}")


def test_the_direct_hubspot_exemption_is_read_only():
    """The exemption buys a lookup, not a write path. Writes stay on the MCP,
    where they are schema-validated and audited, so the direct module must
    never POST or PATCH to an object endpoint."""
    path = AGENT / "packages" / "research_core" / DIRECT_HUBSPOT_EXEMPTION
    if not path.exists():          # exemption already removed — nothing to police
        return
    source = path.read_text(encoding="utf-8")
    for forbidden in ("/crm/v3/objects/contacts/batch/update",
                      '"PATCH"', "'PATCH'", '"PUT"', "'PUT'",
                      '"DELETE"', "'DELETE'"):
        assert forbidden not in source, (
            f"{DIRECT_HUBSPOT_EXEMPTION} is read-only: found {forbidden!r}. "
            "Writes belong on the MCP.")


def test_the_rule_is_actually_documented():
    """Belt and braces: the MCP-only rule must be stated where a reader will
    find it. This is the check that lets the two tests above stay strict about
    code while the prose keeps explaining why."""
    readme = (AGENT / "README.md").read_text(encoding="utf-8")
    assert "api.hubapi.com" in readme and "MCP is the only door" in readme
