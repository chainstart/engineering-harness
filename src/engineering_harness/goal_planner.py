from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from .core import DEFAULT_EXPERIENCE_PLANS, EXPERIENCE_KEYWORDS, EXPERIENCE_KIND_ALIASES
from .goal_intake import normalize_goal_intake
from .io import write_mapping


GOAL_ROADMAP_PLAN_SCHEMA_VERSION = 1
GOAL_ROADMAP_PLAN_KIND = "engineering-harness.goal-roadmap-plan.v1"
GOAL_ROADMAP_PLANNER_ID = "engineering-harness-goal-roadmap-planner"
MIN_GOAL_STAGE_COUNT = 1
DEFAULT_GOAL_STAGE_COUNT = 4
MAX_GOAL_STAGE_COUNT = 4

GOAL_IMPLEMENTATION_FILE_SCOPE = [
    "src/**",
    "app/**",
    "web/**",
    "frontend/**",
    "cli/**",
    "api/**",
    "docs/**",
    "examples/**",
    "tests/**",
    "package.json",
    "pyproject.toml",
]
GOAL_HARDENING_FILE_SCOPE = [
    ".engineering/**",
    "docs/**",
    "tests/**",
    "src/**",
    "app/**",
    "web/**",
    "cli/**",
    "api/**",
    "scripts/**",
    "examples/**",
    "package.json",
    "pyproject.toml",
]

_SELF_ITERATION_KEYWORDS = (
    "agent",
    "autonomous",
    "backtest",
    "continuous",
    "iterate",
    "iteration",
    "long-running",
    "planner",
    "proof",
    "research",
    "roadmap",
    "self-iterate",
    "theorem",
    "worker",
)


def plan_goal_roadmap(
    *,
    project_root: Path,
    project_name: str,
    profile: str,
    goal_text: str,
    blueprint_path: str | Path | None = None,
    constraints: list[str] | tuple[str, ...] | None = None,
    desired_experience_kind: str | None = None,
    stage_count: int | None = DEFAULT_GOAL_STAGE_COUNT,
) -> dict[str, Any]:
    """Build a deterministic starter roadmap proposal from a local goal-intake contract."""

    project_root = project_root.resolve()
    normalized_stage_count = _normalize_stage_count(stage_count)
    goal_intake = normalize_goal_intake(
        project_name=project_name,
        profile=profile,
        goal_text=goal_text,
        blueprint_path=blueprint_path,
        constraints=constraints,
        desired_experience_kind=desired_experience_kind,
    )
    roadmap = _starter_roadmap(
        project_root=project_root,
        goal_intake=goal_intake,
        stage_count=normalized_stage_count,
    )
    roadmap_path = project_root / ".engineering" / "roadmap.yaml"
    return {
        "schema_version": GOAL_ROADMAP_PLAN_SCHEMA_VERSION,
        "kind": GOAL_ROADMAP_PLAN_KIND,
        "status": "proposed",
        "materialized": False,
        "project": goal_intake["project"]["name"],
        "profile": goal_intake["project"]["profile"],
        "project_root": str(project_root),
        "roadmap_path": str(roadmap_path),
        "goal_intake": goal_intake,
        "experience": roadmap["experience"],
        "stage_count": len(roadmap["continuation"]["stages"]),
        "roadmap": roadmap,
    }


def materialize_goal_roadmap(
    *,
    project_root: Path,
    project_name: str,
    profile: str,
    goal_text: str,
    blueprint_path: str | Path | None = None,
    constraints: list[str] | tuple[str, ...] | None = None,
    desired_experience_kind: str | None = None,
    stage_count: int | None = DEFAULT_GOAL_STAGE_COUNT,
    force: bool = False,
) -> dict[str, Any]:
    """Write the proposed starter roadmap to `.engineering/roadmap.yaml`."""

    proposal = plan_goal_roadmap(
        project_root=project_root,
        project_name=project_name,
        profile=profile,
        goal_text=goal_text,
        blueprint_path=blueprint_path,
        constraints=constraints,
        desired_experience_kind=desired_experience_kind,
        stage_count=stage_count,
    )
    roadmap_path = Path(proposal["roadmap_path"])
    if roadmap_path.exists() and not force:
        raise FileExistsError(f"{roadmap_path} already exists. Use --force to replace it.")
    write_mapping(roadmap_path, proposal["roadmap"])
    result = deepcopy(proposal)
    result["status"] = "materialized"
    result["materialized"] = True
    return result


