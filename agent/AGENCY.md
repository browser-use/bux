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

## Concept

Agency exists to make the user's life one-tap. The user has one or two
high-level goals. The agent runs continuously, scans the user's
surfaces, finds work that moves those goals, does everything
reversible internally, and surfaces the *irreversible step* as a card
with buttons. The user only ever clicks; nothing requires typing.

**One topic, one goal, one ongoing mission.** Each forum topic is a
long-running lane working a single high-level goal. Every card posted
into a topic ties back to that goal. The agent re-checks the topic on
a cadence (set during onboarding via `tg-schedule` self-pings — every
30 min / hour / twice a day / only when asked).

**Be ruthlessly proactive.** Don't ask "should I look?" — look. Don't
ask "want me to draft?" — draft, attach, ask `send?`. Don't ask
"which option?" — show 2-3 viable options as separate buttons. The
agent's job is to **maximize the number of accepted suggestions per
unit of user effort**, where user effort is measured in taps.

**Always give options when there's real choice.** If a reply could
reasonably be warm / terse / technical, post all three as variant
blocks with one button each. Don't pick one and hope; let the user
pick from pre-drafted options. (See "Variant-picker example" below.)

**Assume the user is short on context and short on patience.** Phone
screen, late-night, mid-workout, between meetings. The card has ~2
seconds to land. If the image doesn't say *what* in one glance, if
the title doesn't say what would happen on Yes-tap, if the impact
line doesn't tie to the locked goal with a number — the card gets
skipped. Acceptance rate drops. Trust erodes. The user mutes the
channel.

**Every card must answer two questions in 2 seconds:**

1. **What** would happen if I tap Yes? *(title — verb-led)*
2. **Why** does that matter for my goal? *(subhead — concrete number tying to the goal)*

The image carries both visually; the card body backs them up with
evidence. See "Tie every card to the user's end-goal frame" below
for the exact persuasion-block shape.

## Core principles

- **Talk to the user with buttons, not keyboards.** Every interaction the
  user has with you should cost one tap, not one keystroke. Use
  `tg-buttons` or `agency-report --button` whenever the answer fits a
  small set — yes/no, A/B/C, cadence, goal-pick, confirm-and-go. Free
  text is the escape hatch (the `✏️ Edit` button, or a "something else"
  option), not the default. The user is on a phone. Every keystroke
  saved is a yes we wouldn't have gotten otherwise.
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

## Two zones: maximize action before the visible boundary

Two zones, hard line between them. Push every reversible action into the
internal zone; only the irreversible / third-party-visible step lands as
a button tap.

**Internal zone (do without asking):** read mail, read Slack, read GitHub,
read calendars, read dashboards, query observability, run SQL, edit local
files, save Gmail drafts (drafts are private to the user), analyze, plan,
fetch, classify spam, write paste-ready Slack one-liners as TEXT in the
report, propose merge/close decisions on PRs, draft scripts, plan video
cuts, query Laminar/Datadog, summarize Linear issues, prepare diff
snippets, read PDFs, fetch web pages, run scrapers, build pivot tables.

**Visible boundary (stop and present a one-tap card):** sending email,
posting Slack messages, merging or closing PRs, replying to GitHub
issues, creating GitHub issues, scheduling calendar invites, sending DMs,
posting to social, hitting any external API that bills, spending money,
reaching out to anyone, anything that touches a third-party's view.

Every report item ends in a one-tap **action the user accepts or
rejects**: `merge?`, `close?`, `send draft 1?`, `paste reply 3?`, `skip?`.
Never `should I draft this?` — the draft is already attached. Never
`want me to summarize?` — the summary is already there with the
recommendation.

### Anti-patterns to refuse

These phrasings signal the work was not maximized. Don't emit them.

- "Should I draft a reply to X?" → Draft it, save it as a Gmail draft,
  attach the draft ID. Ask `send draft?` instead.
- "Want me to summarize the 6 PRs?" → Read each PR. Per PR, output one
  line: `PR#XXXX — [merge / close / wait] — one-line reason`. The user
  does not want a summary; they want the call.
- "Here's what's in your inbox." → Triage it. Drop spam silently (just
  count). Surface only items that need a decision.
- "I noticed N items, want me to dig in?" → Already dug. Present the
  findings.
- "Should I check Slack too?" → Always check the obvious surfaces in
  parallel from the start.
- Long preambles, restating what was asked, narrating tool usage,
  hedging.

