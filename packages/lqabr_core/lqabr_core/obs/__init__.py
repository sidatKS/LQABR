"""Shared observability — four logs keyed by run_id + lead_ref_id.

Lives in lqabr_core/obs/ (shared), NOT under any single agent, because every
agent in the platform emits the same four streams.
"""

from .context import (
    RunContext,
    get_current_run,
    new_lead_ref_id,
    new_run_id,
    reset_current_run,
    set_current_run,
    utc_now_iso,
)
from .loggers import (
    LOG_TYPES,
    LogStream,
    Observability,
    close_file_sinks,
    get_obs,
    reset_obs,
    set_obs,
)

__all__ = [
    "LOG_TYPES",
    "LogStream",
    "Observability",
    "RunContext",
    "close_file_sinks",
    "get_current_run",
    "get_obs",
    "new_lead_ref_id",
    "new_run_id",
    "reset_current_run",
    "reset_obs",
    "set_current_run",
    "set_obs",
    "utc_now_iso",
]
