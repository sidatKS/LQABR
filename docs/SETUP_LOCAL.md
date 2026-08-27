# LQABR Local MVP — Setup Guide

**As of:** 2026-08-23 · **Scope:** everything built and proven through the
Research Agent (T0–T10). Email / Voice legs (T12–T14) are not yet stood up.

The whole stack runs on ONE laptop (WSL2 Ubuntu). HubSpot is the only public
SaaS; its webhooks reach the laptop through a single static ngrok domain.
Every gateway→agent hop is loopback.

```
                        (public)                       (loopback, this laptop)
HubSpot ── webhook ──► ngrok ──► gateway :8080 ──┬──► research :8086 ─┐
   ▲                armed-equal-share            ├──► email    :8083  │ (not built yet)
   │                .ngrok-free.dev              └──► voice    :8084  │ (not built yet)
   │                                                                  │
   └────────────── HubSpot MCP container :8091 ◄──── all agents ◄─────┘
                   (the ONLY door to HubSpot)          summary :8082
```

---

## 1. Prerequisites (once)

| Requirement | Verified version | Notes |
| --- | --- | --- |
| WSL2 Ubuntu | 22.04+ | ALL commands below are bash **in WSL**, repo at `/mnt/c/Users/SwaroopKumar/Documents/Claude/Projects/LQABR` |
| Docker (in WSL) | 29.x | runs the HubSpot MCP container |
| Python | 3.12 | one shared venv for every agent |
| ngrok | ≥ 3.20 | `apt` install (the account rejects older with `ERR_NGROK_121`) |
| gcloud CLI | any recent | ADC login for the MCP's Secret Manager access |

### 1a. The shared venv

One venv covers all agents; `google-adk` is unpinned (resolved 2.7.1).

```bash
cd /mnt/c/Users/SwaroopKumar/Documents/Claude/Projects/LQABR
bash scripts/dev/setup_venv.sh          # creates .venv, installs scripts/dev/requirements-shared.txt + lqabr_core -e
source .venv/bin/activate
pip install "anthropic>=0.40"           # research agent's model + web-search SDK
```

### 1b. ngrok install (WSL)

```bash
curl -sSL https://ngrok-agent.s3.amazonaws.com/ngrok.asc | sudo tee /etc/apt/trusted.gpg.d/ngrok.asc >/dev/null \
  && echo "deb https://ngrok-agent.s3.amazonaws.com buster main" | sudo tee /etc/apt/sources.list.d/ngrok.list \
  && sudo apt update && sudo apt install -y ngrok
```

---

## 2. Secrets (once, all git-ignored)

| File | Holds | Note |
| --- | --- | --- |
| `mcp/.env` | `LQABR_HUBSPOT_ACCESS_TOKEN` (private-app token) | used by helper scripts for direct HubSpot checks |
| `agents/summary/.env` | `ANTHROPIC_API_KEY` + summary knobs | copy from `.env.example` |
| `agents/research/.env` | `ANTHROPIC_API_KEY` + research knobs | copy from `.env.example` |
| `agents/gateway/config/.env` | `LQABR_GATEWAY_PUBLIC_URL`, `HUBSPOT_PRIVATE_APP_TOKEN`, the three agent URLs | see §5 |
| `ngrok/.env` | `NGROK_AUTHTOKEN` | must be the account that OWNS the static domain |

**ngrok account trap (cost a full debug round):** `armed-equal-share.ngrok-free.dev`
is reserved on ONE specific account. Any other account's authtoken fails with
`ERR_NGROK_320: This domain is reserved for another account.` Record the owning
account beside the domain.

**GCP ADC for the MCP:** the container reads the HubSpot token from Secret
Manager (`ldqfingsrv-dev / lqabr-hubspot-access-token`) using your Application
Default Credentials. `gcloud auth application-default login` once — and note it
**expires roughly hourly** (see §8).

---

## 3. Config maps (all committed; env vars override each)

