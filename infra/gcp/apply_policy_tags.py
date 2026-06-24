#!/usr/bin/env python3
"""Attach a policy tag to PII columns (name, email, phone) on curated tables.
Usage: python3 apply_policy_tags.py PROJECT_ID DATASET POLICY_TAG_RESOURCE_NAME
Install once:  pip install google-cloud-bigquery --break-system-packages
"""
import sys
from google.cloud import bigquery
from google.cloud.bigquery.schema import PolicyTagList

PROJECT, DATASET, POLICY_TAG = sys.argv[1], sys.argv[2], sys.argv[3]
PII_COLUMNS = {"name", "email", "phone"}
TABLES = ["dim_contact", "fct_lead"]

client = bigquery.Client(project=PROJECT)
tags = PolicyTagList(names=[POLICY_TAG])

for tbl in TABLES:
    ref = f"{PROJECT}.{DATASET}.{tbl}"
    table = client.get_table(ref)
    new_schema = []
    touched = []
    for f in table.schema:
        if f.name.lower() in PII_COLUMNS:
            new_schema.append(
                bigquery.SchemaField(
                    f.name, f.field_type, mode=f.mode,
                    description=f.description, policy_tags=tags,
                )
            )
            touched.append(f.name)
        else:
            new_schema.append(f)
    table.schema = new_schema
    client.update_table(table, ["schema"])
    print(f"  {tbl}: tagged {touched}")

print("Done.")
