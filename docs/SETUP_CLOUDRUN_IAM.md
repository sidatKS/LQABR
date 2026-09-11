# LQABR on Cloud Run — Service Accounts, IAM & Network Setup

**Project:** `leadgen-snbox` (ID `leadgen-snbox-11b7c`, number `801662074682`) · **Region:** `us-central1` · **As of:** 2026-08-23

> **SUPERSEDED IN PART (2026-08-26).** The build was executed against **`ldqfingsrv-dev`**,
> not `leadgen-snbox-11b7c`, and with a **single** runtime service account
> (`lqabr-agent-dev`) rather than the six below. `leadgen-snbox-11b7c` is a shared project
> already hosting unrelated Firebase/Stripe/Meta workloads, which conflicts with the
> blast-radius premise of this document. The as-built record, all decisions (D1–D9), and the
> phase-by-phase commands are in **`docs/CloudRun_RunBook.md`** — treat that as authoritative
> for what exists; this document remains the design rationale.
**Follows:** `IAMandPermisssionstructure.pdf` — the three-tier model is kept exactly:

| Tier | Identity | May do | May NOT do |
| --- | --- | --- | --- |
| **Grantor** | `swaroop@` (Owner) | enable APIs, create SAs, grant ALL IAM — once | (does not deploy day-to-day) |
| **Deployer** | group `ai2d@aidefinitive.com` (member `aidcld@`) | build, push, deploy `--service-account=…` | enable APIs, edit IAM, append policy |
| **Runtime** | per-service SAs below | only what its roles allow | grant anything |

The single link between deployer and runtime stays **`actAs` only** (`roles/iam.serviceAccountUser` on each SA — no membership, no shared roles). The one deliberate extension to the PDF: the single `lqabr-agent-dev` SA becomes **six per-service SAs**, because "email can read the Mailgun key" and "voice can read the Vapi key" must not be the same permission.

---

## 1. Topology

```
            PUBLIC                    │                PRIVATE (ingress=internal, VPC egress)
                                      │
HubSpot ──webhook──►┌───────────────┐ │  ID token   ┌──────────────┐
Vapi ──call-report─►│   GATEWAY     │─┼────────────►│ research 8080│──┐
Mailgun ─events───► │ (ingress=all, │ │             ├──────────────┤  │ ID token
                    │  unauth+HMAC) │─┼────────────►│ email        │──┤
                    └───────────────┘ │             ├──────────────┤  │   ┌─────────────┐
                                      │        ┌───►│ text_voice   │──┼──►│ HUBSPOT MCP │──► HubSpot API
   operator/scheduler ────────────────┼────────┘    ├──────────────┤  │   │ (internal,  │    (egress via
        (ID token, invoker)           │             │ summary      │──┘   │  auth req'd)│     Cloud NAT)
                                      │             └──────────────┘      └─────────────┘
```

- **Gateway** is the ONLY public service. HubSpot/Mailgun/Vapi cannot present Google
  identity, so it allows unauthenticated **and verifies provider signatures in-app**
  (HubSpot v3 HMAC, `x-vapi-secret`, Mailgun HMAC). Gap **K4 becomes mandatory here.**
- **Agents + MCP** run `--ingress=internal` — unreachable from the internet even with
  a leaked URL. This retires gap **K3** (their unauthenticated routes) by network + IAM.
- **MCP is the only door to HubSpot**: only its SA holds the HubSpot token. Agents hold
  Anthropic/Mailgun/Vapi credentials but never a HubSpot one.
- All services use Cloud Run's **gen2 managed sandbox** (gVisor); containers run as `nobody`.
- Outbound (HubSpot API, Anthropic, Mailgun, Vapi) leaves through **Direct VPC egress +
  Cloud NAT** → one fixed egress IP you can allowlist at the SaaS side.

---

## 2. Setup — done ONCE by `swaroop@`

### 2.1 APIs

```bash
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com \
  compute.googleapis.com vpcaccess.googleapis.com aiplatform.googleapis.com \
  logging.googleapis.com monitoring.googleapis.com --project=leadgen-snbox-11b7c
```

### 2.2 Runtime service accounts (six, least privilege)

```bash
for SA in gtwy mcp summary research email txtv; do
  gcloud iam service-accounts create "lqabr-${SA}-dev" \
    --display-name="LQABR ${SA} runtime (dev)" --project=leadgen-snbox-11b7c
done
```

