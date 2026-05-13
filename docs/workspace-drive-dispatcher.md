# Workspace Drive Dispatcher

`engh workspace-drive` scans a local workspace, builds a deterministic project queue, and starts at
most one eligible project drive per invocation. It is intended for unattended loops such as cron or a
local supervisor where each tick should make bounded progress without pushing, deploying, or requiring
external accounts.

Inspect the workspace first:

```bash
bin/engh scan --workspace /path/to/workspace --json
```

Run one bounded dispatch tick:

```bash
bin/engh workspace-drive --workspace /path/to/workspace --max-tasks 1 --json
```

For long unattended rolling work, keep each tick small:

```bash
bin/engh workspace-drive \
  --workspace /path/to/workspace \
  --max-tasks 1 \
  --rolling \
  --max-continuations 1 \
  --time-budget-seconds 1800 \
  --json
```

The dispatcher sorts discovered projects by resolved local path. The first safety-eligible project is
driven; later eligible projects are left for later invocations and recorded with
`one_project_per_invocation`.

## Dispatch Lease

`workspace-drive` acquires a durable local lease before scanning the workspace. The lease lives under:

```text
<workspace>/.engineering/state/workspace-dispatch-lease/lease.json
```

The lease records the workspace root, owner pid, start time, last heartbeat time, heartbeat count,
selected project when known, command options, and stale-after threshold. It is released on successful
and failed dispatch completion.

If another tick finds a fresh lease owned by a live process, it refuses to scan or dispatch, exits
non-zero with `status: "lease_held"` in `--json` output, and still writes the normal Markdown report
and JSON sidecar under `.engineering/reports/workspace-dispatches/`.

Stale leases are recovered locally before scanning when either:

- the recorded owner pid is no longer running;
- the recorded heartbeat is older than the lease stale-after threshold.

The default threshold is 3600 seconds. Override it for a command with:

```bash
bin/engh workspace-drive --workspace /path/to/workspace --lease-stale-after-seconds 7200 --json
```

or set `ENGINEERING_HARNESS_WORKSPACE_DISPATCH_LEASE_STALE_AFTER_SECONDS` for cron or supervisor
environments.

## Safety Skips

Projects are skipped, with explicit JSON evidence, when they are:

- missing an engineering roadmap or carrying an invalid roadmap;
- outside the requested local workspace scope;
- paused, cancelled, already running, or stale-running;
- waiting on pending approval gates;
- carrying unresolved isolated task failures;
- empty when neither `--rolling` nor `--self-iterate` is requested.

Skipped projects are inspected read-only. Their roadmap and state files are not rewritten just because
the workspace dispatcher scanned them.

## Reports

Every invocation writes a Markdown report and JSON sidecar under:

```text
<workspace>/.engineering/reports/workspace-dispatches/
```

The JSON output and sidecar include the full queue, skip reasons, selected project, drive status, and
the selected project drive report path. Use the project-level report for task execution details and the
workspace report for scheduling evidence.

## Operating Loop

Use repeated small invocations instead of a single large drive:

```bash
while true; do
  bin/engh workspace-drive --workspace /path/to/workspace --max-tasks 1 --rolling --max-continuations 1 --json
  sleep 300
done
```

The built-in lease is sufficient for overlapping cron or local supervisor ticks. A second tick will
produce machine-readable lease evidence instead of racing the active scanner. Supervisors should treat
`lease_held` as a normal contention outcome and retry on the next interval.

Resolve safety skips locally before expecting a project to re-enter the queue:

- `bin/engh resume --project-root <project>` clears pause, cancel, or stale drive control after review.
- `bin/engh approvals --project-root <project> --json` shows pending approval gates.
- `bin/engh status --project-root <project> --json` shows unresolved isolated failure evidence.

The workspace dispatcher does not expose push flags and passes project drives with local checkpointing
disabled. Roadmap tasks still run under the project policy engine, so live operations and agent or
manual approval gates remain blocked unless explicitly allowed by the operator.
