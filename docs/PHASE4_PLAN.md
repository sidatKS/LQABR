# Phase 4 — Scheduling Agent (E6, Zoom Scheduler)

**Objective:** leads whose probability the Text/Voice Agent pushed past 60
are emailed available schedules across **EST / CST / PST / IST** using a
Zoom Scheduler booking link; a completed booking is written back to
HubSpot and pins probability at 95.

## Build/verify order

1. **Zoom setup** — Server-to-Server OAuth app with Scheduler scopes;
   create a Zoom Scheduler schedule (booking page); add an event
   subscription pointing at `https://<lqabr-scheduling-webhook>/webhooks/zoom`
   with the webhook secret token stored in Secret Manager. Optionally set
   `ZOOM_BOOKING_URL` to skip API lookup.
2. **Invite** — via `adk web agents/scheduling/src` ("send a schedule
   invite to <email>") or the orchestrator. Verify the email offers all
   four zones with example local times and the booking link.
3. **Booking loop** — book a slot as the lead; Zoom's event webhook fires:
   - endpoint URL validation handshake answered correctly
   - `lqabr_meeting_count` +1, probability pinned at 95
   - `lqabr_stage` → `meeting_scheduled` (rep handoff)

## Definition of Done

- Invite renders EST, CST, PST, IST options; link opens the booking page.
- A real booking updates the HubSpot contact within seconds; unknown
  invitee emails return 404 and are logged — never silently ignored.
- Zoom signature verification rejects forged events (401).
- `pytest agents/scheduling` green.

**Exit:** the full lead journey works end-to-end — Phase 5 automates it.
