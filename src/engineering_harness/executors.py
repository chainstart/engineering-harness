from __future__ import annotations

import os
import re
import select
import shlex
import signal
import subprocess
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol


EXECUTOR_CONTRACT_VERSION = 1
EXECUTOR_RESULT_CONTRACT_VERSION = 1
EXECUTOR_WATCHDOG_CONTRACT_VERSION = 1
CAPABILITY_CLASSIFICATION_SCHEMA_VERSION = 1
DAGGER_ENABLE_ENV = "ENGINEERING_HARNESS_ENABLE_DAGGER"
PROCESS_TERMINATION_GRACE_SECONDS = 0.5
SENSITIVE_ENV_NAME_PATTERN = (
    r"[A-Z0-9_]*(?:API[-_]?KEY|ACCESS[-_]?KEY|TOKEN|SECRET|PASSWORD|PASS|"
    r"PRIVATE[-_]?KEY|MNEMONIC|SEED(?:[-_]?PHRASE)?|CREDENTIALS?)[A-Z0-9_]*"
)
SENSITIVE_QUOTED_VALUE_RE = re.compile(
    rf"(?i)\b({SENSITIVE_ENV_NAME_PATTERN})\b(\s*[:=]\s*)(['\"])(.*?)(\3)"
)
SENSITIVE_UNQUOTED_VALUE_RE = re.compile(
    rf"(?i)\b({SENSITIVE_ENV_NAME_PATTERN})\b(\s*=\s*)([^\s\"'`]+)"
)
BEARER_TOKEN_RE = re.compile(r"(?i)\b(Bearer\s+)([A-Za-z0-9._~+/=-]{8,})")
OPENAI_STYLE_TOKEN_RE = re.compile(r"\b(sk-[A-Za-z0-9][A-Za-z0-9_-]{8,})\b")
CAPABILITY_CLASS_BY_NAME = {
    "agent": "agent",
    "browser_automation": "network",
    "containerized_execution": "filesystem",
    "deployment": "deploy",
    "deploy": "deploy",
    "exit_code": "observability",
    "filesystem_escape": "filesystem",
    "host_filesystem_write": "filesystem",
    "live": "deploy",
    "live_operations": "deploy",
    "local_dagger_cli": "process",
    "local_process": "process",
    "network": "network",
    "network_access": "network",
    "requires_explicit_configuration": "configuration",
    "secret_access": "secret",
    "secrets": "secret",
    "stderr": "observability",
    "stdout": "observability",
    "workspace_write": "filesystem",
}
CORE_CAPABILITY_CLASSES = ("filesystem", "network", "secret", "deploy")


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def redact(text: str) -> str:
    redacted = str(text)
    redacted = SENSITIVE_QUOTED_VALUE_RE.sub(r"\1\2\3[REDACTED]\5", redacted)
    redacted = SENSITIVE_UNQUOTED_VALUE_RE.sub(r"\1\2[REDACTED]", redacted)
    redacted = BEARER_TOKEN_RE.sub(r"\1[REDACTED]", redacted)
    redacted = OPENAI_STYLE_TOKEN_RE.sub("[REDACTED]", redacted)
    return redacted


