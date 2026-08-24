"""Rev 5 STEPS 3, 7 and 8 — the agent logic between the two inbound routes."""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Optional, Tuple

from lqabr_core import observability as obs
from lqabr_core.crm.base import CRMError
from lqabr_core.secrets import get_secret
from lqabr_core.types import VoiceLead, VoiceOutcome

try:
    from .mcp_client import StepFiveMCPClient
    from .tools import VapiError, place_call
except ImportError:  # pragma: no cover - uvicorn/pytest put src/ on sys.path
    from mcp_client import StepFiveMCPClient  # type: ignore
    from tools import VapiError, place_call  # type: ignore


MODEL = os.environ.get("LQABR_TEXT_VOICE_MODEL", "anthropic/claude-sonnet-5")

QUALIFIED_EMAIL_STATUSES = frozenset(
    s.strip().upper() for s in
    os.environ.get("LQABR_QUALIFIED_EMAIL_STATUSES", "OPENED").split(",")
    if s.strip())

_IN_FLIGHT_VOICE_STATUSES = ("INITIATED", "CALL_PLACED")
_TERMINAL_VOICE_STATUSES = ("COMPLETED", "VOICEMAIL_LEFT", "FAILED")

mcp = StepFiveMCPClient()


_TERMINAL_ENDED_REASONS: Dict[str, VoiceOutcome] = {
    "customer-did-not-answer": VoiceOutcome.NOT_ANSWERED,
    "customer-busy": VoiceOutcome.NOT_ANSWERED,
    "twilio-failed-to-connect-call": VoiceOutcome.NOT_ANSWERED,
    "vonage-failed-to-connect-call": VoiceOutcome.NOT_ANSWERED,
    "vonage-rejected": VoiceOutcome.NOT_ANSWERED,
    "customer-did-not-give-microphone-permission": VoiceOutcome.NOT_ANSWERED,
    "pipeline-no-available-llm-model": VoiceOutcome.NOT_ANSWERED,
    "voicemail": VoiceOutcome.VOICEMAIL,
}

_FAILURE_PREFIXES = ("call.start.error-", "call-start-error-",
                     "call.in-progress.error-", "assistant-not-",
                     "assistant-request-")

_SUMMARY_INSTRUCTION = """\
You are analysing one completed outbound sales call.

Decide which ONE of these four outcomes describes what actually happened:

- "not_answered": nobody ever picked up, or the call failed to connect. No
  conversation took place.
- "voicemail": a voicemail system or answering machine picked up, not a person.
  A transcript that is only a recorded greeting ("...is not available, please
  leave a message after the tone") is voicemail, not an answered call.
- "answered_not_engaged": a real person answered, but did not agree to
  continue, was not interested, asked to be called another time, or asked to be
  removed from the list.
- "answered_and_engaged": a real person answered AND showed genuine interest —
  they answered the qualifying questions and/or agreed to receive a calendar
  link or a follow-up.

Judge from what was actually said. The endedReason is a hint about how the
call terminated, not a verdict about whether a human engaged: a call can end
because the customer hung up and still have been fully engaged.

Then write a summary of two or three sentences: what was discussed or decided,
any objection or commitment, and anything a human rep would need to know before
following up. If nothing was discussed, say so plainly instead of inventing
detail. Never state anything that is not in the transcript.

Reply with ONLY a JSON object, no code fence and no other text:
{"outcome": "<one of the four>", "summary": "<your summary>"}
"""


_VALID_OUTCOMES = {o.value for o in VoiceOutcome}


