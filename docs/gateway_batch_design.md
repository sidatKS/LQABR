# N leads in one hand-off — gateway only, no orchestrator

**Design note · Agent Gateway (SP-1) · 4 August 2026**
**Constraint: the orchestrator is not in scope. Everything happens in the gateway.
Must work identically for the email agent and the voice agent.**

---

## The short version

The gateway **already receives leads in groups** — it just takes them apart again.

HubSpot delivers up to **100 events in a single POST**. Your gateway routes all of them,
then sends **one separate call per lead** to the agent. The grouping arrives and is
thrown away.

**The fix is to keep it.** Group the routed decisions by agent, and send **one call per
agent per request**, carrying the list of leads.

- No waiting — the leads already arrived together
- No stored state — it all happens inside one request
- No HubSpot writes from the gateway
- No new HubSpot property
- Works for email and voice with the same code

---

# PART 1 — What happens today

```
HubSpot POST  ──▶  5 events in one delivery
                        │
                        ▼
                  router.py  ──▶  5 decisions
                        │
                        ▼
                dispatch_all()  ──▶  loops, one call each
                        │
        ┌───────┬───────┼───────┬───────┐
        ▼       ▼       ▼       ▼       ▼
      call 1  call 2  call 3  call 4  call 5      ← 5 separate agent calls
```

From `src/dispatch.py`:

```python
def dispatch_all(self, decisions, run_id):
    results = []
    for decision in decisions:      # ← one hand-off per lead
        results.append(self.dispatch(decision, run_id))
```

Five leads that arrived together become five wake-ups.

---

# PART 2 — The change

```
HubSpot POST  ──▶  5 events in one delivery
                        │
                        ▼
                  router.py  ──▶  5 decisions
                        │
                        ▼
              GROUP BY AGENT  (new — a few lines)
                        │
            ┌───────────┴───────────┐
            ▼                       ▼
      email: [id1,id2,id3]    voice: [id4,id5]
            │                       │
            ▼                       ▼
       ONE call                ONE call
     3 leads inside          2 leads inside
```

**One call per agent, per request.** If a delivery contains three email-bound leads and
two voice-bound leads, that's exactly two agent calls instead of five.

## 2.1 What the agent receives

Today, per lead:

```json
{ "jsonrpc":"2.0", "method":"message/send",
  "params": { "message": { "parts": [{"kind":"text","text":"trg-eb4613…"}] },
              "metadata": { "trigger_id":"trg-eb4613…",
                            "object_id": 530431312580,
                            "run_id":"run-82cef5…",
                            "source":"agent_gateway",
                            "gateway_version":"0.1.0" } } }
```

Grouped, per agent:

```json
{ "jsonrpc":"2.0", "method":"message/send",
  "params": { "message": { "parts": [{"kind":"text","text":"bat-9f2c…"}] },
              "metadata": { "batch_id":"bat-9f2c…",
                            "object_ids": [530431312580, 523828708059, 529279894212],
                            "batch_size": 3,
                            "trigger_ids": ["trg-eb4613…","trg-4c89a5…","trg-731f4f…"],
                            "run_id":"run-82cef5…",
                            "source":"agent_gateway",
                            "gateway_version":"0.1.0" } } }
```

Still **only ids**. No name, no email, no phone. The trigger-only guarantee holds.

`batch_id` is your "one trigger id covering N leads" — minted by the gateway,
deterministic from the run, and **never written to HubSpot**.

## 2.2 The agent side — identical for email and voice

```python
def work_batch(object_ids: list[str]) -> dict:
    leads = [HubSpotClient().get_lead(oid) for oid in object_ids]
    ...
```

`get_lead()` is a **fetch by id** — strongly consistent, no search-index lag. Fetching
three ids is three fast reads, and it never misses a lead the way a property search can.

The voice agent does the same thing with `POST /lead`, just placing calls instead of
sending mail. Nothing about the mechanism is email-specific.

---

# PART 3 — When you actually get a group

Honest answer: **when HubSpot sends one.** You get batching exactly when there's a burst,
which is exactly when it's worth having.

| What happens in HubSpot | Delivery | Result |
|---|---|---|
| `push_leads.py` inserts 50 contacts | HubSpot batches heavily | **big groups** |
| Email agent marks 20 leads `OPENED` in a loop | batched | **groups** |
| Voice agent writes 10 × `COMPLETED` | batched | **groups** |
| One person edits one contact in the UI | 1 event | 1 lead, 1 call |
| Two leads change an hour apart | 2 deliveries | 2 calls |

So bulk activity groups; isolated changes don't. **This is the correct behaviour** — a
single lead that just opened an email should be called immediately, not held back waiting
for four friends.

**What this does not give you:** a guaranteed "always exactly 5." That requires waiting,
and waiting is Part 5.

---

# PART 4 — What changes in the code

Four files. All small. No new dependency, no HubSpot property, no token.

### 4.1 `src/dispatch.py` — group before dispatching

Add a `dispatch_grouped()` alongside the existing `dispatch_all()`:

```python
def dispatch_grouped(self, decisions, run_id):
    """One hand-off per agent per request, instead of one per lead."""
    by_agent = {}
    for d in decisions:
        by_agent.setdefault(d.agent, []).append(d)

    results = []
    for agent, group in by_agent.items():
        results.append(self.dispatch_batch(agent, group, run_id))
    return results
```

`dispatch_batch()` builds the metadata in §2.1 and posts once to that agent's endpoint.

### 4.2 `lib/soloai/protocols/a2a.py` — widen the guard, deliberately

```python
ALLOWED_METADATA_KEYS = frozenset({
    "trigger_id", "object_id", "run_id", "route_id", "source", "gateway_version",
    "batch_id", "object_ids", "batch_size", "trigger_ids",     # ← added
})
```

This is the only place the trigger-only rule is enforced, so the addition should be
explicit and reviewed. **Note what is still excluded:** every profile field. A list of
ids is still not lead data.

### 4.3 `src/server.py` — call the grouped path

```python
outcomes = dispatcher.dispatch_grouped(result.decisions, run_id)
```

Behind a config flag so it can be turned off without a redeploy:

```yaml
dispatch:
  group_by_agent: true     # false = one call per lead (current behaviour)
```

### 4.4 `src/audit.py` — record the batch

One extra field on the dispatch record: `batch_size`. The `run_summary` already reports
`events_received` and `routed`, so the log will show *5 routed, 2 dispatched* — which is
the whole point, visible at a glance.

## 4.5 Failure handling — the bit worth thinking about

Today one lead fails independently. Grouped, a failed call means **the whole group** is
retried by HubSpot.

That's acceptable **because the dedupe store is per-event**: only leads whose hand-off
actually landed get remembered. On HubSpot's retry, the ones that succeeded are deduped
out and only the genuinely-failed ones are re-sent. That existing mechanism already does
the right thing — no change needed, but it's the reason grouping is safe here.

---

# PART 5 — If you truly need "always exactly 5"

Only reachable by waiting, and waiting brings back everything we ruled out:

- **Lead #1 waits for lead #5.** If leads trickle, first-touch is delayed by hours.
- **A partial batch needs a timer**, so the size becomes "whatever arrived," often 1.
- **The pending batch must survive restarts**, so it needs a shared store — the same
  problem the dedupe store has, and the reason `--max-instances 1` is pinned today.
- **Two Cloud Run instances** each accumulate their own partial batch and neither reaches 5.

If it's genuinely required, do it as a **second stage** on top of Part 2 rather than
instead of it — keep the per-request grouping, and add the accumulator behind a flag with
a mandatory short timer (30–60s). But I'd want a concrete cost that batching solves before
building it.

---

# PART 6 — Both agents, end to end

```
   ┌───────────────────────────────────────────────────────────┐
   │  HubSpot CRM                                              │
   │  5 property changes happen close together                 │
   └────────────────────────────┬──────────────────────────────┘
                                │  ONE webhook POST · 5 events
                                ▼
   ┌───────────────────────────────────────────────────────────┐
   │  AGENT GATEWAY                                            │
   │                                                           │
   │  verify signature  →  route each event                    │
   │                                                           │
   │     3 × decision_maker=true        → email                │
   │     2 × email_status=OPENED  → voice                │
   │                                                           │
   │  GROUP BY AGENT  →  mint one batch_id per group           │
   └───────────┬───────────────────────────┬───────────────────┘
               │                           │
     ONE call  │                           │  ONE call
   3 object_ids│                           │ 2 object_ids
               ▼                           ▼
   ┌───────────────────────┐   ┌───────────────────────────────┐
   │  EMAIL AGENT          │   │  TEXT / VOICE AGENT           │
   │  POST /lead           │   │  POST /lead                   │
   │                       │   │                               │
   │  for id in object_ids:│   │  for id in object_ids:        │
   │    get_lead(id)       │   │    get_lead(id)               │
   │    send email         │   │    place Vapi call            │
   │    write email_status │   │    write voice_status         │
   └───────────┬───────────┘   └───────────────┬───────────────┘
               │                               │
               └───────────┬───────────────────┘
                           ▼
              status written back to HubSpot
              → fires the next route (loop guard prevents self-trigger)
```

**Identical treatment for both agents.** The gateway doesn't know or care which agent a
group is for — it groups by whatever the routing table decided.

---

## Recommendation

Build Part 2. It is a few dozen lines, needs no HubSpot changes, keeps the trigger-only
guarantee, works the same for email and voice, and is switchable via config.

Leave Part 5 alone unless a measured cost justifies it.

---

## Appendix — what this replaces

The earlier proposal was: accumulate 5 events in the gateway → write a shared
`lqabr_trigger_id` to all 5 contacts in HubSpot → agent searches HubSpot for that id.

Dropped because:

| | Old proposal | Part 2 |
|---|---|---|
| Lead #1 waits for lead #5 | Yes | **No** |
| Gateway needs a HubSpot write token | Yes | **No** |
| New HubSpot property | Yes | **No** |
| Agent lookup | search — **eventually consistent**, can miss leads | fetch by id — **strongly consistent** |
| Batch state survives restart | needs shared store | **nothing to lose** |
| Safe with >1 instance | No | **Yes** |
| Lines of code | many | ~40 |