### 2.3 Project-level runtime roles (observability only — everything else is resource-scoped)

```bash
for SA in gtwy mcp summary research email txtv; do
  M="serviceAccount:lqabr-${SA}-dev@leadgen-snbox-11b7c.iam.gserviceaccount.com"
  gcloud projects add-iam-policy-binding leadgen-snbox-11b7c --member=$M --role=roles/logging.logWriter
  gcloud projects add-iam-policy-binding leadgen-snbox-11b7c --member=$M --role=roles/monitoring.metricWriter
done
```

Deliberately **NOT** granted project-wide: `secretmanager.secretAccessor` and
`run.invoker` — both are bound per-resource below, which is the whole point of
splitting the SAs.

### 2.4 Secrets — per-SECRET accessor bindings

| Secret | Readable by (only) |
| --- | --- |
| `lqabr-hubspot-access-token` | **mcp** |
| `lqabr-hubspot-app-secret` (webhook HMAC) | **gtwy** |
| `lqabr-vapi-webhook-secret` | **gtwy**, **txtv** |
| `lqabr-anthropic-api-key` | **summary**, **research**, **txtv** |
| `lqabr-mailgun-api-key`, `lqabr-mailgun-webhook-signing-key` | **email** (signing key also **gtwy** if it relays Mailgun events) |
| `lqabr-vapi-api-key` | **txtv** |

```bash
grant() { gcloud secrets add-iam-policy-binding "$1" --project=leadgen-snbox-11b7c \
  --member="serviceAccount:lqabr-$2-dev@leadgen-snbox-11b7c.iam.gserviceaccount.com" \
  --role=roles/secretmanager.secretAccessor; }

grant lqabr-hubspot-access-token mcp
grant lqabr-hubspot-app-secret   gtwy
grant lqabr-vapi-webhook-secret  gtwy;  grant lqabr-vapi-webhook-secret  txtv
grant lqabr-anthropic-api-key    summary; grant lqabr-anthropic-api-key research; grant lqabr-anthropic-api-key txtv
grant lqabr-mailgun-api-key      email
grant lqabr-mailgun-webhook-signing-key email
grant lqabr-vapi-api-key         txtv
```

Result: a compromised email agent can send email — it cannot read HubSpot's token
or place calls. The blast radius of any one service is its own vendor.

### 2.5 Service-to-service invocation (`run.invoker` on the SERVICE, per caller)

| Callee ⟶ | gateway | summary | research | email | txtv | mcp |
| --- | :-: | :-: | :-: | :-: | :-: | :-: |
| **allUsers** (public) | ✅¹ | — | — | — | — | — |
| gtwy SA | — | — | ✅ | ✅ | ✅ | — |
| summary SA | — | — | — | — | — | ✅ |
| research SA | — | — | — | — | — | ✅ |
| email SA | — | — | — | — | — | ✅ |
| txtv SA | — | — | — | — | — | ✅ |
| group `ai2d@` | ✅ | ✅² | ✅² | ✅² | ✅² | ✅² |

¹ webhooks can't do IAM — the app-level HMAC is the boundary (K4).
² developers may invoke privately for testing (with an ID token); the public internet still cannot.

```bash
inv() { gcloud run services add-iam-policy-binding "$1" --region=us-central1 --project=leadgen-snbox-11b7c \
  --member="$2" --role=roles/run.invoker; }
G="serviceAccount:lqabr-gtwy-dev@leadgen-snbox-11b7c.iam.gserviceaccount.com"

gcloud run services add-iam-policy-binding lqabr-dev-gtwy --region=us-central1 \
  --member=allUsers --role=roles/run.invoker          # public front door
for SVC in lqabr-dev-research lqabr-dev-email lqabr-dev-txtv; do inv $SVC "$G"; done
for A in summary research email txtv; do
  inv lqabr-dev-mcp "serviceAccount:lqabr-${A}-dev@leadgen-snbox-11b7c.iam.gserviceaccount.com"
done
for SVC in lqabr-dev-gtwy lqabr-dev-summary lqabr-dev-research lqabr-dev-email lqabr-dev-txtv lqabr-dev-mcp; do
  inv $SVC "group:ai2d@aidefinitive.com"
done
```

