#!/usr/bin/env python3
"""Small, read-only transport adapter and language-aware message generator."""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

NEXT = {
    "INIT": ("wait_for_plan", "Wait for the existing plan request; do not resend INIT."),
    "PLAN_RECEIVED": ("execute_plan", "Execute the received plan."),
    "EXECUTING": ("continue_execution", "Reconcile local results and continue unfinished work."),
    "EXECUTED_LOCAL": ("send_review", "Send the recorded result for review; do not execute again."),
    "EXECUTED_SENT": ("wait_for_review", "Wait in the same conversation; do not resend results."),
    "DONE": ("report_completion", "Report the verified result."),
    "BLOCKED": ("inspect_blocker", "Inspect the saved blocker before taking another action."),
}


class InputError(ValueError):
    pass


def object_value(value: object) -> dict:
    if not isinstance(value, dict):
        raise InputError("Expected a JSON object.")
    return value


def nonnegative_int(value: object) -> bool:
    return type(value) is int and value >= 0


def status_summary(data: object) -> dict:
    """Allowlist fields; never forward arbitrary provider messages or credentials."""
    d = object_value(data)
    running = d.get("running")
    if running is False:
        state, message = "stopped", "The bridge is not running. No changes were made."
    elif running is True and d.get("ok") is True:
        count = d.get("tokenCount")
        if not nonnegative_int(count):
            state, message = "authentication_unknown", "The bridge is running; authentication is unconfirmed."
        elif count == 0:
            state, message = "authentication_missing", "The bridge is running without an authorized connection."
        else:
            state, message = "web_verification_required", "Local authentication exists. Verify workspace_info in the intended web conversation."
    else:
        state, message = "unknown", "The bridge status could not be confirmed. No changes were made."
    return {"state": state, "message": message, "ready": False}


def resume_summary(data: object) -> dict:
    d = object_value(data)
    if "session" in d:
        if d.get("ok") is not True:
            raise InputError("The transport did not return a successful session response.")
        d = d["session"]
    if d is None:
        return {"action": "inspect_history", "message": "No saved session. Inspect conversation history before starting a new task."}
    d = object_value(d)
    checkpoint = d.get("checkpoint")
    if checkpoint is None:
        return {"action": "inspect_history", "message": "No checkpoint. Inspect the existing conversation and local results."}
    checkpoint = object_value(checkpoint)
    state = checkpoint.get("protocolState")
    if not isinstance(state, str) or state not in NEXT:
        raise InputError("Unknown checkpoint state. Inspect the saved record; do not reset it.")
    iteration = checkpoint.get("iteration")
    if not nonnegative_int(iteration):
        raise InputError("Checkpoint iteration must be a non-negative integer.")
    action, message = NEXT[state]
    return {"state": state, "iteration": iteration, "action": action, "message": message}


def identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
        raise InputError("Use a single-line task/workspace identifier (letters, digits, _, ., :, -).")
    return value


def language_name(value: str) -> str:
    if not value.strip() or len(value) > 64 or not all(c.isalpha() or c in " -()" for c in value):
        raise InputError("Use a short language name, such as English, Korean, or Brazilian Portuguese.")
    return value.strip()


def message(kind: str, task: str, workspace_id: str, language: str, iteration: int, content: str) -> str:
    task, workspace_id, language = identifier(task), identifier(workspace_id), language_name(language)
    if kind not in {"plan", "review"}:
        raise InputError("Unknown message kind.")
    if not nonnegative_int(iteration):
        raise InputError("Iteration must be a non-negative integer.")
    if not content.strip() or len(content) > 8000:
        raise InputError("Provide a concise, non-empty summary of at most 8000 characters.")
    state = "INIT" if kind == "plan" else "EXECUTED"
    instruction = (
        "Inspect the connected workspace. Return STATE: PLAN with rationale, actions, tests, and acceptance criteria."
        if kind == "plan" else
        "Independently inspect the workspace diff and available execution evidence. Return STATE: DONE only if the acceptance criteria are met; otherwise return STATE: PLAN with required changes, or STATE: BLOCKED with the observed blocker."
    )
    return (
        f"[C2C]\nSTATE: {state}\nTASK_ID: {task}\nITERATION: {iteration}\n"
        f"WORKSPACE_ID: {workspace_id}\nRESPONSE_LANGUAGE: {language}\n\n"
        f"{instruction}\nFirst obtain workspace_info and verify WORKSPACE_ID. "
        "Report a mismatch or inaccessible evidence instead of assuming success.\n"
        f"Write explanations in {language}; preserve code, paths, and protocol identifiers.\n\n"
        f"{'GOAL' if kind == 'plan' else 'RESULT_SUMMARY'}:\n{content.strip()}\n"
    )


def query_status(workspace: Path, executable: str) -> dict:
    workspace = workspace.expanduser().resolve()
    if not workspace.is_dir():
        raise InputError("Workspace directory does not exist.")
    program = shutil.which(executable)
    if not program:
        raise InputError("Transport executable not found. Install it or pass --bridge with its path.")
    try:
        result = subprocess.run(
            [program, "status", "-w", str(workspace), "--json"],
            capture_output=True, text=True, encoding="utf-8", timeout=15, check=False,
        )
    except subprocess.TimeoutExpired:
        raise InputError("Status query timed out. No setup or repair was attempted.") from None
    if result.returncode:
        raise InputError(f"Status query failed (exit {result.returncode}). Inspect the transport locally; its raw output was not forwarded.")
    try:
        return status_summary(json.loads(result.stdout))
    except (json.JSONDecodeError, InputError):
        raise InputError("Transport returned an unsupported status response. Its raw output was not forwarded.") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status", help="Inspect local bridge status without setup or repair")
    status.add_argument("--workspace", type=Path, required=True)
    status.add_argument("--executor", choices=("codex", "claude"), default="codex")
    status.add_argument("--bridge", help="Exact executable path; overrides the executor's default CLI")
    resume = sub.add_parser("resume", help="Explain the next action from saved session JSON")
    resume.add_argument("--session-file", type=Path, required=True)
    msg = sub.add_parser("message", help="Generate a request; never send it")
    msg.add_argument("kind", choices=("plan", "review"))
    msg.add_argument("--task", required=True)
    msg.add_argument("--workspace-id", required=True)
    msg.add_argument("--language", default="English")
    msg.add_argument("--iteration", type=int, default=0)
    content = msg.add_mutually_exclusive_group(required=True)
    content.add_argument("--goal-file", type=Path)
    content.add_argument("--summary-file", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            default = "c2c" if args.executor == "codex" else "code-with-chatgpt"
            output = query_status(args.workspace, args.bridge or default)
        elif args.command == "resume":
            output = resume_summary(json.loads(args.session_file.read_text(encoding="utf-8")))
        else:
            path = args.goal_file if args.kind == "plan" else args.summary_file
            if path is None:
                raise InputError("Use --goal-file for plan and --summary-file for review.")
            print(message(args.kind, args.task, args.workspace_id, args.language, args.iteration, path.read_text(encoding="utf-8")), end="")
            return 0
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except (OSError, UnicodeError, ValueError):
        # Never echo arbitrary parser errors: they can contain provider data or secrets.
        exc = sys.exc_info()[1]
        detail = str(exc) if isinstance(exc, InputError) else "Could not read valid UTF-8/JSON input. Check the local file."
        print(f"Error: {detail}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
