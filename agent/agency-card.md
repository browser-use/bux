# `agency-card` — canonical agency-card helper

The companion to `agency-report`. Use it whenever an agency suggestion warrants
an image-bearing card (most of them, on a phone).

```
[optional image]
emoji + bold headline
italic source link · timestamp
optional 1-line subhead
expandable: drafted action / actionable content
expandable: reasoning / why now
buttons: row1 = [primary] [skip], row2 = [third]
```

## Usage

Reads JSON spec on stdin. Required: `headline`. Common optionals: `emoji`,
`source`, `source_url`, `subhead`, `image`, `image_text`, `draft`, `reasoning`,
`primary`, `skip`, `third`, `chat_id`, `thread_id`.

```bash
echo '{
  "emoji": "🚨",
  "headline": "Reply to Jarmin (YC F25) — LinkedIn anti-bot",
  "source": "Gmail · Chalmers Brown",
  "source_url": "https://mail.google.com/mail/u/0/#inbox/19e042b403ec7e03",
  "subhead": "Gregor 3× Superhuman-shared, 26h stale.",
  "draft": "hey chalmers — that is linkedin'\''s anti-bot challenge…",
  "reasoning": "YC F25 batch peer + cofounder-shared 3×.",
  "primary": {"text": "✅ Send", "callback_data": "agcy:0:0"},
  "skip":    {"text": "⏭ Skip",  "callback_data": "agcy:0:1"},
  "third":   {"text": "📝 Edit / pick B|C", "callback_data": "agcy:0:2"}
}' | TG_CHAT_ID=-100... TG_THREAD_ID=0 agency-card
```

Returns `{ok, message_id, chat_id}` on stdout.

## Image — use `image_text`, do not roll your own

For most cards, pass `image_text` and let the helper generate a clean
`placehold.co` SVG-quality card. Color-coded, no font/spacing pain, lands
fast on a phone:

```json
"image_text": "Skyvern · 4 Reddit shills · 58 min"
```

For demo videos / screenshots / real artifacts, pass `image` with a direct URL.

**Do NOT build PIL gradient banners from scratch.** Magnus called this out:
custom PIL output looks ugly compared to placehold.co + the helper's HTML
layout. If `image_text` doesn't fit a specific card type, extend this helper
upstream — don't reinvent it inline.

## HTML escaping

Headline / subhead / draft / reasoning / source are HTML-escaped by default
(safe for arbitrary tool output). To embed raw HTML, append `_html` to the
key — e.g. `draft_html` instead of `draft`. URLs are always escaped.

## Long bodies

If the assembled caption is ≤1024 chars, the helper sends via `sendPhoto`
(image rendered above caption). Otherwise it falls back to `sendMessage`
with `link_preview_options.show_above_text=true` so the image still lands
visually first. Both paths render identically on mobile.

## When to use what

| Card shape                       | Helper           |
|----------------------------------|------------------|
| Suggestion + image + 3 buttons   | `agency-card`    |
| Suggestion text + 3 buttons      | `agency-report`  |
| Plain TG message (no buttons)    | `tg-send`        |
| Photo (no buttons, no caption)   | `tg-photo`       |
| Video                            | `tg-send-video`  |

`agency-report` writes to `/var/lib/bux/agency.db`; `agency-card` does not
(yet) — pair it with a manual `agency_db.insert(...)` call if you need the
suggestion persisted.

## See also

- `agent/agency-report` — text + buttons + DB row
- `agent/tg-send`, `agent/tg-buttons`, `agent/tg-schedule` — lower-level helpers
- `agent/AGENCY.md` — broader agency-mode doctrine
