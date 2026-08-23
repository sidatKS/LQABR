"""THE STANDALONE CONTRACT, enforced mechanically.

agents/summary must be patchable, testable and deployable on its own. That
promise is worth exactly as much as the check behind it, so this test walks
every Python file in the agent and fails the build the moment one of them
reaches into the rest of the repo:

    lqabr_core       the shared package this agent deliberately does not use
    mcp.*            the repo-root in-process MCP. This agent talks to the
                     HubSpot MCP over the network at runtime instead, so an
                     import of it is a regression, not a shortcut.

`summary_core.mcp` is THIS agent's own client package and is fine — the
check targets the top-level `mcp` package only.

It also guards the two ways the coupling creeps back in without an import:
a requirements file pointing at a repo path, and a Dockerfile copying
shared folders into the image.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

AGENT_ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_ROOTS = {"lqabr_core", "mcp"}

SKIP_DIRS = {".venv", "node_modules", "__pycache__", ".pytest_cache", "dist", "build"}


def _python_files() -> list[Path]:
    return [
        path
        for path in AGENT_ROOT.rglob("*.py")
        if not any(part in SKIP_DIRS for part in path.parts)
    ]


def _imported_roots(path: Path) -> set[str]:
    """Top-level module names imported by one file.

    A relative import (`from .sources import web`) has no top-level name and
    is ignored — `node.level > 0` is how ast marks it.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
    return roots


def test_at_least_one_python_file_is_scanned():
    """A guard that silently scans nothing would pass forever."""
    assert _python_files(), "the import guard found no Python files to check"


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: str(p.relative_to(AGENT_ROOT)))
def test_no_shared_repo_imports(path: Path):
    offending = _imported_roots(path) & FORBIDDEN_ROOTS
    assert not offending, (
        f"{path.relative_to(AGENT_ROOT)} imports {sorted(offending)}. "
        "agents/summary is standalone: reach the HubSpot MCP over the network "
        "(summary_core.mcp), and copy what you need from lqabr_core into "
        "summary_core rather than importing it."
    )


def test_requirements_declare_no_repo_paths():
    for name in ("requirements.txt", "requirements-dev.txt"):
        text = (AGENT_ROOT / name).read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or not stripped:
                continue
            assert "packages/lqabr_core" not in stripped, f"{name}: {stripped}"
            assert not stripped.startswith("-e "), (
                f"{name}: editable repo install '{stripped}' breaks the standalone contract"
            )


def test_dockerfile_copies_nothing_shared():
    dockerfile = AGENT_ROOT / "Dockerfile"
    if not dockerfile.exists():
        pytest.skip("Dockerfile arrives in P6")
    for line in dockerfile.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.upper().startswith("COPY"):
            continue
        assert "packages/lqabr_core" not in stripped, stripped
        assert not stripped.split()[1:2] == ["mcp"], stripped