def handle_new_lead(object_id: str) -> Dict[str, Any]:
    """Steps 3 → 4 for one newly-eligible lead; every exit path returns a dict."""
    read = get_lead(object_id)
    if not read.get("callable"):
        return {"status": "stopped", "step": "3", "object_id": object_id,
                "reason": read.get("reason")}

    lead = read["lead"]

    # An unclaimable lead is NOT dialled. The claim is the only thing standing
    # between a redelivered trigger and a second call to a real person, so a
    # failed claim means stop — not "dial anyway with the guard off". The lead
    # keeps its current status and the next trigger retries it cleanly.
    try:
        mcp.upsert_lead(lead.object_id, voice_status="INITIATED", current=lead)
    except CRMError as exc:
        reason = f"crm-error: could not claim the lead, not dialling: {exc}"
        obs.log_process(obs.STEP_PLACE_CALL, "stopped",
                        "voice_status=INITIATED write failed — refusing to dial "
                        "without a claim, because a duplicate trigger would "
                        "otherwise call this person twice",
                        level=logging.ERROR, error=str(exc)[:300],
                        object_id=lead.object_id)
        return {"status": "stopped", "step": "4",
                "object_id": lead.object_id, "reason": reason}

    lead_context = read.get("lead_context") or ""

    with obs.step(obs.STEP_PLACE_CALL, object_id=lead.object_id) as step_result:
        try:
            placed = place_call(lead, lead_context=lead_context)
        except VapiError as exc:
            step_result["status"] = "error"
            _release_claim(lead.object_id, f"vapi-error: {exc}", current=lead)
            return {"status": "error", "step": "4",
                    "object_id": lead.object_id,
                    "reason": f"vapi-error: {exc}"}
        except Exception as exc:  # noqa: BLE001 — e.g. a SecretNotFoundError
            step_result["status"] = "error"
            _release_claim(lead.object_id,
                           f"pre-dial failure: {type(exc).__name__}", current=lead)
            return {"status": "error", "step": "4",
                    "object_id": lead.object_id,
                    "reason": f"pre-dial failure: {type(exc).__name__}: {exc}"}

        if placed.get("error"):
            step_result["status"] = "stopped"
            step_result["reason"] = placed["error"]
            _release_claim(lead.object_id, placed["error"], current=lead)
            return {"status": "stopped", "step": "4",
                    "object_id": lead.object_id,
                    "reason": placed["error"]}

        step_result["call_id"] = placed.get("call_id")
        step_result["lead_context_chars"] = placed.get("lead_context_chars")
        step_result["voice_status"] = _mark_call_placed(lead.object_id)

    return {"status": "initiated", "step": "4",
            "object_id": lead.object_id,
            "call_id": placed.get("call_id"), "to": placed.get("to"),
            "lead_context_chars": placed.get("lead_context_chars")}


def handle_call_report(report: Dict[str, Any]) -> Dict[str, Any]:
    """Steps 7 → 8 for one end-of-call report (Vapi's unwrapped `message`)."""
    ended_reason = str(report.get("endedReason") or "")
    artifact = report.get("artifact") or {}
    transcript = str(artifact.get("transcript") or "")
    recording_url = str(artifact.get("recordingUrl") or "")
    call = report.get("call") or {}
    call_id = str(call.get("id") or "")

    # Both cheap exits are taken BEFORE Step 7 runs: classifying costs a model
    # call, and neither path can write its result anywhere.
    object_id = _object_id_for_report(report)
    if not object_id:
        obs.log_process(obs.STEP_PUSH_MCP, "stopped",
                        "no HubSpot contact resolved for this call — Step 8 "
                        "cannot write the outcome, so it was never classified",
                        level=logging.ERROR, call_id=call_id,
                        ended_reason=ended_reason)
        return {"status": "stopped", "step": "8", "call_id": call_id,
                "ended_reason": ended_reason,
                "reason": "not-found: could not resolve a HubSpot contact for "
                          "this call report"}

    try:
        existing = mcp.get_lead(object_id)
    except Exception as exc:  # noqa: BLE001
        existing = None
        obs.log_process(obs.STEP_PUSH_MCP, "degraded",
                        "idempotency pre-check (get_lead) failed — proceeding "
                        "with Step 8 without the duplicate guard",
                        level=logging.WARNING, error=str(exc)[:200],
                        call_id=call_id, object_id=object_id)
    if existing is not None and existing.is_complete:
        obs.log_process(obs.STEP_PUSH_MCP, "skipped",
                        "duplicate end-of-call report — voice_status is already "
                        "terminal, the outcome was already written; skipping "
                        "Steps 7 and 8 to stay idempotent",
                        call_id=call_id, object_id=object_id,
                        voice_status=existing.voice_status,
                        ended_reason=ended_reason)
        return {"status": "duplicate", "step": "8", "call_id": call_id,
                "object_id": object_id, "ended_reason": ended_reason,
                "voice_status": existing.voice_status}

    decided = summarise_report(ended_reason=ended_reason, transcript=transcript)
    written = push_to_mcp(object_id, decided["outcome"],
                          summary=decided["summary"],
                          recording_url=recording_url, current=existing)
    return {"status": written.get("status", "ok"), "step": "8",
            "call_id": call_id, "object_id": object_id,
            "outcome": decided["outcome"], "summary": decided["summary"],
            "classified_by": decided.get("classified_by"),
            "probability": written.get("probability"),
            "promoted_to_scheduling": written.get("promoted_to_scheduling"),
            "failures": written.get("failures") or []}