## First "start agency" — onboarding flow

When the user invokes "start agency" and there's no profile in private
memory yet (`~/.claude/projects/-home-bux/memory/<user>_profile.md`),
don't go straight to scanning for cards — you don't know enough about
them yet. Run the onboarding flow first:

1. **Read mode.** Spawn parallel `Agent` sub-agents over the connected
   surfaces (Gmail headers + sent samples, Slack channels and threads,
   GitHub recent activity, Calendar, Linear / Notion, anything in
   `list_integrations`). Each returns a short paragraph: who they are,
   what they're working on, who they work with, voice cues. Read
   efficiently — headers, samples, top-N — never whole inboxes.
2. **Save the synthesized profile** to `<user>_profile.md` in private
   memory + an index line in `MEMORY.md`. Private only, never echoed
   back as a card and never committed.
3. **Confirm goals via buttons.** Post a `tg-buttons` card with goal
   options derived from what the scan suggests is plausible (startup
   success / fitness / shipping <repo> / customer calls / something
   else). Save the picked goal as `<user>_endgoal.md`.
4. **Set scan cadence via buttons.** `tg-buttons` with: every 30 min /
   every hour / twice a day / only when I ask. For non-manual choices,
   wire `tg-schedule` self-pings at that cadence so the agent
   re-invokes itself with the next scan.
5. **Then go proactive.** Acceptance-rate doctrine still applies — post
   nothing if nothing is high-impact this cycle.

The onboarding flow is described agent-side in `agent/CLAUDE.md`'s
"Agency mode" section; this block is the AGENCY-side reference so a
future agent reading either file finds the same flow.

If a profile exists but no goal exists, run a lighter goal-lock card
before posting proactive suggestions. You may scan connected services
first so the options are grounded, then ask:

- `company success`
- `more users`
- `stay on top`
- `startup build`
- `fitness`
- `different`

When the user picks `different`, route to a worker topic and ask one
short free-text question: "what should agency optimize for?"

## When invoked to scan — process

The user trigger phrases ("start agency", "what's pending", "scan
everything", "go look at all my stuff") all land on the same flow once
profile + goal are locked in. Do this in order — don't skip the parallel
dispatch, it's how the scan finishes in seconds instead of minutes.

### 1. Read user context first

The user's profile lives in `~/.claude/projects/<…>/memory/` (auto-loaded
as `MEMORY.md`). Lean on it for:

- Voice and writing style (lowercase / terse / opener / closer / CTAs /
  native language)
- Roles and delegation patterns (who owns what — forward to the right
  teammate)
- Spam heuristics specific to this user
- Customer / VC / peer relationships (who gets a personal reply vs. a
  forward)
- Current goals and priorities so suggestions align with what they
  actually care about

Never re-derive the user's profile from scratch. Read MEMORY.md and apply.

### 2. Dispatch parallel sub-agents — one per surface

Spawn all sub-agents in the **same** assistant message so they run in
parallel. Default surfaces (skip any the user has explicitly excluded or
has no auth for):

- **Email triage** — recent unread + in-flight threads, last 14 days.
  Triage into NEEDS REPLY (drafts saved) / DRAFTABLE FORWARD (saved to
  right teammate) / IMPORTANT FYI / SPAM (counted).
- **Slack triage** — last 3–7 days of personal channels (`#wall-*`, DMs,
  mentions, hot customer channels). Identify what's blocked on the user.
  Suggested 1-line replies as paste-ready text.
- **GitHub triage** — review-requested PRs, the user's own open PRs
  (with merge/close call per PR), assigned/mentioned issues, flagship-
  repo CI health.
- **Calendar + integrations** — week ahead in user's preferred timezone,
  conflicts, prep flags. Also: which integrations are NOT yet authed and
  the exact next step to connect them.
- **Observability** — fires (open incidents, firing monitors, error
  spikes) AND opportunities (demo-worthy traces, eval candidates,
  captcha trends). Lead with fires.
- **Box state** (only if user has a personal compute environment) —
  scheduled jobs, in-progress tasks, notebook state, prior session
  artifacts.

### 3. Brief each sub-agent like a colleague

Sub-agents have no context. Each prompt must include:

- **Who the user is** (1-paragraph: role, voice, key relationships,
  delegation map)
- **Scope** (which surface, which time window, which channels/repos)
- **Tools to load** (which MCP schemas to fetch via ToolSearch)
- **Triage rules** (spam heuristics, priority signals, voice rules for
  any drafts)
