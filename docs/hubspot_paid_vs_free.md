# HubSpot free vs paid — what we'd actually be buying

**Agent Gateway (SP-1) · verified against HubSpot documentation, 5 August 2026**

---

## The question

> *"Can we directly trigger N leads at once from HubSpot with the paid version?"*

## The answer in three lines

1. **No. Not on any tier.** HubSpot webhooks fire **once per record**. There is no setting, on free or paid, that says "wait for 5, then send one."
2. **The paid tiers sell storage and a place to write code — not the grouping behaviour.**
3. **The grouping we want already arrives free**, and the gateway currently throws it away.

There is no "batch trigger" feature to buy. It isn't behind a paywall — it doesn't exist.

---

# PART 1 — What the free tier already gives us

All four verified live in portal 246777241.

| Capability | Status |
|---|---|
| Private app webhook subscriptions (contact.creation, propertyChange) | working |
| **Up to 100 events in one POST** | documented, and this is the grouping we want |
| v3 HMAC signature verification | working — `signature_verified: true` |
| Retries: up to 10 times over 24h on non-2xx | working — drives our 503 contract |
| `GET /crm/v3/objects/contacts/{id}` — strongly consistent read | what the agents use |
| `POST /crm/v3/objects/contacts/batch/read` — up to 100 ids, one call | available, unused |
| `POST /crm/v3/objects/contacts/search` — filter by property | available |

Proven end to end on real leads: contact `523828708059` routed in **11.63 ms**, contact `530431312580` in **111.44 ms**.

**Nothing on this list requires payment.**

---

# PART 2 — Why webhooks can't batch, on any tier

A subscription means *"tell me whenever this property changes on any contact."* One contact changes → one event. Five change → five events.

HubSpot **may** put several events in one HTTP delivery — up to 100 — but that is timing, not configuration. It groups whatever happened to occur close together. You cannot request it, tune it, or ask for "exactly 5."

Workflows don't help either: they enrol records **one at a time**. There is no "hold this contact until four more arrive" step at any price.

---

# PART 3 — What the paid tiers actually sell

### 3.1 Operations Hub Professional — **$800/month** ($9,600/year)

First tier with custom-coded (Node.js) workflow actions, data quality automation and programmable webhooks.

So yes, you *could* build the batching inside HubSpot. It would be **the same logic, the same waiting**, in a place that is harder to test, harder to log, and harder to version-control than a Python file in our repo.

Enterprise is $2,000/month and changes nothing about this.

**Verdict: pays to move code from somewhere we control to somewhere we don't.**

### 3.2 Custom Objects — **Enterprise only**

Confirmed in HubSpot's own docs: custom objects require an Enterprise subscription (Marketing / Sales / Service / Data / Content / Smart CRM / Revenue Hub Enterprise).

We could create a `trigger_batch` object and associate five contacts to it. That is a genuinely better *data model* than a text property, and it would be fetched by id rather than searched — so it avoids the eventual-consistency problem in §4.2.

But it is a **filing cabinet, not a trigger.** Something still has to decide *when* five leads form a batch, create the record, and attach them. That something is code we write.

**Verdict: a better shelf. We still build the machine.**

### 3.3 The thing we'd be paying for, we already own

We wanted a property that groups leads. `lqabr_stage` already does exactly that, is already populated, and already has a search API:

```
ingested → profiled → email_outreach → text_voice_outreach → scheduling → meeting_scheduled
```

---

# PART 4 — The part no tier fixes

### 4.1 Somebody waits

Group five leads before acting and **lead #1 waits for lead #5**. If leads arrive an hour apart, lead #1 waits four hours for its first email. If only three ever arrive, they wait forever unless a timer gives up.

That is arithmetic, not a HubSpot limitation. $800/month does not change it. Wherever the waiting logic lives — our gateway or a HubSpot workflow — someone waits.

### 4.2 HubSpot search is eventually consistent

Any design where the agent *searches* for the group hits this. `POST /contacts/search` reads an index that lags writes by seconds. Write five, dispatch immediately, agent searches and gets **three** — with no way to know two are missing and no retry, because the hand-off "succeeded."

`GET /crm/v3/objects/contacts/{id}` — what we do today — is strongly consistent. This is the strongest technical argument against the whole search-the-batch approach, paid or free.

---

# PART 5 — How to get N leads at once, free

**The gateway already receives leads in groups. It just takes them apart again.**

HubSpot delivers up to 100 events in a single POST. `dispatch_all()` loops and sends **one call per lead**. The grouping arrives and is discarded.

```
BEFORE                                  AFTER
5 events in one POST                    5 events in one POST
      ↓                                       ↓
router → 5 decisions                    router → 5 decisions
      ↓                                       ↓
dispatch_all() loops                    group by agent
      ↓                                       ↓
5 separate agent calls                  2 calls — 3 ids + 2 ids
```

One call per agent per request, carrying the list of leads:

```json
"metadata": {
  "batch_id":    "bat-9f2c…",
  "object_ids":  [530431312580, 523828708059, 529279894212],
  "batch_size":  3,
  "trigger_ids": ["trg-eb4613…", "trg-4c89a5…", "trg-731f4f…"]
}
```

- No waiting — the leads already arrived together
- No stored state — it happens inside one request
- No HubSpot writes, no new property, no write token
- Still ids only, so the trigger-only guarantee holds
- ~40 lines, switchable via config, identical for email and voice

**When you get a group:** when HubSpot sends one — bulk inserts, an agent marking 20 leads in a loop. Isolated changes stay ungrouped, which is correct: a lead that just opened an email should be contacted now, not held.

---

## Recommendation

**Buy nothing.**

Free tier does everything this project needs. If we want N-at-a-time, group inside the gateway — it's a few dozen lines and needs no HubSpot changes at all.

Revisit Enterprise only if custom objects model something the standard objects genuinely can't. Not for this.

---

## Sources

- [HubSpot — Create and edit custom objects](https://knowledge.hubspot.com/object-settings/create-custom-objects) — Enterprise required, all hubs
- [HubSpot Ops Hub pricing 2026](https://automationatlas.io/answers/hubspot-operations-hub-pricing-2026/) — Professional $800/mo, Enterprise $2,000/mo; Professional is the first tier with custom-coded actions
- [Custom Objects: Professional vs Enterprise](https://daeda.tech/blogs/hubspot-custom-objects-professional-vs-enterprise/)
- [HubSpot pricing 2026 — all hubs and tiers](https://www.resonatehq.com/hubspot-pricing)
