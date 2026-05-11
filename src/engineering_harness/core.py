from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import os
import re
import subprocess
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .executors import (
    EXECUTOR_RESULT_CONTRACT_VERSION,
    ExecutorInvocation,
    ExecutorRegistry,
    ExecutorTaskCommand,
    ExecutorTaskContext,
    default_executor_registry,
)
from .io import append_jsonl, load_mapping, write_json, write_mapping
from .profiles import command_policy, default_roadmap


COMPLETED_STATUSES = {"done", "passed", "skipped"}
BLOCKED_STATUSES = {"blocked", "paused"}
CONFIG_CANDIDATES = (".engineering/roadmap.yaml", ".engineering/roadmap.json", "ops/engineering/roadmap.yaml")
PRUNE_DIRS = {".git", "node_modules", ".venv", "venv", ".pytest_cache", "dist", "out", "cache", "artifacts"}
EXPERIENCE_KINDS = {"dashboard", "submission-review", "multi-role-app", "api-only", "cli-only"}
DEFAULT_EXPERIENCE_PLANS: dict[str, dict[str, Any]] = {
    "dashboard": {
        "kind": "dashboard",
        "personas": ["operator"],
        "primary_surfaces": ["operator dashboard", "run queue", "artifact viewer"],
        "auth": {"required": False, "roles": []},
        "e2e_journeys": [
            {
                "id": "operator-observes-run",
                "persona": "operator",
                "goal": "Inspect queued work, follow run status, and review latest artifacts or errors.",
            }
        ],
    },
    "submission-review": {
        "kind": "submission-review",
        "personas": ["student", "reviewer"],
        "primary_surfaces": ["submission portal", "review console", "revision upload", "status timeline"],
        "auth": {"required": True, "roles": ["student", "reviewer"]},
        "e2e_journeys": [
            {
                "id": "student-submit-review-revise",
                "persona": "student",
                "goal": "Submit work, receive reviewer comments, upload a revision, and view the decision.",
            }
        ],
    },
    "multi-role-app": {
        "kind": "multi-role-app",
        "personas": ["admin", "operator", "approver"],
        "primary_surfaces": ["login", "admin console", "operator queue", "approval screen", "audit log"],
        "auth": {"required": True, "roles": ["admin", "operator", "approver"]},
        "e2e_journeys": [
            {
                "id": "operator-requests-approval",
                "persona": "operator",
                "goal": "Create a work item, request approval, and verify role boundaries and audit history.",
            }
        ],
    },
    "api-only": {
        "kind": "api-only",
        "personas": ["api client"],
        "primary_surfaces": ["API docs", "OpenAPI schema", "example client journey", "service status"],
        "auth": {"required": False, "roles": []},
        "e2e_journeys": [
            {
                "id": "client-runs-api-example",
                "persona": "api client",
                "goal": "Run the documented API example and verify the expected response.",
            }
        ],
    },
    "cli-only": {
        "kind": "cli-only",
        "personas": ["developer"],
        "primary_surfaces": ["command line", "documented examples", "generated reports"],
        "auth": {"required": False, "roles": []},
        "e2e_journeys": [
            {
                "id": "developer-runs-cli",
                "persona": "developer",
                "goal": "Run the documented command and inspect the generated output or report.",
            }
        ],
    },
}
EXPERIENCE_KIND_ALIASES: dict[str, tuple[str, ...]] = {
    "submission-review": (
        "submission-review",
        "submission review",
        "student review",
        "paper review",
        "review workflow",
    ),
    "multi-role-app": ("multi-role-app", "multi-role", "multi role", "role-specific", "role based"),
    "api-only": ("api-only", "api only", "api-first", "api first", "rest api", "openapi", "api"),
    "cli-only": ("cli-only", "cli only", "command line", "command-line", "cli"),
    "dashboard": ("dashboard", "operator console", "run queue"),
}
EXPERIENCE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "submission-review": (
        "submission",
        "submit",
        "student",
        "reviewer",
        "review",
        "revision",
        "paper",
        "assignment",
        "comments",
        "decision",
        "grade",
    ),
    "multi-role-app": (
        "role",
        "roles",
        "rbac",
        "permission",
        "permissions",
        "admin",
        "operator",
        "approver",
        "approval",
        "audit",
        "login",
        "auth",
        "authenticated",
    ),
    "api-only": (
        "api",
        "rest",
        "openapi",
        "swagger",
        "endpoint",
        "endpoints",
        "graphql",
        "client",
        "http",
        "curl",
        "sdk",
    ),
    "cli-only": (
        "cli",
        "command-line",
        "command line",
        "terminal",
        "argparse",
        "typer",
        "click",
        "subcommand",
        "documented command",
    ),
    "dashboard": (
        "dashboard",
        "autonomous",
        "agent",
        "worker",
        "research",
        "run queue",
        "status",
        "artifact",
        "artifacts",
        "theorem",
        "proof",
        "backtest",
        "monitor",
        "observability",
    ),
}
FRONTEND_TASK_MILESTONE_ID = "frontend-visualization"
FRONTEND_TASK_GENERATOR = "engineering-harness-frontend-task-generator"
FRONTEND_KIND_LABELS = {
    "dashboard": "operator dashboard",
    "submission-review": "submission review workflow",
    "multi-role-app": "multi-role application",
    "api-only": "API-first experience",
    "cli-only": "CLI-first experience",
}
FRONTEND_KIND_TASK_GUIDANCE: dict[str, dict[str, Any]] = {
    "dashboard": {
        "file_scope": [
            "src/**",
            "app/**",
            "web/**",
            "frontend/**",
            "ui/**",
            "components/**",
            "tests/**",
            "docs/**",
            "templates/**",
            "package.json",
            "pyproject.toml",
        ],
        "acceptance_terms": ["dashboard", "status", "queue", "artifact", "loading", "empty", "error"],
        "implementation_focus": (
            "Build or document the operator dashboard using the project's existing UI conventions. "
            "Cover status, queue/detail state, artifacts, loading, empty, and error states."
        ),
        "journey_candidates": (
            "tests/e2e/{slug}.spec.ts",
            "tests/e2e/{slug}.spec.js",
            "tests/e2e/{slug}.test.ts",
            "tests/e2e/{slug}.py",
            "e2e/{slug}.spec.ts",
            "docs/e2e/{slug}.md",
        ),
    },
    "submission-review": {
        "file_scope": [
            "src/**",
            "app/**",
            "web/**",
            "frontend/**",
            "ui/**",
            "components/**",
            "tests/**",
            "docs/**",
            "templates/**",
            "package.json",
            "pyproject.toml",
        ],
        "acceptance_terms": ["submission", "reviewer", "revision", "comments", "decision", "status timeline"],
        "implementation_focus": (
            "Build or document the student and reviewer surfaces using the project's existing stack. "
            "Cover submission upload, reviewer comments, revision upload, decision state, and timeline states."
        ),
        "journey_candidates": (
            "tests/e2e/{slug}.spec.ts",
            "tests/e2e/{slug}.spec.js",
            "tests/e2e/{slug}.test.ts",
            "tests/e2e/{slug}.py",
            "e2e/{slug}.spec.ts",
            "docs/e2e/{slug}.md",
        ),
    },
    "multi-role-app": {
        "file_scope": [
            "src/**",
            "app/**",
            "web/**",
            "frontend/**",
            "ui/**",
            "components/**",
            "tests/**",
            "docs/**",
            "templates/**",
            "package.json",
            "pyproject.toml",
        ],
        "acceptance_terms": ["role", "login", "permission", "approval", "audit", "denied"],
        "implementation_focus": (
            "Build or document authenticated role-specific surfaces using the project's existing stack. "
            "Cover login, role routes, permission denial, approval handoff, and audit history."
        ),
        "journey_candidates": (
            "tests/e2e/{slug}.spec.ts",
            "tests/e2e/{slug}.spec.js",
            "tests/e2e/{slug}.test.ts",
            "tests/e2e/{slug}.py",
            "e2e/{slug}.spec.ts",
            "docs/e2e/{slug}.md",
        ),
    },
    "api-only": {
        "file_scope": [
            "src/**",
            "api/**",
            "openapi/**",
            "docs/**",
            "examples/**",
            "tests/**",
            "templates/**",
            "package.json",
            "pyproject.toml",
        ],
        "acceptance_terms": ["api", "openapi", "example", "request", "response", "status"],
        "implementation_focus": (
            "Build or document the API-first user path without requiring a browser UI. "
            "Cover API reference, example client flow, request/response expectations, auth if required, and service status."
        ),
        "journey_candidates": (
            "tests/e2e/{slug}.py",
            "tests/api/{slug}.py",
            "tests/e2e/{slug}.sh",
            "examples/{slug}.md",
            "docs/e2e/{slug}.md",
        ),
    },
    "cli-only": {
        "file_scope": [
            "src/**",
            "cli/**",
            "docs/**",
            "examples/**",
            "tests/**",
            "templates/**",
            "package.json",
            "pyproject.toml",
        ],
        "acceptance_terms": ["cli", "command", "example", "output", "report"],
        "implementation_focus": (
            "Build or document the CLI-first user path without requiring a browser UI. "
            "Cover documented commands, inputs, output/report inspection, failure messages, and repeatable examples."
        ),
        "journey_candidates": (
            "tests/e2e/{slug}.py",
            "tests/cli/{slug}.py",
            "tests/e2e/{slug}.sh",
            "examples/{slug}.md",
            "docs/e2e/{slug}.md",
        ),
    },
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slug_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def redact(text: str) -> str:
    redacted = text
    for marker in ("PRIVATE_KEY=", "OPENAI_API_KEY=", "ANTHROPIC_API_KEY=", "MNEMONIC="):
        while marker in redacted:
            before, _, after = redacted.partition(marker)
            token = after.split()[0] if after.split() else ""
            redacted = before + marker + "[REDACTED]" + after[len(token) :]
    return redacted


@dataclass(frozen=True)
class Project:
    name: str
    root: Path
    roadmap_path: Path | None
    profile: str | None
    configured: bool
    kind: str


@dataclass(frozen=True)
class AcceptanceCommand:
    name: str
    command: str | None
    timeout_seconds: int
    required: bool = True
    executor: str = "shell"
    prompt: str | None = None
    model: str | None = None
    sandbox: str = "workspace-write"


@dataclass(frozen=True)
class HarnessTask:
    id: str
    title: str
    milestone_id: str
    milestone_title: str
    status: str
    max_attempts: int
    file_scope: tuple[str, ...]
    manual_approval_required: bool
    agent_approval_required: bool
    max_task_iterations: int
    implementation: tuple[AcceptanceCommand, ...]
    repair: tuple[AcceptanceCommand, ...]
    acceptance: tuple[AcceptanceCommand, ...]
    e2e: tuple[AcceptanceCommand, ...]


@dataclass(frozen=True)
class CommandRun:
    phase: str
    name: str
    command: str
    status: str
    returncode: int | None
    started_at: str
    finished_at: str
    stdout: str
    stderr: str
    executor: str = "shell"
    executor_metadata: dict[str, Any] = field(default_factory=dict)
    result_metadata: dict[str, Any] = field(default_factory=dict)


def find_project_config(root: Path) -> Path | None:
    for relative in CONFIG_CANDIDATES:
        path = root / relative
        if path.exists():
            return path
    return None


def guess_profile(root: Path) -> tuple[str | None, str]:
    if (root / "foundry.toml").exists() or any(root.glob("*/foundry.toml")):
        return "evm-protocol", "evm"
    if (root / "pyproject.toml").exists() or (root / "requirements.txt").exists():
        return "python-agent", "python"
    if (root / "package.json").exists():
        return "node-frontend", "node"
    if any((root / child).exists() for child in ("ara", "agents", "runtime")):
        return "python-agent", "agent"
    return None, "unknown"


def project_from_root(root: Path) -> Project:
    root = root.resolve()
    config = find_project_config(root)
    profile, kind = guess_profile(root)
    configured = config is not None
    name = root.name
    if config:
        try:
            roadmap = load_mapping(config)
            name = str(roadmap.get("project", name))
            profile = str(roadmap.get("profile", profile or "")) or profile
        except Exception:
            pass
    return Project(name=name, root=root, roadmap_path=config, profile=profile, configured=configured, kind=kind)


def discover_projects(workspace: Path, max_depth: int = 3) -> list[Project]:
    workspace = workspace.resolve()
    projects: dict[Path, Project] = {}
    for current, dirs, files in os.walk(workspace):
        current_path = Path(current)
        depth = len(current_path.relative_to(workspace).parts)
        dirs[:] = [name for name in dirs if name not in PRUNE_DIRS and not name.startswith(".cache")]
        if depth >= max_depth:
            dirs[:] = []
        has_git = ".git" in dirs or ".git" in files or (current_path / ".git").exists()
        has_config = find_project_config(current_path) is not None
        has_known = any((current_path / name).exists() for name in ("package.json", "pyproject.toml", "foundry.toml"))
        if current_path != workspace and (has_git or has_config or has_known):
            project = project_from_root(current_path)
            projects[current_path] = project
            if has_git or has_config:
                dirs[:] = [name for name in dirs if name.startswith(".engineering")]
    return sorted(projects.values(), key=lambda item: str(item.root))


def init_project(project_root: Path, profile_id: str, name: str | None = None, force: bool = False) -> dict[str, Any]:
    project_root = project_root.resolve()
    engineering = project_root / ".engineering"
    roadmap_path = engineering / "roadmap.yaml"
    policy_dir = engineering / "policies"
    state_dir = engineering / "state"
    report_dir = engineering / "reports"
    if roadmap_path.exists() and not force:
        raise FileExistsError(f"{roadmap_path} already exists. Use --force to replace it.")
    project_name = name or project_root.name
    write_json(roadmap_path, default_roadmap(project_name, profile_id))
    write_json(policy_dir / "command-allowlist.yaml", command_policy(profile_id))
    write_json(
        policy_dir / "deployment-policy.yaml",
        {
            "version": 1,
            "profile": profile_id,
            "requires_human_approval": [
                "mainnet deployment",
                "contract upgrade or migration",
                "private key or API key configuration",
                "real-fund transfer",
                "live trading",
            ],
        },
    )
    write_json(
        policy_dir / "secret-policy.yaml",
        {
            "version": 1,
            "rules": [
                "Do not write secrets into tracked files.",
                "Use environment variables for private keys and API keys.",
                "Reports must redact secret-looking values before becoming versioned artifacts.",
            ],
        },
    )
    for directory in (state_dir, report_dir):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / ".gitignore").write_text("*\n!.gitignore\n!.gitkeep\n", encoding="utf-8")
        (directory / ".gitkeep").write_text("", encoding="utf-8")
    return {
        "project": project_name,
        "profile": profile_id,
        "roadmap": str(roadmap_path),
        "policy_dir": str(policy_dir),
    }