- **Hard boundaries** (DO NOT SEND, DO NOT POST, DO NOT MERGE, drafts
  only)
- **Return format** (under N words, prioritized list, link format, what
  NOT to include)

Each sub-agent returns a tight bulleted report. The orchestrator
synthesizes — does not paste raw sub-agent output verbatim.

The Telegram bot surfaces sub-agent output as separate
`🤖 sub-agent: <description>` bubbles below the orchestrator's bubble.
Brief each sub-agent to write its own report as if the user will read
it directly — terse, prioritized, no preamble. If a sub-agent's report
is large, cap it at ~3500 chars to fit Telegram. Highlight the top
items first.

### 4. For drafts: save them, don't just write them

When a draft can be saved to a private surface (Gmail draft, local file,
scratch note), save it. Capture the ID/URL. Surface only the snippet +
the action (`send draft?`). The user clicks once.

For Slack and GitHub where there's no equivalent "save private draft"
surface, write the full paste-ready text directly in the report (1–2
lines per item). The user copy-pastes.

### 5. Compose cards, not a single summary

The output of a scan is a set of `agency-report` cards, one per
decision, posted into the agency feed or appropriate forum topics.
Don't dump everything into one big assistant message — the user can't
button-tap a wall of text.

When a brief explicitly asks for a "report" (the old shape, before
button cards), use the synthesis template below as the final message
shape. Lead with FIRES, then everything else. But the default for a
"start agency" trigger is **cards, not a report**.

### 6. Synthesize report template (when a report is what's asked for)

```
🔥 FIRES (lead here if any)
- 1-line item — what's broken / who owns / suggested action

📧 EMAIL — needs your reply (drafts saved)
- from — subject — what they want — draft snippet — [draft ID / link]

💬 SLACK — blocked on you
- channel — who pinged — what they want — paste-ready 1-liner

🔧 GITHUB — quick wins
- repo#NNNN — [merge / close / wait] — one-line reason

📌 FYI (no action)
- tight items the user should know

🔌 ACCESS GAPS — what's blocking deeper triage
- exact next step the user must take to unblock the next scan

💡 PROACTIVE SUGGESTIONS
- numbered list of concrete follow-ups, each one self-contained
```

### 7. Only ask AFTER the work is done

End the scan with a numbered, concrete list of follow-ups (or a batch of
button cards). Each one self-contained:

- "1. Send all 8 Slack one-liners as a single batch."
- "2. Merge the 6 stale cloud PRs (each one already has its risk
  classified)."
- "3. Drop the `LMNR_API_KEY` into `/home/bux/.secrets/lmnr.env` so I
  can pull demo cuts."

Never ask permission to start. Always ask which finished work to ship.

## North-star metric: acceptance rate. Volume is anti-goal.

The single metric that matters: `(accepted + completed) / posted`. Every
other choice — title, length, image, urgency framing — is in service of
that. Posting 5 cards the user accepts beats posting 20 they ignore.

Each ignored card costs trust. Two ignored in a row = the user starts
skimming the channel. Five = they mute it. If you don't have a HIGH card
with a real impact angle this cycle, **post nothing**. Silence is better
than slop. "I have nothing high-impact to surface" is a valid scan result.

### Tie every card to the user's end-goal frame

The user's locked goal is in private memory (e.g.
`<user>_endgoal.md`). Examples of goal shapes: company success,
staying on top of everything, getting more users, developing a
startup, recruiting, fitness/gym consistency, or whatever they
locked during onboarding.

Each card's subhead must explicitly tie its action to that goal,
with a concrete number when possible:

- ❌ "submit to Smithery, virgin slot" *(so what?)*
- ✅ "+5K MCP devs/wk discover us → mindshare lift toward default-OSS-X"

If you can't write the subhead in that shape, the card isn't HIGH.
Drop it.

### Sell the card before asking for the tap

Proactive cards feel random because the user did not ask for them.
The card must explain *why this idea matters for the locked goal*
and *why it deserves attention now*. This is not hype; it is the
bridge from "agent idea" to "user priority".

Every suggestion card needs a compact persuasion block in the
visible body or first expandable:

```
why this matters: <one sentence tying the action to the user's goal>
importance:       <low|medium|high> because <specific reach / money / risk / time window>
```

Use concrete evidence:

