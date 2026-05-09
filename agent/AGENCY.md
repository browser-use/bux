# Agency

The bux Telegram bot's "agency" loop is how an agent reports back: **scannable
cards with action buttons, persisted to a DB, dispatched into per-card forum
topics**. This doc is the canonical reference for how cards must look, what
buttons mean, and how the helper + bot wire it all together.

It's a starting point. Personal preferences (your voice, your team, your
specific filters) belong in private memory, not here. This file describes
the *mechanics* — same shape for everyone running bux.

---

## Architecture in one paragraph

A sub-agent (or the `/agency` loop) calls `agency-report` with the suggestion
content. The helper writes a row to `/var/lib/bux/agency.db` and posts a
Telegram message with an inline keyboard. When the user taps a button, the
bot's `_handle_agency_callback` records the decision in the DB and either
dispatches the action prompt to a fresh forum topic, runs it in-place, or
no-ops (Skip). The DB is the source of truth for dedup, status, and which
forum topic owns the work.

```
agent → agency-report → agency.db + TG card
                              │
                              ▼
                         user taps button
                              │
                              ▼
                bot._handle_agency_callback
                  ├─ records decision (DB)
                  ├─ marks picked button visually
                  └─ routes per kind:
                      • action → run_task in (new or current) thread
                      • dismiss → 1-line ack, no dispatch
                      • refine → "what would you change?" + wait
                      • custom → synthesized [agency-button] dispatch
```

---

## Core principles

**Surface DONE work, not forks.** A card says "I did X — commit?", never
"Should I do X or Y?". Do the scan/draft/lookup *before* materializing the
card; the card carries the result. Multi-option buttons only when each
option is a different commitment ("post tweet only / linkedin only / all 3").
Never multiple-choice for "what should I do next".

**Card body is short.** Verb-led one-line action + one context sentence.
Detailed framing belongs in the expandables (collapsed by default) or in the
`--prompt` (only the worker agent sees that). Phone-screen first.

**Always via `agency-report`.** Never raw `tg-send` or `sendMessage` for an
agency card. The helper enforces shape, writes to the DB, and lets the bot's
callback handler track decisions.

**Dedup via `--source <stable-slug> --skip-if-exists`.** Same underlying
signal → same slug → same row → no duplicate posts. Slug examples:
`slack-c-foo`, `gmail-thread-19df00477868154d`, `gh-pr-78`,
`signup-name-domain`. If a row exists with status ∈ {`accepted`, `dismissed`,
`regenerated`, `expired`, `completed`} → skip. If `pending` for >48h → treat
as implicit dismissal.

**Drop lows silently.** Don't tell the user you skipped a low-priority card
— that defeats the purpose. If after filtering you have zero high/med items,
do something genuinely interesting instead.

---

## Canonical card layout

Locked shape — every card uses it unless a per-card-type tweak (below) calls
for something different.

```
[optional image — include whenever it speeds comprehension]
<emoji> <verb-led one-line action>
<one context sentence>

▾ 📝 Drafted action       (one expandable, when there's a draft)
▾ 📎 Context              (optional second expandable)

[primary action] [⏭ Skip]
[third button]            ← 🧵 Open thread, or 📝 Edit / pick B|C, or
                            🔁 More variants when there's high uncertainty
```

### The seven rules

1. **Title = verb-led action**, no rigid prefix. Write
   `Reply to <person> on Slack — explain v0.4.3 RC ETA`, not
   `🤖 Agency #119 — YC F25 wants LinkedIn anti-bot help`. From the title
   alone the user should know what to do.

2. **One context sentence** under the title. Why is this in the queue, why
   it's waiting on you. No "## Why this matters" header, no rigid bullets —
   just prose.

3. **One expandable for the draft.** Title default `📝 Drafted action` (or
   `📝 Drafted reply` / `📝 Drafted DM` / `📝 Drafted SQL`). **Don't label
   it "Variant A" unless B and C actually exist** with buttons to pick —
   inventing variant labels with no alternatives is a trust failure.

4. **Optional second expandable — `📎 Context`** — for provenance, related
   threads, or distinct-from-prior-cards information that supports the
   decision but isn't the action itself. Skip when there's nothing useful.
   Empty expandables are worse than no expandable.

5. **Buttons in a 2+1 grid.** Row 1 = primary action + Skip. Row 2 = third
   button alone — `🧵 Open thread` (URL) for already-spawned topics,
   `📝 Edit / pick B|C` when alternatives are ready, or `🔁 More variants`
   when uncertainty is high.

