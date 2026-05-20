# Hermes on Bux Implementation Plan

## Objective

Add Hermes at the Bux layer first, before BuxFather or cloud provisioning selects
it by default.

Success means a normal Browser Use Box can:

1. Install or discover a Hermes runtime as the `bux` user.
2. Preserve the user's existing Hermes subscription and local credentials.
3. Add Bux operating context to Hermes without overwriting user-specific Hermes
   context.
4. Run Hermes from Telegram as a third lane agent beside Claude and Codex.
5. Use the existing Browser Use Box browser, workspace, Telegram routing, and
   helper scripts.
6. Leave cloud/BuxFather as a later thin selector of the default agent.

## Design Principles

- Bux owns installation and runtime. Cloud should not know how Hermes works.
- Hermes auth is local. Do not move Hermes credentials into cloud or `/etc/bux`.
- The installer must be idempotent and conservative. Existing Hermes state wins.
- The Telegram bot should call a Bux wrapper, not the raw Hermes binary.
- Bux context should be versioned in the repo, while personal context stays
  gitignored.
- BuxFather should only pass `default_agent=hermes` after Bux can run Hermes
  reliably on its own.

## Current Bux Hierarchy

### First Install

`install.sh` sets up a fresh machine:

- system packages
- Node, Claude Code, Codex, browser harness, ttyd
- `/home/bux/CLAUDE.md`
- `/home/bux/AGENTS.md -> /home/bux/CLAUDE.md`
- helper scripts in `/usr/local/bin`
- systemd units
- optional `/etc/bux/tg.env`

Hermes first-install work belongs here only if it is needed on brand-new boxes.

### Update / Self-Heal

`agent/bootstrap.sh` runs on existing boxes after `/update` and boot-time pulls.
It re-links helpers, re-applies systemd units, refreshes browser harness, and
self-heals missing Codex installs.

Hermes update-time work belongs here too. Any new Hermes helper, context file,
or systemd unit must be re-asserted here so older boxes catch up.

### Runtime Agent Router

`agent/telegram_bot.py` maps each Telegram lane to an agent. Today:

- `claude`
- `codex`

Hermes should become a third lane agent here:

- `hermes`

### Cloud Control Agent

`agent/box_agent.py` receives cloud WebSocket commands. It should stay mostly
out of Hermes until cloud needs to set the default Telegram agent during
`tg_install`.

## Target Files

Add these files in `~/Projects/bux`:

```text
agent/HERMES.md
agent/install-hermes
agent/bux-hermes
docs/hermes-bux-implementation-plan.md
```

Later, once runtime is proven:

```text
agent/telegram_bot.py
install.sh
agent/bootstrap.sh
agent/box_agent.py
agent/test_telegram_bot.py
```

Optional only if Hermes needs a daemon:

```text
agent/bux-hermes.service
```

## Runtime Shape

The Telegram bot should run:

```text
telegram_bot.py -> /usr/local/bin/bux-hermes -> hermes
```

`bux-hermes` is the stable Bux-owned boundary. It should:

- run as user `bux`
- set `HOME=/home/bux`
- set the Bux PATH:
  `/home/bux/.local/bin:/home/bux/.npm-global/bin:/usr/local/bin:/usr/bin:/bin`
- source `/home/bux/.claude/browser.env` if present
- keep `TG_CHAT_ID`, `TG_THREAD_ID`, `TG_USER_ID`, `TG_USERNAME`,
  `TG_FROM_NAME`, `TG_OWNER_ID`, and `TG_IS_OWNER` from the bot environment
- set `BUX_HERMES_SOUL=/home/bux/.hermes/SOUL.md`
- execute the real Hermes binary

The raw Hermes binary path should be configurable:

```text
HERMES_BIN=/home/bux/.local/bin/hermes
```

If `HERMES_BIN` is unset, the wrapper should search:

```text
/home/bux/.local/bin/hermes
/home/bux/.npm-global/bin/hermes
/usr/local/bin/hermes
/usr/bin/hermes
```

This lets Bux support either a uv/pipx install, npm install, or a pre-existing
Hermes binary without baking in one package manager too early.

