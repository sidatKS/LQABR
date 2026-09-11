"""The call contract between src/text_voice.py and its two siblings.

Parses the AST rather than importing, so this stays offline and cannot be
fooled by monkeypatched fakes: the suite's FakeMCP accepts whatever the
tests hand it, which is exactly why a keyword the REAL client does not
support can pass every behavioural test and still raise TypeError in
production.

Added 2026-09-08 after restoring src/text_voice.py from 2b5dc79 (repairing
merge 825b8d0). That file called mcp.upsert_lead(..., current=lead) and
mcp.record_call_outcome(..., current=current); the mcp_client.py it was
written against had those parameters, today's does not. Thirteen tests
failed on the fakes; the real client would have raised on the INITIATED
claim, the CALL_PLACED write, the FAILED rollback and Step 8.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
CLIENT_CLASS = "StepFiveMCPClient"
CLIENT_HANDLE = "mcp"          # the module-level client in text_voice.py


def _tree(name: str) -> ast.Module:
    return ast.parse((SRC / name).read_text(encoding="utf-8"))


def _signatures(tree: ast.Module, cls: str) -> dict[str, set[str]]:
    """method name -> every parameter name it will accept by keyword."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == cls:
            return {
                m.name: {a.arg for a in m.args.args}
                        | {a.arg for a in m.args.kwonlyargs}
                        | ({m.args.kwarg.arg} if m.args.kwarg else set())
                for m in node.body
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
    return {}


def _module_functions(tree: ast.Module) -> dict[str, set[str]]:
    return {
        n.name: {a.arg for a in n.args.args}
                | {a.arg for a in n.args.kwonlyargs}
                | ({n.args.kwarg.arg} if n.args.kwarg else set())
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _calls_on(tree: ast.Module, handle: str) -> list[tuple[str, set[str], int]]:
    """(method, keywords passed, lineno) for every `<handle>.method(...)` call."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) \
                and f.value.id == handle:
            found.append((f.attr, {k.arg for k in node.keywords if k.arg},
                          node.lineno))
    return found


def _calls_to_names(tree: ast.Module, names: set[str]) -> list[tuple[str, set[str], int]]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in names:
            found.append((node.func.id, {k.arg for k in node.keywords if k.arg},
                          node.lineno))
    return found


def test_every_mcp_method_called_exists_on_the_client():
    client = _signatures(_tree("mcp_client.py"), CLIENT_CLASS)
    assert client, f"{CLIENT_CLASS} not found in mcp_client.py"
    missing = [f"text_voice.py:{line} calls {CLIENT_HANDLE}.{method}()"
               for method, _, line in _calls_on(_tree("text_voice.py"), CLIENT_HANDLE)
               if method not in client]
    assert not missing, (f"no such method on {CLIENT_CLASS}: {missing}; "
                         f"it has {sorted(client)}")


def test_every_keyword_passed_to_the_client_is_accepted():
    """The guard the fakes cannot provide: a FakeMCP with **kwargs accepts
    anything, so only the real signature settles it."""
    client = _signatures(_tree("mcp_client.py"), CLIENT_CLASS)
    bad = []
    for method, kwargs, line in _calls_on(_tree("text_voice.py"), CLIENT_HANDLE):
        accepted = client.get(method, set())
        for kw in sorted(kwargs - accepted):
            bad.append(f"text_voice.py:{line} {CLIENT_HANDLE}.{method}({kw}=...) "
                       f"but {method} accepts {sorted(accepted)}")
    assert not bad, "unsupported keyword argument(s): " + "; ".join(bad)


def test_every_keyword_passed_into_tools_is_accepted():
    tools = _module_functions(_tree("tools.py"))
    tv = _tree("text_voice.py")
    imported = {a.name for n in ast.walk(tv) if isinstance(n, ast.ImportFrom)
                and (n.module or "").lstrip(".") == "tools" for a in n.names}
    bad = []
    for name, kwargs, line in _calls_to_names(tv, imported & set(tools)):
        for kw in sorted(kwargs - tools[name]):
            bad.append(f"text_voice.py:{line} {name}({kw}=...) "
                       f"but it accepts {sorted(tools[name])}")
    assert not bad, "unsupported keyword argument(s): " + "; ".join(bad)


def test_one_record_id_spelling_in_text_voice():
    """object_id is the decided spelling. contact_id is the pre-rename name
    and must not come back through a merge — 825b8d0 is how it got here."""
    source = (SRC / "text_voice.py").read_text(encoding="utf-8")
    assert "contact_id" not in source, (
        "text_voice.py must use object_id only; found contact_id")


def test_no_duplicate_top_level_definitions():
    """825b8d0 resolved a rename-vs-rename conflict by keeping both sides,
    leaving six functions defined twice. Python keeps the last one, so the
    older half silently won and the suite could not see it."""
    for name in ("text_voice.py", "mcp_client.py", "tools.py"):
        seen, dupes = set(), []
        for node in _tree(name).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name in seen:
                    dupes.append(f"{name}:{node.lineno} {node.name}")
                seen.add(node.name)
        assert not dupes, f"defined more than once: {dupes}"