def _starter_roadmap(
    *,
    project_root: Path,
    goal_intake: dict[str, Any],
    stage_count: int,
) -> dict[str, Any]:
    project = goal_intake["project"]
    goal = str(goal_intake["goal"]["text"])
    profile = str(project["profile"])
    project_name = str(project["name"])
    project_slug = str(project["slug"])
    blueprint_path = goal_intake["blueprint"]["path"]
    constraints = list(goal_intake.get("constraints", []))
    experience = _experience_plan(
        project_name=project_name,
        profile=profile,
        goal_text=goal,
        blueprint_path=blueprint_path,
        explicit_kind=goal_intake["experience"]["kind"],
    )
    continuation_stages = _continuation_stages(
        project_name=project_name,
        project_slug=project_slug,
        profile=profile,
        goal_text=goal,
        constraints=constraints,
        experience=experience,
        blueprint_path=blueprint_path,
        stage_count=stage_count,
    )
    return {
        "version": 1,
        "project": project_name,
        "profile": profile,
        "default_timeout_seconds": 300,
        "state_path": ".engineering/state/harness-state.json",
        "decision_log_path": ".engineering/state/decision-log.jsonl",
        "report_dir": ".engineering/reports/tasks",
        "generated_by": GOAL_ROADMAP_PLANNER_ID,
        "planning": {
            "stage_count": len(continuation_stages),
            "stage_count_requested": stage_count,
            "stage_count_default": DEFAULT_GOAL_STAGE_COUNT,
            "stage_count_max": MAX_GOAL_STAGE_COUNT,
        },
        "generated_from": {
            "kind": goal_intake["kind"],
            "schema_version": goal_intake["schema_version"],
            "project_slug": project_slug,
            "goal": goal,
            "blueprint_path": blueprint_path,
            "constraints": constraints,
            "experience_kind": experience["kind"],
            "experience_provided": bool(goal_intake["experience"]["provided"]),
            "safety_mode": goal_intake["safety"]["mode"],
        },
        "goal": {
            "text": goal,
            "blueprint": blueprint_path,
            "constraints": constraints,
            "safety": {
                "mode": "local-only",
                "allow_live_services": False,
                "rules": list(goal_intake["safety"]["rules"]),
            },
        },
        "experience": experience,
        "milestones": [
            {
                "id": "baseline",
                "title": "Baseline Local Validation",
                "status": "active",
                "objective": (
                    "Establish local, deterministic acceptance before autonomous implementation work expands."
                ),
                "tasks": [_baseline_task(profile=profile)],
            }
        ],
        "continuation": {
            "enabled": True,
            "goal": goal,
            "blueprint": blueprint_path,
            "stages": continuation_stages,
        },
        "self_iteration": _self_iteration_guidance(
            project_root=project_root,
            project_name=project_name,
            profile=profile,
            goal_text=goal,
            blueprint_path=blueprint_path,
        ),
    }


def _baseline_task(*, profile: str) -> dict[str, Any]:
    return {
        "id": "baseline-roadmap",
        "title": "Verify starter roadmap and local safety bounds",
        "status": "pending",
        "max_attempts": 2,
        "max_task_iterations": 1,
        "file_scope": [".engineering/**", "docs/**", "tests/**", "src/**", "package.json", "pyproject.toml"],
        "acceptance": [
            {
                "name": "roadmap exists and has continuation stages",
                "command": (
                    "python3 -c \"import json; from pathlib import Path; "
                    "data=json.loads(Path('.engineering/roadmap.yaml').read_text()); "
                    "assert data.get('milestones'); "
                    "assert data.get('continuation', {}).get('stages'); "
                    "print('starter roadmap ok')\""
                ),
                "timeout_seconds": 30,
            },
            {
                "name": "roadmap remains local-only",
                "command": (
                    "python3 -c \"import json; from pathlib import Path; "
                    "data=json.loads(Path('.engineering/roadmap.yaml').read_text()); "
                    "assert data.get('goal', {}).get('safety', {}).get('allow_live_services') is False; "
                    "assert data.get('profile') == '"
                    + profile
                    + "'; "
                    "print('local safety ok')\""
                ),
                "timeout_seconds": 30,
            },
        ],
    }