## Hermes Context Model

The user mentioned adding the Bux thing to Hermes' `SOUL.md`. Do that, but make
it safe.

Use three layers:

```text
agent/HERMES.md
  Public Bux doctrine for Hermes. Tracked in git.

private/hermes/soul.local.md
  Optional per-box local overlay. Gitignored.

/home/bux/.hermes/SOUL.md
  Generated final file consumed by Hermes.
```

`agent/install-hermes` should generate `/home/bux/.hermes/SOUL.md` with managed
markers:

```text
<!-- BEGIN BUX HERMES MANAGED -->
contents of agent/HERMES.md
<!-- END BUX HERMES MANAGED -->

<!-- BEGIN BUX HERMES LOCAL -->
contents of private/hermes/soul.local.md, if present
<!-- END BUX HERMES LOCAL -->
```

Rules:

- Never delete text outside the managed blocks.
- If `/home/bux/.hermes/SOUL.md` already exists without Bux markers, preserve it
  by appending the Bux managed block below the existing content.
- Make a one-time backup before first modification:
  `/home/bux/.hermes/SOUL.md.pre-bux`.
- If Hermes supports includes or multiple context files, prefer includes over
  rewriting `SOUL.md`. Until that is confirmed, use the marker strategy.

`agent/HERMES.md` should be short and Bux-specific:

- You are Hermes running inside Browser Use Box.
- Default workspace is `/home/bux`.
- Use Browser Use Cloud through `browser-harness-js`.
- Source `/home/bux/.claude/browser.env` before browser work.
- Use `tg-send` for background replies.
- Respect Telegram lane context via `TG_CHAT_ID` and `TG_THREAD_ID`.
- Do not install local Chrome or Playwright browsers.
- Put user-private, durable context in `private/` or Hermes' own memory, not in
  repo-tracked files.

Do not copy the full `agent/CLAUDE.md` into Hermes' soul. Hermes needs a focused
adapter document, not all Claude-specific commands and assumptions.

## Installer Contract

Create `agent/install-hermes`.

Inputs:

```text
WITH_HERMES=1              # default once stable; during rollout it can default to 0
HERMES_BIN=...             # optional explicit binary path
HERMES_INSTALL_CMD=...     # optional operator-provided install command
HERMES_INSTALL_MODE=detect # detect, npm, uv, skip
```

Behavior:

1. Exit early if `WITH_HERMES=0`.
2. Ensure `/home/bux/.hermes` exists and is owned by `bux:bux`.
3. If a Hermes binary already exists, do not reinstall it.
4. If no binary exists:
   - if `HERMES_INSTALL_CMD` is set, run it as `bux`
   - else if `HERMES_INSTALL_MODE` is known, run that installer as `bux`
   - else warn and continue
5. Generate or update `/home/bux/.hermes/SOUL.md`.
6. Symlink `agent/bux-hermes` to `/usr/local/bin/bux-hermes`.
7. Run a non-destructive status check if possible:
   `bux-hermes --version` or `hermes --version`.
8. Never run `hermes login` automatically.

Subscription/auth rule:

Existing Hermes subscription state under `/home/bux` must be preserved. The
installer may detect and report auth state, but it must not rotate, delete, or
replace credentials.

## First Install Wiring

In `install.sh`:

1. Add optional env documentation near `WITH_ZTK`:

   ```text
   WITH_HERMES          - install/configure Hermes support
   HERMES_BIN           - optional existing Hermes binary path
   HERMES_INSTALL_CMD   - optional custom install command, run as bux
   ```

2. Set a rollout default:

   ```bash
   WITH_HERMES="${WITH_HERMES:-0}"
   ```

   Start with `0` until the Hermes package/install source is confirmed. Flip to
   `1` after staging proves stable.

3. After the Codex install block, call:

   ```bash
   if [ "$WITH_HERMES" = "1" ]; then
     /bin/bash "$REPO_DIR/agent/install-hermes" || warn 'hermes install failed'
   fi
   ```

Do not fail the whole Bux install if Hermes fails during the first rollout.
Claude and the browser must still come up.

## Bootstrap Wiring

