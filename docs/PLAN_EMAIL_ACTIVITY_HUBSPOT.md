# Plan — Full email-activity persistence on the HubSpot profile

**Status:** Design / plan only (no code written yet)
**Date:** 2026-07-16
**Scope:** Extend the Email Agent → Mailgun webhook → HubSpot writeback path so
that *every* email event Mailgun emits is persisted on the contact, using the
existing **counters + flags** model. Adds unsubscribe, spam complaint, hard
bounce/failure, and soft-fail/accepted on top of today's delivered/opened/clicked.

---

## 1. Where we are today

Profiles are already the system of record in HubSpot (`HubSpotClient.upsert_lead`),
and three email events already flow back through `record_event`:

| Mailgun event | EventType        | Counter property             | Δ probability |
|---------------|------------------|------------------------------|---------------|
| `delivered`   | `EMAIL_DELIVERED`| `lqabr_email_delivered_count`| +2            |
| `opened`      | `EMAIL_OPENED`   | `lqabr_email_opened_count`   | +5            |
| `clicked`     | `EMAIL_CLICKED`  | `lqabr_email_clicked_count`  | +10           |

`agents/email/src/webhook_app.py` maps those three names via `EVENT_MAP` and
**ignores everything else** (`{"status": "ignored"}`). So delivery + read
("read receipt" = `opened`) are done; unsubscribe and all other activity are
silently dropped.

## 2. The gap and the key decision

The requested events split into two kinds, and they must be treated differently:

**Positive engagement** (already handled) raises probability.

**Negative / terminal signals — unsubscribe, spam complaint, hard bounce —
are not engagement; they are *suppression* signals.** They must NOT raise
probability, and they must set a **do-not-email flag** so the Email Agent stops
contacting that lead. This is a CAN-SPAM / compliance requirement, and today
nothing prevents continued sends to someone who opted out. This flag is the
most important part of the change; the counters are secondary.

**Soft-fail / accepted** are informational only — persisted as counters, no
scoring impact, no suppression.

Design choice (consistent with the "flag, never drop" convention): model
suppression as **boolean + reason + timestamp properties on the contact**, not
by forcing a stage change. It's simple, reversible (a re-subscribe clears it),
and both the Email Agent and orchestrator can honor it by reading one property.
Stage discipline (§6 of CLAUDE.md) is left untouched.

## 3. New event types — `packages/lqabr_core/lqabr_core/types.py`

Add to `EventType`:

```
EMAIL_ACCEPTED       = "email_accepted"        # Mailgun accepted the message
EMAIL_SOFT_FAILED    = "email_soft_failed"     # temporary failure (retryable)
EMAIL_BOUNCED        = "email_bounced"         # permanent failure / hard bounce
EMAIL_COMPLAINED     = "email_complained"      # spam complaint
EMAIL_UNSUBSCRIBED   = "email_unsubscribed"    # opt-out
```

## 4. Probability + suppression rules — `probability.py` (single source of truth)

Do **not** add these to `EVENT_INCREMENTS` — `apply_event` already returns +0
for any event not in the dict, so all five are non-scoring by construction.

Add an explicit suppression set and helper so the rule lives in one place:

```
SUPPRESSION_EVENTS = {EMAIL_UNSUBSCRIBED, EMAIL_COMPLAINED, EMAIL_BOUNCED}

def is_suppression(event_type) -> bool:
    return event_type in SUPPRESSION_EVENTS
```

Extend `EVENT_COUNTERS` with the five new counter properties (below).

## 5. New HubSpot properties — `infra/gcp/04_hubspot_properties.py`

Counters (mirror the existing `lqabr_email_*_count` pattern):

- `lqabr_email_accepted_count`
- `lqabr_email_softfail_count`
- `lqabr_email_bounced_count`
- `lqabr_email_complained_count`
- `lqabr_email_unsubscribed_count`

Suppression flag + metadata:

- `lqabr_email_suppressed` — bool (`booleancheckbox`)
- `lqabr_email_suppression_reason` — text (`unsubscribed` / `complained` / `hard_bounce`)
- `lqabr_email_suppressed_at` — text (ISO timestamp)

Optional refinement (the `last-opened` mentioned in CLAUDE.md §1 that isn't
stored distinctly today): `lqabr_email_last_opened_at`, set only on `opened`.

