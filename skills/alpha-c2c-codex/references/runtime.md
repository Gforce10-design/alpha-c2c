# Deterministic waiting and complete validation evidence

These helpers automate mechanical operations, not permission or review decisions.
Use the installed skill directory below. Invoke literal Python commands; no shell
assignments or compound commands are needed.

## Chrome DevTools delivery

The Orca helper commands also drive a regular Chrome over the Chrome DevTools
Protocol. The user starts Chrome themselves with a dedicated profile; Chrome
does not allow remote debugging on the default profile, and while the port is
open any local program can control that browser. Never point it at your normal
profile, and close that Chrome when the task ends.

```sh
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --remote-debugging-address=127.0.0.1 --remote-debugging-port=9222 --user-data-dir="$HOME/.c2c-chrome"
```

On Linux use `google-chrome`, on Windows `chrome.exe`, with the same flags. The
user signs in to ChatGPT in that window by hand and completes any verification
there; the helper never handles logins or challenges. A different port is set
with `C2C_CDP_ENDPOINT=http://127.0.0.1:<port>`; non-loopback endpoints are refused.

Open the owned tab with `--backend cdp` (no worktree), then use the returned
`cdp:<target>` page with the same `capture`, `submit`, `reconcile` and `wait`
commands as in the Orca section:

```sh
python3 <skill-dir>/scripts/wait_reply.py open --backend cdp --url https://chatgpt.com/ --output <outside-repo>/opened.json
```

The page is read from the DOM, so the AX/DOM identity cross-check of the Orca
path is unavailable. Guarded tabs, native insertion, ready-button checks,
receipts and paced waits are unchanged.

## Agent-driven delivery

Use this without Orca, with any browser tool that can run JavaScript in a specific
ChatGPT tab and click. The helper never touches the browser here; it only judges
observations the agent saves. Use one tab for the task and a stable page label
such as `agent:<tab id>` for every command.

1. Print the read-only observation expression once:

   ```sh
   python3 <skill-dir>/scripts/wait_reply.py observe-expression
   ```

   Each observation = run that exact expression in the owned tab and save the
   returned JSON string, unchanged, to a new file. Never edit or reuse a file.

2. Capture the baseline from a fresh observation. Exit code 2 with `unknownDraft`
   means someone's draft is in the composer: preserve it and stop, as in the Orca flow.

   ```sh
   python3 <skill-dir>/scripts/wait_reply.py agent-capture --observation <obs-0.json> --page-id agent:<tab> --output <outside-repo>/baseline.json
   ```

3. Generate the request and proof with `c2c.py message ... --baseline <baseline.json>`.
   Put exactly that text in the composer with your browser tool (attach the
   expected connector first if needed). Do not press Enter.
4. Observe again and run the pre-click check. Only `READY_TO_CLICK` (exit 0)
   permits one click on the returned `selector`; it writes the ATTEMPTED receipt
   first. Any other result means do not click; correct the draft and observe again.

   ```sh
   python3 <skill-dir>/scripts/wait_reply.py agent-check-send --baseline <baseline.json> --message-file <request.txt> --request-proof <request-proof.json> --receipt <outside-repo>/receipt.json --observation <obs-1.json>
   ```

5. Click that selector once. After that, never click Send or press Enter again
   for this request, even if posting looks absent.
6. Confirm posting by passing every observation taken since the click, oldest
   first. Exit 3 (`PENDING`) means observe again after `nextPollSeconds` and rerun
   with all of them. A timeout records POSTING_UNCONFIRMED; reconcile, never resend.

   ```sh
   python3 <skill-dir>/scripts/wait_reply.py agent-confirm --baseline <baseline.json> --message-file <request.txt> --request-proof <request-proof.json> --receipt <receipt.json> --observation <obs-2.json> <obs-3.json>
   ```

7. Wait for the reply the same way, observing about every 20 seconds. Observations
   must be in order and at least 10 seconds apart; two identical completed reads
   are required. Exit 0 saves the reply to `--output`; exit 2 with
   `reply_mismatch`/`reply_invalid_state` is a refusal as described below.

   ```sh
   python3 <skill-dir>/scripts/wait_reply.py agent-wait --baseline <baseline.json> --task <id> --message-file <request.txt> --request-proof <request-proof.json> --receipt <receipt.json> --output <outside-repo>/reply.json --observation <obs-4.json> <obs-5.json>
   ```