6. **Per-card-type tweaks override the general shape:**
   - *PR / merge cards* — primary expandable is the diff or PR link, not a
     drafted message.
   - *Video / demo cards* — the MP4 itself is the surface; no drafted-text
     expandable.
   - *Status / FYI cards* — sometimes no expandable at all is right. The
     headline + the one context line is the whole card.

7. **Resist filling out a fixed schema for every card.** Let the card type
   drive the shape. Forced uniformity reads as templated noise.

---

## Image-first rule

The image is the fastest path to "I know what this card is about" before
reading any text. **Default to include, not omit.** The image, title, and
context line are read in that order — most of the time the image alone
should hint at the topic.

Per-card-type images:

- **Person / outreach card** — small avatar (initials, or a real headshot
  scraped via the browser harness). Lets the user spot "Reply to <person>"
  by face before reading.
- **Company / customer card** — company logo or favicon, or a small
  "logo + key metric" composite.
- **PR / merge card** — repo logo + PR number, or a tiny diff snippet
  rendered as image.
- **Plot / metric card** — render the actual chart with matplotlib
  (`--image-file`). The chart *is* the content.
- **Video / demo card** — the MP4 itself is the surface; no card-image.
- **Status / FYI card** — large status emoji (`🟢 deploy green`,
  `🐛 bug spike`).

Skip only when nothing useful would be there. Push hard: most cards have
*something* worth visualizing. The placehold.co fallback (`--image-text`)
is fine for short conceptual labels (≤6 words). Anything longer wants a
real chart or screenshot via `--image-file`.

---

## Buttons + the four kinds

The default 3-button set:

| Label (in-place mode) | Label (spawn-topic mode) | Kind |
|---|---|---|
| `✅ Yes` | `🧵 Yes (new thread)` | `action` |
| `⏭ Skip` | `⏭ Skip` | `dismiss` |
| `✏️ Edit` | `🧵 Edit (new thread)` | `refine` |

The bot's callback handler routes per kind:

- **`action`** — record decision, strip prior picked styling, dispatch the
  card's `--prompt` via `run_task`. If `spawn_topic=True`, create a fresh
  forum topic first and dispatch there; otherwise dispatch in the same
  thread the card lives in.
- **`dismiss`** — record decision, post a one-line `⏭ skipped` ack reply.
  No LLM dispatch. The cheapest interaction.
- **`refine`** — record decision, spawn the worker topic (or use the
  current one), display the original card content as visible context
  messages, post `"What would you change?"`, persist the context for
  injection on the user's first reply, and *don't* dispatch (the agent
  fires only when the user replies).
- **`custom`** — for `--button` overrides, dispatch a synthesized
  `[agency-button] <label>` message into the same topic. Multi-tap is
  additive (variant pickers like "Send draft A" + "Send draft B" both
  stick).

### Smart button labels

When the card is shaped differently than "approve a single drafted action",
**rename the buttons to match the action**, don't force the default labels:

- Single drafted reply → `✅ Send` / `⏭ Skip` / `✏️ Edit`
- Three reply drafts → `🅰️ Send A` / `🅱️ Send B` / `🅲 Send C`
- Architectural choice → `🅰️ Pick A` / `🅱️ Pick B` / `🅲 Pick C`
- High-uncertainty draft → `✅ Send` / `🔁 More variants` / `⏭ Skip`
- One unclear idea → `✅ Use it` / `🔁 Generate another` / `⏭ Skip`
- Decision with no draft yet → `🤔 Brainstorm options` / `⏭ Skip`

The agent picks labels that read as the *literal action* the user is taking,
not as "Yes / No". `🔁 More variants` always trumps a generic "Regenerate".

### `--button` is a plain string, not JSON

Don't confuse it with `--block` (which *does* take JSON). Every `--button`
value should be the visible button label, nothing else:

```bash
# correct
agency-report --button "❌ No" --button "✅ Send" --button "📝 Edit"

# wrong — buttons render as the literal JSON blob in TG
agency-report --button '{"text":"❌ No","value":"no"}'
```

The helper has a defensive coercion (`{"text": "❌ No"}` JSON unwraps to
`❌ No`), but rely on plain strings.

### Picked-button visual treatment

Tapped buttons get marked with **bold uppercase + framing arrows**:

