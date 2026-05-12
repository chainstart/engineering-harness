# Durable Drive Controls

Engineering Harness stores drive controls and approval gates in the local project state file:

```text
.engineering/state/harness-state.json
```

The controls are local-first. A pause or cancel request prevents the next drive task from starting.
It does not terminate an operating-system process that is already running; the current task report
and phase state remain the durable evidence for what happened.

## Pause A Long Drive

```bash
python3 -m engineering_harness.cli pause --project-root /path/to/project --reason "operator review"
```

The next `drive` invocation exits with status `paused` and does not start a task:

```bash
python3 -m engineering_harness.cli drive --project-root /path/to/project
```

Inspect the durable state:

```bash
python3 -m engineering_harness.cli status --project-root /path/to/project --json
```

The `drive_control` block shows `status: paused` and `pause_requested: true`.

## Resume A Drive

```bash
python3 -m engineering_harness.cli resume --project-root /path/to/project --reason "review complete"
python3 -m engineering_harness.cli drive --project-root /path/to/project
```

`resume` clears pause and cancel state. It does not start work by itself; run `drive` after resuming.

## Cancel A Drive

```bash
python3 -m engineering_harness.cli cancel --project-root /path/to/project --reason "superseded plan"
```

Future `drive` invocations stop with status `cancelled` until the control state is cleared:

```bash
python3 -m engineering_harness.cli resume --project-root /path/to/project --reason "clear cancellation"
```

Cancellation is a drive control, not a roadmap edit. It does not delete tasks or reports.

## Approval Queue

Manual, live, and agent gates create pending approval records when a task is blocked by policy.

```bash
python3 -m engineering_harness.cli drive --project-root /path/to/project
python3 -m engineering_harness.cli approvals --project-root /path/to/project
```

The queue records the approval id, task id, gate kind, phase or command, and reason. To approve one
gate:

```bash
python3 -m engineering_harness.cli approve --project-root /path/to/project APPROVAL_ID --reason "approved by operator"
python3 -m engineering_harness.cli drive --project-root /path/to/project
```

For local test projects, all pending gates can be approved at once:

```bash
python3 -m engineering_harness.cli approve --project-root /path/to/project --all --reason "local dry-run approval"
```

Approved gates unblock the affected task for a later drive run. When that task completes, its
approval records are marked `consumed` so the state remains auditable.
