# Getting N leads at once — without paying HubSpot

**Design note · Agent Gateway (SP-1) · 4 August 2026**

---

## The question

> *"When 5 leads are triggered, these 5 leads should be taken under one trigger id, that
> trigger should be saved in a HubSpot property, and when it's sent to the agent, the
> agent calls that trigger id in HubSpot and gets all the leads under it."*

And then: *"Can we do this with the paid version of HubSpot?"*

---

## The answer, in three lines

1. **HubSpot cannot trigger N leads at once. Not on any tier — free or paid.** Webhooks
   fire once per record. There is no setting anywhere that says "wait for 5, then send one."
2. **The paid version does not fix this.** It sells you a *place to store* a group and
   *code to write yourself* — not the grouping behaviour.
3. **You already have a way to get N leads at once, and it's already built.** It's the
   lead's **stage**, pulled on a schedule. Nothing to buy, nothing new to invent.

---

# PART 1 — Why HubSpot can't do it

## 1.1 Webhooks are per-record, always

A HubSpot webhook subscription means: *"tell me whenever this property changes on any
contact."* One contact changes → one event. Five contacts change → five events.

HubSpot may happen to put several events in one HTTP delivery (up to 100 per POST), but
that's just timing — it groups whatever happened to occur close together. You cannot
configure it, rely on it, or ask for "exactly 5."

**There is no "batch trigger" feature in HubSpot to buy.** It isn't behind a paywall. It
doesn't exist.

## 1.2 Workflows don't batch either

HubSpot Workflows (Professional and up) enrol records **one at a time**. A workflow runs
per contact. There is no "hold this contact until four more arrive" step.

---

# PART 2 — Why the paid version isn't useful to us

Three reasons, in order of importance.

## 2.1 It doesn't remove the waiting — and waiting is the real problem

You spotted this yourself when we first discussed it:

> *"I got it what you are saying — we should update the agent gateway like we will wait
> till we get the 5 leads and then it will generate the trigger id."*

If you group five leads before acting, **lead #1 waits for lead #5**. If leads arrive one
an hour, lead #1 waits four hours for its first email. If only three ever arrive, they
wait forever unless a timer gives up.

That's arithmetic, not a HubSpot limitation. Paying $800 a month does not change it.
Wherever the waiting logic lives — your gateway or a HubSpot workflow — someone waits.

## 2.2 Custom Objects (Enterprise) give storage, not triggering

Enterprise unlocks **Custom Objects** — you could create a `trigger_batch` object and
associate five contacts to it. That is a genuinely nicer *data model* than a text
property, and it would be fetched by id, so it avoids the search-lag problem in §4.2 of
the appendix.

But it is a **filing cabinet, not a trigger**. Something still has to decide *when* five
leads form a batch, create the record, and attach them. That something is code you write.
Enterprise sells you a better shelf; you still build the machine.

## 2.3 Operations Hub Professional ($800/mo) just lets you write the same code, in HubSpot

Ops Hub Professional is the first tier with custom-coded (Node.js) workflow actions and
webhooks. So yes, you *could* build the batching inside HubSpot instead of in the gateway.

But it's the same logic, the same waiting, in a place where it's harder to test, harder
to log, and harder to version-control than a Python file in your repo. You'd be paying
$9,600 a year to move code from a place you control to a place you don't.

## 2.4 And the thing you'd be buying, you already own

The critical point: **you wanted a property that groups leads. HubSpot already has one.**

`lqabr_stage` — verified live in the portal — holds each lead's pipeline position:

```
ingested → profiled → email_outreach → text_voice_outreach → scheduling → meeting_scheduled
```

Leads waiting for the email agent all sit in `email_outreach`. **That is the group.** It
already exists, it's already populated, and it already has a search API.

---

# PART 3 — How to get N leads at once, free

## 3.1 The idea

Stop trying to make HubSpot *push* five leads. Instead, **pull** five leads whenever you
want them.

The leads are already sitting in a stage. Ask for five.

## 3.2 The flow

