"""Headless entry point + the ADK discovery shim.

    python agents/research/src/agent.py --object-id 533963448020 \
                                        --blog-published-at 2026-08-27T09:30:00Z

`root_agent` is exported so `adk web/run/api_server agents/research/src` works
the same way it does for the other agents; the deterministic logic stays in
pipeline.py and is what the FastAPI service and the tests drive.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

_AGENT_ROOT = Path(__file__).resolve().parents[1]
for _path in (str(_AGENT_ROOT / "packages"), str(_AGENT_ROOT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from research_core.obs import configure_logging, get_obs, new_run_id  # noqa: E402
from research_core.settings import get_settings  # noqa: E402

from pipeline import run_research  # noqa: E402
from schema import ResearchTarget  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lqabr-research-agent",
        description="Research one lead against one published post and write lead_context.")
    parser.add_argument("--object-id", required=True,
                        help="the HubSpot CONTACT record id")
    parser.add_argument("--summary-ref-id", required=True,
                        help="the BLOG POST's record id — the MCP reads the blog "
                             "store by it. A different record from --object-id.")
    parser.add_argument("--dry-run", action="store_true",
                        help="compute the note and log the write, but do not send it")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_file, settings.log_format)

    # For a CLI, stdout is the RESULT DOCUMENT — the caller pipes it to jq.
    # The service logs to stdout because that is what Cloud Run ingests; here
    # that would interleave log lines into the JSON and make it unparseable.
    for handler in logging.getLogger("lqabr.research").handlers:
        if type(handler) is logging.StreamHandler:      # not the FileHandler
            handler.setStream(sys.stderr)

    get_obs(new_run_id(), refresh=True)

    if args.dry_run:
        import os
        os.environ["LQABR_RESEARCH_DRY_RUN"] = "1"
        settings = get_settings(refresh=True)

    response = run_research(
        ResearchTarget(object_id=args.object_id,
                       summary_ref_id=args.summary_ref_id),
        settings=settings)
    print(json.dumps(response.model_dump(), indent=2, default=str))
    return 0 if response.status == "completed" else 1


try:  # pragma: no cover - only when ADK is installed
    from google.adk.agents import LlmAgent

    root_agent = LlmAgent(
        name="research_agent",
        model=get_settings().model,
        description="Researches a lead against a published post and writes lead_context.",
        instruction=("Given a HubSpot contact id and a blog publication timestamp, "
                     "research the lead's company and industry and write a grounded "
                     "lead_context note back to the CRM through the HubSpot MCP."),
    )
except Exception:  # noqa: BLE001 - ADK is optional; the service never needs it
    root_agent = None


if __name__ == "__main__":
    raise SystemExit(main())
