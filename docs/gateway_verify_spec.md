# LQABR Agent Gateway — verify spec

> **§0 · DOCUMENT CONTROL**
>
> | Field | Value |
> | --- | --- |
> | **Document id** | `gateway_verify_spec` |
> | **Version** | `1.0` |
> | **As of** | 2026-08-29 |
> | **Component** | `gateway` → service `lqabr-dev-gtwy` |
> | **Companion** | `docs/gateway_deploy_spec.md` |
> | **Executable form** | `agents/gateway/infra/04_verify.sh` |
>
> **Precedence:** the code wins; then this document; then any script. Derived from a read
> of `agents/gateway/**` on 2026-08-29. **Citation form:** `gateway_verify_spec §5.5`.
>
> **One run, no iteration.** Every check prints what it asserts, and on failure what was
> seen and what to do. §7 maps every id to cause and fix.

---

## §1 · What makes this component different to verify

Two things, both consequences of it being the only public service in the fleet.

**Layer C needs no in-VPC job.** `lqabr-dev-gtwy` is `--allow-unauthenticated` and
`ingress=all`, so every route is reachable from a laptop with plain `curl`. Summary and the
MCP required a Cloud Run job; this does not.

⚠ **That also means `/readyz`, `/metrics` and `/` are publicly readable.** They disclose the
route table, the agent-readiness map and handoff counters to anyone with the URL. Noted in
§9 as an open item, not a check.

**The most important assertion is a negative one.** The gateway's whole security boundary
is one config flag. §5.5 asserts that an *unsigned* POST is rejected — because if it is
accepted, everything else passing is irrelevant.

---

## §2 · Context manifest

| Path | What it settles |
| --- | --- |
| `agents/gateway/src/server.py` | routes, `_config_problems()`, `_public_uri()`, status codes |
| `agents/gateway/lib/soloai/protocols/http.py` | `compute_v3_signature`, the replay window |
| `agents/gateway/src/router.py` | `AgentRegistry.health()`, `DedupeStore`, `endpoint_for` |
| `agents/gateway/config/agents_registry.yaml` | the routes and `endpoint_env` names |
| `agents/gateway/config/config.yaml` | ⚠ baked into the image — the thing most likely to be wrong |
| `docs/gateway_deploy_spec.md` | the values being verified |

---

## §3 · Running it

```bash
cd agents/gateway
bash infra/04_verify.sh
```

Layer C reads `lqabr-hubspot-app-secret` from Secret Manager to construct a valid
signature. **No check triggers outreach**: the only signed request sent carries an event
that matches no route (§5.7).

---

## §4 · Layers

| Layer | Question | Where |
| --- | --- | --- |
| **A** | what Cloud Run says the service *is* | host, `gcloud` |
| **B** | what it said as it booted | host, Cloud Logging |
| **C** | what it actually *does* | host, `curl` — the service is public |

---

## §5 · The flow, and the input set at every hop

### §5.1 · Hop 1 — HubSpot → `POST /hubspot/events`

Body is a JSON array of up to 100 events (a single object is normalised to a list,
`http.py: parse_batch`). Headers that matter:

| Header | Purpose |
| --- | --- |
| `X-HubSpot-Signature-v3` | `base64(HMAC-SHA256(method + uri + body + timestamp))` |
| `X-HubSpot-Request-Timestamp` | epoch **milliseconds**; replay window 300s |

⚠ `uri` is the **full** request URI as HubSpot built it. Cloud Run rewrites `Host`, so
`server.py:_public_uri()` substitutes `LQABR_GATEWAY_PUBLIC_URL`. If that value and
HubSpot's Target URL differ by even a trailing slash, every delivery 401s silently.

### §5.2 · Hop 2 — signature

`verify_v3_signature` (`http.py:67`) raises when: the secret is unset; the header is
missing; the timestamp is not epoch-ms; `age > 300s`; `age < -MAX_CLOCK_SKEW`; or the digest
mismatches. ⚠ It uses a **tighter future-skew allowance than the past window on purpose** —
`abs()` would silently double the replay window.

