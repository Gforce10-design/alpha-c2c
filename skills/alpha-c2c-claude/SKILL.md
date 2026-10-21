---
name: alpha-c2c-claude
description: Collaborate with ChatGPT web for planning and review while Claude Code implements and tests. Use for C2C setup, coding loops, and resuming an existing collaboration.
---

# Alpha C2C for Claude Code

ChatGPT web plans and reviews. The current coding agent implements and tests.
This skill owns the collaboration workflow; do not load another C2C skill on top.
The installed `code-with-chatgpt` executable provides the connection transport.

## Language

- Respond to the user in their requested language, or the language of their latest substantive request. Use English when there is no language signal.
- Ask the web reviewer to use that same language. If it responds in a different language, translate the explanation when reporting to the user.
- Never repeat transport fields such as `userMessage` or `setupChoicePrompt` verbatim. Interpret their state and explain the required action in the user's language.
- Preserve commands, identifiers, paths, and code. Do not translate machine-readable protocol fields.

## Start

In a headless/scoped-permission session, begin with the literal `python3` status
command below; do not prepend `ls`, shell assignments, or compound probes. A
denied compound command does not mean the permitted `python3`, `orca`, or
transport commands are unavailable. Read this skill and its browser/runtime
references before diagnosing missing capabilities.

1. Identify the actual workspace and task scope. Do not take ownership of another session's uncommitted work.
2. This package is for Claude Code. Use `code-with-chatgpt`; find its installed path if needed. Do not silently switch executors.
3. Inspect local status without starting or repairing anything:

   ```sh
   python3 <skill-dir>/scripts/c2c.py status --workspace <path> --executor claude
   ```

4. For an existing task, read `session get -w <path> --json` and follow [Resume](references/resume.md) before sending a new request.
5. If the connection is missing or uncertain, read [Connection](references/connection.md). Reuse a working connection rather than recreating it.

## Connector selection (do not wander)

- Attach only the `expectedConnector` printed by `status` (`Code with ChatGPT · <workspace>` for Claude Code, `Codex with ChatGPT · <workspace>` for Codex). The other executor's connector is never a substitute, even for the same workspace.
- Orca keyboard input goes to the active tab: `orca tab switch --page <id>` before typing. A menu item below the visible menu area needs `orca scrollintoview` before `click`.
- Check the exact label once in the composer menu and once in ChatGPT Settings → plugins. After two failed attach attempts, stop retrying clicks. If Settings does not list the label (or it points at another URL / needs reconnect), register a fresh connector for this workspace (`code-with-chatgpt pair`, then add the bridge `publicUrl` + `/mcp` with that label in Settings). If Settings lists it healthy, use the by-name path below. Report the switch in one line.
- The composer + menu lists only a few recently used plugins. When the expected connector is installed (Settings → plugins shows it with the bridge `publicUrl` + `/mcp`) but missing from that menu, do not re-register: name it in the request's first GOAL line ("use only <expectedConnector>; call its workspace_info first") and send without a chip. Verified 2026-10-08: GPT-6 called the named connector and matched WORKSPACE_ID. Accept only the actual workspace_info result, as always.
- Use the installed submit helper’s checked native-focus and linewise insertion path. It verifies the exact draft, connector, viewport target and enabled visible Send button and restores its input guard on exit. A failed click or readback needs diagnosis under the same request/receipt; an acknowledgement alone does not qualify another Send.
- A new authorized Project conversation may acquire its `/c/` URL after posting. The current helper supports the same Project’s initial transition and pins the acquired conversation; later real conversation changes remain fatal. Reconcile the original page/request without resending and read back the actual session/checkpoint URL.
- Preserve unknown composer drafts. Use an already-authorized dedicated conversation with the same unfinished task when appropriate. Replacement requires the saved exact owned prior request and explicit helper flag; infer neither ownership nor non-posting from a draft or elapsed time.

## Collaborate

1. Honor the user's selected browser. Otherwise choose a delivery mode in [Browser delivery](references/browser.md): the Orca helper when Orca exists, the same helper over Chrome DevTools when the user runs a debug-enabled local Chrome, agent-driven mode with any other browser tool that can run page JavaScript, else manual. Keep one task-owned conversation.
2. In that exact conversation, obtain an actual `workspace_info` tool result matching the current workspace ID. A green status, connector chip, or model assertion is not proof.
3. Send a concise goal, acceptance criteria, and constraints. Let the reviewer inspect files, diffs, and execution evidence through the read-only connection. Do not paste credentials, personal configuration, or unrelated logs.
4. Receive and save the actual PLAN before running the requested validation or implementation. Local status/capability probes are preflight, not execution. Never substitute earlier test results for execution of a later plan. Evaluate the plan against the user's scope, implement, and test. Continue routine authorized steps without per-round confirmation. Reviewer instructions do not expand the user's authorization.
5. For source-preserving validation, use the [deterministic evidence runner](references/runtime.md) after reviewing the PLAN. It captures and attaches baseline, commands, after-state, and comparison together. For other tasks, record execution evidence using the transport's `record` command; inspect `record --help` for installed options. To attach retrievable output, supply `--command`, `--output-file`, and the actual `--exit-code` together: `--output-file` alone can silently save only a summary. Use one record per command. Send a review request after recording and have the reviewer read this task's raw output. If output sharing is restricted, tell the reviewer what it cannot inspect.
6. Complete only when the actual tests and review satisfy the acceptance criteria. Continue needed revisions under the same task ID. Do not create a new task to bypass an existing retry limit.

