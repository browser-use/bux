# Agency

How the bux Telegram bot reports back: scannable cards with action buttons,
persisted to a DB, dispatched into per-card forum topics. This file is the
canonical reference. Personal preferences (your voice, your team, your
filters) belong in private memory, not here.

## Architecture

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
                    • action  → run_task in (new or current) thread
                    • dismiss → 1-line ack, no dispatch
                    • refine  → "what would you change?" + wait
                    • custom  → synthesized [agency-button] dispatch
```

## Core principles

- **Surface DONE work, not forks.** A card says "I did X — commit?", never
  "Should I do X or Y?". Multi-option buttons only when each option is a
  different commitment ("post tweet only / linkedin only / all 3").
- **Card body is short.** Verb-led one-line action + one context sentence.
  Detailed framing belongs in expandables (collapsed) or in `--prompt`
  (only the worker agent sees that).
- **Always via `agency-report`.** Never raw `tg-send` for an agency card.
- **Dedup via `--source <slug> --skip-if-exists`.** Same signal → same
  slug → same row. If status ∈ {accepted, dismissed, regenerated, expired,
  completed} → skip. If pending >48h → implicit dismissal.
- **Drop low-priority cards silently.** Don't surface the "nothing today"
  message — go do something interesting instead.

## North-star metric: acceptance rate. Volume is anti-goal.

The single metric that matters: `(accepted + completed) / posted`. Every
other choice — title, length, image, urgency framing — is in service of
that. Posting 5 cards the user accepts beats posting 20 they ignore.

Each ignored card costs trust. Two ignored in a row = the user starts
skimming the channel. Five = they mute it. If you don't have a HIGH card
with a real impact angle this cycle, **post nothing**. Silence is better
than slop. "I have nothing high-impact to surface" is a valid scan result.

### Tie every card to the user's end-goal frame

The user's `magnus_endgoal.md`-shaped private memory holds their
top-level needles (canonical example: *startup successful · people know
it · people love it*). Each card's subhead must explicitly tie its
action to one of those needles, with a concrete number when possible:

- ❌ "submit to Smithery, virgin slot" *(so what?)*
- ✅ "+5K MCP devs/wk discover us → mindshare lift toward default-OSS-X"

If you can't write the subhead in that shape, the card isn't HIGH. Drop it.

Convince via specifics, not begging:

- A concrete number ("3.5K stars · maintainer ships PRs in 24h")
- A real competitor move ("X listed yesterday — first-mover slot is a 7-day window")
- A user-quote callback ("you said '2× faster than Y' in #general — back it publicly")
- A peer-network proof ("Z is YC W25 + warm investor path")

Don't add "please accept this!" lines. Neediness reads as weakness and
gets dismissed faster.

### Track signal, adapt over time

After each batch, query `agency.db` to see what landed:

```bash
sqlite3 /var/lib/bux/agency.db \
  "SELECT source, status, decision FROM suggestions WHERE id > <last-batch-start>"
