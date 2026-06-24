# Foundation & Governance

All 11 scripts (00–10), SQL models, schemas, docs, and Python helpers are written and aligned to the folder structure.

## Remaining — needs you

1. Fill in `infra/gcp/config.sh` — 3 values: `PROJECT_ID`, your region, and replace the three `user:your-email@example.com` placeholders with your actual GCP account email.

2. Run the scripts from `infra/gcp/`:

   ```bash
   source ./config.sh
   bash 00_prereqs.sh   # → through → bash 10_authorized_views.sh
   ```

3. Verify — run the 5 queries in `docs/PHASE0_PLAN.md` in the BigQuery console to confirm tables, masking, row-level security, and agent SA access all work.

Once verification passes, Phase 0 is complete and E2 (API gateway) can start connecting to the `lead-agent-runtime` SA.
