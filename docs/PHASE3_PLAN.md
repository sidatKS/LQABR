# Phase 3 — Text/Voice Agent (E5, Twilio)

**Objective:** only probability-incremented leads (≥ 30, promoted by the
Email Agent) are contacted by SMS and voice. Two flows:

- **Flow A — no answer:** a customized voicemail is delivered after the
  beep, followed by a customized SMS the lead can reply to.
- **Flow B — answered:** the conversational question-and-answer pattern
  runs (speech Gather, 3 steps); the answered-call counter is incremented
  in HubSpot and completing the flow with interest adds the engaged bonus.

## Build/verify order

1. **Twilio setup** — SMS/voice-capable number; SID + auth token in Secret
   Manager; `TWILIO_FROM_NUMBER` in config.
2. **Webhook receiver** — deploy `lqabr-text-voice-webhook` (script 05
   also sets `LQABR_WEBHOOK_BASE_URL` on both text_voice services). No
   dashboard webhook config needed: answer/status URLs are set per call.
3. **Voicemail flow (A)** — call a test number that goes to voicemail:
   `lqabr_voicemail_count` +1 (+2), SMS follow-up sent, then
   `lqabr_sms_delivered_count` +1 (+3) on the delivery receipt.
4. **Answered flow (B)** — answer the test call, say "yes" through the
   3 questions: `lqabr_call_answered_count` +1 (+15) at pickup;
   `lqabr_call_engaged_count` +1 (+15) at completion. 30 + 15 + 15 = 60 —
   the lead crosses the scheduling threshold and `lqabr_stage` flips to
   `scheduling`.
5. **Queue discipline** — `list_text_voice_queue` only ever returns leads
   in `text_voice_outreach`; leads below 30 are untouchable by this agent.

## Definition of Done

- Both flows verified against real Twilio calls to test numbers, counters
  matching Twilio's call/message logs.
- Signature validation rejects forged webhook posts (401);
  `LQABR_SKIP_TWILIO_SIGNATURE` is never set outside local dev.
- An answered + engaged call promotes a threshold-entry lead to
  `scheduling` in HubSpot.
- `pytest agents/text_voice` green (17 tests: TwiML, state machine,
  webhooks, client).

**Exit:** leads at ≥ 60 await booking invites — Phase 4.