The script is idempotent (409 = exists), so re-running it just adds the new
ones.

## 6. Webhook mapping — `agents/email/src/webhook_app.py`

`EVENT_MAP` handles most names directly:

```
"accepted"     -> EMAIL_ACCEPTED
"unsubscribed" -> EMAIL_UNSUBSCRIBED
"complained"   -> EMAIL_COMPLAINED
```

`failed` needs a branch on Mailgun's severity field (a single name covers both
kinds): `event-data.severity == "permanent"` → `EMAIL_BOUNCED`; `"temporary"`
→ `EMAIL_SOFT_FAILED`.

All these events carry the original message's `user-variables`, so
`hubspot_contact_id` resolves the same way it does today — the existing
"missing contact_id → 422, never drop" guard stays.

## 7. Writeback — `HubSpotClient.record_event`

Extend so that when `is_suppression(event.event_type)` is true it also sets, in
the same PATCH: `lqabr_email_suppressed = "true"`,
`lqabr_email_suppression_reason`, and `lqabr_email_suppressed_at`. Probability
is unchanged for these (+0 → no promotion), so the contact stays in its stage
but is now flagged.

Refinement: only stamp `lqabr_last_engaged_at` for positive engagement
(delivered/opened/clicked), not for bounces/unsubscribes — a bounce isn't
engagement. Set `lqabr_email_last_opened_at` on `opened` if that property is added.

## 8. Enforce suppression on the send path — `agents/email/src/email_agent.py`

The flag only matters if the Email Agent honors it. In `send_outreach_email`,
after loading the lead, return early if `lqabr_email_suppressed` is set —
`{"status": "skipped", "reason": "email-suppressed: <reason>"}` — never sending.
Optionally have `list_email_queue` exclude suppressed leads so they never enter
the work queue. This is the compliance guarantee; without it the flag is cosmetic.

## 9. Tests (all external services mocked)

- `test_mailgun_webhook.py`: one case per new event — `unsubscribed`,
  `complained`, `failed` (permanent → bounced), `failed` (temporary → soft),
  `accepted`. Assert correct counter increment, `probability` unchanged, and
  suppression flag set only for the three suppression events.
- `test_email_agent.py`: a suppressed lead is skipped by `send_outreach_email`
  (and excluded from `list_email_queue` if that option is taken).
- `probability` tests: the five new events yield +0; `is_suppression` returns
  true only for unsubscribe/complaint/bounce.

## 10. Open considerations (worth a decision before building)

- **Duplicate webhook deliveries.** Mailgun retries; counters would
  double-count on redelivery. This already affects delivered/opened/clicked
  today. Setting the suppression boolean is naturally idempotent, but counts
  aren't. If exact counts matter, dedup on Mailgun's event id / message id.
  Recommend noting as a known limitation for now unless you want it in scope.
- **Terminal vs. reversible suppression.** Unsubscribe can be reversed (a lead
  re-subscribes → clear the flag); a spam complaint / hard bounce is
  effectively terminal. Same flag works for all three; the `reason` field
  preserves the distinction if you later want different handling.
- **Should hard-suppressed leads move to `UNRESOLVED`?** Optional. The flag +
  send-path guard already stops outreach; routing to `UNRESOLVED` with a reason
  would also stop the orchestrator from considering them. Leaning: keep stage,
  rely on the flag, revisit if the orchestrator needs it.

## 11. Touch list (when implementing)

1. `packages/lqabr_core/lqabr_core/types.py` — 5 new `EventType` values
2. `packages/lqabr_core/lqabr_core/probability.py` — `SUPPRESSION_EVENTS`,
   `is_suppression`, extend `EVENT_COUNTERS`
3. `infra/gcp/04_hubspot_properties.py` — 5 counters + 3 (or 4) flag properties
4. `packages/lqabr_core/lqabr_core/crm/hubspot.py` — suppression writeback in
   `record_event`; `_PROPERTIES` already derives counters from `EVENT_COUNTERS`
   but add the new flag props to the read list
5. `agents/email/src/webhook_app.py` — extend `EVENT_MAP` + `failed` severity branch
6. `agents/email/src/email_agent.py` — suppression guard in send path
7. Tests in `agents/email/tests/` and `packages/lqabr_core` probability tests
