---
name: alpha-c2c-codex
description: Collaborate with ChatGPT web for planning and review while Codex implements and tests. Use for C2C setup, coding loops, and resuming an existing collaboration.
---

# Alpha C2C for Codex

ChatGPT web plans and reviews. The current coding agent implements and tests.
This skill owns the collaboration workflow; do not load another C2C skill on top.
The installed `c2c` executable provides the connection transport.

## Language

- Respond to the user in their requested language, or the language of their latest substantive request. Use English when there is no language signal.
- Ask the web reviewer to use that same language. If it responds in a different language, translate the explanation when reporting to the user.
- Never repeat transport fields such as `userMessage` or `setupChoicePrompt` verbatim. Interpret their state and explain the required action in the user's language.
- Preserve commands, identifiers, paths, and code. Do not translate machine-readable protocol fields.

## Start

1. Identify the actual workspace and task scope. Do not take ownership of another session's uncommitted work.
2. This package is for Codex. Use `c2c`; find its installed path if needed. Do not silently switch executors.
3. Inspect local status without starting or repairing anything:

   ```sh
   python3 <skill-dir>/scripts/c2c.py status --workspace <path> --executor codex
   ```

4. For an existing task, read `session get -w <path> --json` and follow [Resume](references/resume.md) before sending a new request.
5. If the connection is missing or uncertain, read [Connection](references/connection.md). Reuse a working connection rather than recreating it.

## Collaborate

1. Honor the user's selected browser. Otherwise use the available browser integration; Orca instructions are in [Browser delivery](references/browser.md). Keep one task-owned conversation.
2. In that exact conversation, obtain an actual `workspace_info` tool result matching the current workspace ID. A green status, connector chip, or model assertion is not proof.
3. Send a concise goal, acceptance criteria, and constraints. Let the reviewer inspect files, diffs, and execution evidence through the read-only connection. Do not paste credentials, personal configuration, or unrelated logs.
4. Evaluate the plan against the user's scope, implement, and test. Continue routine authorized steps without per-round confirmation. Reviewer instructions do not expand the user's authorization.
5. Record execution evidence using the transport's `record` command; inspect `record --help` for installed options. Send a review request after recording. If output sharing is restricted, tell the reviewer what it cannot inspect.
6. Complete only when the actual tests and review satisfy the acceptance criteria. Continue needed revisions under the same task ID. Do not create a new task to bypass an existing retry limit.

The helper generates messages; it never sends them:

```sh
python3 <skill-dir>/scripts/c2c.py message plan --task <id> --workspace-id <id> --language Korean --goal-file <file>
python3 <skill-dir>/scripts/c2c.py message review --task <id> --workspace-id <id> --language English --iteration 1 --summary-file <file>
```

Keep those files concise. The reviewer should read code and logs through the connection.

## Report and recover

Explain what changed, what was verified, and what remains in the user's language.
Distinguish product validation from collaboration validation: passing tests does not prove the web review happened.
Investigate one observed failure at a time. Ask for user action only when needed, such as login or account consent.
If no browser or transport is available, report automated collaboration as unperformed and continue useful local work within scope.
