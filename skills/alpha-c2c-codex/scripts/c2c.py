#!/usr/bin/env python3
"""Small, read-only bridge status adapter and language-aware message generator."""
from __future__ import annotations

import argparse
import hashlib
import uuid
import os
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


CONNECTOR_PREFIX = {"claude": "Alpha C2C Claude", "codex": "Alpha C2C Codex"}
BRIDGE = Path(__file__).resolve().with_name("bridge.py")


def expected_connector(executor: str, workspace_name: object) -> str | None:
    """Connector label this executor must attach; the other executor's label is never a substitute."""
    if not isinstance(workspace_name, str) or not workspace_name.strip():
        return None
    return f"{CONNECTOR_PREFIX[executor]} · {workspace_name.strip()}"


def status_summary(data: object, executor: str = "codex") -> dict:
    """Allowlist fields; never forward arbitrary provider messages or credentials."""
    d = object_value(data)
    summary = _status_state(d)
    connector = expected_connector(executor, d.get("workspaceName"))
    if connector:
        summary["expectedConnector"] = connector
    return summary


def _status_state(d: dict) -> dict:
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


def canonical_sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def prepare_request(kind, task, workspace_id, language, iteration, content, baseline_path, output, proof_path):
    """Create a new local correlation record, never a posting/authentication receipt."""
    baseline = object_value(json.loads(baseline_path.read_text(encoding='utf-8')))
    if not all(isinstance(baseline.get(k), str) and baseline[k] for k in ('page', 'url')):
        raise InputError('Capture an exact page and URL before preparing a request.')
    if baseline.get('generating'):
        raise InputError('Baseline is generating; retain its existing request.')
    if output.resolve() == proof_path.resolve() or output.exists() or proof_path.exists():
        raise InputError('Request/proof destinations must be distinct new files; existing files were preserved.')
    body = message(kind, task, workspace_id, language, iteration, content)
    nonce = 'request-' + str(uuid.uuid4())
    header = {'TASK_ID': task, 'WORKSPACE_ID': workspace_id, 'ITERATION': str(iteration), 'REQUEST_ID': nonce}
    request = ' '.join(k + ': ' + v for k, v in header.items()) + '\n\n'
    request += 'Begin your authored response with this exact TASK_ID, WORKSPACE_ID, ITERATION and REQUEST_ID, then STATE.\n' + body
    proof = {'schema': 'c2c-prepared-request-v1', 'status': 'preparing',
             'producer': 'alpha-c2c-codex/scripts/c2c.py',
             'producerSha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
             'messageSha256': hashlib.sha256(request.encode()).hexdigest(),
             'baselineSha256': canonical_sha(baseline), 'page': baseline['page'], 'url': baseline['url'],
             'identity': header}
    # Exclusive creation preserves unknown files and exposes incomplete own artifacts.
    with proof_path.open('x', encoding='utf-8') as proof_file:
        proof_file.write(json.dumps(proof, ensure_ascii=False) + '\n'); proof_file.flush(); os.fsync(proof_file.fileno())
        try:
            with output.open('x', encoding='utf-8') as message_file:
                message_file.write(request); message_file.flush(); os.fsync(message_file.fileno())
            proof['status'] = 'prepared'
        except OSError as error:
            proof['status'] = 'creation_failed'; proof['errorType'] = type(error).__name__
            proof_file.seek(0); proof_file.truncate(); proof_file.write(json.dumps(proof) + '\n'); proof_file.flush()
            raise
        proof_file.seek(0); proof_file.truncate(); proof_file.write(json.dumps(proof, ensure_ascii=False) + '\n'); proof_file.flush(); os.fsync(proof_file.fileno())
    return {'prepared': True, 'output': str(output), 'requestProof': str(proof_path), 'messageSha256': proof['messageSha256']}


def query_status(workspace: Path, executable: str | None = None, executor: str = "codex") -> dict:
    workspace = workspace.expanduser().resolve()
    if not workspace.is_dir():
        raise InputError("Workspace directory does not exist.")
    if executable:
        program = shutil.which(executable)
        if not program:
            raise InputError("Transport executable not found. Check the --bridge path.")
        argv = [program]
    else:
        argv = [sys.executable, str(BRIDGE)]
    env = dict(os.environ, ALPHA_C2C_EXECUTOR=executor)
    try:
        result = subprocess.run(
            [*argv, "status", "-w", str(workspace), "--json"],
            capture_output=True, text=True, encoding="utf-8", timeout=15, check=False, env=env,
        )
    except subprocess.TimeoutExpired:
        raise InputError("Status query timed out. No setup or repair was attempted.") from None
    if result.returncode:
        raise InputError(f"Status query failed (exit {result.returncode}). Inspect the transport locally; its raw output was not forwarded.")
    try:
        return status_summary(json.loads(result.stdout), executor)
    except (json.JSONDecodeError, InputError):
        raise InputError("Transport returned an unsupported status response. Its raw output was not forwarded.") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status", help="Inspect local bridge status without setup or repair")
    status.add_argument("--workspace", type=Path, required=True)
    status.add_argument("--executor", choices=("codex", "claude"), default="codex")
    status.add_argument("--bridge", help="Another bridge executable; default is this skill's scripts/bridge.py")
    resume = sub.add_parser("resume", help="Explain the next action from saved session JSON")
    resume.add_argument("--session-file", type=Path, required=True)
    msg = sub.add_parser("message", help="Generate a request; never send it")
    msg.add_argument("kind", choices=("plan", "review"))
    msg.add_argument("--task", required=True)
    msg.add_argument("--workspace-id", required=True)
    msg.add_argument("--language", default="English")
    msg.add_argument("--iteration", type=int, default=0)
    msg.add_argument("--baseline", type=Path)
    msg.add_argument("--output", type=Path)
    msg.add_argument("--request-proof", type=Path)
    content = msg.add_mutually_exclusive_group(required=True)
    content.add_argument("--goal-file", type=Path)
    content.add_argument("--summary-file", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            output = query_status(args.workspace, args.bridge, args.executor)
        elif args.command == "resume":
            output = resume_summary(json.loads(args.session_file.read_text(encoding="utf-8")))
        else:
            path = args.goal_file if args.kind == "plan" else args.summary_file
            if path is None:
                raise InputError("Use --goal-file for plan and --summary-file for review.")
            flags = (args.baseline, args.output, args.request_proof)
            if any(flags):
                if not all(flags):
                    raise InputError('Use --baseline, --output and --request-proof together.')
                output = prepare_request(args.kind, args.task, args.workspace_id, args.language, args.iteration,
                                         path.read_text(encoding='utf-8'), *flags)
                print(json.dumps(output)); return 0
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