def get_lead(object_id: str) -> Dict[str, Any]:
    """STEP 3 — fetch the lead by HubSpot object id and decide whether to dial."""
    with obs.step(obs.STEP_READ_LEAD, object_id=object_id) as outcome:
        try:
            lead, extras = mcp.get_lead_with_extras(object_id)
            lead_context = str(extras.get("lead_context") or "")
        except CRMError as exc:
            outcome["status"] = "error"
            return {"callable": False, "reason": f"crm-error: {exc}",
                    "object_id": object_id}

        if lead is None:
            outcome["status"] = "stopped"
            outcome["reason"] = "not-found"
            return {"callable": False,
                    "reason": f"not-found: no HubSpot contact with id "
                              f"{object_id}",
                    "object_id": object_id}

        obs.bind(object_id=lead.object_id)

        if (lead.email_status or "").upper() not in QUALIFIED_EMAIL_STATUSES:
            reason = (f"not-qualified: email_status="
                      f"{lead.email_status or 'unset'} (trigger requires one "
                      f"of {sorted(QUALIFIED_EMAIL_STATUSES)})")
            outcome["status"] = "stopped"
            outcome["reason"] = reason
            return {"callable": False, "reason": reason, "lead": lead,
                    "object_id": lead.object_id}

        reason = _blocking_reason(lead)
        if reason:
            outcome["status"] = "stopped"
            outcome["reason"] = reason
            return {"callable": False, "reason": reason, "lead": lead,
                    "object_id": lead.object_id}

        outcome["probability"] = lead.probability
        # Char count only — the raw lead_context is lead content and does not
        # belong on the logs (2026-08-20, user decision).
        outcome["lead_context_chars"] = len(lead_context)
        return {"callable": True, "lead": lead,
                "object_id": lead.object_id,
                "lead_context": lead_context}


def _blocking_reason(lead: VoiceLead) -> Optional[str]:
    """Step 3's stop conditions: block both in-flight statuses, then the base checks."""
    status = (lead.voice_status or "").upper()
    if status in _IN_FLIGHT_VOICE_STATUSES:
        return f"in-flight: voice_status={lead.voice_status}"
    return lead.blocking_reason()


def _mark_call_placed(object_id: Optional[str]) -> str:
    """Advance INITIATED -> CALL_PLACED now that Vapi accepted the call."""
    if not object_id:
        return "unknown"
    try:
        current = mcp.get_lead(object_id)
    except Exception as exc:  # noqa: BLE001 — best-effort guard; a failed
        current = None
        obs.log_process(obs.STEP_PLACE_CALL, "degraded",
                        "could not re-read voice_status before the CALL_PLACED "
                        "write — writing without the no-downgrade guard",
                        level=logging.WARNING, error=str(exc)[:200],
                        object_id=object_id)

    if current is not None and (current.voice_status or "").upper() in _TERMINAL_VOICE_STATUSES:
        obs.log_process(obs.STEP_PLACE_CALL, "skipped",
                        "end-of-call report already wrote a terminal status — "
                        "skipping the CALL_PLACED write so it cannot overwrite "
                        "the real outcome",
                        object_id=object_id,
                        voice_status=current.voice_status)
        return current.voice_status or "unknown"

    try:
        mcp.upsert_lead(object_id, voice_status="CALL_PLACED", current=current)
        return "CALL_PLACED"
    except CRMError as exc:
        obs.log_process(obs.STEP_PLACE_CALL, "degraded",
                        "call was placed but the CALL_PLACED write failed — "
                        "the lead stays on INITIATED",
                        level=logging.WARNING, error=str(exc)[:300],
                        object_id=object_id)
        return "INITIATED"


