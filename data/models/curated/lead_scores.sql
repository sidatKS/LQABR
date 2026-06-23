-- lead_scores.sql — qualification score table written by agents/scoring/ (E9).
-- Placeholder DDL: to be finalised when E9 scoring logic is defined.
-- The scoring agent reads fct_lead and writes one row per employee_id per run.
-- This table is the output of the E9 model — shape TBD pending qualification
-- scoring definition (open architectural decision).
--
-- Executed by: data/migrations/001_create_lead_scores.sql (Phase 1)
-- Owner: agents/scoring/

CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.${DS_CURATED}.lead_scores` (
  client_id            STRING    NOT NULL,
  source_system        STRING    NOT NULL,
  employee_id          STRING    NOT NULL,
  company_id           STRING    NOT NULL,
  score                FLOAT64,           -- 0.0–1.0 qualification score (TBD: E9)
  score_tier           STRING,            -- e.g. 'hot' | 'warm' | 'cold' (TBD: E9)
  score_reason         STRING,            -- human-readable explanation from agent
  model_version        STRING,            -- which scoring model produced this row
  scored_at            TIMESTAMP NOT NULL,
  pipeline_stage       STRING,            -- eight-stage state machine stage at scoring time
  override_flag        BOOL DEFAULT FALSE -- set true if a rep manually overrides the score
)
PARTITION BY DATE(scored_at)
CLUSTER BY client_id, score_tier;

-- TODO (E9): add scoring_features STRUCT or JSON column for model explainability
-- TODO (E6): add suppression_reason STRING for compliance-blocked leads
