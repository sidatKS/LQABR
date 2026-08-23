# Summary Agent — HTTP API

Base URL: the Cloud Run service URL, or `http://localhost:8080` locally.

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | identity + route index |
| GET | `/health`, `/healthz` | identical payloads; what this instance is bound to |
| GET | `/mcp/tools` | what the MCP container exposes right now |
| POST | `/summary/run` | the domain entry point |
| POST | `/summary/a2a` | the gateway / orchestrator A2A envelope |
| POST | `/chat` | AG-UI streaming (when `LQABR_SUMMARY_ROUTES=all` and AG-UI is enabled) |
| POST | `/run` | ADK-native, only under `adk api_server` |

## POST /summary/run

```jsonc
{
  "source": {
    "kind": "url",                      // url | api | json | text
    "url": "https://spring.io/blog/x",  // kind=url
    "endpoint": "http://svc/api/report",// kind=api
    "method": "GET",                    // kind=api
    "headers": {"Authorization": "…"},  // kind=api
    "body": {"q": "leads"},             // kind=api  (request body)
    "payload": {"any": "json"},         // kind=json (the data itself)
    "select": "$.data.article.body",    // kind=json|api, optional
    "text": "…"                         // kind=text
  },
  "hubspot": {                          // omit entirely to summarise only
    "object_id": "328791966455",
    "object_type": "ticket",            // defaults to the configured type
    "industry": "Fintech",              // optional
    "properties": {"source_url": "…"}   // optional extras, merged into the write
  }
}
```

`source` also accepts a bare string: a URL if it looks like one, text otherwise.

```bash
curl -sX POST localhost:8080/summary/run -H 'Content-Type: application/json' \
  -d '{"source": "https://spring.io/blog"}'
```

### Response

```jsonc
{
  "run_id": "sum-9f2c1a4b7d10",
  "status": "completed",                // completed | failed
  "source": {"kind": "url", "reference": "https://…", "title": "…",
             "chars": 18422, "truncated": false},
  "summary": {"title": "…", "topic": "…", "summary": "…",
              "key_points": [], "concepts": [], "technologies": [],
              "takeaways": [], "industry": "…"},
  "hubspot": {"status": "written",      // written | dry_run | skipped | error
              "object_id": "328791966455", "object_type": "ticket",
              "properties": ["blog_industry", "blog_summary"],
              "tool": "post_patch_crm", "error": ""},
  "model": "anthropic/claude-sonnet-5",
  "error": ""
}
```

**Status codes.** A refused source or an unusable model answer is `200` with
`status: "failed"` and the reason — the caller asked a valid question and
needs the outcome, not a stack trace. `422` is a malformed request body,
`500` an unexpected fault, `404` a route this deployment does not serve.

**`status` follows the write.** A summary that could not be landed reports
`status: "failed"` with `hubspot.status: "error"`, and still returns the
summary so the work is not lost.

## POST /summary/a2a

The gateway's JSON-RPC `message/send` envelope. The message text carries
WHAT to summarise (a URL, prose, or a full request object as JSON); the
object id comes from `params.metadata.object_id`, `params.metadata.summary_ref_id`,
or the gateway's top-level `object_id`/`objectId` mirror.

```jsonc
{
  "jsonrpc": "2.0", "id": "…", "method": "message/send",
  "params": {
    "message": {"role": "user", "parts": [{"kind": "text", "text": "https://…"}],
                "messageId": "…"},
    "metadata": {"object_id": "328791966455", "trigger_id": "…"}
  },
  "object_id": "328791966455"
}
```

Same response body as `/summary/run`.

## GET /mcp/tools

```jsonc
{
  "url": "http://hubspot-mcp:8080/mcp",
  "tools": ["get_lead_profile_details", "list_trigger_leads", "post_patch_crm"],
  "configured": {"read": "get_lead_profile_details", "write": "post_patch_crm",
                 "list_leads": "list_trigger_leads"},
  "missing": []
}
```

`missing` non-empty means the configured names do not exist on the server:
point `LQABR_SUMMARY_MCP_TOOL_*` at the real ones. `502` means the MCP could
not be reached at all.
