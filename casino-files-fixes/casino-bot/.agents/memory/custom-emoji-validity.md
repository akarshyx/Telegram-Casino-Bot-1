---
name: Telegram custom emoji validity
description: Telegram custom emoji IDs can become unavailable to a bot even when they remain in source code.
---

Custom emoji tags embedded in Telegram HTML must be validated with the bot token; stale IDs can make an otherwise valid message fail to send. Keep a plain Unicode fallback for IDs that Telegram no longer returns.

**Why:** The Telegram API returned missing IDs for an existing game flow, while the remaining IDs were valid. Falling back prevents message delivery failures without changing game accounting.

**How to apply:** When adding or restoring custom emoji tags, validate them with `getCustomEmojiStickers` and retain a safe plain-emoji path for every user-facing message.