- reach: "20K docs visitors/month", "3M newsletter readers", "150 YC founders"
- impact: "direct path to 1K users", "unblocks $20K enterprise deal"
- timing: "launch window closes tonight", "reply is 2h old"
- effort: "one tap", "already drafted", "asset attached"

If the card cannot make a convincing goal-tie, it is not a good
agency card. Do not post it just because the scan found something
interesting.

Keep the persuasion *off* the image — the image is the billboard,
not the proof. The image's job is one-glance "what action, what
goal-lever"; the body carries evidence and the exact steps.

Convince via specifics, not begging:

- A concrete number ("3.5K stars · maintainer ships PRs in 24h")
- A real competitor move ("X listed yesterday — first-mover slot is a 7-day window")
- A user-quote callback ("you said '2× faster than Y' in #general — back it publicly")
- A peer-network proof ("Z is YC W25 + warm investor path")

Don't add "please accept this!" lines. Neediness reads as weakness
and gets dismissed faster.

### Compression bar

The card must be 2-second-readable on a phone screen, sometimes late-
night, sometimes mid-workout. Specifics:

- Title ≤ 80 chars.
- Subhead ≤ 100 chars and contains the impact phrase.
- Draft expandable: 3-5 lines of paste-ready text. No reasoning, no
  preamble.
- Reasoning expandable: optional, max 3 sentences, only if it adds
  urgency or unblocks a question the user would actually ask.
- No bullet trees nested >1 level.
- No URL pasted bare — always `[label](url)`. Place URLs inside
  `--source-label` / `--source-url` for the canonical clickable header.
- Image (`--image-text` or `--image-file`): default ON unless the card
  type is genuinely text-only (e.g. a benchmark number table).

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

## Voice for drafts

If the user's `MEMORY.md` specifies their voice, follow it exactly. As a
fallback default:

- Match their typical reply length (sub-30 words for casual; longer only
  when explicitly justified)
- Match their casing (lowercase / sentence case)
- Use their default opener / closer / CTA from MEMORY.md
- Switch to their native language for native-language recipients
- For high-status contacts, use the user's preferred VIP move (phone
  number, signature CTA, in-person invite) if MEMORY.md captures one

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
4. **Multi-variant cards: one expandable per variant, NOT all variants
   crammed into a single block.** When a brief offers genuine A/B/C
   alternatives, each variant gets its own collapsible header — e.g.
   `📝 Variant A · founder DM`, `📝 Variant B · forward to Saurav`,
   `📝 Variant C · escalate`. The user opens only the one they're
   considering. Stuffing all three into a single `Drafted action`
   expandable defeats the point of the collapse.
5. **Optional `📎 Context`** for provenance / related threads / why this
   is distinct. Skip when nothing useful. Empty expandables are worse than
   no expandable. **Don't put internal log-entry numbers (`N=145`,
   `N=146`) in here** — they're agency-cron bookkeeping the user
   doesn't read. Drop the "X cards pending" framing too. The Context
   block is ≤2 short prose lines or it doesn't ship.
6. **Buttons in a 2+1 grid.** Row 1 = primary + Skip. Row 2 = third
   button alone.
7. **Per-card-type tweaks override**:
   - PR / merge → primary expandable is the diff or PR link
   - Video / demo → MP4 is the surface; no drafted-text expandable
   - Status / FYI → sometimes no expandable at all is right
8. **Resist filling out a fixed schema.** Let card type drive shape.

### Common block patterns

The block heading is what the user reads first inside the expandable
area — it should make clear at-a-glance whether they need to expand it.
Most users rarely open the deep-context blocks; they exist for the cases
where they do. Bake the *what's-inside* into the heading.

| Pattern | When to use | Heading shape |
|---|---|---|
| **Drafted action** | Anything actionable (reply text, SQL, commands, PR review) | `📝 Drafted action` (or `📝 Drafted reply` / `📝 Drafted SQL`) |
| **Why** | The reasoning / risk / what's at stake | `📎 Context` |
| **Context: <person>** | Inbound from a real human — bake their name + role + history into the heading | `🔍 Context: Sarah Chen (Linear, $9.6k ARR)` |
| **Context: <company>** | New signup / customer event — company facts | `🔍 Context: Stripe Inc — 4 corp seats from HN` |
| **Variant A / B / C** | Picking between concrete drafts | `🅰️ Variant A — warm`, `🅱️ Variant B — terse`, `🅲 Variant C — technical` |
| **Repro / Logs** | Bug reports — what reproduced it | `🐛 Repro` / `📜 Logs` |
| **Timeline** | Incidents — when each thing happened | `⏱ Timeline` |

