# Alpha C2C

**ChatGPT plans and reviews. Your coding agent builds and tests.**

Two standalone skills for collaborating with ChatGPT web from **Codex** or **Claude Code**. The workflow and CLI guidance are written in English. User-facing responses follow the user's language instead of inheriting a transport's hard-coded locale.

| Package | Coding agent | Connection executable | Invocation |
| --- | --- | --- | --- |
| `alpha-c2c-codex` | Codex | `c2c` | `$alpha-c2c-codex` |
| `alpha-c2c-claude` | Claude Code | `code-with-chatgpt` | `/alpha-c2c-claude` |

## What it does

- Keeps planning/review in ChatGPT web and execution in the coding agent.
- Requires an actual workspace tool result before treating a connection as ready.
- Resumes saved work without automatically repeating an already-sent message or completed execution.
- Summarizes transport status without forwarding raw locale-specific messages or authentication data.
- Loads connection, browser, and resume details only when needed.

This is a skill/workflow package, **not a replacement MCP server, OAuth provider, tunnel, or browser driver**. It uses an existing compatible connection transport. It does not install or reset that transport, modify account settings, or migrate active conversations.

## Requirements

- Python 3.10+ for the optional helpers and installer; no Python dependencies.
- Codex or Claude Code with user-skill support.
- A compatible `c2c` / `code-with-chatgpt` transport, such as [codex-with-chatgpt](https://github.com/XiaoDuoYa/codex-with-chatgpt), installed separately.
- Access to ChatGPT web and the connector features required by your account.
- A browser integration for automatic message delivery. With Orca the helper drives the tab itself. With a regular Chrome started with remote debugging on 127.0.0.1, the same helper drives it over the Chrome DevTools Protocol (standard library only). With any other browser tool that can run page JavaScript and click (for example Claude in Chrome or a Playwright MCP server), the agent drives the tab and the helper judges its saved DOM observations (agent-driven mode). Without one, delivery is manual.

Compatibility depends on the installed transport exposing `status --json`, `session get/set`, `record`, and a workspace-info tool. Check command help before setup or repair; those operations may change local services.

## Install

### Claude Code (plugin, recommended)

Claude Code 2.1.275 or later, in a session:

```text
/plugin install alpha-c2c --marketplace Gforce10-design/alpha-c2c
```

Older versions:

```text
/plugin marketplace add Gforce10-design/alpha-c2c
/plugin install alpha-c2c@alpha-c2c
```

The skill is then available as `/alpha-c2c:alpha-c2c-claude`. Update with `/plugin marketplace update alpha-c2c`, or turn on auto-update for this marketplace in `/plugin`.

### Codex (built-in skill installer)

Ask Codex to use `$skill-installer` with repo `Gforce10-design/alpha-c2c` and path `skills/alpha-c2c-codex`. It installs into `$CODEX_HOME/skills`.

### Either client (script)

```sh
git clone https://github.com/Gforce10-design/alpha-c2c.git
cd alpha-c2c
python3 scripts/install.py            # every client detected on this machine
python3 scripts/install.py --dry-run  # show where it would install
```

The installer follows each client's own settings: Claude Code uses `$CLAUDE_CONFIG_DIR/skills` when that variable is set, otherwise `~/.claude/skills`; Codex uses `$CODEX_HOME/skills` when set, otherwise `~/.agents/skills`. If the skill is already installed in one of those places, that location is reused, so no second copy appears. Choose explicitly with `--client codex|claude|all` or `--skills-dir <dir>`.

Installation is idempotent. If an existing copy differs, the installer stops; `--update` first moves the old copy to `.alpha-c2c-backups/` next to the skills directory (outside it, so it is never loaded twice), then installs. It refuses to replace a directory that is not this skill or a symlink. After installing it reminds you if the `c2c` / `code-with-chatgpt` transport is missing. Start a new agent session afterwards. Uninstall by removing only the installed `alpha-c2c-codex` or `alpha-c2c-claude` directory (or `/plugin uninstall alpha-c2c`); transport and account settings are separate.

Existing third-party C2C skills are not removed. Invoke the new skill explicitly to avoid ambiguous selection, and disable old duplicates using your client's own settings when appropriate.

## Use

Codex:

```text
Use $alpha-c2c-codex to plan, implement, and review this feature with ChatGPT.
Respond to me in English.
```

Claude Code:

```text
/alpha-c2c-claude Plan and implement this change with ChatGPT, then review the results.
```

The skill follows your language. Technical identifiers remain unchanged. The helper CLI uses English messages and accepts a `--language` argument for reviewer prompts.

Read local status without initiating setup:

```sh
python3 skills/alpha-c2c-codex/scripts/c2c.py status --workspace /path/to/project
python3 skills/alpha-c2c-claude/scripts/c2c.py status --workspace /path/to/project
```

A local status report deliberately never claims end-to-end readiness. The exact web conversation must return the matching workspace identity.

Generate a planning message without sending it:

```sh
python3 skills/alpha-c2c-codex/scripts/c2c.py message plan \
  --task feature-01 --workspace-id workspace-id --language Spanish \
  --goal-file goal.txt
```

Keep goals and summaries short and free of credentials. The helper preserves supplied text; it is not a secrets scanner or translator.

## Development

Both distributable skills are generated from one maintained source in `src/skill-template/`. Edit the template, not the generated copies.

```sh
python3 scripts/build.py
python3 scripts/build.py --check
python3 -m unittest discover -s tests -v
```

Tests cover both executors, non-English transport messages, malformed checkpoints, repeat prevention recommendations, argument boundaries, multilingual prompts, and non-destructive installation. They do not establish a live ChatGPT login, browser exchange, or end-to-end review. Validate those separately in your own task-owned conversation.

## Provenance

The workflow instructions and Python helpers in this repository were written for Alpha C2C. The external transport is a separate project with its own authors and license. This repository does not vendor its implementation or claim ownership of it. Transport command names describe integration points, not copied source.

MIT license. Contributions should keep the entrypoints short, use English documentation, respect the user's language, and include observable tests for behavior changes.
