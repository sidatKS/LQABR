# What the gateway actually sends

Captured live on **2026-08-25** from the running gateway. Both shapes below are
verbatim — they are the contract this agent parses, and each one broke
something when it first arrived. Keep this file honest: if the gateway changes
shape again, paste the new payload here with the date.

---

## 1. The A2A envelope (what the gateway sends today)

JSON-RPC on the outside, **HubSpot's own event on the inside**. The metadata is
forwarded verbatim from HubSpot, so it is **camelCase**.

```json
{
  "jsonrpc": "2.0",
  "id": "2b2a2857-191d-4d0e-9f4e-468b0428ba5d",
  "method": "message/send",
  "params": {
    "message": {
      "role": "user",
      "parts": [{"kind": "text", "text": "trg-837dd204bd6a5811b410b20d"}],
      "messageId": "4f7a27d1-96c6-4d16-8cc4-d751ce2ec54c"
    },
    "metadata": {
      "objectId": "329213149924",
      "propertyName": "blog_summary",
      "propertyValue": "hi this is srinivas , how are you doing , ",
      "subscriptionType": "ticket.propertyChange",
      "portalId": 246777241,
      "eventId": "1107748449",
      "occurredAt": 1787651383486,
      "attemptNumber": 0,
      "changeSource": "CRM_UI",
      "triggerId": "trg-837dd204bd6a5811b410b20d"
    }
  }
}
```

## 2. The raw HubSpot webhook (also seen, unwrapped)

Same event with no JSON-RPC around it.

```json
{
  "objectId": "329213149924",
  "propertyName": "blog_summary",
  "propertyValue": "hi this is srinivas , how are you doing , ",
  "subscriptionType": "ticket.propertyChange",
  "portalId": 246777241,
  "eventId": "3425256010",
  "occurredAt": 1787652854704,
  "attemptNumber": 0,
  "changeSource": "CRM_UI",
  "triggerId": "trg-f524fec9cdd65a4395f3ec5b"
}
```

---

## What the agent reads

| Field | Read as | Used for |
|---|---|---|
| `metadata.objectId` / `metadata.object_id` / top-level either | `objectId` | the record to work on — a **blog POST** on `/research/campaign/a2a`, a **CONTACT** on `/research/a2a` |
| `metadata.summaryObjectId` / `summaryRefId` / `summary_ref_id` | `summary_objectId` | the post, on the single-lead route only |
| `metadata.runId` / `run_id` | `run_id` | correlates this agent's log with the gateway's |
| `metadata.industry`, `metadata.limit` | campaign overrides | hand-driven re-runs; the gateway does not send them |
| `subscriptionType` | `record_kind()` → `contact` \| `post` | refuses a wrong-route hand-off **at the door** |
| `propertyName`, `eventId` | logged on `http_in` | tracing one run back to one HubSpot event |
| `attemptNumber` | logged **only when > 0** | a redelivery, which otherwise looks like a duplicate campaign |

Everything else (`portalId`, `occurredAt`, `changeSource`, `triggerId`,
`params.message`) is ignored on purpose. `propertyValue` in particular is
**not** used — the summary is read from the CRM through the MCP, so the agent
never works from a value that may already be stale by the time it runs.

**One spelling inside: `objectId`.** Variables, model fields, log fields and
HTTP responses all use it. `object_id` survives in exactly two places, both
because somebody else defines the name:

* **`schema.py`** — the gateway's older metadata key and top-level mirror, read
  through the field alias. Requests to `/research/run` and `/research/campaign`
  still accept `object_id` too, so a documented curl kept working.
* **`hubspot.py`** — the MCP tool's own argument name. Sending `objectId` there
  fails with `Extra inputs are not permitted`.

The blog post's id follows the same rule: it is `summary_objectId` inside — the
same `objectId` token as the contact's — while `summary_ref_id` / `summaryRefId`
stay accepted on the wire, and `--summary-ref-id` stays accepted on the CLI
alongside `--summary-object-id`.

`tests/test_standalone.py` fails the build if `object_id` appears in any other
file, or if the other five wire spellings (`summaryRefId`, `subscriptionType`,
`propertyName`, `eventId`, `attemptNumber`) escape `schema.py`. Metadata wins
over the top level. Widening only — no spelling stopped working.

## Two things this cost us

**A hand-off with a perfectly good id was rejected for not having one.**
`_meta()` looked for `objectId`; the gateway sends `objectId`. The reply was
`{"status": "rejected", "reason": "payload carries no objectId"}` on a payload
that plainly carries one. Fixed by `A2AEnvelope._from_meta`, which reads either
spelling. Pinned by `test_the_gateways_camelcase_metadata_resolves_its_id`.

**A post sent to the contact route used to die three steps later.** The id
resolves fine on both routes, so `/research/a2a` would accept a Ticket, then
fail at `read_lead` with `crm-error: the MCP returned no lead` — which reads
like a missing contact rather than a misrouted post. `subscriptionType` says
which kind it is, so the route now refuses it immediately and names where it
should go:

```json
{"status": "rejected",
 "reason": "bad-data: this route takes a contact, but the hand-off is a HubSpot ticket.propertyChange — id 329213149924 is a post. Send it to /research/campaign/a2a."}
```

## Replaying one

```bash
curl -sX POST localhost:8086/research/campaign/a2a \
  -H 'Content-Type: application/json' \
  -d @- <<'JSON' | python3 -m json.tool
{"jsonrpc":"2.0","id":"replay","method":"message/send",
 "params":{"metadata":{"objectId":"329213149924",
                       "subscriptionType":"ticket.propertyChange"}}}
JSON
```

> **Note on this particular ticket.** `329213149924` is `blog_industry:
> HEALTHCARE`, subject `string`, and its `blog_summary` is
> `"hi this is srinivas , how are you doing , "` — a CRM-UI test edit. A
> campaign on it runs for real against every HEALTHCARE lead and writes
> `lead_context` from that summary. Use a post with a real summary, or expect
> five real contacts to get notes grounded in nothing.
