output "project_id" {
  value = var.project_id
}

output "runtime_service_account" {
  description = "Runtime SA every Cloud Run service is deployed as."
  value       = google_service_account.runtime.email
}

output "artifact_registry_repo" {
  description = "Docker repo path CI pushes images to."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.lqabr.repository_id}"
}

output "secret_ids" {
  description = "Secret Manager containers created (values set out-of-band)."
  value       = [for s in google_secret_manager_secret.secrets : s.secret_id]
}

output "pubsub_topics" {
  value = [for t in google_pubsub_topic.topics : t.name]
}
