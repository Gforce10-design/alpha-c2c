# Deliver messages to the web reviewer

Honor the user's selected browser. Otherwise use an available browser integration that can inspect the page and target a specific tab.
For Orca, read its installed `orca-cli` skill and current browser reference:

```sh
orca skills get orca-cli --reference references/browser.md
```

Before declaring browser automation unavailable, probe the installed CLI (`orca skills get orca-cli --reference references/browser.md --json` and task-scoped `orca tab list`). An empty MCP catalog does not mean no CLI browser exists. Use `orca snapshot` and `orca eval`, not `orca browser snapshot/eval`.

Do not mix APIs from different browser integrations or assume every host has Orca.
If no browser integration exists, explain the limitation and provide the prepared message for manual delivery.

- Preserve every unknown composer draft. An inactive tab is not proof nobody owns its draft. Never clear it merely to continue automation.
- Bind an exact task-owned tab ID and conversation URL. Do not interfere with another session's generating conversation.
- Verify the account, Project, and conversation on the actual page. Resume the saved conversation when a checkpoint exists.
- After filling the composer, read it back and verify the task ID and final paragraph. A successful input-tool response is insufficient.
- Use the integration's documented input API. Do not assume a command named `inserttext` targets the browser rather than a terminal.
- After submitting, verify that the message is posted in the conversation. If the outcome is uncertain, inspect before retrying.
- While generation continues, observe the same tab. Do not recreate the chat or resend a message to resolve a wait timeout.
- Distinguish reviewer claims from actual tool results. An assertion that workspace_info was called is not evidence of its result.

Report in the user's language even when page labels differ. Quote a short visible label only when the user needs it to perform a manual action.

## Shell delivery and waiting

With a scoped shell allowlist, issue literal commands separately. Avoid a variable assignment or unrelated `ls`/`echo` commands before an allowed command. A denial of a compound command does not prove all shell tools are unavailable; use an already permitted simpler form without broadening permissions.

For message text, use real newlines with safe single-quoted shell arguments when supported. Do not place generated text inside double-quoted shell command substitution, or replace real newlines with literal backslash-n text. For text that is awkward to quote, a short local Python script can call the documented browser CLI with a subprocess argument list and text read from a file, provided that browser operation is already authorized. Verify the actual composer text after filling.

Network idle does not mean the model has finished generating. While the latest assistant response is pending, wait about 20 seconds between reads (for example a separate `sleep 20`), then inspect that same response. Do not spin on immediately successful network-idle waits or match protocol markers inside the user's own prompt. Require the latest assistant's final verdict and completion state. A transient snapshot failure calls for a bounded retry or lighter page-text read, not a duplicate send or a browser restart affecting other tasks.
