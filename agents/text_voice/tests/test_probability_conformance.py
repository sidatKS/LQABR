"""Guards the one duplication this agent knowingly carries.

text_voice_core/probability.py is a copy of lqabr_core/probability.py. The
scaffold requires the agent to stand alone; CLAUDE.md §6 and §10 require the
increments to live in one place and never be redefined. Both cannot hold, and
the copy was chosen (Rao, 2026-09-08).

The increments are the scoring contract BETWEEN agents, so drift does not
announce itself: tune VOICEMAIL_LEFT in one file and this agent starts
landing engaged leads at 45 instead of 60, missing SCHEDULING_THRESHOLD, with
every test on both sides of the boundary still green. This test is the only
thing standing between that and production.

It compares behaviour, not source text, so a reformat or a comment does not
fail it and a changed number does.

Scope, stated plainly: this can only run while both copies live in one repo.
The moment text_voice ships separately it stops being a guard, and the real
answer — the MCP returning the new probability so no agent computes it — has
to land instead.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

AGENT = Path(__file__).resolve().parents[1]
SHARED = AGENT.parents[1] / "packages" / "lqabr_core" / "lqabr_core" / "probability.py"

from text_voice_core import probability as local          # noqa: E402
from text_voice_core.types import EventType               # noqa: E402


def _load_shared():
    """Import the shared module by path, without putting lqabr_core on
    sys.path — importing it normally would defeat the independence test."""
    if not SHARED.exists():
        pytest.skip(f"shared probability.py not present at {SHARED}")
    spec = importlib.util.spec_from_file_location("_shared_probability", SHARED)
    module = importlib.util.module_from_spec(spec)

    # The shared file does `from lqabr_core.types import EventType, LeadStage`.
    # Putting lqabr_core on sys.path would defeat the independence test, so
    # stand in a stub package backed by OUR enums. They are (str, Enum) with
    # the same members, so the comparison stays honest: if a member existed
    # only on their side, exec_module would raise here rather than pass.
    import types as _types
    from text_voice_core import types as ours
    pkg = _types.ModuleType("lqabr_core")
    pkg.__path__ = []
    shim = _types.ModuleType("lqabr_core.types")
    for name in ("EventType", "LeadStage", "VoiceOutcome", "VoiceLead"):
        setattr(shim, name, getattr(ours, name))
    saved = {k: sys.modules.get(k) for k in ("lqabr_core", "lqabr_core.types")}
    sys.modules["lqabr_core"], sys.modules["lqabr_core.types"] = pkg, shim
    try:
        spec.loader.exec_module(module)
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    return module


def test_thresholds_match_the_shared_module():
    shared = _load_shared()
    assert local.TEXT_VOICE_THRESHOLD == shared.TEXT_VOICE_THRESHOLD
    assert local.SCHEDULING_THRESHOLD == shared.SCHEDULING_THRESHOLD


def test_every_increment_matches_the_shared_module():
    shared = _load_shared()
    ours = {e.value: v for e, v in local.EVENT_INCREMENTS.items()}
    theirs = {e.value: v for e, v in shared.EVENT_INCREMENTS.items()}
    assert ours == theirs, (
        "text_voice_core/probability.py has drifted from lqabr_core's copy; "
        f"ours={ours} theirs={theirs}")


@pytest.mark.parametrize("start", [0, 15, 30, 45, 60, 95])
@pytest.mark.parametrize("event", list(EventType))
def test_apply_event_agrees_with_the_shared_module(start, event):
    """Behavioural, not textual: the two must produce the same number for
    every event at every score this agent can encounter."""
    shared = _load_shared()
    assert local.apply_event(start, event) == shared.apply_event(start, event), (
        f"apply_event({start}, {event.value}) disagrees across the two copies")


def test_the_engaged_call_still_reaches_the_scheduling_threshold():
    """The rule the duplication most endangers, asserted directly: an answered
    AND engaged call is two events, 30 -> 45 -> 60, and 60 is what promotes the
    lead. Recording only CALL_ENGAGED would strand it at 45."""
    probability = 30
    probability = local.apply_event(probability, EventType.CALL_ANSWERED)
    probability = local.apply_event(probability, EventType.CALL_ENGAGED)
    assert probability == 60
    assert probability >= local.SCHEDULING_THRESHOLD