Gated by `gateway.ingress.signature.enabled`. **If that is false the entire hop is
skipped** and any unsigned body is accepted.

### §5.3 · Hop 3 — route

`Router.route_batch` evaluates `agents_registry.yaml` routes top to bottom, first match
wins:

| Route | subscriptionType | property | match | → agent |
| --- | --- | --- | --- | --- |
| `R2-lead-context` | `contact.propertyChange` | `lead_context` | `non_empty` | `email` |
| `R3-email-opened` | `contact.propertyChange` | `email_status` | `exact_ci` on `OPENED` | `voice` |
| `R-blog-summary` | `ticket.propertyChange` | `blog_summary` | `non_empty` | `research` |

Anything else is **discarded** — a subscription fires on every value of a watched property,
so value filtering lives here. `R1-contact-created` is commented out (disabled 2026-08-25).

### §5.4 · Hop 4 — dedupe, and why it is asymmetric

`DedupeStore` (`router.py:363`) records **only dispatched** events, and reserves the id
*before* the hand-off. Failures and discards are deliberately not recorded, so HubSpot's
redelivery is a real second attempt. ⚠ In-memory and per-instance — see
`gateway_deploy_spec §4.5`.

### §5.5 · Hop 5 — dispatch

`Dispatcher` → `A2AClient` POSTs to the URL in the agent's `endpoint_env`
(`router.py:337`), with `**auth_header(endpoint)` attaching a metadata-server ID token for
any `https://*.run.app` target (`a2a.py:229`, `id_token.py`). Retries:
`1 + max_retries` attempts, exponential backoff 0.5s.

### §5.6 · Hop 6 — the response to HubSpot

| Code | Meaning |
| --- | --- |
| 200 | every event reached a terminal outcome — dispatched, or deliberately discarded |
| 401 | signature missing / stale / wrong |
| 400 | malformed envelope · 413 batch > 100 |
| **503** | matched a route but could not be handed off. ⚠ Those ids are **not** recorded in the dedupe store, so the redelivery is a real retry. This is how "a lead is never silently dropped" holds at the HTTP layer |

### §5.7 · The probe used by C7

A validly signed batch carrying a **deliberately unroutable** event:

```json
[{"eventId": <unique>, "subscriptionType": "contact.propertyChange",
  "propertyName": "lqabr_verify_probe", "propertyValue": "verify",
  "objectId": 0, "attemptNumber": 0}]
```

It exercises signature acceptance and the full routing evaluation, and terminates in a
discard — so **no agent is dispatched and no lead is contacted**. Discards are not recorded
in the dedupe store, so the probe is repeatable.

---

## §6 · Assertions

### §6.1 · Layer A — control plane

| id | Asserts | Fails when |
| --- | --- | --- |
| A1 | the service exists | never deployed |
| A2 | latest revision `Ready=True` | container never became healthy |
| A3 | one revision serves 100% | a split left an old revision live |
| A4 | `ingress=all` | ⚠ internal would block HubSpot entirely |
| A5 | IAM allows `allUsers` to invoke | HubSpot cannot present an ID token; without this every delivery 403s |
| A6 | SA is `lqabr-agent-dev` | fell back to the default compute SA |
| A7 | on the VPC with `all-traffic` egress | cannot reach the `ingress=internal` research agent |
| A8 | `maxScale == 1` **and** `minScale >= 1` | N dedupe stores; cold-start retries |
| A9 | `HUBSPOT_APP_SECRET` bound from `lqabr-hubspot-app-secret` | the wrong credential 401s everything |
| A9b | the bound secret **has a version** | ⚠ a binding to a versionless secret injects nothing; the container sees the var unset |
| A10 | `LQABR_GATEWAY_PUBLIC_URL` equals the service's own URL | silent permanent 401 |
| A11 | all three `LQABR_*_AGENT_URL` set | matching events audited as routing errors |