| Component | Config map | Highlights |
| --- | --- | --- |
| Summary agent | `agents/summary/config/config.yaml` | MCP timeout 60s, retries/backoff, `logging.file` |
| Research agent | `agents/research/config/config.yaml` | model, MCP tool names, search knobs, `logging.file` |
| MCP container run | `mcp/mcp.config` | image, port 8091, log file, secret project/name |
| ngrok | `ngrok/ngrok.config` | domain, port 8080, log file |
| Gateway | `agents/gateway/config/config.yaml` + `agents_registry.yaml` | routes, audience resolution, audit sink → file |

Precedence everywhere: **environment > config map > code default.**

---

## 4. Start order (every session)

```bash
cd /mnt/c/Users/SwaroopKumar/Documents/Claude/Projects/LQABR
source .venv/bin/activate

# 1) HubSpot MCP container  → :8091  (log: logs/mcp/hubspot.log)
bash mcp/run.sh

# 2) ngrok static tunnel    → :8080  (log: logs/ngrok/ngrok.log)
bash ngrok/run.sh

# 3) Gateway                → :8080  (logs: logs/gateway/gateway.jsonl + uvicorn.log)
nohup uvicorn server:app --app-dir agents/gateway/src --host 127.0.0.1 --port 8080 > logs/gateway/uvicorn.log 2>&1 &

# 4) Summary agent          → :8082  (log: logs/agents/summary/agent.log)
( cd agents/summary && nohup uvicorn service_app:app --port 8082 --app-dir src >/dev/null 2>&1 & )

# 5) Research agent         → :8086  (log: logs/agents/research/agent.log)
nohup uvicorn service_app:app --port 8086 --app-dir agents/research/src >/dev/null 2>&1 &
```

Agents take ~10–12 s to boot (SDK import + MCP tool discovery) — don't trust a
health check sooner. Agents are **processes, not containers**: `docker ps`
shows only `lqabr-mcp-local`; check agents with `curl /health` or `pgrep`.

### Port map

| Port | Service | Public? |
| --- | --- | --- |
| 8080 | Agent Gateway | via ngrok only |
| 8082 | Summary agent | no |
| 8083 | Email agent *(future)* | no |
| 8084 | Text/Voice agent *(future)* | no |
| 8086 | Research agent | no |
| 8091 | HubSpot MCP container | no |
| 4040 | ngrok inspector | local |

---

## 5. Gateway wiring

`agents/gateway/config/.env` (values in use):

```
LQABR_GATEWAY_PUBLIC_URL=https://armed-equal-share.ngrok-free.dev
HUBSPOT_PRIVATE_APP_TOKEN=<private-app token>        # audience resolution reads HubSpot
LQABR_EMAIL_AGENT_URL=http://127.0.0.1:8083/hubspot/campaign
LQABR_TEXT_VOICE_AGENT_URL=http://127.0.0.1:8084/voice_agent/lead
LQABR_RESEARCH_AGENT_URL=http://127.0.0.1:8086/research/a2a
PORT=8080
```

`LQABR_GATEWAY_PUBLIC_URL` must equal HubSpot's webhook Target URL **exactly**
or the v3 HMAC differs and every delivery 401s. Local-dev deviations set in
`config.yaml`: `ingress.signature.enabled: false` (no app secret yet — gap K4),
`vapi.report.enabled: false`, `audit.sink: file`.

**Session change (2026-08-22):** the gateway's audience resolver now reads
`blog_published_at` off the ticket (same GET as `blog_industry`) and passes it
in dispatch metadata — the MCP reads the blog store by that timestamp, not by
ticket id. Files: `audience.py`, `router.py`, `dispatch.py`, `lib/soloai/protocols/a2a.py`.

---

## 6. Verify (the health sweep)

```bash
for p in 8080:gateway 8082:summary 8086:research; do
  port=${p%%:*}; name=${p##*:}
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 localhost:$port/healthz)
  [ "$code" = "000" ] && code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 localhost:$port/health)
  printf '%-8s :%s  %s\n' "$name" "$port" "$([ "$code" = 200 ] && echo UP || echo "DOWN ($code)")"
done
docker ps --filter name=lqabr-mcp-local --format 'mcp      :8091  {{.Status}}'
curl -s https://armed-equal-share.ngrok-free.dev/healthz >/dev/null && echo "tunnel   OK" || echo "tunnel   DOWN"
```

