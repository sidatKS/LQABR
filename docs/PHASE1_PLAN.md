# Phase 1 — Lead Profile Pipeline (E1, E2, E3)

**Objective:** leads flow from either source into HubSpot as complete
9-pointer profiles: manual CSV folder loads and automatic ZoomInfo pulls
(default batch of 20), through the Lead Profile Agent.

## Build/verify order

1. **Manual source (CSV), dry run** — no HubSpot writes:

   ```bash
   python agents/ingestion/src/ingestion_agent.py --source csv --dry-run
   # summary shows profiles_built > 0; unresolved records list reasons
   ```

2. **HubSpot writeback** — set `LQABR_HUBSPOT_ACCESS_TOKEN` in
   `agents/ingestion/.env`, drop the three export CSVs in a folder
   (default `data/seeds/b2b/`), run without `--dry-run`:

   ```bash
   python agents/ingestion/src/ingestion_agent.py --source csv
   ```

   Verify in HubSpot: contacts exist with `lqabr_stage = profiled`,
   `lqabr_probability = 10`, `lqabr_source = csv`, and the 9 pointers
   populated (firstname/lastname, jobtitle, company, email, phone,
   lqabr_industry, lqabr_company_size_revenue, lqabr_location,
   lqabr_linkedin_url).

3. **Automatic source (ZoomInfo)** — credentials in `.env` or Secret
   Manager, then:

   ```bash
   python agents/ingestion/src/ingestion_agent.py --source zoominfo \
       --batch-size 20 --criteria '{"jobTitle": "director"}'
   ```

   Verify contacts with `lqabr_source = zoominfo` and LinkedIn URLs.

4. **ADK harness** — `adk web agents/ingestion/src` and
   `adk web agents/lead_profile/src`; ask the agent to ingest/profile and
   confirm the full profile JSON is returned.

## Definition of Done

- Both sources produce HubSpot contacts; re-running upserts (no duplicates
  by email).
- Non-contactable records (no email AND no phone) appear as `unresolved`
  with `bad-data` reasons — count matches input defects; nothing dropped.
- `pytest agents/ingestion agents/lead_profile packages/lqabr_core` green.

**Exit:** profiles exist to be worked by outreach — Phase 2.
