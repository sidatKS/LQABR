"""summary_agent — the ADK face of the same pipeline.

    adk web agents/summary/src          the agent in the ADK UI
    adk run agents/summary/src          one run in the terminal
    adk api_server agents/summary/src   the ADK-native HTTP surface
    summary-agent --url https://…       headless, one run, no session

All four drive the same ``root_agent``, which drives the same tools as
``pipeline.run_summary``. The model chooses which tool to call here; the
pipeline calls them in order. Nothing else differs — same prompt, same
validation, same MCP write.

.env is loaded from the agent directory before anything reads a setting,
because ADK reads it that way and a headless run must not behave differently
from `adk run`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict

HERE = Path(__file__).resolve().parent
AGENT_DIR = HERE.parent

# `adk run <dir>` imports this module as a top-level script, so its siblings
# must be importable by bare name. Do this before the local imports below.
for path in (str(HERE), str(AGENT_DIR / "packages")):
    if path not in sys.path:
        sys.path.insert(0, path)


def _load_dotenv_if_present() -> None:
    """Local convenience only. `override=False` keeps Cloud Run's injected
    environment authoritative, and pytest is skipped so a developer's real
    keys never leak into a test run."""
    if "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ:
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(AGENT_DIR / ".env", override=False)
    load_dotenv(HERE / ".env", override=False)


_load_dotenv_if_present()

from summary_core.summary_logging import configure_logging, get_obs, new_run_id  # noqa: E402
from summary_core.settings import get_settings  # noqa: E402
from summary_core.secrets import ensure_provider_credentials  # noqa: E402

import tools  # noqa: E402
from schema import HubSpotTarget, SummaryRequest  # noqa: E402
from summarizer import build_instruction  # noqa: E402

APP_NAME = "lqabr-summary"

EXIT_OK = 0
EXIT_SOURCE_FAILURE = 2
EXIT_WRITE_FAILURE = 3

AGENT_INSTRUCTION = """
{base}

HOW TO WORK

1. Call `fetch_document` with the kind of input you were given. Never fetch a
   URL any other way — the tool applies the safety and size limits.
2. If you were given a HubSpot object id and the summary should be written
   for a particular reader, you may call `get_lead_profile` first. An empty
   profile is not an error; summarise without it.
3. Produce the JSON object described above.
4. If — and only if — you were given a HubSpot object id, call
   `write_summary_to_hubspot` with that id and your JSON. Report exactly what
   the tool returned: if the write failed, say so plainly. Never describe a
   failed write as done.
5. If you were given no object id, return the summary and say that nothing
   was written.
"""


def build_root_agent():
    """The LlmAgent, wired to the tools and the shared prompt."""
    from google.adk.agents import Agent
    from google.adk.models.lite_llm import LiteLlm

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_dir,
                      settings.log_format,
                      max_bytes=settings.log_max_bytes,
                      backups=settings.log_backups,
                      log_file=settings.log_file, mode=settings.log_mode)
    tools.configure(settings)

    resolved = ensure_provider_credentials(settings.model, settings=settings)
    get_obs().process.emit("agent_build", model=settings.model,
                           secret_name=resolved.name, secret_source=resolved.source,
                           mcp_url=settings.mcp_base_url,
                           summary_property=settings.hubspot_summary_property)

    return Agent(
        name="summary_agent",
        model=LiteLlm(model=settings.model),
        description=(
            "Summarises a web page, a raw JSON payload, another service's HTTP "
            "response or plain text, and writes the summary to HubSpot through "
            "the HubSpot MCP."
        ),
        instruction=AGENT_INSTRUCTION.format(base=build_instruction()),
        tools=tools.AGENT_TOOLS,
    )


root_agent = build_root_agent()


# ---------------------------------------------------------------------------
# Headless entrypoint — the SAME steps, no session, no second orchestrator.
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="summary-agent",
        description=("LQABR Summary Agent — summarise a URL, a JSON payload, an HTTP "
                     "endpoint or text, and write the result to HubSpot through the MCP."),
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--url", help="a web page to summarise")
    source.add_argument("--endpoint", help="an HTTP/FastAPI endpoint to call and summarise")
    source.add_argument("--json-file", help="a file holding the JSON payload to summarise")
    source.add_argument("--text", help="text to summarise directly")
    parser.add_argument("--select", default=None, help="path into a JSON payload, e.g. $.data.body")
    parser.add_argument("--method", default="GET", help="HTTP method for --endpoint")
    parser.add_argument("--debug", action="store_true",
                        help="log every value whole — full payload, full "
                             "summary. Credentials stay redacted. Do not use "
                             "on a shared box.")
    parser.add_argument("--object-id", default="", help="HubSpot record to write the summary to")
    parser.add_argument("--industry", default="", help="industry to write alongside the summary")
    parser.add_argument("--dry-run", action="store_true", help="compute the write, do not send it")
    args = parser.parse_args(argv)

    if args.dry_run:
        os.environ["LQABR_SUMMARY_DRY_RUN"] = "1"

    settings = get_settings(refresh=True)
    configure_logging(settings.log_level, settings.log_dir,
                      settings.log_format,
                      max_bytes=settings.log_max_bytes,
                      backups=settings.log_backups,
                      log_file=settings.log_file,
                      mode="debug" if getattr(args, "debug", False)
                           else settings.log_mode)
    obs = get_obs(new_run_id(), refresh=True)

    if args.url:
        source_spec: Dict[str, Any] = {"kind": "url", "url": args.url}
    elif args.endpoint:
        source_spec = {"kind": "api", "endpoint": args.endpoint, "method": args.method}
    elif args.json_file:
        source_spec = {"kind": "json",
                       "payload": json.loads(Path(args.json_file).read_text(encoding="utf-8"))}
    else:
        source_spec = {"kind": "text", "text": args.text}
    if args.select:
        source_spec["select"] = args.select

    from pipeline import run_summary

    request = SummaryRequest(
        source=source_spec,
        hubspot=HubSpotTarget(object_id=args.object_id, industry=args.industry)
        if args.object_id else None,
    )
    response = run_summary(request, settings=settings, obs=obs)
    print(json.dumps(response.model_dump(), indent=2, ensure_ascii=False))

    if response.status == "completed":
        return EXIT_OK
    return EXIT_WRITE_FAILURE if response.summary else EXIT_SOURCE_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
