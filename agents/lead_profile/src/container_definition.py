"""Container definition for lead_profile_agent.

The agent declares its MCP dependency here. The in-process HubSpot layer lives
in ``lqabr_core.leadgen.hubspot`` and is IMPORTED AND EXECUTED INSIDE THIS
AGENT'S PROCESS — there is no second process and no second MCP instance.
Validation and inserts therefore appear in this agent's own audit log.

One process · one container · one run_id · one audit log.
"""

from __future__ import annotations

from dataclasses import dataclass, field

AGENT_NAME = "lead_profile_agent"
# DECIDED 31 Jul: deterministic orchestrator is the DEFAULT — §7.4 restored.
# USES_MODEL reflects the default; LQABR_ORCHESTRATOR=llm flips it at runtime.
USES_MODEL = False


@dataclass(frozen=True)
class McpDependency:
    """A central MCP module this agent loads in-process."""

    module: str
    tools: tuple[str, ...]
    utilities: tuple[str, ...] = ()
    in_process: bool = True


@dataclass(frozen=True)
class ContainerDefinition:
    agent: str = AGENT_NAME
    uses_model: bool = USES_MODEL
    entrypoint: str = "agents/lead_profile/src/agent.py::main"
    adk_agent: str = "agents/lead_profile/src::root_agent"
    deploy_target: str = "cloud-run-job"
    mcp_dependencies: tuple[McpDependency, ...] = field(
        default_factory=lambda: (
            McpDependency(
                module="lqabr_core.leadgen.hubspot.crm",
                tools=("upsert_lead_profiles", "get_lead_profile"),
                utilities=("lqabr_core.leadgen.hubspot.auth.get_hubspot_token",),
                in_process=True,
            ),
        )
    )
    log_streams: tuple[str, ...] = ("system", "process", "audit", "tokens")
    # tokens is wired but SILENT under the deterministic default (no model).
    # It goes live only under LQABR_ORCHESTRATOR=llm.
    # NON-SECRET configuration only. Credentials are resolved at runtime from
    # Secret Manager (lqabr_core.leadgen.secrets) and never appear in the env.
    orchestrator_default: str = "deterministic"  # LQABR_ORCHESTRATOR
    required_env: tuple[str, ...] = (
        "HUBSPOT_AUTH_MODE",
        "HUBSPOT_API_BASE",
        "LQABR_INCOMING_DIR",
        "LQABR_SECRET_PROJECT",
    )
    # ANTHROPIC_API_KEY is needed ONLY under LQABR_ORCHESTRATOR=llm.
    secrets: tuple[str, ...] = (
        "ANTHROPIC_API_KEY",
        "HUBSPOT_PRIVATE_APP_TOKEN",
        "HUBSPOT_CLIENT_ID",
        "HUBSPOT_CLIENT_SECRET",
        "HUBSPOT_REFRESH_TOKEN",
    )
    # Cloud Run runtime service account needs this on each secret above.
    required_iam: tuple[str, ...] = ("roles/secretmanager.secretAccessor",)


CONTAINER = ContainerDefinition()
