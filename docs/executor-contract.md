# Executor Contract

Executor adapters let the harness run shell commands, Codex prompts, and future execution backends
without changing task semantics. Roadmap tasks still define command groups with `executor`,
`command` or `prompt`, `timeout_seconds`, `required`, `model`, and `sandbox`.

## Adapter Interface

An adapter provides:

- `metadata`: stable executor identity and capability metadata.
- `display_command(invocation)`: the auditable command string written to reports and manifests.
- `execute(invocation)`: a normalized result with status, return code, timestamps, stdout, stderr,
  and adapter-specific metadata.

Built-in executors are registered in `default_executor_registry()`:

- `shell`: local `/bin/bash` process execution. It uses the command allowlist policy.
- `codex`: `codex exec` agent execution. It requires `--allow-agent`.

Tests and future integrations can pass a custom registry to `Harness(..., executor_registry=...)`.
Unknown executor ids fail roadmap validation and task preflight with the same clear messages used
by existing harness behavior.

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
- `model`
- `sandbox`

Each run also includes normalized executor fields:

- `executor_metadata`: schema version, id, display name, kind, adapter id, input mode,
  capabilities, and policy/approval flags.
- `executor_result`: schema version, status, return code, started/finished timestamps, stdout and
  stderr summaries, and optional adapter-specific result metadata.

This keeps reports and manifest readers compatible while giving future adapters a stable place to
record capabilities and result details.