| Default | After tap |
|---|---|
| `✅ Yes` | `▶ ✅ 𝗬𝗘𝗦 ◀` |
| `⏭ Skip` | `▶ ⏭ 𝗦𝗞𝗜𝗣 ◀` |
| `Send draft A` | `▶ 𝗦𝗘𝗡𝗗 𝗗𝗥𝗔𝗙𝗧 𝗔 ◀` |

Bold via Mathematical Sans-Serif Bold (U+1D5D4 onward). Implemented in
`_agency_mark_picked` in `telegram_bot.py`. Default kinds reset the picked
styling from siblings on re-tap so only the latest pick is highlighted.
Custom buttons stay additive.

The keyboard is **not** stripped after a tap — buttons stay visible and
re-tappable so the user can change their mind. The visual mark on the
button itself is the at-a-glance "what I picked" signal; no separate
emoji reaction needed.

---

## Yes-tap routing — auto-default by thread context

`agency-report` infers the right `--spawn-topic` default from where the
helper is being called from:

- **Posting from a thread that's already a `worker_topic`** for some prior
  card → default `--no-spawn-topic` (in-place). The agent is already deep
  in one task; follow-up cards keep the conversation in the same thread.
- **Posting from anywhere else** (the main agency feed, a fresh chat) →
  default `--spawn-topic` (each card gets its own forum topic).

Implemented via `agency_db.is_worker_topic(thread_id)` — True iff some
prior suggestion has `worker_topic_id == thread_id` AND `tg_thread_id !=
worker_topic_id` (excludes in-place bookkeeping).

### Override knobs

- `--spawn-topic` — force spawn (always create a new topic on Yes/Edit).
- `--no-spawn-topic` — force in-place (always run in the current thread).

You don't normally need either — the auto-default does the right thing.
Reach for the explicit flags only when the auto-detected default is wrong.

### Multi-tap dedupes the worker topic

Tapping Yes twice doesn't spawn two topics. The first tap sets
`worker_topic_id` in the DB; subsequent action/refine taps reuse it and
re-dispatch into the same thread.

### Deep-link glued to the card

When a fresh topic gets spawned, the bot appends a `🧵 Open thread`
URL-button row to the card's *own* keyboard — not as a separate "→ working
in X" reply that gets buried as more cards land. The link scrolls with
the card; tap it from anywhere later to jump to the worker topic.

---

## Spawned-topic UX

When `kind=action` spawns a fresh topic, the bot:

1. `createForumTopic` named after the suggestion title (truncated to 128
   chars).
2. Posts the original `--prompt` as a visible header in the new topic
   (rendered as a `<blockquote>`, not `<pre>` — the `<pre>` widget's
   "copy" affordance reads as visual noise on phone). Without this, only
   the agent's response surfaces in TG and the user has no record of what
   the agent was asked to do.
3. Calls `run_task((chat_id, new_thread_id), prompt, ...)` to fire the
   lane.
4. Appends a `🧵 Open thread` URL row to the original card's keyboard.

When `kind=refine` spawns a fresh topic:

1. Same `createForumTopic`.
2. Posts the original card content (title + context + draft) as visible
   messages so the user sees what they're refining.
3. Persists the same content (plain text) to
   `/var/lib/bux/agency-refine-context/<thread_id>.txt`. This is read +
   deleted by `run_task` on the user's first reply, prepended to their
   prompt so the worker agent has the original card in scope.
4. Posts `"👇 What would you change?"` as the next visible message.
5. Does **not** dispatch — the agent fires only when the user replies.

---

## Telegram message rules

These apply to every TG message the bot sends, agency or otherwise.

**Visual structure**

- Emoji prefixes on the headline and on each major section (📌 brief,
  🚨 risk, ✅ done, 🟡 open, 📅 meeting, 👥 people, 🎯 goal, 🔗 link,
  📎 attachment).
- Bold via `*single asterisk*` (MarkdownV2) or `**double**` (the bot
  converts) — verify it renders, never let `**` leak as literal text.
- No `#` / `##` / `###` heading lines — they render as literal `\#\#`
  after MDV2 escaping.
- Lead with the headline / verdict; details below.

**Send images often**

- Tables, briefs, status grids, comparisons, timelines → render as PNG and
  attach via the bot's photo path. Don't paste a fenced-block table.
- Quick sketches, hand-drawn-style diagrams beat the prose equivalent.
- Default: when an agency card or a brief reply contains *any* tabular or
  comparative data, render an image.

**No VM paths in TG**

- Never reference `/home/<user>/...` paths as if clickable — phone-first
  means the user is rarely at the machine.