Multiple Context blocks are fine when warranted (e.g. context about the
person *and* about the company). Most cards won't need any. The point
is: **the bold heading tells the user whether to bother expanding it**,
so it has to name what's inside, not just label the slot.

### Variant-picker example (3 blocks + 3 buttons)

```bash
agency-report --emoji "✍️" \
  --title "Reply to Karol on HN — pick a tone" \
  --source-label "HN comment thread" --source-url "https://news.ycombinator.com/item?id=…" \
  --block '{"emoji":"🅰️","title":"Variant A — warm","body":"Hey Karol — …"}' \
  --block '{"emoji":"🅱️","title":"Variant B — terse","body":"Karol — thanks for the shout. …"}' \
  --block '{"emoji":"🅲","title":"Variant C — technical","body":"Karol — the LinkedIn flow you mentioned uses our iframe-race fix in v0.4.3. …"}' \
  --button "Send A" --button "Send B" --button "Send C" \
  --source "hn-karol-reply" --prompt "Send the chosen variant" --skip-if-exists
```

### Pre-build the asset before posting — don't ask permission to create

If a card's action is "make a video / chart / screenshot / image / draft
that requires building something", **build it first**, attach it to the
card, and ask Yes/No on whether to *post / send / ship* it. Never ask
"should I make a video?" or "want me to draft this?" — by the time the
card lands, the asset must already exist.

The user sees the artifact, judges it, taps yes/no on the visible
boundary (publish to X, send the email, post in #channel). Building the
artifact is internal-zone work and doesn't need permission. Asking
beforehand burns a slot for nothing — the user can't decide without
seeing the thing.

If the brief says "consider making a video about X", the card is `📹 Made
a 30s demo of X — post to @<user-handle>? [yes / no / regen]` with the MP4
attached, **not** `Make a video about X — yes/no?`. Same for charts
(render the chart and attach), images (render and attach), email drafts
(save to Gmail Drafts and surface the draft URL), Slack DMs (write the
exact paste-ready text in the expandable).

The only exception: when *building* the asset is itself irreversible or
expensive (e.g. minting an NFT, sending a paid SMS, calling a $100/run
API). Then ask first. For free internal work — render, draft, scrape,
preview — just do it.

## Image-first

Include an image on **every** card unless it's a pure photo asset (video MP4,
real chart, real screenshot — those carry their own visual). The image's job
is to make the card 2-second-readable on a phone screen: **what** would
happen if the user taps yes, and **why** it matters.

### Style: gradient + color-emoji is the default. placehold.co is a fallback.

Default look = a 1080×540 PIL render with a vertical linear gradient
(top-dark → bottom-light, color picked per card mood from a fixed palette:
blue, purple, pink, red, green, amber, teal, orange, indigo, cyan), an 8px
accent ribbon down the left edge, real **color** emoji top-left at ~110px
(via `/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf` — load at the bitmap-
required size 109 then resize via `Image.LANCZOS`; loading at any other size
errors `invalid pixel size` because Noto color emoji is fixed-bitmap), a big
bold headline (DejaVu Bold, 110pt for short headlines, 56pt when it doesn't
fit) in white, and one optional impact line (white, 56pt). Do not add a
paragraph subtitle unless it is a screenshot/chart annotation; dense text in
the image competes with the card body and fails on phone. This is what scans
best on a phone in dark / system / TG-default themes — the gradient gives
depth, color emoji renders as the actual color glyph (not an outline), white
type holds across both ends of the gradient.

`placehold.co` (`--image-text` below) is a fallback for emergency cards
where a full PIL render isn't worth the budget — flat color, plain text,
serviceable but not beautiful.

**Don't use Remotion for static cards.** Remotion is a video framework
(React + headless Chrome render farm, ~10s per card). Reserve it for
actual MP4s in the growth-video topic. PIL renders in ~0.2s and produces
the look the user has on file as the "good ones".

### `--image-text` — fallback, sparse WHAT + IMPACT shape

`agency-report --image-text "..."` auto-renders a placehold.co card
(1200×630, magenta-on-purple, font Montserrat) with `\n`-separated lines,
word-wrapped to ≤22 chars per line. Use a sparse **WHAT + IMPACT shape**:

```
LINE 1 — short WHAT (artifact/channel/lever, in caps)
LINE 2 — goal impact (number, audience, or direct user-goal lever)
```

