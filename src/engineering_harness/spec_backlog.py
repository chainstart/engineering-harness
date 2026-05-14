from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path
from typing import Any


SPEC_BACKLOG_GENERATOR_ID = "engineering-harness-spec-backlog-materializer"
SPEC_BACKLOG_PLAN_KIND = "engineering-harness.spec-backlog-plan.v1"

_STAGE_HEADING_RE = re.compile(r"^(?P<level>#{2,3})\s+Stage\s+(?P<number>\d+):\s+(?P<title>.+?)\s*$")
_NUMBERED_TASK_RE = re.compile(r"^\d+\.\s+(?P<text>.+?)\s*$")
_BULLET_TASK_RE = re.compile(r"^-\s+(?P<text>.+?)\s*$")
_SPEC_REF_RE = re.compile(r"\b[A-Z][A-Z0-9]+-SPEC-\d+\b|\bEH-SPEC-\d+\b")


def default_spec_backlog_sources(
    roadmap: dict[str, Any],
    *,
    include_blueprint: bool = False,
) -> list[str]:
    sources: list[str] = []
    spec = roadmap.get("spec") if isinstance(roadmap.get("spec"), dict) else {}
    development_plan = spec.get("development_plan") if isinstance(spec, dict) else None
    if str(development_plan or "").strip():
        sources.append(str(development_plan))
    if include_blueprint:
        continuation = roadmap.get("continuation") if isinstance(roadmap.get("continuation"), dict) else {}
        blueprint = continuation.get("blueprint") if isinstance(continuation, dict) else None
        if str(blueprint or "").strip():
            sources.append(str(blueprint))
    return list(dict.fromkeys(sources))


def build_spec_backlog_plan(
    *,
    project_root: Path,
    roadmap: dict[str, Any],
    source_paths: list[str] | None = None,
    include_blueprint: bool = False,
    from_stage: int = 1,
) -> dict[str, Any]:
    source_values = source_paths or default_spec_backlog_sources(roadmap, include_blueprint=include_blueprint)
    existing_stage_ids = _existing_stage_ids(roadmap)
    existing_task_ids = _existing_task_ids(roadmap)
    existing_source_stage_keys = _existing_source_stage_keys(roadmap)
    existing_stage_semantic_keys = _existing_stage_semantic_keys(roadmap)
    stages: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []

    for source_value in source_values:
        source_path = _resolve_source(project_root, source_value)
        source_relative = _relative_source(project_root, source_path)
        parsed = parse_spec_backlog_source(source_path, project_root=project_root, from_stage=from_stage)
        sources.append(
            {
                "path": source_relative,
                "stage_count": len(parsed["stages"]),
                "task_count": sum(len(stage["source_tasks"]) for stage in parsed["stages"]),
            }
        )
        for parsed_stage in parsed["stages"]:
            stage = build_continuation_stage(parsed_stage)
            stage_id = str(stage["id"])
            task_ids = [str(task["id"]) for task in stage.get("tasks", [])]
            source_stage_key = _source_stage_key(stage)
            stage_semantic_key = _stage_semantic_key(stage)
            reasons = []
            if stage_id in existing_stage_ids:
                reasons.append("existing_stage_id")
            duplicate_tasks = [task_id for task_id in task_ids if task_id in existing_task_ids]
            if duplicate_tasks:
                reasons.append("existing_task_id")
            if source_stage_key and source_stage_key in existing_source_stage_keys:
                reasons.append("existing_source_stage")
            if stage_semantic_key and stage_semantic_key in existing_stage_semantic_keys:
                reasons.append("existing_stage_semantics")
            if reasons:
                skipped.append(
                    {
                        "id": stage_id,
                        "title": stage["title"],
                        "source": parsed_stage["source"],
                        "stage_number": parsed_stage["stage_number"],
                        "reasons": reasons,
                        "duplicate_task_ids": duplicate_tasks,
                    }
                )
                continue
            existing_stage_ids.add(stage_id)
            existing_task_ids.update(task_ids)
            if source_stage_key:
                existing_source_stage_keys.add(source_stage_key)
            if stage_semantic_key:
                existing_stage_semantic_keys.add(stage_semantic_key)
            stages.append(stage)

    return {
        "kind": SPEC_BACKLOG_PLAN_KIND,
        "status": "proposed",
        "materialized": False,
        "project": str(roadmap.get("project", project_root.name)),
        "roadmap_path": str(project_root / ".engineering" / "roadmap.yaml"),
        "source_count": len(sources),
        "sources": sources,
        "stage_count": len(stages),
        "task_count": sum(len(stage.get("tasks", [])) for stage in stages),
        "skipped_stage_count": len(skipped),
        "skipped_stages": skipped,
        "stages": stages,
    }


