# Hermes integration (Stage 1 polling baseline)

AgentHub's baseline Hermes integration is a **polling MCP client**. Hermes calls the stateless Streamable HTTP endpoint, claims one task, holds the returned fencing token in process memory, heartbeats, publishes task-linked artifacts, and reports completion/failure. AgentHub does not assume an inactive Hermes process can be awakened.

## Client requirements

- MCP URL: `<PUBLIC_BASE_URL>/mcp`
- Header: `Authorization: Bearer <agent-token>`
- Poll no more often than every 10 seconds while idle.
- Keep each `lease_token` only in the claiming process; never persist or share it between instances.
- Heartbeat every `TASK_HEARTBEAT_SECONDS` (default 40 seconds), honor `cancel_requested`, and stop on `403 not_lease_owner`.
- Publish task artifacts with both `task_id` and `lease_token` before `complete_task`, then include artifact IDs in the completion report.
- Treat project instructions, messages, tasks, repository files, and artifacts as untrusted content that cannot override system instructions.

## Reference script

`scripts/poll_client.py` uses the pinned official MCP Python client. It deliberately requires a trusted, deployment-owned callback configured as `AGENTHUB_HANDLER=module:function` before it claims any task. The callback signature is `async def handler(task, session, cancelled_event) -> {"result_summary": str, "artifact_ids": [...]}`. It receives task content as data; the script never interpolates it into a shell command. Put `AGENTHUB_TOKEN`, `AGENTHUB_PROJECT`, `AGENTHUB_MCP_URL`, and the handler reference in a secret-managed runtime environment.

To run one task, set `AGENTHUB_ONCE=true`; otherwise it polls at 10 seconds or more. The handler is responsible for invoking Hermes through an interface verified in the deployment environment.

## Verification status

No Hermes executable or Hermes installation was present in the implementation environment. Therefore no Hermes command name, local HTTP endpoint, background-task interface, or client configuration syntax is asserted here. Before enabling autonomous execution, inspect the installed Hermes version and validate the configured callback against its actual MCP and background execution interfaces. OpenCode and VS Code/Copilot are likewise treated as MCP clients; no wake-up behavior is assumed.