Compared with the Orca helper this mode has no AX/DOM cross-check and no keyboard
input guard, and the helper cannot tell whether a file is a genuine, unedited
observation. Report it as agent-driven delivery.

## Orca reply wait

Open a new task-owned ChatGPT tab through the helper, not raw `orca tab create`:

```sh
python3 <skill-dir>/scripts/wait_reply.py open --url https://chatgpt.com/ --worktree <browser-worktree> --output <outside-repo>/opened.json
```

A new ChatGPT tab autofocuses its composer, so the owner's live keystrokes aimed at another
window can land there; the new-chat draft is shared by every chatgpt.com tab. `open` blocks
trusted keyboard, IME, paste, and drop input on the owned page as soon as the composer exists
and records the draft present at that moment. Tool fills still work. `capture`, `wait`,
`reconcile`, and `submit` re-arm the guard on each observation (reloads drop it); `submit`
releases it only after the unknown-draft check, just for fill/click. A nonzero `open` exit with
`unknownDraft: true` means a draft existed before the guard: keep it, report its exact text
from `draftAtOpen` or `protectedDraft`, and do not clear it. A 1–3 second gap between tab
creation and the guard remains; the draft check still protects it.

A selected connector appears as a non-editable chip in the composer and as a mention pill in
the posted message. It is an attachment, not a draft or request text: composer and posted-body
reads exclude it and report it separately. When a chip is present, `submit` inserts the request
before it instead of filling (a fill would delete the chip). It proves a fresh editable hit target inside the visible viewport, focuses
natively through Orca, then positions the caret and inserts every original line
with native `Shift+Enter` separators. It refuses to send if editor text, the chip,
the captured conversation or readback changes. An empty chip-bearing editor and
a supported exact owned-prefix replacement use the same line-preserving input
path; a prepared matching request still needs no input. Select the connector before `submit`; do not send manually when `submit`
refuses — report its error instead.

Before sending each PLAN or review request, capture the exact owned page:

```sh
python3 <skill-dir>/scripts/wait_reply.py capture --page <page-id> --output <outside-repo>/baseline.json
```

After capture, prepare the message and correlation record before any delivery:

```sh
python3 <skill-dir>/scripts/c2c.py message review --task <task-id> --workspace-id <workspace-id> --language Korean --iteration <n> --summary-file <summary.md> --baseline <outside-repo>/baseline.json --output <outside-repo>/request.txt --request-proof <outside-repo>/request-proof.json
```

Use `message plan --goal-file <goal.md>` for PLAN preparation. The three file flags
are all-or-none. The producer mints a fresh REQUEST_ID once, binds the exact UTF-8
message SHA, canonical baseline SHA, owned page/URL, TASK_ID, WORKSPACE_ID,
ITERATION and producer SHA, and creates the request and proof exclusively. It never
sends. Existing files and any partial creation evidence are preserved on failure;
inspect that failure before preparing a genuinely new request into distinct paths.
Stdout-only generation is compatible but provides no qualified creation proof.
The record is local correlation evidence, not permission, a signed server receipt
or an authentication guarantee. Do not copy a previous request ID into a new proof
or retroactively qualify an uncertain historical attempt.

Submit the prepared message file through the helper (only this subcommand sends):

```sh
python3 <skill-dir>/scripts/wait_reply.py submit --baseline <outside-repo>/baseline.json --message-file <request.txt> --request-proof <outside-repo>/request-proof.json --receipt <outside-repo>/sent.json
```

Select the unique visible chat composer inside its live form and require one fresh accessibility reference with exactly the same nonempty accessible name before any focus or fill, even when only one textbox reference is present. Code editors outside the form are excluded. Missing or ambiguous names refuse delivery without creating an attempt receipt. Existing draft, connector, request-generation and reconciliation protections remain in force.

