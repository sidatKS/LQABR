-- vw_company.sql — sandbox view: all firmographic columns, no PII.
-- App-devs and agents in dev mode query this instead of dim_company directly.
-- Executed by: infra/gcp/10_authorized_views.sh (as an authorised view)
-- Access: GROUP_APPDEV (READER on sandbox_b2b dataset only)

CREATE OR REPLACE VIEW `${PROJECT_ID}.${DS_SANDBOX}.vw_company` AS
SELECT *
FROM `${PROJECT_ID}.${DS_CURATED}.dim_company`;
-- No PII columns exist in dim_company — full projection is safe.