def _continuation_stages(
    *,
    project_name: str,
    project_slug: str,
    profile: str,
    goal_text: str,
    constraints: list[str],
    experience: dict[str, Any],
    blueprint_path: str | None,
    stage_count: int,
) -> list[dict[str, Any]]:
    experience_kind = str(experience["kind"])
    journey = _primary_journey(experience)
    journey_id = str(journey["id"])
    blueprint_note = f" Use `{blueprint_path}` as design context." if blueprint_path else ""
    stages = [
        {
            "id": "stage-1-local-slice",
            "title": "Stage 1 Local Slice",
            "objective": f"Deliver the first local, testable slice of {project_name}.{blueprint_note}",
            "tasks": [
                _continuation_task(
                    stage_id="stage-1-local-slice",
                    task_id=f"{project_slug}-first-slice",
                    title="Implement the first locally testable goal slice",
                    goal_text=goal_text,
                    profile=profile,
                    constraints=constraints,
                    experience_kind=experience_kind,
                    journey=journey,
                    blueprint_path=blueprint_path,
                    file_scope=GOAL_IMPLEMENTATION_FILE_SCOPE,
                    implementation_focus=(
                        "Implement the first small, locally testable slice of this high-level goal. "
                        "Keep the slice narrow enough for one autonomous implementation pass and include focused tests."
                    ),
                    stage_objective=f"Deliver the first local, testable slice of {project_name}.",
                )
            ],
        },
        {
            "id": "stage-2-experience-validation",
            "title": "Stage 2 Experience Validation",
            "objective": (
                f"Turn the {experience_kind} experience plan into concrete local validation for `{journey_id}`."
            ),
            "tasks": [
                _continuation_task(
                    stage_id="stage-2-experience-validation",
                    task_id=f"{project_slug}-experience-validation",
                    title="Validate the primary user or operator journey",
                    goal_text=goal_text,
                    profile=profile,
                    constraints=constraints,
                    experience_kind=experience_kind,
                    journey=journey,
                    blueprint_path=blueprint_path,
                    file_scope=GOAL_IMPLEMENTATION_FILE_SCOPE,
                    implementation_focus=(
                        "Add or tighten local validation for the primary experience journey. "
                        "Cover the relevant persona, main surfaces, expected output, empty/error states, and documented usage."
                    ),
                    stage_objective=(
                        f"Validate the `{journey_id}` journey for {journey.get('persona')} without external accounts."
                    ),
                )
            ],
        },
        {
            "id": "stage-3-policy-evidence-observability",
            "title": "Stage 3 Policy, Evidence, and Observability",
            "objective": (
                "Harden local policy boundaries, evidence capture, and observable run state before broader automation."
            ),
            "tasks": [
                _continuation_task(
                    stage_id="stage-3-policy-evidence-observability",
                    task_id=f"{project_slug}-policy-evidence-observability",
                    title="Harden policy, evidence, and observability paths",
                    goal_text=goal_text,
                    profile=profile,
                    constraints=constraints,
                    experience_kind=experience_kind,
                    journey=journey,
                    blueprint_path=blueprint_path,
                    file_scope=GOAL_HARDENING_FILE_SCOPE,
                    implementation_focus=(
                        "Harden local safety policy, evidence artifacts, logs, reports, and status visibility. "
                        "Make failures diagnosable through deterministic files or command output."
                    ),
                    stage_objective=(
                        "Ensure policy decisions, acceptance evidence, and observable state are local, reviewable, and testable."
                    ),
                )
            ],
        },
        {
            "id": "stage-4-unattended-drive-readiness",
            "title": "Stage 4 Unattended Drive Readiness",
            "objective": (
                f"Prepare `{project_name}` for bounded unattended harness drive runs under the `{profile}` profile."
            ),
            "tasks": [
                _continuation_task(
                    stage_id="stage-4-unattended-drive-readiness",
                    task_id=f"{project_slug}-unattended-drive-readiness",
                    title="Prepare bounded unattended drive readiness",
                    goal_text=goal_text,
                    profile=profile,
                    constraints=constraints,
                    experience_kind=experience_kind,
                    journey=journey,
                    blueprint_path=blueprint_path,
                    file_scope=GOAL_HARDENING_FILE_SCOPE,
                    implementation_focus=(
                        "Make the project ready for bounded unattended local drive runs. "
                        "Review roadmap ordering, idempotent commands, timeouts, repair paths, and docs for resuming after failure."
                    ),
                    stage_objective=(
                        "Verify the backlog can continue safely without live services, credentials, deployments, or manual accounts."
                    ),
                )
            ],
        },
    ]
    return stages[:stage_count]


