---
name: agency
description: This skill should be used when the user asks to "start agency", "scan everything", "what's pending", "give me context", "go agency", "what needs my attention", "what should I do next", "check all my surfaces", "triage my inbox/slack/github", or any open-ended "go look at all my stuff and tell me what's up" request. Triggers a parallel multi-surface scan that reads email, Slack, GitHub, calendar, observability, and box state, pre-executes every reversible internal action, then returns a phone-readable list of one-tap cards where the work has already been done.
---

# Agency

Run a proactive, parallel multi-surface scan across the user's working surfaces, pre-execute every reversible internal action, and return a phone-readable list of one-tap cards. The user picks; the agent has already done the work.

**Read [`/opt/bux/agent/AGENCY.md`](file:///opt/bux/agent/AGENCY.md) end-to-end before composing any card.** That file is the canonical doctrine: two-zone framing, anti-patterns to refuse, the scan-orchestration process (parallel sub-agents per surface), card layout, button kinds, image-first rules, dedup, the acceptance-rate north-star, voice for drafts, telegram message rules, embargo handling, per-topic shape rules. Everything that used to live in this skill file has moved there so claude and codex share one source of truth.

This file is intentionally short — it exists to surface the trigger phrases to Claude Code's skill picker, and to point at the canonical doctrine. Don't grow it. If you find yourself wanting to add a rule, add it to `AGENCY.md` instead.
