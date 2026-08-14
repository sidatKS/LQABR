"""Config loading — ``config/config.yaml`` and ``config/agents_registry.yaml``.

Rev 3: behaviour is tuned through config files, not code changes. So this
module is deliberately dumb — it reads YAML, applies defaults, and resolves
``${ENV_VAR}`` references. It contains no routing logic; that belongs to
``router.py``, which consumes the registry document this module returns.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

#: Repo-relative default location of the config directory
#: (``agents/gateway/config``), resolved from this file's position so the
#: gateway works the same from a container, a test, or ``uvicorn`` in ``src/``.
DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"

_ENV_REF = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


class ConfigError(RuntimeError):
    """Configuration is missing, unreadable, or internally inconsistent."""


def _expand_env(value: Any) -> Any:
    """Expand ``${VAR}`` / ``${VAR:-default}`` inside string leaves."""
    if isinstance(value, str):
        def repl(m: "re.Match[str]") -> str:
            return os.environ.get(m.group(1), m.group(2) or "")
        return _ENV_REF.sub(repl, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def _read_yaml(path: Path) -> Dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from exc
    try:
        document = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(document, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")
    return _expand_env(document)


class Config:
    """Read-only view over ``config.yaml`` with dotted lookup.

    ``cfg.get("protocols.a2a.timeout_seconds", 30)`` — a missing key returns
    the default rather than raising, so adding a tunable to the YAML never
    requires a code change to read it, and removing one never crashes a
    running gateway.
    """

    def __init__(self, document: Optional[Dict[str, Any]] = None) -> None:
        self._doc: Dict[str, Any] = document or {}

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._doc
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def require(self, dotted: str) -> Any:
        sentinel = object()
        value = self.get(dotted, sentinel)
        if value is sentinel:
            raise ConfigError(f"required config key '{dotted}' is missing")
        return value

    def section(self, dotted: str) -> Dict[str, Any]:
        value = self.get(dotted, {})
        return value if isinstance(value, dict) else {}

    def as_dict(self) -> Dict[str, Any]:
        return self._doc

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Config(keys={sorted(self._doc)})"


def load_config(path: Optional[Path] = None) -> Config:
    """Load ``config.yaml``. Defaults to ``agents/gateway/config/config.yaml``."""
    target = Path(path) if path else DEFAULT_CONFIG_DIR / "config.yaml"
    return Config(_read_yaml(target))


def load_registry_document(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load the raw ``agents_registry.yaml`` document.

    Returned unvalidated on purpose: ``router.AgentRegistry`` owns validation,
    because what makes a registry valid is a routing concern.
    """
    target = Path(path) if path else DEFAULT_CONFIG_DIR / "agents_registry.yaml"
    return _read_yaml(target)