def _continuation_task(
    *,
    stage_id: str,
    task_id: str,
    title: str,
    goal_text: str,
    profile: str,
    constraints: list[str],
    experience_kind: str,
    journey: dict[str, Any],
    blueprint_path: str | None,
    file_scope: list[str],
    implementation_focus: str,
    stage_objective: str,
) -> dict[str, Any]:
    journey_id = str(journey.get("id", "primary-journey"))
    return {
        "id": task_id,
        "title": title,
        "status": "pending",
        "max_attempts": 2,
        "max_task_iterations": 2,
        "manual_approval_required": False,
        "agent_approval_required": True,
        "file_scope": list(file_scope),
        "implementation": [
            {
                "name": "codex implementation",
                "executor": "codex",
                "prompt": _implementation_prompt(
                    title=title,
                    stage_objective=stage_objective,
                    implementation_focus=implementation_focus,
                    goal_text=goal_text,
                    profile=profile,
                    constraints=constraints,
                    experience_kind=experience_kind,
                    journey=journey,
                    blueprint_path=blueprint_path,
                ),
                "timeout_seconds": 3600,
            }
        ],
        "repair": [
            {
                "name": "codex repair",
                "executor": "codex",
                "prompt": _repair_prompt(title=title),
                "timeout_seconds": 1800,
            }
        ],
        "acceptance": [
            {
                "name": "generated task contract is locally checkable",
                "command": _task_contract_command(stage_id=stage_id, task_id=task_id),
                "timeout_seconds": 30,
            }
        ],
        "e2e": [
            {
                "name": f"{journey_id} e2e gate is wired",
                "command": _task_e2e_contract_command(stage_id=stage_id, task_id=task_id, journey_id=journey_id),
                "timeout_seconds": 30,
            }
        ],
        "generated_by": GOAL_ROADMAP_PLANNER_ID,
    }


def _primary_journey(experience: dict[str, Any]) -> dict[str, Any]:
    journeys = experience.get("e2e_journeys")
    if isinstance(journeys, list) and journeys:
        journey = journeys[0]
        if isinstance(journey, dict):
            return journey
    return {
        "id": "primary-local-journey",
        "persona": "local operator",
        "goal": "Run the local workflow and inspect the result.",
    }


def _task_contract_command(*, stage_id: str, task_id: str) -> str:
    return _python_command(
        "import json; from pathlib import Path;",
        "data=json.loads(Path('.engineering/roadmap.yaml').read_text());",
        f"stage=next(item for item in data.get('continuation', {{}}).get('stages', []) if item.get('id') == '{stage_id}');",
        f"task=next(item for item in stage.get('tasks', []) if item.get('id') == '{task_id}');",
        "assert task.get('file_scope');",
        "assert task.get('implementation') and task['implementation'][0].get('executor') == 'codex';",
        "assert task.get('repair') and task['repair'][0].get('executor') == 'codex';",
        "assert task.get('acceptance');",
        f"print('{task_id} contract ok')",
    )


