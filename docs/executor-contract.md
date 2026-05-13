# Executor Contract

Executor adapters let the harness run shell commands, Codex prompts, and future execution backends
without changing task semantics. Roadmap tasks still define command groups with `executor`,
`command` or `prompt`, `timeout_seconds`, optional `no_progress_timeout_seconds`, `required`,
`model`, `sandbox`, and optional `requested_capabilities`.

## Adapter Interface

An adapter provides:

- `metadata`: stable executor identity and capability metadata.
- `display_command(invocation)`: the auditable command string written to reports and manifests.
- `execute(invocation)`: a normalized result with status, return code, timestamps, stdout, stderr,
  and adapter-specific metadata.

Built-in executors are registered in `default_executor_registry()`:

- `shell`: local `/bin/bash` process execution. It uses the command allowlist policy.
- `codex`: `codex exec` agent execution. It requires `--allow-agent`.
- `dagger`: local Dagger CLI execution. It is discoverable by roadmap validation, but execution is
  blocked unless Dagger support is explicitly enabled.

Tests and future integrations can pass a custom registry to `Harness(..., executor_registry=...)`.
Unknown executor ids fail roadmap validation and task preflight with the same clear messages used
by existing harness behavior.

## Capability Requests

Roadmap command entries may declare `requested_capabilities` when a task needs an explicit local
executor capability contract:

```json
{
  "name": "local tests",
  "command": "python3 -m pytest tests -q",
  "requested_capabilities": ["local_process", "workspace_write", "stdout", "stderr", "exit_code"]
}
```

The field is optional for backward compatibility. When present it must be a non-empty list of known
capability names. The current local vocabulary is:

- Low-risk local capabilities: `local_process`, `workspace_write`, `exit_code`, `stdout`, `stderr`,
  `agent`, `local_dagger_cli`, `containerized_execution`, and `requires_explicit_configuration`.
- Unsafe request markers: `network`, `network_access`, `secret_access`, `secrets`,
  `browser_automation`, `deployment`, `deploy`, `live_operations`, and `live`.

Before a command runs, the harness compares `requested_capabilities` with the selected executor's
metadata capabilities. Supported low-risk requests are allowed. Unsupported requests are denied.
Unsafe request markers are denied even if an executor could technically perform them, because the
unattended local harness does not yet have an explicit approval mechanism for network access,
secret access, browser automation, deployment, or live operations.

## Manifest Contract

Each run keeps the legacy manifest fields for compatibility:

- `executor`
- `command`
- `status`
- `returncode`
- `stdout`
- `stderr`
- `required`
- `timeout_seconds`
- `no_progress_timeout_seconds`
- `model`
- `sandbox`
- `requested_capabilities`
- `executor_capabilities`

Each run also includes normalized executor fields:

- `executor_metadata`: schema version, id, display name, kind, adapter id, input mode,
  capabilities, and policy/approval flags.
- `executor_result`: schema version, status, return code, started/finished timestamps, stdout and
  stderr summaries, and optional adapter-specific result metadata.

This keeps reports and manifest readers compatible while giving future adapters a stable place to
record capabilities and result details.

## Watchdog Results

Built-in subprocess adapters (`shell`, enabled `dagger`, and `codex`) enforce `timeout_seconds` with
an owned local process group. When `no_progress_timeout_seconds` or the local
`executor_watchdog` roadmap defaults are configured, the same process monitor also terminates runs
that produce no stdout/stderr progress before the threshold.

Watchdog outcomes use deterministic executor statuses:

- `timeout`: the command exceeded `timeout_seconds`;
- `no_progress`: the command exceeded the configured no-progress threshold.

Task status remains `failed` for required commands, and the task manifest records
`failure_isolation.executor_watchdog` with the phase, executor metadata, command name, pid when
available, thresholds, last output/progress timestamp, report paths, and local next action.
Self-iteration planner watchdog failures return planner failure-isolation evidence in the
self-iteration result and report.

## Dagger Adapter Stub

The Dagger adapter is intentionally local-first. Roadmaps select it with `executor: "dagger"` and
provide a Dagger CLI command in `command`. The command may be written either as arguments after the
binary, such as `call test --source=.`, or with the leading binary, such as
`dagger call test --source=.`. The adapter records the auditable display command as `dagger ...`,
runs from the project root, captures stdout/stderr into the standard manifest fields, and does not
execute through a shell.

By default the adapter returns a blocked executor result:

- status: `blocked`
- return code: `null`
- metadata: `{"configured": false, "required_environment": "ENGINEERING_HARNESS_ENABLE_DAGGER"}`

To opt into local Dagger execution, set `ENGINEERING_HARNESS_ENABLE_DAGGER=1` in the harness
environment and ensure the `dagger` binary is on `PATH`. If the flag is enabled but the binary is
missing, the adapter still blocks and records `{"configured": true, "missing_binary": "dagger"}`.

This keeps the current harness safe while leaving a clear integration path for later work:

- add project-level Dagger module discovery;
- map roadmap task metadata into Dagger function arguments;
- define cache, network, and secret policies before enabling remote or deployment-oriented runs;
- keep all Dagger outcomes flowing through the same executor metadata and result contract used by
  shell and Codex.
