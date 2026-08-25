"""One typed read of the environment. Nothing else in the agent calls os.environ.

Everything the agent touches on the outside — the model, the MCP URL, the
tool names, the HubSpot property names — is a variable with a default, so a
rename anywhere out there is a config change and never a code edit. That is
the rule in this agent's CLAUDE.md and this module is where it is kept.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Tuple


def _str(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(str(raw).strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _list(name: str, default: Tuple[str, ...] = ()) -> List[str]:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _resolve_log_file(cfg: Dict[str, object] | None = None) -> str:
    """The agent's own log-file path. Precedence:
      1. LQABR_SUMMARY_LOG_FILE env  (absolute, or relative to repo root; "" disables)
      2. config map  logging.file
      3. code default  <repo_root>/logs/agents/summary/agent.log
    A relative value (env or config map) resolves against the repo root."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[4]   # summary_core->packages->summary->agents->REPO

    def _resolve(v: str) -> str:
        v = str(v).strip()
        if v == "":
            return ""
        pth = Path(v)
        return str(pth if pth.is_absolute() else (root / pth))

    override = os.environ.get("LQABR_SUMMARY_LOG_FILE")
    if override is not None:
        return _resolve(override)
    cfg_val = _cfg(cfg or {}, "logging", "file", None)
    if cfg_val is not None:
        return _resolve(cfg_val)
    return str(root / "logs" / "agents" / "summary" / "agent.log")


def _load_config_map() -> Dict[str, object]:
    """The agent config map (YAML). Optional and dependency-tolerant: if PyYAML
    is absent or the file is missing/unreadable, returns {} and the agent falls
    back to env vars + code defaults. Path: LQABR_SUMMARY_CONFIG_FILE, else
    agents/summary/config/config.yaml relative to this package."""
    from pathlib import Path
    path = os.environ.get("LQABR_SUMMARY_CONFIG_FILE", "").strip()
    if not path:
        # settings.py -> summary_core -> packages -> <agent root>/config/config.yaml
        path = str(Path(__file__).resolve().parents[2] / "config" / "config.yaml")
    try:
        import yaml  # type: ignore
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - config map is a convenience, never required
        return {}


def _cfg(cfg: Dict[str, object], section: str, key: str, default):
    """One value out of the config map, with a code-default fallback."""
    sect = cfg.get(section)
    if isinstance(sect, dict) and key in sect and sect[key] is not None:
        return sect[key]
    return default


