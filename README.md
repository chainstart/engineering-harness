# Engineering Harness

Engineering Harness is a small, goal-driven control layer for long-running software work across
multiple repositories. It keeps roadmap milestones, tasks, acceptance commands, safety policies,
state, and reports separate from the coding agent that performs implementation.

The harness is designed for the projects under `/home/biostar/work`, including EVM protocols,
AI/agent runtimes, research pipelines, trading systems, and frontend apps.

## Core Idea

Each project owns a small `.engineering/` directory:

```text
.engineering/
  roadmap.yaml
  policies/
    command-allowlist.yaml
    deployment-policy.yaml
    secret-policy.yaml
  state/
  reports/
```

The shared harness provides a common CLI:

```bash
PYTHONPATH=src python3 -m engineering_harness.cli profiles
PYTHONPATH=src python3 -m engineering_harness.cli scan --workspace /home/biostar/work
PYTHONPATH=src python3 -m engineering_harness.cli status --project-root /home/biostar/work/projects/utopiai
PYTHONPATH=src python3 -m engineering_harness.cli next --project-root /home/biostar/work/projects/utopiai
PYTHONPATH=src python3 -m engineering_harness.cli run --project-root /home/biostar/work/projects/utopiai --dry-run
PYTHONPATH=src python3 -m engineering_harness.cli drive --project-root /home/biostar/work/projects/utopiai
PYTHONPATH=src python3 -m engineering_harness.cli drive --project-root /home/biostar/work/projects/utopiai --rolling
```

After installing in editable mode, the same commands are available as `engh`.

```bash
python3 -m pip install -e /home/biostar/work/projects/engineering-harness
engh scan --workspace /home/biostar/work
```

## Profiles

Built-in profiles:

- `evm-protocol`
- `node-frontend`
- `python-agent`
- `agent-monorepo`
- `evm-security-research`
- `trading-research`
- `lean-formalization`

Initialize a project:

```bash
PYTHONPATH=src python3 -m engineering_harness.cli init \
  --project-root /home/biostar/work/projects/ara-math \
  --profile python-agent \
  --name ara-math
```

## Safety

The harness does not execute arbitrary commands by default. Each project has a command allowlist.
Live deployment, private keys, mainnet actions, trading, and high-risk deletes must remain behind
explicit human approval.

The first version focuses on:

- roadmap and task selection;
- command allowlists;
- local test/build acceptance checks;
- durable state and reports;
- workspace project discovery.

Coding-agent execution is available through an explicit gated executor and must be enabled with
`--allow-agent`.

## Rolling Continuation

`drive` normally stops when the explicit roadmap queue is empty. For long autonomous sessions, add a
top-level `continuation` block to the project roadmap and run with `--rolling`.

```json
{
  "continuation": {
    "enabled": true,
    "goal": "Ship the full system described by the vision and blueprint.",
    "blueprint": "docs/design/system-architecture.md",
    "stages": [
      {
        "id": "next-stage",
        "title": "Next Stage",
        "objective": "A measurable phase objective.",
        "tasks": [
          {
            "id": "next-stage-tests",
            "title": "Run the next acceptance gate",
            "file_scope": ["runtime/**", "tests/**"],
            "acceptance": [
              {"name": "focused tests", "command": "python3 -m pytest tests/test_example.py -q"}
            ]
          }
        ]
      }
    ]
  }
}
```

Commands:

```bash
engh advance --project-root /home/biostar/work/projects/utopiai
engh drive --project-root /home/biostar/work/projects/utopiai --rolling --time-budget-seconds 14400
```

When the queue is empty, rolling drive materializes the next unstarted continuation stage, executes
its tasks, and repeats. It stops only when a configured budget is exhausted, a task fails or is
blocked, a manual gate is reached, no continuation stages remain, or repeated continuation attempts
make no progress.

Important controls:

