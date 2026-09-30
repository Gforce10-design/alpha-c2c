# Deterministic waiting and complete validation evidence

These helpers automate mechanical operations, not permission or review decisions.
Use the installed skill directory below. Invoke literal Python commands; no shell
assignments or compound commands are needed.

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

Before sending each PLAN or review request, capture the exact owned page:

```sh
python3 <skill-dir>/scripts/wait_reply.py capture --page <page-id> --output <outside-repo>/baseline.json
```

Submit the prepared message file through the helper (only this subcommand sends):

```sh
python3 <skill-dir>/scripts/wait_reply.py submit --baseline <outside-repo>/baseline.json --message-file <request.txt> --receipt <outside-repo>/sent.json
```

It fills the exact accessible composer, verifies its full text, clicks the visible
Send button, and requires the full request in a **new user turn**, not in the
composer. Enter can insert a newline instead of sending. Existing differing drafts
are preserved. Replacing your own prior request requires both `--replace-owned-draft`
and `--owned-draft-file <previously-saved-request>`: the current text must match
that saved request and carry the same TASK_ID. Do not create an ownership file from
an unknown draft. Tab inactivity, a task-owned page, or a short draft is never
proof of ownership. Leave unknown drafts untouched; use a new authorized dedicated
conversation and carry forward the same unfinished task ID. An uncertain click receipt prevents duplicate sends. The default submit observation window is 120 seconds, sampled every 20 seconds; it does not retry the click. An expired window yields `posted: null`, `status: POSTING_UNCONFIRMED` and exit 2, not proof of non-posting. The receipt retains the request SHA and owned page. Repeating submit with that receipt only reconciles and cannot send again.

Recheck a delayed send without filling, clicking, navigating, or opening another conversation:

```sh
python3 <skill-dir>/scripts/wait_reply.py reconcile --baseline <outside-repo>/baseline.json --message-file <request.txt> --receipt <outside-repo>/sent.json
```

Use `--timeout 0` for one observation. If still uncertain, retain the same receipt/page/request and report uncertainty. Before declaring collaboration unavailable or using local fallback, inspect that exact task-owned tab again, including its latest posted user body, current conversation URL and completed assistant reply. Do not infer non-posting from elapsed time, an unchanged message count, or an empty composer. Do not send duplicates with Enter, JavaScript clicks, new requests, or a new conversation to resolve an uncertain send.

A protected unrelated draft is different from an uncertain send. Use an already-authorized dedicated conversation with the same unfinished task ID if appropriate; preserve the draft instead of asking to delete it when that alternative is available. A new Project page can acquire its `/c/` URL asynchronously. The helper accepts the same Project's initial URL transition and pins the acquired conversation during polling; checkpoint the actual URL after confirmation and read back both session and checkpoint URLs.

After confirmed posting, wait:


```sh
python3 <skill-dir>/scripts/wait_reply.py wait --baseline <outside-repo>/baseline.json --task <task-id> --message-file <request.txt> --output <outside-repo>/reply.json
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
A timeout retains the request: reconcile the same page/receipt and reuse the original baseline, never recapture a pending request as a new baseline. The waiter does not infer non-posting from unchanged counts. Passing `--message-file` ties the reply to the exact latest posted request.
It requires a newly observed posted user body, a changed assistant response associated with that request, task/STATE markers, a completion control, and two stable reads. Turn counts are supplementary evidence because the UI may render a fixed-size recent-turn window. Posting checks read only the posted user-message DOM body when available, preserving inert link text and excluding toolbar/timing labels; they never use composer text as posting proof. If this structure is unavailable, the accessibility body is a conservative fallback and omitted links leave posting unconfirmed. Marker checks decode visible StaticText so bold `STATE:` and separate `DONE` nodes work; save/read the raw response snapshot too, since tables and headings need not be StaticText. Unsupported page language/structure fails closed; inspect the
same page instead of resending. Capture each new request into a distinct file.


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
python3 <skill-dir>/scripts/evidence.py --workspace <repo> --task <task-id> --iteration <n> --plan-file <actual-plan-file> --spec <commands.json> --output <outside-repo>/execution --bridge __BRIDGE__
```

It requires the current matching PLAN/EXECUTING checkpoint, records HEAD, full
index, status, staged/unstaged diff, and hashes of tracked/untracked nonignored
files; executes argv without a shell with bytecode disabled; captures real exit
codes; compares the after-state; and attaches every command plus before/after/
comparison as separate retrievable transport outputs. Dirty baselines are valid.
Symlinks are recorded without reading their targets. Submodules need a task-specific
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
