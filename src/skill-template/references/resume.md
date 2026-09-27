# Resume an existing collaboration

Read the task ID, conversation URL, and checkpoint from `session get -w <path> --json`.
Keep the transport's checkpoint as the source of truth instead of creating a competing state store.
Do not claim another session's tab based only on a matching title.

| protocolState | Next action |
|---|---|
| INIT | Wait for the existing plan request. Do not resend INIT. |
| PLAN_RECEIVED | Execute the received plan. |
| EXECUTING | Reconcile files and evidence, then continue unfinished work. |
| EXECUTED_LOCAL | Send the review request only; do not execute again. |
| EXECUTED_SENT | Wait in the same conversation. Do not resend execution results. |
| DONE | Report verified completion. |
| BLOCKED | Inspect the recorded cause and recover within the same scope if possible. |
| Unknown | Inspect the saved record; do not reset to INIT. |

A missing checkpoint does not prove a new task. Inspect the conversation and local results before sending anything.
A timeout does not establish that a message was not sent.
If a conversation is lost, hand off the original task ID, stage, completed work, and open issues to a new task-owned chat, then verify the workspace again.

Summarize the next action from a saved JSON response:

```sh
python3 <skill-dir>/scripts/c2c.py resume --session-file <response.json>
```

Supported checkpoint updates in the compatible transport:

```sh
__BRIDGE__ session set -w <path> --task <id> --protocol-state EXECUTING --waiting-for none
__BRIDGE__ session set -w <path> --task <id> --protocol-state EXECUTED_LOCAL --waiting-for none
__BRIDGE__ session set -w <path> --task <id> --protocol-state EXECUTED_SENT --waiting-for GPT_REVIEW
```

Use EXECUTING immediately before execution, EXECUTED_LOCAL after recording real results, and EXECUTED_SENT only after the message is visibly posted.
