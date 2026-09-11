# LQABR Agent Gateway — build, push & deploy spec

> **§0 · DOCUMENT CONTROL**
>
> | Field | Value |
> | --- | --- |
> | **Document id** | `gateway_deploy_spec` |
> | **Version** | `1.0` |
> | **As of** | 2026-08-29 |
> | **Component** | `gateway` → service `lqabr-dev-gtwy` |
> | **Companion** | `docs/gateway_verify_spec.md` |
> | **Siblings** | `docs/summary_deploy_spec.md`, `docs/research_deploy_spec.md` — **the gateway breaks their pattern in five ways; see §1.1** |
> | **Supersedes** | `infra/gcp/07_deploy_gateway.sh` for this component |
>
> **Precedence, highest first:** 1. the code (`agents/gateway/**`); 2. this document;
> 3. any other script or document. **Derived from a code read on 2026-08-29, not from
> prior documentation.** Where `infra/gcp/07_deploy_gateway.sh` disagrees, §11 records
> which of its statements the code contradicts — three of its five "KNOWN GAPS" are stale.
>
> **Citation form:** `gateway_deploy_spec §4.3`. ⚠ marks something established by reading
> code that contradicts a reasonable assumption.

---

## §1 · What this service is

The single ingress for HubSpot webhooks. It carries a **trigger id only** — never lead
data (`server.py` module docstring; `requirements.txt` refuses a `lqabr_core` dependency
specifically so it *cannot* hold a `LeadProfile`). Order of operations is
`ingress → router → audit → dispatch` and nothing else lives here.

### §1.1 · Five ways it breaks the summary/research pattern

| # | The other agents | The gateway | Why |
| --- | --- | --- | --- |
| 1 | build context is the agent folder | ⚠ **build context is the REPO ROOT** | `Dockerfile` does `COPY agents/gateway /app/agents/gateway` |
| 2 | `--ingress internal --no-allow-unauthenticated` | ⚠ **`--ingress all --allow-unauthenticated`** | HubSpot cannot present a Google ID token. §4.2 |
| 3 | one deploy pass | ⚠ **two passes** | `LQABR_GATEWAY_PUBLIC_URL` must equal the service's own URL, which does not exist until pass 1 finishes. §4.4 |
| 4 | `--max-instances 3` | ⚠ **`--max-instances 1`** | the dedupe store is in-memory and per-process. §4.5 |
| 5 | behaviour is env-driven | ⚠ **behaviour is baked into the image** | `config.yaml` is COPYed and `load_config()` accepts no env path. §5 |

---

## §2 · Identity & registry

| Key | Value | Source |
| --- | --- | --- |
| `PROJECT_ID` | `ldqfingsrv-dev` | |
| `PROJECT_NUMBER` | `432617526728` | |
| `REGION` | `us-central1` | |
| `SERVICE_NAME` | `lqabr-dev-gtwy` | the live service; ⚠ `07_deploy_gateway.sh` still says `lqabr-agent-gateway` |
| `IMAGE` | `us-central1-docker.pkg.dev/ldqfingsrv-dev/lqabr/lqabr-dev-gtwy` | |
| `IMAGE_TAG` | `agents/gateway/VERSION` → `0.1.0` | |
| `RUNTIME_SA` | `lqabr-agent-dev@ldqfingsrv-dev.iam.gserviceaccount.com` | live service |
| `BUILD_SA` | `projects/ldqfingsrv-dev/serviceAccounts/lqabr-build@…` | |
| `APP_SECRET` | secret `lqabr-hubspot-app-secret` | ⚠ the private app's **client secret**, NOT `lqabr-hubspot-access-token` |
| `INGRESS_PATH` | `/hubspot/events` | `config.yaml: gateway.ingress.path` |

---

## §3 · Build & push

⚠ **Context is the repo root.** `agents/gateway/Dockerfile` copies
`agents/gateway/requirements.txt` and then `agents/gateway`, both repo-root-relative.
Submitting `agents/gateway` as the context fails to find its own files.

```bash
gcloud builds submit .                                  \
  --project ldqfingsrv-dev                              \
  --config agents/gateway/infra/cloudbuild.yaml         \
  --substitutions "_IMAGE=${IMAGE},_TAG=${IMAGE_TAG}"   \
  --service-account "${BUILD_SA}"
```

`--service-account` is mandatory: without it Cloud Build uses the default compute SA,
which holds no roles here.