```

Then:

- **Accepted repeatedly** → write more in that shape (impact framing,
  length, target type, urgency cue).
- **Ignored ≥48h** → that shape doesn't land. Don't repeat.
- **Regenerated** → user wants the underlying idea but framed
  differently (usually: more concrete, less speculative, lower-friction).
- **Dismissed (No-tap)** → active rejection. Save the rejection signal
  in a memory file (`feedback_agency_acceptance_signals.md` or
  user-equivalent) so future agents don't re-pitch.

### A/B test card formats — keep what wins

Vary one dimension at a time across consecutive batches:

- **Length**: 3-line cards · 5-line · collapsed-by-default expandable
- **Image**: `--image-text` · custom `--image-file` chart · no image
- **Subhead style**: number-first · urgency-first · proof-first · user-quote-callback
- **Draft shape**: paste-ready DM · PR diff · form fields · 1-line action
- **Tone**: terse · slightly conversational · founder-quote-led

Save observations per batch. Future agents read the signal file before
drafting and skew toward winning shapes.

### Talk WITH the user — ask occasionally, never spammy

Build the relationship over time. Periodically (≈once per 10–15 cards,
or after a noticeable acceptance shift) ask **one** lightweight question
that helps you draft better:

- "this week — more enterprise / OSS distribution / video lever?"
  *(buttons: enterprise · OSS · video)*
- "did the impact-first format land better than the older one?"
  *(yes · same · old was better)*
- "the bounty idea — interesting or noise right now?"
  *(interesting · noise · later)*

**Tone rules for question cards:**

- Sound like a curious co-worker, not a survey. Lowercase. No emoji-overload.
- One question, max ~15 words.
- Buttons that make answering a single tap. Open replies only when the
  answer is genuinely valuable to your work.
- Never ask twice in close succession. Never frame as "to serve you
  better" — that's salesperson voice.
- Never ask about things you can derive from existing context (profile,
  MEMORY.md, recent activity).

Question cards count toward the same north-star metric — if the user
answers, you've won; if they ignore, you've burned a slot. Make them
earn the post.

### Stop-doing list (when ignore-rate climbs)

If acceptance rate drops below ~30% across a 10-card batch:

1. Pause new posts for 24h.
2. Read what got dismissed/ignored. Identify the common shape.
3. Save the rejected pattern as a memory file ("don't post X-shaped
   cards — user consistently ignores them").
4. Resume with a different angle.

Don't fight disengagement with more volume. The fastest way to lose
the channel is to keep posting after the user stops engaging.

## Canonical card layout

```
[optional image — include whenever it speeds comprehension]
<emoji> <verb-led one-line action>
<one context sentence>