### 2.6 Deployer group — unchanged from the PDF, plus `actAs` on each new SA

```bash
for R in roles/run.developer roles/cloudbuild.builds.editor roles/artifactregistry.writer \
         roles/secretmanager.viewer roles/logging.viewer roles/monitoring.viewer; do
  gcloud projects add-iam-policy-binding leadgen-snbox-11b7c --member="group:ai2d@aidefinitive.com" --role=$R
done
for SA in gtwy mcp summary research email txtv; do
  gcloud iam service-accounts add-iam-policy-binding \
    "lqabr-${SA}-dev@leadgen-snbox-11b7c.iam.gserviceaccount.com" \
    --member="group:ai2d@aidefinitive.com" --role=roles/iam.serviceAccountUser
done
```

`aidcld@` cannot enable an API or mint a role — it deploys and impersonates at deploy
time.

> **CORRECTION (2026-08-26) — the "cannot touch a secret's value" claim does not hold as
> deployed.** In `ldqfingsrv-dev`, `group:ai2d@aidefinitive.com` holds project-wide
> `roles/secretmanager.secretAccessor` and therefore **can** read secret values. This is a
> deliberate, recorded deviation: `agents/text_voice/setup_env.sh` depends on it for local
> onboarding (it pulls `lqabr-vapi-api-key` and `lqabr-anthropic-api-key` into a git-ignored
> `.env`), and removing the role breaks that path for the whole group.
>
> Note also that `ai2d@` holds `roles/iam.serviceAccountUser` on the runtime SA, so a member
> can impersonate it and reach the same secrets regardless. The `secretAccessor` grant mainly
> determines whether that access is direct or appears in the audit trail as impersonation.
>
> The deploy path itself never needed it: `--set-secrets` requires the **runtime** SA to hold
> `secretAccessor`; the deployer needs only `secretmanager.viewer`.
>
> **Production fix — split the human group, not the service account.** The deployer/runtime
> split is already correct (deployer = group, runtime = service account); no second service
> account is required. Split `ai2d@` into `ai2d-deploy@` (the six roles above, no
> `secretAccessor`) and `ai2d-dev@` (`secretAccessor` only, for local development).
> Tracked as D9 in `docs/CloudRun_RunBook.md`.

### 2.7 Network (private ring + fixed egress IP)

```bash
gcloud compute networks create lqabr-vpc --subnet-mode=custom --project=leadgen-snbox-11b7c
gcloud compute networks subnets create lqabr-run-uscentral1 \
  --network=lqabr-vpc --region=us-central1 --range=10.10.0.0/24
gcloud compute routers create lqabr-router --network=lqabr-vpc --region=us-central1
gcloud compute addresses create lqabr-egress-ip --region=us-central1
gcloud compute routers nats create lqabr-nat --router=lqabr-router --region=us-central1 \
  --nat-external-ip-pool=lqabr-egress-ip --nat-all-subnet-ip-ranges
```

`lqabr-egress-ip` is the ONE address HubSpot/Mailgun/Vapi ever see — allowlist it there.

---

## 3. Deploy — by `aidcld@` (uses what was granted, grants nothing)

Images: `us-central1-docker.pkg.dev/leadgen-snbox-11b7c/lqabr/lqabr-dev-<component>:<ver>`
(built via the existing compose files / Cloud Build; `artifactregistry.writer` covers push).

