# Connection and recovery

## Local status is not readiness

`status --json` is read-only. The transport's `doctor` may start or repair services; do not treat it as a status query.
Use `setup`, `doctor`, or `pair` only within the user's connection/setup request. Inspect the installed command's `--help` before using unfamiliar flags.

Check all of the following before claiming readiness:

- The intended executor and workspace were inspected.
- The bridge is running and current authentication is present.
- The correct web account, Project, conversation, and connector are selected.
- An actual `workspace_info` tool result from that conversation matches the local workspace ID.

The helper reports local state only and never declares readiness. Zero or unknown authentication counts are not an authenticated connection.
Similar directory names do not establish workspace identity.

## First connection

Read existing `prefs --json` and `session get --json` first. Reuse saved choices instead of asking on every reconnect.
Get connection settings from structured CLI output. Do not invent addresses or reuse another task's connector.
Translate any transport-provided setup question into the user's language; never force the transport's locale on them.
Use the normal login, two-factor, and consent process. A dismissed dialog alone does not prove authorization.

## Recovery

- Missing executable: locate the selected transport. If absent, report the missing dependency rather than claiming the connection works.
- Zero authentication or 401: inspect the exact connector's authentication. Do not use another account's connection as a substitute.
- Changed address: compare the current CLI address with the selected connector's registration. Repair only that connection after confirming the mismatch.
- Connector chip but no tools: inspect authentication and tool exposure. Do not immediately delete/recreate the connector.
- Web tool failure: record the actual failure, change the relevant condition, and retry within the task's existing budget. Waiting alone is not a reason to restart the task.

`unpair` revokes access. Use it only when the user requests disconnection for that workspace.
Installing this skill does not require resetting the bridge, browser sessions, user preferences, or account connections.