def _task_e2e_contract_command(*, stage_id: str, task_id: str, journey_id: str) -> str:
    return _python_command(
        "import json; from pathlib import Path;",
        "data=json.loads(Path('.engineering/roadmap.yaml').read_text());",
        f"stage=next(item for item in data.get('continuation', {{}}).get('stages', []) if item.get('id') == '{stage_id}');",
        f"task=next(item for item in stage.get('tasks', []) if item.get('id') == '{task_id}');",
        "assert task.get('e2e');",
        "journeys=data.get('experience', {}).get('e2e_journeys', []);",
        f"assert any(item.get('id') == '{journey_id}' for item in journeys);",
        f"print('{task_id} e2e gate ok')",
    )


def _python_command(*statements: str) -> str:
    return 'python3 -c "' + " ".join(statements) + '"'


def _implementation_prompt(
    *,
    title: str,
    stage_objective: str,
    implementation_focus: str,
    goal_text: str,
    profile: str,
    constraints: list[str],
    experience_kind: str,
    journey: dict[str, Any],
    blueprint_path: str | None,
) -> str:
    blueprint = f"\nBlueprint: `{blueprint_path}`." if blueprint_path else ""
    return (
        f"{implementation_focus}\n\n"
        f"Task: {title}.\n"
        f"Stage objective: {stage_objective}\n"
        f"Goal: {goal_text}.{blueprint}\n"
        f"Profile: {profile}.\n"
        f"Experience kind: {experience_kind}.\n"
        f"Primary E2E journey: {journey.get('id')} for {journey.get('persona')} - {journey.get('goal')}\n"
        f"{_constraints_prompt(constraints)}\n"
        "Keep the work deterministic and local. Add or update focused tests and docs. Do not use private keys, "
        "paid live services, production deployments, mainnet writes, real-fund movement, or live trading."
    )


def _repair_prompt(*, title: str) -> str:
    return (
        f"Repair the generated task `{title}` using the failing implementation, acceptance, or e2e evidence. "
        "Make the smallest local fix inside file_scope, keep checks deterministic, and update tests or docs when behavior changes. "
        "Do not add live deployments, paid services, private keys, mainnet writes, real-fund movement, or live trading."
    )


def _constraints_prompt(constraints: list[str]) -> str:
    if not constraints:
        return "Additional constraints: none."
    rendered = "; ".join(constraints)
    return f"Additional constraints: {rendered}."


def _normalize_stage_count(stage_count: int | None) -> int:
    if stage_count is None:
        return DEFAULT_GOAL_STAGE_COUNT
    try:
        value = int(stage_count)
    except (TypeError, ValueError) as exc:
        raise ValueError("stage_count must be an integer") from exc
    if value < MIN_GOAL_STAGE_COUNT or value > MAX_GOAL_STAGE_COUNT:
        raise ValueError(
            f"stage_count must be between {MIN_GOAL_STAGE_COUNT} and {MAX_GOAL_STAGE_COUNT}"
        )
    return value


def _self_iteration_guidance(
    *,
    project_root: Path,
    project_name: str,
    profile: str,
    goal_text: str,
    blueprint_path: str | None,
) -> dict[str, Any]:
    enabled = _self_iteration_is_appropriate(profile=profile, goal_text=goal_text)
    return {
        "enabled": enabled,
        "objective": f"Assess current state and append the next safe, testable stage for {project_name}.",
        "max_stages_per_iteration": 1,
        "file_scope": [".engineering/roadmap.yaml", "docs/**", "src/**", "app/**", "tests/**", "examples/**"],
        "guidance": [
            "Append new work to continuation.stages; do not mark tasks complete.",
            "Each generated task must include implementation, repair, acceptance, and e2e gates when user-visible behavior is affected.",
            "Keep all checks local and deterministic; do not require private credentials, paid live services, deployments, or live trading.",
        ],
        "planner": {
            "name": "Codex self-iteration planner",
            "executor": "codex",
            "timeout_seconds": 3600,
            "sandbox": "workspace-write",
            "prompt": _self_iteration_prompt(goal_text=goal_text, blueprint_path=blueprint_path, project_root=project_root),
        },
    }