def parse_spec_backlog_source(source_path: Path, *, project_root: Path, from_stage: int = 1) -> dict[str, Any]:
    text = source_path.read_text(encoding="utf-8")
    source_relative = _relative_source(project_root, source_path)
    source_slug = slugify(Path(source_relative).with_suffix("").as_posix().replace("/", "-"), max_length=64)
    stages: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    section: str | None = None
    active_task: dict[str, str] | None = None

    def finish_current() -> None:
        nonlocal current, section, active_task
        if current is not None and current["stage_number"] >= from_stage and current["source_tasks"]:
            stages.append(current)
        current = None
        section = None
        active_task = None

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        heading = _STAGE_HEADING_RE.match(stripped)
        if heading:
            finish_current()
            stage_number = int(heading.group("number"))
            title = heading.group("title").strip()
            current = {
                "source": source_relative,
                "source_slug": source_slug,
                "stage_number": stage_number,
                "title": title,
                "stage_slug": slugify(title, max_length=64),
                "requirement_refs": [],
                "goal": "",
                "source_tasks": [],
                "acceptance": [],
            }
            continue
        if current is None:
            continue
        if stripped in {"Requirement refs:", "Goal:", "Tasks:", "Acceptance:"}:
            section = stripped[:-1].lower().replace(" ", "_")
            active_task = None
            continue
        if not stripped:
            active_task = None if section != "tasks" else active_task
            continue
        if section == "requirement_refs":
            refs = _SPEC_REF_RE.findall(stripped)
            for ref in refs:
                if ref not in current["requirement_refs"]:
                    current["requirement_refs"].append(ref)
            continue
        if section == "goal":
            current["goal"] = _append_sentence(current["goal"], stripped)
            continue
        if section == "tasks":
            match = _NUMBERED_TASK_RE.match(stripped) or (_BULLET_TASK_RE.match(stripped) if line.startswith("- ") else None)
            if match:
                task = {"text": match.group("text").strip()}
                current["source_tasks"].append(task)
                active_task = task
            elif active_task is not None and line.startswith(("  ", "\t")):
                active_task["text"] = _append_sentence(active_task["text"], stripped.lstrip("- ").strip())
            continue
        if section == "acceptance":
            match = _BULLET_TASK_RE.match(stripped) if line.startswith("- ") else None
            if match:
                current["acceptance"].append(match.group("text").strip())
            continue

    finish_current()
    return {
        "source": source_relative,
        "stage_count": len(stages),
        "task_count": sum(len(stage["source_tasks"]) for stage in stages),
        "stages": stages,
    }


def build_continuation_stage(parsed_stage: dict[str, Any]) -> dict[str, Any]:
    stage_id = (
        f"{parsed_stage['source_slug']}-stage-{parsed_stage['stage_number']}-"
        f"{parsed_stage['stage_slug']}"
    )
    stage_id = slugify(stage_id, max_length=96)
    requirement_refs = list(parsed_stage.get("requirement_refs", []))
    source_tasks = list(parsed_stage.get("source_tasks", []))
    tasks = [
        build_continuation_task(parsed_stage, stage_id=stage_id, task_index=index, source_task=source_task)
        for index, source_task in enumerate(source_tasks, start=1)
    ]
    objective_parts = [
        f"Implement the source specification stage from {parsed_stage['source']}.",
        f"Stage goal: {parsed_stage.get('goal') or parsed_stage['title']}.",
    ]
    if requirement_refs:
        objective_parts.append(f"Requirement refs: {', '.join(requirement_refs)}.")
    return {
        "id": stage_id,
        "title": parsed_stage["title"],
        "objective": " ".join(objective_parts),
        "status": "planned",
        "generated_by": SPEC_BACKLOG_GENERATOR_ID,
        "source": {
            "path": parsed_stage["source"],
            "stage_number": parsed_stage["stage_number"],
            "stage_title": parsed_stage["title"],
            "task_count": len(tasks),
        },
        "spec_refs": requirement_refs,
        "tasks": tasks,
    }


