"""Ingestion trigger agent — dual-source entry point of the LQABR pipeline.

The operator initiates ingestion and specifies the source:

    manual   --source csv        loads the three export CSVs from a folder
    auto     --source zoominfo   pulls a batch (default 20) from the ZoomInfo API

Either way, the pulled records are normalized and handed to the Lead
Profile pipeline (lqabr_core.profile), which builds the 9-pointer profile
per lead and upserts it into HubSpot — the central lead-profile source.

Runs three ways (same pattern as every LQABR agent):
    adk web agents/ingestion/src            # ADK dev UI
    python agents/ingestion/src/ingestion_agent.py --source csv|zoominfo   # CLI
    import ... as a library (typed, mockable)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from google.adk.agents import Agent

from lqabr_core.crm import HubSpotClient
from lqabr_core.profile import profile_and_upsert

# csv_loader / zoominfo_client are co-located modules; support both package
# and flat (ADK loader) import styles.
try:
    from .csv_loader import CsvSourceError, load_csv_records
    from .zoominfo_client import ZoomInfoClient, ZoomInfoError
except ImportError:  # pragma: no cover
    from csv_loader import CsvSourceError, load_csv_records          # type: ignore
    from zoominfo_client import ZoomInfoClient, ZoomInfoError        # type: ignore

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CSV_FOLDER = _REPO_ROOT / "data" / "seeds" / "b2b"
MODEL = os.environ.get("LQABR_INGESTION_MODEL", "gemini-2.0-flash")
DEFAULT_BATCH_SIZE = int(os.environ.get("ZOOMINFO_BATCH_SIZE", "20"))


def run_ingestion(source: str,
                  csv_folder: Optional[str] = None,
                  batch_size: int = DEFAULT_BATCH_SIZE,
                  zoominfo_criteria: Optional[Dict[str, Any]] = None,
                  dry_run: bool = False) -> Dict[str, Any]:
    """Trigger one ingestion run.

    Args:
        source: "csv" (manual folder load) or "zoominfo" (automatic API pull).
        csv_folder: folder containing the three export CSVs (csv source only);
            defaults to data/seeds/b2b/.
        batch_size: ZoomInfo pull size (zoominfo source only; default 20).
        zoominfo_criteria: optional ZoomInfo search filters, e.g.
            {"jobTitle": "director", "industry": "software"}.
        dry_run: build profiles but skip the HubSpot upsert (validation runs).

    Returns:
        {"summary": {...}, "profiles": [...], "unresolved": [...],
         "upsert_failures": [...]} — unresolved/failed records always carry an
        explicit reason; no lead is ever silently dropped. On unrecoverable
        source errors returns {"error": "..."} instead of raising.
    """
    try:
        if source == "csv":
            records = load_csv_records(Path(csv_folder) if csv_folder else DEFAULT_CSV_FOLDER)
        elif source == "zoominfo":
            records = ZoomInfoClient().pull_records(zoominfo_criteria, batch_size)
        else:
            return {"error": f"unknown source '{source}' — use 'csv' or 'zoominfo'"}
    except (CsvSourceError, ZoomInfoError) as exc:
        return {"error": str(exc)}

    if dry_run:
        from lqabr_core.profile import profile_records
        result = profile_records(records)
    else:
        result = profile_and_upsert(records, HubSpotClient())

    return {
        "source": source,
        "summary": result.summary,
        "profiles": [p.to_dict() for p in result.profiles],
        "unresolved": [{"reason": u.reason,
                        "external_employee_id": u.external_employee_id,
                        "external_company_id": u.external_company_id} for u in result.unresolved],
        "upsert_failures": [{"reason": u.reason,
                             "external_employee_id": u.external_employee_id} for u in result.upsert_failures],
    }


root_agent = Agent(
    name="ingestion_agent",
    model=MODEL,
    description=("LQABR ingestion trigger: pulls leads from a CSV folder (manual) "
                 "or the ZoomInfo API (automatic, batches of 20), builds 9-pointer "
                 "lead profiles, and saves them to HubSpot."),
    instruction=(
        "You are the LQABR ingestion agent. When asked to ingest, load, pull, or "
        "import leads, call run_ingestion. Ask which source to use if the user "
        "didn't specify: 'csv' (manual folder load) or 'zoominfo' (automatic API "
        "pull, default batch of 20).\n\n"
        "Report the summary counts, list any unresolved records and upsert "
        "failures with their reasons — never hide or drop them — and confirm "
        "how many profiles were saved to HubSpot."
    ),
    tools=[run_ingestion],
)


def _cli_main(argv: Optional[list[str]] = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="LQABR ingestion trigger (manual CSV / auto ZoomInfo).")
    parser.add_argument("--source", required=True, choices=["csv", "zoominfo"])
    parser.add_argument("--csv-folder", default=None, help="Folder with the 3 export CSVs (csv source)")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="ZoomInfo pull size (default 20)")
    parser.add_argument("--criteria", default=None, help="ZoomInfo search criteria as JSON")
    parser.add_argument("--dry-run", action="store_true", help="Build profiles, skip HubSpot writes")
    args = parser.parse_args(argv)

    criteria = json.loads(args.criteria) if args.criteria else None
    result = run_ingestion(args.source, args.csv_folder, args.batch_size, criteria, args.dry_run)
    if "error" in result:
        print(f"ingestion: {result['error']}", file=sys.stderr)
        return 1
    print(json.dumps(result["summary"], indent=2))
    for u in result["unresolved"]:
        print(f"unresolved: {u}")
    for f in result["upsert_failures"]:
        print(f"upsert failure: {f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli_main())