**What the image contains** (read from the Dockerfile): python 3.12-slim; non-root user
`agent` (uid 1001); `PYTHONPATH=/app/agents/gateway/lib:/app/agents/gateway/src`;
`ENTRYPOINT docker-entrypoint.sh`; `PORT=8080`; `AGENTGATEWAY_ENABLED=1` **as an image
default — override it at deploy (§4.7)**. The agentgateway install step is `|| echo WARN`,
so a failed download does not fail the build; the entrypoint degrades to direct dispatch.

---

## §4 · Deploy

Two passes. Pass 1 creates the service; pass 2 sets the URLs that could not be known
before it existed.

### §4.1 · Pass 1

```bash
gcloud run deploy lqabr-dev-gtwy \
  --image "${IMAGE}:${IMAGE_TAG}" \
  --project ldqfingsrv-dev --region us-central1 \
  --service-account "${RUNTIME_SA}" \
  --execution-environment gen2 \
  --network lqabr-vpc --subnet lqabr-run-uscentral1 --vpc-egress all-traffic \
  --ingress all --allow-unauthenticated \
  --port 8080 --cpu 1 --memory 512Mi --timeout 60 \
  --min-instances 1 --max-instances 1 --concurrency 10 \
  --set-secrets "HUBSPOT_APP_SECRET=lqabr-hubspot-app-secret:latest" \
  --set-env-vars "AGENTGATEWAY_ENABLED=0"
```

### §4.2 · ⚠ Why public, and what actually defends it

`--allow-unauthenticated` is **required, not a shortcut**: HubSpot cannot mint a Google ID
token. The only thing standing between the open internet and the authority to start
outreach is the HubSpot **v3 HMAC signature**, verified in
`lib/soloai/protocols/http.py:67` against `HUBSPOT_APP_SECRET`.

`verify_v3_signature` hashes `method + uri + body + timestamp` and enforces a
`max_age_seconds` replay window (300s). It raises if the secret is unset — it does **not**
fail open.

⚠ **But it only runs when `gateway.ingress.signature.enabled` is true, and the shipped
`config.yaml` sets it to `false`** with the comment *"LOCAL DEV (session 2026-08-21)"*.
Deploying that image leaves the ingress completely unauthenticated. See §5.

### §4.3 · Network — required, and currently missing in production

⚠ The live service has **no VPC connector at all**. That is a live defect, because
`lqabr-dev-research` is `ingress=internal`: the `R-blog-summary` route
(`agents_registry.yaml`) dispatches to `LQABR_RESEARCH_AGENT_URL`, and an off-VPC caller
gets a 404 from Google's front end. `--network/--subnet/--vpc-egress all-traffic` is
therefore mandatory for the gateway's own routing to work, even though its *ingress* is
public. Ingress and egress are independent axes.

The gateway authenticates to agents by minting an ID token from the metadata server —
`lib/soloai/id_token.py`, attached at `lib/soloai/protocols/a2a.py:229` via
`**auth_header(endpoint)`. Only `https://*.run.app` targets get one; loopback gets none.
The runtime SA therefore needs `roles/run.invoker` on each callee.

### §4.4 · Pass 2 — the URLs

`LQABR_GATEWAY_PUBLIC_URL` must equal the URL HubSpot calls **character for character**,
because the signature is computed over the full request URI and Cloud Run rewrites `Host`.
Any drift is a silent, permanent 401 — never an obvious error.

Agent endpoint variable names are **not** free choices; they come from `endpoint_env` in
`config/agents_registry.yaml`, and `router.py:337` reads exactly those names:

| Agent key | `endpoint_env` | Callee | Callee ingress |
| --- | --- | --- | --- |
| `email` | `LQABR_EMAIL_AGENT_URL` | `lqabr-dev-email-agent` | all |
| `voice` | `LQABR_TEXT_VOICE_AGENT_URL` | `lqabr-dev-txtv` | all |
| `research` | `LQABR_RESEARCH_AGENT_URL` | `lqabr-dev-research` | **internal** → needs §4.3 |

⚠ There is **no `scheduling` agent in the registry.** `07_deploy_gateway.sh` wires
`LQABR_SCHEDULING_AGENT_URL`; nothing reads it.

⚠ The live service has **none of these three set**, so `registry.health()`
(`router.py:348`) reports every agent not ready, `/readyz` returns 503, and every matching
event is audited as a routing error rather than dispatched.

```bash
gcloud run services update lqabr-dev-gtwy --region us-central1 --project ldqfingsrv-dev \
  --update-env-vars "LQABR_GATEWAY_PUBLIC_URL=${URL},LQABR_EMAIL_AGENT_URL=…,LQABR_TEXT_VOICE_AGENT_URL=…,LQABR_RESEARCH_AGENT_URL=…"
```

