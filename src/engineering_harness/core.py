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
POLICY_INPUT_SCHEMA_VERSION = 1
POLICY_DECISION_SCHEMA_VERSION = 1
PHASE_STATE_SCHEMA_VERSION = 1
DRIVE_CONTROL_SCHEMA_VERSION = 1
APPROVAL_QUEUE_SCHEMA_VERSION = 1
APPROVAL_DECISION_KINDS = {
    "manual_approval": "manual",
    "agent_approval": "agent",
    "executor_approval": "agent",
    "live_approval": "live",
}
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
SELF_ITERATION_CONTEXT_SCHEMA_VERSION = 1
SELF_ITERATION_CONTEXT_LIMITS = {
    "recent_manifest_count": 5,
    "recent_report_count": 8,
    "doc_count": 8,
    "doc_excerpt_chars": 1200,
    "test_file_count": 60,
    "test_name_count": 20,
    "source_file_count": 120,
    "continuation_stage_count": 12,
    "manifest_run_count": 8,
    "message_chars": 500,
    "git_commit_count": 8,
}
SELF_ITERATION_DOC_EXTENSIONS = {".md", ".markdown", ".rst", ".txt"}
SELF_ITERATION_TEST_EXTENSIONS = {".py", ".js", ".jsx", ".ts", ".tsx", ".sh"}
SELF_ITERATION_SOURCE_EXTENSIONS = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".c",
    ".cc",
    ".cpp",
    ".h",
    ".hpp",
    ".cs",
    ".rb",
    ".php",
    ".sh",
}
SELF_ITERATION_UNSAFE_REQUIREMENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private credential use",
        re.compile(
            r"\b(?:use|configure|require|load|import|set|provide|read)\s+"
            r"(?:a\s+)?(?:private\s+keys?|mnemonics?|seed\s+phrases?|api\s+keys?)\b"
        ),
    ),
    (
        "private credential assignment",
        re.compile(r"\b(?:PRIVATE_KEY|MNEMONIC|OPENAI_API_KEY|ANTHROPIC_API_KEY)\s*=", re.IGNORECASE),
    ),
    (
        "mainnet write",
        re.compile(
            r"\b(?:cast\s+send|deploy:mainnet|--broadcast|--rpc-url\s+mainnet|"
            r"mainnet\s+(?:write|transaction|deploy|deployment|release))\b"
        ),
    ),
    ("production deployment", re.compile(r"\bdeploy(?:ment)?\s+(?:to\s+)?(?:production|prod|mainnet)\b")),
    ("production deployment", re.compile(r"\b(?:production|prod|mainnet)\s+(?:deploy|deployment|release)\b")),
    (
        "production service mutation",
        re.compile(
            r"\b(?:call|write\s+to|mutate|modify)\s+(?:the\s+)?(?:production|prod|live)\s+"
            r"(?:api|service|database|system)\b"
        ),
    ),
    ("live operation", re.compile(r"\b--live\b|\blive\s+(?:deployment|service|trading|orders?|trades?)\b")),
    ("live operation", re.compile(r"\b(?:place|submit|send|execute)\s+(?:real|live)\s+(?:orders?|trades?)\b")),
    (
        "real-fund movement",
        re.compile(
            r"\b(?:withdraw|transfer|move)\s+real\s+(?:funds|money)\b|"
            r"\breal[- ]?fund\s+(?:transfer|withdrawal|payment)\b"
        ),
    ),
    ("paid service", re.compile(r"\bpaid[- ]?(?:service|services|account|accounts|hosting|deployment|api|subscription)\b")),
)
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
        cursor = 0
        while True:
            index = redacted.find(marker, cursor)
            if index < 0:
                break
            token_start = index + len(marker)
            if redacted.startswith("[REDACTED]", token_start):
                cursor = token_start + len("[REDACTED]")
                continue
            after = redacted[token_start:]
            token = after.split()[0] if after.split() else ""
            if not token:
                cursor = token_start
                continue
            redacted = redacted[:token_start] + "[REDACTED]" + redacted[token_start + len(token) :]
            cursor = token_start + len("[REDACTED]")
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


@dataclass(frozen=True)
class PolicyInput:
    project: dict[str, Any]
    task: dict[str, Any]
    phase: str
    command: dict[str, Any] | None
    executor: dict[str, Any] | None
    git: dict[str, Any]
    worktree: dict[str, Any]
    file_scope: dict[str, Any]
    approvals: dict[str, Any]
    live: dict[str, Any]
    context: dict[str, Any]

    def as_contract(self) -> dict[str, Any]:
        return {
            "schema_version": POLICY_INPUT_SCHEMA_VERSION,
            "project": self.project,
            "task": self.task,
            "phase": self.phase,
            "command": self.command,
            "executor": self.executor,
            "git": self.git,
            "worktree": self.worktree,
            "file_scope": self.file_scope,
            "approvals": self.approvals,
            "live": self.live,
            "context": self.context,
        }