It fills the exact accessible composer, verifies its full text, waits up to 15 seconds
for the live form's unique Send button to be enabled, visible and unobscured, then
clicks its semantic selector once. Each readiness observation checks conversation,
full draft and connector identity again. Orca may acknowledge a disabled button as
`clicked` while no click event occurs. A readiness failure preserves the draft and
creates **no attempt receipt**; investigate readiness before another submit, keeping
the same baseline and request. An attempted send is handled only by reconciliation.
The input guard is rearmed on submit exit, including failed readback/readiness checks. An exact request already in the composer is reused without refilling (including its connector chip). If an obscured hit target may reflect stale background layout, the helper makes one native capture of that owned page, discards the image, and checks conversation, draft and hit target again. Capture alone never makes a button eligible.
Posting confirmation requires the full request in a **new authored user turn**, with validated producer correlation evidence, not in the composer. Absence from a partial baseline ID list, count changes, a bare UUID or a historical `posted: true` flag cannot establish freshness. With supported DOM structure, the helper searches every rendered posted user turn and records its stable message identity. A unique exact request already present prevents input and Send, including a request present in the baseline; that observation alone does not prove fresh posting or a new review. Enter can insert a newline instead of sending. Existing differing drafts
are preserved. Replacing your own prior request requires both `--replace-owned-draft`
and `--owned-draft-file <previously-saved-request>`: the current text must match
that saved request and carry the same TASK_ID. Matching is exact editor text, with
only one documented trailing separator allowed for a single trailing connector
chip; collapsed whitespace is not ownership proof. For that supported layout,
the helper checks the captured conversation before editing, focuses only an
editable point proven by a fresh viewport-intersection hit test, then selects the
full owned prefix while excluding every noneditable node. Native `Shift+Enter`
preserves each original request newline during insertion. Interspersed chips,
text after a chip, changed ownership, focus/selection failures and incomplete
readback retain the draft and create no Send attempt. This does not permit
resending an uncertain old request: a genuinely new scope uses separate freshly
prepared request/proof/receipt files and preserves the old uncertainty. Do not create an ownership file from
an unknown draft. Tab inactivity, a task-owned page, or a short draft is never
proof of ownership. Leave unknown drafts untouched; use a new authorized dedicated
conversation and carry forward the same unfinished task ID. An uncertain click receipt prevents duplicate sends. The default submit observation window is 120 seconds, sampled every 20 seconds; it does not retry the click. An expired window yields `posted: null`, `status: POSTING_UNCONFIRMED` and exit 2, not proof of non-posting. The receipt retains the request SHA and owned page. Repeating submit with that receipt only reconciles and cannot send again.

Recheck a delayed send without filling, clicking, navigating, or opening another conversation:

```sh
python3 <skill-dir>/scripts/wait_reply.py reconcile --baseline <outside-repo>/baseline.json --message-file <request.txt> --request-proof <outside-repo>/request-proof.json --receipt <outside-repo>/sent.json
```

Use `--timeout 0` for one observation. If still uncertain, retain the same receipt/page/request and investigate the concrete failure under [Recovery](resume.md). Inspect every available rendered authored user turn for the exact request, and the response associated with that turn; the latest message alone can miss an earlier posted request. Preserve the observed IDs, URL and completion evidence. Continue supported helper repair and local regression testing within the authorized scope instead of treating uncertainty as a terminal result. Do not infer non-posting from elapsed time, an unchanged message count, or an empty composer. Do not send duplicates with Enter, JavaScript clicks, new requests, or a new conversation to resolve an uncertain send.

A protected unrelated draft is different from an uncertain send. Use an already-authorized dedicated conversation with the same unfinished task ID if appropriate; preserve the draft instead of asking to delete it when that alternative is available. A new Project page can acquire its `/c/` URL asynchronously. The helper accepts the same Project's initial URL transition and pins the acquired conversation during polling; checkpoint the actual URL after confirmation and read back both session and checkpoint URLs.

After confirmed posting, wait:


```sh
python3 <skill-dir>/scripts/wait_reply.py wait --baseline <outside-repo>/baseline.json --task <task-id> --message-file <request.txt> --request-proof <outside-repo>/request-proof.json --receipt <outside-repo>/sent.json --output <outside-repo>/reply.json
```