```bash
REG=us-central1-docker.pkg.dev/leadgen-snbox-11b7c/lqabr
COMMON="--region=us-central1 --project=leadgen-snbox-11b7c --execution-environment=gen2 \
        --network=lqabr-vpc --subnet=lqabr-run-uscentral1 --vpc-egress=all-traffic"

# MCP — private, auth required, holds the ONLY HubSpot credential
gcloud run deploy lqabr-dev-mcp --image=$REG/lqabr-dev-mcp:0.1.0 $COMMON \
  --service-account=lqabr-mcp-dev@leadgen-snbox-11b7c.iam.gserviceaccount.com \
  --ingress=internal --no-allow-unauthenticated \
  --set-secrets=LQABR_SECRET_HUBSPOT_PRIVATE_APP_TOKEN=lqabr-hubspot-access-token:latest \
  --set-env-vars=LQABR_SECRET_PROJECT=leadgen-snbox-11b7c

# Agents — private, auth required, MCP URL injected
MCP_URL=$(gcloud run services describe lqabr-dev-mcp --region=us-central1 --format='value(status.url)')
for A in summary research email txtv; do
  gcloud run deploy lqabr-dev-$A --image=$REG/lqabr-dev-$A:0.1.0 $COMMON \
    --service-account=lqabr-${A}-dev@leadgen-snbox-11b7c.iam.gserviceaccount.com \
    --ingress=internal --no-allow-unauthenticated \
    --set-env-vars=LQABR_${A^^}_MCP_BASE_URL=${MCP_URL}/mcp
done   # per-agent --set-secrets: anthropic / mailgun / vapi keys as in §2.4

# Gateway — the ONLY public service
gcloud run deploy lqabr-dev-gtwy --image=$REG/lqabr-dev-gtwy:0.1.0 $COMMON \
  --service-account=lqabr-gtwy-dev@leadgen-snbox-11b7c.iam.gserviceaccount.com \
  --ingress=all --allow-unauthenticated \
  --set-secrets=HUBSPOT_APP_SECRET=lqabr-hubspot-app-secret:latest,LQABR_VAPI_WEBHOOK_SECRET=lqabr-vapi-webhook-secret:latest \
  --set-env-vars="LQABR_GATEWAY_PUBLIC_URL=https://lqabr-dev-gtwy-<hash>-uc.a.run.app,\
LQABR_RESEARCH_AGENT_URL=<research URL>/research/a2a,\
LQABR_EMAIL_AGENT_URL=<email URL>/hubspot/campaign,\
LQABR_TEXT_VOICE_AGENT_URL=<txtv URL>/voice_agent/lead,\
LQABR_VOICE_REPORT_URL=<txtv URL>/voice_agent/vapi_report"
```

HubSpot webhook Target URL → `https://lqabr-dev-gtwy-<hash>-uc.a.run.app/hubspot/events`
(ngrok retires entirely). Vapi assistant Server URL → the gateway's `/call-report`
(relay re-enabled: `vapi.report.enabled: true`, since txtv is private). Mailgun events
either stay **polled** (as in the local MVP) or get a gateway relay path later.

---

## 4. Config deltas vs the local MVP

| Knob | Local | Cloud Run |
| --- | --- | --- |
| Gateway `ingress.signature.enabled` | `false` | **`true`** (HUBSPOT_APP_SECRET set) — K4 closed |
| Gateway `vapi.report.enabled` | `false` (polling) | **`true`** — Vapi cannot reach private txtv |
| MCP auth | hourly `gcloud` ADC | **none needed** — the SA *is* the identity (no reauth ritual, K7 gone) |
| Secrets | `.env` files | `--set-secrets` from Secret Manager only |
| Agent URLs | `127.0.0.1:<port>` | Cloud Run URLs via `LQABR_<AGENT>_AGENT_URL` |
| Logs | `logs/` files | stdout → Cloud Logging (`audit.sink: stdout`) |

**One code change required (all Python callers):** private callees require a
**Google-signed ID token** with `audience=<callee URL>`. The gateway's A2A client
and each agent's MCP client must attach `Authorization: Bearer <id-token>` fetched
from the metadata server (`google.auth.fetch_id_token`) — attach only when the
target is `https://…run.app`, so local loopback keeps working unchanged.

---

## 5. Why this holds (threat → control)

| Threat | Control |
| --- | --- |
| Leaked agent/MCP URL | `ingress=internal` + IAM — unreachable, and uninvokable without `run.invoker` |
| Forged HubSpot webhook | v3 HMAC verified in the gateway (K4) |
| Forged Vapi report | `x-vapi-secret` verified at the gateway before relay |
| Email agent compromised | its SA reads Mailgun keys only — no HubSpot token exists in its world |
| Any agent writing HubSpot directly | impossible: only `lqabr-mcp-dev` can read the token; MCP validates + audits every write |
| Deployer key abuse | `aidcld@` can deploy but not edit IAM or enable APIs. **It CAN currently read secret values** — see the correction in §2.6; the intended control is `secretmanager.viewer` only |
| Untraceable egress | single NAT IP; every hop logged (Cloud Logging + the gateway's audit stream) |
