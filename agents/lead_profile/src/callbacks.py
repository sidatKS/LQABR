"""ADK callbacks -> the four logs.

Without this the repo would have two parallel observability systems: ADK's own
session/event stream, and lqabr_core.obs. These callbacks bind them together.

  before_agent   binds ONE RunContext whose run_id IS the ADK invocation id, so
                 a run is traceable from the adk web event view straight into
                 the process and audit logs
  before_tool /  a process line per tool call — under an LlmAgent the step
  after_tool     ORDER is a model decision, so which tools were chosen, with
                 what arguments, and what came back, is itself an audit trail
  after_model    the tokens/model log. This agent used to have no model, so the
                 fourth stream was wired-but-silent. It is now real.
  after_agent    releases the run's in-memory feed/profiles
"""

from __future__ import annotations

from typing import Any

from lqabr_core.obs import Observability, RunContext, get_obs, set_current_run, set_obs

# Co-located module; support both package and flat (ADK loader / tests) imports.
try:
    from .tools import clear_run_store
except ImportError:  # pragma: no cover
    from tools import clear_run_store  # type: ignore

_MAX_LOGGED_VALUE = 300


def _short(value: Any) -> Any:
    """Tool results carry counts, not lead data — but never risk a huge log line."""
    text = str(value)
    return text if len(text) <= _MAX_LOGGED_VALUE else text[:_MAX_LOGGED_VALUE] + "…"


def before_agent(callback_context: Any) -> None:
    """Bind the run. run_id == ADK invocation id, so the two views line up."""
    invocation_id = getattr(callback_context, "invocation_id", None)
    ctx = RunContext(
        run_id=str(invocation_id) if invocation_id else RunContext().run_id,
        agent=getattr(callback_context, "agent_name", "lead_profile_agent"),
        uses_model=True,  # an LlmAgent orchestrates -> the tokens log is required
    )
    set_current_run(ctx)
    obs = set_obs(Observability(ctx))
    obs.system_snapshot(
        "container_up",
        agent=ctx.agent,
        started_at=ctx.started_at,
        invocation="adk",
    )
    obs.process.emit(
        "agent_invoked",
        step=0,
        orchestration="llm",
        note="an LlmAgent chooses the step order; each tool enforces its own precondition",
    )
    return None


def after_agent(callback_context: Any) -> None:
    obs = get_obs()
    obs.process.emit("agent_complete")
    obs.system_snapshot("container_down")
    clear_run_store(str(getattr(callback_context, "invocation_id", "") or "") or None)
    return None


def before_tool(tool: Any, args: dict[str, Any], tool_context: Any) -> None:
    get_obs().process.emit(
        "tool_call",
        tool=getattr(tool, "name", str(tool)),
        args={key: _short(value) for key, value in (args or {}).items()},
        chosen_by="model",
    )
    return None


def after_tool(tool: Any, args: dict[str, Any], tool_context: Any, tool_response: Any) -> None:
    response = tool_response if isinstance(tool_response, dict) else {"result": tool_response}
    get_obs().process.emit(
        "tool_result",
        tool=getattr(tool, "name", str(tool)),
        ok=response.get("ok"),
        step=response.get("step"),
        halt=response.get("halt"),
        error=response.get("error"),
    )
    return None


def after_model(callback_context: Any, llm_response: Any) -> None:
    """The fourth log — tokens/model activity, keyed by the same run_id."""
    usage = getattr(llm_response, "usage_metadata", None)
    if usage is None:
        return None
    get_obs().tokens.emit(
        "model_call",
        model=getattr(llm_response, "model_version", None),
        input_tokens=getattr(usage, "prompt_token_count", None),
        output_tokens=getattr(usage, "candidates_token_count", None),
        total_tokens=getattr(usage, "total_token_count", None),
        cached_tokens=getattr(usage, "cached_content_token_count", None),
        finish_reason=str(getattr(llm_response, "finish_reason", "") or "") or None,
    )
    return None