This single process polls every 20 seconds for up to 10 minutes. In headless
`claude -p`, invoke Bash with `timeout: 600000` and keep `run_in_background` false.
Do not end the turn with a waiting message: a final response exits the headless
process and no later tool notification can resume it. If a tool backgrounds itself,
observe its task output until it completes before returning a final response.
Do not launch competing pollers or replace this with network-idle loops. A shell
timeout means inspect the process before starting another. The wait subcommand never sends, reloads, changes tabs,
or decides acceptance. Read the saved response and inspect actual connector tool
results separately. A returned BLOCKED is a completed response, not task success.
A timeout retains the request: reconcile the same page/receipt and reuse the original baseline, never recapture a pending request as a new baseline. The waiter does not infer non-posting from unchanged counts. Passing `--message-file` ties the reply to the exact posted request, even when a later unrelated turn is visible. The optional `--receipt` validates the same page/request SHA. A pinned user-message ID is usable for reply admission only when the receipt retains valid original producer correlation evidence; bare legacy positive flags or IDs remain historical evidence and do not qualify completion. Qualified pins are immutable. A missing virtualized turn is not a completed reply, and an uncertain legacy receipt cannot acquire a new proof during recovery. For a legacy attempt, omit `--request-proof` and continue observation only; preserve its request, baseline and receipt.
It requires validated original request-generation bindings, an exact unique authored user body, an assistant response associated with that request, task/STATE markers, completion evidence belonging to that response, and two stable reads of the same request/response identity and body. Supported posted-turn DOM uses stable message IDs, including ChatGPT search-unit IDs, and an assistant response before the next user turn. Distinct exact request matches or unprovable association remain unconfirmed. TASK_ID, WORKSPACE_ID, ITERATION and REQUEST_ID must match the prepared request; a previous iteration DONE is not a fresh review. Turn counts are supplementary evidence because the UI may render a fixed-size recent-turn window. Posting checks read only the posted user-message DOM body when available, preserving inert link text and excluding toolbar/timing labels; they never use composer text as posting proof. If this structure is unavailable, the accessibility body is a conservative fallback and omitted links leave posting unconfirmed. Marker checks decode visible StaticText so bold `STATE:` and separate `DONE` nodes work; save/read the raw response snapshot too, since tables and headings need not be StaticText. Unsupported page language/structure fails closed; inspect the
same page instead of resending. Capture each new request into a distinct file.


A separate terminal `type: reply_mismatch` is available only after the original full request-generation bindings, page/conversation identity, exact authored user body, confirmed receipt's pinned user ID and unique associated assistant identity pass. The completed assistant must be explicitly non-generating and have one canonical authored header containing TASK_ID, WORKSPACE_ID, ITERATION, REQUEST_ID and one explicit STATE, with an explicit identity conflict. Two consecutive reads at the normal polling interval must match page/URL, user/reply IDs, full authored bodies and expected/observed conflict. Blank/read errors, renewed generation, incomplete output, body or association changes reset negative stability; actual identity drift retains the existing fail-closed stop. Missing or quoted/body-only headers remain unqualified, and a corrected valid reply still needs the unchanged positive two-read rule. Baseline body equality cannot suppress a precisely associated explicit conflict.

A separate `type: reply_invalid_state` uses the same original generation bindings, pinned user ID, unique canonical authored header, explicit non-generation, response completion and two consecutive stable full reads. All four IDs must match exactly, and one authored STATE must be outside `PLAN`, `REVISE`, `DONE`, or `BLOCKED`. It preserves the rejected response and observed state, exits 2, and never sends. An explicit ID conflict has priority even when STATE is also invalid. Missing, duplicate, quoted or ambiguous headers remain unqualified; ordinary read errors, blank pages, generation and body changes reset stability. `EXECUTING` is a transport checkpoint state, not a terminal review reply state. Instructions requesting a review must use the four supported reply states.

The refused result preserves the full original reply/raw snapshot, original generation proof and expected/observed identities under `rejectedResponse` and the result's proof fields. `rejectedResponse.complete=true` records observed completion; top-level `ok=false` and `complete=false` mean the reply was not admitted for this request. The CLI writes that refused result and exits 2 without sending or modifying receipts. Codex and Claude each retain their own unchanged adjacent producer and producer-name/hash bindings; an actual proof from one provider must never be relabelled or grafted into the other's receipt.