- Short doc (<4 KB) → inline as fenced block or render as image.
- Meaningful doc → real Telegram file attachment via `sendDocument`.
- "Saved on the box at X" goes *after* inlined content, never as the
  primary delivery.

**4096-char hard limit**

- Telegram drops oversized messages silently. Aim for ≤3500 chars per
  message to leave headroom for MDV2 escaping.
- If a reply must exceed that, split into multiple sequential messages
  (1/3, 2/3, …, or naturally chunked sections). Never compress by
  stripping content.

---

## Helper API: `agency-report`

Required:

- `--title` — the verb-led one-liner.

Required when default buttons are used (not `--button`, not `--info-only`):

- `--prompt` — the literal action that runs when the user taps Yes/Edit.
  Without it, the bot's fallback dispatches the button label itself
  ("`🧵 Yes (new thread)`") as the prompt, which is useless. The helper
  rejects a default-button card with no prompt at post time.

Layout fields:

- `--emoji` — prefixed to the title.
- `--source-label` / `--source-url` — small clickable provenance link at
  the end of the body.
- `--subhead` — optional one-line subhead under the title.
- `--image` / `--image-file` / `--image-text` — image; first = direct URL,
  second = local file (multipart upload), third = auto-rendered
  placehold.co text card.
- `--draft` / `--draft-title` / `--draft-emoji` — first expandable.
  Defaults: `📝 Drafted action`.
- `--reasoning` / `--reasoning-title` / `--reasoning-emoji` — second
  expandable. Defaults: `📎 Context`.
- `--block '<JSON>'` (repeatable) — variable-count expandables. Each
  `--block` becomes one expandable. JSON shape:
  `{"emoji": "…", "title": "…", "body": "…", "body_html": bool}`. When
  any `--block` is given, `--draft` / `--reasoning` are ignored.
- `--button "<label>"` (repeatable) — custom button labels. Each gets
  `kind=custom`. Plain string, not JSON.
- `--info-only` — drop the inline keyboard entirely (FYI cards). Mutually
  exclusive with `--button`.
- `--spawn-topic` / `--no-spawn-topic` — override the auto-default.
- `--source <slug>` — stable dedup key. Combine with `--skip-if-exists` to
  suppress repeats.
- `--importance high|med|low` — triage bucket. Lows are dropped silently.

HTML escaping:

- Free-text fields (`title`, `subhead`, `draft`, `reasoning`, `source-label`)
  are HTML-escaped by default.
- For raw HTML in any field, use the `--<field>-html` long form (e.g.
  `--draft-html '<code>...</code>'`).

Long-body fallback:

- If the body exceeds Telegram's 1024-char caption cap, the helper
  falls back from `sendPhoto` to `sendMessage` + `link_preview_options`
  with the image displayed large above the text. Visually identical to
  a captioned photo, no length cap.

---

## DB schema (`agency.db`)

One row per suggestion, in `/var/lib/bux/agency.db`. Schema lives in
`agency_db.py`'s `init_schema`.

| Column | Notes |
|---|---|
| `id` | autoinc PK |
| `title`, `description`, `prompt`, `buttons_json` | card content |
| `importance` | high/med/low |
| `source` | dedup slug |
| `tg_chat_id`, `tg_thread_id`, `tg_message_id` | TG addressing |
| `status` | pending / accepted / dismissed / differently / regenerated / expired / completed / failed |
| `decision` | the literal label tapped |
| `decision_at` | unix timestamp |
| `worker_topic_id` | the thread the lane runs in (set after Yes/Edit) |
| `worker_started_at`, `worker_completed_at` | optional |
| `spawn_topic` | 0/1 — does Yes-tap fork a new topic? |
| `created_at`, `updated_at` | unix timestamps |

Public helpers in `agency_db.py`:

- `conn()` — open + init.
- `insert(...)` → `int` — new suggestion.
- `update_message(suggestion_id, message_id)` — wire the TG message id
  back so callbacks can find the row.
- `find_by_message(chat_id, message_id) -> dict | None` — used by the
  callback handler.
- `record_decision(chat_id, message_id, decision)` — sets `decision`,
  `decision_at`, and a derived `status`.
- `set_worker_topic(suggestion_id, worker_topic_id)`.
- `set_status(suggestion_id, status, completed_at=None)`.
- `exists(source) -> dict | None` — used by `--skip-if-exists`.
- `is_worker_topic(thread_id) -> bool` — used to auto-default
  `--spawn-topic`.
