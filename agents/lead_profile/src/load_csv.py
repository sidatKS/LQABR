"""STEP 2 — Load CSV data into the agent.

End goal: read the three CSVs into memory and fail loudly on structurally bad
input BEFORE any processing.

A missing file or a missing required column raises a typed error that HALTS the
run, naming the exact file and column. That is a *structural* feed failure and
is deliberately different from per-record bad data, which Step 3 flags as
unresolved and carries through.

Local files only -> no audit log, no model -> no token log. Logs: system, process.
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from pathlib import Path

from lqabr_core.obs import get_obs

# FR Step 1 — exact file names and required columns.
EMPLOYEES_FILE = "employees_with_company_sample.csv"
CONTACTS_FILE = "employee_contacts_5234.csv"
COMPANIES_FILE = "companies_clean_734.csv"

REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    EMPLOYEES_FILE: ("Employee_ID", "Company_ID", "Job_Title", "Decision_Maker_Flag"),
    CONTACTS_FILE: ("Employee_ID", "Company_ID", "Job_Title", "Email", "Phone"),
    COMPANIES_FILE: ("Company_ID", "Industry", "Annual_Revenue (M)", "Frequency_of_Purchase"),
}


class CsvStructureError(Exception):
    """Structural feed failure — halts the run. Names the exact file + column."""


def _alias_prefix(column: str) -> str | None:
    """Unit-suffixed columns may vary by unit in the real feed.

    FR §0 field 8 names the source column "Annual_Revenue (M)". The real feed
    (first seen 31 Jul) ships "Annual_Revenue (₹)" — a currency symbol, not M.
    Rule, deterministic: a required column of the form "Name (unit)" also
    matches exactly ONE header sharing the "Name (" prefix; the loader rewrites
    it to the canonical name and logs the alias. Zero matches still halts;
    more than one match halts too (ambiguity is never guessed away).

    NOTE for Mahi: the real header says currency, not millions — this sharpens
    the open annualrevenue-unit question (§8).
    """
    if " (" in column and column.endswith(")"):
        return column[: column.index(" (") + 2]
    return None


@dataclass
class RawFeed:
    """The three in-memory row lists produced by Step 2."""

    employees: list[dict[str, str]]
    contacts: list[dict[str, str]]
    companies: list[dict[str, str]]

    @property
    def counts(self) -> dict[str, int]:
        return {
            "employees": len(self.employees),
            "contacts": len(self.contacts),
            "companies": len(self.companies),
        }


def default_incoming_dir() -> Path:
    return Path(os.getenv("LQABR_INCOMING_DIR", "data/incoming"))


def _read_one(path: Path, required: tuple[str, ...]) -> list[dict[str, str]]:
    obs = get_obs()

    if not path.exists():
        raise CsvStructureError(f"missing input file: {path}")

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        header = tuple(reader.fieldnames or ())

        # canonical name -> actual header name (identity unless aliased)
        aliases: dict[str, str] = {}
        missing: list[str] = []
        for column in required:
            if column in header:
                continue
            prefix = _alias_prefix(column)
            candidates = [h for h in header if prefix and h.startswith(prefix)]
            if len(candidates) == 1:
                aliases[candidates[0]] = column
                obs.process.emit(
                    "column_alias_applied",
                    file=path.name,
                    canonical=column,
                    actual=candidates[0],
                    note="unit-suffixed source column differs from the contract name",
                )
            elif len(candidates) > 1:
                raise CsvStructureError(
                    f"{path.name}: required column {column!r} is ambiguous — "
                    f"multiple headers match: {', '.join(candidates)}"
                )
            else:
                missing.append(column)
        if missing:
            obs.process.emit(
                "column_validation_failed",
                file=path.name,
                missing_columns=missing,
                header=list(header),
            )
            raise CsvStructureError(
                f"{path.name} is missing required column(s): {', '.join(missing)}"
            )

        rows = []
        for row in reader:
            record = dict(row)
            for actual, canonical in aliases.items():
                record[canonical] = record.pop(actual, None)
            rows.append(record)

    obs.process.emit(
        "csv_loaded", file=path.name, rows=len(rows), columns=list(header), validation="pass"
    )
    return rows


def load_csvs(
    incoming_dir: str | Path | None = None,
    *,
    employees_path: str | Path | None = None,
    contacts_path: str | Path | None = None,
    companies_path: str | Path | None = None,
) -> RawFeed:
    """Read the three CSVs into memory.

    Paths are overridable per file so the future BigQuery ``fct_lead`` source
    can be swapped in without touching Step 3.
    """
    obs = get_obs()
    base = Path(incoming_dir) if incoming_dir else default_incoming_dir()

    paths = {
        EMPLOYEES_FILE: Path(employees_path) if employees_path else base / EMPLOYEES_FILE,
        CONTACTS_FILE: Path(contacts_path) if contacts_path else base / CONTACTS_FILE,
        COMPANIES_FILE: Path(companies_path) if companies_path else base / COMPANIES_FILE,
    }

    obs.process.emit("step2_start", step=2, name="load_csv", incoming_dir=str(base))
    obs.system_snapshot("step2_memory_before")

    employees = _read_one(paths[EMPLOYEES_FILE], REQUIRED_COLUMNS[EMPLOYEES_FILE])
    contacts = _read_one(paths[CONTACTS_FILE], REQUIRED_COLUMNS[CONTACTS_FILE])
    companies = _read_one(paths[COMPANIES_FILE], REQUIRED_COLUMNS[COMPANIES_FILE])

    feed = RawFeed(employees=employees, contacts=contacts, companies=companies)

    obs.process.emit("step2_complete", step=2, files_loaded=3, **feed.counts)
    obs.system_snapshot("step2_memory_after")
    return feed
