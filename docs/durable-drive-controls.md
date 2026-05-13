# Durable Drive Controls

Engineering Harness stores drive controls and approval gates in the local project state file:

```text
.engineering/state/harness-state.json
```

The controls are local-first. A pause or cancel request prevents the next drive task from starting.
It does not terminate an operating-system process that is already running; the current task report
and phase state remain the durable evidence for what happened.

## Heartbeat And Watchdog

While `drive` is running, the `drive_control` block records the owning process id, drive start time,
last heartbeat, current activity, current task when known, and last progress message. Inspect it with:

```bash
python3 -m engineering_harness.cli status --project-root /path/to/project --json
```

The JSON includes `drive_control.watchdog`. A healthy active drive reports `status: running` and
`stale: false`. A stale drive reports `status: stale` when the recorded process is gone, the recorded
pid is missing, or the last heartbeat is older than the local threshold.

The default stale-heartbeat threshold is one hour. Configure it locally in the roadmap:

```json
{
  "drive_watchdog": {
    "stale_after_seconds": 7200
  }
}
```

For one shell session, override it with:

```bash
ENGINEERING_HARNESS_DRIVE_STALE_AFTER_SECONDS=7200 python3 -m engineering_harness.cli status --project-root /path/to/project --json
```

The watchdog only probes the recorded pid with a non-destructive local liveness check. It never kills
or signals unrelated work beyond that liveness probe.

## Rolling Checkpoint Boundaries

When `drive --rolling` materializes continuation stages and `--commit-after-task` or
`--push-after-task` is enabled, the harness treats roadmap materialization as its own checkpoint
boundary before generated tasks run.

If the git worktree is clean immediately before materialization, the harness commits the roadmap
materialization first. Generated task checkpoints then run against a clean boundary and do not mistake
the harness-owned roadmap edit for pre-existing user dirtiness.

If the worktree already has user changes, the harness does not commit the materialization. The drive
report records a deferred materialization checkpoint and later generated task checkpoints are
explicitly marked `deferred` with the dirty paths that forced the boundary to stay open. This preserves
the existing protection against committing unrelated user changes while distinguishing that case from
ordinary task checkpoint skips.

## Goal-Gap Retrospective

Every completed drive report includes a deterministic `Goal-Gap Retrospective` section and a matching
JSON sidecar next to the Markdown report under `.engineering/reports/tasks/drives/`.

The retrospective compares the final local harness state with the unattended reliability goal: drain
or safely extend the roadmap, preserve local audit evidence, surface blockers deterministically, and
avoid unsafe external dependencies. It does not call a model or external service. The evidence is
bounded to local harness artifacts:

- final status summary, including continuation and self-iteration settings;
- manifest index summary and recent task/drive report metadata;
- drive control and approval queue state;
- self-iteration context-pack summaries when any exist;
- local test inventory; and
- local git status and recent commits.

The Markdown section lists completed reliability capabilities, remaining risks, likely next stage
themes, and whether another self-iteration should be requested. The embedded JSON block uses the
same machine-readable `engineering-harness.goal-gap-retrospective` payload that is also present in
the drive JSON sidecar and in `drive --json` output.

Self-iteration is recommended only when the roadmap queue is empty, self-iteration is enabled, no
task or continuation stage is pending, and the drive is not blocked, failed, interrupted, or stopped
by budget. If queued work, pending approvals, failed tasks, or budget exhaustion remain, the
retrospective records those blockers instead of recommending another planning loop.

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

`resume` clears pause, cancel, and stale watchdog state. It does not start work by itself; run `drive`
after resuming. If a drive is still actively running with a fresh heartbeat, `resume` leaves that
active state in place.

## Recover A Stale Drive

If status shows `drive_control.status: stale`, inspect the last activity and task fields first:

```bash
python3 -m engineering_harness.cli status --project-root /path/to/project --json
```

When the recorded process is gone or the heartbeat is stale, clear the stale running state locally:

```bash
python3 -m engineering_harness.cli resume --project-root /path/to/project --reason "recover stale drive"
python3 -m engineering_harness.cli drive --project-root /path/to/project
```

The next drive selects work from durable task state and reports. It does not kill the old pid.

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