@dataclass(frozen=True)
class Settings:
    """The whole outside world, resolved once at startup."""

    # ── model ────────────────────────────────────────────────
    model: str = "anthropic/claude-sonnet-5"
    temperature: float = 1.0

    # ── MCP (the runtime connection to the HubSpot MCP container) ──
    mcp_base_url: str = "http://localhost:8080/mcp"
    mcp_timeout_seconds: int = 30
    mcp_auth_token: str = ""
    mcp_protocol_version: str = "2025-06-18"
    #: Defaults to bind. The real surface is DISCOVERED via tools/list at
    #: startup; these are what we look for, not what we assume.
    mcp_tool_read: str = "get_lead_profile_details"
    mcp_tool_write: str = "post_patch_crm"
    mcp_tool_list_leads: str = "list_trigger_leads"
    #: "patch" -> post_patch_crm{object_id, properties} (generic contact/ticket patch).
    #: "blog_summary" -> upsert_blog_summary{subject, blog_summary, blog_published_at,
    #: blog_industry}, the FastMCP central server's blog writer (keyed on blog_published_at).
    mcp_write_style: str = "patch"
    mcp_assert_tools: bool = True
    #: What a failed startup discovery does. `warn` logs and keeps
    #: serving (the MCP container scales to zero, so being asleep at
    #: boot is normal); `strict` refuses to start; `off` skips it.
    mcp_startup_check: str = "warn"
    #: The ARGUMENT names the write tool expects. Overridable for the
    #: same reason the tool names are: a server-side rename must never
    #: require a code edit here.
    mcp_arg_object_id: str = "object_id"
    mcp_arg_properties: str = "properties"

    # ── HubSpot target ───────────────────────────────────────
    hubspot_object_type: str = "ticket"
    hubspot_summary_property: str = "blog_summary"
    hubspot_industry_property: str = "blog_industry"
    dry_run: bool = False

    # ── source fetching ──────────────────────────────────────
    http_timeout_seconds: int = 15
    max_chars: int = 50_000
    max_retries: int = 3
    #: MCP retry/backoff, sourced from the agent config map (config/config.yaml).
    mcp_backoff_base_seconds: float = 1.0
    mcp_backoff_cap_seconds: float = 8.0
    mcp_retryable_statuses: Tuple[int, ...] = (429, 500, 502, 503, 504)
    allowed_hosts: List[str] = field(default_factory=list)
    #: Loopback/private targets are refused by default. Set this only for a
    #: local dev run against a service on the same box.
    allow_private_hosts: bool = False
    user_agent: str = "lqabr-summary-agent/0.1"

    # ── HTTP surface ─────────────────────────────────────────
    routes: str = "all"                 # all | api | chat
    enable_agui: bool = True
    cors_origins: List[str] = field(default_factory=lambda: ["http://localhost:5173"])
    port: int = 8080

    # ── secrets + logging ────────────────────────────────────
    secrets_source: str = "env"         # env | secret_manager | auto
    gcp_project: str = ""
    log_level: str = "INFO"
    log_file: str = ""    # native file log; empty = stdout only

    @classmethod
    def from_env(cls) -> "Settings":
        cfg = _load_config_map()
        _rs = _cfg(cfg, "mcp", "retryable_statuses", [429, 500, 502, 503, 504])
        _rs_env = _list("LQABR_SUMMARY_MCP_RETRYABLE_STATUSES")
        retryable = tuple(int(x) for x in (_rs_env if _rs_env else _rs))
        return cls(
            model=_str("LQABR_SUMMARY_MODEL", "anthropic/claude-sonnet-5"),
            temperature=_float("LQABR_SUMMARY_TEMPERATURE", 1.0),
            mcp_base_url=_str("LQABR_SUMMARY_MCP_BASE_URL", "http://localhost:8080/mcp"),
            mcp_timeout_seconds=_int("LQABR_SUMMARY_MCP_TIMEOUT_SECONDS", _cfg(cfg, "mcp", "timeout_seconds", 30)),
            mcp_auth_token=_str("LQABR_SUMMARY_MCP_AUTH_TOKEN"),
            mcp_protocol_version=_str("LQABR_SUMMARY_MCP_PROTOCOL_VERSION", "2025-06-18"),
            mcp_tool_read=_str("LQABR_SUMMARY_MCP_TOOL_READ", "get_lead_profile_details"),
            mcp_tool_write=_str("LQABR_SUMMARY_MCP_TOOL_WRITE", "post_patch_crm"),
            mcp_tool_list_leads=_str("LQABR_SUMMARY_MCP_TOOL_LIST_LEADS", "list_trigger_leads"),
            mcp_write_style=_str("LQABR_SUMMARY_MCP_WRITE_STYLE", "patch").lower(),
            mcp_assert_tools=_bool("LQABR_SUMMARY_MCP_ASSERT_TOOLS", True),
            mcp_startup_check=_str("LQABR_SUMMARY_MCP_STARTUP_CHECK", "warn").lower(),
            mcp_arg_object_id=_str("LQABR_SUMMARY_MCP_ARG_OBJECT_ID", "object_id"),
            mcp_arg_properties=_str("LQABR_SUMMARY_MCP_ARG_PROPERTIES", "properties"),
            hubspot_object_type=_str("LQABR_SUMMARY_HUBSPOT_OBJECT_TYPE", "ticket"),
            hubspot_summary_property=_str("LQABR_SUMMARY_HUBSPOT_SUMMARY_PROPERTY", "blog_summary"),
            hubspot_industry_property=_str("LQABR_SUMMARY_HUBSPOT_INDUSTRY_PROPERTY", "blog_industry"),
            dry_run=_bool("LQABR_SUMMARY_DRY_RUN", False),
            http_timeout_seconds=_int("LQABR_SUMMARY_HTTP_TIMEOUT_SECONDS", 15),
            max_chars=_int("LQABR_SUMMARY_MAX_CHARS", 50_000),
            max_retries=_int("LQABR_SUMMARY_MAX_RETRIES", _cfg(cfg, "mcp", "max_retries", 3)),
            mcp_backoff_base_seconds=_float("LQABR_SUMMARY_MCP_BACKOFF_BASE_SECONDS", _cfg(cfg, "mcp", "backoff_base_seconds", 1.0)),
            mcp_backoff_cap_seconds=_float("LQABR_SUMMARY_MCP_BACKOFF_CAP_SECONDS", _cfg(cfg, "mcp", "backoff_cap_seconds", 8.0)),
            mcp_retryable_statuses=retryable,
            allowed_hosts=[h.lower() for h in _list("LQABR_SUMMARY_ALLOWED_HOSTS")],
            allow_private_hosts=_bool("LQABR_SUMMARY_ALLOW_PRIVATE_HOSTS", False),
            user_agent=_str("LQABR_SUMMARY_USER_AGENT", "lqabr-summary-agent/0.1"),
            routes=_str("LQABR_SUMMARY_ROUTES", "all").lower(),
            enable_agui=_bool("LQABR_SUMMARY_ENABLE_AGUI", True),
            cors_origins=_list("LQABR_SUMMARY_CORS_ORIGINS", ("http://localhost:5173",)),
            port=_int("PORT", 8080),
            secrets_source=_str("LQABR_SUMMARY_SECRETS_SOURCE", "env").lower(),
            gcp_project=_str("LQABR_SUMMARY_GCP_PROJECT"),
            log_level=_str("LQABR_SUMMARY_LOG_LEVEL", "INFO").upper(),
            log_file=_resolve_log_file(cfg),
        )

    # ------------------------------------------------------------------
    @property
    def serves_api(self) -> bool:
        return self.routes in ("all", "api")

    @property
    def serves_chat(self) -> bool:
        return self.enable_agui and self.routes in ("all", "chat")

    def redacted(self) -> Dict[str, object]:
        """Safe to log at startup: every knob, no secret values."""
        data = {
            key: value for key, value in self.__dict__.items()
            if key not in ("mcp_auth_token",)
        }
        data["mcp_auth_token"] = "set" if self.mcp_auth_token else "unset"
        return data


_SETTINGS: Settings | None = None


def get_settings(refresh: bool = False) -> Settings:
    """Process-wide settings. `refresh=True` re-reads the environment — the
    tests use it; nothing in production should need it."""
    global _SETTINGS
    if _SETTINGS is None or refresh:
        _SETTINGS = Settings.from_env()
    return _SETTINGS