In `agent/bootstrap.sh`:

1. Re-link the wrapper on every update:

   ```bash
   ln -sfn "$REPO_DIR/agent/bux-hermes" /usr/local/bin/bux-hermes
   ```

2. Re-run the Hermes installer when enabled:

   ```bash
   if [ "${WITH_HERMES:-0}" = "1" ] || [ -x /usr/local/bin/bux-hermes ]; then
     /bin/bash "$AGENT_DIR/install-hermes" || \
       echo "bootstrap: hermes install/update failed (non-fatal)" >&2
   fi
   ```

The second condition matters: once a box has Hermes enabled, `/update` should
keep its wrapper and Bux soul current even if the environment variable is not
present on a later bootstrap.

## Telegram Integration

In `agent/telegram_bot.py`:

1. Add constants:

   ```python
   AGENT_HERMES = "hermes"
   AGENTS = (AGENT_CLAUDE, AGENT_CODEX, AGENT_HERMES)
   ```

2. Add `/hermes` to `BOT_COMMANDS`.

3. Add command handling next to `/claude` and `/codex`:

   - `/hermes`: switch current lane to Hermes
   - `/hermes status`: run `bux-hermes --version` and auth/status check if
     Hermes supports it
   - `/hermes login`: only if Hermes has a headless or terminal-safe login

4. Dispatch:

   ```python
   if agent == AGENT_CODEX:
       self._run_codex(...)
   elif agent == AGENT_HERMES:
       self._run_hermes(...)
   else:
       self._run_claude(...)
   ```

5. Implement `_run_hermes(...)` using the existing `StreamingMessage` pattern.

Minimum first implementation:

- call `/usr/local/bin/bux-hermes`
- pass the prompt as a final argument or stdin, depending on Hermes CLI shape
- use `cwd=/home/bux`
- pass the same `_build_env(...)` output used for Claude and Codex
- stream stdout into Telegram
- capture stderr to a temp file
- surface install/auth errors clearly

If Hermes has no JSON streaming mode, plain stdout streaming is acceptable for
the first pass. Add structured event parsing later.

## Default Agent Config

Use `BUX_DEFAULT_AGENT`, not `TG_DEFAULT_AGENT`.

Reason:

- The default is a Bux runtime preference.
- Telegram is only one input surface.
- The same setting could later apply to miniapp or shell-created lanes.

Storage:

```text
/etc/bux/tg.env
BUX_DEFAULT_AGENT=hermes
```

`telegram_bot.py` should read it from the existing systemd environment or from
`/etc/bux/tg.env` via `_read_kv(TG_ENV)`.

Resolution:

1. Explicit lane binding in `/etc/bux/tg-state.json` wins.
2. `BUX_DEFAULT_AGENT` wins for unbound lanes if valid.
3. Auth/install-aware fallback:
   - if default is Hermes but `bux-hermes` is missing, show a Hermes install
     error
   - do not silently run Claude for a lane explicitly defaulted to Hermes
4. If no default is set, keep current behavior: prefer authed Claude, then
   authed Codex, then Claude.

This lets BuxFather later pass `BUX_DEFAULT_AGENT=hermes` during `tg_install`
without changing how Hermes itself is installed.

## Cloud/BuxFather Boundary Later

Only after the Bux-level runtime works:

1. Cloud adds `agent_kind` to the BuxFather pending `/newbox` payload.
2. Managed-bot flow passes `default_agent="hermes"` to `install_telegram(...)`.
3. `install_telegram(...)` includes `default_agent` or `bux_default_agent` in
   the WebSocket `tg_install` payload.
4. `box_agent.py` writes `BUX_DEFAULT_AGENT=hermes` into `/etc/bux/tg.env`.

Cloud should not install Hermes, store Hermes credentials, or track Hermes auth
state in phase one.

## Phased Work Plan

### Phase 1: Bux Hermes Installer And Context

Files:

- `agent/HERMES.md`
- `agent/install-hermes`
- `agent/bux-hermes`
- `install.sh`
- `agent/bootstrap.sh`

Done when:

- running `WITH_HERMES=1 sudo ./install.sh` creates or preserves
  `/home/bux/.hermes/SOUL.md`