def classify_capabilities(capabilities: tuple[str, ...] | list[str]) -> dict[str, Any]:
    classes: dict[str, list[str]] = {class_name: [] for class_name in CORE_CAPABILITY_CLASSES}
    for class_name in ("process", "observability", "agent", "configuration"):
        classes.setdefault(class_name, [])
    unknown: list[str] = []
    for capability in capabilities:
        name = str(capability).strip()
        if not name:
            continue
        class_name = CAPABILITY_CLASS_BY_NAME.get(name)
        if class_name is None:
            unknown.append(name)
            continue
        classes.setdefault(class_name, []).append(name)
    classes = {key: sorted(dict.fromkeys(value)) for key, value in classes.items()}
    return {
        "schema_version": CAPABILITY_CLASSIFICATION_SCHEMA_VERSION,
        "classes": classes,
        "core_classes": {
            class_name: {
                "capabilities": classes.get(class_name, []),
                "supported": bool(classes.get(class_name)),
            }
            for class_name in CORE_CAPABILITY_CLASSES
        },
        "unknown": sorted(dict.fromkeys(unknown)),
    }


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
        capability_classifications = classify_capabilities(self.capabilities)
        return {
            "schema_version": EXECUTOR_CONTRACT_VERSION,
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "adapter": self.adapter,
            "input_mode": self.input_mode,
            "capabilities": list(self.capabilities),
            "capability_classifications": capability_classifications,
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
    phase: str | None = None
    no_progress_timeout_seconds: int | None = None
    progress_callback: Callable[[dict[str, Any]], None] | None = field(default=None, repr=False, compare=False)

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


def _tail_redacted(chunks: list[bytes]) -> str:
    return redact(b"".join(chunks).decode("utf-8", errors="replace")[-8000:])


def _safe_positive_seconds(value: int | None) -> int | None:
    if value is None:
        return None
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def _emit_progress(invocation: ExecutorInvocation, payload: dict[str, Any]) -> None:
    if invocation.progress_callback is None:
        return
    try:
        invocation.progress_callback(payload)
    except Exception:
        return


def _terminate_owned_process_tree(
    process: subprocess.Popen[bytes],
    *,
    owned_process_group_id: int | None = None,
) -> dict[str, Any]:
    details: dict[str, Any] = {
        "pid": process.pid,
        "owned_process_group": False,
        "terminated_process_group": False,
        "termination_signal": None,
        "killed": False,
    }
    if process.poll() is not None and owned_process_group_id is None:
        details["already_exited"] = True
        return details

    pgid: int | None = owned_process_group_id
    if os.name == "posix":
        if pgid is None:
            try:
                pgid = os.getpgid(process.pid)
            except OSError:
                pgid = None
    details["process_group_id"] = pgid
    owned_process_group = pgid is not None and (owned_process_group_id is not None or pgid == process.pid)
    details["owned_process_group"] = owned_process_group

    try:
        if owned_process_group:
            os.killpg(pgid, signal.SIGTERM)
            details["terminated_process_group"] = True
            details["termination_signal"] = "SIGTERM"
        else:
            process.terminate()
            details["termination_signal"] = "SIGTERM"
    except ProcessLookupError:
        return details
    except OSError as exc:
        details["termination_error"] = str(exc)
        return details

    deadline = time.monotonic() + PROCESS_TERMINATION_GRACE_SECONDS
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    if process.poll() is not None:
        return details

    try:
        if owned_process_group:
            os.killpg(pgid, signal.SIGKILL)
            details["termination_signal"] = "SIGKILL"
        else:
            process.kill()
            details["termination_signal"] = "SIGKILL"
        details["killed"] = True
    except ProcessLookupError:
        pass
    except OSError as exc:
        details["kill_error"] = str(exc)
    return details


def _read_available(fd: int) -> bytes | None:
    try:
        return os.read(fd, 4096)
    except BlockingIOError:
        return None
    except OSError:
        return b""


def _run_subprocess_with_watchdog(
    invocation: ExecutorInvocation,
    *,
    args: str | list[str],
    executor_id: str,
    shell: bool = False,
    executable: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> ExecutorResult:
    started_at = _utc_now()
    started_monotonic = time.monotonic()
    last_progress_at = started_at
    last_progress_monotonic = started_monotonic
    no_progress_seconds = _safe_positive_seconds(invocation.no_progress_timeout_seconds)
    timeout_seconds = _safe_positive_seconds(invocation.timeout_seconds)
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    process: subprocess.Popen[bytes] | None = None
    owned_process_group_id: int | None = None
    termination: dict[str, Any] = {}
    watchdog_status = "running"
    watchdog_reason: str | None = None
    watchdog_message: str | None = None

    def watchdog_payload(*, event: str, finished_at: str | None = None) -> dict[str, Any]:
        now_monotonic = time.monotonic()
        threshold_seconds = (
            no_progress_seconds
            if watchdog_reason == "no_progress"
            else timeout_seconds
            if watchdog_reason == "runtime_timeout"
            else no_progress_seconds
        )
        return {
            "schema_version": EXECUTOR_WATCHDOG_CONTRACT_VERSION,
            "event": event,
            "status": watchdog_status,
            "reason": watchdog_reason,
            "message": watchdog_message,
            "phase": invocation.phase,
            "executor_id": executor_id,
            "command_name": invocation.name,
            "pid": process.pid if process is not None else None,
            "started_at": started_at,
            "finished_at": finished_at,
            "runtime_seconds": round(max(0.0, now_monotonic - started_monotonic), 3),
            "timeout_seconds": timeout_seconds,
            "no_progress_timeout_seconds": no_progress_seconds,
            "threshold_seconds": threshold_seconds,
            "last_progress_at": last_progress_at,
            "last_output_at": last_progress_at,
            "stdout_bytes": sum(len(chunk) for chunk in stdout_chunks),
            "stderr_bytes": sum(len(chunk) for chunk in stderr_chunks),
            **({"termination": termination} if termination else {}),
        }

    try:
        process = subprocess.Popen(
            args,
            cwd=invocation.project_root,
            shell=shell,
            executable=executable,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=invocation.env(),
            start_new_session=(os.name == "posix"),
        )
    except FileNotFoundError:
        raise
    if os.name == "posix":
        try:
            process_group_id = os.getpgid(process.pid)
        except OSError:
            process_group_id = None
        if process_group_id == process.pid:
            owned_process_group_id = process_group_id

    _emit_progress(invocation, watchdog_payload(event="started"))
    streams: dict[int, tuple[str, list[bytes]]] = {}
    for stream_name, pipe, chunks in (
        ("stdout", process.stdout, stdout_chunks),
        ("stderr", process.stderr, stderr_chunks),
    ):
        if pipe is None:
            continue
        fd = pipe.fileno()
        os.set_blocking(fd, False)
        streams[fd] = (stream_name, chunks)

    drain_deadline: float | None = None
    while streams or process.poll() is None:
        now = time.monotonic()
        if watchdog_reason is None:
            if timeout_seconds is not None and now - started_monotonic >= timeout_seconds:
                watchdog_status = "timeout"
                watchdog_reason = "runtime_timeout"
                watchdog_message = f"Command timed out after {timeout_seconds} seconds."
                termination = _terminate_owned_process_tree(
                    process,
                    owned_process_group_id=owned_process_group_id,
                )
                drain_deadline = time.monotonic() + PROCESS_TERMINATION_GRACE_SECONDS
                _emit_progress(invocation, watchdog_payload(event="timeout"))
            elif no_progress_seconds is not None and now - last_progress_monotonic >= no_progress_seconds:
                watchdog_status = "no_progress"
                watchdog_reason = "no_progress"
                watchdog_message = f"Command produced no output for {no_progress_seconds} seconds."
                termination = _terminate_owned_process_tree(
                    process,
                    owned_process_group_id=owned_process_group_id,
                )
                drain_deadline = time.monotonic() + PROCESS_TERMINATION_GRACE_SECONDS
                _emit_progress(invocation, watchdog_payload(event="no_progress"))

        if streams:
            timeout = 0.05
            if watchdog_reason is None:
                deadlines = []
                if timeout_seconds is not None:
                    deadlines.append(timeout_seconds - (time.monotonic() - started_monotonic))
                if no_progress_seconds is not None:
                    deadlines.append(no_progress_seconds - (time.monotonic() - last_progress_monotonic))
                positive_deadlines = [item for item in deadlines if item > 0]
                if positive_deadlines:
                    timeout = max(0.01, min(timeout, min(positive_deadlines)))
            readable, _, _ = select.select(list(streams), [], [], timeout)
            for fd in readable:
                data = _read_available(fd)
                if data is None:
                    continue
                if data == b"":
                    streams.pop(fd, None)
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    continue
                stream_name, chunks = streams[fd]
                chunks.append(data)
                last_progress_at = _utc_now()
                last_progress_monotonic = time.monotonic()
                _emit_progress(
                    invocation,
                    {
                        **watchdog_payload(event="output"),
                        "stream": stream_name,
                        "bytes": len(data),
                    },
                )
        else:
            time.sleep(0.02)

        if drain_deadline is not None and time.monotonic() >= drain_deadline:
            for fd in list(streams):
                streams.pop(fd, None)
                try:
                    os.close(fd)
                except OSError:
                    pass

    try:
        returncode = process.wait(timeout=PROCESS_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        termination = termination or _terminate_owned_process_tree(
            process,
            owned_process_group_id=owned_process_group_id,
        )
        returncode = process.poll()

    finished_at = _utc_now()
    stdout = _tail_redacted(stdout_chunks)
    stderr = _tail_redacted(stderr_chunks)
    if watchdog_message:
        stderr = f"{stderr}\n{watchdog_message}".strip()

    if watchdog_reason == "runtime_timeout":
        status = "timeout"
        returncode_payload = None
    elif watchdog_reason == "no_progress":
        status = "no_progress"
        returncode_payload = None
    else:
        watchdog_status = "passed" if returncode == 0 else "failed"
        status = watchdog_status
        returncode_payload = returncode

    watchdog = watchdog_payload(event="finished", finished_at=finished_at)
    watchdog["status"] = status
    if returncode is not None:
        watchdog["process_returncode"] = returncode
    result_metadata = dict(metadata or {})
    result_metadata["watchdog"] = watchdog
    if watchdog_reason == "runtime_timeout":
        result_metadata["timed_out"] = True
    if watchdog_reason == "no_progress":
        result_metadata["no_progress"] = True
    _emit_progress(invocation, watchdog)
    return ExecutorResult(
        status=status,
        returncode=returncode_payload,
        started_at=started_at,
        finished_at=finished_at,
        stdout=stdout,
        stderr=stderr,
        metadata=result_metadata,
    )


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
        capabilities=("local_process", "workspace_write", "exit_code", "stdout", "stderr"),
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
        return _run_subprocess_with_watchdog(
            invocation,
            args=invocation.command or "",
            executor_id=self.metadata.id,
            shell=True,
            executable="/bin/bash",
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
        args = ["codex", "exec", "--full-auto", "--sandbox", invocation.sandbox, "-C", str(invocation.project_root)]
        if invocation.model:
            args.extend(["--model", invocation.model])
        args.append(invocation.prompt or invocation.command or "")
        return _run_subprocess_with_watchdog(invocation, args=args, executor_id=self.metadata.id)


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
            return _run_subprocess_with_watchdog(
                invocation,
                args=["dagger", *args],
                executor_id=self.metadata.id,
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
                "capability_classifications": classify_capabilities(()),
                "requires_agent_approval": None,
                "uses_command_policy": None,
            }
        return executor.metadata.as_contract()

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._executors))


def default_executor_registry() -> ExecutorRegistry:
    return ExecutorRegistry((ShellExecutorAdapter(), CodexExecutorAdapter(), DaggerExecutorAdapter()))
