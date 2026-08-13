"""Google ADK wrapper for the text_voice pipeline — `root_agent`.

Kept in its own module, separate from `text_voice.py`, specifically so
`google-adk`/`google-genai` are only required when THIS file is imported —
i.e. by `agent.py`, for `adk web`/`adk run`/`adk api_server` use. The
production webhook (`tools.py`'s /voice_agent/lead and /voice_agent/vapi_report routes) imports
`text_voice.py` directly and never touches this module, so a production
deploy that never runs `adk web` never needs `google-adk` installed for the
real pipeline to work.

`root_agent` is a *custom* ADK agent — a `google.adk.agents.BaseAgent`
subclass, ADK's "advanced concept" category — deliberately not an `LlmAgent`
(no model decides what to do) and not a template/graph workflow (no
predefined orchestration shape; ADK's own docs file graph-based workflows
under the same "Workflows" category as Sequential/Parallel/Loop, so a graph
doesn't satisfy "not workflow-driven" either — BaseAgent is the only
category left). It is a manual, `adk web`/`adk run`-only control surface
over Steps 3/7/8 in `text_voice.py`: routing is plain code, dispatching on
the shape of whatever text is typed into the chat box:

    a bare number   -> get_lead(id), the read-only lookup
    "run <id>"      -> handle_new_lead(id), places a real call
    a JSON object   -> handle_call_report(report)
    "queue"         -> list_text_voice_queue()

Nothing here is a production entry point, and "run <id>" genuinely dials
through Vapi and writes to HubSpot if real credentials are configured — it's
not a dry run.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types as genai_types

try:
    from . import text_voice
    from . import tools as tv_tools
except ImportError:  # pragma: no cover - `adk run` puts src/ on sys.path
    import text_voice  # type: ignore
    import tools as tv_tools  # type: ignore


def _text_from_user_content(ctx: InvocationContext) -> str:
    """Pull the plain text typed into `adk web`'s chat box out of `ctx`.

    `ctx.user_content` is a `google.genai.types.Content | None` — `None` on a
    turn with no new user message (e.g. a resumed session), and otherwise a
    list of `Part`s where a plain-text turn has exactly one `Part` with a
    `text` field.
    """
    content = ctx.user_content
    if content is None or not content.parts:
        return ""
    return (content.parts[0].text or "").strip()


def _event(name: str, text: str) -> Event:
    """Build an Event carrying a plain-text reply, for `adk web`/`adk run`."""
    return Event(author=name, content=genai_types.Content(
        parts=[genai_types.Part(text=text)]))


class TextVoiceAgent(BaseAgent):
    """A manual, `adk web`/`adk run`-only control surface over Steps 3/7/8.

    See the module docstring for why this is a custom BaseAgent rather than
    an LlmAgent or a template/graph workflow. The live webhook path
    (tools.py's /voice_agent/lead and /voice_agent/vapi_report routes) never touches this class —
    it calls handle_new_lead/handle_call_report directly, unaffected by
    whatever root_agent is.
    """

    def __init__(self, name: str = "text_voice_agent") -> None:
        super().__init__(
            name=name,
            description=(
                "LQABR Text/Voice Agent (Rev 5) — manual control surface and "
                "test runtime, not the production path. Type a bare HubSpot "
                "contact id to look it up, 'run <id>' to place a real call, "
                "'test <id>' for the full end-to-end live test (dial, wait "
                "for the call to end, classify, write back), a JSON "
                "call-report object to classify+push an outcome, or 'queue' "
                "to list the leads waiting in this stage."
            ),
        )

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        raw = _text_from_user_content(ctx)

        if not raw:
            yield _event(self.name, "Type a contact id, 'run <id>', a JSON "
                         "call-report, or 'queue'.")
            return

        if raw.strip().lower() == "queue":
            result = text_voice.list_text_voice_queue()
            yield _event(self.name, json.dumps(result, indent=2))
            return

        if raw.strip().startswith("{"):
            try:
                report = json.loads(raw)
            except json.JSONDecodeError as exc:
                yield _event(self.name, f"Not valid JSON: {exc}")
                return
            result = text_voice.handle_call_report(report)
            yield _event(self.name, json.dumps(result, indent=2))
            return

        if raw.lower().startswith("run "):
            object_id = raw[4:].strip()
            result = text_voice.handle_new_lead(object_id)
            yield _event(self.name, json.dumps(result, indent=2))
            return

        if raw.lower().startswith("test "):
            # Full end-to-end live test, run entirely inside the ADK runtime:
            # Steps 3+4 (dial), then poll Vapi until the call ends (instead of
            # hosting a webhook for the report), then Steps 7+8 on the fetched
            # report. Mirrors live_test.py so `adk web` can be the testing
            # runtime. This places a REAL call and writes REAL HubSpot state.
            object_id = raw[5:].strip()
            async for event in self._live_test(object_id):
                yield event
            return

        # Default: a bare id is treated as a read-only lookup, never a dial —
        # placing a real call needs the explicit "run "/"test " prefix so a
        # stray number typed into the chat box can't accidentally trigger one.
        result = text_voice.get_lead(raw.strip())
        yield _event(self.name, json.dumps(result, indent=2))

    async def _live_test(
        self, object_id: str
    ) -> AsyncGenerator[Event, None]:
        """Dial -> poll until ended -> classify + write back, with progress
        events streamed to the adk web chat as each stage completes."""
        yield _event(self.name, f"[1/3] Steps 3+4 — handle_new_lead({object_id})…")
        placed = text_voice.handle_new_lead(object_id)
        yield _event(self.name, json.dumps(placed, indent=2, default=str))

        call_id = placed.get("call_id")
        if not call_id:
            yield _event(self.name, "No call_id — dial did not happen "
                         "(see result above). Stopping.")
            return

        yield _event(self.name, f"[2/3] Polling Vapi for call {call_id} to end "
                     "— answer the phone…")
        client = tv_tools._vapi()
        call, deadline = None, time.time() + 8 * 60
        while time.time() < deadline:
            call = client._request("GET", f"/call/{call_id}")
            status = call.get("status")
            if status == "ended":
                break
            await asyncio.sleep(10)
        else:
            yield _event(self.name, "Timed out waiting for the call to end. "
                         "Steps 7+8 not run.")
            return

        # The same fields production receives via the gateway's report webhook
        # live on the Call object; rebuild the report shape from it.
        report = {
            "endedReason": call.get("endedReason"),
            "artifact": call.get("artifact") or {},
            "customer": call.get("customer") or {},
            "assistantOverrides": call.get("assistantOverrides") or {},
            "call": {"id": call_id,
                     "customer": call.get("customer") or {},
                     "assistantOverrides": call.get("assistantOverrides") or {}},
        }
        yield _event(self.name, f"[3/3] Steps 7+8 — handle_call_report "
                     f"(endedReason={report['endedReason']!r})…")
        outcome = text_voice.handle_call_report(report)
        yield _event(self.name, json.dumps(outcome, indent=2, default=str))


root_agent = TextVoiceAgent()


__all__ = ["root_agent", "TextVoiceAgent"]