```
   ┌──────────────────────────────────────────────────────────────┐
   │  HubSpot CRM                                                 │
   │                                                              │
   │  Leads accumulate naturally in a stage:                      │
   │     lqabr_stage = "email_outreach"                           │
   │                                                              │
   │  ← this IS the group. No new property needed.                │
   └───────────────────────────┬──────────────────────────────────┘
                               │
                               │  ① Cloud Scheduler fires
                               │     every 30 minutes
                               ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  ORCHESTRATOR            lqabr-orchestrator-agent            │
   │                                                              │
   │  ② mints ONE batch_id for this cycle                         │
   │  ③ asks HubSpot for the queue:                               │
   │                                                              │
   │       leads_in_stage(EMAIL_OUTREACH, limit=5)                │
   │       → POST /crm/v3/objects/contacts/search                 │
   │         filter: lqabr_stage EQ "email_outreach"              │
   │                                                              │
   │  ④ gets 5 LeadProfiles back in ONE call                      │
   └───────────────────────────┬──────────────────────────────────┘
                               │
                               │  ⑤ one A2A hand-off carrying
                               │     batch_id + the 5 object_ids
                               ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  EMAIL AGENT                                                 │
   │                                                              │
   │  ⑥ works all 5 together                                      │
   │  ⑦ writes email_status + moves each lead's stage on    │
   └───────────────────────────┬──────────────────────────────────┘
                               │
                               ▼
              lead leaves the queue automatically —
              next cycle picks up whoever is left
```

## 3.3 Why this is better than the batch design

| | Batch in the gateway | Pull by stage (this) |
|---|---|---|
| Lead #1 waits for lead #5 | **Yes** | **No** — it's already in the queue, working or not |
| New HubSpot property | Yes (`lqabr_trigger_id`) | **No** — `lqabr_stage` exists |
| Gateway writes to CRM | Yes | **No** |
| Batch state that can be lost | Yes | **No** — HubSpot holds the queue |
| Breaks if 2 instances run | Yes | **No** |
| Paid tier needed | No, but Enterprise would help | **No** |
| Already built | No | **Mostly yes** |

The deep reason it's better: **HubSpot is the queue.** You don't have to hold five leads
in memory and hope nothing restarts — they're sitting safely in the CRM with a stage on
them. If the orchestrator crashes mid-cycle, nothing is lost; the next cycle sees the
same leads still in `email_outreach`.

## 3.4 What already exists

Almost all of it. Verified in the repo:

| Piece | Where | Status |
|---|---|---|
| Scheduled trigger, every 30 min | `infra/gcp/06_cloud_scheduler.sh` → `lqabr-dispatch-cycle` | **Built** |
| Orchestrator + A2A dispatch | `agents/orchestrator/src/a2a_dispatch.py` | **Built** |
| The stage property | `lqabr_stage` in HubSpot | **Live** |
| Pull N leads by stage | `lqabr_core/crm/hubspot.py` → `leads_in_stage(stage, limit)` | **Built** |
| Email agent's queue tool | `email_agent.py` → `list_email_queue(limit=25)` | **Built** |

The exact call that returns N leads at once:

```python
def leads_in_stage(self, stage: LeadStage, min_probability: int = 0,
                   limit: int = 100) -> List[LeadProfile]:
    filters = [{"propertyName": "lqabr_stage", "operator": "EQ", "value": stage.value}]
    ...
    result = self._request("POST", "/crm/v3/objects/contacts/search", json=body)
    return [self._from_contact(c) for c in result.get("results", [])]
```

`limit=5` gives you five leads. That is the whole feature you were asking HubSpot to sell you.

## 3.5 What's left to do

Small, and none of it needs HubSpot money.

1. **Set the batch size.** `list_email_queue(limit=25)` today — change to 5 if that's the
   group size you want, or make it config.
2. **Mint a `batch_id` per cycle** in the orchestrator, and pass it in the A2A metadata
   alongside the object_ids. This is your "trigger id for the group" — but it's minted by
   the orchestrator and never needs storing in HubSpot.