def _self_iteration_prompt(*, goal_text: str, blueprint_path: str | None, project_root: Path) -> str:
    blueprint = f" Use the blueprint at `{blueprint_path}` when it exists." if blueprint_path else ""
    return (
        f"Project root: {project_root}. Goal: {goal_text}.{blueprint} "
        "Append exactly one continuation stage with concrete, local acceptance and e2e gates. "
        "Do not call paid APIs, require accounts, deploy services, move funds, or use private keys."
    )


def _experience_plan(
    *,
    project_name: str,
    profile: str,
    goal_text: str,
    blueprint_path: str | None,
    explicit_kind: str | None,
) -> dict[str, Any]:
    if explicit_kind:
        plan = deepcopy(DEFAULT_EXPERIENCE_PLANS[explicit_kind])
        plan["derived"] = False
        plan["provided_by"] = "goal-intake"
        plan["derivation_rationale"] = [
            f"explicit experience kind: {explicit_kind}",
            f"profile: {profile}",
        ]
        return plan

    kind, rationale = _derive_experience_kind(
        project_name=project_name,
        profile=profile,
        goal_text=goal_text,
        blueprint_path=blueprint_path,
    )
    plan = deepcopy(DEFAULT_EXPERIENCE_PLANS[kind])
    plan["derived"] = True
    plan["derived_by"] = GOAL_ROADMAP_PLANNER_ID
    plan["derivation_rationale"] = rationale
    return plan


def _derive_experience_kind(
    *,
    project_name: str,
    profile: str,
    goal_text: str,
    blueprint_path: str | None,
) -> tuple[str, list[str]]:
    hint_text = " ".join(item for item in (project_name, profile, goal_text, str(blueprint_path or "")) if item).lower()
    priority = ["submission-review", "multi-role-app", "api-only", "cli-only", "dashboard"]
    for kind in priority:
        matches = _keyword_matches(hint_text, EXPERIENCE_KIND_ALIASES[kind])
        if matches:
            return kind, _rationale(profile=profile, decision=f"matched {kind} goal hint", matches=matches)

    matches_by_kind = {
        kind: _keyword_matches(hint_text, keywords)
        for kind, keywords in EXPERIENCE_KEYWORDS.items()
    }
    scores = {kind: len(matches) for kind, matches in matches_by_kind.items()}
    if profile in {"python-agent", "agent-monorepo"}:
        scores["dashboard"] += 1
    if profile in {"trading-research", "evm-security-research", "lean-formalization"}:
        scores["dashboard"] += 2
    thresholds = {
        "submission-review": 2,
        "multi-role-app": 2,
        "api-only": 2,
        "cli-only": 2,
        "dashboard": 1,
    }
    candidates = [kind for kind in priority if scores[kind] >= thresholds[kind]]
    if candidates:
        chosen = max(candidates, key=lambda kind: (scores[kind], -priority.index(kind)))
        return chosen, _rationale(
            profile=profile,
            decision=f"matched {chosen} goal signals",
            matches=matches_by_kind[chosen],
        )
    return "dashboard", _rationale(profile=profile, decision="defaulted to dashboard experience", matches=[])


def _self_iteration_is_appropriate(*, profile: str, goal_text: str) -> bool:
    if profile in {"agent-monorepo", "python-agent", "trading-research", "lean-formalization"}:
        return True
    return bool(_keyword_matches(goal_text.lower(), _SELF_ITERATION_KEYWORDS))


def _keyword_matches(text: str, keywords: tuple[str, ...]) -> list[str]:
    matches: list[str] = []
    for keyword in keywords:
        expression = re.escape(keyword.lower()).replace(r"\ ", r"\s+")
        if re.search(rf"(?<![a-z0-9]){expression}(?![a-z0-9])", text):
            matches.append(keyword)
    return matches


def _rationale(*, profile: str, decision: str, matches: list[str]) -> list[str]:
    rationale = [decision]
    if profile:
        rationale.append(f"profile: {profile}")
    if matches:
        rationale.append("matched hints: " + ", ".join(matches[:6]))
    return rationale
