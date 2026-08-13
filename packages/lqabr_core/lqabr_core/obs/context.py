"""Run context — the one run_id that spans a whole run.

Context §7.7 / FR "Observability": one run_id (UUID) correlates the run; each
record additionally carries its own lead_ref_id so a single failed insert is
individually traceable.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone

_CURRENT_RUN: ContextVar["RunContext | None"] = ContextVar("lqabr_run_context", default=None)


def new_run_id() -> str:
    """A fresh run id. One per process invocation."""
    return str(uuid.uuid4())


def new_lead_ref_id() -> str:
    """A fresh per-record reference id.

    Assigned in Step 4 BEFORE the insert is attempted, so a lead that fails
    mid-write is still individually identifiable in the audit trail.
    """
    return f"lead-{uuid.uuid4()}"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class RunContext:
    """Immutable identity of a single run."""

    run_id: str = field(default_factory=new_run_id)
    agent: str = "lead_profile_agent"
    started_at: str = field(default_factory=utc_now_iso)
    # Under ADK a model orchestrates the steps, so the tokens/model log becomes
    # required for this agent. The pure-pipeline CLI path leaves this False.
    uses_model: bool = False


def set_current_run(ctx: RunContext) -> RunContext:
    """Bind a run context to the current execution context."""
    _CURRENT_RUN.set(ctx)
    return ctx


def get_current_run() -> RunContext:
    """The bound run context, creating a default one if nothing bound it yet.

    The MCP tools are in-process and reused by other agents (email / voice /
    scheduler), so they read the caller's run context rather than owning one.
    """
    ctx = _CURRENT_RUN.get()
    if ctx is None:
        ctx = RunContext()
        _CURRENT_RUN.set(ctx)
    return ctx


def reset_current_run() -> None:
    _CURRENT_RUN.set(None)