3. **Tune the schedule.** 30 minutes today. Every 5 minutes gives smaller, fresher batches.
4. **Make sure agents move the stage on** when done, so leads leave the queue. If they
   don't, the next cycle picks up the same five forever.

Point 4 is the one that bites. The queue only drains if the agent advances
`lqabr_stage`.

---

# PART 4 — The two paths, and why you want both

This isn't batch *instead of* the gateway. They do different jobs.

```
                    ┌──────────────────────┐
                    │     HubSpot CRM      │
                    └──────┬────────┬──────┘
                           │        │
        webhook, instantly │        │ search by stage, on a schedule
                           ▼        ▼
              ┌────────────────┐  ┌────────────────────┐
              │  AGENT GATEWAY │  │   ORCHESTRATOR     │
              │  1 lead        │  │   N leads          │
              │  milliseconds  │  │   every 30 min     │
              │  event-driven  │  │   queue-driven     │
              └───────┬────────┘  └─────────┬──────────┘
                      │                     │
                      └──────────┬──────────┘
                                 ▼
                         Email / Voice / Scheduling agents
```

| | Gateway | Orchestrator |
|---|---|---|
| Trigger | HubSpot webhook | Cloud Scheduler |
| Leads per hand-off | 1 | N |
| Latency | milliseconds | up to one cycle |
| Good for | "this lead just opened the email — call them now" | "work through the backlog efficiently" |
| Status | **proven live** | built, waiting on agents |

Use the gateway when timing matters. Use the orchestrator when volume matters.

---

## Recommendation

**Don't buy anything.** Set `limit` to your batch size, mint a `batch_id` in the
orchestrator, and tune the schedule. That's the feature.

Revisit Enterprise only if you find a reason custom objects genuinely serve — modelling
something the standard objects can't. Not for this.

---

# Appendix — the original batch design, and the four reasons it was dropped

Kept for the record. This is what we designed before landing on the pull model.

**The design:** gateway accumulates 5 matched events → mints one `trigger_id` → writes it
to `lqabr_trigger_id` on all 5 contacts → dispatches once → agent searches HubSpot for
contacts with that trigger_id.

**A1 — Somebody waits.** §2.1 above. This alone is disqualifying for first-touch outreach.

**A2 — HubSpot search is eventually consistent.** The agent would find the group with
`POST /contacts/search` filtered on the property. That index lags writes by seconds. The
gateway writes five, dispatches immediately, the agent searches and gets **three** — with
no way to know two are missing, and no retry, because the hand-off "succeeded."

`GET /crm/v3/objects/contacts/{id}` — what we do now — is strongly consistent. This is the
strongest technical argument against it.

**A3 — The gateway would have to write to HubSpot.** Today it makes zero CRM calls and
holds no token, deliberately:

> *"The gateway routes a trigger id; it holds no lead data, calls no model, and touches no
> CRM SDK."* — `requirements.txt`

Giving it write scope is a Rev 3 deviation needing sign-off, not a code change.

**A4 — Partial failures get much worse.** Three of five contacts written, then the
dispatch fails. HubSpot retries all five original events independently, each starting a
*new* batch, while a half-written trigger id sits on three records. Today there is one
failure mode; this design has several, tied to CRM writes that can't be rolled back.

**What we chose instead — deviation D-05:** one event → one `trigger_id` → one
`object_id`, resolved with a strongly-consistent fetch. `trigger_id` stays as a
correlation handle only — deterministic, so retries mint the same id — and is
deliberately not stored in HubSpot.

Proven live: contact `523828708059` in 11.63 ms, contact `530431312580` in 111.44 ms.

---

## Sources

- [HubSpot — create custom objects](https://knowledge.hubspot.com/object-settings/create-custom-objects) (Enterprise required)
- [Custom Objects: Professional vs Enterprise](https://daeda.tech/blogs/hubspot-custom-objects-professional-vs-enterprise/)
- [Operations Hub pricing 2026](https://automationatlas.io/answers/hubspot-operations-hub-pricing-2026/) ($800/mo Professional)
