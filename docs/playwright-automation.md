# Playwright Automation With Browser Use Box

Browser Use Box is useful when a Playwright-style workflow needs more than a local script:

- persistent cookies and login state
- a real cloud browser that survives your laptop closing
- Telegram control for starting, checking, and approving work
- a live browser handoff when a site asks for login, 2FA, CAPTCHA, or a consent click

Instead of keeping a Playwright test runner open on your laptop, you can run the agent on a VPS and ask it to keep working:

```text
Watch this GitHub PR until CI is green, then summarize the failing check if it turns red.
```

```text
Use the browser profile to update this logged-in dashboard every morning and tell me what changed.
```

```text
Check Gmail every 30 minutes, draft replies, and ask before sending anything.
```

The browser is maintained by Browser Use Cloud through `bux-browser-keeper`, and the agent drives it with `browser-harness` over CDP. Cookies persist through the Browser Use profile, so repeated automations can reuse the same session.

<a href="https://www.tiktok.com/@browser_use/video/7639824093721758989">
  <img src="tiktok-demo-thumbnail.jpg" alt="Watch the 14-second Browser Use Box demo on TikTok" width="280" />
</a>

Watch the short demo: [Browser Use Box on TikTok](https://www.tiktok.com/@browser_use/video/7639824093721758989).

Install it from the repo root:

```bash
curl -fsSL https://raw.githubusercontent.com/browser-use/bux/main/install.sh \
  | sudo BROWSER_USE_API_KEY=bu_xxx bash
```
