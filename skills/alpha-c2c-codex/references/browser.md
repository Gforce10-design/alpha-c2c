# Deliver messages to the web reviewer

Honor the user's selected browser. Otherwise use an available browser integration that can inspect the page and target a specific tab.
For Orca, read its installed `orca-cli` skill and current browser reference:

```sh
orca skills get orca-cli --reference references/browser.md
```

Do not mix APIs from different browser integrations or assume every host has Orca.
If no browser integration exists, explain the limitation and provide the prepared message for manual delivery.

- Bind an exact task-owned tab ID and conversation URL. Do not interfere with another session's generating conversation.
- Verify the account, Project, and conversation on the actual page. Resume the saved conversation when a checkpoint exists.
- After filling the composer, read it back and verify the task ID and final paragraph. A successful input-tool response is insufficient.
- Use the integration's documented input API. Do not assume a command named `inserttext` targets the browser rather than a terminal.
- After submitting, verify that the message is posted in the conversation. If the outcome is uncertain, inspect before retrying.
- While generation continues, observe the same tab. Do not recreate the chat or resend a message to resolve a wait timeout.
- Distinguish reviewer claims from actual tool results. An assertion that workspace_info was called is not evidence of its result.

Report in the user's language even when page labels differ. Quote a short visible label only when the user needs it to perform a manual action.
