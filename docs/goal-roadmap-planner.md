# Goal Roadmap Planner

`plan-goal` is the local starter workflow that turns a high-level goal into a deterministic
`.engineering/roadmap.yaml` proposal.

It does not call model APIs, require external accounts, deploy software, move funds, or perform live
trading. The planner only normalizes local input and renders a template-driven roadmap.

## Usage

Preview a roadmap without writing files:

```bash
PYTHONPATH=src python3 -m engineering_harness.cli plan-goal \
  --project-root /path/to/project \
  --name "Autonomous Report Worker" \
  --profile python-agent \
  --experience-kind dashboard \
  --constraint "Keep generated reports local and deterministic." \
  --stage-count 4 \
  --goal "Build an autonomous dashboard worker for local research artifacts."
```

Write the starter roadmap:

```bash
PYTHONPATH=src python3 -m engineering_harness.cli plan-goal \
  --project-root /path/to/project \
  --name "Autonomous Report Worker" \
  --profile python-agent \
  --goal-file docs/goal.txt \
  --blueprint docs/blueprint.md \
  --materialize
```

Use `--force` only when replacing an existing `.engineering/roadmap.yaml`.

`--stage-count` is bounded from 1 to 4 and defaults to 4. The generated stages are deterministic:

1. `stage-1-local-slice`
2. `stage-2-experience-validation`
3. `stage-3-policy-evidence-observability`
4. `stage-4-unattended-drive-readiness`

Use a smaller stage count when the first roadmap should stay intentionally narrow. `--experience-kind`
can pin the experience plan to `dashboard`, `submission-review`, `multi-role-app`, `api-only`, or
`cli-only`; otherwise the planner derives the kind from the project name, profile, goal text, and
blueprint path. Repeated `--constraint` values are normalized into the goal intake and copied into
generated task prompts.

## Generated Roadmap Shape

The generated roadmap includes:

- an explicit `experience` block derived from the project name, profile, goal text, and blueprint
  path hints;
- a first `baseline` milestone with local `python3` acceptance commands that verify the starter
  roadmap and local-only safety settings;
- an ordered continuation backlog with a first local slice, experience validation, policy/evidence/
  observability hardening, and unattended drive readiness stages up to the configured stage count;
- every generated continuation task includes `file_scope`, `codex` implementation and repair
  entries, profile-aware local acceptance commands, and local e2e or journey-evidence gates tied to
  the experience plan;
- `self_iteration` guidance for profiles or goals that are likely to need rolling autonomous work.

The implementation and repair gates are configured as gated `codex` executor entries so normal
drives will require explicit `--allow-agent` approval before invoking an agent. Acceptance and e2e
commands remain local shell checks.

## Self-Iteration Safety Contract

When `self_iteration` is enabled, planner output is treated as an untrusted roadmap diff. After the
planner exits, the harness reloads `.engineering/roadmap.yaml` and accepts the output only when it:

- appends exactly `self_iteration.max_stages_per_iteration` new unmaterialized
  `continuation.stages` entries;
- leaves existing roadmap fields, milestones, tasks, task statuses, and continuation stages unchanged;
- avoids duplicate stage or task ids;
- avoids duplicate continuation plans by comparing deterministic semantic fingerprints of stable
  local fields such as titles, objectives, task titles, file scope, acceptance commands, and E2E
  commands while ignoring ids, generated timestamps, and status fields; the context pack also
  includes an identity fingerprint with task ids for auditability;
- gives every new task non-empty `file_scope` and local acceptance commands;
- includes `codex` implementation and `codex` repair entries for tasks that require implementation
  work;
- avoids live operations, private keys, mainnet writes, production deployments, paid services,
  real-fund movement, and live trading requirements.

Accepted output is then checked with the normal roadmap validator. Invalid output is rejected, the
previous roadmap text is restored, and the self-iteration report records the validation errors.

## Self-Iteration Planner Input

Before a self-iteration planner runs, the harness writes a bounded JSON context pack next to the
self-iteration snapshot under `.engineering/reports/tasks/assessments/`. The planner prompt includes
both paths:

- `Status snapshot`: the machine-readable status snapshot for the run.
- `Planner context pack`: the bounded planner input contract.

Planners should read the context pack first and use the roadmap file only for the final append. The
context pack is local-only, redacted with the harness secret redaction helper, and capped by count and
excerpt size. Its top-level fields are:

- `summary`: compact counts for continuation stages, duplicate-plan fingerprints, manifests,
  reports, docs, tests, source files, git status lines, and recent commits.
- `roadmap`: project/profile/goal metadata, task status counts, next task, continuation summary, and
  capped continuation stage summaries.
- `duplicate_plan`: a bounded list of existing continuation-stage fingerprints and task-id/title
  hints so planners can avoid re-appending the same local plan under new ids.
- `manifests`: latest manifest-index summary plus the most recent task-run manifest summaries.
- `reports`: recent task report and drive report metadata.
- `docs`: blueprint metadata and capped excerpts from relevant local docs.
- `test_inventory` and `source_inventory`: capped local file inventories.
- `git`: repository flag, branch/head, short status lines, and recent commits.

The self-iteration snapshot and Markdown report both record the context-pack path and summary so an
operator can audit exactly what the planner saw.

## Generated Goal Gates

Continuation tasks now put behavioral checks before the small roadmap contract smoke:

- `python-agent` and `agent-monorepo` tasks start with `python3 -m pytest tests -q` and require a
  local `tests/e2e` pytest journey check.
- `node-frontend` tasks use npm-oriented gates such as `npm test` and `npm run e2e`, leaving the
  generated implementation prompt to create or wire the local scripts.
- `cli-only` and `api-only` experience plans add deterministic documented-example or local command
  checks under `examples/`, `docs/examples/`, `tests/cli/`, `tests/api/`, or `tests/e2e/`.
- Every generated task also requires local journey evidence or an executable journey check tied to
  the selected `experience.e2e_journeys` entry, for example under `docs/e2e/`, `docs/evidence/`, or
  `tests/e2e/`.

The generated implementation prompt lists the exact acceptance and E2E commands plus candidate test,
example, and evidence paths. Coding agents should create those local artifacts as part of the task,
not replace the gates with external-account, paid-service, production-deployment, private-key,
mainnet, real-fund, or live-trading checks.

## Safety Boundary

The planner reuses the local goal-intake validator. It rejects non-local blueprint URLs and unsafe
requirements such as production deployment, mainnet writes, private key use, live trading, real-fund
movement, and paid live services.

The generated starter is intentionally conservative. Tighten the generated acceptance and e2e checks
with project-specific local tests as implementation details become clear, while preserving the
local-only safety boundary.