- existing `SOUL.md` content survives
- Bux-managed block updates on rerun
- `/usr/local/bin/bux-hermes` exists
- no Hermes login is attempted automatically

### Phase 2: Local Wrapper Smoke Test

Files:

- `agent/bux-hermes`
- maybe `agent/install-hermes`

Done when:

- `sudo -iu bux bux-hermes --version` works when Hermes is installed
- wrapper exits with a useful error when Hermes is missing
- wrapper sees `BU_CDP_WS` after browser keeper writes browser env
- wrapper runs from `/home/bux`

### Phase 3: Telegram Hermes Lane

Files:

- `agent/telegram_bot.py`
- `agent/test_telegram_bot.py`

Done when:

- `/hermes` switches only the current lane
- normal prompts in that lane call `_run_hermes(...)`
- missing Hermes binary produces a clear Telegram message
- Claude and Codex behavior is unchanged

### Phase 4: Bux Default Agent

Files:

- `agent/telegram_bot.py`
- `agent/box_agent.py` later

Done when:

- `BUX_DEFAULT_AGENT=hermes` routes new unbound lanes to Hermes
- explicit `/claude` and `/codex` still override the default
- invalid default is ignored with a log warning or visible status

### Phase 5: Cloud Selector

Files in `~/Projects/cloud`:

- `backend/common/services/buxfather/nonce.py`
- `backend/common/services/buxfather/flows/newbox.py`
- `backend/common/services/buxfather/flows/managed_bot.py`
- `backend/app/endpoints/api/v3/boxes/services.py`

Files in `~/Projects/bux`:

- `agent/box_agent.py`

Done when:

- BuxFather can create a normal Box whose child bot starts with
  `BUX_DEFAULT_AGENT=hermes`
- cloud never touches Hermes credentials
- old boxes without default-agent support still fall back safely

## Test Plan

Shell/script tests:

- `bash -n agent/install-hermes`
- `bash -n agent/bux-hermes`
- `bash -n install.sh`
- `bash -n agent/bootstrap.sh`

Installer behavior tests, preferably with a temp HOME or fixture:

- no existing `SOUL.md`
- existing unmarked `SOUL.md`
- existing marked `SOUL.md`
- `private/hermes/soul.local.md` present
- missing Hermes binary
- explicit `HERMES_BIN`

Telegram tests:

- `AGENTS` includes Hermes
- `/agent hermes` or `/hermes` binds only current lane
- missing wrapper error path
- `BUX_DEFAULT_AGENT=hermes` controls unbound lane
- explicit lane binding overrides `BUX_DEFAULT_AGENT`

Manual staging:

1. Provision or use a staging Box.
2. Install Hermes manually as `bux` using the real subscription flow.
3. Run `WITH_HERMES=1 sudo ./install.sh` or `sudo agent/bootstrap.sh`.
4. Confirm subscription state still works.
5. Confirm `/home/bux/.hermes/SOUL.md` contains Bux managed context.
6. Confirm `source ~/.claude/browser.env && bux-hermes ...` can use the Browser
   Use browser.
7. Enable Telegram and switch a topic with `/hermes`.
8. Send a browser task and confirm Hermes uses the existing Box browser.

## Main Risks

- Hermes' real CLI contract is unknown. Keep `bux-hermes` as the adapter so only
  one file changes when the contract is known.
- Hermes may already manage `SOUL.md` itself. Use markers and backups, or switch
  to includes if Hermes supports them.
- Hermes may need an interactive login. Do not automate it until the exact flow
  is known; use `/terminal hermes login` as the fallback.
- Defaulting a lane to Hermes should not hide install failures by silently
  routing to Claude. For an explicit Hermes default, show a clear Hermes error.

## Recommendation

Implement this in Bux first:

1. `agent/install-hermes`
2. `agent/bux-hermes`
3. `agent/HERMES.md`
4. Telegram `/hermes`
5. `BUX_DEFAULT_AGENT`
6. BuxFather/cloud pass-through

That order makes Hermes real on the Box before cloud markets or provisions it as
a Hermes Box.