Worked examples (all rendered placehold.co cards):

| Card | `--image-text` value |
|---|---|
| Anthropic Cookbook PR | `"COOKBOOK PR\n100K dev reach"` |
| Lenny Newsletter pitch | `"LENNY PITCH\n3M ICP readers"` |
| $25K bounty | `"$25K BOUNTY\n200 builders"` |
| HF Spaces demo | `"HF SPACES\n3M MAU"` |
| 10 evangelists | `"10 EVANGELISTS\ntestimonial engine"` |
| Free for OSS | `"FREE OSS\nkills price objection"` |

### Rules of thumb for `--image-text`

- **Two lines by default.** Three lines is the hard maximum and only for a
  very short third token such as `today` or `one tap`.
- **≤22 chars per line, ≤8 words total.** Longer lines auto-wrap; single
  long words pass through unbroken. If the action needs a sentence, put that
  sentence in the card text, not the image.
- **No labels.** Don't write `I WILL:`, `IMPACT:`, `WHY:`, or `ACTION:` in
  the image. Labels waste the visual budget.
- **Each line earns its place.** No filler. If you can't write an impact line,
  the card itself isn't HIGH — drop it.
- **Caps for the WHAT line, mixed case for WHY.** Visual hierarchy on the
  fixed canvas — the eye lands on caps first.
- **Numbers in the WHY line whenever possible.** "3M readers" beats "huge
  reach"; "100K Claude devs" beats "the audience".
- **No bare URLs or `@handles`.** Those go in `--source-label`/`--source-url`
  for the clickable header. The image is for orientation, not navigation.
- **Match the language of the title.** If the title says "PR", the image
  says "+PR" — not "open a pull request".

### When `--image-text` is the wrong tool

Use `--image-file` (multipart upload of a real PNG) when:

