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
  entries, local acceptance commands, and local e2e gates tied to the experience plan;
- `self_iteration` guidance for profiles or goals that are likely to need rolling autonomous work.

The implementation and repair gates are configured as gated `codex` executor entries so normal
drives will require explicit `--allow-agent` approval before invoking an agent. Acceptance and e2e
commands remain local shell checks.

## Safety Boundary

The planner reuses the local goal-intake validator. It rejects non-local blueprint URLs and unsafe
requirements such as production deployment, mainnet writes, private key use, live trading, real-fund
movement, and paid live services.

The generated starter is intentionally conservative. Replace the template acceptance and e2e checks
with project-specific tests as implementation details become clear.