### §6.2 · Layer B — startup

Read `jsonPayload.event` on the serving revision, filtered by revision name.
⚠ **Not `textPayload`** — the audit stream is structured JSON.

| id | Asserts |
| --- | --- |
| `B-startup` | a startup record was emitted (`record_startup`) |
| `B-no_config_error` | **no** config-error record — `_config_problems()` fails *closed*: the service looks healthy and 401s forever |

### §6.3 · Layer C — data plane

| id | Check | Pass criterion |
| --- | --- | --- |
| C1 | `GET /` | the three live routes match `agents_registry.yaml` |
| C2 | `GET /healthz` | `status: ok` |
| C3 | `GET /readyz` | **200**, every agent `ready: true`, no `config` block |
| C4 | `GET /metrics` | returns handoff / ingress / dedupe counters |
| **C5** | **unsigned** `POST /hubspot/events` | **401** — the door is closed |
| C6 | signed but **stale** (timestamp 10 min old) | **401** — replay window enforced |
| C7 | validly signed, unroutable event (§5.7) | **200** — signature accepted, routing ran, nothing dispatched |

⚠ **C5 is the single most important check in this document.** A 200 there means the public
ingress accepts forged triggers and can be made to start outreach by anyone with the URL.

⚠ **But C5 alone is not sufficient, because it can pass for the wrong reason.**
`verify_v3_signature` raises *"signature verification is on but HUBSPOT_APP_SECRET is
unset"* — which is also a 401. So a gateway with **no usable secret at all** passes C5 while
being unable to accept any real webhook either. Observed live on 2026-08-29 (§10). C5 must
therefore be read together with **A9** (the secret resolves) and **C7** (a *valid* signature
is accepted). C5 proves the door is shut; only C7 proves the key still works.

---

## §7 · Failure decode

| id | Cause | Fix |
| --- | --- | --- |
| A4 / A5 | deployed with internal ingress or without `--allow-unauthenticated` | HubSpot cannot call it; redeploy with `03_deploy_run.sh` |
| A7 | no VPC | the research route 404s; redeploy with the network flags |
| A8 | `maxScale > 1` | each instance has its own dedupe store; duplicate outreach on retry |
| A10 | mismatch, often a trailing slash | must equal HubSpot's Target URL character for character |
| A11 | agent service not deployed, or pass 2 skipped | re-run `03_deploy_run.sh` after the agents exist |
| `B-no_config_error` | `_config_problems()` found something | it fails closed — read the recorded message; it names the variable |
| C3 503 | an `endpoint_env` does not resolve, or a config problem | same as A11 / B |
| **C5 200** | ⚠ `signature.enabled` is false in the **image** | `config.yaml` is baked; fix it and **rebuild** — `02_build_push.sh` refuses to build otherwise |
| C6 200 | replay window not enforced | check `max_age_seconds` |
| C7 401 | secret mismatch, or `LQABR_GATEWAY_PUBLIC_URL` ≠ the URL used | A9 / A10 |
| C7 503 | the event matched a route and dispatch failed | the probe should be unroutable — a route was added that matches it |

---

## §8 · Negative constraints

| # | Forbidden | Consequence |
| --- | --- | --- |
| N1 | Sending a **routable** signed event as a check | contacts a real lead |
| N2 | Treating C5 as optional | it is the only proof the ingress is defended |
| N3 | `--freshness` instead of a revision filter | stale entries read as current |
| N4 | Reading `textPayload` for audit events | they are `jsonPayload`; every assertion silently false-negatives |
| N5 | Concluding health from `/healthz` | it is liveness only; `/readyz` is the one that checks endpoints |

---

## §9 · Out of scope / open items

- **A real dispatch is not verified.** C7 stops at a discard by design. Proving hop 5
  end-to-end means contacting a lead, which belongs in a staged test with a fake agent.