- `--max-tasks`: maximum acceptance tasks to execute in this drive.
- `--time-budget-seconds`: wall-clock budget.
- `--max-continuations`: maximum generated roadmap stages.
- `--continuation-batch-size`: number of stages to materialize at a time.
- `--no-progress-limit`: stop after repeated continuation attempts that add no tasks.

## Self Iteration

When the explicit continuation queue is exhausted, the harness can run a configured self-iteration
planner. The planner reads the current roadmap, reports, git status, and project docs, then appends
the next measurable `continuation.stages` entry. `drive --self-iterate --rolling` then materializes
that new stage and keeps executing.

```json
{
  "self_iteration": {
    "enabled": true,
    "objective": "Assess current state and append the next safe, testable project stage.",
    "max_stages_per_iteration": 1,
    "file_scope": [".engineering/roadmap.yaml", "docs/**", "runtime/**", "tests/**"],
    "planner": {
      "name": "Codex self-iteration planner",
      "executor": "codex",
      "timeout_seconds": 3600,
      "sandbox": "workspace-write",
      "prompt": "Use the project blueprint and latest harness reports to append exactly one new continuation stage."
    }
  }
}
```

Commands:

```bash
engh self-iterate --project-root /home/biostar/work/projects/utopiai --allow-agent
engh drive --project-root /home/biostar/work/projects/utopiai \
  --rolling \
  --self-iterate \
  --allow-agent \
  --time-budget-seconds 14400 \
  --commit-after-task
```

Self-iteration stops when the planner is disabled, blocked, fails, repeatedly makes no progress,
or the configured task/time/self-iteration budgets are exhausted. The planner is not allowed to mark
tasks complete, edit state or report artifacts, or require private keys, paid live services, or
mainnet writes.

## Git Checkpoints

For long-running work, `run` and `drive` can create a git checkpoint after each task that completes
successfully:

```bash
engh drive --project-root /home/biostar/work/projects/utopiai --commit-after-task
engh drive --project-root /home/biostar/work/projects/utopiai --commit-after-task --push-after-task
```

The default commit message is `chore(engineering): complete {task_id}`. Override it with
`--git-message-template`; available fields are `{task_id}`, `{task_title}`, `{milestone_id}`, and
`{milestone_title}`. `--push-after-task` implies committing first, then pushing `HEAD` to the current
branch on `origin` unless `--git-remote` or `--git-branch` is provided.

## Autonomous Implementation Loop

Tasks can now include three phases:

- `implementation`: commands or an agent executor that changes the working tree.
- `acceptance`: required tests/checks that decide whether the task is done.
- `repair`: commands or an agent executor used after a failed acceptance run.

The harness executes `implementation`, then `acceptance`. If acceptance fails and `repair` is
configured, it runs `repair` and retries acceptance until `max_task_iterations` is exhausted.

```json
{
  "id": "worker-node-loop",
  "title": "Implement long-running worker node loop",
  "max_task_iterations": 3,
  "file_scope": ["runtime/**", "tests/**"],
  "implementation": [
    {
      "name": "Codex implementation",
      "executor": "codex",
      "prompt": "Implement the worker loop described by this task.",
      "timeout_seconds": 3600
    }
  ],
  "repair": [
    {
      "name": "Codex repair",
      "executor": "codex",
      "prompt": "Fix the failing acceptance tests for this task.",
      "timeout_seconds": 1800
    }
  ],
  "acceptance": [
    {"name": "focused tests", "command": "python3 -m pytest tests/test_worker.py -q"}
  ]
}
```

Agent executors are gated. Use `--allow-agent` when you intentionally want the harness to invoke a
non-interactive coding agent:

```bash
engh drive --project-root /home/biostar/work/projects/utopiai --rolling --allow-agent --commit-after-task --push-after-task
```

The built-in `codex` executor calls `codex exec --full-auto --sandbox workspace-write -C <project>`.
It is still bounded by the roadmap task, file scope, acceptance commands, time budget, git
checkpoints, and the command/live/manual gates already enforced by the harness.
