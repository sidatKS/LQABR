# Phase 2 — Email Agent (E4, Mailgun)

**Objective:** profiled leads with listed email IDs are contacted via
Mailgun; delivered / read (opened, last-opened) / clicked-link engagement
is recorded on the HubSpot contact and raises probability toward the
text/voice threshold (30).

## Build/verify order

1. **Mailgun setup** — verified sending domain (DNS), API key + webhook
   signing key in Secret Manager; `MAILGUN_DOMAIN`/`MAILGUN_FROM` in config.
2. **Webhook receiver** — deploy `lqabr-email-webhook` (script 05) or run
   locally behind a tunnel; register the URL in Mailgun → Webhooks for
   `delivered`, `opened`, `clicked`:
   `https://<service-url>/webhooks/mailgun`
3. **Send** — via the ADK harness (`adk web agents/email/src`, "send an
   outreach email to <email>") or the orchestrator. Every send attaches
   `hubspot_contact_id` as a Mailgun variable and enables open/click
   tracking.
4. **Engagement loop** — open the email and click the CTA link, then check
   the HubSpot contact:
   - `lqabr_email_delivered_count` +1 → probability +2
   - `lqabr_email_opened_count` +1, `lqabr_last_engaged_at` updated → +5
   - `lqabr_email_clicked_count` +1 → +10
   - crossing 30 flips `lqabr_stage` to `text_voice_outreach`

## Definition of Done

- End-to-end: send → open → click moves a lead from 10/12 to ≥ 27 with
  counters matching Mailgun's event log exactly.
- Forged webhook signatures are rejected (401); events without a contact
  id return 422 and appear in logs — never silently swallowed.
- A lead crossing 30 shows up in `list_text_voice_queue`.
- `pytest agents/email` green.

**Exit:** warmed leads exist for the Text/Voice Agent — Phase 3.
