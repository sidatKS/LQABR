#!/usr/bin/env python3
"""Authorize the sandbox views to read the curated dataset.
An authorized view lets app-devs query it without holding access to the
underlying curated tables. Reads config from environment (source config.sh).
Install once:  pip install google-cloud-bigquery --break-system-packages
"""
import os
from google.cloud import bigquery

P = os.environ["PROJECT_ID"]
CUR = os.environ["DS_CURATED"]
SBX = os.environ["DS_SANDBOX"]
VIEWS = ["vw_company", "vw_contact_masked", "vw_lead_masked"]

client = bigquery.Client(project=P)
ds = client.get_dataset(f"{P}.{CUR}")
entries = list(ds.access_entries)
existing = {(e.entity_type, str(e.entity_id)) for e in entries}

for v in VIEWS:
    ref = {"projectId": P, "datasetId": SBX, "tableId": v}
    if ("view", str(ref)) not in existing:
        entries.append(bigquery.AccessEntry(None, "view", ref))

ds.access_entries = entries
client.update_dataset(ds, ["access_entries"])
print(f"Authorized {len(VIEWS)} views on {CUR}.")
