# Resume an existing collaboration

Read the task ID, conversation URL, and checkpoint from `session get -w <path> --json`.
Keep the bridge's checkpoint as the source of truth instead of creating a competing state store.
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

Supported checkpoint updates in the bundled bridge:

```sh
python3 <skill-dir>/scripts/bridge.py session set -w <path> --task <id> --protocol-state EXECUTING --waiting-for none
python3 <skill-dir>/scripts/bridge.py session set -w <path> --task <id> --protocol-state EXECUTED_LOCAL --waiting-for none
python3 <skill-dir>/scripts/bridge.py session set -w <path> --task <id> --protocol-state EXECUTED_SENT --waiting-for GPT_REVIEW
```

Use EXECUTING immediately before execution, EXECUTED_LOCAL after recording real results, and EXECUTED_SENT only after the message is visibly posted.

After saving a new conversation or a completed checkpoint, read `session get` back. The top-level session URL and `checkpoint.chatUrl` must both identify the same task-owned conversation. A mismatch is a persistence defect, not completion evidence; preserve the record and correct it within the task's scope. Never clear an unfinished checkpoint or reset its task ID to evade a retry limit.

## Recover a delivery or observation defect

A timeout records uncertainty. It does not require abandoning authorized repair, a new task ID, or an arbitrary consultation-count extension. Keep the original immutable request file, pre-send baseline, owned page/conversation, producer correlation record if one was originally created, and attempt receipt. Do not regenerate the nonce or add a proof to an old uncertain attempt.

1. Reconcile that receipt before reading or changing the composer. Search all available rendered authored user turns for the full request; do not use a composer, tool quotation, count change or request-ID substring as posting proof. A unique exact request already present suppresses further input/Send even if its freshness remains unknown.
2. If found, retain its observed message ID and inspect only the associated assistant response before the next user turn. Reply eligibility requires validated original producer correlation evidence; count or baseline-ID absence does not qualify freshness. Bare legacy positive pins remain historical evidence and observation-only for completion. A qualified pin is immutable. Verify task and supplied iteration/workspace identity, completion, two stable observations, and actual workspace/tool evidence required by the task. A historical DONE or a later unrelated answer does not complete the request.
3. If the user saw a completed review elsewhere, compare that exact conversation URL and request identity through an authorized read-only source. Ask for the missing URL only when it cannot be obtained from current task records; keep useful local repair moving independently.
4. Reproduce a suspected helper defect against the observed page structure in an owned local fixture. Preserve failed evidence, make the smallest supported correction, run regressions, review the diff, then resume the original receipt observation. Tool acknowledgements alone do not justify replacing the Send method. Keep unknown drafts and connector chips intact.
5. Report the actual outcome: posted and associated completed review, posted but pending, or posting unconfirmed. A confirmed receipt retains its positive historical posting observation through temporary virtualization. Only a pin with valid original producer correlation evidence qualifies reply admission; neither it nor a bare old positive flag proves a currently completed reply. Visible identity contradictions remain diagnostic failures.

Readiness failure before any attempted Send is different: no attempt receipt was created, so after correcting and rechecking readiness, the same request can proceed normally. After an uncertain attempted Send, preserve observation-only recovery; do not issue a second Send merely because a window expired or the current rendered history omits the request. A local skill fix and a live web review are separate completion claims.