def _release_claim(object_id: Optional[str], reason: str,
                   current: Optional[VoiceLead] = None) -> None:
    """Undo the pre-dial INITIATED claim (as FAILED) when the call never happened."""
    if not object_id:
        return
    try:
        mcp.upsert_lead(object_id, voice_status="FAILED", current=current)
    except CRMError as exc:
        obs.log_process(obs.STEP_PLACE_CALL, "degraded",
                        "call was never placed but the FAILED rollback also "
                        "failed — contact is stranded on INITIATED",
                        level=logging.ERROR, error=str(exc)[:300],
                        object_id=object_id, dial_failure_reason=reason)


def summarise_report(ended_reason: str = "", transcript: str = "") -> Dict[str, Any]:
    """STEP 7 — decide the outcome and write the summary."""
    transcript = (transcript or "").strip()
    with obs.step(obs.STEP_SUMMARISE, ended_reason=ended_reason,
                  transcript_chars=len(transcript)) as step_result:

        terminal = _classify_from_ended_reason(ended_reason)
        if terminal is not None:
            step_result["outcome"] = terminal.value
            step_result["classified_by"] = "ended_reason"
            return {"outcome": terminal.value,
                    "summary": _deterministic_summary(ended_reason, transcript, terminal),
                    "classified_by": "ended_reason", "ended_reason": ended_reason}

        if not transcript:
            step_result["outcome"] = VoiceOutcome.VOICEMAIL.value
            step_result["classified_by"] = "empty_transcript"
            return {"outcome": VoiceOutcome.VOICEMAIL.value,
                    "summary": ("Call connected but no speech was transcribed, so "
                                "no person was confirmed on the line "
                                f"(endedReason: {ended_reason or 'unknown'})."),
                    "classified_by": "empty_transcript",
                    "ended_reason": ended_reason}

        try:
            decided = _model_classify(ended_reason, transcript)
        except Exception as exc:  # noqa: BLE001 - never block Step 8
            fallback = VoiceOutcome.ANSWERED_NOT_ENGAGED
            obs.log_process(obs.STEP_SUMMARISE, "degraded",
                            "model classification failed — falling back to a "
                            "deterministic outcome so Step 8 still runs",
                            level=logging.ERROR, error=str(exc)[:300],
                            model=MODEL)
            step_result["outcome"] = fallback.value
            step_result["classified_by"] = "fallback"
            return {"outcome": fallback.value,
                    "summary": _deterministic_summary(ended_reason, transcript, fallback),
                    "classified_by": "fallback", "ended_reason": ended_reason,
                    "error": f"model-error: {exc}"}

        step_result["outcome"] = decided["outcome"]
        step_result["classified_by"] = "model"
        decided["ended_reason"] = ended_reason
        return decided


def _classify_from_ended_reason(ended_reason: str) -> Optional[VoiceOutcome]:
    """The outcome when endedReason alone settles it, else None."""
    reason = (ended_reason or "").strip()
    if reason in _TERMINAL_ENDED_REASONS:
        return _TERMINAL_ENDED_REASONS[reason]
    if any(reason.startswith(prefix) for prefix in _FAILURE_PREFIXES):
        return VoiceOutcome.NOT_ANSWERED
    return None


def _deterministic_summary(ended_reason: str, transcript: str,
                           outcome: VoiceOutcome) -> str:
    """A factual summary for the paths that never involve the model."""
    if outcome is VoiceOutcome.NOT_ANSWERED:
        return (f"Call did not connect to a person (endedReason: "
                f"{ended_reason or 'unknown'}). No conversation took place.")
    if outcome is VoiceOutcome.VOICEMAIL:
        return (f"Call reached a voicemail system (endedReason: "
                f"{ended_reason or 'unknown'}); the voicemail message was left. "
                "No live conversation took place.")
    spoken = len((transcript or "").split())
    return (f"Call was answered and ran for roughly {spoken} transcribed words "
            f"(endedReason: {ended_reason or 'unknown'}). Outcome classified "
            "without the model — see process_log for why.")


