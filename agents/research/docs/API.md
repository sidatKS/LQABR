# Research Agent — HTTP API

Base: `http://127.0.0.1:8086` (loopback; the agent is never public — only the
gateway is, through ngrok).

## `GET /health` · `GET /healthz`

What this instance is bound to. Use it to confirm the MCP is reachable and the
three tool names bound before trusting a run.

```json
{
  "status": "UP",
  "service": "lqabr-research-agent",
  "model": "claude-sonnet-4-6",
  "dry_run": false,
  "search": { "enabled": true, "max_uses": 5 },
  "mcp": {
    "url": "http://localhost:8091/mcp",
    "reachable": true,
    "tools": ["get_blog_summary", "get_lead_profile", "upsert_blog_summary", "upsert_lead_profile"],
    "read_lead": "get_lead_profile",
    "read_blog": "get_blog_summary",
    "write": "upsert_lead_profile"
  },
  "hubspot": { "context_property": "lead_context" }
}
```

## `GET /mcp/tools`

What the MCP exposes **right now**, and which configured names are absent.
`missing: []` is the healthy answer.

## `POST /research/run`

The domain entry point. **Synchronous** — the caller gets the outcome.

```bash
curl -sX POST localhost:8086/research/run -H 'Content-Type: application/json' \
  -d '{"objectId":"533963448020","blog_published_at":"2026-08-27T09:30:00Z"}'
```

Nested form is equivalent:

```json
{ "target": { "objectId": "533963448020",
              "blog_published_at": "2026-08-27T09:30:00Z" } }
```

Response:

```json
{
  "run_id": "res-1a2b3c4d5e6f",
  "status": "completed",
  "objectId": "533963448020",
  "lead": { "company": "Axiom Law", "industry": "HEALTHCARE", "...": "..." },
  "blog": { "blog_published_at": "2026-08-27T09:30:00Z", "...": "..." },
  "note": "Axiom Law's alternative-legal-services model puts it …",
  "sources": ["https://…", "https://…"],
  "hubspot": { "status": "written", "property_name": "lead_context", "chars": 1180 },
  "model": "claude-sonnet-4-6",
  "error": ""
}
```

`status` is `completed` only when the **write** succeeded. A run that
researched well but could not land the note is `failed` — with `note` still
populated, so the work is recoverable.

## `POST /research/a2a`

The gateway's `message/send` envelope. **Asynchronous** — it acknowledges
immediately and runs the pass in the background, because HubSpot's delivery
budget is far shorter than a research pass.

Ids are read from `params.metadata`:

```json
{
  "jsonrpc": "2.0", "id": "1", "method": "message/send",
  "params": {
    "message": { "role": "user", "parts": [{ "kind": "text", "text": "trg-…" }] },
    "metadata": {
      "objectId": "533963448020",
      "blog_published_at": "2026-08-27T09:30:00Z",
      "summary_objectId": "329444635358",
      "run_id": "run-…"
    }
  }
}
```

Reply:

```json
{ "jsonrpc": "2.0", "id": "1",
  "result": { "status": "accepted", "objectId": "533963448020", "run_id": "run-…" } }
```

A payload with no `objectId` is answered `{"status": "rejected"}` — never a
500, because a malformed hand-off is a routing problem, not a server fault.

## Failure reasons

`error` always names the step:

| Prefix | Meaning |
| --- | --- |
| `bad-data:` | the request or the lead is not workable (missing id, missing write fields) |
| `crm-error:` | the MCP had nothing, or refused |
| other | the search/model call failed — the provider's own message |
