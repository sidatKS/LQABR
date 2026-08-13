# PROD remote state — separate from dev (no overlap).
# Create the bucket once (owner):
#   gsutil mb -p ldqfingsrv-prod -l us-central1 gs://ldqfingsrv-prod-tfstate
#   gsutil versioning set on gs://ldqfingsrv-prod-tfstate
terraform {
  backend "gcs" {
    bucket = "ldqfingsrv-prod-tfstate"   # CHANGE ME (prod state bucket)
    prefix = "lqabr/prod"
  }
}
