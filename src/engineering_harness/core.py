from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .io import append_jsonl, load_mapping, write_json, write_mapping
from .profiles import command_policy, default_roadmap


COMPLETED_STATUSES = {"done", "passed", "skipped"}
BLOCKED_STATUSES = {"blocked", "paused"}
CONFIG_CANDIDATES = (".engineering/roadmap.yaml", ".engineering/roadmap.json", "ops/engineering/roadmap.yaml")
PRUNE_DIRS = {".git", "node_modules", ".venv", "venv", ".pytest_cache", "dist", "out", "cache", "artifacts"}


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
    def __init__(self, project_root: Path, roadmap_path: Path | None = None) -> None:
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
        self.command_policy = self._load_command_policy()

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
        if command.executor == "codex" and not allow_agent:
            run = CommandRun(
                "self-iteration",
                command.name,
                self._display_command(command, self._self_iteration_task(command, snapshot_path, "")),
                "blocked",
                None,
                utc_now(),
                utc_now(),
                "",
                "codex planner requires --allow-agent",
            )
            self._write_self_iteration_report(report_path, reason, snapshot_path, before, before, run)
            return {
                "status": "blocked",
                "message": "codex planner requires --allow-agent",
                "stage_count_before": before["stage_count"],
                "stage_count_after": before["stage_count"],
                "pending_stage_count_after": before["pending_stage_count"],
                "report": str(report_path.relative_to(self.project_root)),
            }
        if command.executor == "shell":
            allowed, block_reason = self.command_allowed(command.command, allow_live=allow_live)
            if not allowed:
                run = CommandRun(
                    "self-iteration",
                    command.name,
                    command.command or "",
                    "blocked",
                    None,
                    utc_now(),
                    utc_now(),
                    "",
                    block_reason,
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
        elif command.executor != "codex":
            run = CommandRun(
                "self-iteration",
                command.name,
                command.command or command.prompt or command.executor,
                "blocked",
                None,
                utc_now(),
                utc_now(),
                "",
                f"unknown executor: {command.executor}",
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
            agent_approval_required=command.executor == "codex",
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
            "continuation": self.continuation_summary(),
            "self_iteration": self.self_iteration_summary(),
        }

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
        report_path = self.report_dir / f"{slug_now()}-{task.id}.md"
        state = self.load_state()
        task_state = state.setdefault("tasks", {}).setdefault(task.id, {})
        task_state["attempts"] = int(task_state.get("attempts", 0)) + (0 if dry_run else 1)
        task_state["last_started_at"] = started_at
        task_state["last_dry_run"] = dry_run

        if task.manual_approval_required and not allow_manual:
            return self._finish_task(state, task, report_path, started_at, [], "blocked", "manual approval required", not dry_run)
        if task.agent_approval_required and not allow_agent:
            return self._finish_task(state, task, report_path, started_at, [], "blocked", "agent implementation requires --allow-agent", not dry_run)
        if not task.acceptance:
            return self._finish_task(state, task, report_path, started_at, [], "blocked", "task has no acceptance", not dry_run)

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

        status = "dry-run" if dry_run and overall_status == "passed" else overall_status
        return self._finish_task(state, task, report_path, started_at, runs, status, message, not dry_run)

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
            if command.executor == "codex" and not allow_agent:
                runs.append(
                    CommandRun(phase, command.name, self._display_command(command, task), "blocked", None, utc_now(), utc_now(), "", "codex executor requires --allow-agent")
                )
                return "blocked", "codex executor requires --allow-agent"
            if command.executor == "shell":
                allowed, reason = self.command_allowed(command.command, allow_live=allow_live)
                if not allowed:
                    runs.append(
                        CommandRun(phase, command.name, command.command or "", "blocked", None, utc_now(), utc_now(), "", reason)
                    )
                    return "blocked", reason
            elif command.executor != "codex":
                runs.append(
                    CommandRun(phase, command.name, self._display_command(command, task), "blocked", None, utc_now(), utc_now(), "", f"unknown executor: {command.executor}")
                )
                return "blocked", f"unknown executor: {command.executor}"
            if dry_run:
                runs.append(CommandRun(phase, command.name, self._display_command(command, task), "dry-run", None, utc_now(), utc_now(), "", ""))
                continue
            run = self._run_command(command, phase=phase, task=task)
            runs.append(run)
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

        status_before = self._git(["status", "--porcelain"])
        if status_before["returncode"] != 0:
            return {
                "status": "failed",
                "message": "could not inspect git status",
                "stderr": status_before["stderr"],
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
        if command.executor == "codex":
            model = f" --model {command.model}" if command.model else ""
            return f"codex exec --full-auto --sandbox {command.sandbox}{model} -C {self.project_root} <task:{task.id}>"
        return command.command or ""

    def _codex_prompt(self, command: AcceptanceCommand, task: HarnessTask) -> str:
        acceptance = "\n".join(f"- {item.name}: {item.command or item.prompt or item.executor}" for item in task.acceptance)
        file_scope = "\n".join(f"- {scope}" for scope in task.file_scope) or "- repository-scoped, but keep changes minimal"
        prompt = command.prompt or task.title
        return (
            "You are executing one roadmap task for an autonomous engineering harness.\n\n"
            f"Project root: {self.project_root}\n"
            f"Milestone: {task.milestone_id} - {task.milestone_title}\n"
            f"Task: {task.id} - {task.title}\n\n"
            "Goal:\n"
            f"{prompt}\n\n"
            "Allowed file scope:\n"
            f"{file_scope}\n\n"
            "Acceptance commands that must pass after your changes:\n"
            f"{acceptance}\n\n"
            "Constraints:\n"
            "- Edit files directly in the working tree.\n"
            "- Do not commit or push; the harness handles git checkpoints.\n"
            "- Do not use private keys, paid live deployment, or live trading.\n"
            "- Prefer focused, test-driven changes that satisfy the acceptance commands.\n"
            "- If the task cannot be completed locally, write a clear blocker into the relevant project docs.\n"
        )

    def _run_command(self, acceptance: AcceptanceCommand, *, phase: str, task: HarnessTask) -> CommandRun:
        started_at = utc_now()
        display_command = self._display_command(acceptance, task)
        try:
            if acceptance.executor == "codex":
                args = ["codex", "exec", "--full-auto", "--sandbox", acceptance.sandbox, "-C", str(self.project_root)]
                if acceptance.model:
                    args.extend(["--model", acceptance.model])
                args.append(self._codex_prompt(acceptance, task))
                completed = subprocess.run(
                    args,
                    cwd=self.project_root,
                    text=True,
                    capture_output=True,
                    timeout=acceptance.timeout_seconds,
                    env={**os.environ, "ENGINEERING_HARNESS": "1"},
                )
            else:
                completed = subprocess.run(
                    acceptance.command or "",
                    cwd=self.project_root,
                    shell=True,
                    executable="/bin/bash",
                    text=True,
                    capture_output=True,
                    timeout=acceptance.timeout_seconds,
                    env={**os.environ, "ENGINEERING_HARNESS": "1"},
                )
            return CommandRun(
                phase,
                acceptance.name,
                display_command,
                "passed" if completed.returncode == 0 else "failed",
                completed.returncode,
                started_at,
                utc_now(),
                redact(completed.stdout[-8000:]),
                redact(completed.stderr[-8000:]),
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            return CommandRun(
                phase,
                acceptance.name,
                display_command,
                "failed",
                None,
                started_at,
                utc_now(),
                redact(stdout[-8000:]),
                f"Command timed out after {acceptance.timeout_seconds} seconds.",
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
    ) -> dict[str, Any]:
        finished_at = utc_now()
        self._write_report(report_path, task, started_at, finished_at, runs, status, message)
        if persist:
            task_state = state.setdefault("tasks", {}).setdefault(task.id, {})
            task_state["status"] = status
            task_state["last_finished_at"] = finished_at
            task_state["last_report"] = str(report_path.relative_to(self.project_root))
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
            },
        )
        return {
            "task": self.task_payload(task),
            "status": status,
            "message": message,
            "report": str(report_path.relative_to(self.project_root)),
            "runs": [
                {
                    "phase": run.phase,
                    "name": run.name,
                    "command": run.command,
                    "status": run.status,
                    "returncode": run.returncode,
                }
                for run in runs
            ],
        }

    def _write_report(
        self,
        report_path: Path,
        task: HarnessTask,
        started_at: str,
        finished_at: str,
        runs: list[CommandRun],
        status: str,
        message: str,
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
        report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