class Harness:
    def __init__(
        self,
        project_root: Path,
        roadmap_path: Path | None = None,
        executor_registry: ExecutorRegistry | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.roadmap_path = roadmap_path or find_project_config(self.project_root)
        if self.roadmap_path is None:
            raise FileNotFoundError(f"No engineering roadmap found in {self.project_root}")
        self.roadmap = load_mapping(self.roadmap_path)
        self.default_timeout = int(self.roadmap.get("default_timeout_seconds", 300))
        self.state_path = self.project_root / str(self.roadmap.get("state_path", ".engineering/state/harness-state.json"))
        self.decision_log_path = self.project_root / str(
            self.roadmap.get("decision_log_path", ".engineering/state/decision-log.jsonl")
        )
        self.report_dir = self.project_root / str(self.roadmap.get("report_dir", ".engineering/reports/tasks"))
        manifest_index_path = self.roadmap.get("manifest_index_path")
        self.manifest_index_path = (
            self.project_root / str(manifest_index_path)
            if manifest_index_path
            else self.report_dir / "manifest-index.json"
        )
        self.command_policy = self._load_command_policy()
        self.executor_registry = executor_registry or default_executor_registry()

    def _load_command_policy(self) -> dict[str, Any]:
        policy_candidates = [
            self.project_root / ".engineering/policies/command-allowlist.yaml",
            self.project_root / "ops/engineering/policies/command-allowlist.yaml",
        ]
        for path in policy_candidates:
            if path.exists():
                return load_mapping(path)
        profile = str(self.roadmap.get("profile", "")) or "python-agent"
        return command_policy(profile)

    def load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"version": 1, "updated_at": None, "tasks": {}}
        payload = load_mapping(self.state_path)
        if "tasks" not in payload or not isinstance(payload["tasks"], dict):
            payload["tasks"] = {}
        return payload

    def save_state(self, state: dict[str, Any]) -> None:
        state["version"] = 1
        state["updated_at"] = utc_now()
        write_json(self.state_path, state)

    def save_roadmap(self) -> None:
        write_mapping(self.roadmap_path, self.roadmap)

    def continuation_summary(self) -> dict[str, Any]:
        config = self.roadmap.get("continuation") or {}
        if not isinstance(config, dict):
            config = {}
        stages = config.get("stages") or []
        if not isinstance(stages, list):
            stages = []
        existing = {str(item.get("id")) for item in self.roadmap.get("milestones", [])}
        pending = [stage for stage in stages if str(stage.get("id")) not in existing]
        return {
            "enabled": bool(config.get("enabled", False)),
            "goal": config.get("goal"),
            "blueprint": config.get("blueprint"),
            "stage_count": len(stages),
            "pending_stage_count": len(pending),
            "next_stage": self._continuation_stage_payload(pending[0]) if pending else None,
        }

    def self_iteration_summary(self) -> dict[str, Any]:
        config = self.roadmap.get("self_iteration") or {}
        if not isinstance(config, dict):
            config = {}
        planner = config.get("planner") or {}
        if not isinstance(planner, dict):
            planner = {}
        return {
            "enabled": bool(config.get("enabled", False)),
            "objective": config.get("objective"),
            "planner_executor": str(planner.get("executor", "shell")) if planner else None,
            "max_stages_per_iteration": int(config.get("max_stages_per_iteration", 1)),
        }

    def advance_roadmap(self, *, max_new_milestones: int = 1, reason: str = "queue_empty") -> dict[str, Any]:
        config = self.roadmap.get("continuation") or {}
        if not isinstance(config, dict) or not config.get("enabled", False):
            return {
                "status": "disabled",
                "message": "roadmap continuation is not enabled",
                "milestones_added": [],
                "tasks_added": 0,
            }
        stages = config.get("stages") or []
        if not isinstance(stages, list):
            return {
                "status": "error",
                "message": "continuation.stages must be a list",
                "milestones_added": [],
                "tasks_added": 0,
            }
        existing_milestones = {str(item.get("id")) for item in self.roadmap.get("milestones", [])}
        existing_tasks = {task.id for task in self.iter_tasks()}
        materialized: list[dict[str, Any]] = []
        tasks_added = 0
        for stage in stages:
            if len(materialized) >= max_new_milestones:
                break
            stage_id = str(stage.get("id", ""))
            if not stage_id or stage_id in existing_milestones:
                continue
            milestone = self._materialize_continuation_stage(stage, existing_tasks=existing_tasks)
            self.roadmap.setdefault("milestones", []).append(milestone)
            existing_milestones.add(stage_id)
            for task in milestone.get("tasks", []):
                existing_tasks.add(str(task["id"]))
            task_count = len(milestone.get("tasks", []))
            tasks_added += task_count
            materialized.append({"id": stage_id, "title": milestone.get("title", stage_id), "tasks": task_count})
        if not materialized:
            return {
                "status": "exhausted",
                "message": "no unmaterialized continuation stage remains",
                "milestones_added": [],
                "tasks_added": 0,
            }
        self.save_roadmap()
        event = {
            "at": utc_now(),
            "event": "roadmap_continuation",
            "reason": reason,
            "milestones_added": materialized,
            "tasks_added": tasks_added,
            "goal": config.get("goal"),
        }
        append_jsonl(self.decision_log_path, event)
        return {
            "status": "advanced",
            "message": f"materialized {len(materialized)} continuation milestone(s)",
            "milestones_added": materialized,
            "tasks_added": tasks_added,
        }

    def run_self_iteration(
        self,
        *,
        reason: str = "roadmap_exhausted",
        allow_agent: bool = False,
        allow_live: bool = False,
    ) -> dict[str, Any]:
        config = self.roadmap.get("self_iteration") or {}
        if not isinstance(config, dict) or not config.get("enabled", False):
            return {
                "status": "disabled",
                "message": "self_iteration is not enabled",
                "stage_count_before": self.continuation_summary()["stage_count"],
                "stage_count_after": self.continuation_summary()["stage_count"],
                "pending_stage_count_after": self.continuation_summary()["pending_stage_count"],
                "report": None,
            }
        planner = config.get("planner") or {}
        if not isinstance(planner, dict):
            return {
                "status": "error",
                "message": "self_iteration.planner must be a mapping",
                "stage_count_before": self.continuation_summary()["stage_count"],
                "stage_count_after": self.continuation_summary()["stage_count"],
                "pending_stage_count_after": self.continuation_summary()["pending_stage_count"],
                "report": None,
            }

        before = self.continuation_summary()
        snapshot = {
            "generated_at": utc_now(),
            "reason": reason,
            "status": self.status_summary(),
            "recent_git": self._git(["log", "--oneline", "-8"]),
            "git_status": self._git(["status", "--short"]),
        }
        assessment_dir = self.report_dir / "assessments"
        assessment_dir.mkdir(parents=True, exist_ok=True)
        snapshot_path = assessment_dir / f"{slug_now()}-self-iteration-snapshot.json"
        write_json(snapshot_path, snapshot)
        report_path = assessment_dir / f"{slug_now()}-self-iteration.md"

        command = self._parse_task_commands([planner], default_name="self-iteration-planner")[0]
        executor = self.executor_registry.get(command.executor)
        if executor is None:
            run = CommandRun(
                "self-iteration",
                command.name,
                self._display_command(command, self._self_iteration_task(command, snapshot_path, "")),
                "blocked",
                None,
                utc_now(),
                utc_now(),
                "",
                f"unknown executor: {command.executor}",
                executor=command.executor,
                executor_metadata=self.executor_registry.metadata_for(command.executor),
            )
            self._write_self_iteration_report(report_path, reason, snapshot_path, before, before, run)
            return {
                "status": "blocked",
                "message": f"unknown executor: {command.executor}",
                "stage_count_before": before["stage_count"],
                "stage_count_after": before["stage_count"],
                "pending_stage_count_after": before["pending_stage_count"],
                "report": str(report_path.relative_to(self.project_root)),
            }
        if executor.metadata.requires_agent_approval and not allow_agent:
            block_reason = f"{command.executor} planner requires --allow-agent"
            run = CommandRun(
                "self-iteration",
                command.name,
                self._display_command(command, self._self_iteration_task(command, snapshot_path, "")),
                "blocked",
                None,
                utc_now(),
                utc_now(),
                "",
                block_reason,
                executor=command.executor,
                executor_metadata=executor.metadata.as_contract(),
            )
            self._write_self_iteration_report(report_path, reason, snapshot_path, before, before, run)
            return {
                "status": "blocked",
                "message": block_reason,
                "stage_count_before": before["stage_count"],
                "stage_count_after": before["stage_count"],
                "pending_stage_count_after": before["pending_stage_count"],
                "report": str(report_path.relative_to(self.project_root)),
            }
        if executor.metadata.uses_command_policy:
            allowed, block_reason = self.command_allowed(command.command, allow_live=allow_live)
            if not allowed:
                run = CommandRun(
                    "self-iteration",
                    command.name,
                    self._display_command(command, self._self_iteration_task(command, snapshot_path, "")),
                    "blocked",
                    None,
                    utc_now(),
                    utc_now(),
                    "",
                    block_reason,
                    executor=command.executor,
                    executor_metadata=executor.metadata.as_contract(),
                )
                self._write_self_iteration_report(report_path, reason, snapshot_path, before, before, run)
                return {
                    "status": "blocked",
                    "message": block_reason,
                    "stage_count_before": before["stage_count"],
                    "stage_count_after": before["stage_count"],
                    "pending_stage_count_after": before["pending_stage_count"],
                    "report": str(report_path.relative_to(self.project_root)),
                }

        planner_prompt = self._self_iteration_prompt(config, snapshot_path)
        planner_task = self._self_iteration_task(command, snapshot_path, planner_prompt)
        command = replace(command, prompt=planner_prompt)
        run = self._run_command(command, phase="self-iteration", task=planner_task)

        self.roadmap = load_mapping(self.roadmap_path)
        after = self.continuation_summary()
        if run.returncode != 0:
            status = "failed"
            message = f"self-iteration planner failed: {command.name}"
        elif after["stage_count"] > before["stage_count"] or after["pending_stage_count"] > before["pending_stage_count"]:
            status = "planned"
            message = "self-iteration planner added continuation stage(s)"
        else:
            status = "stalled"
            message = "self-iteration planner did not add continuation stages"

        self._write_self_iteration_report(report_path, reason, snapshot_path, before, after, run)
        append_jsonl(
            self.decision_log_path,
            {
                "at": utc_now(),
                "event": "self_iteration",
                "reason": reason,
                "status": status,
                "message": message,
                "stage_count_before": before["stage_count"],
                "stage_count_after": after["stage_count"],
                "pending_stage_count_after": after["pending_stage_count"],
                "report": str(report_path.relative_to(self.project_root)),
            },
        )
        return {
            "status": status,
            "message": message,
            "stage_count_before": before["stage_count"],
            "stage_count_after": after["stage_count"],
            "pending_stage_count_after": after["pending_stage_count"],
            "report": str(report_path.relative_to(self.project_root)),
            "run": {
                "name": run.name,
                "command": run.command,
                "status": run.status,
                "returncode": run.returncode,
            },
        }

    def _self_iteration_task(self, command: AcceptanceCommand, snapshot_path: Path, prompt: str) -> HarnessTask:
        return HarnessTask(
            id="self-iteration-planner",
            title="Self-assess current state and generate the next roadmap stage",
            milestone_id="self-iteration",
            milestone_title="Autonomous Self Iteration",
            status="pending",
            max_attempts=1,
            file_scope=tuple(str(scope) for scope in (self.roadmap.get("self_iteration") or {}).get("file_scope", [".engineering/**", "docs/**"])),
            manual_approval_required=False,
            agent_approval_required=bool(self.executor_registry.metadata_for(command.executor).get("requires_agent_approval")),
            max_task_iterations=1,
            implementation=(),
            repair=(),
            acceptance=(
                AcceptanceCommand(
                    name="roadmap has new continuation stage",
                    command=f"python3 -c \"import json; from pathlib import Path; x=json.loads(Path('{self.roadmap_path}').read_text()); assert x.get('continuation', {{}}).get('stages')\"",
                    timeout_seconds=60,
                ),
            ),
            e2e=(),
        )

    def _self_iteration_prompt(self, config: dict[str, Any], snapshot_path: Path) -> str:
        custom = str((config.get("planner") or {}).get("prompt", "")).strip()
        objective = str(config.get("objective", "Assess current project status and plan the next engineering stage."))
        max_stages = int(config.get("max_stages_per_iteration", 1))
        base = f"""
You are the self-iteration planner for an autonomous engineering harness.

Project root: {self.project_root}
Roadmap file: {self.roadmap_path}
Status snapshot: {snapshot_path}
Objective: {objective}

Read the repository, the roadmap file, and the status snapshot. Assess what has just been completed,
identify the next highest-value engineering stage, and append exactly {max_stages} new unmaterialized
stage(s) to `continuation.stages` in the roadmap file.

Rules:
- Do not edit `.engineering/state` or `.engineering/reports`.
- Do not mark tasks done and do not add generated stages to `milestones`.
- New stages must be concrete, measurable, and automatable.
- Each task must include acceptance commands.
- If code must be written, use an `implementation` entry with `"executor": "codex"` and a focused prompt.
- Include a `repair` entry for non-trivial implementation tasks.
- Do not require live private keys, Sepolia writes, mainnet writes, paid services, or external accounts.
- Prefer the next step that moves the project toward the stated blueprint and vision.
- Keep scope tight enough that a coding agent can complete the stage in one iteration.
"""
        return base if not custom else f"{base}\n\nProject-specific planning guidance:\n{custom}\n"

    def _write_self_iteration_report(
        self,
        report_path: Path,
        reason: str,
        snapshot_path: Path,
        before: dict[str, Any],
        after: dict[str, Any],
        run: CommandRun,
    ) -> None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# Self Iteration Report",
            "",
            f"- Reason: `{reason}`",
            f"- Snapshot: `{snapshot_path.relative_to(self.project_root)}`",
            f"- Before stages: `{before.get('stage_count')}` pending `{before.get('pending_stage_count')}`",
            f"- After stages: `{after.get('stage_count')}` pending `{after.get('pending_stage_count')}`",
            "",
            "## Planner Run",
            "",
            f"- Name: {run.name}",
            f"- Status: `{run.status}`",
            f"- Return code: `{run.returncode}`",
            "",
            "```bash",
            run.command,
            "```",
            "",
        ]
        if run.stdout:
            lines.extend(["Stdout:", "", "```text", run.stdout, "```", ""])
        if run.stderr:
            lines.extend(["Stderr:", "", "```text", run.stderr, "```", ""])
        report_path.write_text("\n".join(lines), encoding="utf-8")

    def _continuation_stage_payload(self, stage: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": str(stage.get("id", "")),
            "title": str(stage.get("title", stage.get("id", ""))),
            "objective": str(stage.get("objective", "")),
            "task_count": len(stage.get("tasks", []) if isinstance(stage.get("tasks", []), list) else []),
        }

    def _materialize_continuation_stage(
        self,
        stage: dict[str, Any],
        *,
        existing_tasks: set[str],
    ) -> dict[str, Any]:
        stage_id = str(stage.get("id", ""))
        if not stage_id:
            raise ValueError("continuation stage is missing id")
        tasks = stage.get("tasks") or []
        if not isinstance(tasks, list) or not tasks:
            raise ValueError(f"continuation stage {stage_id} must define at least one task")
        materialized_tasks = []
        for task in tasks:
            task_id = str(task.get("id", ""))
            if not task_id:
                raise ValueError(f"continuation stage {stage_id} has a task without id")
            if task_id in existing_tasks:
                raise ValueError(f"continuation task id already exists: {task_id}")
            acceptance = task.get("acceptance") or []
            if not isinstance(acceptance, list) or not acceptance:
                raise ValueError(f"continuation task {task_id} must define acceptance commands")
            materialized_tasks.append(
                {
                    "id": task_id,
                    "title": str(task.get("title", task_id)),
                    "status": str(task.get("status", "pending")),
                    "max_attempts": int(task.get("max_attempts", 2)),
                    "max_task_iterations": int(task.get("max_task_iterations", 1)),
                    "manual_approval_required": bool(task.get("manual_approval_required", False)),
                    "agent_approval_required": bool(task.get("agent_approval_required", False)),
                    "file_scope": list(task.get("file_scope", [])),
                    "implementation": task.get("implementation", []),
                    "repair": task.get("repair", []),
                    "acceptance": acceptance,
                    "generated_by": "engineering-harness-continuation",
                    "generated_at": utc_now(),
                }
            )
        return {
            "id": stage_id,
            "title": str(stage.get("title", stage_id)),
            "status": str(stage.get("status", "planned")),
            "objective": str(stage.get("objective", "")),
            "generated_by": "engineering-harness-continuation",
            "generated_at": utc_now(),
            "tasks": materialized_tasks,
        }

    def iter_tasks(self) -> list[HarnessTask]:
        tasks: list[HarnessTask] = []
        for milestone in self.roadmap.get("milestones", []):
            if str(milestone.get("status", "planned")) in BLOCKED_STATUSES:
                continue
            for task in milestone.get("tasks", []):
                implementation = self._parse_task_commands(task.get("implementation", []), default_name="implementation")
                repair = self._parse_task_commands(task.get("repair", []), default_name="repair")
                acceptance = self._parse_task_commands(task.get("acceptance", []), default_name="acceptance")
                e2e = self._parse_task_commands(task.get("e2e", []), default_name="e2e")
                tasks.append(
                    HarnessTask(
                        id=str(task["id"]),
                        title=str(task.get("title", task["id"])),
                        milestone_id=str(milestone["id"]),
                        milestone_title=str(milestone.get("title", milestone["id"])),
                        status=str(task.get("status", "pending")),
                        max_attempts=int(task.get("max_attempts", 3)),
                        file_scope=tuple(str(scope) for scope in task.get("file_scope", [])),
                        manual_approval_required=bool(task.get("manual_approval_required", False)),
                        agent_approval_required=bool(task.get("agent_approval_required", bool(implementation or repair))),
                        max_task_iterations=max(1, int(task.get("max_task_iterations", 1))),
                        implementation=tuple(implementation),
                        repair=tuple(repair),
                        acceptance=tuple(acceptance),
                        e2e=tuple(e2e),
                    )
                )
        return tasks

    def _parse_task_commands(self, items: list[dict[str, Any]] | None, *, default_name: str) -> list[AcceptanceCommand]:
        commands = []
        for item in items or []:
            executor = str(item.get("executor", "shell"))
            command = item.get("command")
            prompt = item.get("prompt")
            name = item.get("name") or command or prompt or default_name
            commands.append(
                AcceptanceCommand(
                    name=str(name),
                    command=str(command) if command is not None else None,
                    timeout_seconds=int(item.get("timeout_seconds", self.default_timeout)),
                    required=bool(item.get("required", True)),
                    executor=executor,
                    prompt=str(prompt) if prompt is not None else None,
                    model=str(item["model"]) if item.get("model") else None,
                    sandbox=str(item.get("sandbox", "workspace-write")),
                )
            )
        return commands

    def task_by_id(self, task_id: str) -> HarnessTask:
        for task in self.iter_tasks():
            if task.id == task_id:
                return task
        raise KeyError(f"Unknown task: {task_id}")

    def next_task(self) -> HarnessTask | None:
        state = self.load_state()
        state_tasks = state.get("tasks", {})
        for task in self.iter_tasks():
            task_state = state_tasks.get(task.id, {})
            status = str(task_state.get("status", task.status))
            attempts = int(task_state.get("attempts", 0))
            if status in COMPLETED_STATUSES or status in BLOCKED_STATUSES:
                continue
            if attempts >= task.max_attempts:
                continue
            return task
        return None

    def status_summary(self) -> dict[str, Any]:
        state = self.load_state()
        state_tasks = state.get("tasks", {})
        milestones: dict[str, dict[str, Any]] = {}
        for task in self.iter_tasks():
            task_state = state_tasks.get(task.id, {})
            status = str(task_state.get("status", task.status))
            milestone = milestones.setdefault(
                task.milestone_id,
                {
                    "id": task.milestone_id,
                    "title": task.milestone_title,
                    "total": 0,
                    "done": 0,
                    "blocked": 0,
                    "failed": 0,
                    "pending": 0,
                },
            )
            milestone["total"] += 1
            if status in COMPLETED_STATUSES:
                milestone["done"] += 1
            elif status in BLOCKED_STATUSES:
                milestone["blocked"] += 1
            elif status == "failed":
                milestone["failed"] += 1
            else:
                milestone["pending"] += 1
        return {
            "project": self.roadmap.get("project", self.project_root.name),
            "profile": self.roadmap.get("profile"),
            "root": str(self.project_root),
            "roadmap": str(self.roadmap_path),
            "state": str(self.state_path),
            "milestones": list(milestones.values()),
            "next_task": self.task_payload(self.next_task()),
            "experience": self.frontend_experience_plan(),
            "continuation": self.continuation_summary(),
            "self_iteration": self.self_iteration_summary(),
            "manifest_index": self.manifest_index_summary(),
        }

    def frontend_experience_plan(self) -> dict[str, Any]:
        experience = self.roadmap.get("experience")
        if isinstance(experience, dict):
            plan = deepcopy(experience)
            plan["source"] = "explicit"
            plan["derived"] = False
            plan["recommendation"] = str(plan.get("kind", "")).strip() or None
            plan["rationale"] = ["roadmap declares an explicit experience block"]
            return plan
        if experience is not None:
            return {
                "source": "explicit-invalid",
                "derived": False,
                "recommendation": None,
                "kind": None,
                "rationale": ["roadmap declares an experience block, but it is not a mapping"],
            }

        kind, rationale = self._derive_default_experience_kind()
        plan = deepcopy(DEFAULT_EXPERIENCE_PLANS[kind])
        plan["source"] = "derived"
        plan["derived"] = True
        plan["recommendation"] = kind
        plan["rationale"] = rationale
        return plan

    def _derive_default_experience_kind(self) -> tuple[str, list[str]]:
        profile = str(self.roadmap.get("profile", "") or "").strip().lower()
        project_kind = self._roadmap_project_kind()
        hint_text = self._roadmap_hint_text(profile=profile, project_kind=project_kind)

        for kind in ("submission-review", "multi-role-app", "api-only", "cli-only", "dashboard"):
            aliases = EXPERIENCE_KIND_ALIASES[kind]
            alias_matches = self._keyword_matches(hint_text, aliases)
            if alias_matches:
                return kind, self._experience_rationale(
                    profile=profile,
                    project_kind=project_kind,
                    matched=alias_matches,
                    decision=f"matched {kind} roadmap hint",
                )

        matches = {
            kind: self._keyword_matches(hint_text, keywords)
            for kind, keywords in EXPERIENCE_KEYWORDS.items()
        }
        scores = {kind: len(kind_matches) for kind, kind_matches in matches.items()}
        if profile in {"python-agent", "agent-monorepo"}:
            scores["dashboard"] += 1
        if profile in {"trading-research", "evm-security-research", "lean-formalization"}:
            scores["dashboard"] += 2
        if project_kind in {"python", "agent", "evm"}:
            scores["dashboard"] += 1

        thresholds = {
            "submission-review": 2,
            "multi-role-app": 2,
            "api-only": 2,
            "cli-only": 2,
            "dashboard": 1,
        }
        priority = ["submission-review", "multi-role-app", "api-only", "cli-only", "dashboard"]
        candidates = [kind for kind in priority if scores[kind] >= thresholds[kind]]
        if candidates:
            chosen = max(candidates, key=lambda kind: (scores[kind], -priority.index(kind)))
            return chosen, self._experience_rationale(
                profile=profile,
                project_kind=project_kind,
                matched=matches[chosen],
                decision=f"matched {chosen} roadmap signals",
            )

        return "dashboard", self._experience_rationale(
            profile=profile,
            project_kind=project_kind,
            matched=[],
            decision="defaulted to the operator dashboard plan",
        )

    def _experience_rationale(
        self,
        *,
        profile: str,
        project_kind: str,
        matched: list[str],
        decision: str,
    ) -> list[str]:
        rationale = [decision]
        if profile:
            rationale.append(f"profile: {profile}")
        if project_kind:
            rationale.append(f"project kind: {project_kind}")
        if matched:
            rationale.append("matched hints: " + ", ".join(matched[:6]))
        return rationale

    def _roadmap_project_kind(self) -> str:
        for key in ("project_kind", "kind", "category"):
            value = self.roadmap.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip().lower()
        _, project_kind = guess_profile(self.project_root)
        return project_kind

    def _roadmap_hint_text(self, *, profile: str, project_kind: str) -> str:
        values: list[str] = [profile, project_kind]

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                for child in value.values():
                    visit(child)
                return
            if isinstance(value, list):
                for child in value:
                    visit(child)
                return
            if isinstance(value, str):
                text = value.strip()
                if text:
                    values.append(text)

        visit(self.roadmap)
        return " ".join(values).lower()

    def _keyword_matches(self, text: str, keywords: tuple[str, ...]) -> list[str]:
        matches: list[str] = []
        for keyword in keywords:
            expression = re.escape(keyword.lower()).replace(r"\ ", r"\s+")
            pattern = rf"(?<![a-z0-9]){expression}(?![a-z0-9])"
            if re.search(pattern, text):
                matches.append(keyword)
        return matches

    def frontend_task_plan(
        self,
        *,
        milestone_id: str = FRONTEND_TASK_MILESTONE_ID,
    ) -> dict[str, Any]:
        experience = self.frontend_experience_plan()
        errors: list[str] = []
        self._validate_experience_payload(experience, errors=errors)
        if errors:
            return {
                "status": "error",
                "message": "frontend experience plan is invalid",
                "errors": errors,
                "materialized": False,
                "experience": experience,
                "milestone": None,
                "tasks": [],
            }

        existing_task_ids = {task.id for task in self.iter_tasks()}
        milestone = self._build_frontend_milestone(
            experience,
            milestone_id=milestone_id,
            existing_task_ids=existing_task_ids,
        )
        tasks = milestone.get("tasks", []) if isinstance(milestone.get("tasks"), list) else []
        return {
            "status": "proposed",
            "message": f"proposed {len(tasks)} frontend task(s) from {experience.get('source')} experience plan",
            "materialized": False,
            "project": str(self.roadmap.get("project", self.project_root.name)),
            "roadmap": str(self.roadmap_path),
            "experience": experience,
            "milestone": milestone,
            "tasks": tasks,
            "tasks_added": 0,
        }

    def materialize_frontend_tasks(
        self,
        *,
        milestone_id: str = FRONTEND_TASK_MILESTONE_ID,
        reason: str = "manual_frontend_task_generation",
    ) -> dict[str, Any]:
        milestones = self.roadmap.get("milestones")
        if milestones is not None and not isinstance(milestones, list):
            return {
                "status": "error",
                "message": "`milestones` must be a list before frontend tasks can be materialized",
                "materialized": False,
                "experience": self.frontend_experience_plan(),
                "milestone": None,
                "tasks": [],
            }

        existing_milestone = None
        for milestone in milestones or []:
            if isinstance(milestone, dict) and str(milestone.get("id", "")) == milestone_id:
                existing_milestone = milestone
                break
        if existing_milestone is not None:
            tasks = existing_milestone.get("tasks", []) if isinstance(existing_milestone.get("tasks"), list) else []
            return {
                "status": "skipped",
                "message": f"milestone `{milestone_id}` already exists",
                "materialized": False,
                "project": str(self.roadmap.get("project", self.project_root.name)),
                "roadmap": str(self.roadmap_path),
                "experience": self.frontend_experience_plan(),
                "milestone": existing_milestone,
                "tasks": tasks,
                "tasks_added": 0,
            }

        proposal = self.frontend_task_plan(milestone_id=milestone_id)
        if proposal["status"] != "proposed":
            return proposal

        if milestones is None:
            milestones = []
            self.roadmap["milestones"] = milestones
        milestone = proposal["milestone"]
        milestones.append(milestone)
        self.save_roadmap()
        tasks = milestone.get("tasks", []) if isinstance(milestone, dict) else []
        event = {
            "at": utc_now(),
            "event": "frontend_task_generation",
            "reason": reason,
            "milestone_id": milestone_id,
            "tasks_added": len(tasks),
            "experience_kind": proposal["experience"].get("kind"),
            "experience_source": proposal["experience"].get("source"),
        }
        append_jsonl(self.decision_log_path, event)
        return {
            **proposal,
            "status": "materialized",
            "message": f"materialized {len(tasks)} frontend task(s)",
            "materialized": True,
            "tasks_added": len(tasks),
        }

    def _build_frontend_milestone(
        self,
        experience: dict[str, Any],
        *,
        milestone_id: str,
        existing_task_ids: set[str],
    ) -> dict[str, Any]:
        kind = str(experience.get("kind", "dashboard"))
        label = FRONTEND_KIND_LABELS.get(kind, kind.replace("-", " "))
        generated_at = utc_now()
        task_ids = set(existing_task_ids)
        tasks = [
            self._frontend_contract_task(
                experience,
                kind=kind,
                label=label,
                task_ids=task_ids,
                generated_at=generated_at,
            )
        ]
        for journey in experience.get("e2e_journeys", []):
            if isinstance(journey, dict):
                tasks.append(
                    self._frontend_journey_task(
                        experience,
                        journey,
                        kind=kind,
                        label=label,
                        task_ids=task_ids,
                        generated_at=generated_at,
                    )
                )
        return {
            "id": milestone_id,
            "title": "Frontend Visualization",
            "status": "planned",
            "objective": f"Create stack-neutral {label} tasks and E2E journey gates from the roadmap experience plan.",
            "generated_by": FRONTEND_TASK_GENERATOR,
            "generated_at": generated_at,
            "experience_kind": kind,
            "experience_source": experience.get("source"),
            "tasks": tasks,
        }

    def _frontend_contract_task(
        self,
        experience: dict[str, Any],
        *,
        kind: str,
        label: str,
        task_ids: set[str],
        generated_at: str,
    ) -> dict[str, Any]:
        task_id = self._unique_frontend_task_id(f"frontend-{kind}-experience-contract", task_ids)
        personas = self._string_items(experience.get("personas"))
        surfaces = self._string_items(experience.get("primary_surfaces"))
        journeys = [
            journey
            for journey in experience.get("e2e_journeys", [])
            if isinstance(journey, dict)
        ]
        required_terms = [kind, *personas, *surfaces, *[str(journey.get("id", "")) for journey in journeys]]
        return {
            "id": task_id,
            "title": f"Define {label} experience contract",
            "status": "pending",
            "max_attempts": 2,
            "max_task_iterations": 2,
            "manual_approval_required": False,
            "agent_approval_required": True,
            "file_scope": ["docs/**", "tests/**", "templates/**"],
            "implementation": [
                {
                    "name": "Draft stack-neutral experience contract",
                    "executor": "codex",
                    "prompt": self._frontend_contract_prompt(experience, label=label),
                    "timeout_seconds": 3600,
                    "sandbox": "workspace-write",
                }
            ],
            "acceptance": [
                {
                    "name": f"{label} experience contract is documented",
                    "command": self._content_check_command(
                        "docs/frontend-experience.md",
                        required_terms,
                        missing_label="missing frontend experience terms",
                    ),
                    "timeout_seconds": 60,
                }
            ],
            "e2e": [
                {
                    "name": f"{journey.get('id')} journey is represented in the experience contract",
                    "command": self._content_check_command(
                        "docs/frontend-experience.md",
                        [str(journey.get("id", "")), str(journey.get("persona", ""))],
                        missing_label="missing frontend journey terms",
                    ),
                    "timeout_seconds": 60,
                }
                for journey in journeys
            ],
            "frontend": self._frontend_task_metadata(experience, task_kind="experience-contract"),
            "generated_by": FRONTEND_TASK_GENERATOR,
            "generated_at": generated_at,
        }

    def _frontend_journey_task(
        self,
        experience: dict[str, Any],
        journey: dict[str, Any],
        *,
        kind: str,
        label: str,
        task_ids: set[str],
        generated_at: str,
    ) -> dict[str, Any]:
        guidance = FRONTEND_KIND_TASK_GUIDANCE[kind]
        journey_id = str(journey.get("id", "")).strip()
        journey_slug = self._slugify(journey_id or "journey")
        task_id = self._unique_frontend_task_id(f"frontend-{kind}-{journey_slug}", task_ids)
        persona = str(journey.get("persona", "")).strip()
        auth = experience.get("auth") if isinstance(experience.get("auth"), dict) else {}
        roles = self._string_items(auth.get("roles") if isinstance(auth, dict) else [])
        surfaces = self._string_items(experience.get("primary_surfaces"))
        acceptance_terms = [
            kind,
            journey_id,
            persona,
            *roles,
            *surfaces[:4],
            *self._string_items(guidance.get("acceptance_terms")),
        ]
        candidates = [str(pattern).format(slug=journey_slug) for pattern in guidance["journey_candidates"]]
        return {
            "id": task_id,
            "title": f"Add {label} journey check for {journey_id}",
            "status": "pending",
            "max_attempts": 2,
            "max_task_iterations": 2,
            "manual_approval_required": False,
            "agent_approval_required": True,
            "file_scope": list(guidance["file_scope"]),
            "implementation": [
                {
                    "name": f"Implement {journey_id} experience check",
                    "executor": "codex",
                    "prompt": self._frontend_journey_prompt(
                        experience,
                        journey,
                        label=label,
                        guidance=str(guidance["implementation_focus"]),
                        candidates=candidates,
                    ),
                    "timeout_seconds": 3600,
                    "sandbox": "workspace-write",
                }
            ],
            "acceptance": [
                {
                    "name": f"{label} acceptance criteria cover {journey_id}",
                    "command": self._content_check_command(
                        "docs/frontend-experience.md",
                        acceptance_terms,
                        missing_label="missing frontend acceptance terms",
                    ),
                    "timeout_seconds": 60,
                }
            ],
            "e2e": [
                {
                    "name": f"{journey_id} e2e journey check exists",
                    "command": self._candidate_content_check_command(
                        candidates,
                        [journey_id, persona],
                        missing_label="missing e2e journey check",
                    ),
                    "timeout_seconds": 120,
                }
            ],
            "frontend": self._frontend_task_metadata(
                experience,
                task_kind="journey-check",
                journey=journey,
                candidate_paths=candidates,
            ),
            "generated_by": FRONTEND_TASK_GENERATOR,
            "generated_at": generated_at,
        }

    def _frontend_contract_prompt(self, experience: dict[str, Any], *, label: str) -> str:
        return (
            "Create or update `docs/frontend-experience.md` as a stack-neutral experience contract.\n"
            f"Experience kind: {experience.get('kind')} ({label}).\n"
            f"Personas: {', '.join(self._string_items(experience.get('personas')))}.\n"
            f"Primary surfaces: {', '.join(self._string_items(experience.get('primary_surfaces')))}.\n"
            f"Auth: {json.dumps(experience.get('auth', {}), sort_keys=True)}.\n"
            f"E2E journeys: {json.dumps(experience.get('e2e_journeys', []), sort_keys=True)}.\n"
            "Document expected screens or non-browser surfaces, loading/empty/error states, role or auth boundaries, "
            "and the files or commands that will exercise each journey. Use the existing project conventions; "
            "do not introduce a frontend framework solely to satisfy this task."
        )

    def _frontend_journey_prompt(
        self,
        experience: dict[str, Any],
        journey: dict[str, Any],
        *,
        label: str,
        guidance: str,
        candidates: list[str],
    ) -> str:
        return (
            f"Implement or document the `{journey.get('id')}` {label} journey check using the project's existing stack.\n"
            f"Persona: {journey.get('persona')}.\n"
            f"Goal: {journey.get('goal')}.\n"
            f"Experience kind: {experience.get('kind')}.\n"
            f"Guidance: {guidance}\n"
            "Keep the work local and deterministic. Browser projects may use their existing browser test framework; "
            "API-only and CLI-only projects may use documented examples, API tests, CLI tests, or shell/Python checks.\n"
            f"Place journey evidence or executable checks in one of: {', '.join(candidates)}.\n"
            "Update `docs/frontend-experience.md` with the acceptance criteria and the selected E2E command. "
            "Do not require private services, live deployments, paid accounts, or a specific frontend framework."
        )

    def _frontend_task_metadata(
        self,
        experience: dict[str, Any],
        *,
        task_kind: str,
        journey: dict[str, Any] | None = None,
        candidate_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "task_kind": task_kind,
            "experience_kind": experience.get("kind"),
            "experience_source": experience.get("source"),
            "personas": self._string_items(experience.get("personas")),
            "primary_surfaces": self._string_items(experience.get("primary_surfaces")),
            "auth": experience.get("auth") if isinstance(experience.get("auth"), dict) else {},
            "stack_policy": "use existing project conventions; no required frontend framework",
        }
        if journey is not None:
            payload["e2e_journey"] = {
                "id": str(journey.get("id", "")),
                "persona": str(journey.get("persona", "")),
                "goal": str(journey.get("goal", "")),
            }
        if candidate_paths is not None:
            payload["candidate_check_paths"] = candidate_paths
        return payload

    def _unique_frontend_task_id(self, base: str, task_ids: set[str]) -> str:
        base = self._slugify(base)
        candidate = base
        counter = 2
        while candidate in task_ids:
            candidate = f"{base}-{counter}"
            counter += 1
        task_ids.add(candidate)
        return candidate

    def _slugify(self, value: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
        return slug or "item"

    def _string_items(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if isinstance(item, str) and str(item).strip()]

    def _content_check_command(
        self,
        path: str,
        required_terms: list[str],
        *,
        missing_label: str,
    ) -> str:
        terms = [term for term in dict.fromkeys(required_terms) if str(term).strip()]
        encoded_terms = self._b64_json(terms)
        return (
            "python3 -c \"import base64,json; from pathlib import Path; "
            f"p=Path('{path}'); assert p.exists(), 'missing {path}'; "
            "text=p.read_text(encoding='utf-8', errors='ignore').lower(); "
            f"terms=json.loads(base64.b64decode('{encoded_terms}')); "
            "missing=[term for term in terms if str(term).lower() not in text]; "
            f"assert not missing, '{missing_label}: ' + ', '.join(missing)\""
        )

    def _candidate_content_check_command(
        self,
        candidates: list[str],
        required_terms: list[str],
        *,
        missing_label: str,
    ) -> str:
        encoded_candidates = self._b64_json(candidates)
        encoded_terms = self._b64_json([term for term in dict.fromkeys(required_terms) if str(term).strip()])
        return (
            "python3 -c \"import base64,json; from pathlib import Path; "
            f"candidates=json.loads(base64.b64decode('{encoded_candidates}')); "
            "paths=[Path(item) for item in candidates if Path(item).exists()]; "
            f"assert paths, '{missing_label}; expected one of: ' + ', '.join(candidates); "
            "text='\\n'.join(path.read_text(encoding='utf-8', errors='ignore').lower() for path in paths); "
            f"terms=json.loads(base64.b64decode('{encoded_terms}')); "
            "missing=[term for term in terms if str(term).lower() not in text]; "
            f"assert not missing, '{missing_label} terms: ' + ', '.join(missing)\""
        )

    def _b64_json(self, value: Any) -> str:
        return base64.b64encode(json.dumps(value).encode("utf-8")).decode("ascii")

    def manifest_index(self) -> dict[str, Any]:
        if self.manifest_index_path.exists():
            return load_mapping(self.manifest_index_path)
        return self._build_manifest_index()

    def manifest_index_summary(self) -> dict[str, Any]:
        index = self.manifest_index()
        return {
            "path": index["manifest_index_path"],
            "manifest_count": index["manifest_count"],
            "latest_manifest": index["latest_manifest"],
            "status_counts": index["status_counts"],
        }

    def rebuild_manifest_index(self) -> dict[str, Any]:
        index = self._build_manifest_index()
        write_json(self.manifest_index_path, index)
        return index

    def _build_manifest_index(self) -> dict[str, Any]:
        manifests = []
        for manifest_path in sorted(
            self.report_dir.rglob("*.json"),
            key=lambda path: self._project_relative_path(path),
        ):
            if manifest_path.resolve() == self.manifest_index_path.resolve():
                continue
            payload = load_mapping(manifest_path)
            if payload.get("kind") != "engineering-harness.task-run-manifest":
                continue
            manifests.append(self._manifest_index_entry(manifest_path, payload))

        manifests.sort(
            key=lambda item: (
                str(item.get("started_at") or ""),
                str(item.get("finished_at") or ""),
                str(item.get("task_id") or ""),
                int(item.get("attempt") or 0),
                str(item.get("manifest_path") or ""),
            )
        )
        status_counts: dict[str, int] = {}
        latest_by_task: dict[str, str] = {}
        for item in manifests:
            status = str(item.get("status") or "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1
            latest_by_task[str(item["task_id"])] = str(item["manifest_path"])

        latest_manifest = manifests[-1]["manifest_path"] if manifests else None
        latest_finished_at = max((str(item.get("finished_at") or "") for item in manifests), default="") or None
        return {
            "schema_version": 1,
            "kind": "engineering-harness.task-run-manifest-index",
            "project": str(self.roadmap.get("project", self.project_root.name)),
            "project_root": str(self.project_root),
            "roadmap_path": self._project_relative_path(self.roadmap_path) if self.roadmap_path else None,
            "report_dir": self._project_relative_path(self.report_dir),
            "manifest_index_path": self._project_relative_path(self.manifest_index_path),
            "updated_at": latest_finished_at,
            "manifest_count": len(manifests),
            "status_counts": dict(sorted(status_counts.items())),
            "latest_manifest": latest_manifest,
            "latest_by_task": dict(sorted(latest_by_task.items())),
            "manifests": manifests,
        }

    def _new_task_report_path(self, task: HarnessTask) -> Path:
        base = self.report_dir / f"{slug_now()}-{task.id}.md"
        candidate = base
        counter = 2
        while candidate.exists() or candidate.with_suffix(".json").exists():
            candidate = base.with_name(f"{base.stem}_{counter}{base.suffix}")
            counter += 1
        return candidate

    def _manifest_index_entry(self, manifest_path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
        task = manifest.get("task") if isinstance(manifest.get("task"), dict) else {}
        milestone = manifest.get("milestone") if isinstance(manifest.get("milestone"), dict) else {}
        runs = manifest.get("runs") if isinstance(manifest.get("runs"), list) else []
        git = manifest.get("git") if isinstance(manifest.get("git"), dict) else {}
        return {
            "manifest_path": str(manifest.get("manifest_path") or self._project_relative_path(manifest_path)),
            "report_path": str(manifest.get("report_path") or ""),
            "task_id": str(manifest.get("task_id") or task.get("id") or ""),
            "task_title": str(task.get("title") or ""),
            "milestone_id": str(manifest.get("milestone_id") or milestone.get("id") or ""),
            "milestone_title": str(milestone.get("title") or ""),
            "status": str(manifest.get("status") or "unknown"),
            "message": str(manifest.get("message") or ""),
            "started_at": manifest.get("started_at"),
            "finished_at": manifest.get("finished_at"),
            "dry_run": bool(manifest.get("dry_run", False)),
            "attempt": manifest.get("attempt"),
            "run_count": len(runs),
            "runs": [
                {
                    "phase": str(run.get("phase") or ""),
                    "name": str(run.get("name") or ""),
                    "executor": str(run.get("executor") or ""),
                    "status": str(run.get("status") or "unknown"),
                    "returncode": run.get("returncode"),
                    "executor_metadata": run.get("executor_metadata") if isinstance(run.get("executor_metadata"), dict) else {},
                    "executor_result": run.get("executor_result") if isinstance(run.get("executor_result"), dict) else {},
                }
                for run in runs
                if isinstance(run, dict)
            ],
            "git": {
                "is_repository": bool(git.get("is_repository", False)),
                "head": git.get("head"),
            },
        }

    def _project_relative_path(self, path: Path) -> str:
        if path.is_relative_to(self.project_root):
            return str(path.relative_to(self.project_root))
        return str(path)

    def validate_roadmap(self) -> dict[str, Any]:
        errors: list[str] = []
        warnings: list[str] = []

        if int(self.roadmap.get("version", 0) or 0) <= 0:
            warnings.append("top-level `version` is missing or not positive")
        if not str(self.roadmap.get("project", "")).strip():
            errors.append("top-level `project` is required")
        if not str(self.roadmap.get("profile", "")).strip():
            warnings.append("top-level `profile` is missing")

        self._validate_experience_payload(self.roadmap.get("experience"), errors=errors)

        milestones = self.roadmap.get("milestones", [])
        if not isinstance(milestones, list):
            errors.append("`milestones` must be a list")
            milestones = []

        seen_task_ids: set[str] = set()
        materialized_stage_ids: set[str] = set()
        for milestone_index, milestone in enumerate(milestones):
            if not isinstance(milestone, dict):
                errors.append(f"milestones[{milestone_index}] must be a mapping")
                continue
            milestone_id = str(milestone.get("id", "")).strip()
            if not milestone_id:
                errors.append(f"milestones[{milestone_index}].id is required")
                milestone_id = f"milestone-{milestone_index}"
            materialized_stage_ids.add(milestone_id)
            tasks = milestone.get("tasks", [])
            if not isinstance(tasks, list):
                errors.append(f"milestone `{milestone_id}` tasks must be a list")
                continue
            for task_index, task in enumerate(tasks):
                self._validate_task_payload(
                    task,
                    location=f"milestone `{milestone_id}` task[{task_index}]",
                    seen_task_ids=seen_task_ids,
                    errors=errors,
                    warnings=warnings,
                )

        continuation = self.roadmap.get("continuation")
        if continuation is not None:
            if not isinstance(continuation, dict):
                errors.append("`continuation` must be a mapping")
            else:
                stages = continuation.get("stages", [])
                if stages is None:
                    stages = []
                if not isinstance(stages, list):
                    errors.append("`continuation.stages` must be a list")
                else:
                    seen_stage_ids: set[str] = set()
                    for stage_index, stage in enumerate(stages):
                        if not isinstance(stage, dict):
                            errors.append(f"continuation.stages[{stage_index}] must be a mapping")
                            continue
                        stage_id = str(stage.get("id", "")).strip()
                        if not stage_id:
                            errors.append(f"continuation.stages[{stage_index}].id is required")
                            stage_id = f"stage-{stage_index}"
                        elif stage_id in seen_stage_ids:
                            errors.append(f"duplicate continuation stage id: {stage_id}")
                        seen_stage_ids.add(stage_id)
                        tasks = stage.get("tasks", [])
                        if not isinstance(tasks, list) or not tasks:
                            errors.append(f"continuation stage `{stage_id}` must define at least one task")
                            continue
                        task_ids_for_stage = set() if stage_id in materialized_stage_ids else seen_task_ids
                        for task_index, task in enumerate(tasks):
                            self._validate_task_payload(
                                task,
                                location=f"continuation stage `{stage_id}` task[{task_index}]",
                                seen_task_ids=task_ids_for_stage,
                                errors=errors,
                                warnings=warnings,
                            )

        status = "passed" if not errors else "failed"
        return {
            "status": status,
            "error_count": len(errors),
            "warning_count": len(warnings),
            "errors": errors,
            "warnings": warnings,
        }

    def _validate_experience_payload(
        self,
        experience: Any,
        *,
        errors: list[str],
    ) -> None:
        if experience is None:
            return
        if not isinstance(experience, dict):
            errors.append("top-level `experience` must be a mapping")
            return

        kind = str(experience.get("kind", "")).strip()
        if not kind:
            errors.append("experience.kind is required")
        elif kind not in EXPERIENCE_KINDS:
            allowed = ", ".join(sorted(EXPERIENCE_KINDS))
            errors.append(f"experience.kind `{kind}` is not supported; expected one of: {allowed}")

        personas = self._validate_string_list(
            experience.get("personas"),
            location="experience.personas",
            errors=errors,
            required=True,
            non_empty=True,
        )
        persona_names = set(personas)

        self._validate_string_list(
            experience.get("primary_surfaces"),
            location="experience.primary_surfaces",
            errors=errors,
            required=True,
            non_empty=True,
        )

        auth = experience.get("auth")
        if auth is None:
            errors.append("experience.auth is required")
        elif not isinstance(auth, dict):
            errors.append("experience.auth must be a mapping")
        else:
            auth_required = auth.get("required", False)
            if not isinstance(auth_required, bool):
                errors.append("experience.auth.required must be true or false")
            roles = self._validate_string_list(
                auth.get("roles"),
                location="experience.auth.roles",
                errors=errors,
                required=True,
                non_empty=False,
            )
            if auth_required is True and not roles:
                errors.append("experience.auth.roles must include at least one role when auth.required is true")

        journeys = experience.get("e2e_journeys")
        if journeys is None:
            errors.append("experience.e2e_journeys is required")
            return
        if not isinstance(journeys, list):
            errors.append("experience.e2e_journeys must be a list")
            return
        if not journeys:
            errors.append("experience.e2e_journeys must define at least one journey")
            return

        seen_journey_ids: set[str] = set()
        for journey_index, journey in enumerate(journeys):
            location = f"experience.e2e_journeys[{journey_index}]"
            if not isinstance(journey, dict):
                errors.append(f"{location} must be a mapping")
                continue
            journey_id = str(journey.get("id", "")).strip()
            if not journey_id:
                errors.append(f"{location}.id is required")
            elif journey_id in seen_journey_ids:
                errors.append(f"duplicate experience e2e journey id: {journey_id}")
            seen_journey_ids.add(journey_id)

            persona = str(journey.get("persona", "")).strip()
            if not persona:
                errors.append(f"{location}.persona is required")
            elif persona_names and persona not in persona_names:
                errors.append(f"{location}.persona `{persona}` must match one of experience.personas")

            if not str(journey.get("goal", "")).strip():
                errors.append(f"{location}.goal is required")

    def _validate_string_list(
        self,
        value: Any,
        *,
        location: str,
        errors: list[str],
        required: bool,
        non_empty: bool,
    ) -> list[str]:
        if value is None:
            if required:
                errors.append(f"{location} is required")
            return []
        if not isinstance(value, list):
            errors.append(f"{location} must be a list")
            return []
        if non_empty and not value:
            errors.append(f"{location} must include at least one item")
        items: list[str] = []
        seen_items: set[str] = set()
        for item_index, item in enumerate(value):
            text = str(item).strip() if isinstance(item, str) else ""
            if not text:
                errors.append(f"{location}[{item_index}] must be a non-empty string")
                continue
            if text in seen_items:
                errors.append(f"{location} contains duplicate item `{text}`")
            seen_items.add(text)
            items.append(text)
        return items

    def _validate_task_payload(
        self,
        task: Any,
        *,
        location: str,
        seen_task_ids: set[str],
        errors: list[str],
        warnings: list[str],
    ) -> None:
        if not isinstance(task, dict):
            errors.append(f"{location} must be a mapping")
            return
        task_id = str(task.get("id", "")).strip()
        if not task_id:
            errors.append(f"{location}.id is required")
            task_id = location
        elif task_id in seen_task_ids:
            errors.append(f"duplicate task id: {task_id}")
        seen_task_ids.add(task_id)
        if not str(task.get("title", task_id)).strip():
            warnings.append(f"task `{task_id}` title is empty")
        file_scope = task.get("file_scope", [])
        if file_scope is not None and not isinstance(file_scope, list):
            errors.append(f"task `{task_id}` file_scope must be a list")
        for group_name in ("implementation", "repair", "acceptance", "e2e"):
            group = task.get(group_name, [])
            if group_name == "acceptance" and not group:
                errors.append(f"task `{task_id}` must define at least one acceptance command")
            if group is None:
                group = []
            if not isinstance(group, list):
                errors.append(f"task `{task_id}` {group_name} must be a list")
                continue
            for command_index, item in enumerate(group):
                self._validate_command_payload(
                    item,
                    location=f"task `{task_id}` {group_name}[{command_index}]",
                    errors=errors,
                    warnings=warnings,
                )
        try:
            if int(task.get("max_task_iterations", 1)) < 1:
                errors.append(f"task `{task_id}` max_task_iterations must be positive")
        except (TypeError, ValueError):
            errors.append(f"task `{task_id}` max_task_iterations must be an integer")

    def _validate_command_payload(
        self,
        item: Any,
        *,
        location: str,
        errors: list[str],
        warnings: list[str],
    ) -> None:
        if not isinstance(item, dict):
            errors.append(f"{location} must be a mapping")
            return
        executor = str(item.get("executor", "shell"))
        executor_adapter = self.executor_registry.get(executor)
        if executor_adapter is None:
            errors.append(f"{location} has unknown executor `{executor}`")
            return
        executor_metadata = executor_adapter.metadata
        if executor_metadata.input_mode == "command":
            command = item.get("command")
            if not str(command or "").strip():
                errors.append(f"{location} {executor} command is required")
            elif executor_metadata.uses_command_policy:
                allowed, reason = self.command_allowed(str(command))
                if not allowed:
                    warnings.append(f"{location} command is not currently allowlisted: {reason}")
        if executor_metadata.input_mode == "prompt" and not str(item.get("prompt", "") or item.get("command", "")).strip():
            errors.append(f"{location} {executor} prompt is required")
        try:
            if int(item.get("timeout_seconds", self.default_timeout)) <= 0:
                errors.append(f"{location} timeout_seconds must be positive")
        except (TypeError, ValueError):
            errors.append(f"{location} timeout_seconds must be an integer")

    def task_payload(self, task: HarnessTask | None) -> dict[str, Any] | None:
        if task is None:
            return None
        def command_payload(command: AcceptanceCommand) -> dict[str, Any]:
            payload = {
                "name": command.name,
                "timeout_seconds": command.timeout_seconds,
                "required": command.required,
                "executor": command.executor,
            }
            if command.command is not None:
                payload["command"] = command.command
            if command.prompt is not None:
                payload["prompt"] = command.prompt
            if command.model is not None:
                payload["model"] = command.model
            return payload

        return {
            "id": task.id,
            "title": task.title,
            "milestone_id": task.milestone_id,
            "milestone_title": task.milestone_title,
            "file_scope": list(task.file_scope),
            "manual_approval_required": task.manual_approval_required,
            "agent_approval_required": task.agent_approval_required,
            "max_task_iterations": task.max_task_iterations,
            "implementation": [command_payload(command) for command in task.implementation],
            "repair": [command_payload(command) for command in task.repair],
            "acceptance": [command_payload(command) for command in task.acceptance],
            "e2e": [command_payload(command) for command in task.e2e],
        }

    def command_allowed(self, command: str | None, allow_live: bool = False) -> tuple[bool, str]:
        if command is None:
            return False, "shell command is missing"
        stripped = command.strip()
        for pattern in self.command_policy.get("blocked_patterns", []):
            if pattern in stripped:
                return False, f"blocked pattern matched: {pattern}"
        if not allow_live:
            for pattern in self.command_policy.get("requires_live_flag_patterns", []):
                if pattern in stripped:
                    return False, f"live command requires --allow-live: {pattern}"
        prefixes = tuple(str(prefix) for prefix in self.command_policy.get("allowed_prefixes", []))
        if prefixes and not stripped.startswith(prefixes):
            return False, "command prefix is not allowlisted"
        return True, "allowed"

    def _git_status_paths(self) -> list[str]:
        if not self._is_git_repo():
            return []
        result = self._git(["status", "--porcelain"])
        if result["returncode"] != 0:
            return []
        paths: list[str] = []
        for line in result["stdout"].splitlines():
            if len(line) < 4:
                continue
            path = line[3:].strip()
            if " -> " in path:
                _, _, path = path.partition(" -> ")
            normalized = self._normalize_repo_path(path)
            if normalized:
                paths.append(normalized)
        return sorted(dict.fromkeys(paths))

    def _normalize_repo_path(self, path: str) -> str:
        normalized = path.replace("\\", "/").strip()
        while normalized.startswith("./"):
            normalized = normalized[2:]
        return normalized

    def _path_in_scope(self, path: str, scopes: tuple[str, ...]) -> bool:
        normalized = self._normalize_repo_path(path)
        normalized_scopes = tuple(self._normalize_repo_path(scope) for scope in scopes if str(scope).strip())
        if not normalized_scopes or any(scope in {"**", "**/*"} for scope in normalized_scopes):
            return True
        for scope in normalized_scopes:
            if scope.endswith("/**"):
                prefix = scope[:-3].rstrip("/")
                if normalized == prefix or normalized.startswith(f"{prefix}/"):
                    return True
            if fnmatch.fnmatchcase(normalized, scope):
                return True
            try:
                if PurePosixPath(normalized).match(scope):
                    return True
            except ValueError:
                continue
        return False

    def _scope_violations(self, paths: list[str] | set[str], scopes: tuple[str, ...]) -> list[str]:
        return sorted(path for path in paths if not self._path_in_scope(path, scopes))

    def _git_safety_preflight(self, task: HarnessTask) -> dict[str, Any]:
        if not self._is_git_repo():
            return {
                "status": "skipped",
                "message": "project root is not inside a git repository",
                "dirty_before_paths": [],
                "dirty_before_fingerprints": {},
                "dirty_before_out_of_scope_paths": [],
            }
        dirty_before = self._git_status_paths()
        out_of_scope = self._scope_violations(dirty_before, task.file_scope)
        return {
            "status": "dirty" if dirty_before else "clean",
            "message": "worktree has pre-existing changes" if dirty_before else "worktree is clean",
            "dirty_before_paths": dirty_before,
            "dirty_before_fingerprints": self._file_fingerprints(dirty_before),
            "dirty_before_out_of_scope_paths": out_of_scope,
        }

    def _file_fingerprints(self, paths: list[str] | set[str]) -> dict[str, str]:
        fingerprints: dict[str, str] = {}
        for path in paths:
            normalized = self._normalize_repo_path(path)
            file_path = self.project_root / normalized
            if not file_path.exists():
                fingerprints[normalized] = "<missing>"
                continue
            if file_path.is_dir():
                fingerprints[normalized] = "<directory>"
                continue
            try:
                fingerprints[normalized] = hashlib.sha256(file_path.read_bytes()).hexdigest()
            except OSError:
                fingerprints[normalized] = "<unreadable>"
        return fingerprints

    def _file_scope_guard(
        self,
        task: HarnessTask,
        *,
        dirty_before_paths: list[str],
        dirty_before_fingerprints: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if not self._is_git_repo():
            return {
                "status": "skipped",
                "message": "project root is not inside a git repository",
                "dirty_after_paths": [],
                "new_dirty_paths": [],
                "changed_preexisting_dirty_paths": [],
                "new_or_changed_dirty_paths": [],
                "violations": [],
            }
        dirty_before = set(dirty_before_paths)
        dirty_after = set(self._git_status_paths())
        after_fingerprints = self._file_fingerprints(dirty_after)
        new_dirty = sorted(dirty_after - dirty_before)
        before_fingerprints = dirty_before_fingerprints or {}
        changed_preexisting = sorted(
            path
            for path in dirty_after & dirty_before
            if after_fingerprints.get(path) != before_fingerprints.get(path)
        )
        new_or_changed = sorted(dict.fromkeys([*new_dirty, *changed_preexisting]))
        violations = self._scope_violations(new_or_changed, task.file_scope)
        status = "passed" if not violations else "failed"
        message = (
            "new or changed task paths are inside file_scope"
            if not violations
            else f"new or changed task paths outside file_scope: {', '.join(violations[:8])}"
        )
        return {
            "status": status,
            "message": message,
            "dirty_after_paths": sorted(dirty_after),
            "new_dirty_paths": new_dirty,
            "changed_preexisting_dirty_paths": changed_preexisting,
            "new_or_changed_dirty_paths": new_or_changed,
            "violations": violations,
        }

    def run_task(
        self,
        task: HarnessTask,
        *,
        dry_run: bool = False,
        allow_live: bool = False,
        allow_manual: bool = False,
        allow_agent: bool = False,
    ) -> dict[str, Any]:
        started_at = utc_now()
        report_path = self._new_task_report_path(task)
        state = self.load_state()
        task_state = state.setdefault("tasks", {}).setdefault(task.id, {})
        task_state["attempts"] = int(task_state.get("attempts", 0)) + (0 if dry_run else 1)
        task_state["last_started_at"] = started_at
        task_state["last_dry_run"] = dry_run
        safety: dict[str, Any] = {
            "git_preflight": self._git_safety_preflight(task),
            "file_scope_guard": {
                "status": "not_run",
                "message": "task did not reach post-run scope guard",
                "dirty_after_paths": [],
                "new_dirty_paths": [],
                "violations": [],
            },
        }
        task_state["last_dirty_before_paths"] = safety["git_preflight"].get("dirty_before_paths", [])

        if task.manual_approval_required and not allow_manual:
            return self._finish_task(
                state,
                task,
                report_path,
                started_at,
                [],
                "blocked",
                "manual approval required",
                not dry_run,
                safety=safety,
                allow_live=allow_live,
                allow_manual=allow_manual,
                allow_agent=allow_agent,
            )
        if task.agent_approval_required and not allow_agent:
            return self._finish_task(
                state,
                task,
                report_path,
                started_at,
                [],
                "blocked",
                "agent implementation requires --allow-agent",
                not dry_run,
                safety=safety,
                allow_live=allow_live,
                allow_manual=allow_manual,
                allow_agent=allow_agent,
            )
        if not task.acceptance:
            return self._finish_task(
                state,
                task,
                report_path,
                started_at,
                [],
                "blocked",
                "task has no acceptance",
                not dry_run,
                safety=safety,
                allow_live=allow_live,
                allow_manual=allow_manual,
                allow_agent=allow_agent,
            )

        runs: list[CommandRun] = []
        implementation_status, message = self._run_command_group(
            task.implementation,
            phase="implementation",
            runs=runs,
            dry_run=dry_run,
            allow_live=allow_live,
            allow_agent=allow_agent,
            task=task,
        )
        overall_status = implementation_status

        if overall_status == "passed":
            for iteration in range(task.max_task_iterations):
                acceptance_status, message = self._run_command_group(
                    task.acceptance,
                    phase=f"acceptance-{iteration + 1}",
                    runs=runs,
                    dry_run=dry_run,
                    allow_live=allow_live,
                    allow_agent=allow_agent,
                    task=task,
                )
                overall_status = acceptance_status
                if acceptance_status == "passed":
                    message = "All required acceptance commands passed."
                    break
                if acceptance_status == "blocked" or iteration + 1 >= task.max_task_iterations or not task.repair:
                    break
                repair_status, message = self._run_command_group(
                    task.repair,
                    phase=f"repair-{iteration + 1}",
                    runs=runs,
                    dry_run=dry_run,
                    allow_live=allow_live,
                    allow_agent=allow_agent,
                    task=task,
                )
                overall_status = repair_status
                if repair_status != "passed":
                    break

        if overall_status == "passed" and task.e2e:
            e2e_status, message = self._run_command_group(
                task.e2e,
                phase="e2e",
                runs=runs,
                dry_run=dry_run,
                allow_live=allow_live,
                allow_agent=allow_agent,
                task=task,
            )
            overall_status = e2e_status
            if e2e_status == "passed":
                message = "All required acceptance and e2e commands passed."

        if not dry_run:
            safety["file_scope_guard"] = self._file_scope_guard(
                task,
                dirty_before_paths=list(safety["git_preflight"].get("dirty_before_paths", [])),
                dirty_before_fingerprints=dict(safety["git_preflight"].get("dirty_before_fingerprints", {})),
            )
            task_state["last_dirty_after_paths"] = safety["file_scope_guard"].get("dirty_after_paths", [])
            task_state["last_new_dirty_paths"] = safety["file_scope_guard"].get("new_dirty_paths", [])
            task_state["last_file_scope_violations"] = safety["file_scope_guard"].get("violations", [])
            if overall_status == "passed" and safety["file_scope_guard"].get("status") == "failed":
                overall_status = "failed"
                message = safety["file_scope_guard"]["message"]

        status = "dry-run" if dry_run and overall_status == "passed" else overall_status
        return self._finish_task(
            state,
            task,
            report_path,
            started_at,
            runs,
            status,
            message,
            not dry_run,
            safety=safety,
            allow_live=allow_live,
            allow_manual=allow_manual,
            allow_agent=allow_agent,
        )

    def _run_command_group(
        self,
        commands: tuple[AcceptanceCommand, ...],
        *,
        phase: str,
        runs: list[CommandRun],
        dry_run: bool,
        allow_live: bool,
        allow_agent: bool,
        task: HarnessTask,
    ) -> tuple[str, str]:
        if not commands:
            return "passed", f"No {phase} commands configured."
        for command in commands:
            executor = self.executor_registry.get(command.executor)
            if executor is None:
                runs.append(
                    CommandRun(
                        phase,
                        command.name,
                        self._display_command(command, task),
                        "blocked",
                        None,
                        utc_now(),
                        utc_now(),
                        "",
                        f"unknown executor: {command.executor}",
                        executor=command.executor,
                        executor_metadata=self.executor_registry.metadata_for(command.executor),
                    )
                )
                return "blocked", f"unknown executor: {command.executor}"
            if executor.metadata.requires_agent_approval and not allow_agent:
                reason = f"{command.executor} executor requires --allow-agent"
                runs.append(
                    CommandRun(
                        phase,
                        command.name,
                        self._display_command(command, task),
                        "blocked",
                        None,
                        utc_now(),
                        utc_now(),
                        "",
                        reason,
                        executor=command.executor,
                        executor_metadata=executor.metadata.as_contract(),
                    )
                )
                return "blocked", reason
            if executor.metadata.uses_command_policy:
                allowed, reason = self.command_allowed(command.command, allow_live=allow_live)
                if not allowed:
                    runs.append(
                        CommandRun(
                            phase,
                            command.name,
                            self._display_command(command, task),
                            "blocked",
                            None,
                            utc_now(),
                            utc_now(),
                            "",
                            reason,
                            executor=command.executor,
                            executor_metadata=executor.metadata.as_contract(),
                        )
                    )
                    return "blocked", reason
            if dry_run:
                runs.append(
                    CommandRun(
                        phase,
                        command.name,
                        self._display_command(command, task),
                        "dry-run",
                        None,
                        utc_now(),
                        utc_now(),
                        "",
                        "",
                        executor=command.executor,
                        executor_metadata=executor.metadata.as_contract(),
                    )
                )
                continue
            run = self._run_command(command, phase=phase, task=task)
            runs.append(run)
            if command.required and run.status == "blocked":
                return "blocked", run.stderr or f"Required {phase} command blocked: {command.name}"
            if command.required and run.returncode != 0:
                return "failed", f"Required {phase} command failed: {command.name}"
        return "passed", f"All required {phase} commands passed."

    def git_checkpoint(
        self,
        task: HarnessTask,
        *,
        push: bool = False,
        remote: str = "origin",
        branch: str | None = None,
        message_template: str = "chore(engineering): complete {task_id}",
    ) -> dict[str, Any]:
        if not self._is_git_repo():
            return {"status": "skipped", "message": "project root is not inside a git repository"}

        task_state = self.load_state().get("tasks", {}).get(task.id, {})
        dirty_before = [str(path) for path in task_state.get("last_dirty_before_paths", [])]
        if dirty_before:
            return {
                "status": "skipped",
                "message": "dirty worktree existed before the task; refusing to checkpoint mixed changes",
                "dirty_before_paths": dirty_before,
            }

        status_before = self._git(["status", "--porcelain"])
        if status_before["returncode"] != 0:
            return {
                "status": "failed",
                "message": "could not inspect git status",
                "stderr": status_before["stderr"],
            }
        current_paths = self._git_status_paths()
        scope_violations = self._scope_violations(current_paths, task.file_scope)
        if scope_violations:
            return {
                "status": "skipped",
                "message": "dirty files are outside task file_scope; refusing checkpoint",
                "violations": scope_violations,
            }
        if not status_before["stdout"].strip():
            return {"status": "skipped", "message": "no git changes to commit"}

        add_result = self._git(["add", "-A", "--", "."])
        if add_result["returncode"] != 0:
            return {"status": "failed", "message": "git add failed", "stderr": add_result["stderr"]}

        staged = self._git(["diff", "--cached", "--quiet"])
        if staged["returncode"] == 0:
            return {"status": "skipped", "message": "no staged git changes to commit"}
        if staged["returncode"] not in (0, 1):
            return {"status": "failed", "message": "could not inspect staged git diff", "stderr": staged["stderr"]}

        message = message_template.format(
            task_id=task.id,
            task_title=task.title,
            milestone_id=task.milestone_id,
            milestone_title=task.milestone_title,
        )
        commit_result = self._git(["commit", "-m", message])
        if commit_result["returncode"] != 0:
            return {"status": "failed", "message": "git commit failed", "stderr": commit_result["stderr"]}

        commit_sha = self._git(["rev-parse", "HEAD"])
        payload: dict[str, Any] = {
            "status": "committed",
            "message": message,
            "commit": commit_sha["stdout"].strip() if commit_sha["returncode"] == 0 else None,
        }

        if push:
            target_branch = branch or self._current_branch()
            if not target_branch:
                payload.update({"status": "failed", "push_status": "failed", "stderr": "could not resolve current branch"})
                return payload
            push_result = self._git(["push", remote, f"HEAD:{target_branch}"])
            payload["push_status"] = "pushed" if push_result["returncode"] == 0 else "failed"
            payload["push_remote"] = remote
            payload["push_branch"] = target_branch
            payload["push_stdout"] = push_result["stdout"]
            payload["push_stderr"] = push_result["stderr"]
            if push_result["returncode"] != 0:
                payload["status"] = "failed"
        return payload

    def _is_git_repo(self) -> bool:
        result = self._git(["rev-parse", "--is-inside-work-tree"])
        return result["returncode"] == 0 and result["stdout"].strip() == "true"

    def _current_branch(self) -> str | None:
        result = self._git(["branch", "--show-current"])
        branch = result["stdout"].strip()
        return branch or None

    def _git(self, args: list[str]) -> dict[str, Any]:
        completed = subprocess.run(
            ["git", *args],
            cwd=self.project_root,
            text=True,
            capture_output=True,
        )
        return {
            "returncode": completed.returncode,
            "stdout": redact(completed.stdout[-8000:]),
            "stderr": redact(completed.stderr[-8000:]),
        }

    def _display_command(self, command: AcceptanceCommand, task: HarnessTask) -> str:
        executor = self.executor_registry.get(command.executor)
        if executor is None:
            return command.command or command.prompt or command.executor
        return executor.display_command(self._executor_invocation(command, task))

    def _executor_invocation(self, command: AcceptanceCommand, task: HarnessTask) -> ExecutorInvocation:
        invocation = ExecutorInvocation(
            project_root=self.project_root,
            task_id=task.id,
            name=command.name,
            command=command.command,
            prompt=command.prompt,
            timeout_seconds=command.timeout_seconds,
            model=command.model,
            sandbox=command.sandbox,
        )
        executor = self.executor_registry.get(command.executor)
        if executor is None:
            return invocation
        prepare_invocation = getattr(executor, "prepare_invocation", None)
        if prepare_invocation is None:
            return invocation
        return prepare_invocation(invocation, self._executor_task_context(task))

    def _executor_task_context(self, task: HarnessTask) -> ExecutorTaskContext:
        def task_command(command: AcceptanceCommand) -> ExecutorTaskCommand:
            return ExecutorTaskCommand(
                name=command.name,
                command=command.command,
                prompt=command.prompt,
                executor=command.executor,
            )

        return ExecutorTaskContext(
            project_root=self.project_root,
            task_id=task.id,
            title=task.title,
            milestone_id=task.milestone_id,
            milestone_title=task.milestone_title,
            file_scope=task.file_scope,
            acceptance=tuple(task_command(item) for item in task.acceptance),
            e2e=tuple(task_command(item) for item in task.e2e),
        )

    def _run_command(self, acceptance: AcceptanceCommand, *, phase: str, task: HarnessTask) -> CommandRun:
        executor = self.executor_registry.get(acceptance.executor)
        if executor is None:
            return CommandRun(
                phase,
                acceptance.name,
                self._display_command(acceptance, task),
                "failed",
                None,
                utc_now(),
                utc_now(),
                "",
                f"unknown executor: {acceptance.executor}",
                executor=acceptance.executor,
                executor_metadata=self.executor_registry.metadata_for(acceptance.executor),
            )
        invocation = self._executor_invocation(acceptance, task)
        display_command = executor.display_command(invocation)
        result = executor.execute(invocation)
        return CommandRun(
            phase,
            acceptance.name,
            display_command,
            result.status,
            result.returncode,
            result.started_at,
            result.finished_at,
            result.stdout,
            result.stderr,
            executor=executor.metadata.id,
            executor_metadata=executor.metadata.as_contract(),
            result_metadata=result.metadata,
        )

    def _finish_task(
        self,
        state: dict[str, Any],
        task: HarnessTask,
        report_path: Path,
        started_at: str,
        runs: list[CommandRun],
        status: str,
        message: str,
        persist: bool,
        *,
        safety: dict[str, Any] | None = None,
        allow_live: bool = False,
        allow_manual: bool = False,
        allow_agent: bool = False,
    ) -> dict[str, Any]:
        finished_at = utc_now()
        manifest_path = report_path.with_suffix(".json")
        self._write_report(report_path, task, started_at, finished_at, runs, status, message, safety=safety)
        self._write_task_manifest(
            manifest_path,
            report_path,
            task,
            started_at,
            finished_at,
            runs,
            status,
            message,
            persist,
            int(state.get("tasks", {}).get(task.id, {}).get("attempts", 0)),
            safety=safety,
            allow_live=allow_live,
            allow_manual=allow_manual,
            allow_agent=allow_agent,
        )
        self.rebuild_manifest_index()
        if persist:
            task_state = state.setdefault("tasks", {}).setdefault(task.id, {})
            task_state["status"] = status
            task_state["last_finished_at"] = finished_at
            task_state["last_report"] = str(report_path.relative_to(self.project_root))
            task_state["last_manifest"] = str(manifest_path.relative_to(self.project_root))
            self.save_state(state)
        append_jsonl(
            self.decision_log_path,
            {
                "at": finished_at,
                "event": "task_run",
                "task_id": task.id,
                "milestone_id": task.milestone_id,
                "status": status,
                "dry_run": not persist,
                "report": str(report_path.relative_to(self.project_root)),
                "manifest": str(manifest_path.relative_to(self.project_root)),
            },
        )
        return {
            "task": self.task_payload(task),
            "status": status,
            "message": message,
            "report": str(report_path.relative_to(self.project_root)),
            "manifest": str(manifest_path.relative_to(self.project_root)),
            "runs": [
                {
                    "phase": run.phase,
                    "name": run.name,
                    "command": run.command,
                    "status": run.status,
                    "returncode": run.returncode,
                    "executor": run.executor,
                    "executor_metadata": run.executor_metadata or self.executor_registry.metadata_for(run.executor),
                    "executor_result": self._executor_result_contract(run),
                }
                for run in runs
            ],
            "safety": safety or {},
        }

    def _write_task_manifest(
        self,
        manifest_path: Path,
        report_path: Path,
        task: HarnessTask,
        started_at: str,
        finished_at: str,
        runs: list[CommandRun],
        status: str,
        message: str,
        persist: bool,
        attempt: int,
        *,
        safety: dict[str, Any] | None = None,
        allow_live: bool = False,
        allow_manual: bool = False,
        allow_agent: bool = False,
    ) -> None:
        manifest_relative = str(manifest_path.relative_to(self.project_root))
        report_relative = str(report_path.relative_to(self.project_root))
        payload = {
            "schema_version": 1,
            "kind": "engineering-harness.task-run-manifest",
            "project": str(self.roadmap.get("project", self.project_root.name)),
            "project_root": str(self.project_root),
            "profile": self.roadmap.get("profile"),
            "roadmap_path": str(self.roadmap_path.relative_to(self.project_root))
            if self.roadmap_path and self.roadmap_path.is_relative_to(self.project_root)
            else str(self.roadmap_path),
            "task_id": task.id,
            "milestone_id": task.milestone_id,
            "task": self.task_payload(task),
            "milestone": {
                "id": task.milestone_id,
                "title": task.milestone_title,
            },
            "status": status,
            "message": message,
            "started_at": started_at,
            "finished_at": finished_at,
            "dry_run": not persist,
            "attempt": attempt,
            "report_path": report_relative,
            "manifest_path": manifest_relative,
            "artifacts": [
                {"kind": "markdown_report", "path": report_relative},
                {"kind": "json_manifest", "path": manifest_relative},
            ],
            "runs": [self._command_run_manifest(task, run) for run in runs],
            "safety": safety or {},
            "policy_decisions": self._policy_decisions(
                task,
                safety=safety or {},
                allow_live=allow_live,
                allow_manual=allow_manual,
                allow_agent=allow_agent,
            ),
            "git": self._git_context(safety or {}),
        }
        write_json(manifest_path, payload)

    def _command_run_manifest(self, task: HarnessTask, run: CommandRun) -> dict[str, Any]:
        metadata = self._configured_command_metadata(task, run)
        stdout_summary = self._stream_summary(run.stdout)
        stderr_summary = self._stream_summary(run.stderr)
        return {
            "phase": run.phase,
            "name": run.name,
            "executor": metadata["executor"],
            "command": run.command,
            "status": run.status,
            "returncode": run.returncode,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "required": metadata.get("required"),
            "timeout_seconds": metadata.get("timeout_seconds"),
            "model": metadata.get("model"),
            "sandbox": metadata.get("sandbox"),
            "stdout": stdout_summary,
            "stderr": stderr_summary,
            "executor_metadata": run.executor_metadata
            or metadata.get("executor_metadata")
            or self.executor_registry.metadata_for(metadata["executor"]),
            "executor_result": self._executor_result_contract(
                run,
                stdout_summary=stdout_summary,
                stderr_summary=stderr_summary,
            ),
        }

    def _executor_result_contract(
        self,
        run: CommandRun,
        *,
        stdout_summary: dict[str, Any] | None = None,
        stderr_summary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "schema_version": EXECUTOR_RESULT_CONTRACT_VERSION,
            "status": run.status,
            "returncode": run.returncode,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "stdout": stdout_summary or self._stream_summary(run.stdout),
            "stderr": stderr_summary or self._stream_summary(run.stderr),
            "metadata": run.result_metadata,
        }

    def _configured_command_metadata(self, task: HarnessTask, run: CommandRun) -> dict[str, Any]:
        for command in (*task.implementation, *task.repair, *task.acceptance, *task.e2e):
            if command.name == run.name and self._display_command(command, task) == run.command:
                return {
                    "executor": command.executor,
                    "required": command.required,
                    "timeout_seconds": command.timeout_seconds,
                    "model": command.model,
                    "sandbox": command.sandbox,
                    "executor_metadata": self.executor_registry.metadata_for(command.executor),
                }
        return {
            "executor": run.executor or ("codex" if run.command.startswith("codex exec ") else "shell"),
            "required": None,
            "timeout_seconds": None,
            "model": None,
            "sandbox": None,
            "executor_metadata": run.executor_metadata
            or self.executor_registry.metadata_for(
                run.executor or ("codex" if run.command.startswith("codex exec ") else "shell")
            ),
        }

    def _stream_summary(self, text: str) -> dict[str, Any]:
        encoded = text.encode("utf-8")
        return {
            "bytes": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest() if text else None,
        }

    def _policy_decisions(
        self,
        task: HarnessTask,
        *,
        safety: dict[str, Any],
        allow_live: bool,
        allow_manual: bool,
        allow_agent: bool,
    ) -> list[dict[str, Any]]:
        decisions: list[dict[str, Any]] = [
            {
                "kind": "manual_approval",
                "scope": "task",
                "outcome": "allowed" if not task.manual_approval_required or allow_manual else "denied",
                "reason": self._approval_reason(task.manual_approval_required, allow_manual, "manual approval"),
            },
            {
                "kind": "agent_approval",
                "scope": "task",
                "outcome": "allowed" if not task.agent_approval_required or allow_agent else "denied",
                "reason": self._approval_reason(task.agent_approval_required, allow_agent, "agent approval"),
            },
        ]
        for phase, commands in (
            ("implementation", task.implementation),
            ("repair", task.repair),
            ("acceptance", task.acceptance),
            ("e2e", task.e2e),
        ):
            for command in commands:
                executor = self.executor_registry.get(command.executor)
                if executor and executor.metadata.uses_command_policy:
                    allowed, reason = self.command_allowed(command.command, allow_live=allow_live)
                    decisions.append(
                        {
                            "kind": "command_policy",
                            "scope": "command",
                            "phase": phase,
                            "name": command.name,
                            "executor": command.executor,
                            "outcome": "allowed" if allowed else "denied",
                            "reason": reason,
                        }
                    )
                elif executor and executor.metadata.requires_agent_approval:
                    decisions.append(
                        {
                            "kind": "executor_approval",
                            "scope": "command",
                            "phase": phase,
                            "name": command.name,
                            "executor": command.executor,
                            "outcome": "allowed" if allow_agent else "denied",
                            "reason": f"{command.executor} executor requires --allow-agent"
                            if not allow_agent
                            else "agent approval satisfied",
                        }
                    )
                elif executor:
                    decisions.append(
                        {
                            "kind": "executor_policy",
                            "scope": "command",
                            "phase": phase,
                            "name": command.name,
                            "executor": command.executor,
                            "outcome": "allowed",
                            "reason": "registered executor",
                        }
                    )
                else:
                    decisions.append(
                        {
                            "kind": "executor_policy",
                            "scope": "command",
                            "phase": phase,
                            "name": command.name,
                            "executor": command.executor,
                            "outcome": "denied",
                            "reason": f"unknown executor: {command.executor}",
                        }
                    )

        git_preflight = safety.get("git_preflight", {})
        file_scope_guard = safety.get("file_scope_guard", {})
        decisions.extend(
            [
                {
                    "kind": "git_preflight",
                    "scope": "worktree",
                    "outcome": str(git_preflight.get("status", "unknown")),
                    "reason": str(git_preflight.get("message", "")),
                },
                {
                    "kind": "file_scope_guard",
                    "scope": "worktree",
                    "outcome": str(file_scope_guard.get("status", "unknown")),
                    "reason": str(file_scope_guard.get("message", "")),
                },
            ]
        )
        return decisions

    def _approval_reason(self, required: bool, allowed: bool, label: str) -> str:
        if not required:
            return f"{label} not required"
        return f"{label} satisfied" if allowed else f"{label} required"

    def _git_context(self, safety: dict[str, Any]) -> dict[str, Any]:
        git_preflight = safety.get("git_preflight", {})
        file_scope_guard = safety.get("file_scope_guard", {})
        context: dict[str, Any] = {
            "is_repository": False,
            "root": None,
            "branch": None,
            "head": None,
            "short_head": None,
            "dirty_before_paths": git_preflight.get("dirty_before_paths", []),
            "dirty_after_paths": file_scope_guard.get("dirty_after_paths", []),
            "dirty_before_out_of_scope_paths": git_preflight.get("dirty_before_out_of_scope_paths", []),
            "file_scope_violations": file_scope_guard.get("violations", []),
            "status_short": "",
        }
        if not self._is_git_repo():
            return context

        root = self._git(["rev-parse", "--show-toplevel"])
        head = self._git(["rev-parse", "HEAD"])
        short_head = self._git(["rev-parse", "--short", "HEAD"])
        status = self._git(["status", "--porcelain"])
        context.update(
            {
                "is_repository": True,
                "root": root["stdout"].strip() if root["returncode"] == 0 else None,
                "branch": self._current_branch(),
                "head": head["stdout"].strip() if head["returncode"] == 0 else None,
                "short_head": short_head["stdout"].strip() if short_head["returncode"] == 0 else None,
                "status_short": status["stdout"] if status["returncode"] == 0 else "",
            }
        )
        context["refs"] = {
            "head": context["head"],
            "short_head": context["short_head"],
            "branch": context["branch"],
        }
        return context

    def _write_report(
        self,
        report_path: Path,
        task: HarnessTask,
        started_at: str,
        finished_at: str,
        runs: list[CommandRun],
        status: str,
        message: str,
        *,
        safety: dict[str, Any] | None = None,
    ) -> None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            f"# Task Report: {task.id}",
            "",
            f"- Status: `{status}`",
            f"- Project: `{self.roadmap.get('project', self.project_root.name)}`",
            f"- Milestone: `{task.milestone_id}` {task.milestone_title}",
            f"- Task: {task.title}",
            f"- Started: {started_at}",
            f"- Finished: {finished_at}",
            f"- Message: {message}",
            "",
            "## Task Runs",
            "",
        ]
        if not runs:
            lines.append("No task commands were executed.")
        for run in runs:
            lines.extend(
                [
                    f"### {run.phase}: {run.name}",
                    "",
                    f"- Status: `{run.status}`",
                    f"- Return code: `{run.returncode}`",
                    "",
                    "```bash",
                    run.command,
                    "```",
                    "",
                ]
            )
            if run.stdout:
                lines.extend(["Stdout:", "", "```text", run.stdout, "```", ""])
            if run.stderr:
                lines.extend(["Stderr:", "", "```text", run.stderr, "```", ""])
        if safety:
            git_preflight = safety.get("git_preflight", {})
            file_scope_guard = safety.get("file_scope_guard", {})
            lines.extend(
                [
                    "## Safety",
                    "",
                    f"- Git preflight: `{git_preflight.get('status', 'unknown')}` - {git_preflight.get('message', '')}",
                    f"- Dirty before: `{len(git_preflight.get('dirty_before_paths', []))}`",
                    f"- File-scope guard: `{file_scope_guard.get('status', 'unknown')}` - {file_scope_guard.get('message', '')}",
                    f"- New dirty paths: `{len(file_scope_guard.get('new_dirty_paths', []))}`",
                    f"- Changed pre-existing dirty paths: `{len(file_scope_guard.get('changed_preexisting_dirty_paths', []))}`",
                    f"- File-scope violations: `{len(file_scope_guard.get('violations', []))}`",
                    "",
                ]
            )
            violations = file_scope_guard.get("violations", [])
            if violations:
                lines.extend(["Violations:", ""])
                lines.extend(f"- `{path}`" for path in violations[:40])
                lines.append("")
        report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
