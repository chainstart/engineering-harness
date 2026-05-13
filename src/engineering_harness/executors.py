from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol


EXECUTOR_CONTRACT_VERSION = 1
EXECUTOR_RESULT_CONTRACT_VERSION = 1
DAGGER_ENABLE_ENV = "ENGINEERING_HARNESS_ENABLE_DAGGER"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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
class ExecutorMetadata:
    id: str
    name: str
    kind: str
    adapter: str
    input_mode: str
    capabilities: tuple[str, ...]
    requires_agent_approval: bool = False
    uses_command_policy: bool = False

    def as_contract(self) -> dict[str, Any]:
        return {
            "schema_version": EXECUTOR_CONTRACT_VERSION,
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "adapter": self.adapter,
            "input_mode": self.input_mode,
            "capabilities": list(self.capabilities),
            "requires_agent_approval": self.requires_agent_approval,
            "uses_command_policy": self.uses_command_policy,
        }


@dataclass(frozen=True)
class ExecutorInvocation:
    project_root: Path
    task_id: str
    name: str
    command: str | None
    prompt: str | None
    timeout_seconds: int
    model: str | None = None
    sandbox: str = "workspace-write"
    environment: dict[str, str] = field(default_factory=dict)

    def env(self) -> dict[str, str]:
        return {**os.environ, **self.environment, "ENGINEERING_HARNESS": "1"}


@dataclass(frozen=True)
class ExecutorTaskCommand:
    name: str
    command: str | None
    prompt: str | None
    executor: str

    def summary(self) -> str:
        return self.command or self.prompt or self.executor


@dataclass(frozen=True)
class ExecutorTaskContext:
    project_root: Path
    task_id: str
    title: str
    milestone_id: str
    milestone_title: str
    file_scope: tuple[str, ...]
    acceptance: tuple[ExecutorTaskCommand, ...]
    e2e: tuple[ExecutorTaskCommand, ...]


@dataclass(frozen=True)
class ExecutorResult:
    status: str
    returncode: int | None
    started_at: str
    finished_at: str
    stdout: str
    stderr: str
    metadata: dict[str, Any] = field(default_factory=dict)


class Executor(Protocol):
    metadata: ExecutorMetadata

    def display_command(self, invocation: ExecutorInvocation) -> str:
        ...

    def execute(self, invocation: ExecutorInvocation) -> ExecutorResult:
        ...


class InvocationPreparingExecutor(Protocol):
    def prepare_invocation(
        self,
        invocation: ExecutorInvocation,
        task_context: ExecutorTaskContext,
    ) -> ExecutorInvocation:
        ...