def build_continuation_task(
    parsed_stage: dict[str, Any],
    *,
    stage_id: str,
    task_index: int,
    source_task: dict[str, str],
) -> dict[str, Any]:
    task_text = source_task["text"].rstrip(".")
    task_slug = slugify(task_text, max_length=48)
    task_id = slugify(f"{stage_id}-task-{task_index}-{task_slug}", max_length=120)
    requirement_refs = list(parsed_stage.get("requirement_refs", []))
    prompt = _implementation_prompt(parsed_stage, task_text)
    repair_prompt = _repair_prompt(parsed_stage, task_text)
    task: dict[str, Any] = {
        "id": task_id,
        "title": task_text,
        "status": "pending",
        "max_attempts": 4,
        "max_task_iterations": 4,
        "manual_approval_required": False,
        "agent_approval_required": True,
        "file_scope": [
            "src/engineering_harness/**",
            "tests/**",
            "docs/**",
            "README.md",
            "bin/**",
            ".github/**",
        ],
        "implementation": [
            {
                "name": "Codex implementation",
                "executor": "codex",
                "timeout_seconds": 5400,
                "sandbox": "workspace-write",
                "prompt": prompt,
            }
        ],
        "repair": [
            {
                "name": "Codex repair",
                "executor": "codex",
                "timeout_seconds": 2700,
                "sandbox": "workspace-write",
                "prompt": repair_prompt,
            }
        ],
        "acceptance": [
            {
                "name": "full harness tests",
                "command": "python3 -m pytest tests/test_engineering_harness.py -q",
                "required": True,
                "timeout_seconds": 1500,
            },
            {
                "name": "current roadmap validates",
                "command": "bin/engh validate --project-root .",
                "required": True,
                "timeout_seconds": 300,
            },
        ],
        "e2e": [
            {
                "name": "status remains machine-readable",
                "command": "bin/engh status --project-root . --json",
                "required": True,
                "timeout_seconds": 300,
            }
        ],
        "source_spec_task": {
            "path": parsed_stage["source"],
            "stage_number": parsed_stage["stage_number"],
            "stage_title": parsed_stage["title"],
            "task_index": task_index,
            "task": source_task["text"],
        },
    }
    if requirement_refs:
        task["spec_refs"] = requirement_refs
        for group_name in ("acceptance", "e2e"):
            for command in task[group_name]:
                command["spec_refs"] = requirement_refs
    return task


def materialize_spec_backlog_plan(
    roadmap: dict[str, Any],
    stages: list[dict[str, Any]],
) -> tuple[dict[str, Any], int]:
    updated = deepcopy(roadmap)
    continuation = updated.setdefault("continuation", {})
    if not isinstance(continuation, dict):
        continuation = {"enabled": True, "stages": []}
        updated["continuation"] = continuation
    continuation["enabled"] = True
    continuation.setdefault(
        "goal",
        "Materialize and complete specification-derived continuation stages under harness control.",
    )
    continuation_stages = continuation.setdefault("stages", [])
    if not isinstance(continuation_stages, list):
        continuation_stages = []
        continuation["stages"] = continuation_stages
    existing_stage_ids = _existing_stage_ids(updated)
    added = 0
    for stage in stages:
        if str(stage.get("id")) in existing_stage_ids:
            continue
        continuation_stages.append(stage)
        existing_stage_ids.add(str(stage.get("id")))
        added += 1
    return updated, added


def slugify(value: str, *, max_length: int = 80) -> str:
    text = value.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    if not text:
        text = "item"
    if len(text) <= max_length:
        return text
    return text[:max_length].rstrip("-")


def _implementation_prompt(parsed_stage: dict[str, Any], task_text: str) -> str:
    refs = ", ".join(parsed_stage.get("requirement_refs", [])) or "none"
    acceptance = "; ".join(parsed_stage.get("acceptance", [])) or "Keep the roadmap valid and tests passing."
    return (
        "Implement this specification-derived engineering-harness backlog task.\n\n"
        f"Source: {parsed_stage['source']}\n"
        f"Stage: Stage {parsed_stage['stage_number']} - {parsed_stage['title']}\n"
        f"Requirement refs: {refs}\n"
        f"Stage goal: {parsed_stage.get('goal') or parsed_stage['title']}\n"
        f"Task: {task_text}\n"
        f"Stage acceptance summary: {acceptance}\n\n"
        "Use existing engineering-harness patterns and keep the change local, testable, and reviewable. "
        "Add or update focused tests and documentation where the behavior changes. Preserve drive, "
        "self-iteration, checkpoint readiness, failure isolation, approval/capability policy, workspace "
        "dispatch, runtime dashboard, and local-only execution semantics. Do not require external accounts, "
        "private keys, paid services, production deployments, mainnet writes, live trading, or real pushes."
    )