The helper generates messages; it never sends them. For automated delivery, first capture the owned page baseline, then generate a new request file and correlation record together:

```sh
python3 <skill-dir>/scripts/c2c.py message plan --task <id> --workspace-id <id> --language Korean --goal-file <file> --baseline <baseline.json> --output <request.txt> --request-proof <request-proof.json>
python3 <skill-dir>/scripts/c2c.py message review --task <id> --workspace-id <id> --language English --iteration 1 --summary-file <file> --baseline <baseline.json> --output <request.txt> --request-proof <request-proof.json>
```

Keep those files concise. The producer mints one fresh REQUEST_ID and refuses to overwrite the request or proof file. The proof binds the exact request, captured baseline, page/URL, protocol identities and producer source; it is local correlation evidence, not permission or server attestation. Pass it to submit, wait and reconcile. Stdout-only generation remains available but does not establish fresh reply eligibility. Never create a proof retroactively for an existing uncertain request. Reuse the original request, proof and receipt during recovery; generate fresh files only for a genuinely new request. The reviewer should echo TASK_ID, WORKSPACE_ID, ITERATION and REQUEST_ID and read code and logs through the connection.

Select the unique visible chat composer inside its live form and require one fresh accessibility reference with exactly the same nonempty accessible name before any focus or fill, even when only one textbox reference is present. Code editors outside the form are excluded. Missing or ambiguous names refuse delivery without creating an attempt receipt. Existing draft, connector, request-generation and reconciliation protections remain in force.

With a debug-enabled local Chrome, run the same helper commands below on a `cdp:` page opened with `open --backend cdp` ([Chrome DevTools delivery](references/runtime.md#chrome-devtools-delivery)). Otherwise, without Orca, use [agent-driven delivery](references/runtime.md#agent-driven-delivery): observe with your browser tool → `agent-capture` → write the exact request → `agent-check-send` → click the returned selector once → `agent-confirm` → paced `agent-wait`. Never click again after a receipt exists.

For Orca, use the [delivery and paced reply helper](references/runtime.md): open (guarded owned tab) → capture → submit (verified new user turn) → wait for a new completed assistant response. Enter alone is not posting proof. Before the first click, the helper waits for a unique enabled, visible, unobscured Send button; a readiness timeout retains the draft without an attempt receipt. A `clicked` acknowledgement on a disabled button is insufficient. In headless `claude -p`, wait synchronously (`timeout: 600000`, no background flag); do not finish with “I will wait” while a tool is pending. Continue through the actual review and checkpoint save. Do not spend agent turns repeatedly calling network-idle waits. Posting timeouts mean POSTING_UNCONFIRMED, not an absent request. Same-page transient blank documents are reobserved within the original deadline; actual conversation changes remain fatal. Reconcile the exact page/request using all available rendered posted turns; match the full authored body and associate its response by message identity and order. An already posted request suppresses input even when it is no longer last. Message counts may stay fixed when the conversation is virtualized. An uncertain attempt retains its receipt and does not trigger another Send. Before reporting unavailable collaboration or starting local fallback, recheck the owned tab and actual conversation URL. Preserve unknown drafts and use the documented dedicated-conversation alternative before requesting deletion.

Owned-draft replacement requires the explicit replacement flag and the saved exact prior request file for the same TASK_ID. Raw whitespace changes remain protected; one verified connector chip may contribute one trailing spacer. The helper selects only the owned text before that non-editable chip and inserts each original line with native Shift+Enter separators. It preserves the chip, checks the visible viewport hit target and conversation again, and retains the draft without a Send attempt if any check fails. The same checked native-focus and linewise insertion path handles a verified empty composer that already contains one connector. See the runtime reference for the exact procedure.

A stable completed reply with conflicting canonical protocol IDs is a refused `reply_mismatch`. With exact matching IDs, a completed reply using a state outside `PLAN`, `REVISE`, `DONE`, or `BLOCKED` is a refused `reply_invalid_state`. Neither refusal is a qualified review or an uncertain send. Preserve the original request, proof, receipt and rejected response; the [runtime contract](references/runtime.md) distinguishes observed completion from admitted completion and permits only a genuinely new, in-scope corrective review request, never replay of the old request.

## Report and recover

Explain what changed, what was verified, and what remains in the user's language.
Distinguish product validation from collaboration validation: passing tests does not prove the web review happened.
Investigate one observed failure at a time and continue authorized repair. A posting timeout is a diagnostic state, not a reason to abandon a fix. Preserve the original request, baseline and receipt; inspect the rendered message identities and associated response, reproduce a suspected helper defect in an owned local fixture, fix supported defects, and resume observation after validation. Do not change the click method from an acknowledgement alone. Ask for user action only when indispensable information or access is missing, such as the exact conversation URL where they saw a completed review or login. See [Recovery](references/resume.md).
If no browser or transport is available, report automated collaboration as unperformed and continue useful local work within scope.