Retain the completed bad reply and all prior request/proof/receipt files. If correction is within the owner's existing scope, record a genuinely distinct corrective review request in the current task/iteration journal, explaining the mismatch and current evidence; obtain a fresh baseline and new provider-produced REQUEST_ID/proof without resubmitting the old body. Preserve task identity, prior iterations, consumed budgets and draft protection. A mismatch verdict itself grants no Send authority. Uncertain sends remain observation-only and must not be converted into fresh requests or new conversations to evade reconciliation.

A same-page empty `about:blank` (or an empty snapshot at the pinned URL) is a transient observation, not a conversation-change verdict. The helper observes until the original deadline, never sends or navigates to repair it, and keeps POSTING_UNCONFIRMED on timeout. A real page ID, origin, or conversation change still stops immediately. Blank observations reset response stability; completion still requires two subsequent matching full reads. An existing receipt makes submit observe before any composer read. If the posted body differs from the saved request, retain that mismatch and original receipt; inspect the actual body rather than silently trimming characters or resending.

## Source-preserving validation

After receiving and evaluating the PLAN, save it outside the repository, and save
the matching transport checkpoint as PLAN_RECEIVED (or EXECUTING when resuming).
Write the commands you reviewed as JSON argv arrays, for example:

```json
[["python3", "-B", "scripts/build.py", "--check"],
 ["python3", "-B", "-m", "unittest", "discover", "-s", "tests", "-v"]]
```

Run with a new evidence directory outside the tested workspace:

```sh
python3 <skill-dir>/scripts/evidence.py --workspace <repo> --task <task-id> --iteration <n> --plan-file <actual-plan-file> --spec <commands.json> --output <outside-repo>/execution --bridge c2c
```

It requires the current matching PLAN/EXECUTING checkpoint, records HEAD, branch
symbolic ref (or detached), all refs, full index entries, the raw `.git/index`
file SHA-256, status, staged/unstaged diff, and hashes of tracked/untracked
nonignored files. Its git reads use a private index copy, so observing never
rewrites `.git/index`; an index change during observation fails the snapshot. It executes argv without a shell with bytecode disabled; captures real exit
codes; compares the after-state; and attaches every command plus before/after/
comparison as separate retrievable transport outputs. Dirty baselines are valid.
State these covered items in the PLAN request so the reviewer does not ask for a
separate manual collection. Symlinks are recorded without reading their targets. Submodules need a task-specific
verifier and are rejected. Ignored files are outside this helper's preservation
claim. Only run commands and share outputs already within the owner's scope; a
web plan is not authority. For implementation tasks where change is intended,
use normal scoped evidence collection instead.

Read the record receipts and have the reviewer retrieve every relevant raw output
(including before/after/comparison). Transport exit zero alone is not proof the
reviewer accessed the outputs. If a record fails, inspect its receipt and the
transport before retrying; do not silently duplicate an uncertain write. A failed
command or preservation comparison remains failure even when upload succeeded.
Never manufacture a prior baseline after execution. Never mark DONE from helper
success alone; require actual final review and consistent checkpoint readback.

A completed `STATE: REVISE` is a received review that requires correction, not a task completion. The reply helper accepts PLAN, REVISE, DONE and BLOCKED only after matching the exact request, task and conversation and observing two stable completed responses. Unknown states, stale replies, partial replies and generation in progress remain ineligible.


Qualified inline-code observation: a posted user DOM may render single-backtick inline code without its authored markers. The observer retains the raw visible body and builds an additional representation only from actual simple inline code nodes (no pre/code block, nested child, backtick or newline; at most64). That representation must still match the entire original prepared request; generic backtick removal and draft normalization are unsupported. Keep the original baseline, producer proof and uncertain receipt, repair the observer with rejection tests, and reconcile the original posted message before waiting. Missing DOM qualification, changed content, ambiguous message IDs and code-block formatting remain unconfirmed and grant no resend. This reconstructs rendered formatting, not raw server wire bytes.