▾ 📝 Drafted action     (one expandable, when there's a draft)
▾ 📎 Context            (optional second expandable)

[primary action] [⏭ Skip]
[third button]          ← 🧵 Open thread, 📝 Edit, or 🔁 More variants
```

**Rules:**

1. **Title = verb-led action**: `Reply to <person> on Slack — explain
   v0.4.3 RC ETA`. Not `🤖 Agency #119 — wants help`.
2. **One context sentence** under the title. No bullets, no "## Why this
   matters" header. Prose.
3. **One expandable for the draft**, default `📝 Drafted action`. Don't
   label it "Variant A" unless B and C actually exist with buttons to pick.
4. **Optional `📎 Context`** for provenance / related threads / why this
   is distinct. Skip when nothing useful. Empty expandables are worse than
   no expandable.
5. **Buttons in a 2+1 grid.** Row 1 = primary + Skip. Row 2 = third
   button alone.
6. **Per-card-type tweaks override**:
   - PR / merge → primary expandable is the diff or PR link
   - Video / demo → MP4 is the surface; no drafted-text expandable
   - Status / FYI → sometimes no expandable at all is right
7. **Resist filling out a fixed schema.** Let card type drive shape.

## Image-first

Include an image whenever it speeds comprehension. Per card type:

- Person / outreach → small avatar
- Company → logo / favicon
- PR / merge → repo logo + PR number, or tiny diff snippet
- Plot / metric → real chart via `--image-file` (matplotlib)
- Video → the MP4 itself
- Status / FYI → large status emoji

`--image-text` is fine for short conceptual labels (≤6 words). Anything
longer wants a real chart or screenshot via `--image-file`. Skip only when
nothing useful would be there.

## Buttons

Default 3-button set, label adapts to spawn-mode:

| In-place | Spawn-topic | Kind |
|---|---|---|
| `✅ Yes` | `🧵 Yes (new thread)` | `action` |
| `⏭ Skip` | `⏭ Skip` | `dismiss` |
| `✏️ Edit` | `🧵 Edit (new thread)` | `refine` |

**Per-kind callback behavior:**

- `action` — record decision, dispatch `--prompt` via `run_task`
- `dismiss` — record decision, post `⏭ skipped` ack reply, **no** LLM
- `refine` — record decision, ensure worker topic, post the original card
  content as visible context messages, post "What would you change?", wait
  for the user's reply (no immediate dispatch)
- `custom` — `[agency-button] <label>` synthesized dispatch in the same
  topic; multi-tap is additive

**Smart labels** when the card isn't "approve a single drafted action":

- Three reply drafts → `🅰️ Send A` / `🅱️ Send B` / `🅲 Send C`
- Architectural choice → `Pick A` / `Pick B` / `Pick C`
- High-uncertainty draft → `✅ Send` / `🔁 More variants` / `⏭ Skip`

`--button` is a **plain string, not JSON**. Don't confuse it with `--block`
(which *does* take JSON). The helper has a defensive coercion for accidental
JSON, but write plain strings.

**Picked-button visual treatment** — bold uppercase + framing arrows:

| Default | After tap |
|---|---|
| `✅ Yes` | `▶ ✅ 𝗬𝗘𝗦 ◀` |
| `Send draft A` | `▶ 𝗦𝗘𝗡𝗗 𝗗𝗥𝗔𝗙𝗧 𝗔 ◀` |

The keyboard is **not** stripped after a tap — buttons stay visible and
re-tappable so the user can change their mind. Default kinds reset prior
picked styling on re-tap; custom buttons stay additive.

## Yes-tap routing — auto-default by thread context

`agency-report` infers `--spawn-topic` automatically:

- Thread is already a `worker_topic` for some prior card → in-place
  (the agent is deep in one task; don't fork another)
- Otherwise (main agency feed, fresh chat) → spawn fresh forum topic

Backed by `agency_db.is_worker_topic(thread_id)`. Override with
`--spawn-topic` / `--no-spawn-topic` when the auto-detect is wrong.

**Multi-tap dedupes the worker topic.** Tapping Yes twice doesn't spawn
two topics; subsequent action/refine taps reuse the first `worker_topic_id`.

**Deep-link glued to the card.** When work runs in a different thread,
the bot appends a `🧵 Open thread` URL row to the card's own keyboard so
the link survives no matter how many newer cards land below.

## Spawned-topic UX

For `kind=action`:

1. `createForumTopic` named after the suggestion title.
2. Post the original `--prompt` as a visible header in the new topic
   (rendered as `<blockquote>`, not `<pre>` — the `<pre>` widget's "copy"
   affordance reads as visual noise on phone).
3. `run_task` to fire the lane.
4. Append `🧵 Open thread` URL row to the original card's keyboard.

For `kind=refine`:

1. Same `createForumTopic` (or reuse existing worker topic).
2. Post the original card content (title + context + draft) as visible
   messages so the user sees what they're refining.
3. Post `"👇 What would you change?"`.
4. Do **not** dispatch — the agent fires only when the user replies.

On the user's first reply in a refine thread, `run_task` looks up the
suggestion via `agency_db.find_by_worker_topic` and prepends the original
card's title + description + prompt to the user's message before
dispatching. So the worker agent re-drafts with the original in scope.

(No file-based context cache. The DB already has all the data; querying
it on the user's first reply is one SELECT and avoids a separate
state-tracking surface.)

## Telegram message rules

- **2-second-scannable on phone.** Lead with verdict / headline; details
  below.
- **Bold via `*single asterisk*` (MDV2)** or `**double**` (the bot
  converts). No `#` / `##` headings — they render as literal `\#\#`.
- **Send images often** — tables, briefs, status grids, comparisons,
  timelines render as PNG, not fenced-block tables.
- **No VM paths in TG.** Phone-first means clickable from the phone. Short
  doc → inline; meaningful doc → `sendDocument` attachment.
- **≤3500 chars per message.** TG drops oversized messages silently. If a
  reply must exceed, split into sequential messages. Never compress by
  stripping content.

## Helper API: `agency-report`

**Required:**

- `--title` — verb-led one-liner.
- `--prompt` — required when the card uses default buttons (not
  `--info-only`, not `--button`). It's the literal action the agent runs
  on Yes-tap. Without it the helper rejects the post.

**Layout fields:**

- `--emoji` / `--source-label` / `--source-url` / `--subhead`
- `--image` (URL) / `--image-file` (local path) / `--image-text` (auto
  placehold.co)
- `--draft` / `--reasoning` — first / second expandable
- `--block '<JSON>'` (repeatable) — variable-count expandables. JSON:
  `{"emoji": "…", "title": "…", "body": "…", "body_html": bool}`.
  Overrides `--draft` / `--reasoning`.
- `--button "<label>"` (repeatable) — custom buttons. Plain string.
- `--info-only` — drop the keyboard entirely.
- `--spawn-topic` / `--no-spawn-topic` — override the auto-default.
- `--source <slug>` — dedup key.
- `--skip-if-exists` — suppress if a non-pending row exists for the slug.

**HTML escaping:** free-text fields are HTML-escaped by default. For raw
HTML, use `--<field>-html` (e.g. `--draft-html '<code>...</code>'`).

**Long-body fallback:** if the body exceeds Telegram's 1024-char caption
cap, the helper falls back from `sendPhoto` to `sendMessage` +
`link_preview_options`. Visually identical, no length cap.

## DB schema

`/var/lib/bux/agency.db`. One row per suggestion. Schema in
`agency_db.py:init_schema`.

Fields used at runtime: `title`, `description`, `prompt`, `buttons_json`,
`tg_chat_id`, `tg_thread_id`, `tg_message_id`, `status`, `decision`,
`worker_topic_id`, `spawn_topic`.

Public helpers in `agency_db.py`:

- `conn()` — open + init.
- `insert(...)` → suggestion id.
- `update_message(suggestion_id, message_id)`.
- `find_by_message(chat_id, message_id) -> dict | None`.
- `find_by_worker_topic(thread_id) -> dict | None` — used by `run_task`
  to inject refine context.
- `record_decision(chat_id, message_id, decision)` — sets decision +
  derived status.
- `set_worker_topic(suggestion_id, worker_topic_id)`.
- `set_status(suggestion_id, status, completed_at=None)`.
- `exists(source) -> dict | None` — backs `--skip-if-exists`.
- `is_worker_topic(thread_id) -> bool` — backs the auto-default for
  `--spawn-topic`.

## Bot restart safe pattern

Don't call raw `systemctl restart bux-tg` from inside an active agent
turn — it kills the bot tree (and the agent process), so the user's
final summary never lands.

Use `bux-restart` (the wrapper). It records the lane in
`/var/lib/bux/update-request.lanes` so the post-boot announce sends a
"✅ back online (sha=…)" ping into the same lane.

For the agent's final summary to also land:
```bash
echo "summary…" | tg-send && bux-restart
```

`tg-send` hits the TG API directly, so the message lands regardless of
whether the agent dies right after.

## Safety: never fabricate

When iterating on agency-card layouts or testing the helper, cards
posted to live forum topics must NOT contain plausible-looking
fabricated content. The user reads cards as real signals.

**Banned in live cards:** real-sounding person names tied to fabricated
quotes; fabricated ARR / version / ETA / retry-rate claims; anything
that pattern-matches a real customer ping but isn't.

**For demos:** use obvious placeholders (`<placeholder name>`,
`<placeholder company>`, LOREM-IPSUM). Source slug must contain `demo`
or `template-test`. Better yet: post into a private demo topic, not the
live agency queue.

Real Slack / Gmail search before referencing a real customer name. If
the search returns no match, the person doesn't exist — stop.

## Where things live

| Surface | Purpose |
|---|---|
| `agent/AGENCY.md` *(this file)* | Generic mechanics |
| `~/.claude/projects/<…>/memory/` | Personal preferences (private) |
| `agent/agency-report` | CLI helper — posts cards, validates inputs |
| `agent/agency_db.py` | SQLite store + public helpers |
| `agent/telegram_bot.py` | Callback handler + lane dispatch |
| `/var/lib/bux/agency.db` | Per-suggestion ledger |
