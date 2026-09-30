# Deterministic waiting and complete validation evidence

These helpers automate mechanical operations, not permission or review decisions.
Use the installed skill directory below. Invoke literal Python commands; no shell
assignments or compound commands are needed.

## Orca reply wait

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
conversation and carry forward the same unfinished task ID. An uncertain click receipt prevents automatic duplicate
sends; inspect before another attempt. After confirmed posting, wait:


```sh
python3 <skill-dir>/scripts/wait_reply.py wait --baseline <outside-repo>/baseline.json --task <task-id> --output <outside-repo>/reply.json
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
An unposted-request error requires inspecting the composer and using submit; it is not a generation timeout. A generation timeout retains the request: reuse the same baseline to wait again.
It requires a new assistant turn, task/STATE markers, a completion control, and
two stable reads. Unsupported page language/structure fails closed; inspect the
same page instead of resending. Capture each new request into a distinct file.

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
