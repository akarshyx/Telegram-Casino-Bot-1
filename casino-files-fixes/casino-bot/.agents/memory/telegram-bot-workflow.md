---
name: Telegram bot workflow
description: Runtime setup needed to keep the nested Python Telegram bot polling.
---

The Telegram polling process must run as a separate console workflow from the casino web artifact.

**Why:** The registered casino web artifact only starts the Vite preview, so Telegram commands appear dead even though the web preview is healthy.

**How to apply:** Keep a workflow that runs `cd casino-files-fixes/casino-bot && bash run.sh` and verify its logs show webhook removal, polling startup, and successful command registration.