- ⚠ **`/readyz`, `/metrics` and `/` are public**, disclosing the route table, agent
  readiness and counters. Consider moving them behind a header check.
- **Dedupe is per-instance**, so A8 pins the service to one instance.
- **`config.yaml` is baked into the image**, so C5 can only be fixed by a rebuild.

---

## §10 · Live findings

### §10.1 · First run, 2026-08-29 — before the rebuild

Against revision `lqabr-dev-gtwy-00002-cqh` (deployed 2026-08-04). **The gateway could not
accept a single HubSpot webhook.**

| Check | Result |
| --- | --- |
| A2 | FAIL — `Ready=False`: *"secret_key_ref.name: Secret …/lqabr-hubspot-webhook-secret/versions/latest was not found"* |
| A9 | FAIL — the secret existed with **zero versions**, so the binding injected nothing |
| A7 / A8 / A11 | FAIL — no VPC · `maxScale=20` · all three agent URLs unset |
| C1 | FAIL — ⚠ the image served `R1-contact-created`, `R2-decision-maker`, `R3-email-opened` on `lqabr_email_status`, `R4-voice-completed` → `scheduling`. **It predated the `lqabr_*` property rename, the lead_context route, the blog-summary route, and the removal of the scheduling agent.** |
| C2 / C3 | FAIL |
| C5 | PASS — ⚠ but only because the secret was missing, not because a signature was checked |

### §10.2 · After `01_secrets` → `02_build_push` → `03_deploy_run` (WIRE_AGENTS=0)

Revision `lqabr-dev-gtwy-00008-lbx`, image `lqabr-dev-gtwy:0.1.0`.

**A1–A10 PASS · B PASS · C1, C4, C5, C6, C7 PASS.**

| Was | Now |
| --- | --- |
| A2 `Ready=False` | **PASS** — the secret version fixed it |
| A9 versionless | **PASS** — bound to `lqabr-hubspot-webhook-secret`, `has_version=1` |
| A7 no VPC | **PASS** — `lqabr-vpc/lqabr-run-uscentral1`, `all-traffic` |
| A8 `maxScale=20` | **PASS** — `maxScale=1 minScale=1` |
| C1 stale routes | **PASS** — `R2-lead-context`, `R3-email-opened` on `email_status`, `R-blog-summary` |

**C7 = 200** with `discards_by_reason: {"no_matching_route": 1}`. That is the proof the
client secret is the real webhook signing key: the gateway recomputed the same HMAC over
`POST + uri + body + timestamp` and accepted it, then discarded the event because it matched
no route. C5 and C6 now fail-closed for the *right* reason rather than for a missing key.

**A11 and C3 FAIL by design** — `WIRE_AGENTS=0` deploys the ingress with no agent endpoint,
so nothing can be dispatched while HubSpot triggers are being tested. Re-run
`03_deploy_run.sh` with no flag once the agents are ready.

### §10.3 · ⚠ UNRESOLVED — C2 `/healthz` returns 404

Reproduced on the freshly built image, so it is **not** image staleness:

- `@app.get("/healthz")` is registered at `server.py:230`, alongside `/readyz` (239) and
  `/metrics` (250). The only other definition, `server.py:526`, belongs to the
  failed-to-start fallback app, which is not in play — the real routes are serving.
- `/` → 200, `/readyz` → 503, `/metrics` → 200; **only `/healthz` 404s.**
- The 404 body is HTML and the response carries **no `server: Google Frontend` header**,
  which `/readyz` does — so it is answered by something other than the path that serves the
  app's other routes.
- Cloud Run's request log has no entry for it: **the container never received it.**

Liveness is still provable — `/` and `/metrics` both return 200 from the app itself — so
this does not block. It is recorded rather than explained, and the Dockerfile `HEALTHCHECK`
targets `/healthz`, so it is worth resolving before anything depends on that probe.
