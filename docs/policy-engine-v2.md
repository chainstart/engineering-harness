# Policy Engine V2 Schema

Policy Engine V2 records the data used for harness safety checks as a structured `policy_input`
contract and emits normalized `policy_decisions` from that input. Task manifests persist those
decisions and a deterministic `policy_decision_summary`; Markdown task reports embed the same
summary and decisions in a JSON evidence block; manifest indexes aggregate the summaries across
runs. The current Python evaluator preserves the existing command allowlist, executor approval,
manual approval, live flag, git preflight, and file-scope behavior.

## Policy Input

Task-run manifests include a top-level `policy_input` object with `schema_version: 1`.

The input captures:

- `project`: project name, root, profile, and roadmap path.
- `task`: task and milestone ids, title, status, approval requirements, and iteration limit.
- `phase`: `task` for task-level checks or the command group name for command-level checks.
- `command`: command name, command text or prompt, required flag, timeout, model, sandbox, and executor id.
- `executor`: normalized executor metadata from the executor contract.
- `git`: repository flag, root, branch, head, short head, and refs.
- `worktree`: git preflight status, file-scope guard status, dirty paths, changed paths, and violations.
- `file_scope`: allowed patterns plus out-of-scope and violation paths.
- `approvals`: `allow_manual`, `allow_agent`, task approval requirements, and executor agent requirement.
- `live`: `allow_live`, live-gated patterns, matched patterns, and whether a live action was detected.
- `context`: policy profile, command policy version, and harness default timeout.

## Policy Decisions

Each decision has `schema_version: 1`, `kind`, `scope`, `outcome`, `effect`, `severity`, `reason`,
and the structured `input` used to make the decision.

Decision styles:

- Allow: `outcome: allowed`, `effect: allow`.
- Deny: `outcome: denied`, `effect: deny`.
- Warning: `outcome: warning`, `effect: warn`.
- Requires approval: `outcome: requires_approval`, `effect: requires_approval`, with
  `requires_approval: true` and an `approval_flag`.

The evaluator currently emits decisions for manual approval, task agent approval, executor policy,
executor approval, command policy, live approval, git preflight, and file-scope guard checks.

The summary shape is intentionally compact:

- `total`: number of decisions emitted for the run or index.
- `by_kind`, `by_outcome`, `by_effect`, and `by_severity`: deterministic count maps.
- `blocking`: compact decision records whose effect is `deny` or `requires_approval`.
- `requires_approval`: compact decision records that name the required approval flag.
