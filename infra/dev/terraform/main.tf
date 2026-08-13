# ============================================================
# LQABR infra SETUP (Terraform) — mirrors the ../gcp scripts.
# Creates: APIs, runtime SA + IAM, developer-group IAM, Artifact Registry,
# Secret Manager containers, Pub/Sub topics + subscription.
# Does NOT deploy Cloud Run (that is ../deploy) and does NOT set secret
# VALUES (added out-of-band). Use gcloud OR Terraform per resource, not both.
# ============================================================

locals {
  runtime_sa_email = "${var.agent_sa_name}@${var.project_id}.iam.gserviceaccount.com"
}

# ── APIs ─────────────────────────────────────────────────────
resource "google_project_service" "apis" {
  for_each                   = toset(var.enable_apis)
  project                    = var.project_id
  service                    = each.value
  disable_on_destroy         = false
  disable_dependent_services = false
}

# ── Runtime service account ──────────────────────────────────
resource "google_service_account" "runtime" {
  project      = var.project_id
  account_id   = var.agent_sa_name
  display_name = "LQABR agent runtime"
  depends_on   = [google_project_service.apis]
}

# ── Runtime roles on the SA (project scope) ──────────────────
resource "google_project_iam_member" "runtime_roles" {
  for_each = toset(var.runtime_roles)
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.runtime.email}"
}

# ── Developer group — deploy-and-operate roles (project scope) ─
resource "google_project_iam_member" "deployer_group_roles" {
  for_each = var.dev_group == "" ? toset([]) : toset(var.deployer_group_roles)
  project  = var.project_id
  role     = each.value
  member   = "group:${var.dev_group}"
}

# ── actAs — the only deployer→runtime link ───────────────────
resource "google_service_account_iam_member" "deployer_actas" {
  count              = var.dev_group == "" ? 0 : 1
  service_account_id = google_service_account.runtime.name
  role               = "roles/iam.serviceAccountUser"
  member             = "group:${var.dev_group}"
}

# ── Artifact Registry (CI pushes images here) ────────────────
resource "google_artifact_registry_repository" "lqabr" {
  project       = var.project_id
  location      = var.region
  repository_id = var.ar_repo
  format        = "DOCKER"
  description   = "LQABR gateway, MCP and agent images"
  depends_on    = [google_project_service.apis]
}

# ── Secret Manager containers (values added out-of-band) ─────
resource "google_secret_manager_secret" "secrets" {
  for_each  = toset(var.secrets)
  project   = var.project_id
  secret_id = each.value
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

# Runtime SA may read every secret value.
resource "google_secret_manager_secret_iam_member" "runtime_accessor" {
  for_each  = google_secret_manager_secret.secrets
  project   = var.project_id
  secret_id = each.value.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime.email}"
}

# ── Pub/Sub (planned event spine) ────────────────────────────
resource "google_pubsub_topic" "topics" {
  for_each   = toset(var.pubsub_topics)
  project    = var.project_id
  name       = each.value
  depends_on = [google_project_service.apis]
}

resource "google_pubsub_subscription" "engagement_pull" {
  project              = var.project_id
  name                 = "${var.engagement_topic}-pull"
  topic                = google_pubsub_topic.topics[var.engagement_topic].id
  ack_deadline_seconds = 60
}
