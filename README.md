# Alpha C2C

**ChatGPT plans and reviews. Your coding agent builds and tests.**

Two standalone skills for collaborating with ChatGPT web from **Codex** or **Claude Code**. The workflow and CLI guidance are written in English. User-facing responses follow the user's language instead of a hard-coded locale.

| Package | Coding agent | ChatGPT connector name | Invocation |
| --- | --- | --- | --- |
| `alpha-c2c-codex` | Codex | `Alpha C2C Codex · <workspace>` | `$alpha-c2c-codex` |
| `alpha-c2c-claude` | Claude Code | `Alpha C2C Claude · <workspace>` | `/alpha-c2c-claude` |

## What it does

- Keeps planning/review in ChatGPT web and execution in the coding agent.
- Requires an actual workspace tool result before treating a connection as ready.
- Resumes saved work without automatically repeating an already-sent message or completed execution.
- Summarizes bridge status without forwarding raw locale-specific messages or authentication data.
- Loads connection, browser, and resume details only when needed.

Each skill bundles its own connection, `scripts/bridge.py`: a read-only MCP connector for one workspace, with OAuth pairing and a Cloudflare tunnel, written with the Python standard library only. No other C2C tool is needed. The bridge never writes to the workspace or runs commands for the reviewer, and the skill does not modify account settings or migrate active conversations.

## Requirements

- Python 3.10+ and `git`; no Python packages.
- Codex or Claude Code with user-skill support.
- [`cloudflared`](https://github.com/cloudflare/cloudflared) (for example `brew install cloudflared`). ChatGPT only connects to public HTTPS addresses; the bridge uses a free quick tunnel, so no Cloudflare account is needed. A fixed address needs a Cloudflare account with your own domain (see below).
- A ChatGPT plan that allows custom connectors (developer mode). You are responsible for using ChatGPT within OpenAI's terms; this project only automates your own browser session.
- A browser integration for automatic message delivery. With Orca the helper drives the tab itself. With a regular Chrome started with remote debugging on 127.0.0.1, the same helper drives it over the Chrome DevTools Protocol (standard library only). With any other browser tool that can run page JavaScript and click (for example Claude in Chrome or a Playwright MCP server), the agent drives the tab and the helper judges its saved DOM observations (agent-driven mode). Without one, delivery is manual.

## Connect a workspace

```sh
python3 <skill-dir>/scripts/bridge.py setup -w <repo> --json
```

This starts the bridge and tunnel and prints `connectorName`, `mcpUrl` and a five-minute pairing code. In ChatGPT (developer mode on), create a connector with that name and URL and OAuth authentication; on the sign-in page, type the pairing code. `status --json` is read-only, `doctor --json` checks and restarts what is missing, `unpair` revokes access, `stop` shuts it down. A quick-tunnel address changes when the bridge restarts, and ChatGPT cannot edit a connector's URL, so `doctor` tells you when to delete and recreate the connector and pair again. The agent runs these steps for you when you ask it to set up C2C.

**Fixed address (optional, needs your own domain on Cloudflare).** Run `cloudflared tunnel login` once and pick the domain, then:

```sh
python3 <skill-dir>/scripts/bridge.py setup -w <repo> --zone example.com --json
```

The bridge creates a named tunnel for that workspace and a DNS record such as `ac2c-claude-<folder>-<id>.example.com`, so the connector URL survives restarts and you pair only once. Existing DNS records are never overwritten. The zone is remembered for new workspaces; `--hostname` picks an exact name and `--quick-tunnel` switches a workspace back. Removing a named tunnel is left to you (`cloudflared tunnel delete`).

The reviewer gets nine read-only tools: workspace info, directory listing, file reading, search, git status, git diff, and the recorded test/execution evidence. `.env`, keys and credentials are always denied, and you can add patterns to a `.c2cignore` file. Tokens, sessions and evidence are stored outside the repository, owner-only, in the platform's app-state directory.

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

Installation is idempotent. If an existing copy differs, the installer stops; `--update` first moves the old copy to `.alpha-c2c-backups/` next to the skills directory (outside it, so it is never loaded twice), then installs. It refuses to replace a directory that is not this skill or a symlink. After installing it reminds you if `cloudflared` is missing. Start a new agent session afterwards. Uninstall by removing only the installed `alpha-c2c-codex` or `alpha-c2c-claude` directory (or `/plugin uninstall alpha-c2c`); bridge state and ChatGPT connectors are separate (`bridge.py unpair` first if you want access revoked).

Machine-specific additions (private workflows, local paths, notes) belong in `~/.config/alpha-c2c/local.md` (or `$XDG_CONFIG_HOME/alpha-c2c/local.md`, or any file named by `$ALPHA_C2C_LOCAL`), not in the installed `SKILL.md`. The skill reads that file at start, and installs or updates never touch it.

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

Tests cover both executors, the bridge's OAuth pairing and MCP tools over real local HTTP, workspace and secret boundaries, non-English messages, malformed checkpoints, repeat prevention recommendations, argument boundaries, multilingual prompts, and non-destructive installation. They do not establish a live ChatGPT login, browser exchange, or end-to-end review. Validate those separately in your own task-owned conversation.

## Provenance

The workflow, helpers and bridge in this repository were written for Alpha C2C in Python. The bridge's command names, tool names and pairing flow follow the design of [codex-with-chatgpt](https://github.com/XiaoDuoYa/codex-with-chatgpt) (MIT), which inspired this project; no source was copied from it, and it is not required.

MIT license. Contributions should keep the entrypoints short, use English documentation, respect the user's language, and include observable tests for behavior changes.
