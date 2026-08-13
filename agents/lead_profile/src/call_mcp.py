"""STEP 4 — Loop the list, one MCP call per record.

End goal: drive every qualified profile into HubSpot through the central MCP,
one record at a time, IN-PROCESS.

``upsert_lead_profiles(p)`` here is a Python call into the in-process MCP — not
an API hop and not a network hop to a separate MCP instance. The only network
traffic happens inside the tool, against HubSpot.

Each record is assigned its own ``lead_ref_id`` BEFORE the insert is attempted,
so a single failed lead is individually traceable — finer-grained than the one
run_id that spans the run.

Unresolved leads from Step 3 are NOT passed here.

Review finding B1: the loop now distinguishes a bad RECORD from a dead
DEPENDENCY. N consecutive transport failures trips a circuit breaker and halts
the run, instead of converting one outage into 263 misleading error records. A
systemic failure (auth) halts immediately and writes no per-lead record at all.

Logs: system, process. Network + audit happen inside Step 5.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from lqabr_core.obs import get_obs, new_lead_ref_id
from lqabr_core.leadgen.hubspot.crm import HubSpotHttp, upsert_lead_profiles
from lqabr_core.leadgen.hubspot.failures import CircuitBreaker, SystemicFailure
from lqabr_core.leadgen.hubspot.schema import LeadProfile, PushResult


@dataclass
class RunSummary:
    """Step 4 output — one run_id correlates all of it."""

    run_id: str
    profiles_seen: int = 0
    pushed: int = 0
    failed: int = 0
    # B1: split so "failed" can never again mean "the dependency was down".
    failed_record: int = 0
    failed_transport: int = 0
    halted: bool = False
    halt_reason: str | None = None
    results: list[PushResult] = field(default_factory=list)

    @property
    def summary(self) -> dict[str, int | str | bool | None]:
        return {
            "run_id": self.run_id,
            "profiles_seen": self.profiles_seen,
            "pushed": self.pushed,
            "failed": self.failed,
            "failed_record": self.failed_record,
            "failed_transport": self.failed_transport,
            "not_attempted": max(0, self.profiles_seen - len(self.results)),
            "halted": self.halted,
            "halt_reason": self.halt_reason,
        }


def push_profiles(
    profiles: list[LeadProfile],
    *,
    http: HubSpotHttp | None = None,
    breaker: CircuitBreaker | None = None,
) -> RunSummary:
    """For each profile: assign a lead_ref_id, then call the MCP write tool."""
    obs = get_obs()
    summary = RunSummary(run_id=obs.run_id, profiles_seen=len(profiles))
    breaker = breaker or CircuitBreaker()

    obs.process.emit("step4_start", step=4, name="call_mcp", profiles_seen=len(profiles))
    obs.system_snapshot("step4_memory_before")

    for index, profile in enumerate(profiles, start=1):
        lead_ref_id = new_lead_ref_id()
        obs.process.emit(
            "lead_dispatch",
            lead_ref_id=lead_ref_id,
            position=index,
            of=len(profiles),
            employee_id=profile.employee_id,
            company_id=profile.company_id,
        )

        try:
            result = upsert_lead_profiles(profile, lead_ref_id, http=http)
        except SystemicFailure as exc:
            # No lead is at fault and no lead can succeed. Stop now; the leads
            # not yet attempted are reported as not_attempted, not as failed.
            summary.halted = True
            summary.halt_reason = f"systemic: {exc}"
            obs.process.emit(
                "run_halted",
                lead_ref_id=lead_ref_id,
                reason=summary.halt_reason,
                attempted=index - 1,
                not_attempted=len(profiles) - (index - 1),
            )
            break

        summary.results.append(result)

        if result.status == "pushed":
            summary.pushed += 1
            breaker.record_success()
        else:
            summary.failed += 1
            if result.failure_kind == "transport":
                summary.failed_transport += 1
                tripped = breaker.record_transport_failure()
            else:
                summary.failed_record += 1
                breaker.record_success()  # a bad record says nothing about HubSpot
                tripped = False

            if tripped:
                summary.halted = True
                summary.halt_reason = (
                    f"circuit breaker: {breaker.consecutive} consecutive transport "
                    "failures — treating this as a dependency outage, not bad data"
                )
                obs.process.emit(
                    "run_halted",
                    lead_ref_id=lead_ref_id,
                    reason=summary.halt_reason,
                    attempted=index,
                    not_attempted=len(profiles) - index,
                )
                break

        obs.process.emit(
            "lead_result",
            lead_ref_id=lead_ref_id,
            status=result.status,
            failure_kind=result.failure_kind,
            contact_hs_id=result.contact_hs_id,
            company_hs_id=result.company_hs_id,
            reasons=result.reasons or None,
        )

        if index % 50 == 0 or index == len(profiles):
            obs.system_snapshot("step4_loop_progress", processed=index, of=len(profiles))

    obs.process.emit("step4_complete", step=4, **summary.summary)
    obs.system_snapshot("step4_memory_after")
    return summary
