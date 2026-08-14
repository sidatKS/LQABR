# infra/gcp/setup — provisioning run log (DEV)

Step-by-step record of the LQABR **dev** infrastructure provisioning run
(project `ldqfingsrv-dev`, billing `01B906-D3DC6E-7DA770`), one document per
numbered script. Each doc captures: what the step does, the exact commands run,
the output/verification observed, and any deviations or gotchas hit along the
way. Dev scripts are run with `source ./config.dev.sh` (not `config.sh`).

**Never record secret values here — names and metadata only.**

| Step | Doc | Script | Status (dev) |
|---|---|---|---|
| Config | — | `config.dev.sh` | done (dev project, SA `lqabr-agent-dev`, anthropic key) |
| 00 | [00-enable-apis.md](00-enable-apis.md) | `00_enable_apis.sh` | done (2026-07-21) |
| 01 | [01-service-accounts.md](01-service-accounts.md) | `01_service_accounts.sh` | done (2026-07-21, SA `lqabr-agent-dev`) |
| 02 | [02-secret-manager.md](02-secret-manager.md) | `02_secret_manager.sh` | done (2026-07-21, 6/12 populated; zoom/zoominfo parked) |
| Access | [access-developer-group.md](access-developer-group.md) | dev group IAM grant | done (2026-07-21, ai2d@ + secretmanager.viewer/accessor) |
| 03 | [03-pubsub.md](03-pubsub.md) | `03_pubsub.sh` | pending |
| 04 | 04-hubspot-properties.md _(pending)_ | `04_hubspot_properties.py` | pending (HubSpot token present) |
| 05 | 05-deploy-agents.md _(pending)_ | `05_deploy_agents.sh` | pending (needs Anthropic model code change) |
| 06 | 06-cloud-scheduler.md _(pending)_ | `06_cloud_scheduler.sh` | pending (needs deployed services) |
| 07 | 07-verification.md _(pending)_ | — | pending |

## Open items

- **Dev-group access:** grant `secretmanager.viewer` + `secretmanager.secretAccessor`
  on `ldqfingsrv-dev` so developers can see and read dev secrets.
- **Anthropic model code change:** agents pass a bare model string (Gemini-only);
  needs a shared `build_model()` helper wrapping non-Gemini models in ADK
  `LiteLlm` + `litellm` dependency, before deploy (05).
- **Parked secrets:** ZoomInfo (SSO blocker) and Zoom (no developer access) — 4+2
  empty secret containers awaiting admin/credential access.

Run date: dev provisioning started 2026-07-21.
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
| 02 | [02-secret-manager.md](02-secret-manager.md) | `02_secret_manager.sh` | in progress (3/11: Mailgun + HubSpot) |
| 02·mg | [mailgun-secrets-commands.md](mailgun-secrets-commands.md) | Mailgun secret cmds | done |
| 02·hs | [hubspot-secrets-commands.md](hubspot-secrets-commands.md) | HubSpot secret cmds + scopes | done |
| 03 | [03-pubsub.md](03-pubsub.md) | `03_pubsub.sh` | done |
| 04 | 04-hubspot-properties.md _(pending)_ | `04_hubspot_properties.py` | pending (needs HubSpot token) |
| 05 | 05-deploy-agents.md _(pending)_ | `05_deploy_agents.sh` | pending (needs config + secrets) |
| 06 | 06-cloud-scheduler.md _(pending)_ | `06_cloud_scheduler.sh` | pending (needs deployed services) |
| 07 | 07-verification.md _(pending)_ | — | pending |

Run date: started 2026-07-15.
