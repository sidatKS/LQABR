"""Headless entry point — one lead, one post, one note.

    python agents/research/src/agent.py --object-id 533963448020 \
                                        --summary-object-id 329473274558

Two different records: `--object-id` is the CONTACT, `--summary-object-id` is
the BLOG POST. (`--summary-ref-id` still works and means the same thing.)

stdout is the result document — pipe it to jq; the logs go to stderr. The
deterministic logic lives in pipeline.py and is what the FastAPI service and
the tests drive; this file is only the argument parsing around it.
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
from research_core import SERVICE_NAME  # noqa: E402
from research_core.settings import get_settings  # noqa: E402

from pipeline import run_research  # noqa: E402
from schema import ResearchTarget  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=SERVICE_NAME,
        description="Research one lead against one published post and write lead_context.")
    parser.add_argument("--object-id", dest="objectId", required=True,
                        help="the HubSpot CONTACT record id")
    parser.add_argument("--summary-object-id", "--summary-ref-id",
                        dest="summary_objectId", required=True,
                        help="the BLOG POST's record id — the MCP reads the blog "
                             "store by it. A different record from --object-id.")
    parser.add_argument("--dry-run", action="store_true",
                        help="compute the note and log the write, but do not send it")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_file, settings.log_format,
                      detail=settings.log_detail)

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
        ResearchTarget(objectId=args.objectId,
                       summary_objectId=args.summary_objectId),
        settings=settings)
    print(json.dumps(response.model_dump(), indent=2, default=str))
    return 0 if response.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