Note: a bare GET on `:8091/mcp` returns **406** — that is FastMCP alive and
demanding the SSE Accept header, not a failure.

Deep checks: `curl -s localhost:8082/health` and `:8086/health` both report
`mcp.reachable: true` and the bound tool names; the gateway startup line in
`logs/gateway/gateway.jsonl` shows all three agents `ready`.

---

## 7. Functional smoke tests (proven working)

**Summary → HubSpot ticket** (chain link 1):

```bash
curl -sX POST localhost:8082/summary/run -H 'Content-Type: application/json' \
  -d '{"source":"https://spring.io/blog","hubspot":{"subject":"<title>","blog_published_at":"2026-08-27T09:30:00Z","industry":"HEALTHCARE"}}'
```

`blog_industry` must be one of `FINANCIAL_SERVICES | HEALTHCARE | LEGAL_SERVICES`
(HubSpot enum — free text 400s; gap D3). `blog_published_at` is normalized to
full ISO automatically. Success = `hubspot.status: "written"` + a ticket id.

**Research → lead_context** (chain link 2, agent side):

```bash
curl -sX POST localhost:8086/research/run -H 'Content-Type: application/json' \
  -d '{"object_id":"<contact id>","blog_published_at":"2026-08-27T09:30:00Z","company":"<Company Name>"}'
```

The `company` field is a **TEST-ONLY override** for gap B1 (the MCP's
`get_lead_profile` returns `company_id` but not the company name — the name
lives on the associated Company object, which the MCP already reads for
`industry`). Remove the override once the MCP image returns `company`. The
gateway never sends it. Dry-run first with `LQABR_RESEARCH_DRY_RUN=1`.

---

## 8. Operations

| Ritual | Command | When |
| --- | --- | --- |
| **ADC reauth** | `bash mcp/reauth.sh` | ~hourly, or when MCP writes return `status: halted` / `AuthConfigError`. Re-login alone is NOT enough — the `:ro` adc.json bind-mount pins the old file, so the script recreates the container. |
| Restart an agent | `fuser -k <port>/tcp; sleep 2; <start cmd>` | after any `.env` / config-map change (read once at boot) |
| Tests (research) | `python3 -m pytest agents/research/tests -c agents/research/tests/pytest.ini -q` | 47 tests, offline |
| Read a run | `grep run_complete logs/agents/research/agent.log \| tail -1` | every component logs natively under `logs/` |

### Logs (all native, no shell redirects)

```
logs/agents/summary/agent.log     logs/agents/research/agent.log
logs/gateway/gateway.jsonl        logs/gateway/uvicorn.log
logs/mcp/hubspot.log              logs/ngrok/ngrok.log
```

---

## 9. HubSpot objects & properties in play

| Object | Properties written | By |
| --- | --- | --- |
| Ticket | `subject`, `blog_summary`, `blog_published_at`, `blog_industry` (enum) | Summary agent via `upsert_blog_summary` (upsert KEYED on `blog_published_at`) |
| Contact | `lead_context` | Research agent via `upsert_lead_profile` (requires `employee_id` + `company_id` + `decision_maker_flag`) |
| Contact | 9-pointer profile (`employee_id`, `email_id`, `jobtitle`, `decision_maker`, …) | lead_profile agent |
| Company | `name`, `industry` — the audience fan-out matches on **Company.industry**, and contact-level `company`/`industry` are typically null | lead_profile agent |

MCP tools on `:8091`: `get_lead_profile(object_id)` · `get_blog_summary(blog_published_at)` ·
`upsert_lead_profile(...)` · `upsert_blog_summary(subject, blog_summary, blog_published_at, blog_industry)`.

---

## 10. What remains

Next tasks: **T7** HubSpot webhook target → `https://armed-equal-share.ngrok-free.dev/hubspot/events` (re-save twice; the field silently reverts) · **T12** Email agent `:8083` + Mailgun · **T13** Mailgun events poll → `OPENED` · **T14** text_voice `:8084` + Vapi · **T15** full E2E · **T16** one-command start script.

Open gaps, bugs and owners: **`docs/LOCAL_MVP_GAPS.md`** (B1 MCP company name,
D2/D3 data rules, K1–K7). Research agent internals: `agents/research/docs/`.
