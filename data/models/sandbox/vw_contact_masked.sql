-- vw_contact_masked.sql — sandbox view: all contact fields EXCEPT raw PII.
-- Substitutes name → masked_name, drops email + phone, exposes email_domain + contact_hash.
-- App-devs can build and test agent logic against this without accessing real PII.
-- Executed by: infra/gcp/10_authorized_views.sh (as an authorised view)
-- Access: GROUP_APPDEV (READER on sandbox_b2b dataset only)

CREATE OR REPLACE VIEW `${PROJECT_ID}.${DS_SANDBOX}.vw_contact_masked` AS
SELECT
  client_id,
  source_system,
  employee_id,
  company_id,
  CONCAT('Contact-', SUBSTR(contact_hash, 1, 8)) AS masked_name,  -- replaces name
  job_title,
  department,
  seniority_level,
  decision_maker_flag,
  influence_score,
  campaign_response_rate_pct,
  event_attendance,
  newsletter_subscription,
  preferred_contact_method,
  language,
  tenure_years,
  education_level,
  owner_rep,
  active_flag,
  email_domain,   -- safe surrogate: domain only, no local part
  contact_hash    -- safe surrogate: SHA-256 hex of email
  -- email, phone, name intentionally omitted — access via pii-approved role on dim_contact
FROM `${PROJECT_ID}.${DS_CURATED}.dim_contact`;
