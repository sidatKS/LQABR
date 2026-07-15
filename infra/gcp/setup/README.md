# infra/gcp/setup — provisioning run log

Step-by-step record of the actual LQABR infrastructure provisioning run,
one document per numbered script. Each doc captures: what the step does,
the exact commands run, the output/verification observed, and any
deviations or gotchas hit along the way.

**Never record secret values here — names and metadata only.**

| Step | Doc | Script | Status |
|---|---|---|---|
| Prereqs | [prerequisites.md](prerequisites.md) | gcloud SDK install | done |
| Access | [access-developer-group.md](access-developer-group.md) | dev group IAM grant | done (all 10 bindings) |
| Onboarding | [developer-onboarding.md](developer-onboarding.md) | dev local gcloud setup | ready to share |
| Config | [config.md](config.md) | `config.sh` | done (env-override; file still placeholder) |
| 00 | [00-enable-apis.md](00-enable-apis.md) | `00_enable_apis.sh` | done |
| 01 | [01-service-accounts.md](01-service-accounts.md) | `01_service_accounts.sh` | done |
| 02 | [02-secret-manager.md](02-secret-manager.md) | `02_secret_manager.sh` | pending (owner — needs secret values) |
| 03 | [03-pubsub.md](03-pubsub.md) | `03_pubsub.sh` | done |
| 04 | [04-hubspot-properties.md](04-hubspot-properties.md) | `04_hubspot_properties.py` | pending (needs HubSpot token) |
| 05 | [05-deploy-agents.md](05-deploy-agents.md) | `05_deploy_agents.sh` | pending (needs config + secrets) |
| 06 | [06-cloud-scheduler.md](06-cloud-scheduler.md) | `06_cloud_scheduler.sh` | pending (needs deployed services) |
| 07 | [07-verification.md](07-verification.md) | — | pending |

Run date: started 2026-07-15.