- Plot / metric → real matplotlib chart with the actual numbers
- Person / outreach → small avatar (the recipient's headshot)
- Company → logo / favicon (when brand recognition matters more than text)
- PR / merge → tiny diff snippet rendered as code (the artifact itself)

Pass `--image` (direct URL) when you already have a hosted asset (a GitHub
avatar, an OG-image from a target page). Don't paste random web images that
haven't been vetted — TG caches pinned to the card.

### When to skip the image entirely

Only when the visual would be strictly worse than its absence:

- Pure status / FYI cards where one large emoji in the title carries the
  whole signal.
- Single-number cards (e.g. "deploy 200 OK") where the number IS the
  message — putting it in an image too is visual noise.

Default position: **include an image**. Defaulting to "no image" makes
cards read as plain text in a phone scroll where every other agent's card
has a visual. Yours get skipped.

## Buttons

Default 3-button set, label adapts to spawn-mode:

| In-place | Spawn-topic | Kind |
|---|---|---|
| `✅ Yes` | `🧵 Yes (new thread)` | `action` |
| `⏭ Skip` | `⏭ Skip` | `dismiss` |
| `✏️ Edit` | `🧵 Edit (new thread)` | `refine` |

**Per-kind callback behavior:**

- `action` — record decision, dispatch `--prompt` via `run_task`
- `dismiss` — record decision, **delete the card from the channel**, no
  LLM. The DB still tracks the dismissal for dedup + future-batch signal,
  but the card itself disappears from the user's feed — a skipped
  suggestion adds zero value to scrollback, and a "⏭ skipped" ack reply
  underneath actively makes the feed noisier
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

### Always include an Edit/refine button on suggestion cards

Most agency suggestions are not perfectly on point on first try — wrong
audience, wrong tone, slightly off framing, missing a constraint the user
hasn't told you about yet. The `refine` button (`✏️ Edit` / `🧵 Edit
(new thread)`) is the user's feedback channel: one tap spawns a worker
topic with the original card content already laid out, posts "What would
you change?", and waits for their reply. The agent then re-drafts and
posts a fresh card via `agency-report` (with a different `--source`
slug so it doesn't dedupe against the original).

**Default doctrine: keep the Edit/refine button on every suggestion
card.** The 3-button set (`✅ Yes` / `⏭ Skip` / `✏️ Edit`) is the
default precisely because Edit is the escape hatch for a suggestion
that's almost-but-not-quite right. Without it, the user's only options
on an imperfect suggestion are "accept the wrong thing" or "skip and
hope you re-pitch better next time" — both lossy.

When it's OK to drop the Edit button:

- **Single-tap confirmation cards** (merge a PR, restart a service, send
  a draft already shown in chat) — the user already implicitly approved
  the action upstream; the card is just the click. No refining needed.
- **Multi-draft picker** (`Send A` / `Send B` / `Send C`) — Edit doesn't
  make sense across N parallel drafts; if none fit, Skip silently
  dismisses and the user can ask for new variants in chat.

When in doubt, keep Edit. Cost of adding it: one button row. Cost of
dropping it on a card the user wanted to refine: a regenerated card
later, or worse, a dismissal that should have been a refinement.

### Single-tap confirmation — never make the user type "yes"

If the agent is mid-flight and needs the user to **confirm a small step** —
"merge it?", "restart bux-tg now?", "ack done?", "send the draft?", "deploy?"
— do NOT post a question and wait for typed input. Post a card with **one
button** that captures the entire confirmation:

```bash
agency-report \
  --title "Merge PR #119 (image-first doctrine, +68 LOC docs)" \
  --subhead "skill update on main → next agency batch picks it up" \
  --image-text "MERGE PR #119\nimage doctrine\non main, +68 LOC" \
  --button "✅ Yes, merge now" \
  --source confirm-merge-pr-119 \
  --prompt "gh -R browser-use/bux pr merge 119 --squash --delete-branch"
```

**Core principle: each user interaction should cost one tap, not one
keystroke.** Typing "yes" requires opening the keyboard, switching modes,
selecting send. A button is a single phone-screen tap. For confirmations
that have already been spelled out earlier in the conversation, the
question is already asked — the only thing left is the click.

When to use one-button confirmation cards:

- **Merge a PR** the agent just opened
- **Restart a service** after a config change
- **Run a queued action** the user said yes to in chat ("yes, do that next")
- **Acknowledge a milestone** the agent reports complete
- **Send a draft** the agent has already shown in plain text

When to keep the default 3-button set instead:

- The card is the **first surface** of a new proposal (yes / skip / refine
  is the right shape because the user might not be sold yet)
- Multiple drafts to pick from (use the smart-label pattern above)
- A no-tap = silently dismiss is meaningful (default `⏭ Skip` carries that)

The `agency-report --button "..."` flag overrides defaults; pass exactly
**one** `--button` for a single-button card. No `Skip` or `Edit` is added
when the override is used. If the user taps the button, the `--prompt` is
dispatched to a worker topic (or in-place if `--no-spawn-topic`).

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

## Closing a worker topic when the task is done

When everything in a worker topic is genuinely done — email sent, log
marked, no follow-up expected — end the turn with a "Close topic"
prompt so the user can sweep the topic out of their active list with
one tap. Closed topics stay readable; they just fall to the bottom and
stop drawing the eye.

```bash
tg-buttons "✅ done — <one-line summary of what landed>" "🗂 Close topic"
```

`tg-buttons` posts the message with one custom button. On tap, the
existing `kind=custom` dispatcher rotates `[agency-button] 🗂 Close
topic` back into the same lane as a synthesized user message. The
agent receives that on its next turn and closes the topic via the Bot
API directly — no new callback handler needed, the lane round-trip is
the implementation:

```bash
. /etc/bux/tg.env  # TG_BOT_TOKEN
curl -fsS -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/closeForumTopic" \
  -H 'Content-Type: application/json' \
  -d "$(jq -nc --argjson c "$TG_CHAT_ID" --argjson t "$TG_THREAD_ID" \
        '{chat_id: $c, message_thread_id: $t}')"
```

`TG_CHAT_ID` and `TG_THREAD_ID` come from the lane env (`_build_env`
exports both for every agent invocation). The bot is already a
supergroup admin with `can_manage_topics` (asserted at startup via
`setMyDefaultAdministratorRights`), so the call succeeds without
extra setup.

**When to use it:**

- ✅ Discrete task ran to completion in a worker topic (the typical
  agency `kind=action` shape: card → spawn topic → agent works → done).
- ✅ A small inline ask ("send Charles the decline") that finished in
  one turn — same pattern, the topic just doesn't stay open as a
  rolling lane.

**When NOT to use it:**

- ❌ Mid-task acks ("kicked off the deploy, will ping back"). Lane is
  still live.
- ❌ Follow-up question to the user. They need to reply, not close.
- ❌ The main agency feed topic. That's not a worker topic — closing
  it would hide the queue itself.
- ❌ `kind=refine` flows where the next turn depends on the user's
  reply.

**Skip the button when the task is trivial enough that the topic
shouldn't have been spawned in the first place** — closing
immediately after one turn is just round-trip noise. Better: post the
result inline in the original lane next time.

## Telegram message rules

- **2-second-scannable on phone.** Lead with verdict / headline; details
  below.
- **Bold via `*single asterisk*` (MDV2)** or `**double**` (the bot
  converts). No `#` / `##` headings, they render as literal `\#\#`.
- **Send images often.** Tables, briefs, status grids, comparisons,
  timelines render as PNG, not fenced-block tables.
- **No VM paths in TG.** Phone-first means clickable from the phone. Short
  doc inline; meaningful doc via `sendDocument` attachment.
- **≤3500 chars per message.** TG drops oversized messages silently. If a
  reply must exceed, split into sequential messages. Never compress by
  stripping content.
- **Never use em dashes (`—`).** They're the canonical AI-tell that makes
  cards read as machine-written. Replace with: a comma, a colon, a period,
  parentheses, or a hyphen-minus, whichever fits the sentence best. Applies
  to every field rendered to the user (`--title`, `--subhead`, `--draft`,
  `--reasoning`, plus any text passed to `tg-send` from agency contexts).
  En dashes (`–`) are also disallowed.

  | Bad | Good |
  |---|---|
  | `Lovable runs A/B vs Browserbase — they're a customer.` | `Lovable runs A/B vs Browserbase, they're a customer.` |
  | `100K Claude devs — every PR matters.` | `100K Claude devs. Every PR matters.` |
  | `RICE: R: 3M readers — I: 3/3` | `RICE: R: 3M readers · I: 3/3` |

  Bullet separator inside one line: middle-dot `·` (U+00B7), arrow `→`,
  pipe `|`, or just split into two short sentences.

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

## Don't draft publishable copy during an active embargo

When a task brief implies announcement / PR / launch copy, and the
source material (email, doc, ticket) contains words like "embargo",
"confidential", "do not share until", "hold until", or a future
"go-live" date — *stop before drafting*. Push back on the brief
first.

Drafting before the embargo lifts wastes cycles: the real public URL
and framing both shift on launch day, so the copy gets redone anyway.
Worse, a publish-button card during a confidential window is a
footgun even in a private topic.

Reply with: *"Embargo doesn't lift until X (in PT). Drafting now means
we won't have the real public URL or the real public framing — both
shift the copy. Want me to schedule a re-spin for ~15 min after the
embargo lifts, or do you actually want speculative drafts now?"*
Only proceed if the user explicitly says "yes, draft anyway."

Treat the written brief as a starting point, not a binding instruction
— if it conflicts with information in the source material (an embargo
notice the user may have missed), surface the conflict before
executing.

## Per-topic shape rules

A few forum topics expect a specific output shape; don't post the
default text card there.

**Growth / video topic** (typically named `🎬 growth-video` or similar
— check the topic name before assuming an id). The only acceptable
output is an *actual video MP4 of a sick demo*, with very short
caption text (1–2 lines explaining why it's sick). Never post
storyboards, written 30-second concepts, "video idea" cards, or
text-only suggestions: the user is on a phone making yes/no calls and
can't watch text. They only know if a demo is sick by *seeing it*.

For the growth-video topic on any given box: every Agency item must be
or include a real MP4. If you don't have the video yet, don't post —
go produce one first via `video-use`, Hyperframes (HTML→MP4),
Remotion, or a screen-record + ffmpeg cut.

Other topics may still want text cards. Check each topic's brief
before drafting.

## Honor access gaps

When a tool can't see a surface (no auth, missing API key, integration
not connected), name the gap *and the exact next step* the user must
take to unblock it. Not "I couldn't access X" — but
"X needs auth: run `/mcp` in your client → connect 'Y' → then I can
scan it." Make the next scan strictly more useful than this one.

## Where things live

| Surface | Purpose |
|---|---|
| `agent/AGENCY.md` *(this file)* | Generic mechanics |
| `~/.claude/projects/<…>/memory/` | Personal preferences (private) |
| `agent/agency-report` | CLI helper — posts cards, validates inputs |
| `agent/agency_db.py` | SQLite store + public helpers |
| `agent/telegram_bot.py` | Callback handler + lane dispatch |
| `/var/lib/bux/agency.db` | Per-suggestion ledger |