class ShellExecutorAdapter:
    metadata = ExecutorMetadata(
        id="shell",
        name="Shell",
        kind="process",
        adapter="builtin.shell",
        input_mode="command",
        capabilities=("local_process", "exit_code", "stdout", "stderr"),
        uses_command_policy=True,
    )

    def prepare_invocation(
        self,
        invocation: ExecutorInvocation,
        task_context: ExecutorTaskContext,
    ) -> ExecutorInvocation:
        return invocation

    def display_command(self, invocation: ExecutorInvocation) -> str:
        return invocation.command or ""

    def execute(self, invocation: ExecutorInvocation) -> ExecutorResult:
        started_at = _utc_now()
        try:
            completed = subprocess.run(
                invocation.command or "",
                cwd=invocation.project_root,
                shell=True,
                executable="/bin/bash",
                text=True,
                capture_output=True,
                timeout=invocation.timeout_seconds,
                env=invocation.env(),
            )
            return ExecutorResult(
                status="passed" if completed.returncode == 0 else "failed",
                returncode=completed.returncode,
                started_at=started_at,
                finished_at=_utc_now(),
                stdout=redact(completed.stdout[-8000:]),
                stderr=redact(completed.stderr[-8000:]),
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            return ExecutorResult(
                status="failed",
                returncode=None,
                started_at=started_at,
                finished_at=_utc_now(),
                stdout=redact(stdout[-8000:]),
                stderr=f"Command timed out after {invocation.timeout_seconds} seconds.",
                metadata={"timed_out": True},
            )


class CodexExecutorAdapter:
    metadata = ExecutorMetadata(
        id="codex",
        name="Codex",
        kind="agent",
        adapter="builtin.codex",
        input_mode="prompt",
        capabilities=("agent", "workspace_write", "exit_code", "stdout", "stderr"),
        requires_agent_approval=True,
    )

    def prepare_invocation(
        self,
        invocation: ExecutorInvocation,
        task_context: ExecutorTaskContext,
    ) -> ExecutorInvocation:
        prompt = invocation.prompt or invocation.command or task_context.title
        acceptance = "\n".join(f"- {item.name}: {item.summary()}" for item in task_context.acceptance)
        e2e = "\n".join(f"- {item.name}: {item.summary()}" for item in task_context.e2e)
        file_scope = "\n".join(f"- {scope}" for scope in task_context.file_scope) or "- repository-scoped, but keep changes minimal"
        verification = acceptance if not e2e else f"{acceptance}\n\nE2E/user-experience commands:\n{e2e}"
        expanded_prompt = (
            "You are executing one roadmap task for an autonomous engineering harness.\n\n"
            f"Project root: {task_context.project_root}\n"
            f"Milestone: {task_context.milestone_id} - {task_context.milestone_title}\n"
            f"Task: {task_context.task_id} - {task_context.title}\n\n"
            "Goal:\n"
            f"{prompt}\n\n"
            "Allowed file scope:\n"
            f"{file_scope}\n\n"
            "Verification commands that must pass after your changes:\n"
            f"{verification}\n\n"
            "Constraints:\n"
            "- Edit files directly in the working tree.\n"
            "- Do not commit or push; the harness handles git checkpoints.\n"
            "- Do not use private keys, paid live deployment, or live trading.\n"
            "- Prefer focused, test-driven changes that satisfy the acceptance commands.\n"
            "- If the task cannot be completed locally, write a clear blocker into the relevant project docs.\n"
        )
        return replace(invocation, prompt=expanded_prompt)

    def display_command(self, invocation: ExecutorInvocation) -> str:
        model = f" --model {invocation.model}" if invocation.model else ""
        return (
            f"codex exec --full-auto --sandbox {invocation.sandbox}{model} "
            f"-C {invocation.project_root} <task:{invocation.task_id}>"
        )

    def execute(self, invocation: ExecutorInvocation) -> ExecutorResult:
        started_at = _utc_now()
        args = ["codex", "exec", "--full-auto", "--sandbox", invocation.sandbox, "-C", str(invocation.project_root)]
        if invocation.model:
            args.extend(["--model", invocation.model])
        args.append(invocation.prompt or invocation.command or "")
        try:
            completed = subprocess.run(
                args,
                cwd=invocation.project_root,
                text=True,
                capture_output=True,
                timeout=invocation.timeout_seconds,
                env=invocation.env(),
            )
            return ExecutorResult(
                status="passed" if completed.returncode == 0 else "failed",
                returncode=completed.returncode,
                started_at=started_at,
                finished_at=_utc_now(),
                stdout=redact(completed.stdout[-8000:]),
                stderr=redact(completed.stderr[-8000:]),
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            return ExecutorResult(
                status="failed",
                returncode=None,
                started_at=started_at,
                finished_at=_utc_now(),
                stdout=redact(stdout[-8000:]),
                stderr=f"Command timed out after {invocation.timeout_seconds} seconds.",
                metadata={"timed_out": True},
            )


class DaggerExecutorAdapter:
    metadata = ExecutorMetadata(
        id="dagger",
        name="Dagger",
        kind="container",
        adapter="builtin.dagger",
        input_mode="command",
        capabilities=(
            "local_dagger_cli",
            "containerized_execution",
            "exit_code",
            "stdout",
            "stderr",
            "requires_explicit_configuration",
        ),
    )

    def prepare_invocation(
        self,
        invocation: ExecutorInvocation,
        task_context: ExecutorTaskContext,
    ) -> ExecutorInvocation:
        return invocation

    def display_command(self, invocation: ExecutorInvocation) -> str:
        command = (invocation.command or "").strip()
        if command.startswith("dagger "):
            return f"{command} <task:{invocation.task_id}>"
        if command:
            return f"dagger {command} <task:{invocation.task_id}>"
        return f"dagger <task:{invocation.task_id}>"

    def execute(self, invocation: ExecutorInvocation) -> ExecutorResult:
        started_at = _utc_now()
        env = invocation.env()
        if env.get(DAGGER_ENABLE_ENV, "").lower() not in {"1", "true", "yes", "on"}:
            return ExecutorResult(
                status="blocked",
                returncode=None,
                started_at=started_at,
                finished_at=_utc_now(),
                stdout="",
                stderr=(
                    "Dagger executor is disabled. Set "
                    f"{DAGGER_ENABLE_ENV}=1 to enable local Dagger CLI execution."
                ),
                metadata={
                    "configured": False,
                    "required_environment": DAGGER_ENABLE_ENV,
                },
            )

        try:
            args = self._dagger_args(invocation.command or "")
        except ValueError as exc:
            return ExecutorResult(
                status="failed",
                returncode=2,
                started_at=started_at,
                finished_at=_utc_now(),
                stdout="",
                stderr=f"Invalid Dagger command: {exc}",
                metadata={"configured": True},
            )

        if not args:
            return ExecutorResult(
                status="failed",
                returncode=2,
                started_at=started_at,
                finished_at=_utc_now(),
                stdout="",
                stderr="Dagger command is missing.",
                metadata={"configured": True},
            )

        try:
            completed = subprocess.run(
                ["dagger", *args],
                cwd=invocation.project_root,
                text=True,
                capture_output=True,
                timeout=invocation.timeout_seconds,
                env=env,
            )
            return ExecutorResult(
                status="passed" if completed.returncode == 0 else "failed",
                returncode=completed.returncode,
                started_at=started_at,
                finished_at=_utc_now(),
                stdout=redact(completed.stdout[-8000:]),
                stderr=redact(completed.stderr[-8000:]),
                metadata={"configured": True},
            )
        except FileNotFoundError:
            return ExecutorResult(
                status="blocked",
                returncode=None,
                started_at=started_at,
                finished_at=_utc_now(),
                stdout="",
                stderr="Dagger CLI was not found on PATH.",
                metadata={"configured": True, "missing_binary": "dagger"},
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            return ExecutorResult(
                status="failed",
                returncode=None,
                started_at=started_at,
                finished_at=_utc_now(),
                stdout=redact(stdout[-8000:]),
                stderr=f"Dagger command timed out after {invocation.timeout_seconds} seconds.",
                metadata={"configured": True, "timed_out": True},
            )

    def _dagger_args(self, command: str) -> list[str]:
        args = shlex.split(command)
        if args[:1] == ["dagger"]:
            return args[1:]
        return args


ShellExecutor = ShellExecutorAdapter
CodexExecutor = CodexExecutorAdapter
DaggerExecutor = DaggerExecutorAdapter


class ExecutorRegistry:
    def __init__(self, executors: tuple[Executor, ...]) -> None:
        self._executors = {executor.metadata.id: executor for executor in executors}

    def get(self, executor_id: str) -> Executor | None:
        return self._executors.get(executor_id)

    def metadata_for(self, executor_id: str) -> dict[str, Any]:
        executor = self.get(executor_id)
        if executor is None:
            return {
                "schema_version": EXECUTOR_CONTRACT_VERSION,
                "id": executor_id,
                "name": executor_id,
                "kind": "unknown",
                "adapter": None,
                "input_mode": "unknown",
                "capabilities": [],
                "requires_agent_approval": None,
                "uses_command_policy": None,
            }
        return executor.metadata.as_contract()

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._executors))


def default_executor_registry() -> ExecutorRegistry:
    return ExecutorRegistry((ShellExecutorAdapter(), CodexExecutorAdapter(), DaggerExecutorAdapter()))
