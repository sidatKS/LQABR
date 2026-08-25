"""One typed read of the environment. Nothing else in the agent calls os.environ.

Everything the agent touches on the outside — the model, the MCP URL, the tool
names, the HubSpot property names, the search knobs — is a variable with a
default, so a rename out there is a CONFIG change and never a code edit.

Precedence, highest first:
    1. environment variable (LQABR_RESEARCH_*)   per-deployment override
    2. config/config.yaml                        the agent's config map
    3. code default                              last resort
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple


def _str(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return int(default)
    try:
        return int(str(raw).strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return float(default)
    try:
        return float(str(raw).strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return bool(default)
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _list(name: str, default: Tuple[str, ...] = ()) -> List[str]:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


#: settings.py -> research_core -> packages -> research -> agents -> REPO ROOT
_REPO_ROOT = Path(__file__).resolve().parents[4]
_AGENT_ROOT = Path(__file__).resolve().parents[2]


def _load_config_map() -> Dict[str, object]:
    """The agent config map (YAML). Optional and dependency-tolerant: if PyYAML
    is absent or the file is missing, returns {} and the agent falls back to env
    vars + code defaults. Path: LQABR_RESEARCH_CONFIG_FILE, else
    <agent>/config/config.yaml."""
    path = os.environ.get("LQABR_RESEARCH_CONFIG_FILE", "").strip() \
        or str(_AGENT_ROOT / "config" / "config.yaml")
    try:
        import yaml  # type: ignore
        with open(path, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - the config map is a convenience, never required
        return {}


def _cfg(cfg: Dict[str, object], section: str, key: str, default):
    """One value out of the config map, with a code-default fallback."""
    sect = cfg.get(section)
    if isinstance(sect, dict) and sect.get(key) is not None:
        return sect[key]
    return default


def _resolve_path(value: str) -> str:
    """A relative path resolves against the REPO ROOT; empty disables."""
    text = str(value or "").strip()
    if not text:
        return ""
    path = Path(text)
    return str(path if path.is_absolute() else (_REPO_ROOT / path))


@dataclass(frozen=True)
class Settings:
    """Every knob this agent has, resolved once."""

    # ── model ────────────────────────────────────────────────
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 2000
    #: The model credential's NAME in Secret Manager — never its value. Resolved
    #: at run time exactly like the HubSpot token, so no key is written to disk
    #: and a rotation needs no change on anyone's machine.
    model_token_secret: str = "lqabr-anthropic-api-key"

    # ── MCP (the runtime connection to the HubSpot MCP container) ──
    mcp_base_url: str = "http://localhost:8091/mcp"
    mcp_timeout_seconds: int = 60
    mcp_auth_token: str = ""
    mcp_protocol_version: str = "2025-06-18"
    #: Tool names on that server. DISCOVERED via tools/list at startup and
    #: asserted; these are what we look for, not what we assume.
    mcp_tool_read_lead: str = "get_lead_profile"
    mcp_tool_read_blog: str = "get_blog_summary"
    mcp_tool_write: str = "upsert_lead_profile"
    #: Campaign mode only: every lead in one industry. NOT on the central MCP
    #: as of 2026-08-24 (its surface is the four tools above plus
    #: upsert_blog_summary), so /research/campaign fails loudly with the tool
    #: name until it lands. Name it here the moment it does — no code edit.
    mcp_tool_list_leads: str = "list_leads_by_industry"
    #: What the MCP calls the record id in its tool arguments. It was
    #: `object_id`; on 2026-08-25 the container began REQUIRING `objectId` and
    #: rejecting the old spelling ("Unexpected keyword argument"). A rename on
    #: their side is a config change on ours, never a code edit.
    mcp_object_id_arg: str = "objectId"
    mcp_assert_tools: bool = True
    #: warn = log and keep serving if the MCP is asleep at boot (default)
    #: strict = refuse to start   |   off = do not check at all
    mcp_startup_check: str = "warn"
    max_retries: int = 3
    mcp_backoff_base_seconds: float = 1.0
    mcp_backoff_cap_seconds: float = 8.0
    mcp_retryable_statuses: Tuple[int, ...] = (429, 500, 502, 503, 504)

    # ── HubSpot target (names owned by the HubSpot schema) ───
    hubspot_context_property: str = "lead_context"
    #: 1 = compute and log the write but never send it. Use for the first run.
    dry_run: bool = False
    #: Skip the write when the lead already carries a context note.
    skip_if_context_present: bool = False

    # ── web search ───────────────────────────────────────────
    search_enabled: bool = True
    search_max_uses: int = 5
    search_timeout_seconds: int = 90
    #: Domains the search is confined to / kept away from. Empty = no filter.
    search_allowed_domains: List[str] = field(default_factory=list)
    search_blocked_domains: List[str] = field(default_factory=list)

    # ── note shape ───────────────────────────────────────────
    note_max_chars: int = 60_000
    note_target_words: int = 160

    # ── HTTP surface ─────────────────────────────────────────
    route_campaign_a2a: str = "/research/campaign/a2a"   # gateway -> ONE POST
    cors_origins: List[str] = field(default_factory=lambda: ["http://localhost:5173"])

    # ── direct HubSpot (campaign lead lookup ONLY — see hubspot_direct.py) ──
    #: The MCP has no lead-listing tool, so "which leads are in this industry"
    #: is the one read that goes straight to HubSpot. Everything else — every
    #: lead read and every write — stays on the MCP. Set
    #: `use_direct_lead_lookup=False` the day the MCP grows the tool.
    use_direct_lead_lookup: bool = True
    #: Empty means "the default in hubspot_direct.py". The hostname literal
    #: lives only in that one exempted module, so the standalone guard stays
    #: strict about every other file.
    hubspot_base_url: str = ""
    hubspot_token_secret: str = "lqabr-hubspot-access-token"
    hubspot_timeout_seconds: int = 30

    # ── secrets + logging ────────────────────────────────────
    secrets_source: str = "env"         # env | secret_manager | auto
    gcp_project: str = ""
    log_level: str = "INFO"
    log_file: str = ""
    # console shape only — the log FILE is always JSON. "auto" means text when
    # stdout is a terminal (a human is reading) and JSON when it is not (Cloud
    # Run, a pipe), so deployed structured logging is never traded for looks.
    log_format: str = "auto"            # auto | text | json
    #: Payload previews and the parameter bag on every outbound call — the
    #: "what exactly did we send the model / the MCP" detail. On by default so
    #: a run is legible without remembering a flag; 0 gives the terser shape.
    log_detail: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        cfg = _load_config_map()
        statuses_env = _list("LQABR_RESEARCH_MCP_RETRYABLE_STATUSES")
        statuses_cfg = _cfg(cfg, "mcp", "retryable_statuses", [429, 500, 502, 503, 504])
        retryable = tuple(int(x) for x in (statuses_env if statuses_env else statuses_cfg))

        log_file = os.environ.get("LQABR_RESEARCH_LOG_FILE")
        if log_file is None:
            log_file = str(_cfg(cfg, "logging", "file",
                                "logs/agents/research/agent.log"))

        return cls(
            model=_str("LQABR_RESEARCH_MODEL", _cfg(cfg, "model", "name", "claude-sonnet-4-6")),
            max_tokens=_int("LQABR_RESEARCH_MAX_TOKENS", _cfg(cfg, "model", "max_tokens", 2000)),
            model_token_secret=_str("LQABR_RESEARCH_MODEL_TOKEN_SECRET",
                                    _cfg(cfg, "model", "token_secret",
                                         "lqabr-anthropic-api-key")),

            mcp_base_url=_str("LQABR_RESEARCH_MCP_BASE_URL",
                              _cfg(cfg, "mcp", "base_url", "http://localhost:8091/mcp")),
            mcp_timeout_seconds=_int("LQABR_RESEARCH_MCP_TIMEOUT_SECONDS",
                                     _cfg(cfg, "mcp", "timeout_seconds", 60)),
            mcp_auth_token=_str("LQABR_RESEARCH_MCP_AUTH_TOKEN"),
            mcp_protocol_version=_str("LQABR_RESEARCH_MCP_PROTOCOL_VERSION",
                                      _cfg(cfg, "mcp", "protocol_version", "2025-06-18")),
            mcp_tool_read_lead=_str("LQABR_RESEARCH_MCP_TOOL_READ_LEAD",
                                    _cfg(cfg, "mcp", "tool_read_lead", "get_lead_profile")),
            mcp_tool_read_blog=_str("LQABR_RESEARCH_MCP_TOOL_READ_BLOG",
                                    _cfg(cfg, "mcp", "tool_read_blog", "get_blog_summary")),
            mcp_tool_write=_str("LQABR_RESEARCH_MCP_TOOL_WRITE",
                                _cfg(cfg, "mcp", "tool_write", "upsert_lead_profile")),
            mcp_tool_list_leads=_str("LQABR_RESEARCH_MCP_TOOL_LIST_LEADS",
                                     _cfg(cfg, "mcp", "tool_list_leads",
                                          "list_leads_by_industry")),
            mcp_object_id_arg=_str("LQABR_RESEARCH_MCP_OBJECT_ID_ARG",
                                   _cfg(cfg, "mcp", "object_id_arg", "objectId")),
            mcp_assert_tools=_bool("LQABR_RESEARCH_MCP_ASSERT_TOOLS",
                                   _cfg(cfg, "mcp", "assert_tools", True)),
            mcp_startup_check=_str("LQABR_RESEARCH_MCP_STARTUP_CHECK",
                                   _cfg(cfg, "mcp", "startup_check", "warn")).lower(),
            max_retries=_int("LQABR_RESEARCH_MAX_RETRIES", _cfg(cfg, "mcp", "max_retries", 3)),
            mcp_backoff_base_seconds=_float("LQABR_RESEARCH_MCP_BACKOFF_BASE_SECONDS",
                                            _cfg(cfg, "mcp", "backoff_base_seconds", 1.0)),
            mcp_backoff_cap_seconds=_float("LQABR_RESEARCH_MCP_BACKOFF_CAP_SECONDS",
                                           _cfg(cfg, "mcp", "backoff_cap_seconds", 8.0)),
            mcp_retryable_statuses=retryable,

            hubspot_context_property=_str("LQABR_RESEARCH_HUBSPOT_CONTEXT_PROPERTY",
                                          _cfg(cfg, "hubspot", "context_property", "lead_context")),
            dry_run=_bool("LQABR_RESEARCH_DRY_RUN", _cfg(cfg, "hubspot", "dry_run", False)),
            skip_if_context_present=_bool("LQABR_RESEARCH_SKIP_IF_CONTEXT_PRESENT",
                                          _cfg(cfg, "hubspot", "skip_if_context_present", False)),

            search_enabled=_bool("LQABR_RESEARCH_SEARCH_ENABLED",
                                 _cfg(cfg, "search", "enabled", True)),
            search_max_uses=_int("LQABR_RESEARCH_SEARCH_MAX_USES",
                                 _cfg(cfg, "search", "max_uses", 5)),
            search_timeout_seconds=_int("LQABR_RESEARCH_SEARCH_TIMEOUT_SECONDS",
                                        _cfg(cfg, "search", "timeout_seconds", 90)),
            search_allowed_domains=_list("LQABR_RESEARCH_SEARCH_ALLOWED_DOMAINS",
                                         tuple(_cfg(cfg, "search", "allowed_domains", []) or ())),
            search_blocked_domains=_list("LQABR_RESEARCH_SEARCH_BLOCKED_DOMAINS",
                                         tuple(_cfg(cfg, "search", "blocked_domains", []) or ())),

            note_max_chars=_int("LQABR_RESEARCH_NOTE_MAX_CHARS",
                                _cfg(cfg, "note", "max_chars", 60_000)),
            note_target_words=_int("LQABR_RESEARCH_NOTE_TARGET_WORDS",
                                   _cfg(cfg, "note", "target_words", 160)),

            route_campaign_a2a=_str(
                "LQABR_RESEARCH_ROUTE_CAMPAIGN_A2A",
                _cfg(cfg, "service", "route_campaign_a2a", "/research/campaign/a2a")),
            cors_origins=_list("LQABR_RESEARCH_CORS_ORIGINS",
                               tuple(_cfg(cfg, "service", "cors_origins",
                                          ["http://localhost:5173"]) or ())),

            use_direct_lead_lookup=_bool(
                "LQABR_RESEARCH_USE_DIRECT_LEAD_LOOKUP",
                _cfg(cfg, "hubspot", "use_direct_lead_lookup", True)),
            hubspot_base_url=_str("LQABR_RESEARCH_HUBSPOT_BASE_URL",
                                  _cfg(cfg, "hubspot", "base_url", "")),
            hubspot_token_secret=_str("LQABR_RESEARCH_HUBSPOT_TOKEN_SECRET",
                                      _cfg(cfg, "hubspot", "token_secret",
                                           "lqabr-hubspot-access-token")),
            hubspot_timeout_seconds=_int("LQABR_RESEARCH_HUBSPOT_TIMEOUT_SECONDS",
                                         _cfg(cfg, "hubspot", "timeout_seconds", 30)),
            secrets_source=_str("LQABR_RESEARCH_SECRETS_SOURCE",
                                _cfg(cfg, "secrets", "source", "env")).lower(),
            gcp_project=_str("LQABR_RESEARCH_GCP_PROJECT", _cfg(cfg, "secrets", "gcp_project", "")),
            log_level=_str("LQABR_RESEARCH_LOG_LEVEL",
                           _cfg(cfg, "logging", "level", "INFO")).upper(),
            log_file=_resolve_path(log_file),
            log_format=_str("LQABR_RESEARCH_LOG_FORMAT",
                            _cfg(cfg, "logging", "format", "auto")).lower(),
            log_detail=_bool("LQABR_RESEARCH_LOG_DETAIL",
                             _cfg(cfg, "logging", "detail", True)),
        )

    # ------------------------------------------------------------------
    def redacted(self) -> Dict[str, object]:
        """Safe to log at startup: every knob, no secret values."""
        data = {key: value for key, value in self.__dict__.items()
                if key not in ("mcp_auth_token",)}
        data["mcp_auth_token"] = "set" if self.mcp_auth_token else "unset"
        #: `mcp_auth_token` is a VALUE and is blanked by the redactor, so the
        #: one fact worth seeing at boot gets a name of its own.
        data["mcp_protected"] = bool(self.mcp_auth_token)
        return data


_SETTINGS: Settings | None = None


def get_settings(refresh: bool = False) -> Settings:
    """Process-wide settings. `refresh=True` re-reads the environment — the
    tests use it; nothing in production should need it."""
    global _SETTINGS
    if _SETTINGS is None or refresh:
        _SETTINGS = Settings.from_env()
    return _SETTINGS
