---
name: TON/GRAM deposit aliases
description: Durable rules for handling native Toncoin labels across provider sessions, chain scans, rates, and user-facing messages.
---

Native Toncoin is represented inconsistently across the project and payment provider data: current records may use `TON`, while older records or user-facing copy may use `GRAM`. Treat both as the same native TON asset, not as a Jetton.

**Why:** A GRAM session routed to the Jetton endpoint can miss a valid native transfer, and a missing GRAM-to-TON rate alias can make an otherwise detected transfer impossible to credit.

**How to apply:** When adding or changing deposit parsing, preserve the saved/provider label for settlement and display where needed, but normalize TON/GRAM together for blockchain routing and USD conversion. Keep the processing notification label as GRAM when the deposit is native TON. The approved visible copy is `Processing payment Of 0.3572 GRAM` with the premium processing emoji.