- `search(query, limit=10)` — fuzzy LIKE over title + description.

---

## Spinning up a new topic with a task

When the user asks to "spin up a new topic with task X" (or "create a
new topic and start working on Y"):

1. **Create a TG forum topic** via Bot API:
   ```bash
   curl -sS "https://api.telegram.org/bot${TG_BOT_TOKEN}/createForumTopic" \
     -d "chat_id=${TG_CHAT_ID}" \
     --data-urlencode "name=<emoji + ≤30-char summary>" \
     -d "icon_color=<color>"
   ```

2. **Post the task as a clearly-formatted visible message** in the new
   topic via `tg-send` so it's unambiguously the initial prompt:
   ```bash
   TG_THREAD_ID=<new_thread_id> tg-send "📝 *Initial prompt:*

   <exact task text>"
   ```

3. **Dispatch a fresh agent** via `bot.run_task` so the lane starts
   immediately:
   ```python
   bot.run_task((chat_id, new_thread_id), task_text,
                reply_to=None, sender={...})
   ```

4. **Reply briefly in the original topic** confirming the new topic was
   created and the agent is working — don't do the task yourself in the
   original lane.

The user's original task text should land verbatim as the visible message;
don't rephrase it.

---

## Bot restart safe pattern

Restarting the bot during an active agent turn is dangerous: the systemd
SIGTERM kills the bot tree, which kills the in-flight agent process,
mid-stream. The user's reasoning/thinking already streamed but the
consolidated final summary never lands.

**Use `bux-restart`, not raw `systemctl restart bux-tg`.** The wrapper
records the lane in `/var/lib/bux/update-request.lanes` so the post-boot
announce sends a "✅ back online (sha=…)" ping into the same lane the user
was talking to.

For the agent's final summary to also land:

```bash
echo "summary…" | tg-send && bux-restart
```

`tg-send` hits the TG API directly — the message lands regardless of
whether the agent dies right after.

---

## Safety: never fabricate

When iterating on agency-card layouts or testing the helper, cards posted
to live forum topics must NOT contain plausible-looking fabricated content.
The user reads cards as real signals and acts on them.

**Banned in live cards:**

- Real-sounding person names tied to fabricated quotes.
- Real customer names with fake ARR / cap-table claims.
- Fabricated version numbers, ETAs, fix-merged claims, retry rates,
  percentages, dollar figures.
- Anything that pattern-matches a real customer ping but isn't.

**If you must demo a layout in the live queue:**

- Use obvious placeholders: `<demo headline>`, `<placeholder name>`,
  `<placeholder company>`, or LOREM-IPSUM filler.
- Source slug must contain `demo` or `template-test` so the dedup layer
  can spot and drop it.
- Better: post into a private demo topic, NOT the live agency queue.

Real Slack / Gmail search before referencing a real customer name. If the
search returns no match, the person doesn't exist — stop.

---

## Embargo handling

When a task brief implies announcement copy (X / Bluesky / LinkedIn / blog)
AND the source material (email, doc, ticket) contains words like *embargo*,
*confidential*, *do not share until*, *hold until*, or a future *go-live*
date — STOP before drafting. Reply with:

> "Embargo doesn't lift until X. Drafting now means we won't have the real
> public URL or the real public framing — both shift the copy. Want me to
> schedule a re-spin for ~15 min after the embargo lifts, or do you actually
> want speculative drafts now?"

Treat a written brief as a starting point, not a binding instruction — if
it conflicts with information in the source material, surface the conflict
before executing.

---

## What lives where

| Surface | Purpose | File |
|---|---|---|
| Public skill (this file) | Generic mechanics, card shape, button kinds, helper API, DB schema, Telegram rules | `agent/AGENCY.md` |
| Private memory | The user's specific preferences, voice, contacts, company facts, current directives | `~/.claude/projects/<project>/memory/` |
| CLI helper | Posts cards, validates inputs, writes DB | `agent/agency-report` |
| DB module | SQLite store, public helpers | `agent/agency_db.py` |
| Bot wiring | Callback handler, lane dispatch | `agent/telegram_bot.py` |
| DB | Per-suggestion ledger | `/var/lib/bux/agency.db` |
| Refine context | Per-thread context files (one-shot) | `/var/lib/bux/agency-refine-context/` |

The skill describes how the system works. Personal preferences (whose
messages the agent should/shouldn't surface, the user's voice for drafts,
specific people/companies/tools in their world) belong in private memory
files only — they're not part of the public mechanic.
