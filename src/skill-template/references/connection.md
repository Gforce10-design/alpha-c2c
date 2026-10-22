# Connection and recovery

The bridge is `python3 <skill-dir>/scripts/bridge.py`. It needs Python 3.10+, `git`, and
[`cloudflared`](https://github.com/cloudflare/cloudflared)
for the public HTTPS address ChatGPT connects to (a quick tunnel by default, no Cloudflare account; or a named tunnel on the user's own domain, see Fixed address).
It serves read-only tools for one workspace and never writes files or runs commands for the reviewer.
State lives outside the workspace (`~/Library/Application Support/alpha-c2c/<executor>` on macOS,
`%LOCALAPPDATA%\alpha-c2c\<executor>` on Windows, `${XDG_STATE_HOME:-~/.local/state}/alpha-c2c/<executor>` elsewhere;
`ALPHA_C2C_STATE_DIR` overrides it).

## Local status is not readiness

`status --json` is read-only. `doctor` may start the bridge or tunnel; do not treat it as a status query (`doctor --no-fix` only checks).
Use `setup`, `doctor`, or `pair` only within the user's connection/setup request. Inspect `bridge.py <command> --help` before using unfamiliar flags.

Check all of the following before claiming readiness:

- The intended executor and workspace were inspected.
- The bridge is running and current authentication is present.
- The correct web account, Project, conversation, and connector are selected.
- An actual `workspace_info` tool result from that conversation matches the local workspace ID.

The helper reports local state only and never declares readiness. Zero or unknown authentication counts are not an authenticated connection.
Similar directory names do not establish workspace identity.

## First connection

1. Read existing `prefs --json` and `session get -w <path> --json` first. Reuse saved choices instead of asking on every reconnect.
2. `python3 <skill-dir>/scripts/bridge.py setup -w <path> --json` starts the bridge and tunnel and prints `connectorName`, `mcpUrl` and a five-minute `pairingCode`. Use only these structured values; do not invent addresses.
3. In ChatGPT, open Plugins → Add → Add custom MCP server (developer mode must be enabled for custom connectors). Create a connector named exactly `connectorName` with server URL `mcpUrl` and OAuth authentication, then choose Connect. ChatGPT cannot edit a connector's URL: when `connectorAction` is `update`, delete the old connector with that name (its settings page → Delete app) and create it again with the new `mcpUrl`, so only one exists.
4. ChatGPT opens the bridge's sign-in page. The user types the pairing code shown on this computer. A code works once; if it expired, run `pair -w <path> --json` for a new one.
5. Verify with an actual `workspace_info` call in the task conversation.

Translate setup steps into the user's language. Use the normal login, two-factor, and consent process. A dismissed dialog alone does not prove authorization.

## Fixed address

A quick-tunnel address changes on every bridge restart, and each change means recreating the connector and pairing again. A user with a domain on Cloudflare can avoid that:

1. The user runs `cloudflared tunnel login` once (a browser login where they choose the domain). Never run it for them or bypass it.
2. `setup -w <path> --zone <domain> --json` creates this workspace's named tunnel and a DNS record `ac2c-<executor>-<folder>-<id>.<domain>`, and reports `tunnel.mode: named`. `--hostname` sets an exact name instead. Ask before creating DNS records in the user's domain; the bridge never overwrites an existing record.
3. The zone is remembered (`prefs --json` → `namedZone`), so later workspaces get a fixed address automatically. `setup --quick-tunnel` switches one workspace back; `prefs set --named-zone none` forgets the default.

Switching an existing workspace to a fixed address changes its URL once, so recreate that connector one last time.

## Recovery

- `cloudflared` missing: `doctor --no-fix --json` reports it. Ask the user to install it; do not claim the connection works.
- Zero authentication or 401: inspect the exact connector's authentication, then pair again. Do not use another account's connection as a substitute.
- Changed address: a quick-tunnel address changes whenever the bridge or tunnel restarts. `doctor --json` reports `connector-address`; after confirming the mismatch, delete and recreate only that connector with the new URL and pair again. Keep the bridge running between sessions to avoid this.
- ChatGPT says it could not register the OAuth client: the bridge or tunnel is usually down. Check `status --json` or `doctor --no-fix --json` before changing connector settings.
- Connector chip but no tools: inspect authentication and tool exposure. Do not immediately delete/recreate the connector.
- Unknown bridge state: `stop -w <path>`, then `setup` again. Tokens survive restarts; the tunnel address does not.
- Web tool failure: record the actual failure, change the relevant condition, and retry within the task's existing budget. Waiting alone is not a reason to restart the task.

`unpair` revokes access. Use it only when the user requests disconnection for that workspace.
Installing this skill does not require resetting browser sessions, user preferences, or account connections.