### §4.5 · ⚠ `--max-instances 1` is a correctness constraint

`DedupeStore` (`router.py:363`) is an in-memory TTL+LRU set. Its own docstring: *"In-memory
and therefore per-instance: two Cloud Run instances do not share it."* The code states the
mitigation — trigger ids are deterministic and agents are idempotent on trigger id — so
this is a bounded risk, not a guaranteed duplicate. But **the live service runs
`maxScale=20`**, which is 20 independent dedupe stores.

`--min-instances 1` is the paired setting: a cold start can exceed HubSpot's webhook
timeout, producing a retry, and a retry against a *different* instance is exactly the case
the store cannot cover.

### §4.6 · `--concurrency 10`

Matches `gateway.ingress.max_concurrent_requests` in `config.yaml`, which also drives an
`asyncio.Semaphore` (`server.py`) **and** a `ConcurrencyGuard`. These are independent
throttles; keeping Cloud Run equal to them avoids an effective limit nobody chose.

### §4.7 · `AGENTGATEWAY_ENABLED=0`

The Dockerfile sets `1`. Override to `0`: `config/agentgateway.yaml` routes use
`${LQABR_*_AGENT_URL}` as their own backends — the same variables `dispatch.py` posts to —
so an enabled sidecar either forwards to itself or is bypassed. `docker-entrypoint.sh`
degrades cleanly either way; leaving it on only produces a misleading startup log.

---

## §5 · ⚠ The image is the configuration

`create_app()` calls `load_config()` with no argument (`server.py:100`), which resolves to
`agents/gateway/config/config.yaml` (`lib/soloai/config.py: DEFAULT_CONFIG_DIR`). **There
is no env var to point at a different file.** `config.yaml` is COPYed into the image, so
every value in it is fixed at build time.

`_expand_env` (`config.py:32`) does resolve `${VAR}` / `${VAR:-default}` inside string
leaves — but it returns **strings**, and:

⚠ **Every boolean in the config is read with `bool(...)`**, at `server.py:122,129,139,150,151`,
`audit_hooks.py:343`, `mcp.py:71`. `bool("false") is True`. **A boolean parameterised
through the environment is permanently ON**, whatever the value says. Ints (`int(...)`)
and strings (`str(...)`) are safe.

**Consequences for two shipped values:**

| Key | Shipped | Effect if deployed as-is | Correct fix |
| --- | --- | --- | --- |
| `gateway.ingress.signature.enabled` | `false` | ⚠ the public ingress accepts **unsigned** webhooks — anyone with the URL can start outreach | edit to `true` and rebuild. It cannot be an env flag (bool bug) — and a security control should not be env-flippable |
| `audit.sink` | `file` (`./logs/gateway/gateway.jsonl`, relative to CWD = `/app/agents/gateway/src`) | ⚠ the entire audit stream goes to a container-local file and **never reaches Cloud Logging** | `${LQABR_AUDIT_SINK:-stdout}` — safe, it is read with `str()` |

Both are applied to `config/config.yaml` as a **precondition of this spec**; a build from
an unmodified working tree is not deployable.

---

## §6 · Environment surface

Every variable the code reads (`grep os.environ` over `src/` and `lib/`):

| Variable | Read at | Required | Notes |
| --- | --- | --- | --- |
| `HUBSPOT_APP_SECRET` | `server.py`, `http.py` | **yes** | client secret; unset ⇒ every webhook 401s |
| `LQABR_GATEWAY_PUBLIC_URL` | `server.py` | **yes** | must equal HubSpot's Target URL exactly |
| `LQABR_EMAIL_AGENT_URL` | `router.py:337` | yes | via `endpoint_env` |
| `LQABR_TEXT_VOICE_AGENT_URL` | `router.py:337` | yes | |
| `LQABR_RESEARCH_AGENT_URL` | `router.py:337` | yes | callee is internal — §4.3 |
| `HUBSPOT_PRIVATE_APP_TOKEN` | `server.py:130` | only if `audience.enabled` | shipped `false`; without it the gateway falls back to a single hand-off carrying the ticket id |
| `LQABR_VOICE_REPORT_URL` | `server.py` | only if `vapi.report.enabled` | shipped `false` |
| `LQABR_VAPI_WEBHOOK_SECRET` | `server.py` | only if `vapi.report.enabled` | the **gateway** owns Vapi authenticity; txtv verifies nothing |
| `LQABR_DISPATCH_MODE` | `dispatch.py:347` | no | `per_lead` \| `grouped`; **env wins over config** |
| `LQABR_BATCH_SIZE` | `dispatch.py:349` | no | env wins over config |
| `LQABR_LOG_PAYLOADS` | audit | no | |
| `AGENTGATEWAY_ENABLED` | entrypoint | no | set `0` (§4.7) |
| `PORT` | entrypoint | no | Cloud Run injects it |

