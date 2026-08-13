"""The accuracy corpus as a regression gate.

65 labeled utterances in evals/corpus.py. The deterministic parser must hold:
tier A+B executed exactly; tier C+D never produce a write action. Any change
to the grammar that breaks these numbers fails CI, not production.
"""

from __future__ import annotations

import sys
from pathlib import Path

# corpus.py lives in the repo-root evals/ dir; parse_command is the agent's
# deterministic parser, imported by bare name (conftest puts src/ on the path).
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "evals"))

from corpus import TIERS  # noqa: E402

from pipeline_agent import parse_command  # noqa: E402


def test_documented_and_paraphrase_tiers_are_100_percent():
    for tier_name in ("A_documented", "B_paraphrase"):
        for text, expected in TIERS[tier_name]:
            got = {k: v for k, v in parse_command(text).items() if k != "message"}
            assert got == expected, f"{tier_name}: {text!r} -> {got}, wanted {expected}"


def test_novel_and_dangerous_tiers_never_write():
    for tier_name in ("C_novel", "D_dangerous"):
        for text, _ in TIERS[tier_name]:
            action = parse_command(text)["action"]
            assert action != "run", f"{tier_name}: {text!r} produced a WRITE action"
