# 03 — Pub/Sub topics + subscription

Script: `03_pubsub.sh` · Run 2026-07-15 · **done**

## What it does

Creates the two Pub/Sub topics that carry ingestion triggers and engagement
events, plus the pull subscription the platform consumes. Idempotent.

## Command

```bash
# env-override context as in config.md
bash 03_pubsub.sh
```

## Output / verification observed

```
topics:
  projects/ldqfingsrv/topics/lqabr-ingestion-trigger
  projects/ldqfingsrv/topics/lqabr-engagement-events
subscriptions:
  projects/ldqfingsrv/subscriptions/lqabr-engagement-events-pull
```

## Deviations / gotchas

- None. Clean create.