⚠ `LQABR_AUDIT_SINK` / `LQABR_AUDIT_FILE` appear in `config/.env.example` but **nothing
reads them** — `config.yaml` contains no `${…}` reference. They become live only after the
§5 edit.

---

## §7 · Routes served

| Route | Method | Purpose |
| --- | --- | --- |
| `/hubspot/events` | POST | the ingress. 200 terminal · 401 signature · 400 malformed · 413 batch >100 · **503 matched but not handed off** |
| `/healthz` | GET | liveness only |
| `/readyz` | GET | 200/503; checks every agent endpoint resolves **and** config problems |
| `/metrics` | GET | handoff counters, ingress snapshot, dedupe size |
| `/` | GET | identity + the live route table |
| `/call-report` | POST | Vapi relay; disabled in shipped config |

⚠ **503 on the ingress is load-bearing**: those event ids are deliberately *not* recorded
in the dedupe store, so HubSpot's redelivery is a real second attempt. That is how "a lead
is never silently dropped" holds at the HTTP layer.

---

## §8 · Negative constraints

| # | Forbidden | Consequence |
| --- | --- | --- |
| F1 | `--ingress internal` or `--no-allow-unauthenticated` | HubSpot cannot call it at all |
| F2 | Deploying with `signature.enabled: false` | an open door to outreach (§5) |
| F3 | `--max-instances > 1` | N independent dedupe stores (§4.5) |
| F4 | Omitting `--network/--subnet/--vpc-egress` | cannot reach the internal research agent (§4.3) |
| F5 | Submitting `agents/gateway` as the build context | build cannot find its own files (§3) |
| F6 | Using `lqabr-hubspot-access-token` as `HUBSPOT_APP_SECRET` | every webhook 401s; they are different credentials |
| F7 | `LQABR_GATEWAY_PUBLIC_URL` ≠ HubSpot's Target URL | silent permanent 401 |
| F8 | Parameterising a config **boolean** through env | permanently ON, whatever the value (§5) |
| F9 | Setting `LQABR_SCHEDULING_AGENT_URL` | no such agent in the registry; nothing reads it |
| F10 | Omitting `--service-account` on build or deploy | default compute SA holds no roles |

---

## §9 · Structural requirements

`bash`, `set -euo pipefail`, `source "$(dirname "$0")/config.sh"`, every value
`${VAR:-default}`, no literals in the `.sh`, idempotent by service name, numbered files
under `agents/gateway/infra/`, mode `+x`.

---

## §10 · Verification

Specified separately in **`docs/gateway_verify_spec.md`**, executable as
`bash infra/04_verify.sh`.

---

## §11 · Statements in `infra/gcp/07_deploy_gateway.sh` the code contradicts

| Its claim | Code says |
| --- | --- |
| KNOWN GAP 3 — *"No identity token is attached … A2AClient uses plain requests with no Authorization header"* | **False now.** `a2a.py:229` splats `**auth_header(endpoint)`; `id_token.py` mints from the metadata server |
| Service is `lqabr-agent-gateway` | live service is `lqabr-dev-gtwy` |
| Wires `LQABR_SCHEDULING_AGENT_URL` to `lqabr-scheduling-agent` | no `scheduling` agent in `agents_registry.yaml`; no such service deployed |
| Resolves `lqabr-email-agent` / `lqabr-text-voice-agent` | live names are `lqabr-dev-email-agent` / `lqabr-dev-txtv` |
| No VPC flags at all | required — the research callee is `ingress=internal` (§4.3) |

Its notes on public ingress, the two-pass URL, and `max-instances=1` are **correct** and
are carried forward here.

---

## §12 · Open items

- ⚠ **`bool()` on config values makes every boolean env-unsettable** (§5). Fix is a
  `_as_bool` helper in `soloai/config.py` treating `"false"/"0"/"no"/""` as false.
- **No env override for the config path.** A `LQABR_GATEWAY_CONFIG` read by `load_config()`
  would let one image serve dev and prod.
- **Dedupe is per-instance**, capping the service at one instance. A shared store (Redis,
  Firestore) removes the cap.
- **`config/.env.example` is stale** — documents `LQABR_AUDIT_SINK`/`LQABR_AUDIT_FILE` and
  a `sink: ${LQABR_AUDIT_SINK:-stdout}` line that `config.yaml` does not contain.