def _model_classify(ended_reason: str, transcript: str) -> Dict[str, Any]:
    """One completion: classify the call and summarise it; tokens/latency to process_log."""
    import litellm

    model = _litellm_model_name(MODEL)

    if model.startswith("anthropic/"):
        os.environ["ANTHROPIC_API_KEY"] = get_secret("lqabr-anthropic-api-key")

    prompt = (f"endedReason: {ended_reason or 'unknown'}\n\n"
              f"Transcript:\n{transcript}")

    started = time.perf_counter()
    response = litellm.completion(
        model=model,
        messages=[{"role": "system", "content": _SUMMARY_INSTRUCTION},
                  {"role": "user", "content": prompt}],
        max_tokens=500,
    )
    latency_ms = (time.perf_counter() - started) * 1000

    content = (response.choices[0].message.content or "").strip()
    usage = getattr(response, "usage", None)
    outcome, summary = _parse_model_reply(content)

    obs.log_model_call(obs.STEP_SUMMARISE, model=model, latency_ms=latency_ms,
                       input_tokens=getattr(usage, "prompt_tokens", None),
                       output_tokens=getattr(usage, "completion_tokens", None),
                       outcome=outcome)

    return {"outcome": outcome, "summary": summary, "classified_by": "model"}


def _parse_model_reply(content: str) -> Tuple[str, str]:
    """Outcome + summary out of the model's reply; raises on anything out of vocabulary."""
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0].strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"model reply contained no JSON object: {content[:200]!r}")

    parsed = json.loads(text[start:end + 1])
    outcome = str(parsed.get("outcome", "")).strip().lower()
    if outcome not in _VALID_OUTCOMES:
        raise ValueError(f"model returned unknown outcome {outcome!r}; "
                         f"expected one of {sorted(_VALID_OUTCOMES)}")
    summary = str(parsed.get("summary") or "").strip()
    if not summary:
        raise ValueError("model returned an empty summary")
    return outcome, summary


def _litellm_model_name(model_name: str) -> str:
    """litellm needs a provider prefix; bare gemini-* names get one, others pass through."""
    if "/" in model_name:
        return model_name
    if model_name.startswith("gemini"):
        return f"gemini/{model_name}"
    return model_name


def push_to_mcp(object_id: str, outcome: str, summary: str = "",
                recording_url: str = "",
                current: Optional[VoiceLead] = None) -> Dict[str, Any]:
    """STEP 8 — make this call's result the lead's state via mcp.record_call_outcome."""
    with obs.step(obs.STEP_PUSH_MCP, object_id=object_id, outcome=outcome) as step_result:
        try:
            result = mcp.record_call_outcome(object_id, outcome, current=current)
        except CRMError as exc:
            step_result["status"] = "error"
            return {"status": "error", "reason": f"crm-error: {exc}",
                    "object_id": object_id, "outcome": outcome}

        result["summary"] = summary
        result["recording_url"] = recording_url
        step_result["status"] = result.get("status", "ok")
        step_result["probability"] = result.get("probability")
        step_result["promoted_to_scheduling"] = result.get("promoted_to_scheduling")
        if result.get("failures"):
            step_result["failures"] = result["failures"]
        return result


def _object_id_for_report(report: Dict[str, Any]) -> Optional[str]:
    """Resolve the report's contact from the `object_id` Step 4 put in variableValues."""
    call = report.get("call") or {}
    for source in (report.get("assistantOverrides"),
                   call.get("assistantOverrides")):
        if isinstance(source, dict):
            variables = source.get("variableValues")
            if isinstance(variables, dict) and variables.get("object_id"):
                return str(variables["object_id"])
    return None


obs.configure()

__all__ = [
    "get_lead", "summarise_report", "push_to_mcp",
    "handle_new_lead", "handle_call_report",
]
