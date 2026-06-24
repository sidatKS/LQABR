#!/usr/bin/env python3
"""Grant dataset-level BigQuery roles per persona (least privilege).
Reads config from environment (run `source ./config.sh` first).
Install once:  pip install google-cloud-bigquery --break-system-packages

BigLake edition: DS_RAW removed (raw files live in GCS, no raw BQ dataset).
Handles both user: and group: prefixes in GROUP_* env vars automatically.
"""
import os
from google.cloud import bigquery

P  = os.environ["PROJECT_ID"]
SA = os.environ["SA_EMAIL"]

client = bigquery.Client(project=P)


def parse_principal(v: str) -> tuple[str, str]:
    """Return (entity_type, entity_id) from a 'user:x' or 'group:x' string."""
    if v.startswith("user:"):
        return ("userByEmail", v[len("user:"):])
    if v.startswith("group:"):
        return ("groupByEmail", v[len("group:"):])
    # Default: treat bare value as a user email (backwards compat)
    return ("userByEmail", v)


dataeng_type, dataeng_id = parse_principal(os.environ["GROUP_DATAENG"])
appdev_type,  appdev_id  = parse_principal(os.environ["GROUP_APPDEV"])

# dataset -> list of (role, entity_type, entity_id)
# DS_RAW intentionally absent — raw data lives in GCS, queried via BigLake external tables
PLAN = {
    os.environ["DS_CLEANSED"]: [
        ("WRITER", dataeng_type, dataeng_id),
    ],
    os.environ["DS_CURATED"]: [
        ("WRITER", dataeng_type, dataeng_id),
        ("READER", "userByEmail", SA),
    ],
    os.environ["DS_SANDBOX"]: [
        ("READER", appdev_type, appdev_id),
        ("READER", "userByEmail", SA),
    ],
}

for ds_id, grants in PLAN.items():
    ds      = client.get_dataset(f"{P}.{ds_id}")
    entries = list(ds.access_entries)
    have    = {(e.role, e.entity_type, e.entity_id) for e in entries}
    added   = []
    for role, etype, eid in grants:
        if (role, etype, eid) not in have:
            entries.append(bigquery.AccessEntry(role=role, entity_type=etype, entity_id=eid))
            added.append(f"{role}:{eid}")
    ds.access_entries = entries
    client.update_dataset(ds, ["access_entries"])
    print(f"  {ds_id}: {added if added else 'already up to date'}")

print("Dataset IAM done.")
