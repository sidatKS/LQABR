variable "project_id" {
  description = "GCP project id for this environment."
  type        = string
}

variable "region" {
  description = "Default region for regional resources."
  type        = string
  default     = "us-central1"
}

variable "agent_sa_name" {
  description = "Runtime service account id (the part before @)."
  type        = string
}

variable "dev_group" {
  description = "Developer group email granted deploy-and-operate roles. Empty to skip."
  type        = string
  default     = ""
}

variable "ar_repo" {
  description = "Artifact Registry docker repository name."
  type        = string
  default     = "lqabr"
}

variable "enable_apis" {
  description = "APIs to enable on the project."
  type        = list(string)
  default = [
    "run.googleapis.com",
    "cloudbuild.googleapis.com",
    "artifactregistry.googleapis.com",
    "secretmanager.googleapis.com",
    "pubsub.googleapis.com",
    "cloudscheduler.googleapis.com",
    "aiplatform.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "cloudidentity.googleapis.com",
  ]
}

variable "runtime_roles" {
  description = "Project roles bound to the runtime service account."
  type        = list(string)
  default = [
    "roles/secretmanager.secretAccessor",
    "roles/pubsub.publisher",
    "roles/pubsub.subscriber",
    "roles/run.invoker",
    "roles/aiplatform.user",
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
  ]
}

variable "deployer_group_roles" {
  description = "Project roles bound to the developer group (deploy-and-operate)."
  type        = list(string)
  default = [
    "roles/run.developer",
    "roles/cloudbuild.builds.editor",
    "roles/artifactregistry.writer",
    "roles/secretmanager.viewer",
    "roles/secretmanager.secretAccessor",
    "roles/logging.viewer",
    "roles/monitoring.viewer",
    "roles/serviceusage.serviceUsageConsumer",
  ]
}

variable "secrets" {
  description = "Secret Manager secret containers to create (values added out-of-band)."
  type        = list(string)
}

variable "pubsub_topics" {
  description = "Pub/Sub topics to create."
  type        = list(string)
  default     = ["lqabr-ingestion-trigger", "lqabr-engagement-events"]
}

variable "engagement_topic" {
  description = "Topic that gets a pull subscription."
  type        = string
  default     = "lqabr-engagement-events"
}
