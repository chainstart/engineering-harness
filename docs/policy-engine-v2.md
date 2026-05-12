# Policy Engine V2 Schema

Policy Engine V2 records the data used for harness safety checks as a structured `policy_input`
contract and emits normalized `policy_decisions` from that input. The current Python evaluator
preserves the existing command allowlist, executor approval, manual approval, live flag, git
preflight, and file-scope behavior.

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

The evaluator currently emits decisions for manual approval, task agent approval, executor policy
or executor approval, command policy, git preflight, and file-scope guard checks.