@dataclass(frozen=True)
class PolicyDecision:
    kind: str
    scope: str
    outcome: str
    reason: str
    policy_input: PolicyInput
    severity: str = "info"
    effect: str = "allow"
    requires_approval: bool = False
    approval_flag: str | None = None
    status: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def blocks_execution(self) -> bool:
        return self.effect in {"deny", "requires_approval"}

    def as_contract(self) -> dict[str, Any]:
        policy_input = self.policy_input.as_contract()
        command = policy_input.get("command") or {}
        executor = policy_input.get("executor") or {}
        payload: dict[str, Any] = {
            "schema_version": POLICY_DECISION_SCHEMA_VERSION,
            "kind": self.kind,
            "scope": self.scope,
            "outcome": self.outcome,
            "effect": self.effect,
            "severity": self.severity,
            "reason": self.reason,
            "requires_approval": self.requires_approval,
            "input": policy_input,
        }
        if self.status is not None:
            payload["status"] = self.status
        if self.approval_flag is not None:
            payload["approval_flag"] = self.approval_flag
        if self.metadata:
            payload["metadata"] = self.metadata
        phase = policy_input.get("phase")
        if phase:
            payload["phase"] = phase
        if command.get("name"):
            payload["name"] = command["name"]
        if executor.get("id"):
            payload["executor"] = executor["id"]
        return payload


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

    def _drive_control(self, state: dict[str, Any]) -> dict[str, Any]:
        control = state.setdefault("drive_control", {})
        if not isinstance(control, dict):
            control = {}
            state["drive_control"] = control
        control.setdefault("schema_version", DRIVE_CONTROL_SCHEMA_VERSION)
        control.setdefault("status", "idle")
        control.setdefault("active", False)
        control.setdefault("pause_requested", False)
        control.setdefault("cancel_requested", False)
        control.setdefault("reason", None)
        control.setdefault("updated_at", state.get("updated_at"))
        history = control.setdefault("history", [])
        if not isinstance(history, list):
            control["history"] = []
        return control

    def _record_drive_control_event(
        self,
        state: dict[str, Any],
        *,
        command: str,
        from_status: str,
        to_status: str,
        reason: str,
    ) -> dict[str, Any]:
        control = self._drive_control(state)
        event = {
            "at": utc_now(),
            "command": command,
            "from_status": from_status,
            "to_status": to_status,
            "reason": reason,
        }
        history = control.setdefault("history", [])
        if isinstance(history, list):
            history.append(event)
            control["history"] = history[-100:]
        return event

    def set_drive_control(self, command: str, *, reason: str = "manual") -> dict[str, Any]:
        state = self.load_state()
        control = self._drive_control(state)
        from_status = str(control.get("status", "idle"))
        now = utc_now()
        if command == "pause":
            control.update(
                {
                    "status": "paused",
                    "active": False,
                    "pause_requested": True,
                    "cancel_requested": False,
                    "paused_at": now,
                    "reason": reason,
                    "updated_at": now,
                }
            )
            message = "drive pause requested"
        elif command == "resume":
            control.update(
                {
                    "status": "idle",
                    "active": False,
                    "pause_requested": False,
                    "cancel_requested": False,
                    "resumed_at": now,
                    "reason": reason,
                    "updated_at": now,
                }
            )
            message = "drive controls cleared; run `drive` to continue"
        elif command == "cancel":
            control.update(
                {
                    "status": "cancelled",
                    "active": False,
                    "pause_requested": False,
                    "cancel_requested": True,
                    "cancelled_at": now,
                    "reason": reason,
                    "updated_at": now,
                }
            )
            message = "drive cancel requested"
        else:
            raise ValueError(f"unknown drive control command: {command}")
        self._record_drive_control_event(
            state,
            command=command,
            from_status=from_status,
            to_status=str(control["status"]),
            reason=reason,
        )
        self.save_state(state)
        append_jsonl(
            self.decision_log_path,
            {
                "at": now,
                "event": "drive_control",
                "command": command,
                "from_status": from_status,
                "to_status": control["status"],
                "reason": reason,
            },
        )
        return {"status": control["status"], "message": message, "drive_control": deepcopy(control)}

    def start_drive(self, *, reason: str = "drive_command") -> dict[str, Any]:
        state = self.load_state()
        control = self._drive_control(state)
        from_status = str(control.get("status", "idle"))
        if bool(control.get("pause_requested")) or from_status == "paused":
            return {
                "started": False,
                "status": "paused",
                "message": "drive is paused; run `resume` before starting another drive",
                "drive_control": deepcopy(control),
            }
        if bool(control.get("cancel_requested")) or from_status == "cancelled":
            return {
                "started": False,
                "status": "cancelled",
                "message": "drive is cancelled; run `resume` to clear the cancellation before driving again",
                "drive_control": deepcopy(control),
            }
        now = utc_now()
        control.update(
            {
                "schema_version": DRIVE_CONTROL_SCHEMA_VERSION,
                "status": "running",
                "active": True,
                "pause_requested": False,
                "cancel_requested": False,
                "started_at": now,
                "reason": reason,
                "updated_at": now,
            }
        )
        self._record_drive_control_event(
            state,
            command="start",
            from_status=from_status,
            to_status="running",
            reason=reason,
        )
        self.save_state(state)
        return {"started": True, "status": "running", "message": "drive started", "drive_control": deepcopy(control)}

    def finish_drive(self, *, status: str, message: str) -> dict[str, Any]:
        state = self.load_state()
        control = self._drive_control(state)
        from_status = str(control.get("status", "idle"))
        now = utc_now()
        if bool(control.get("cancel_requested")) or status == "cancelled":
            to_status = "cancelled"
            pause_requested = False
            cancel_requested = True
        elif bool(control.get("pause_requested")) or status == "paused":
            to_status = "paused"
            pause_requested = True
            cancel_requested = False
        else:
            to_status = "idle"
            pause_requested = False
            cancel_requested = False
        control.update(
            {
                "schema_version": DRIVE_CONTROL_SCHEMA_VERSION,
                "status": to_status,
                "active": False,
                "pause_requested": pause_requested,
                "cancel_requested": cancel_requested,
                "last_drive_status": status,
                "last_drive_message": message,
                "finished_at": now,
                "updated_at": now,
            }
        )
        self._record_drive_control_event(
            state,
            command="finish",
            from_status=from_status,
            to_status=to_status,
            reason=message,
        )
        self.save_state(state)
        return deepcopy(control)

    def drive_control_summary(self) -> dict[str, Any]:
        state = self.load_state()
        return deepcopy(self._drive_control(state))

    def _approval_queue(self, state: dict[str, Any]) -> dict[str, Any]:
        queue = state.setdefault("approval_queue", {})
        if not isinstance(queue, dict):
            queue = {}
            state["approval_queue"] = queue
        queue.setdefault("schema_version", APPROVAL_QUEUE_SCHEMA_VERSION)
        items = queue.setdefault("items", {})
        if not isinstance(items, dict):
            queue["items"] = {}
        queue.setdefault("updated_at", state.get("updated_at"))
        return queue

    def approval_queue_summary(self, *, status_filter: str | None = "pending") -> dict[str, Any]:
        state = self.load_state()
        queue = self._approval_queue(state)
        items = [
            deepcopy(item)
            for item in queue.get("items", {}).values()
            if isinstance(item, dict) and (status_filter is None or str(item.get("status")) == status_filter)
        ]
        items.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("id") or "")))
        all_items = [item for item in queue.get("items", {}).values() if isinstance(item, dict)]
        counts: dict[str, int] = {}
        for item in all_items:
            status = str(item.get("status", "unknown"))
            counts[status] = counts.get(status, 0) + 1
        return {
            "schema_version": APPROVAL_QUEUE_SCHEMA_VERSION,
            "path": self._project_relative_path(self.state_path),
            "status_filter": status_filter,
            "counts": dict(sorted(counts.items())),
            "pending_count": counts.get("pending", 0),
            "items": items,
        }

    def approve_approval(
        self,
        approval_id: str,
        *,
        approved_by: str = "local",
        reason: str = "manual approval",
    ) -> dict[str, Any]:
        state = self.load_state()
        queue = self._approval_queue(state)
        items = queue.setdefault("items", {})
        record = items.get(approval_id)
        if not isinstance(record, dict):
            return {"status": "not_found", "message": f"approval not found: {approval_id}", "approval_id": approval_id}
        previous_status = str(record.get("status", "pending"))
        now = utc_now()
        if previous_status == "consumed":
            return {
                "status": "consumed",
                "message": f"approval was already consumed: {approval_id}",
                "approval": deepcopy(record),
            }
        record.update(
            {
                "status": "approved",
                "approved_at": now,
                "approved_by": approved_by,
                "approval_reason": reason,
                "updated_at": now,
            }
        )
        queue["updated_at"] = now
        task_id = str(record.get("task_id", ""))
        task_state = state.setdefault("tasks", {}).get(task_id)
        if isinstance(task_state, dict) and str(task_state.get("status")) == "blocked":
            task_state["status"] = "pending"
            task_state["approval_unblocked_at"] = now
            task_state["approval_unblocked_by"] = approval_id
            task_state["blocked_on_approval"] = False
        self.save_state(state)
        append_jsonl(
            self.decision_log_path,
            {
                "at": now,
                "event": "approval",
                "approval_id": approval_id,
                "task_id": task_id,
                "status": "approved",
                "previous_status": previous_status,
                "approved_by": approved_by,
                "reason": reason,
            },
        )
        return {"status": "approved", "message": f"approval recorded: {approval_id}", "approval": deepcopy(record)}

    def approve_all_pending(
        self,
        *,
        approved_by: str = "local",
        reason: str = "manual approval",
    ) -> dict[str, Any]:
        pending = self.approval_queue_summary(status_filter="pending")["items"]
        results = [
            self.approve_approval(str(item["id"]), approved_by=approved_by, reason=reason)
            for item in pending
            if item.get("id")
        ]
        return {
            "status": "approved",
            "message": f"approved {len(results)} pending approval(s)",
            "approved_count": len(results),
            "approvals": results,
        }

    def _approval_phase_key(self, phase: str | None) -> str:
        value = str(phase or "task")
        if value.startswith("acceptance-"):
            return "acceptance"
        if value.startswith("repair-"):
            return "repair"
        return value

    def _approval_identity_from_decision(self, task: HarnessTask, decision: dict[str, Any]) -> dict[str, Any] | None:
        decision_kind = str(decision.get("kind", ""))
        approval_kind = APPROVAL_DECISION_KINDS.get(decision_kind)
        if approval_kind is None:
            return None
        policy_input = decision.get("input") if isinstance(decision.get("input"), dict) else {}
        command = policy_input.get("command") if isinstance(policy_input.get("command"), dict) else {}
        phase = self._approval_phase_key(str(decision.get("phase") or policy_input.get("phase") or "task"))
        name = str(decision.get("name") or command.get("name") or "")
        executor = str(decision.get("executor") or command.get("executor") or "")
        approval_flag = str(decision.get("approval_flag") or "")
        identity = {
            "task_id": task.id,
            "milestone_id": task.milestone_id,
            "approval_kind": approval_kind,
            "decision_kind": decision_kind,
            "phase": phase,
            "name": name,
            "executor": executor,
            "approval_flag": approval_flag,
        }
        raw = json.dumps(identity, sort_keys=True)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
        label_parts = [task.id, approval_kind, phase]
        if name:
            label_parts.append(name)
        elif executor:
            label_parts.append(executor)
        identity["id"] = f"{self._slugify('-'.join(label_parts))}-{digest}"
        return identity

    def _approval_is_approved(
        self,
        task: HarnessTask,
        *,
        decision_kind: str,
        phase: str = "task",
        name: str | None = None,
        executor: str | None = None,
    ) -> bool:
        state = self.load_state()
        items = self._approval_queue(state).get("items", {})
        phase_key = self._approval_phase_key(phase)
        for item in items.values():
            if not isinstance(item, dict) or str(item.get("status")) != "approved":
                continue
            if str(item.get("task_id")) != task.id:
                continue
            if str(item.get("decision_kind")) != decision_kind:
                continue
            if str(item.get("phase", "task")) != phase_key:
                continue
            if name is not None and str(item.get("name", "")) != name:
                continue
            if executor is not None and str(item.get("executor", "")) != executor:
                continue
            return True
        return False

    def _queue_required_approvals(
        self,
        state: dict[str, Any],
        task: HarnessTask,
        decisions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        queue = self._approval_queue(state)
        items = queue.setdefault("items", {})
        created_or_updated: list[dict[str, Any]] = []
        now = utc_now()
        for decision in decisions:
            if not decision.get("requires_approval") or decision.get("outcome") != "requires_approval":
                continue
            identity = self._approval_identity_from_decision(task, decision)
            if identity is None:
                continue
            approval_id = str(identity["id"])
            existing = items.get(approval_id)
            if isinstance(existing, dict) and str(existing.get("status")) in {"pending", "approved"}:
                existing.update(
                    {
                        "last_seen_at": now,
                        "reason": decision.get("reason"),
                        "policy_decision": self._compact_policy_decision(decision),
                        "updated_at": now,
                    }
                )
                created_or_updated.append(deepcopy(existing))
                continue
            record = {
                "schema_version": APPROVAL_QUEUE_SCHEMA_VERSION,
                **identity,
                "status": "pending",
                "reason": decision.get("reason"),
                "created_at": now,
                "updated_at": now,
                "last_seen_at": now,
                "source": "policy",
                "policy_decision": self._compact_policy_decision(decision),
            }
            items[approval_id] = record
            created_or_updated.append(deepcopy(record))
            append_jsonl(
                self.decision_log_path,
                {
                    "at": now,
                    "event": "approval",
                    "approval_id": approval_id,
                    "task_id": task.id,
                    "status": "pending",
                    "decision_kind": identity["decision_kind"],
                    "approval_kind": identity["approval_kind"],
                    "reason": decision.get("reason"),
                },
            )
        if created_or_updated:
            queue["updated_at"] = now
        return created_or_updated

    def _consume_task_approvals(
        self,
        state: dict[str, Any],
        task: HarnessTask,
        *,
        status: str,
    ) -> list[dict[str, Any]]:
        queue = self._approval_queue(state)
        items = queue.setdefault("items", {})
        now = utc_now()
        consumed: list[dict[str, Any]] = []
        for item in items.values():
            if not isinstance(item, dict):
                continue
            if str(item.get("task_id")) != task.id or str(item.get("status")) not in {"pending", "approved"}:
                continue
            item.update(
                {
                    "status": "consumed",
                    "consumed_at": now,
                    "consumed_by_status": status,
                    "updated_at": now,
                }
            )
            consumed.append(deepcopy(item))
        if consumed:
            queue["updated_at"] = now
        return consumed

    def _record_phase_state(
        self,
        state: dict[str, Any],
        task: HarnessTask,
        *,
        phase: str,
        event: str,
        status: str,
        persist: bool,
        message: str | None = None,
        metadata: dict[str, Any] | None = None,
        runs: list[CommandRun] | None = None,
    ) -> dict[str, Any] | None:
        if not persist:
            return None
        task_state = state.setdefault("tasks", {}).setdefault(task.id, {})
        history = task_state.setdefault("phase_history", [])
        if not isinstance(history, list):
            history = []
            task_state["phase_history"] = history
        sequence = self._next_phase_sequence(task_state, history)
        recorded_at = utc_now()
        payload: dict[str, Any] = {
            "schema_version": PHASE_STATE_SCHEMA_VERSION,
            "sequence": sequence,
            "recorded_at": recorded_at,
            "event": event,
            "phase": phase,
            "status": status,
            "task_id": task.id,
            "milestone_id": task.milestone_id,
            "task_attempt": int(task_state.get("attempts", 0)),
        }
        if message is not None:
            payload["message"] = message
        if metadata:
            payload["metadata"] = metadata
        if runs is not None:
            payload["runs"] = [self._command_run_state_summary(run) for run in runs]
        history.append(payload)
        task_state["phase_sequence"] = sequence
        task_state["last_phase_event"] = payload
        phase_states = task_state.setdefault("phase_states", {})
        if isinstance(phase_states, dict):
            phase_states[phase] = payload
        if event == "before":
            task_state["current_phase"] = payload
        else:
            current_phase = task_state.get("current_phase")
            if isinstance(current_phase, dict) and current_phase.get("phase") == phase:
                task_state["current_phase"] = None
        self.save_state(state)
        return payload

    def _next_phase_sequence(self, task_state: dict[str, Any], history: list[Any]) -> int:
        current = int(task_state.get("phase_sequence", 0) or 0)
        for item in history:
            if isinstance(item, dict):
                try:
                    current = max(current, int(item.get("sequence", 0) or 0))
                except (TypeError, ValueError):
                    continue
        return current + 1

    def _command_run_state_summary(self, run: CommandRun) -> dict[str, Any]:
        return {
            "phase": run.phase,
            "name": run.name,
            "status": run.status,
            "returncode": run.returncode,
            "executor": run.executor,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
        }

    def _command_group_state_metadata(self, commands: tuple[AcceptanceCommand, ...]) -> dict[str, Any]:
        return {
            "command_count": len(commands),
            "commands": [
                {
                    "name": command.name,
                    "executor": command.executor,
                    "required": command.required,
                    "timeout_seconds": command.timeout_seconds,
                    "has_command": command.command is not None,
                    "has_prompt": command.prompt is not None,
                    "model": command.model,
                    "sandbox": command.sandbox,
                }
                for command in commands
            ],
        }

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

        before_roadmap = deepcopy(self.roadmap)
        before_roadmap_text = self.roadmap_path.read_text(encoding="utf-8")
        before = self.continuation_summary()
        expected_new_stages = max(1, int(config.get("max_stages_per_iteration", 1)))
        assessment_dir = self.report_dir / "assessments"
        assessment_dir.mkdir(parents=True, exist_ok=True)
        assessment_slug = slug_now()
        snapshot_path = assessment_dir / f"{assessment_slug}-self-iteration-snapshot.json"
        context_path = assessment_dir / f"{assessment_slug}-self-iteration-context.json"
        report_path = assessment_dir / f"{assessment_slug}-self-iteration.md"
        context_pack = self._self_iteration_context_pack(
            reason=reason,
            snapshot_path=snapshot_path,
            context_path=context_path,
        )
        write_json(context_path, context_pack)
        context_info = {
            "path": str(context_path.relative_to(self.project_root)),
            "summary": context_pack["summary"],
        }
        snapshot = {
            "generated_at": utc_now(),
            "reason": reason,
            "status": self.status_summary(),
            "recent_git": self._git(["log", "--oneline", "-8"]),
            "git_status": self._git(["status", "--short"]),
            "context_pack": context_info,
        }
        write_json(snapshot_path, snapshot)

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
            self._write_self_iteration_report(
                report_path,
                reason,
                snapshot_path,
                before,
                before,
                run,
                context_pack=context_info,
            )
            return {
                "status": "blocked",
                "message": f"unknown executor: {command.executor}",
                "stage_count_before": before["stage_count"],
                "stage_count_after": before["stage_count"],
                "pending_stage_count_after": before["pending_stage_count"],
                "report": str(report_path.relative_to(self.project_root)),
                "snapshot": str(snapshot_path.relative_to(self.project_root)),
                "context_pack": context_info,
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
            self._write_self_iteration_report(
                report_path,
                reason,
                snapshot_path,
                before,
                before,
                run,
                context_pack=context_info,
            )
            return {
                "status": "blocked",
                "message": block_reason,
                "stage_count_before": before["stage_count"],
                "stage_count_after": before["stage_count"],
                "pending_stage_count_after": before["pending_stage_count"],
                "report": str(report_path.relative_to(self.project_root)),
                "snapshot": str(snapshot_path.relative_to(self.project_root)),
                "context_pack": context_info,
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
                self._write_self_iteration_report(
                    report_path,
                    reason,
                    snapshot_path,
                    before,
                    before,
                    run,
                    context_pack=context_info,
                )
                return {
                    "status": "blocked",
                    "message": block_reason,
                    "stage_count_before": before["stage_count"],
                    "stage_count_after": before["stage_count"],
                    "pending_stage_count_after": before["pending_stage_count"],
                    "report": str(report_path.relative_to(self.project_root)),
                    "snapshot": str(snapshot_path.relative_to(self.project_root)),
                    "context_pack": context_info,
                }

        planner_prompt = self._self_iteration_prompt(config, snapshot_path, context_path)
        planner_task = self._self_iteration_task(command, snapshot_path, planner_prompt)
        command = replace(command, prompt=planner_prompt)
        run = self._run_command(command, phase="self-iteration", task=planner_task)

        observed_after = before
        validation: dict[str, Any]
        if run.returncode != 0:
            status = "failed"
            message = f"self-iteration planner failed: {command.name}"
        else:
            try:
                after_roadmap = load_mapping(self.roadmap_path)
            except Exception as exc:
                validation = self._self_iteration_validation_result(
                    status="failed",
                    errors=[f"planner output is not a readable roadmap mapping: {exc}"],
                    warnings=[],
                    expected_new_stage_count=expected_new_stages,
                    actual_new_stage_count=0,
                    new_stage_ids=[],
                )
                self.roadmap_path.write_text(before_roadmap_text, encoding="utf-8")
                self.roadmap = deepcopy(before_roadmap)
                status = "rejected"
                message = "self-iteration planner output failed validation"
            else:
                self.roadmap = after_roadmap
                observed_after = self.continuation_summary()
                validation = self._validate_self_iteration_output(
                    before_roadmap,
                    after_roadmap,
                    expected_new_stage_count=expected_new_stages,
                )
                if validation["status"] == "passed":
                    status = "planned"
                    count = len(validation.get("new_stage_ids", []))
                    message = f"self-iteration planner added {count} validated continuation stage(s)"
                else:
                    self.roadmap_path.write_text(before_roadmap_text, encoding="utf-8")
                    self.roadmap = deepcopy(before_roadmap)
                    status = "rejected"
                    message = "self-iteration planner output failed validation"

        if run.returncode != 0:
            self.roadmap_path.write_text(before_roadmap_text, encoding="utf-8")
            self.roadmap = deepcopy(before_roadmap)
            validation = self._self_iteration_validation_result(
                status="skipped",
                errors=[],
                warnings=["planner command failed; output was restored and not validated"],
                expected_new_stage_count=expected_new_stages,
                actual_new_stage_count=0,
                new_stage_ids=[],
            )

        final_after = self.continuation_summary()

        self._write_self_iteration_report(
            report_path,
            reason,
            snapshot_path,
            before,
            observed_after,
            run,
            validation=validation,
            context_pack=context_info,
        )
        append_jsonl(
            self.decision_log_path,
            {
                "at": utc_now(),
                "event": "self_iteration",
                "reason": reason,
                "status": status,
                "message": message,
                "stage_count_before": before["stage_count"],
                "stage_count_after": final_after["stage_count"],
                "pending_stage_count_after": final_after["pending_stage_count"],
                "validation": validation,
                "report": str(report_path.relative_to(self.project_root)),
                "snapshot": str(snapshot_path.relative_to(self.project_root)),
                "context_pack": context_info,
            },
        )
        return {
            "status": status,
            "message": message,
            "stage_count_before": before["stage_count"],
            "stage_count_after": final_after["stage_count"],
            "pending_stage_count_after": final_after["pending_stage_count"],
            "report": str(report_path.relative_to(self.project_root)),
            "snapshot": str(snapshot_path.relative_to(self.project_root)),
            "context_pack": context_info,
            "validation": validation,
            "run": {
                "name": run.name,
                "command": run.command,
                "status": run.status,
                "returncode": run.returncode,
            },
        }

    def _self_iteration_context_pack(
        self,
        *,
        reason: str,
        snapshot_path: Path,
        context_path: Path,
    ) -> dict[str, Any]:
        roadmap_context = self._self_iteration_roadmap_context()
        manifest_context = self._self_iteration_manifest_context()
        report_context = self._self_iteration_report_context()
        docs_context = self._self_iteration_docs_context()
        test_inventory = self._self_iteration_test_inventory()
        source_inventory = self._self_iteration_source_inventory()
        git_context = self._self_iteration_git_context()
        summary = {
            "project": roadmap_context.get("project"),
            "roadmap_path": roadmap_context.get("path"),
            "continuation_stage_count": roadmap_context.get("continuation", {}).get("stage_count", 0),
            "pending_stage_count": roadmap_context.get("continuation", {}).get("pending_stage_count", 0),
            "manifest_count": manifest_context.get("index", {}).get("manifest_count", 0),
            "recent_manifest_count": len(manifest_context.get("recent_task_manifests", [])),
            "task_report_count": report_context.get("task_reports", {}).get("total_count", 0),
            "drive_report_count": report_context.get("drive_reports", {}).get("total_count", 0),
            "doc_count": docs_context.get("relevant_docs", {}).get("included_count", 0),
            "test_file_count": test_inventory.get("included_count", 0),
            "source_file_count": source_inventory.get("included_count", 0),
            "git_is_repository": git_context.get("is_repository", False),
            "git_status_line_count": len(git_context.get("status_lines", [])),
            "recent_commit_count": len(git_context.get("recent_commits", [])),
        }
        payload: dict[str, Any] = {
            "schema_version": SELF_ITERATION_CONTEXT_SCHEMA_VERSION,
            "kind": "engineering-harness.self-iteration-context-pack",
            "reason": reason,
            "project_root": str(self.project_root),
            "snapshot_path": str(snapshot_path.relative_to(self.project_root)),
            "context_path": str(context_path.relative_to(self.project_root)),
            "limits": dict(SELF_ITERATION_CONTEXT_LIMITS),
            "summary": summary,
            "roadmap": roadmap_context,
            "manifests": manifest_context,
            "reports": report_context,
            "docs": docs_context,
            "test_inventory": test_inventory,
            "source_inventory": source_inventory,
            "git": git_context,
        }
        return self._redact_context_value(payload)

    def _self_iteration_roadmap_context(self) -> dict[str, Any]:
        milestones = self.roadmap.get("milestones", [])
        if not isinstance(milestones, list):
            milestones = []
        continuation = self.continuation_summary()
        continuation_config = self.roadmap.get("continuation") if isinstance(self.roadmap.get("continuation"), dict) else {}
        continuation_stages = continuation_config.get("stages", []) if isinstance(continuation_config, dict) else []
        if not isinstance(continuation_stages, list):
            continuation_stages = []
        state = self.load_state()
        task_states = state.get("tasks", {}) if isinstance(state.get("tasks"), dict) else {}
        tasks = list(self.iter_tasks())
        task_status_counts: dict[str, int] = {}
        for task in tasks:
            task_state = task_states.get(task.id, {}) if isinstance(task_states.get(task.id), dict) else {}
            status = str(task_state.get("status", task.status))
            task_status_counts[status] = task_status_counts.get(status, 0) + 1
        goal = self.roadmap.get("goal") if isinstance(self.roadmap.get("goal"), dict) else {}
        generated_from = self.roadmap.get("generated_from") if isinstance(self.roadmap.get("generated_from"), dict) else {}
        return {
            "path": self._project_relative_path(self.roadmap_path),
            "project": str(self.roadmap.get("project", self.project_root.name)),
            "profile": self.roadmap.get("profile"),
            "generated_by": self.roadmap.get("generated_by"),
            "goal": {
                "text": self._truncate_text(str(goal.get("text") or generated_from.get("goal") or ""), 500),
                "blueprint": goal.get("blueprint") or generated_from.get("blueprint_path"),
                "constraints": self._string_items(goal.get("constraints")) if isinstance(goal, dict) else [],
            },
            "milestone_count": len(milestones),
            "task_count": len(tasks),
            "task_status_counts": dict(sorted(task_status_counts.items())),
            "next_task": self.task_payload(self.next_task()),
            "continuation": {
                **continuation,
                "stages": [
                    self._self_iteration_stage_summary(stage)
                    for stage in continuation_stages[: SELF_ITERATION_CONTEXT_LIMITS["continuation_stage_count"]]
                    if isinstance(stage, dict)
                ],
                "stage_count_truncated": len(continuation_stages)
                > SELF_ITERATION_CONTEXT_LIMITS["continuation_stage_count"],
            },
            "self_iteration": self.self_iteration_summary(),
        }

    def _self_iteration_stage_summary(self, stage: dict[str, Any]) -> dict[str, Any]:
        tasks = stage.get("tasks", [])
        if not isinstance(tasks, list):
            tasks = []
        return {
            "id": str(stage.get("id", "")),
            "title": str(stage.get("title", "")),
            "status": str(stage.get("status", "planned")),
            "objective": self._truncate_text(str(stage.get("objective", "")), 500),
            "task_count": len(tasks),
            "tasks": [
                {
                    "id": str(task.get("id", "")),
                    "title": str(task.get("title", "")),
                    "status": str(task.get("status", "pending")),
                    "file_scope": [str(scope) for scope in task.get("file_scope", [])]
                    if isinstance(task.get("file_scope"), list)
                    else [],
                    "acceptance_count": len(task.get("acceptance", [])) if isinstance(task.get("acceptance"), list) else 0,
                    "e2e_count": len(task.get("e2e", [])) if isinstance(task.get("e2e"), list) else 0,
                }
                for task in tasks[:8]
                if isinstance(task, dict)
            ],
            "task_count_truncated": len(tasks) > 8,
        }

    def _self_iteration_manifest_context(self) -> dict[str, Any]:
        index = self._build_manifest_index()
        recent_entries = list(reversed(index.get("manifests", [])))[: SELF_ITERATION_CONTEXT_LIMITS["recent_manifest_count"]]
        recent_manifests = []
        for entry in recent_entries:
            manifest_path = self.project_root / str(entry.get("manifest_path", ""))
            try:
                manifest = load_mapping(manifest_path)
            except Exception as exc:
                recent_manifests.append(
                    {
                        **entry,
                        "load_error": self._truncate_text(str(exc), SELF_ITERATION_CONTEXT_LIMITS["message_chars"]),
                    }
                )
                continue
            recent_manifests.append(self._self_iteration_manifest_summary(entry, manifest))
        return {
            "index": {
                "path": index.get("manifest_index_path"),
                "manifest_count": index.get("manifest_count", 0),
                "latest_manifest": index.get("latest_manifest"),
                "latest_by_task": index.get("latest_by_task", {}),
                "status_counts": index.get("status_counts", {}),
                "policy_decision_summary": index.get("policy_decision_summary", {}),
            },
            "recent_task_manifests": recent_manifests,
        }

    def _self_iteration_manifest_summary(self, entry: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
        runs = manifest.get("runs", []) if isinstance(manifest.get("runs"), list) else []
        git = manifest.get("git", {}) if isinstance(manifest.get("git"), dict) else {}
        return {
            "manifest_path": entry.get("manifest_path") or manifest.get("manifest_path"),
            "report_path": entry.get("report_path") or manifest.get("report_path"),
            "task_id": entry.get("task_id") or manifest.get("task_id"),
            "task_title": entry.get("task_title"),
            "milestone_id": entry.get("milestone_id") or manifest.get("milestone_id"),
            "status": entry.get("status") or manifest.get("status"),
            "message": self._truncate_text(
                str(manifest.get("message", "")),
                SELF_ITERATION_CONTEXT_LIMITS["message_chars"],
            ),
            "started_at": manifest.get("started_at"),
            "finished_at": manifest.get("finished_at"),
            "attempt": manifest.get("attempt"),
            "run_count": len(runs),
            "policy_decision_summary": manifest.get("policy_decision_summary", {}),
            "runs": [
                {
                    "phase": str(run.get("phase", "")),
                    "name": str(run.get("name", "")),
                    "executor": str(run.get("executor", "")),
                    "status": str(run.get("status", "")),
                    "returncode": run.get("returncode"),
                }
                for run in runs[: SELF_ITERATION_CONTEXT_LIMITS["manifest_run_count"]]
                if isinstance(run, dict)
            ],
            "run_count_truncated": len(runs) > SELF_ITERATION_CONTEXT_LIMITS["manifest_run_count"],
            "git": {
                "is_repository": bool(git.get("is_repository", False)),
                "branch": git.get("branch"),
                "head": git.get("head"),
                "short_head": git.get("short_head"),
            },
        }

    def _self_iteration_report_context(self) -> dict[str, Any]:
        task_reports = self._self_iteration_recent_reports(self.report_dir.glob("*.md"))
        drive_reports = self._self_iteration_recent_reports((self.report_dir / "drives").glob("*.md"))
        return {
            "task_reports": task_reports,
            "drive_reports": drive_reports,
        }

    def _self_iteration_recent_reports(self, paths: Any) -> dict[str, Any]:
        report_paths = sorted(
            [path for path in paths if isinstance(path, Path) and path.is_file()],
            key=self._project_relative_path,
        )
        recent = list(reversed(report_paths))[: SELF_ITERATION_CONTEXT_LIMITS["recent_report_count"]]
        return {
            "total_count": len(report_paths),
            "included_count": len(recent),
            "files": [self._self_iteration_report_file_summary(path) for path in recent],
        }

    def _self_iteration_report_file_summary(self, path: Path) -> dict[str, Any]:
        try:
            size = path.stat().st_size
        except OSError:
            size = None
        return {
            "path": self._project_relative_path(path),
            "bytes": size,
            "title": self._markdown_title(path),
        }

    def _self_iteration_docs_context(self) -> dict[str, Any]:
        blueprint = self._self_iteration_blueprint_context()
        blueprint_path = str(blueprint.get("path") or "")
        docs = self._self_iteration_doc_paths()
        docs.sort(key=lambda path: self._self_iteration_doc_sort_key(path, blueprint_path))
        included = docs[: SELF_ITERATION_CONTEXT_LIMITS["doc_count"]]
        return {
            "blueprint": blueprint,
            "relevant_docs": {
                "total_count": len(docs),
                "included_count": len(included),
                "files": [self._self_iteration_doc_summary(path) for path in included],
            },
        }

    def _self_iteration_blueprint_context(self) -> dict[str, Any]:
        path_value = self._self_iteration_blueprint_value()
        if not path_value:
            return {"path": None, "exists": False, "excerpt": ""}
        candidate = self._self_iteration_project_path(path_value)
        if candidate is None:
            return {"path": str(path_value), "exists": False, "error": "blueprint path is outside the project root"}
        payload = {
            "path": self._project_relative_path(candidate),
            "exists": candidate.exists() and candidate.is_file(),
        }
        if payload["exists"]:
            payload.update(self._text_excerpt_payload(candidate, SELF_ITERATION_CONTEXT_LIMITS["doc_excerpt_chars"]))
        return payload

    def _self_iteration_blueprint_value(self) -> str | None:
        for container_name, key in (
            ("goal", "blueprint"),
            ("generated_from", "blueprint_path"),
            ("continuation", "blueprint"),
        ):
            container = self.roadmap.get(container_name)
            if isinstance(container, dict):
                value = container.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    def _self_iteration_doc_paths(self) -> list[Path]:
        docs = self._iter_project_files(("docs",), extensions=SELF_ITERATION_DOC_EXTENSIONS)
        for readme_name in ("README.md", "README.rst", "README.txt"):
            readme = self.project_root / readme_name
            if readme.exists() and readme.is_file():
                docs.append(readme)
        unique = {self._project_relative_path(path): path for path in docs}
        return [unique[key] for key in sorted(unique)]

    def _self_iteration_doc_sort_key(self, path: Path, blueprint_path: str) -> tuple[int, str]:
        relative = self._project_relative_path(path)
        lowered = relative.lower()
        if blueprint_path and relative == blueprint_path:
            priority = 0
        elif Path(relative).name.lower().startswith("readme"):
            priority = 1
        elif any(keyword in lowered for keyword in ("blueprint", "roadmap", "planner", "plan", "contract", "design")):
            priority = 2
        else:
            priority = 3
        return (priority, relative)

    def _self_iteration_doc_summary(self, path: Path) -> dict[str, Any]:
        payload = self._text_excerpt_payload(path, SELF_ITERATION_CONTEXT_LIMITS["doc_excerpt_chars"])
        return {
            "path": self._project_relative_path(path),
            "bytes": payload.get("bytes"),
            "excerpt": payload.get("excerpt", ""),
            "excerpt_truncated": payload.get("excerpt_truncated", False),
        }

    def _self_iteration_test_inventory(self) -> dict[str, Any]:
        files = self._iter_project_files(("tests",), extensions=SELF_ITERATION_TEST_EXTENSIONS)
        included = files[: SELF_ITERATION_CONTEXT_LIMITS["test_file_count"]]
        return {
            "total_count": len(files),
            "included_count": len(included),
            "files": [self._self_iteration_test_file_summary(path) for path in included],
        }

    def _self_iteration_test_file_summary(self, path: Path) -> dict[str, Any]:
        names = self._test_names(path)
        return {
            "path": self._project_relative_path(path),
            "bytes": self._file_size(path),
            "test_count": len(names),
            "tests": names[: SELF_ITERATION_CONTEXT_LIMITS["test_name_count"]],
            "test_count_truncated": len(names) > SELF_ITERATION_CONTEXT_LIMITS["test_name_count"],
        }

    def _self_iteration_source_inventory(self) -> dict[str, Any]:
        files = self._iter_project_files(
            ("src", "app", "lib", "packages", "cli"),
            extensions=SELF_ITERATION_SOURCE_EXTENSIONS,
        )
        for path in self.project_root.iterdir():
            if not path.is_file() or path.suffix.lower() not in SELF_ITERATION_SOURCE_EXTENSIONS:
                continue
            files.append(path)
        unique = {self._project_relative_path(path): path for path in files}
        files = [unique[key] for key in sorted(unique)]
        included = files[: SELF_ITERATION_CONTEXT_LIMITS["source_file_count"]]
        return {
            "total_count": len(files),
            "included_count": len(included),
            "files": [
                {
                    "path": self._project_relative_path(path),
                    "bytes": self._file_size(path),
                }
                for path in included
            ],
        }

    def _self_iteration_git_context(self) -> dict[str, Any]:
        context: dict[str, Any] = {
            "is_repository": False,
            "root": None,
            "branch": None,
            "head": None,
            "short_head": None,
            "status": {"returncode": None, "stdout": "", "stderr": ""},
            "status_lines": [],
            "recent_commits": [],
        }
        if not self._is_git_repo():
            return context
        root = self._git(["rev-parse", "--show-toplevel"])
        head = self._git(["rev-parse", "HEAD"])
        short_head = self._git(["rev-parse", "--short", "HEAD"])
        status = self._git(["status", "--short"])
        commits = self._git(["log", "--oneline", f"-{SELF_ITERATION_CONTEXT_LIMITS['git_commit_count']}"])
        context.update(
            {
                "is_repository": True,
                "root": root["stdout"].strip() if root["returncode"] == 0 else None,
                "branch": self._current_branch(),
                "head": head["stdout"].strip() if head["returncode"] == 0 else None,
                "short_head": short_head["stdout"].strip() if short_head["returncode"] == 0 else None,
                "status": status,
                "status_lines": [line for line in status["stdout"].splitlines() if line.strip()],
                "recent_commits": [line for line in commits["stdout"].splitlines() if line.strip()],
            }
        )
        return context

    def _iter_project_files(self, roots: tuple[str, ...], *, extensions: set[str]) -> list[Path]:
        files: dict[str, Path] = {}
        for root_name in roots:
            root = self.project_root / root_name
            if root.is_file():
                candidates = [root]
            elif root.exists():
                candidates = [path for path in root.rglob("*") if path.is_file()]
            else:
                candidates = []
            for path in candidates:
                relative = path.relative_to(self.project_root)
                if any(part in PRUNE_DIRS or part == ".engineering" for part in relative.parts):
                    continue
                if path.suffix.lower() not in extensions:
                    continue
                files[str(relative)] = path
        return [files[key] for key in sorted(files)]

    def _self_iteration_project_path(self, path_value: str) -> Path | None:
        if "://" in path_value:
            return None
        path = Path(path_value)
        candidate = path if path.is_absolute() else self.project_root / path
        try:
            resolved = candidate.resolve(strict=False)
        except OSError:
            return None
        if not resolved.is_relative_to(self.project_root):
            return None
        return resolved

    def _text_excerpt_payload(self, path: Path, max_chars: int) -> dict[str, Any]:
        try:
            size = path.stat().st_size
        except OSError:
            size = None
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as handle:
                text = handle.read(max_chars + 1)
        except OSError as exc:
            return {
                "bytes": size,
                "excerpt": "",
                "excerpt_truncated": False,
                "read_error": self._truncate_text(str(exc), SELF_ITERATION_CONTEXT_LIMITS["message_chars"]),
            }
        return {
            "bytes": size,
            "excerpt": self._truncate_text(text, max_chars),
            "excerpt_truncated": len(text) > max_chars,
        }

    def _markdown_title(self, path: Path) -> str:
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as handle:
                text = handle.read(4096)
        except OSError:
            return ""
        for line in text.splitlines():
            stripped = line.strip()
            if stripped:
                return self._truncate_text(stripped.lstrip("#").strip(), 160)
        return ""

    def _test_names(self, path: Path) -> list[str]:
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as handle:
                text = handle.read(20000)
        except OSError:
            return []
        names = [
            match.group(1)
            for match in re.finditer(r"^\s*(?:async\s+)?def\s+(test_[A-Za-z0-9_]+)\s*\(", text, re.MULTILINE)
        ]
        names.extend(
            match.group(2)
            for match in re.finditer(r"^\s*(?:test|it)\s*\(\s*(['\"])(.*?)\1", text, re.MULTILINE)
        )
        return [name for name in names if name]

    def _file_size(self, path: Path) -> int | None:
        try:
            return path.stat().st_size
        except OSError:
            return None

    def _truncate_text(self, text: str, max_chars: int) -> str:
        text = redact(text)
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + "\n...[truncated]"

    def _redact_context_value(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {str(key): self._redact_context_value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._redact_context_value(item) for item in value]
        if isinstance(value, tuple):
            return [self._redact_context_value(item) for item in value]
        if isinstance(value, str):
            return redact(value)
        return value

    def _self_iteration_validation_result(
        self,
        *,
        status: str,
        errors: list[str],
        warnings: list[str],
        expected_new_stage_count: int,
        actual_new_stage_count: int,
        new_stage_ids: list[str],
        roadmap_validation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "error_count": len(errors),
            "warning_count": len(warnings),
            "errors": errors,
            "warnings": warnings,
            "expected_new_stage_count": expected_new_stage_count,
            "actual_new_stage_count": actual_new_stage_count,
            "new_stage_ids": new_stage_ids,
            "roadmap_validation": roadmap_validation,
        }

    def _validate_self_iteration_output(
        self,
        before_roadmap: dict[str, Any],
        after_roadmap: dict[str, Any],
        *,
        expected_new_stage_count: int,
    ) -> dict[str, Any]:
        errors: list[str] = []
        warnings: list[str] = []
        before_stages = self._self_iteration_continuation_stages(
            before_roadmap,
            location="previous roadmap continuation.stages",
            errors=errors,
        )
        after_stages = self._self_iteration_continuation_stages(
            after_roadmap,
            location="planner output continuation.stages",
            errors=errors,
        )
        existing_stage_count = len(before_stages)
        if len(after_stages) < existing_stage_count:
            errors.append("planner output removed existing continuation stages")
            new_stages: list[Any] = []
        else:
            existing_prefix = after_stages[:existing_stage_count]
            if existing_prefix != before_stages:
                errors.append("planner output mutated existing continuation stages; only appends are allowed")
            new_stages = after_stages[existing_stage_count:]

        if self._self_iteration_without_new_stages(after_roadmap, existing_stage_count) != before_roadmap:
            if after_roadmap.get("milestones", []) != before_roadmap.get("milestones", []):
                errors.append("planner output mutated existing milestones; only continuation.stages may be appended")
            else:
                errors.append("planner output changed existing roadmap fields; only continuation.stages may be appended")

        new_stage_ids: list[str] = []
        if len(new_stages) != expected_new_stage_count:
            errors.append(
                f"expected exactly {expected_new_stage_count} new continuation stage(s), found {len(new_stages)}"
            )

        existing_ids = self._self_iteration_existing_ids(before_roadmap)
        seen_new_ids: set[str] = set()
        materialized_stage_ids = self._self_iteration_milestone_ids(after_roadmap)
        for offset, stage in enumerate(new_stages):
            stage_index = existing_stage_count + offset
            location = f"continuation.stages[{stage_index}]"
            if not isinstance(stage, dict):
                errors.append(f"{location} must be a mapping")
                continue
            stage_id = str(stage.get("id", "")).strip()
            if not stage_id:
                errors.append(f"{location}.id is required")
            else:
                new_stage_ids.append(stage_id)
                if stage_id in existing_ids:
                    errors.append(f"new continuation stage id duplicates an existing id: {stage_id}")
                if stage_id in seen_new_ids:
                    errors.append(f"duplicate new continuation id: {stage_id}")
                if stage_id in materialized_stage_ids:
                    errors.append(f"new continuation stage `{stage_id}` was also materialized as a milestone")
                seen_new_ids.add(stage_id)
            stage_status = str(stage.get("status", "planned")).strip()
            if stage_status and stage_status not in {"planned", "pending"}:
                errors.append(f"new continuation stage `{stage_id or stage_index}` status must be planned or pending")
            self._validate_self_iteration_new_stage(
                stage,
                stage_id=stage_id or f"stage-{stage_index}",
                location=location,
                existing_ids=existing_ids,
                seen_new_ids=seen_new_ids,
                errors=errors,
                warnings=warnings,
            )

        if errors:
            return self._self_iteration_validation_result(
                status="failed",
                errors=errors,
                warnings=warnings,
                expected_new_stage_count=expected_new_stage_count,
                actual_new_stage_count=len(new_stages),
                new_stage_ids=new_stage_ids,
            )

        current_roadmap = self.roadmap
        self.roadmap = after_roadmap
        try:
            roadmap_validation = self.validate_roadmap()
        finally:
            self.roadmap = current_roadmap
        if roadmap_validation["status"] != "passed":
            errors.extend(f"roadmap validation: {error}" for error in roadmap_validation.get("errors", []))
        warnings.extend(f"roadmap validation: {warning}" for warning in roadmap_validation.get("warnings", []))
        return self._self_iteration_validation_result(
            status="passed" if not errors else "failed",
            errors=errors,
            warnings=warnings,
            expected_new_stage_count=expected_new_stage_count,
            actual_new_stage_count=len(new_stages),
            new_stage_ids=new_stage_ids,
            roadmap_validation=roadmap_validation,
        )

    def _self_iteration_continuation_stages(
        self,
        roadmap: dict[str, Any],
        *,
        location: str,
        errors: list[str],
    ) -> list[Any]:
        continuation = roadmap.get("continuation", {})
        if continuation is None:
            return []
        if not isinstance(continuation, dict):
            errors.append(f"{location} parent must be a mapping")
            return []
        stages = continuation.get("stages", [])
        if stages is None:
            return []
        if not isinstance(stages, list):
            errors.append(f"{location} must be a list")
            return []
        return stages

    def _self_iteration_without_new_stages(
        self,
        roadmap: dict[str, Any],
        existing_stage_count: int,
    ) -> dict[str, Any]:
        payload = deepcopy(roadmap)
        continuation = payload.get("continuation")
        if isinstance(continuation, dict):
            stages = continuation.get("stages", [])
            if isinstance(stages, list):
                continuation["stages"] = deepcopy(stages[:existing_stage_count])
        return payload

    def _self_iteration_milestone_ids(self, roadmap: dict[str, Any]) -> set[str]:
        milestones = roadmap.get("milestones", [])
        if not isinstance(milestones, list):
            return set()
        return {
            str(milestone.get("id", "")).strip()
            for milestone in milestones
            if isinstance(milestone, dict) and str(milestone.get("id", "")).strip()
        }

    def _self_iteration_existing_ids(self, roadmap: dict[str, Any]) -> set[str]:
        ids: set[str] = set()
        milestones = roadmap.get("milestones", [])
        if isinstance(milestones, list):
            for milestone in milestones:
                if not isinstance(milestone, dict):
                    continue
                milestone_id = str(milestone.get("id", "")).strip()
                if milestone_id:
                    ids.add(milestone_id)
                tasks = milestone.get("tasks", [])
                if isinstance(tasks, list):
                    ids.update(
                        str(task.get("id", "")).strip()
                        for task in tasks
                        if isinstance(task, dict) and str(task.get("id", "")).strip()
                    )
        continuation = roadmap.get("continuation", {})
        stages = continuation.get("stages", []) if isinstance(continuation, dict) else []
        if isinstance(stages, list):
            for stage in stages:
                if not isinstance(stage, dict):
                    continue
                stage_id = str(stage.get("id", "")).strip()
                if stage_id:
                    ids.add(stage_id)
                tasks = stage.get("tasks", [])
                if isinstance(tasks, list):
                    ids.update(
                        str(task.get("id", "")).strip()
                        for task in tasks
                        if isinstance(task, dict) and str(task.get("id", "")).strip()
                    )
        return ids

    def _validate_self_iteration_new_stage(
        self,
        stage: dict[str, Any],
        *,
        stage_id: str,
        location: str,
        existing_ids: set[str],
        seen_new_ids: set[str],
        errors: list[str],
        warnings: list[str],
    ) -> None:
        self._validate_self_iteration_unsafe_requirements(stage, location=location, errors=errors)
        tasks = stage.get("tasks", [])
        if not isinstance(tasks, list) or not tasks:
            errors.append(f"new continuation stage `{stage_id}` must define at least one task")
            return
        for task_index, task in enumerate(tasks):
            task_location = f"{location}.tasks[{task_index}]"
            if not isinstance(task, dict):
                errors.append(f"{task_location} must be a mapping")
                continue
            task_id = str(task.get("id", "")).strip()
            if not task_id:
                errors.append(f"{task_location}.id is required")
                task_id = f"{stage_id}-task-{task_index}"
            elif task_id in existing_ids:
                errors.append(f"new continuation task id duplicates an existing id: {task_id}")
            elif task_id in seen_new_ids:
                errors.append(f"duplicate new continuation task id: {task_id}")
            seen_new_ids.add(task_id)
            self._validate_self_iteration_new_task(
                task,
                task_id=task_id,
                location=task_location,
                errors=errors,
                warnings=warnings,
            )

    def _validate_self_iteration_new_task(
        self,
        task: dict[str, Any],
        *,
        task_id: str,
        location: str,
        errors: list[str],
        warnings: list[str],
    ) -> None:
        status = str(task.get("status", "pending")).strip()
        if status and status not in {"pending", "planned"}:
            errors.append(f"new continuation task `{task_id}` status must be pending or planned")

        file_scope = task.get("file_scope")
        if not isinstance(file_scope, list) or not any(str(item).strip() for item in file_scope):
            errors.append(f"new continuation task `{task_id}` must define non-empty file_scope")

        acceptance = task.get("acceptance")
        if not isinstance(acceptance, list) or not acceptance:
            errors.append(f"new continuation task `{task_id}` must define acceptance commands")
        elif not any(isinstance(item, dict) and str(item.get("command", "")).strip() for item in acceptance):
            errors.append(f"new continuation task `{task_id}` must define at least one acceptance command")

        implementation = task.get("implementation", [])
        if implementation is None:
            implementation = []
        repair = task.get("repair", [])
        if repair is None:
            repair = []
        if implementation:
            if not isinstance(implementation, list):
                errors.append(f"new continuation task `{task_id}` implementation must be a list")
            elif not self._self_iteration_has_codex_entry(implementation):
                errors.append(f"new continuation task `{task_id}` implementation work must use a codex entry")
            if not isinstance(repair, list) or not self._self_iteration_has_codex_entry(repair):
                errors.append(f"new continuation task `{task_id}` implementation work must define a codex repair entry")

        for group_name in ("implementation", "repair", "acceptance", "e2e"):
            group = task.get(group_name, [])
            if group is None:
                continue
            if not isinstance(group, list):
                continue
            for command_index, item in enumerate(group):
                if not isinstance(item, dict):
                    continue
                command = str(item.get("command", "") or "")
                if not command:
                    continue
                outcome, reason, metadata = self._command_policy_match(command)
                if outcome == "requires_approval" or metadata.get("blocked_pattern"):
                    errors.append(
                        f"new continuation task `{task_id}` {group_name}[{command_index}] has unsafe command: {reason}"
                    )
                elif outcome == "denied":
                    warnings.append(
                        f"new continuation task `{task_id}` {group_name}[{command_index}] command is not currently allowlisted: {reason}"
                    )

    def _self_iteration_has_codex_entry(self, items: list[Any]) -> bool:
        return any(
            isinstance(item, dict)
            and str(item.get("executor", "")).strip() == "codex"
            and str(item.get("prompt", "") or item.get("command", "")).strip()
            for item in items
        )

    def _validate_self_iteration_unsafe_requirements(
        self,
        value: Any,
        *,
        location: str,
        errors: list[str],
    ) -> None:
        seen: set[tuple[str, str, str]] = set()
        for text_location, text in self._self_iteration_string_values(value, location=location):
            lowered = text.lower()
            for reason, pattern in SELF_ITERATION_UNSAFE_REQUIREMENT_PATTERNS:
                for match in pattern.finditer(lowered):
                    if self._self_iteration_requirement_is_negated(lowered, match.start()):
                        continue
                    matched_text = text[match.start() : match.end()]
                    key = (text_location, reason, matched_text.lower())
                    if key in seen:
                        continue
                    seen.add(key)
                    errors.append(
                        f"{text_location} introduces unsafe {reason} requirement `{matched_text}`"
                    )

    def _self_iteration_string_values(self, value: Any, *, location: str) -> list[tuple[str, str]]:
        values: list[tuple[str, str]] = []
        if isinstance(value, dict):
            for key, item in value.items():
                values.extend(self._self_iteration_string_values(item, location=f"{location}.{key}"))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                values.extend(self._self_iteration_string_values(item, location=f"{location}[{index}]"))
        elif isinstance(value, str) and value.strip():
            values.append((location, value))
        return values

    def _self_iteration_requirement_is_negated(self, text: str, match_start: int) -> bool:
        prefix = text[max(0, match_start - 120) : match_start]
        sentence_prefix = re.split(r"[.;\n]", prefix)[-1]
        return bool(
            re.search(
                r"\b(no|not|never|without|avoid|do not|don't|must not|should not|cannot|can't)\b",
                sentence_prefix,
            )
        )

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

    def _self_iteration_prompt(self, config: dict[str, Any], snapshot_path: Path, context_path: Path) -> str:
        custom = str((config.get("planner") or {}).get("prompt", "")).strip()
        objective = str(config.get("objective", "Assess current project status and plan the next engineering stage."))
        max_stages = int(config.get("max_stages_per_iteration", 1))
        base = f"""
You are the self-iteration planner for an autonomous engineering harness.

Project root: {self.project_root}
Roadmap file: {self.roadmap_path}
Status snapshot: {snapshot_path}
Planner context pack: {context_path}
Objective: {objective}

Read the bounded JSON planner context pack first. It summarizes the roadmap, continuation state,
recent manifests and reports, blueprint/docs excerpts, test and source inventories, git status, and
recent commits so you can assess current state without ad hoc repository discovery. Use the roadmap
file and status snapshot only when you need to verify or write the final roadmap diff. Append exactly
{max_stages} new unmaterialized stage(s) to `continuation.stages` in the roadmap file.

Rules:
- Do not edit `.engineering/state` or `.engineering/reports`.
- Do not mark tasks done and do not add generated stages to `milestones`.
- Existing roadmap fields, milestones, tasks, statuses, and continuation stages must remain unchanged.
- New stages must be concrete, measurable, and automatable.
- Each new task must include non-empty `file_scope` and local acceptance commands.
- If code must be written, use an `implementation` entry with `"executor": "codex"` and a focused prompt.
- Include a `repair` entry with `"executor": "codex"` for every task that has implementation work.
- Do not require live operations, private keys, mainnet writes, production deployments, paid services, or external accounts.
- Prefer the next step that moves the project toward the stated blueprint and vision.
- Keep scope tight enough that a coding agent can complete the stage in one iteration.
Planner output is accepted only if validation can prove that exactly {max_stages} safe, unmaterialized
continuation stage(s) were appended.
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
        validation: dict[str, Any] | None = None,
        context_pack: dict[str, Any] | None = None,
    ) -> None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# Self Iteration Report",
            "",
            f"- Reason: `{reason}`",
            f"- Snapshot: `{snapshot_path.relative_to(self.project_root)}`",
        ]
        if context_pack is not None:
            lines.append(f"- Context pack: `{context_pack.get('path')}`")
        lines.extend(
            [
                f"- Before stages: `{before.get('stage_count')}` pending `{before.get('pending_stage_count')}`",
                f"- After stages: `{after.get('stage_count')}` pending `{after.get('pending_stage_count')}`",
                "",
            ]
        )
        if context_pack is not None:
            lines.extend(
                [
                    "## Planner Context Pack",
                    "",
                    "```json",
                    json.dumps(context_pack.get("summary", {}), indent=2, sort_keys=True),
                    "```",
                    "",
                ]
            )
        lines.extend(
            [
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
        )
        if validation is not None:
            lines.extend(
                [
                    "## Output Validation",
                    "",
                    f"- Status: `{validation.get('status')}`",
                    f"- Expected new stages: `{validation.get('expected_new_stage_count')}`",
                    f"- Actual new stages: `{validation.get('actual_new_stage_count')}`",
                    f"- New stage ids: `{', '.join(validation.get('new_stage_ids') or [])}`",
                    f"- Errors: `{validation.get('error_count', 0)}`",
                    f"- Warnings: `{validation.get('warning_count', 0)}`",
                    "",
                ]
            )
            if validation.get("errors"):
                lines.extend(["Errors:", ""])
                lines.extend(f"- {error}" for error in validation.get("errors", []))
                lines.append("")
            if validation.get("warnings"):
                lines.extend(["Warnings:", ""])
                lines.extend(f"- {warning}" for warning in validation.get("warnings", []))
                lines.append("")
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
                    "e2e": task.get("e2e", []),
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
            "drive_control": self.drive_control_summary(),
            "approval_queue": self.approval_queue_summary(status_filter="pending"),
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
            "policy_decision_summary": index.get("policy_decision_summary", {}),
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
        policy_decision_summary = self._aggregate_policy_decision_summaries(manifests)
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
            "policy_decision_summary": policy_decision_summary,
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
        policy_decisions = manifest.get("policy_decisions") if isinstance(manifest.get("policy_decisions"), list) else []
        policy_decision_summary = (
            manifest.get("policy_decision_summary")
            if isinstance(manifest.get("policy_decision_summary"), dict)
            else self._policy_decision_summary(policy_decisions)
        )
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
            "policy_decision_summary": policy_decision_summary,
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

    def _aggregate_policy_decision_summaries(self, manifests: list[dict[str, Any]]) -> dict[str, Any]:
        by_kind: dict[str, int] = {}
        by_outcome: dict[str, int] = {}
        by_effect: dict[str, int] = {}
        by_severity: dict[str, int] = {}
        total = 0
        blocking: list[dict[str, Any]] = []
        requires_approval: list[dict[str, Any]] = []
        for manifest in manifests:
            summary = manifest.get("policy_decision_summary")
            if not isinstance(summary, dict):
                continue
            total += int(summary.get("total") or 0)
            for source, target in (
                (summary.get("by_kind"), by_kind),
                (summary.get("by_outcome"), by_outcome),
                (summary.get("by_effect"), by_effect),
                (summary.get("by_severity"), by_severity),
            ):
                if not isinstance(source, dict):
                    continue
                for key, value in source.items():
                    target[str(key)] = target.get(str(key), 0) + int(value or 0)
            for decision in summary.get("blocking", []):
                if isinstance(decision, dict):
                    blocking.append(self._manifest_policy_summary_item(manifest, decision))
            for decision in summary.get("requires_approval", []):
                if isinstance(decision, dict):
                    requires_approval.append(self._manifest_policy_summary_item(manifest, decision))
        return {
            "total": total,
            "by_kind": dict(sorted(by_kind.items())),
            "by_outcome": dict(sorted(by_outcome.items())),
            "by_effect": dict(sorted(by_effect.items())),
            "by_severity": dict(sorted(by_severity.items())),
            "blocking": blocking,
            "requires_approval": requires_approval,
        }

    def _manifest_policy_summary_item(self, manifest: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
        return {
            "manifest_path": manifest.get("manifest_path"),
            "task_id": manifest.get("task_id"),
            "manifest_status": manifest.get("status"),
            **decision,
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

    def _policy_input(
        self,
        task: HarnessTask,
        *,
        phase: str = "task",
        command: AcceptanceCommand | None = None,
        safety: dict[str, Any] | None = None,
        git_context: dict[str, Any] | None = None,
        allow_live: bool = False,
        allow_manual: bool = False,
        allow_agent: bool = False,
        executor_metadata: dict[str, Any] | None = None,
    ) -> PolicyInput:
        safety = safety or {}
        git_context = git_context or self._git_context(safety)
        git_preflight = safety.get("git_preflight", {})
        file_scope_guard = safety.get("file_scope_guard", {})
        executor_contract = executor_metadata
        if executor_contract is None and command is not None:
            executor_contract = self.executor_registry.metadata_for(command.executor)
        command_payload: dict[str, Any] | None = None
        live_matches: list[str] = []
        if command is not None:
            live_matches = self._live_policy_matches(command.command)
            command_payload = {
                "name": command.name,
                "command": command.command,
                "prompt": command.prompt,
                "required": command.required,
                "timeout_seconds": command.timeout_seconds,
                "model": command.model,
                "sandbox": command.sandbox,
                "executor": command.executor,
            }
        roadmap_path = (
            str(self.roadmap_path.relative_to(self.project_root))
            if self.roadmap_path and self.roadmap_path.is_relative_to(self.project_root)
            else str(self.roadmap_path)
        )
        return PolicyInput(
            project={
                "name": str(self.roadmap.get("project", self.project_root.name)),
                "root": str(self.project_root),
                "profile": self.roadmap.get("profile"),
                "roadmap_path": roadmap_path,
            },
            task={
                "id": task.id,
                "title": task.title,
                "milestone_id": task.milestone_id,
                "milestone_title": task.milestone_title,
                "status": task.status,
                "manual_approval_required": task.manual_approval_required,
                "agent_approval_required": task.agent_approval_required,
                "max_task_iterations": task.max_task_iterations,
            },
            phase=phase,
            command=command_payload,
            executor=executor_contract,
            git={
                "is_repository": git_context.get("is_repository", False),
                "root": git_context.get("root"),
                "branch": git_context.get("branch"),
                "head": git_context.get("head"),
                "short_head": git_context.get("short_head"),
                "refs": git_context.get("refs", {}),
            },
            worktree={
                "git_preflight_status": git_preflight.get("status", "unknown"),
                "git_preflight_message": git_preflight.get("message", ""),
                "file_scope_guard_status": file_scope_guard.get("status", "unknown"),
                "file_scope_guard_message": file_scope_guard.get("message", ""),
                "dirty_before_paths": git_preflight.get("dirty_before_paths", []),
                "dirty_after_paths": file_scope_guard.get("dirty_after_paths", []),
                "dirty_before_out_of_scope_paths": git_preflight.get("dirty_before_out_of_scope_paths", []),
                "new_dirty_paths": file_scope_guard.get("new_dirty_paths", []),
                "changed_preexisting_dirty_paths": file_scope_guard.get("changed_preexisting_dirty_paths", []),
                "new_or_changed_dirty_paths": file_scope_guard.get("new_or_changed_dirty_paths", []),
                "file_scope_violations": file_scope_guard.get("violations", []),
                "status_short": git_context.get("status_short", ""),
            },
            file_scope={
                "patterns": list(task.file_scope),
                "dirty_before_out_of_scope_paths": git_preflight.get("dirty_before_out_of_scope_paths", []),
                "violations": file_scope_guard.get("violations", []),
            },
            approvals={
                "allow_manual": allow_manual,
                "allow_agent": allow_agent,
                "manual_required": task.manual_approval_required,
                "agent_required": task.agent_approval_required,
                "executor_agent_required": bool((executor_contract or {}).get("requires_agent_approval")),
            },
            live={
                "allow_live": allow_live,
                "requires_live_flag_patterns": list(self.command_policy.get("requires_live_flag_patterns", [])),
                "matched_patterns": live_matches,
                "detected": bool(live_matches),
            },
            context={
                "policy_profile": self.command_policy.get("profile") or self.roadmap.get("profile"),
                "command_policy_version": self.command_policy.get("version"),
                "default_timeout_seconds": self.default_timeout,
            },
        )

    def _live_policy_matches(self, command: str | None) -> list[str]:
        if command is None:
            return []
        stripped = command.strip()
        return [
            str(pattern)
            for pattern in self.command_policy.get("requires_live_flag_patterns", [])
            if str(pattern) in stripped
        ]

    def _command_policy_match(self, command: str | None, *, allow_live: bool = False) -> tuple[str, str, dict[str, Any]]:
        if command is None:
            return "denied", "shell command is missing", {}
        stripped = command.strip()
        for pattern in self.command_policy.get("blocked_patterns", []):
            if pattern in stripped:
                return "denied", f"blocked pattern matched: {pattern}", {"blocked_pattern": pattern}
        live_matches = self._live_policy_matches(command)
        if live_matches and not allow_live:
            return (
                "requires_approval",
                f"live command requires --allow-live: {live_matches[0]}",
                {"approval_flag": "--allow-live", "matched_live_patterns": live_matches},
            )
        prefixes = tuple(str(prefix) for prefix in self.command_policy.get("allowed_prefixes", []))
        if prefixes and not stripped.startswith(prefixes):
            return "denied", "command prefix is not allowlisted", {"allowed_prefixes": list(prefixes)}
        return "allowed", "allowed", {}

    def command_allowed(self, command: str | None, allow_live: bool = False) -> tuple[bool, str]:
        outcome, reason, _metadata = self._command_policy_match(command, allow_live=allow_live)
        return outcome == "allowed", reason

    def _approval_policy_decision(
        self,
        policy_input: PolicyInput,
        *,
        kind: str,
        required_key: str,
        allowed_key: str,
        label: str,
        approval_flag: str,
    ) -> PolicyDecision:
        required = bool(policy_input.approvals.get(required_key))
        allowed = bool(policy_input.approvals.get(allowed_key))
        if not required:
            return PolicyDecision(
                kind=kind,
                scope="task",
                outcome="allowed",
                effect="allow",
                severity="info",
                reason=f"{label} not required",
                policy_input=policy_input,
            )
        if allowed:
            return PolicyDecision(
                kind=kind,
                scope="task",
                outcome="allowed",
                effect="allow",
                severity="info",
                reason=f"{label} satisfied",
                policy_input=policy_input,
                approval_flag=approval_flag,
            )
        return PolicyDecision(
            kind=kind,
            scope="task",
            outcome="requires_approval",
            effect="requires_approval",
            severity="approval",
            reason=f"{label} required",
            policy_input=policy_input,
            requires_approval=True,
            approval_flag=approval_flag,
        )

    def _manual_approval_decision(self, policy_input: PolicyInput) -> PolicyDecision:
        return self._approval_policy_decision(
            policy_input,
            kind="manual_approval",
            required_key="manual_required",
            allowed_key="allow_manual",
            label="manual approval",
            approval_flag="--allow-manual",
        )

    def _agent_approval_decision(self, policy_input: PolicyInput) -> PolicyDecision:
        return self._approval_policy_decision(
            policy_input,
            kind="agent_approval",
            required_key="agent_required",
            allowed_key="allow_agent",
            label="agent approval",
            approval_flag="--allow-agent",
        )

    def _executor_policy_decision(self, policy_input: PolicyInput) -> PolicyDecision:
        command = policy_input.command or {}
        executor = policy_input.executor or {}
        executor_id = str(command.get("executor") or executor.get("id") or "")
        if not executor_id or executor.get("kind") == "unknown":
            return PolicyDecision(
                kind="executor_policy",
                scope="command",
                outcome="denied",
                effect="deny",
                severity="error",
                reason=f"unknown executor: {executor_id}",
                policy_input=policy_input,
                status="unknown",
            )
        return PolicyDecision(
            kind="executor_policy",
            scope="command",
            outcome="allowed",
            effect="allow",
            severity="info",
            reason="registered executor",
            policy_input=policy_input,
        )

    def _executor_approval_decision(self, policy_input: PolicyInput) -> PolicyDecision:
        command = policy_input.command or {}
        executor = policy_input.executor or {}
        executor_id = str(command.get("executor") or executor.get("id") or "")
        if not executor_id or executor.get("kind") == "unknown":
            return PolicyDecision(
                kind="executor_approval",
                scope="command",
                outcome="warning",
                effect="warn",
                severity="warning",
                reason=f"executor approval not evaluated for unknown executor: {executor_id}",
                policy_input=policy_input,
                status="unknown",
            )
        if executor.get("requires_agent_approval"):
            if not policy_input.approvals.get("allow_agent"):
                return PolicyDecision(
                    kind="executor_approval",
                    scope="command",
                    outcome="requires_approval",
                    effect="requires_approval",
                    severity="approval",
                    reason=f"{executor_id} executor requires --allow-agent",
                    policy_input=policy_input,
                    requires_approval=True,
                    approval_flag="--allow-agent",
                )
            return PolicyDecision(
                kind="executor_approval",
                scope="command",
                outcome="allowed",
                effect="allow",
                severity="info",
                reason="agent approval satisfied",
                policy_input=policy_input,
                approval_flag="--allow-agent",
            )
        return PolicyDecision(
            kind="executor_approval",
            scope="command",
            outcome="allowed",
            effect="allow",
            severity="info",
            reason="executor approval not required",
            policy_input=policy_input,
        )

    def _command_policy_decision(self, policy_input: PolicyInput) -> PolicyDecision:
        command = policy_input.command or {}
        outcome, reason, metadata = self._command_policy_match(
            command.get("command"),
            allow_live=bool(policy_input.live.get("allow_live")),
        )
        if outcome == "allowed":
            return PolicyDecision(
                kind="command_policy",
                scope="command",
                outcome="allowed",
                effect="allow",
                severity="info",
                reason=reason,
                policy_input=policy_input,
            )
        if outcome == "requires_approval":
            return PolicyDecision(
                kind="command_policy",
                scope="command",
                outcome="requires_approval",
                effect="requires_approval",
                severity="approval",
                reason=reason,
                policy_input=policy_input,
                requires_approval=True,
                approval_flag=str(metadata.get("approval_flag", "--allow-live")),
                metadata=metadata,
            )
        return PolicyDecision(
            kind="command_policy",
            scope="command",
            outcome="denied",
            effect="deny",
            severity="error",
            reason=reason,
            policy_input=policy_input,
            metadata=metadata,
        )

    def _live_approval_decision(self, policy_input: PolicyInput) -> PolicyDecision:
        live = policy_input.live or {}
        matches = list(live.get("matched_patterns", []))
        if not live.get("detected"):
            return PolicyDecision(
                kind="live_approval",
                scope="command",
                outcome="allowed",
                effect="allow",
                severity="info",
                reason="live approval not required",
                policy_input=policy_input,
            )
        metadata = {"matched_live_patterns": matches}
        if live.get("allow_live"):
            return PolicyDecision(
                kind="live_approval",
                scope="command",
                outcome="allowed",
                effect="allow",
                severity="info",
                reason="live approval satisfied",
                policy_input=policy_input,
                approval_flag="--allow-live",
                metadata=metadata,
            )
        return PolicyDecision(
            kind="live_approval",
            scope="command",
            outcome="requires_approval",
            effect="requires_approval",
            severity="approval",
            reason="live command requires --allow-live",
            policy_input=policy_input,
            requires_approval=True,
            approval_flag="--allow-live",
            metadata=metadata,
        )

    def _git_preflight_decision(self, policy_input: PolicyInput) -> PolicyDecision:
        status = str(policy_input.worktree.get("git_preflight_status", "unknown"))
        reason = str(policy_input.worktree.get("git_preflight_message", ""))
        if status == "clean":
            return PolicyDecision(
                kind="git_preflight",
                scope="worktree",
                outcome="allowed",
                effect="allow",
                severity="info",
                reason=reason,
                policy_input=policy_input,
                status=status,
            )
        return PolicyDecision(
            kind="git_preflight",
            scope="worktree",
            outcome="warning",
            effect="warn",
            severity="warning",
            reason=reason,
            policy_input=policy_input,
            status=status,
            metadata={
                "dirty_before_paths": policy_input.worktree.get("dirty_before_paths", []),
                "dirty_before_out_of_scope_paths": policy_input.worktree.get("dirty_before_out_of_scope_paths", []),
            },
        )

    def _file_scope_decision(self, policy_input: PolicyInput) -> PolicyDecision:
        status = str(policy_input.worktree.get("file_scope_guard_status", "unknown"))
        reason = str(policy_input.worktree.get("file_scope_guard_message", ""))
        if status == "failed":
            return PolicyDecision(
                kind="file_scope_guard",
                scope="worktree",
                outcome="denied",
                effect="deny",
                severity="error",
                reason=reason,
                policy_input=policy_input,
                status=status,
                metadata={"violations": policy_input.file_scope.get("violations", [])},
            )
        if status == "passed":
            return PolicyDecision(
                kind="file_scope_guard",
                scope="worktree",
                outcome="allowed",
                effect="allow",
                severity="info",
                reason=reason,
                policy_input=policy_input,
                status=status,
            )
        return PolicyDecision(
            kind="file_scope_guard",
            scope="worktree",
            outcome="warning",
            effect="warn",
            severity="warning",
            reason=reason,
            policy_input=policy_input,
            status=status,
        )

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
        effective_allow_manual = allow_manual or self._approval_is_approved(
            task,
            decision_kind="manual_approval",
            phase="task",
        )
        effective_allow_agent = allow_agent or self._approval_is_approved(
            task,
            decision_kind="agent_approval",
            phase="task",
        )

        manual_decision = self._manual_approval_decision(
            self._policy_input(
                task,
                safety=safety,
                allow_live=allow_live,
                allow_manual=effective_allow_manual,
                allow_agent=effective_allow_agent,
            )
        )
        if manual_decision.blocks_execution():
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
                allow_manual=effective_allow_manual,
                allow_agent=effective_allow_agent,
            )
        agent_decision = self._agent_approval_decision(
            self._policy_input(
                task,
                safety=safety,
                allow_live=allow_live,
                allow_manual=effective_allow_manual,
                allow_agent=effective_allow_agent,
            )
        )
        if agent_decision.blocks_execution():
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
                allow_manual=effective_allow_manual,
                allow_agent=effective_allow_agent,
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
                allow_manual=effective_allow_manual,
                allow_agent=effective_allow_agent,
            )

        runs: list[CommandRun] = []
        implementation_status, message = self._run_command_group(
            task.implementation,
            phase="implementation",
            runs=runs,
            dry_run=dry_run,
            allow_live=allow_live,
            allow_manual=effective_allow_manual,
            allow_agent=effective_allow_agent,
            task=task,
            state=state,
            persist_state=not dry_run,
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
                    allow_manual=effective_allow_manual,
                    allow_agent=effective_allow_agent,
                    task=task,
                    state=state,
                    persist_state=not dry_run,
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
                    allow_manual=effective_allow_manual,
                    allow_agent=effective_allow_agent,
                    task=task,
                    state=state,
                    persist_state=not dry_run,
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
                allow_manual=effective_allow_manual,
                allow_agent=effective_allow_agent,
                task=task,
                state=state,
                persist_state=not dry_run,
            )
            overall_status = e2e_status
            if e2e_status == "passed":
                message = "All required acceptance and e2e commands passed."

        if not dry_run:
            self._record_phase_state(
                state,
                task,
                phase="file-scope-guard",
                event="before",
                status="running",
                persist=True,
                metadata={
                    "file_scope": list(task.file_scope),
                    "dirty_before_paths": list(safety["git_preflight"].get("dirty_before_paths", [])),
                },
            )
            safety["file_scope_guard"] = self._file_scope_guard(
                task,
                dirty_before_paths=list(safety["git_preflight"].get("dirty_before_paths", [])),
                dirty_before_fingerprints=dict(safety["git_preflight"].get("dirty_before_fingerprints", {})),
            )
            task_state["last_dirty_after_paths"] = safety["file_scope_guard"].get("dirty_after_paths", [])
            task_state["last_new_dirty_paths"] = safety["file_scope_guard"].get("new_dirty_paths", [])
            task_state["last_file_scope_violations"] = safety["file_scope_guard"].get("violations", [])
            file_scope_decision = self._file_scope_decision(
                self._policy_input(
                    task,
                    safety=safety,
                    allow_live=allow_live,
                    allow_manual=effective_allow_manual,
                    allow_agent=effective_allow_agent,
                )
            )
            if overall_status == "passed" and file_scope_decision.blocks_execution():
                overall_status = "failed"
                message = safety["file_scope_guard"]["message"]
            self._record_phase_state(
                state,
                task,
                phase="file-scope-guard",
                event="after",
                status=str(safety["file_scope_guard"].get("status", "unknown")),
                message=str(safety["file_scope_guard"].get("message", "")),
                persist=True,
                metadata={
                    "dirty_after_paths": safety["file_scope_guard"].get("dirty_after_paths", []),
                    "new_dirty_paths": safety["file_scope_guard"].get("new_dirty_paths", []),
                    "changed_preexisting_dirty_paths": safety["file_scope_guard"].get(
                        "changed_preexisting_dirty_paths",
                        [],
                    ),
                    "new_or_changed_dirty_paths": safety["file_scope_guard"].get("new_or_changed_dirty_paths", []),
                    "violations": safety["file_scope_guard"].get("violations", []),
                },
            )

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
            allow_manual=effective_allow_manual,
            allow_agent=effective_allow_agent,
        )

    def _run_command_group(
        self,
        commands: tuple[AcceptanceCommand, ...],
        *,
        phase: str,
        runs: list[CommandRun],
        dry_run: bool,
        allow_live: bool,
        allow_manual: bool,
        allow_agent: bool,
        task: HarnessTask,
        state: dict[str, Any] | None = None,
        persist_state: bool = False,
    ) -> tuple[str, str]:
        state_payload = state if state is not None else {}
        self._record_phase_state(
            state_payload,
            task,
            phase=phase,
            event="before",
            status="running",
            persist=persist_state,
            metadata=self._command_group_state_metadata(commands),
        )
        run_start_index = len(runs)

        def finish(status: str, message: str) -> tuple[str, str]:
            phase_runs = runs[run_start_index:]
            self._record_phase_state(
                state_payload,
                task,
                phase=phase,
                event="after",
                status=status,
                message=message,
                persist=persist_state,
                metadata={
                    "command_count": len(commands),
                    "run_count": len(phase_runs),
                    "required_failures": [
                        run.name
                        for run in phase_runs
                        if any(command.name == run.name and command.required for command in commands)
                        and (run.status == "blocked" or run.returncode != 0)
                    ],
                },
                runs=phase_runs,
            )
            return status, message

        if not commands:
            return finish("passed", f"No {phase} commands configured.")
        for command in commands:
            approval_phase = self._approval_phase_key(phase)
            command_allow_live = allow_live or self._approval_is_approved(
                task,
                decision_kind="live_approval",
                phase=approval_phase,
                name=command.name,
                executor=command.executor,
            )
            command_allow_agent = allow_agent or self._approval_is_approved(
                task,
                decision_kind="executor_approval",
                phase=approval_phase,
                name=command.name,
                executor=command.executor,
            )
            executor_metadata = self.executor_registry.metadata_for(command.executor)
            policy_input = self._policy_input(
                task,
                phase=phase,
                command=command,
                allow_live=command_allow_live,
                allow_manual=allow_manual,
                allow_agent=command_allow_agent,
                executor_metadata=executor_metadata,
            )
            executor_decision = self._executor_policy_decision(policy_input)
            executor = self.executor_registry.get(command.executor)
            if executor_decision.blocks_execution():
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
                        executor_decision.reason,
                        executor=command.executor,
                        executor_metadata=executor_metadata,
                    )
                )
                return finish("blocked", executor_decision.reason)
            executor_approval_decision = self._executor_approval_decision(policy_input)
            if executor_approval_decision.blocks_execution():
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
                        executor_approval_decision.reason,
                        executor=command.executor,
                        executor_metadata=executor_metadata,
                    )
                )
                return finish("blocked", executor_approval_decision.reason)
            if executor is None:
                return finish("blocked", executor_decision.reason)
            if executor.metadata.uses_command_policy:
                command_decision = self._command_policy_decision(policy_input)
                if command_decision.blocks_execution():
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
                            command_decision.reason,
                            executor=command.executor,
                            executor_metadata=executor.metadata.as_contract(),
                        )
                    )
                    return finish("blocked", command_decision.reason)
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
                return finish("blocked", run.stderr or f"Required {phase} command blocked: {command.name}")
            if command.required and run.returncode != 0:
                return finish("failed", f"Required {phase} command failed: {command.name}")
        return finish("passed", f"All required {phase} commands passed.")

    def git_checkpoint(
        self,
        task: HarnessTask,
        *,
        push: bool = False,
        remote: str = "origin",
        branch: str | None = None,
        message_template: str = "chore(engineering): complete {task_id}",
    ) -> dict[str, Any]:
        state = self.load_state()
        self._record_phase_state(
            state,
            task,
            phase="checkpoint-intent",
            event="before",
            status="running",
            persist=True,
            metadata={
                "push": push,
                "remote": remote,
                "branch": branch,
                "message_template": message_template,
            },
        )

        def finish(payload: dict[str, Any]) -> dict[str, Any]:
            self._record_phase_state(
                state,
                task,
                phase="checkpoint-intent",
                event="after",
                status=str(payload.get("status", "unknown")),
                message=str(payload.get("message", "")),
                persist=True,
                metadata={
                    key: value
                    for key, value in payload.items()
                    if key
                    not in {
                        "stdout",
                        "stderr",
                        "push_stdout",
                        "push_stderr",
                    }
                },
            )
            return payload

        if not self._is_git_repo():
            return finish({"status": "skipped", "message": "project root is not inside a git repository"})

        task_state = state.get("tasks", {}).get(task.id, {})
        dirty_before = [str(path) for path in task_state.get("last_dirty_before_paths", [])]
        if dirty_before:
            return finish({
                "status": "skipped",
                "message": "dirty worktree existed before the task; refusing to checkpoint mixed changes",
                "dirty_before_paths": dirty_before,
            })

        status_before = self._git(["status", "--porcelain"])
        if status_before["returncode"] != 0:
            return finish({
                "status": "failed",
                "message": "could not inspect git status",
                "stderr": status_before["stderr"],
            })
        current_paths = self._git_status_paths()
        scope_violations = self._scope_violations(current_paths, task.file_scope)
        if scope_violations:
            return finish({
                "status": "skipped",
                "message": "dirty files are outside task file_scope; refusing checkpoint",
                "violations": scope_violations,
            })
        if not status_before["stdout"].strip():
            return finish({"status": "skipped", "message": "no git changes to commit"})

        add_result = self._git(["add", "-A", "--", "."])
        if add_result["returncode"] != 0:
            return finish({"status": "failed", "message": "git add failed", "stderr": add_result["stderr"]})

        staged = self._git(["diff", "--cached", "--quiet"])
        if staged["returncode"] == 0:
            return finish({"status": "skipped", "message": "no staged git changes to commit"})
        if staged["returncode"] not in (0, 1):
            return finish(
                {"status": "failed", "message": "could not inspect staged git diff", "stderr": staged["stderr"]}
            )

        message = message_template.format(
            task_id=task.id,
            task_title=task.title,
            milestone_id=task.milestone_id,
            milestone_title=task.milestone_title,
        )
        commit_result = self._git(["commit", "-m", message])
        if commit_result["returncode"] != 0:
            return finish({"status": "failed", "message": "git commit failed", "stderr": commit_result["stderr"]})

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
                return finish(payload)
            push_result = self._git(["push", remote, f"HEAD:{target_branch}"])
            payload["push_status"] = "pushed" if push_result["returncode"] == 0 else "failed"
            payload["push_remote"] = remote
            payload["push_branch"] = target_branch
            payload["push_stdout"] = push_result["stdout"]
            payload["push_stderr"] = push_result["stderr"]
            if push_result["returncode"] != 0:
                payload["status"] = "failed"
        return finish(payload)

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
        safety_payload = safety or {}
        git_context = self._git_context(safety_payload)
        policy_input = self._policy_input(
            task,
            safety=safety_payload,
            git_context=git_context,
            allow_live=allow_live,
            allow_manual=allow_manual,
            allow_agent=allow_agent,
        )
        policy_decisions = self._policy_decisions(
            task,
            safety=safety_payload,
            git_context=git_context,
            allow_live=allow_live,
            allow_manual=allow_manual,
            allow_agent=allow_agent,
        )
        policy_decision_summary = self._policy_decision_summary(policy_decisions)
        report_relative = str(report_path.relative_to(self.project_root))
        manifest_relative = str(manifest_path.relative_to(self.project_root))
        self._record_phase_state(
            state,
            task,
            phase="manifest-writing",
            event="before",
            status="running",
            persist=persist,
            metadata={
                "report_path": report_relative,
                "manifest_path": manifest_relative,
                "run_count": len(runs),
                "result_status": status,
            },
        )
        self._write_report(
            report_path,
            task,
            started_at,
            finished_at,
            runs,
            status,
            message,
            safety=safety_payload,
            policy_decisions=policy_decisions,
            policy_decision_summary=policy_decision_summary,
        )
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
            git_context=git_context,
            policy_input=policy_input.as_contract(),
            policy_decisions=policy_decisions,
            policy_decision_summary=policy_decision_summary,
        )
        self._record_phase_state(
            state,
            task,
            phase="manifest-writing",
            event="after",
            status="passed",
            message="task report and manifest were written",
            persist=persist,
            metadata={
                "report_path": report_relative,
                "manifest_path": manifest_relative,
                "report_exists": report_path.exists(),
                "manifest_exists": manifest_path.exists(),
            },
        )
        self.rebuild_manifest_index()
        if persist:
            queued_approvals = self._queue_required_approvals(state, task, policy_decisions)
            if status in COMPLETED_STATUSES:
                self._consume_task_approvals(state, task, status=status)
            approval_blocked = status == "blocked" and bool(queued_approvals)
            self._record_phase_state(
                state,
                task,
                phase="final-result",
                event="before",
                status="running",
                persist=True,
                metadata={
                    "status": status,
                    "message": message,
                    "report_path": report_relative,
                    "manifest_path": manifest_relative,
                },
            )
            task_state = state.setdefault("tasks", {}).setdefault(task.id, {})
            task_state["status"] = status
            if approval_blocked:
                task_state["blocked_on_approval"] = True
            task_state["last_finished_at"] = finished_at
            task_state["last_report"] = report_relative
            task_state["last_manifest"] = manifest_relative
            self._record_phase_state(
                state,
                task,
                phase="final-result",
                event="after",
                status=status,
                message=message,
                persist=True,
                metadata={
                    "report_path": report_relative,
                    "manifest_path": manifest_relative,
                    "run_count": len(runs),
                },
            )
            if approval_blocked:
                state.setdefault("tasks", {}).setdefault(task.id, {})["attempts"] = max(
                    0,
                    int(state.get("tasks", {}).get(task.id, {}).get("attempts", 0)) - 1,
                )
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
        git_context: dict[str, Any] | None = None,
        policy_input: dict[str, Any] | None = None,
        policy_decisions: list[dict[str, Any]] | None = None,
        policy_decision_summary: dict[str, Any] | None = None,
    ) -> None:
        manifest_relative = str(manifest_path.relative_to(self.project_root))
        report_relative = str(report_path.relative_to(self.project_root))
        safety_payload = safety or {}
        git_payload = git_context or self._git_context(safety_payload)
        policy_input_payload = policy_input or self._policy_input(
            task,
            safety=safety_payload,
            git_context=git_payload,
            allow_live=allow_live,
            allow_manual=allow_manual,
            allow_agent=allow_agent,
        ).as_contract()
        policy_decision_payload = policy_decisions or self._policy_decisions(
            task,
            safety=safety_payload,
            git_context=git_payload,
            allow_live=allow_live,
            allow_manual=allow_manual,
            allow_agent=allow_agent,
        )
        policy_decision_summary_payload = policy_decision_summary or self._policy_decision_summary(
            policy_decision_payload
        )
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
            "safety": safety_payload,
            "policy_input": policy_input_payload,
            "policy_decisions": policy_decision_payload,
            "policy_decision_summary": policy_decision_summary_payload,
            "git": git_payload,
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
        git_context: dict[str, Any] | None = None,
        allow_live: bool,
        allow_manual: bool,
        allow_agent: bool,
    ) -> list[dict[str, Any]]:
        task_allow_manual = allow_manual or self._approval_is_approved(
            task,
            decision_kind="manual_approval",
            phase="task",
        )
        task_allow_agent = allow_agent or self._approval_is_approved(
            task,
            decision_kind="agent_approval",
            phase="task",
        )
        base_input = self._policy_input(
            task,
            safety=safety,
            git_context=git_context,
            allow_live=allow_live,
            allow_manual=task_allow_manual,
            allow_agent=task_allow_agent,
        )
        decisions: list[PolicyDecision] = [
            self._manual_approval_decision(base_input),
            self._agent_approval_decision(base_input),
        ]
        for phase, commands in (
            ("implementation", task.implementation),
            ("repair", task.repair),
            ("acceptance", task.acceptance),
            ("e2e", task.e2e),
        ):
            for command in commands:
                command_allow_live = allow_live or self._approval_is_approved(
                    task,
                    decision_kind="live_approval",
                    phase=phase,
                    name=command.name,
                    executor=command.executor,
                )
                command_allow_agent = task_allow_agent or self._approval_is_approved(
                    task,
                    decision_kind="executor_approval",
                    phase=phase,
                    name=command.name,
                    executor=command.executor,
                )
                executor_metadata = self.executor_registry.metadata_for(command.executor)
                command_input = self._policy_input(
                    task,
                    phase=phase,
                    command=command,
                    safety=safety,
                    git_context=git_context,
                    allow_live=command_allow_live,
                    allow_manual=task_allow_manual,
                    allow_agent=command_allow_agent,
                    executor_metadata=executor_metadata,
                )
                executor_decision = self._executor_policy_decision(command_input)
                decisions.append(executor_decision)
                if not executor_decision.blocks_execution():
                    decisions.append(self._executor_approval_decision(command_input))
                executor = self.executor_registry.get(command.executor)
                if executor is not None and executor.metadata.uses_command_policy:
                    decisions.append(self._command_policy_decision(command_input))
                    decisions.append(self._live_approval_decision(command_input))

        decisions.extend(
            [
                self._git_preflight_decision(base_input),
                self._file_scope_decision(base_input),
            ]
        )
        return [decision.as_contract() for decision in decisions]

    def _policy_decision_summary(self, decisions: list[dict[str, Any]]) -> dict[str, Any]:
        by_kind: dict[str, int] = {}
        by_outcome: dict[str, int] = {}
        by_effect: dict[str, int] = {}
        by_severity: dict[str, int] = {}
        blocking: list[dict[str, Any]] = []
        requires_approval: list[dict[str, Any]] = []
        for decision in decisions:
            kind = str(decision.get("kind") or "unknown")
            outcome = str(decision.get("outcome") or "unknown")
            effect = str(decision.get("effect") or "unknown")
            severity = str(decision.get("severity") or "unknown")
            by_kind[kind] = by_kind.get(kind, 0) + 1
            by_outcome[outcome] = by_outcome.get(outcome, 0) + 1
            by_effect[effect] = by_effect.get(effect, 0) + 1
            by_severity[severity] = by_severity.get(severity, 0) + 1
            if effect in {"deny", "requires_approval"}:
                blocking.append(self._compact_policy_decision(decision))
            if decision.get("requires_approval"):
                requires_approval.append(self._compact_policy_decision(decision))
        return {
            "total": len(decisions),
            "by_kind": dict(sorted(by_kind.items())),
            "by_outcome": dict(sorted(by_outcome.items())),
            "by_effect": dict(sorted(by_effect.items())),
            "by_severity": dict(sorted(by_severity.items())),
            "blocking": blocking,
            "requires_approval": requires_approval,
        }

    def _compact_policy_decision(self, decision: dict[str, Any]) -> dict[str, Any]:
        policy_input = decision.get("input") if isinstance(decision.get("input"), dict) else {}
        command = policy_input.get("command") if isinstance(policy_input.get("command"), dict) else {}
        compact = {
            "kind": decision.get("kind"),
            "scope": decision.get("scope"),
            "outcome": decision.get("outcome"),
            "effect": decision.get("effect"),
            "severity": decision.get("severity"),
            "reason": decision.get("reason"),
            "phase": decision.get("phase") or policy_input.get("phase"),
            "name": decision.get("name") or command.get("name"),
            "executor": decision.get("executor"),
            "approval_flag": decision.get("approval_flag"),
            "status": decision.get("status"),
        }
        return {key: value for key, value in compact.items() if value is not None}

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
        policy_decisions: list[dict[str, Any]] | None = None,
        policy_decision_summary: dict[str, Any] | None = None,
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
        if policy_decisions is not None:
            summary = policy_decision_summary or self._policy_decision_summary(policy_decisions)
            lines.extend(
                [
                    "## Policy Decisions",
                    "",
                    f"- Total decisions: `{summary.get('total', len(policy_decisions))}`",
                    f"- Outcomes: `{json.dumps(summary.get('by_outcome', {}), sort_keys=True)}`",
                    f"- Effects: `{json.dumps(summary.get('by_effect', {}), sort_keys=True)}`",
                    f"- Blocking decisions: `{len(summary.get('blocking', []))}`",
                    "",
                ]
            )
            blocking = summary.get("blocking", [])
            if blocking:
                lines.extend(["Blocking decisions:", ""])
                for decision in blocking[:40]:
                    lines.append(
                        "- "
                        f"`{decision.get('kind', 'unknown')}` "
                        f"`{decision.get('outcome', 'unknown')}`: {decision.get('reason', '')}"
                    )
                lines.append("")
            lines.extend(
                [
                    "```json",
                    json.dumps(
                        {
                            "policy_decision_summary": summary,
                            "policy_decisions": policy_decisions,
                        },
                        indent=2,
                        sort_keys=True,
                    ),
                    "```",
                    "",
                ]
            )
        report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