def _repair_prompt(parsed_stage: dict[str, Any], task_text: str) -> str:
    return (
        "Fix failing tests or validation for this specification-derived backlog task while preserving "
        "the intended source task behavior.\n\n"
        f"Source: {parsed_stage['source']}\n"
        f"Stage: Stage {parsed_stage['stage_number']} - {parsed_stage['title']}\n"
        f"Task: {task_text}\n"
        "Keep repairs scoped, local-only, and compatible with existing harness evidence and safety semantics."
    )


def _resolve_source(project_root: Path, source_value: str) -> Path:
    source_path = Path(source_value)
    if not source_path.is_absolute():
        source_path = project_root / source_path
    return source_path.resolve()


def _relative_source(project_root: Path, source_path: Path) -> str:
    try:
        return source_path.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return str(source_path)


def _existing_stage_ids(roadmap: dict[str, Any]) -> set[str]:
    stage_ids = {str(item.get("id")) for item in roadmap.get("milestones", []) if isinstance(item, dict)}
    continuation = roadmap.get("continuation") if isinstance(roadmap.get("continuation"), dict) else {}
    stages = continuation.get("stages", []) if isinstance(continuation, dict) else []
    if isinstance(stages, list):
        stage_ids.update(str(item.get("id")) for item in stages if isinstance(item, dict))
    return {stage_id for stage_id in stage_ids if stage_id}


def _existing_task_ids(roadmap: dict[str, Any]) -> set[str]:
    task_ids: set[str] = set()
    for milestone in roadmap.get("milestones", []):
        if not isinstance(milestone, dict):
            continue
        for task in milestone.get("tasks", []):
            if isinstance(task, dict) and str(task.get("id", "")).strip():
                task_ids.add(str(task["id"]))
    continuation = roadmap.get("continuation") if isinstance(roadmap.get("continuation"), dict) else {}
    stages = continuation.get("stages", []) if isinstance(continuation, dict) else []
    if isinstance(stages, list):
        for stage in stages:
            if not isinstance(stage, dict):
                continue
            for task in stage.get("tasks", []):
                if isinstance(task, dict) and str(task.get("id", "")).strip():
                    task_ids.add(str(task["id"]))
    return task_ids


def _existing_source_stage_keys(roadmap: dict[str, Any]) -> set[tuple[str, int]]:
    keys: set[tuple[str, int]] = set()
    for stage in _roadmap_stages(roadmap):
        key = _source_stage_key(stage)
        if key:
            keys.add(key)
    return keys


def _existing_stage_semantic_keys(roadmap: dict[str, Any]) -> set[tuple[tuple[str, ...], tuple[str, ...]]]:
    keys: set[tuple[tuple[str, ...], tuple[str, ...]]] = set()
    for stage in _roadmap_stages(roadmap):
        key = _stage_semantic_key(stage)
        if key:
            keys.add(key)
    return keys


def _roadmap_stages(roadmap: dict[str, Any]) -> list[dict[str, Any]]:
    stages: list[dict[str, Any]] = []
    milestones = roadmap.get("milestones", [])
    if isinstance(milestones, list):
        stages.extend(item for item in milestones if isinstance(item, dict))
    continuation = roadmap.get("continuation") if isinstance(roadmap.get("continuation"), dict) else {}
    continuation_stages = continuation.get("stages", []) if isinstance(continuation, dict) else []
    if isinstance(continuation_stages, list):
        stages.extend(item for item in continuation_stages if isinstance(item, dict))
    return stages


def _source_stage_key(stage: dict[str, Any]) -> tuple[str, int] | None:
    source = stage.get("source")
    if not isinstance(source, dict):
        return None
    path = str(source.get("path") or "").strip()
    if not path:
        return None
    try:
        stage_number = int(source.get("stage_number"))
    except (TypeError, ValueError):
        return None
    return (path, stage_number)


def _stage_semantic_key(stage: dict[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    refs = _unique_texts(stage.get("spec_refs"))
    tasks = stage.get("tasks", [])
    if not isinstance(tasks, list):
        tasks = []
    task_texts: list[str] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        refs.extend(ref for ref in _unique_texts(task.get("spec_refs")) if ref not in refs)
        source_task = task.get("source_spec_task") if isinstance(task.get("source_spec_task"), dict) else {}
        task_text = str(source_task.get("task") or task.get("title") or "").rstrip(".")
        normalized = _semantic_text(task_text)
        if normalized:
            task_texts.append(normalized)
    if not refs or not task_texts:
        return None
    return (tuple(sorted(refs)), tuple(task_texts))


def _unique_texts(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    items: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in items:
            items.append(text)
    return items


def _semantic_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _append_sentence(existing: str, text: str) -> str:
    clean = text.strip()
    if not clean:
        return existing
    if not existing:
        return clean
    return f"{existing} {clean}"
