# private/

Per-box drop zone for personal context. **Anything you put here stays local — `.gitignore` blocks every file in this directory from being committed**, with the lone exception of this README and `.gitkeep`.

Use it for things that should never end up on GitHub:

- **Personal skills** — domain-specific `SKILL.md` files for your own bux that you don't want shared upstream. Either drop them directly here, or symlink from `~/.claude/skills/<name>/` into `private/skills/<name>/`.
- **Memory / context files** — long-form notes about you, your team, your projects, your style. Anything you'd want a fresh agent to read on day one but you wouldn't post publicly.
- **Scratch notebooks** — research, drafts, snippets you're iterating on.
- **Per-box config snippets** — anything you'd want versioned alongside the bux checkout but kept off the public repo.

The folder itself **is** tracked (via `.gitkeep`) so the path always exists and tooling can rely on it. The contents are not.

## Where this fits in the bux memory model

bux already has the [Claude Code auto-memory system](https://docs.claude.com/en/docs/claude-code/memory) at `/home/bux/.claude/projects/-opt-bux-repo/memory/`, which the agent reads and writes automatically across sessions. That system stores its files outside the repo and is the right home for *agent-managed* memory.

Use `private/` for *human-managed* context: things you want to put in front of the agent yourself, in a stable location, that are too sensitive to publish.

## Quick smoke test

```bash
echo "hello" > private/test.txt
git status         # should NOT show private/test.txt
rm private/test.txt
```
