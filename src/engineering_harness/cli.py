from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .core import (
    COMPLETED_STATUSES,
    Harness,
    discover_projects,
    init_project,
    parse_utc_timestamp,
    project_from_root,
    slug_now,
    utc_now,
)
from .goal_planner import DEFAULT_GOAL_STAGE_COUNT, materialize_goal_roadmap, plan_goal_roadmap
from .io import write_json
from .profiles import list_profiles


WORKSPACE_DISPATCH_LEASE_SCHEMA_VERSION = 1
WORKSPACE_DISPATCH_LEASE_DIRNAME = "workspace-dispatch-lease"
WORKSPACE_DISPATCH_LEASE_STALE_SECONDS_ENV = "ENGINEERING_HARNESS_WORKSPACE_DISPATCH_LEASE_STALE_AFTER_SECONDS"
DEFAULT_WORKSPACE_DISPATCH_LEASE_STALE_SECONDS = 3600


def resolve_project_root(args: argparse.Namespace) -> Path:
    if getattr(args, "project_root", None):
        return Path(args.project_root).resolve()
    workspace = Path(getattr(args, "workspace", ".")).resolve()
    project_name = getattr(args, "project", None)
    if not project_name:
        raise ValueError("Provide --project-root or --project")
    for project in discover_projects(workspace):
        if project.name == project_name or project.root.name == project_name or str(project.root).endswith(project_name):
            return project.root
    raise ValueError(f"Project not found in {workspace}: {project_name}")


def cmd_profiles(args: argparse.Namespace) -> int:
    payload = list_profiles()
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for item in payload:
            print(f"{item['id']}: {item['description']}")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    projects = discover_projects(Path(args.workspace), max_depth=args.max_depth)
    payload = [
        {
            "name": project.name,
            "root": str(project.root),
            "configured": project.configured,
            "roadmap": str(project.roadmap_path) if project.roadmap_path else None,
            "profile": project.profile,
            "kind": project.kind,
        }
        for project in projects
    ]
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for item in payload:
            marker = "configured" if item["configured"] else "candidate"
            profile = item["profile"] or "unknown"
            print(f"{item['name']} [{marker}, {profile}] {item['root']}")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    result = init_project(Path(args.project_root), args.profile, name=args.name, force=args.force)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"Initialized {result['project']} with profile {result['profile']}")
        print(f"Roadmap: {result['roadmap']}")
    return 0


def cmd_plan_goal(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    project_name = args.name or project_root.name
    goal_text = _goal_text_from_args(args)
    if args.materialize:
        result = materialize_goal_roadmap(
            project_root=project_root,
            project_name=project_name,
            profile=args.profile,
            goal_text=goal_text,
            blueprint_path=args.blueprint,
            constraints=args.constraint,
            desired_experience_kind=args.experience_kind,
            stage_count=args.stage_count,
            force=args.force,
        )
    else:
        result = plan_goal_roadmap(
            project_root=project_root,
            project_name=project_name,
            profile=args.profile,
            goal_text=goal_text,
            blueprint_path=args.blueprint,
            constraints=args.constraint,
            desired_experience_kind=args.experience_kind,
            stage_count=args.stage_count,
        )

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        action = "Materialized" if result["materialized"] else "Proposed"
        print(f"{action} starter roadmap for {result['project']} ({result['profile']})")
        print(f"Roadmap: {result['roadmap_path']}")
        print(f"Experience: {result['experience'].get('kind')}")
        milestones = result["roadmap"].get("milestones", [])
        continuation = result["roadmap"].get("continuation", {})
        stages = continuation.get("stages", []) if isinstance(continuation, dict) else []
        print(f"Baseline milestones: {len(milestones)}")
        print(f"Continuation stages: {len(stages)}")
        if not result["materialized"]:
            print("Run again with --materialize to write .engineering/roadmap.yaml.")
    return 0


def _goal_text_from_args(args: argparse.Namespace) -> str:
    if args.goal_file:
        return Path(args.goal_file).read_text(encoding="utf-8")
    return str(args.goal or "")


def cmd_status(args: argparse.Namespace) -> int:
    if getattr(args, "workspace", None) and not getattr(args, "project_root", None) and not getattr(args, "project", None):
        summaries = []
        for project in discover_projects(Path(args.workspace), max_depth=args.max_depth):
            if not project.configured:
                continue
            try:
                summaries.append(Harness(project.root, project.roadmap_path).status_summary())
            except Exception as exc:
                summaries.append({"project": project.name, "root": str(project.root), "error": str(exc)})
        if args.json:
            print(json.dumps(summaries, indent=2, sort_keys=True))
        else:
            for summary in summaries:
                if "error" in summary:
                    print(f"{summary['project']}: error - {summary['error']}")
                    continue
                next_task = summary.get("next_task")
                next_id = next_task["id"] if next_task else "none"
                manifest_count = summary.get("manifest_index", {}).get("manifest_count", 0)
                print(f"{summary['project']}: next={next_id} manifests={manifest_count} root={summary['root']}")
        return 0

    root = resolve_project_root(args)
    summary = Harness(root).status_summary()
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(f"Project: {summary['project']}")
        print(f"Profile: {summary.get('profile') or 'unknown'}")
        print(f"Root: {summary['root']}")
        print(f"Roadmap: {summary['roadmap']}")
        print(f"Run manifests: {summary.get('manifest_index', {}).get('manifest_count', 0)}")
        drive_control = summary.get("drive_control", {})
        approval_queue = summary.get("approval_queue", {})
        print(f"Drive control: {drive_control.get('status', 'idle')}")
        watchdog = drive_control.get("watchdog", {}) if isinstance(drive_control, dict) else {}
        if watchdog:
            print(f"Drive watchdog: {watchdog.get('status', 'idle')} - {watchdog.get('message', '')}")
        if drive_control.get("current_activity"):
            print(f"Drive activity: {drive_control.get('current_activity')}")
        if drive_control.get("current_task"):
            current_task = drive_control.get("current_task", {})
            print(f"Drive task: {current_task.get('id', 'unknown')}")
        if drive_control.get("last_progress_message"):
            print(f"Drive progress: {drive_control.get('last_progress_message')}")
        print(f"Pending approvals: {approval_queue.get('pending_count', 0)}")
        print(f"Stale approvals: {approval_queue.get('stale_count', 0)}")
        print("")
        for milestone in summary["milestones"]:
            print(
                f"- {milestone['id']}: {milestone['done']}/{milestone['total']} done, "
                f"{milestone['pending']} pending, {milestone['failed']} failed, {milestone['blocked']} blocked"
            )
        next_task = summary.get("next_task")
        print("")
        print(f"Next task: {next_task['id']} - {next_task['title']}" if next_task else "Next task: none")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    root = resolve_project_root(args)
    harness = Harness(root)
    payload = harness.validate_roadmap()
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Validation: {payload['status']}")
        print(f"Errors: {payload['error_count']}")
        for error in payload["errors"]:
            print(f"- error: {error}")
        print(f"Warnings: {payload['warning_count']}")
        for warning in payload["warnings"]:
            print(f"- warning: {warning}")
    return 0 if payload["status"] == "passed" else 1


def cmd_next(args: argparse.Namespace) -> int:
    root = resolve_project_root(args)
    harness = Harness(root)
    payload = harness.task_payload(harness.next_task())
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif payload is None:
        print("No pending task.")
    else:
        print(f"{payload['id']}: {payload['title']}")
        print(f"Milestone: {payload['milestone_id']}")
        print("Acceptance:")
        for command in payload["acceptance"]:
            print(f"- {command['name']}: {command['command']}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    root = resolve_project_root(args)
    harness = Harness(root)
    results = []
    seen_task_ids: set[str] = set()
    for _ in range(args.max_tasks):
        task = harness.task_by_id(args.task) if args.task else harness.next_task()
        if task is None or task.id in seen_task_ids:
            break
        seen_task_ids.add(task.id)
        result = harness.run_task(
            task,
            dry_run=args.dry_run,
            allow_live=args.allow_live,
            allow_manual=args.allow_manual,
            allow_agent=args.allow_agent,
        )
        maybe_checkpoint_task(harness, task, result, args, dry_run=args.dry_run)
        results.append(result)
        if args.task or args.dry_run or result["status"] not in COMPLETED_STATUSES:
            break
    if args.json:
        print(json.dumps(results, indent=2, sort_keys=True))
    else:
        if not results:
            print("No pending task.")
        for result in results:
            print(f"{result['task']['id']}: {result['status']} - {result['message']}")
            print(f"Report: {result['report']}")
    if any(result["status"] in {"failed", "blocked"} for result in results):
        return 1
    return 0


def maybe_checkpoint_task(
    harness: Harness,
    task,
    result: dict,
    args: argparse.Namespace,
    *,
    dry_run: bool = False,
    checkpoint_defer: dict | None = None,
) -> None:
    if dry_run or not checkpoint_requested(args) or result["status"] not in COMPLETED_STATUSES:
        return
    harness.drive_heartbeat(activity="git-checkpoint", message=f"checkpointing task {task.id}", task=task)
    if checkpoint_defer:
        result["git"] = harness.defer_git_checkpoint(
            task,
            reason=str(checkpoint_defer.get("message") or "task checkpoint deferred by roadmap materialization boundary"),
            metadata=checkpoint_defer,
        )
        return
    result["git"] = harness.git_checkpoint(
        task,
        push=bool(getattr(args, "push_after_task", False)),
        remote=str(getattr(args, "git_remote", "origin")),
        branch=getattr(args, "git_branch", None),
        message_template=str(getattr(args, "git_message_template", "chore(engineering): complete {task_id}")),
    )


def checkpoint_requested(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "commit_after_task", False) or getattr(args, "push_after_task", False))


def materialization_checkpoint_should_defer_tasks(checkpoint: dict) -> bool:
    if checkpoint.get("status") in {"committed"}:
        return bool(checkpoint.get("push") and checkpoint.get("push_status") == "failed")
    return checkpoint.get("status") in {"deferred", "failed"}


def materialization_task_checkpoint_defer_payload(checkpoint: dict) -> dict:
    reason = str(checkpoint.get("reason") or checkpoint.get("status") or "unknown")
    dirty_before = checkpoint.get("dirty_before_paths") or []
    unrelated = checkpoint.get("unrelated_dirty_paths") or []
    if reason == "preexisting_dirty_worktree":
        detail = "pre-existing user changes were present before roadmap materialization"
    elif reason == "unrelated_dirty_paths":
        detail = "unrelated dirty paths appeared beside the harness-owned roadmap materialization"
    elif reason == "git_push_failed":
        detail = "the roadmap materialization commit could not be pushed before generated tasks"
    else:
        detail = f"roadmap materialization checkpoint status was {checkpoint.get('status', 'unknown')}"
    if dirty_before:
        detail = f"{detail}: {', '.join(str(path) for path in dirty_before[:8])}"
    elif unrelated:
        detail = f"{detail}: {', '.join(str(path) for path in unrelated[:8])}"
    return {
        "message": f"task checkpoint deferred because roadmap materialization checkpoint was deferred: {detail}",
        "materialization_checkpoint_status": checkpoint.get("status"),
        "materialization_checkpoint_reason": checkpoint.get("reason"),
        "materialization_paths": checkpoint.get("materialization_paths", []),
        "dirty_before_paths": dirty_before,
        "unrelated_dirty_paths": unrelated,
        "materialization_checkpoint": checkpoint,
    }


def checkpoint_requested_payload(payload: dict) -> bool:
    if payload.get("checkpoint_requested"):
        return True
    if any("git" in result for result in payload.get("results", [])):
        return True
    return any("materialization_checkpoint_intent" in item for item in payload.get("continuations", []))


def write_drive_report(harness: Harness, payload: dict) -> str:
    report_dir = harness.report_dir / "drives"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{slug_now()}-drive.md"
    json_path = report_path.with_suffix(".json")
    payload["drive_report"] = str(report_path.relative_to(harness.project_root))
    payload["drive_report_json"] = str(json_path.relative_to(harness.project_root))
    final_status = payload.get("final_status") if isinstance(payload.get("final_status"), dict) else {}
    payload["approval_queue"] = (
        final_status.get("approval_queue")
        if isinstance(final_status.get("approval_queue"), dict)
        else payload.get("approval_queue", {})
    )
    payload["failure_isolation"] = harness.drive_failure_isolation_summary(
        payload,
        final_status=final_status if final_status else None,
    )
    lines = [
        "# Harness Drive Report",
        "",
        f"- Project: `{payload['project']}`",
        f"- Status: `{payload['status']}`",
        f"- Started: {payload['started_at']}",
        f"- Finished: {payload['finished_at']}",
        f"- Tasks run: {len(payload['results'])}",
        f"- Message: {payload['message']}",
        "",
        "## Task Results",
        "",
    ]
    if not payload["results"]:
        lines.append("No task was executed.")
    for result in payload["results"]:
        task = result["task"]
        lines.extend(
            [
                f"- `{task['id']}`: `{result['status']}` - {result['message']}",
                f"  - Report: `{result['report']}`",
            ]
        )
        if "git" in result:
            git = result["git"]
            lines.append(f"  - Git: `{git.get('status')}` - {git.get('message')}")
            if git.get("commit"):
                lines.append(f"  - Commit: `{git['commit']}`")
            if git.get("push_status"):
                lines.append(f"  - Push: `{git.get('push_status')}` `{git.get('push_remote')}/{git.get('push_branch')}`")
    continuations = payload.get("continuations", [])
    lines.extend(["", "## Continuations", ""])
    if not continuations:
        lines.append("No continuation was requested.")
    for item in continuations:
        lines.extend(
            [
                f"- `{item['status']}` - {item['message']}",
                f"  - Tasks added: `{item.get('tasks_added', 0)}`",
            ]
        )
        for milestone in item.get("milestones_added", []):
            lines.append(f"  - Milestone: `{milestone.get('id')}` {milestone.get('title')} ({milestone.get('tasks')} task(s))")
        intent = item.get("materialization_checkpoint_intent")
        if intent:
            lines.append(
                "  - Materialization checkpoint intent: "
                f"`{intent.get('status')}` - {intent.get('message')}"
            )
        checkpoint = item.get("materialization_checkpoint")
        if checkpoint:
            lines.append(
                "  - Materialization checkpoint: "
                f"`{checkpoint.get('status')}` - {checkpoint.get('message')}"
            )
            if checkpoint.get("reason"):
                lines.append(f"  - Materialization checkpoint reason: `{checkpoint.get('reason')}`")
            if checkpoint.get("commit"):
                lines.append(f"  - Materialization commit: `{checkpoint.get('commit')}`")
            if checkpoint.get("dirty_before_paths"):
                dirty = ", ".join(str(path) for path in checkpoint.get("dirty_before_paths", [])[:8])
                lines.append(f"  - Dirty before materialization: `{dirty}`")
    lines.extend(["", "## Checkpoint Boundaries", ""])
    if not checkpoint_requested_payload(payload):
        lines.append("Task checkpointing was not requested for this drive.")
    else:
        lines.append(
            "Rolling roadmap materialization is checkpointed before generated task execution when it can be "
            "isolated from pre-existing user changes."
        )
        deferred_results = [
            result
            for result in payload.get("results", [])
            if (result.get("git") or {}).get("status") == "deferred"
        ]
        if not deferred_results:
            lines.append("No task checkpoint deferral was recorded.")
        for result in deferred_results:
            task = result.get("task", {})
            git = result.get("git", {})
            lines.append(f"- Task `{task.get('id')}` checkpoint deferred: {git.get('message')}")
    self_iterations = payload.get("self_iterations", [])
    lines.extend(["", "## Self Iterations", ""])
    if not self_iterations:
        lines.append("No self-iteration planner was requested.")
    for item in self_iterations:
        lines.extend(
            [
                f"- `{item['status']}` - {item['message']}",
                f"  - Stages: `{item.get('stage_count_before')}` -> `{item.get('stage_count_after')}`",
                f"  - Pending stages after: `{item.get('pending_stage_count_after')}`",
            ]
        )
        if item.get("report"):
            lines.append(f"  - Report: `{item['report']}`")
    approval_queue = payload.get("approval_queue", {})
    lines.extend(["", "## Approval Leases", ""])
    if not approval_queue:
        lines.append("No approval queue state was recorded.")
    else:
        lines.extend(
            [
                f"- Pending approvals: `{approval_queue.get('pending_count', 0)}`",
                f"- Approved leases: `{approval_queue.get('approved_count', 0)}`",
                f"- Stale approvals: `{approval_queue.get('stale_count', 0)}`",
                f"- Lease TTL seconds: `{approval_queue.get('lease_ttl_seconds')}`",
                "",
            ]
        )
        stale_reasons = approval_queue.get("stale_reasons", {})
        if isinstance(stale_reasons, dict) and stale_reasons:
            lines.extend(["Stale reasons:", ""])
            for reason, count in sorted(stale_reasons.items()):
                lines.append(f"- `{count}` {reason}")
            lines.append("")
        lines.extend(
            [
                "Machine-readable approval queue:",
                "",
                "```json",
                json.dumps(approval_queue, indent=2, sort_keys=True),
                "```",
            ]
        )
    failure_isolation = payload.get("failure_isolation", {})
    lines.extend(["", "## Failure Isolation", ""])
    if not failure_isolation or int(failure_isolation.get("unresolved_count", 0) or 0) == 0:
        lines.append("No unresolved isolated task failure was recorded.")
    else:
        lines.append(f"Unresolved isolated failures: `{failure_isolation.get('unresolved_count', 0)}`")
        for item in failure_isolation.get("latest_isolated_failures", []):
            lines.append(
                "- "
                f"`{item.get('task_id')}` `{item.get('phase')}` `{item.get('failure_kind')}` - "
                f"{item.get('local_next_action')}"
            )
    lines.extend(
        [
            "",
            "Machine-readable failure isolation:",
            "",
            "```json",
            json.dumps(failure_isolation, indent=2, sort_keys=True),
            "```",
        ]
    )
    retrospective = payload.get("goal_gap_retrospective")
    lines.extend(["", "## Goal-Gap Retrospective", ""])
    if not retrospective:
        lines.append("No goal-gap retrospective was generated.")
    else:
        request = retrospective.get("request_self_iteration", {})
        recommendation = "yes" if request.get("recommended") else "no"
        lines.extend(
            [
                f"- Goal: {retrospective.get('goal')}",
                f"- Stop class: `{retrospective.get('trigger', {}).get('stop_class')}`",
                f"- Request self-iteration: `{recommendation}` - {request.get('reason')}",
                "",
                "Completed reliability capabilities:",
                "",
            ]
        )
        capabilities = retrospective.get("completed_reliability_capabilities", [])
        if not capabilities:
            lines.append("- None recorded.")
        for item in capabilities:
            lines.append(f"- `{item.get('id')}`: {item.get('title')} ({item.get('detail')})")
        lines.extend(["", "Remaining risks:", ""])
        risks = retrospective.get("remaining_risks", [])
        if not risks:
            lines.append("- None recorded.")
        for item in risks:
            lines.append(f"- `{item.get('severity')}` `{item.get('id')}`: {item.get('summary')}")
        lines.extend(["", "Likely next stage themes:", ""])
        themes = retrospective.get("likely_next_stage_themes", [])
        if not themes:
            lines.append("- None recorded.")
        for item in themes:
            sources = ", ".join(str(source) for source in item.get("source_risks", []))
            lines.append(f"- `{item.get('id')}`: {item.get('title')} ({sources})")
        lines.extend(
            [
                "",
                "Machine-readable retrospective:",
                "",
                "```json",
                json.dumps(retrospective, indent=2, sort_keys=True),
                "```",
            ]
        )
    lines.extend(["", "## Final Status", "", "```json", json.dumps(payload["final_status"], indent=2, sort_keys=True), "```", ""])
    report_path.write_text("\n".join(lines), encoding="utf-8")
    write_json(json_path, payload)
    return payload["drive_report"]


def cmd_advance(args: argparse.Namespace) -> int:
    root = resolve_project_root(args)
    harness = Harness(root)
    result = harness.advance_roadmap(max_new_milestones=args.max_new_milestones, reason=args.reason)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"Advance status: {result['status']} - {result['message']}")
        print(f"Tasks added: {result.get('tasks_added', 0)}")
        for milestone in result.get("milestones_added", []):
            print(f"- {milestone['id']}: {milestone['title']} ({milestone['tasks']} task(s))")
    return 0 if result["status"] in {"advanced", "exhausted", "disabled"} else 1


def cmd_frontend_tasks(args: argparse.Namespace) -> int:
    root = resolve_project_root(args)
    harness = Harness(root)
    if args.materialize:
        result = harness.materialize_frontend_tasks(milestone_id=args.milestone_id, reason=args.reason)
    else:
        result = harness.frontend_task_plan(milestone_id=args.milestone_id)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"Frontend tasks: {result['status']} - {result['message']}")
        experience = result.get("experience") or {}
        if experience:
            print(f"Experience: {experience.get('kind') or 'unknown'} ({experience.get('source') or 'unknown'})")
        milestone = result.get("milestone") or {}
        if milestone:
            print(f"Milestone: {milestone.get('id')} - {milestone.get('title')}")
        for task in result.get("tasks", []):
            print(f"- {task.get('id')}: {task.get('title')}")
            print(f"  file_scope: {', '.join(task.get('file_scope', []))}")
            print(f"  acceptance: {len(task.get('acceptance', []))} check(s)")
            print(f"  e2e: {len(task.get('e2e', []))} journey check(s)")
        if not args.materialize and result.get("status") == "proposed":
            print("Run again with --materialize to append the milestone to the roadmap.")
    return 0 if result["status"] in {"proposed", "materialized", "skipped"} else 1


def cmd_self_iterate(args: argparse.Namespace) -> int:
    root = resolve_project_root(args)
    harness = Harness(root)
    result = harness.run_self_iteration(
        reason=args.reason,
        allow_agent=args.allow_agent,
        allow_live=args.allow_live,
    )
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"Self-iteration status: {result['status']} - {result['message']}")
        print(f"Stages: {result.get('stage_count_before')} -> {result.get('stage_count_after')}")
        print(f"Pending stages after: {result.get('pending_stage_count_after')}")
        if result.get("report"):
            print(f"Report: {result['report']}")
    return 0 if result["status"] in {"planned", "disabled"} else 1


def cmd_pause(args: argparse.Namespace) -> int:
    root = resolve_project_root(args)
    payload = Harness(root).set_drive_control("pause", reason=args.reason)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Drive control: {payload['status']} - {payload['message']}")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    root = resolve_project_root(args)
    payload = Harness(root).set_drive_control("resume", reason=args.reason)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Drive control: {payload['status']} - {payload['message']}")
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    root = resolve_project_root(args)
    payload = Harness(root).set_drive_control("cancel", reason=args.reason)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Drive control: {payload['status']} - {payload['message']}")
    return 0


def cmd_approvals(args: argparse.Namespace) -> int:
    root = resolve_project_root(args)
    status_filter = None if args.all else args.status
    payload = Harness(root).approval_queue_summary(status_filter=status_filter)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            f"Approvals: {len(payload['items'])} shown, {payload['pending_count']} pending, "
            f"{payload.get('stale_count', 0)} stale"
        )
        for item in payload["items"]:
            detail = item.get("name") or item.get("phase") or item.get("decision_kind")
            stale = f" ({item.get('stale_reason')})" if item.get("status") == "stale" else ""
            print(
                f"- {item['id']}: {item.get('status')} {item.get('approval_kind')} "
                f"{item.get('task_id')} {detail} - {item.get('reason')}{stale}"
            )
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    root = resolve_project_root(args)
    harness = Harness(root)
    if args.all:
        payload = harness.approve_all_pending(approved_by=args.approved_by, reason=args.reason)
    else:
        if not args.approval_id:
            raise ValueError("Provide an approval id or use --all")
        payload = harness.approve_approval(args.approval_id, approved_by=args.approved_by, reason=args.reason)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Approval status: {payload['status']} - {payload['message']}")
    return 0 if payload["status"] in {"approved", "consumed"} else 1


def run_project_drive(root: Path, args: argparse.Namespace) -> tuple[int, dict]:
    root = root.resolve()
    harness = Harness(root)
    started_at = utc_now()
    start = harness.start_drive()
    if not start["started"]:
        final_status = harness.status_summary()
        payload = {
            "project": final_status["project"],
            "root": str(root),
            "status": start["status"],
            "message": start["message"],
            "started_at": started_at,
            "finished_at": utc_now(),
            "results": [],
            "continuations": [],
            "self_iterations": [],
            "final_status": final_status,
        }
        payload["goal_gap_retrospective"] = harness.drive_goal_gap_retrospective(payload, final_status=final_status)
        payload["drive_report"] = write_drive_report(harness, payload)
        return (0 if start["status"] == "paused" else 1), payload

    deadline = time.monotonic() + args.time_budget_seconds if args.time_budget_seconds else None
    harness.drive_heartbeat(activity="drive-loop", message="drive loop started", clear_task=True)
    results = []
    continuations = []
    self_iterations = []
    continuation_count = 0
    self_iteration_count = 0
    no_progress_count = 0
    task_checkpoint_defer: dict | None = None
    status = "completed"
    message = "No pending task."

    while True:
        harness.drive_heartbeat(activity="drive-loop", message="checking drive controls and budgets", clear_task=True)
        control = harness.drive_control_summary()
        if control.get("cancel_requested") or control.get("status") == "cancelled":
            status = "cancelled"
            message = "Drive cancelled by control state."
            break
        if control.get("pause_requested") or control.get("status") == "paused":
            status = "paused"
            message = "Drive paused by control state."
            break
        if deadline is not None and time.monotonic() >= deadline:
            status = "timeout"
            message = "Time budget expired."
            break
        if len(results) >= args.max_tasks:
            status = "budget_exhausted"
            message = f"Task budget exhausted after {args.max_tasks} task(s)."
            break
        harness.drive_heartbeat(activity="task-selection", message="selecting next roadmap task", clear_task=True)
        task = harness.next_task()
        if task is None:
            isolated_failures = harness.latest_isolated_failures_summary()
            if int(isolated_failures.get("unresolved_count", 0) or 0) > 0:
                status = "isolated_failure"
                message = "Unresolved isolated task failure exists; resolve it before adding continuation work."
                break
            if not args.rolling and not args.self_iterate:
                status = "completed"
                message = "Roadmap queue is empty."
                break
            continuation = None
            if args.rolling:
                if continuation_count >= args.max_continuations:
                    if not args.self_iterate:
                        status = "budget_exhausted"
                        message = f"Continuation budget exhausted after {args.max_continuations} continuation(s)."
                        break
                else:
                    harness.drive_heartbeat(
                        activity="continuation-materialization",
                        message="materializing continuation because roadmap queue is empty",
                        clear_task=True,
                    )
                    checkpoint_intent = None
                    if checkpoint_requested(args):
                        checkpoint_intent = harness.roadmap_materialization_checkpoint_intent(
                            reason="rolling_drive_queue_empty",
                            push=bool(getattr(args, "push_after_task", False)),
                            remote=str(getattr(args, "git_remote", "origin")),
                            branch=getattr(args, "git_branch", None),
                        )
                    continuation = harness.advance_roadmap(
                        max_new_milestones=args.continuation_batch_size,
                        reason="rolling_drive_queue_empty",
                    )
                    if checkpoint_intent is not None:
                        continuation["materialization_checkpoint_intent"] = checkpoint_intent
                        materialization_checkpoint = harness.git_checkpoint_roadmap_materialization(
                            checkpoint_intent,
                            continuation,
                            push=bool(getattr(args, "push_after_task", False)),
                            remote=str(getattr(args, "git_remote", "origin")),
                            branch=getattr(args, "git_branch", None),
                        )
                        continuation["materialization_checkpoint"] = materialization_checkpoint
                        if materialization_checkpoint_should_defer_tasks(materialization_checkpoint):
                            task_checkpoint_defer = materialization_task_checkpoint_defer_payload(
                                materialization_checkpoint
                            )
                    continuations.append(continuation)
                    if continuation["status"] == "advanced" and continuation.get("tasks_added", 0) > 0:
                        continuation_count += 1
                        no_progress_count = 0
                        harness = Harness(root)
                        continue
            if args.self_iterate:
                if self_iteration_count >= args.max_self_iterations:
                    status = "budget_exhausted"
                    message = f"Self-iteration budget exhausted after {args.max_self_iterations} iteration(s)."
                    break
                harness.drive_heartbeat(
                    activity="self-iteration",
                    message="running self-iteration because roadmap queue is empty",
                    clear_task=True,
                )
                iteration = harness.run_self_iteration(
                    reason="drive_queue_empty",
                    allow_agent=args.allow_agent,
                    allow_live=args.allow_live,
                )
                self_iterations.append(iteration)
                if iteration["status"] == "planned" and int(iteration.get("pending_stage_count_after", 0)) > 0:
                    self_iteration_count += 1
                    no_progress_count = 0
                    harness = Harness(root)
                    continue
                no_progress_count += 1
                if no_progress_count >= args.no_progress_limit:
                    status = "stalled"
                    message = f"No-progress limit reached after {no_progress_count} self-iteration attempt(s): {iteration['message']}"
                    break
                status = "stalled" if iteration["status"] not in {"disabled"} else "completed"
                message = iteration["message"]
                break
            if continuation is None:
                status = "completed"
                message = "Roadmap queue is empty."
                break
            no_progress_count += 1
            if continuation["status"] in {"disabled", "exhausted"}:
                status = "completed"
                message = continuation["message"]
                break
            if no_progress_count >= args.no_progress_limit:
                status = "stalled"
                message = f"No-progress limit reached after {no_progress_count} continuation attempt(s)."
                break
            status = "stalled"
            message = continuation["message"]
            break
        harness.drive_heartbeat(
            activity="task-execution",
            message=f"running task {task.id}",
            task=task,
        )
        result = harness.run_task(
            task,
            allow_live=args.allow_live,
            allow_manual=args.allow_manual,
            allow_agent=args.allow_agent,
        )
        maybe_checkpoint_task(harness, task, result, args, checkpoint_defer=task_checkpoint_defer)
        results.append(result)
        harness.drive_heartbeat(
            activity="task-execution",
            message=f"task {task.id} finished with status {result['status']}",
            task=task,
        )
        if result["status"] not in COMPLETED_STATUSES:
            status = result["status"]
            message = f"Stopped at task {task.id}: {result['message']}"
            break
        if args.stop_after_each:
            status = "paused"
            message = f"Stopped after task {task.id} because --stop-after-each was set."
            break

    harness.drive_heartbeat(activity="drive-finishing", message=message, clear_task=True)
    harness.finish_drive(status=status, message=message)
    final_status = harness.status_summary()
    payload = {
        "project": final_status["project"],
        "root": str(root),
        "status": status,
        "message": message,
        "started_at": started_at,
        "finished_at": utc_now(),
        "results": results,
        "continuations": continuations,
        "self_iterations": self_iterations,
        "checkpoint_requested": checkpoint_requested(args),
        "final_status": final_status,
    }
    payload["goal_gap_retrospective"] = harness.drive_goal_gap_retrospective(payload, final_status=final_status)
    payload["drive_report"] = write_drive_report(harness, payload)
    return (0 if status in {"completed", "paused", "budget_exhausted"} else 1), payload


def print_drive_payload(payload: dict, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print(f"Drive status: {payload['status']} - {payload['message']}")
    print(f"Tasks run: {len(payload.get('results', []))}")
    print(f"Continuations: {len(payload.get('continuations', []))}")
    print(f"Self-iterations: {len(payload.get('self_iterations', []))}")
    print(f"Drive report: {payload['drive_report']}")
    final_status = payload.get("final_status") if isinstance(payload.get("final_status"), dict) else {}
    next_task = final_status.get("next_task") if isinstance(final_status, dict) else None
    print(f"Next task: {next_task['id'] if next_task else 'none'}")


def cmd_drive(args: argparse.Namespace) -> int:
    root = resolve_project_root(args)
    exit_code, payload = run_project_drive(root, args)
    print_drive_payload(payload, json_output=args.json)
    return exit_code


def _workspace_drive_args(args: argparse.Namespace, project_root: Path) -> argparse.Namespace:
    return argparse.Namespace(
        project_root=project_root,
        workspace=args.workspace,
        project=None,
        max_depth=args.max_depth,
        json=False,
        max_tasks=args.max_tasks,
        time_budget_seconds=args.time_budget_seconds,
        rolling=args.rolling,
        self_iterate=args.self_iterate,
        max_continuations=args.max_continuations,
        max_self_iterations=args.max_self_iterations,
        continuation_batch_size=args.continuation_batch_size,
        no_progress_limit=args.no_progress_limit,
        allow_live=args.allow_live,
        allow_manual=args.allow_manual,
        allow_agent=args.allow_agent,
        stop_after_each=False,
        commit_after_task=False,
        push_after_task=False,
        git_remote="origin",
        git_branch=None,
        git_message_template="chore(engineering): complete {task_id}",
    )


def _path_within(path: Path, parent: Path) -> bool:
    try:
        return path.resolve().is_relative_to(parent.resolve())
    except OSError:
        return False


def _dispatch_skip(code: str, message: str, **evidence) -> dict:
    payload = {"code": code, "message": message}
    payload.update({key: value for key, value in evidence.items() if value is not None})
    return payload


def _add_dispatch_skip(item: dict, code: str, message: str, **evidence) -> None:
    item.setdefault("skip_reasons", []).append(_dispatch_skip(code, message, **evidence))


def _workspace_task_counts(summary: dict) -> dict:
    counts = {"total": 0, "done": 0, "blocked": 0, "failed": 0, "pending": 0}
    for milestone in summary.get("milestones", []):
        if not isinstance(milestone, dict):
            continue
        for key in counts:
            counts[key] += int(milestone.get(key, 0) or 0)
    return counts


def _compact_workspace_status_summary(summary: dict) -> dict:
    drive_control = summary.get("drive_control") if isinstance(summary.get("drive_control"), dict) else {}
    watchdog = drive_control.get("watchdog") if isinstance(drive_control.get("watchdog"), dict) else {}
    approval_queue = summary.get("approval_queue") if isinstance(summary.get("approval_queue"), dict) else {}
    failure_isolation = summary.get("failure_isolation") if isinstance(summary.get("failure_isolation"), dict) else {}
    next_task = summary.get("next_task") if isinstance(summary.get("next_task"), dict) else None
    return {
        "project": summary.get("project"),
        "profile": summary.get("profile"),
        "root": summary.get("root"),
        "roadmap": summary.get("roadmap"),
        "task_counts": _workspace_task_counts(summary),
        "next_task": (
            {
                "id": next_task.get("id"),
                "title": next_task.get("title"),
                "milestone_id": next_task.get("milestone_id"),
            }
            if next_task
            else None
        ),
        "drive_control": {
            "status": drive_control.get("status", "idle"),
            "active": bool(drive_control.get("active", False)),
            "pause_requested": bool(drive_control.get("pause_requested", False)),
            "cancel_requested": bool(drive_control.get("cancel_requested", False)),
            "stale": bool(drive_control.get("stale", False)),
            "stale_reason": drive_control.get("stale_reason") or watchdog.get("reason"),
            "watchdog_status": watchdog.get("status"),
            "watchdog_message": watchdog.get("message"),
        },
        "approval_queue": {
            "pending_count": int(approval_queue.get("pending_count", 0) or 0),
            "stale_count": int(approval_queue.get("stale_count", 0) or 0),
        },
        "failure_isolation": {
            "unresolved_count": int(failure_isolation.get("unresolved_count", 0) or 0),
            "has_unresolved": bool(failure_isolation.get("has_unresolved", False)),
            "latest_isolated_failures": failure_isolation.get("latest_isolated_failures", []),
        },
    }


def _workspace_dispatch_queue_item(workspace: Path, project, args: argparse.Namespace, index: int) -> dict:
    item = {
        "index": index,
        "project": project.name,
        "root": str(project.root),
        "roadmap": str(project.roadmap_path) if project.roadmap_path else None,
        "profile": project.profile,
        "kind": project.kind,
        "configured": project.configured,
        "eligible": False,
        "selected": False,
        "dispatch_status": "skipped",
        "skip_reasons": [],
    }
    if not _path_within(project.root, workspace):
        _add_dispatch_skip(
            item,
            "outside_local_scope",
            "project root resolves outside the requested workspace",
            workspace=str(workspace),
        )
        return item
    if project.roadmap_path is not None and not _path_within(project.roadmap_path, project.root):
        _add_dispatch_skip(
            item,
            "outside_local_scope",
            "roadmap path resolves outside the project root",
            workspace=str(workspace),
        )
        return item
    if not project.configured or project.roadmap_path is None:
        _add_dispatch_skip(item, "missing_roadmap", "project has no engineering roadmap")
        return item

    try:
        harness = Harness(project.root, project.roadmap_path)
        validation = harness.validate_roadmap()
    except Exception as exc:
        _add_dispatch_skip(item, "invalid_roadmap", f"roadmap could not be loaded: {exc}")
        return item
    item["roadmap_validation"] = validation
    if validation.get("status") != "passed":
        _add_dispatch_skip(
            item,
            "invalid_roadmap",
            "roadmap validation failed",
            errors=validation.get("errors", []),
        )
        return item

    summary = harness.status_summary(refresh_approvals=False)
    compact_summary = _compact_workspace_status_summary(summary)
    item["summary"] = compact_summary
    drive_control = compact_summary["drive_control"]
    if drive_control.get("pause_requested") or drive_control.get("status") == "paused":
        _add_dispatch_skip(item, "paused", "drive is paused for this project")
    if drive_control.get("cancel_requested") or drive_control.get("status") == "cancelled":
        _add_dispatch_skip(item, "cancelled", "drive is cancelled for this project")
    if drive_control.get("stale") or drive_control.get("status") == "stale":
        _add_dispatch_skip(
            item,
            "stale_running",
            "drive control is stale and must be resumed before dispatch",
            stale_reason=drive_control.get("stale_reason"),
        )
    elif drive_control.get("active") or drive_control.get("status") == "running":
        _add_dispatch_skip(item, "already_running", "a drive is already running for this project")

    pending_approvals = int(compact_summary["approval_queue"].get("pending_count", 0) or 0)
    if pending_approvals > 0:
        _add_dispatch_skip(
            item,
            "waiting_on_approvals",
            "project has pending approval gates",
            pending_count=pending_approvals,
        )

    unresolved_failures = int(compact_summary["failure_isolation"].get("unresolved_count", 0) or 0)
    if unresolved_failures > 0:
        _add_dispatch_skip(
            item,
            "unresolved_isolated_failures",
            "project has unresolved isolated task failures",
            unresolved_count=unresolved_failures,
        )

    if compact_summary.get("next_task") is None and not args.rolling and not args.self_iterate:
        _add_dispatch_skip(item, "no_pending_task", "project has no pending roadmap task")

    item["eligible"] = not item["skip_reasons"]
    item["dispatch_status"] = "eligible" if item["eligible"] else "skipped"
    return item


def build_workspace_dispatch_queue(workspace: Path, args: argparse.Namespace) -> list[dict]:
    workspace = workspace.resolve()
    projects = discover_projects(workspace, max_depth=args.max_depth)
    return [
        _workspace_dispatch_queue_item(workspace, project, args, index)
        for index, project in enumerate(projects)
    ]


def _workspace_dispatch_report_path(workspace: Path) -> Path:
    report_dir = workspace / ".engineering" / "reports" / "workspace-dispatches"
    report_dir.mkdir(parents=True, exist_ok=True)
    base = report_dir / f"{slug_now()}-workspace-dispatch.md"
    candidate = base
    counter = 2
    while candidate.exists() or candidate.with_suffix(".json").exists():
        candidate = base.with_name(f"{base.stem}_{counter}{base.suffix}")
        counter += 1
    return candidate


def _workspace_relative(workspace: Path, path: Path) -> str:
    try:
        return str(path.relative_to(workspace))
    except ValueError:
        return str(path)


def _workspace_dispatch_limits(args: argparse.Namespace) -> dict:
    return {
        "max_tasks": args.max_tasks,
        "time_budget_seconds": args.time_budget_seconds,
        "rolling": bool(args.rolling),
        "self_iterate": bool(args.self_iterate),
        "max_continuations": args.max_continuations,
        "max_self_iterations": args.max_self_iterations,
        "continuation_batch_size": args.continuation_batch_size,
        "no_progress_limit": args.no_progress_limit,
        "allow_live": bool(args.allow_live),
        "allow_manual": bool(args.allow_manual),
        "allow_agent": bool(args.allow_agent),
        "push_after_task": False,
        "commit_after_task": False,
    }


def _coerce_positive_int(value, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _workspace_dispatch_lease_stale_after_seconds(args: argparse.Namespace) -> int:
    cli_value = getattr(args, "lease_stale_after_seconds", None)
    if cli_value is not None:
        return _coerce_positive_int(cli_value, DEFAULT_WORKSPACE_DISPATCH_LEASE_STALE_SECONDS)
    return _coerce_positive_int(
        os.environ.get(WORKSPACE_DISPATCH_LEASE_STALE_SECONDS_ENV),
        DEFAULT_WORKSPACE_DISPATCH_LEASE_STALE_SECONDS,
    )


def _workspace_dispatch_lease_dir(workspace: Path) -> Path:
    return workspace / ".engineering" / "state" / WORKSPACE_DISPATCH_LEASE_DIRNAME


def _workspace_dispatch_lease_path(workspace: Path) -> Path:
    return _workspace_dispatch_lease_dir(workspace) / "lease.json"


def _workspace_dispatch_command_options(workspace: Path, args: argparse.Namespace) -> dict:
    payload = {
        "workspace": str(workspace),
        "max_depth": args.max_depth,
        "lease_stale_after_seconds": _workspace_dispatch_lease_stale_after_seconds(args),
    }
    payload.update(_workspace_dispatch_limits(args))
    return payload


def _workspace_dispatch_owner_pid(value) -> int | None:
    try:
        pid = int(value)
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _workspace_dispatch_process_is_running(pid: int | None) -> bool | None:
    if pid is None:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _workspace_dispatch_timestamp_age_seconds(value) -> int | None:
    parsed = parse_utc_timestamp(value)
    if parsed is None:
        return None
    return max(0, int((datetime.now(timezone.utc) - parsed).total_seconds()))


def _write_workspace_dispatch_lease(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp_path, path)


def _read_workspace_dispatch_lease(path: Path) -> dict | None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _workspace_dispatch_lease_snapshot(lease: dict | None) -> dict | None:
    if not isinstance(lease, dict):
        return None
    keys = [
        "schema_version",
        "kind",
        "status",
        "workspace",
        "owner_pid",
        "started_at",
        "last_heartbeat_at",
        "heartbeat_count",
        "selected_project",
        "command_options",
        "stale_after_seconds",
        "current_activity",
    ]
    return {key: lease.get(key) for key in keys if key in lease}


def _workspace_dispatch_lease_assessment(
    lease_dir: Path,
    lease_path: Path,
    *,
    stale_after_seconds: int,
) -> dict:
    checked_at = utc_now()
    lease = _read_workspace_dispatch_lease(lease_path)
    if lease is None:
        try:
            age_seconds = max(0, int(time.time() - lease_dir.stat().st_mtime))
        except OSError:
            age_seconds = None
        stale = age_seconds is not None and age_seconds > stale_after_seconds
        return {
            "schema_version": WORKSPACE_DISPATCH_LEASE_SCHEMA_VERSION,
            "status": "stale" if stale else "held",
            "stale": stale,
            "reason": "missing_lease_metadata" if stale else None,
            "message": (
                "workspace dispatch lease metadata is missing"
                if stale
                else "workspace dispatch lease metadata is being created"
            ),
            "checked_at": checked_at,
            "threshold_seconds": stale_after_seconds,
            "pid": None,
            "pid_alive": None,
            "heartbeat_at": None,
            "heartbeat_age_seconds": age_seconds,
            "holder": None,
        }

    threshold = _coerce_positive_int(lease.get("stale_after_seconds"), stale_after_seconds)
    pid = _workspace_dispatch_owner_pid(lease.get("owner_pid"))
    pid_alive = _workspace_dispatch_process_is_running(pid)
    heartbeat_at = lease.get("last_heartbeat_at")
    heartbeat_age_seconds = _workspace_dispatch_timestamp_age_seconds(heartbeat_at)
    stale = False
    reason = None
    message = "workspace dispatch lease is held by a live process with a fresh heartbeat"
    if pid is None:
        stale = True
        reason = "missing_pid"
        message = "workspace dispatch lease has no owner pid"
    elif pid_alive is False:
        stale = True
        reason = "pid_gone"
        message = f"workspace dispatch lease owner pid is not running: {pid}"
    elif heartbeat_age_seconds is None:
        stale = True
        reason = "missing_heartbeat"
        message = "workspace dispatch lease has no heartbeat"
    elif heartbeat_age_seconds > threshold:
        stale = True
        reason = "heartbeat_stale"
        message = f"workspace dispatch lease heartbeat is stale after {heartbeat_age_seconds}s"
    return {
        "schema_version": WORKSPACE_DISPATCH_LEASE_SCHEMA_VERSION,
        "status": "stale" if stale else "held",
        "stale": stale,
        "reason": reason,
        "message": message,
        "checked_at": checked_at,
        "threshold_seconds": threshold,
        "pid": pid,
        "pid_alive": pid_alive,
        "heartbeat_at": heartbeat_at,
        "heartbeat_age_seconds": heartbeat_age_seconds,
        "holder": _workspace_dispatch_lease_snapshot(lease),
    }


def _recover_workspace_dispatch_lease_dir(lease_dir: Path) -> dict:
    recovered_at = utc_now()
    recovery_dir = lease_dir.with_name(
        f"{lease_dir.name}.recovered-{os.getpid()}-{int(time.time() * 1_000_000)}"
    )
    try:
        os.rename(lease_dir, recovery_dir)
    except FileNotFoundError:
        return {
            "recovered": True,
            "recovered_at": recovered_at,
            "message": "workspace dispatch lease disappeared before recovery",
        }
    except OSError as exc:
        return {
            "recovered": False,
            "recovered_at": recovered_at,
            "message": f"could not recover stale workspace dispatch lease: {exc}",
        }
    shutil.rmtree(recovery_dir, ignore_errors=True)
    return {
        "recovered": True,
        "recovered_at": recovered_at,
        "message": "stale workspace dispatch lease was recovered",
    }


def _new_workspace_dispatch_lease(
    workspace: Path,
    args: argparse.Namespace,
    *,
    stale_after_seconds: int,
    recovery: dict | None,
) -> dict:
    now = utc_now()
    payload = {
        "schema_version": WORKSPACE_DISPATCH_LEASE_SCHEMA_VERSION,
        "kind": "engineering-harness.workspace-dispatch-lease",
        "status": "running",
        "workspace": str(workspace),
        "owner_pid": os.getpid(),
        "started_at": now,
        "last_heartbeat_at": now,
        "heartbeat_count": 1,
        "selected_project": None,
        "command_options": _workspace_dispatch_command_options(workspace, args),
        "stale_after_seconds": stale_after_seconds,
        "current_activity": "workspace-dispatch-starting",
    }
    if recovery:
        payload["recovered_from"] = recovery
    return payload


def acquire_workspace_dispatch_lease(workspace: Path, args: argparse.Namespace) -> dict:
    lease_dir = _workspace_dispatch_lease_dir(workspace)
    lease_path = _workspace_dispatch_lease_path(workspace)
    lease_dir.parent.mkdir(parents=True, exist_ok=True)
    stale_after_seconds = _workspace_dispatch_lease_stale_after_seconds(args)
    recovery: dict | None = None
    for _attempt in range(10):
        try:
            os.mkdir(lease_dir)
        except FileExistsError:
            assessment = _workspace_dispatch_lease_assessment(
                lease_dir,
                lease_path,
                stale_after_seconds=stale_after_seconds,
            )
            if not assessment.get("stale"):
                return {
                    "acquired": False,
                    "status": "held",
                    "lease_dir": lease_dir,
                    "lease_path": lease_path,
                    "assessment": assessment,
                }
            recovered = _recover_workspace_dispatch_lease_dir(lease_dir)
            recovery = {
                "reason": assessment.get("reason"),
                "message": assessment.get("message"),
                "assessment": assessment,
                "recovery": recovered,
            }
            if recovered.get("recovered"):
                continue
            return {
                "acquired": False,
                "status": "recovery_failed",
                "lease_dir": lease_dir,
                "lease_path": lease_path,
                "assessment": assessment,
                "recovery": recovery,
            }
        else:
            lease = _new_workspace_dispatch_lease(
                workspace,
                args,
                stale_after_seconds=stale_after_seconds,
                recovery=recovery,
            )
            _write_workspace_dispatch_lease(lease_path, lease)
            return {
                "acquired": True,
                "status": "acquired",
                "lease_dir": lease_dir,
                "lease_path": lease_path,
                "lease": lease,
                "recovery": recovery,
                "_lock": threading.Lock(),
            }
    return {
        "acquired": False,
        "status": "acquire_raced",
        "lease_dir": lease_dir,
        "lease_path": lease_path,
        "assessment": _workspace_dispatch_lease_assessment(
            lease_dir,
            lease_path,
            stale_after_seconds=stale_after_seconds,
        ),
    }


def _workspace_dispatch_lease_matches(lease: dict | None, acquisition: dict) -> bool:
    expected = acquisition.get("lease") if isinstance(acquisition.get("lease"), dict) else {}
    return (
        isinstance(lease, dict)
        and lease.get("owner_pid") == expected.get("owner_pid")
        and lease.get("started_at") == expected.get("started_at")
    )


def heartbeat_workspace_dispatch_lease(
    acquisition: dict,
    *,
    activity: str,
    selected_project: dict | None = None,
) -> dict | None:
    if not acquisition.get("acquired"):
        return None
    lock = acquisition.get("_lock")
    if lock is None:
        lock = threading.Lock()
        acquisition["_lock"] = lock
    with lock:
        lease_path = acquisition["lease_path"]
        current = _read_workspace_dispatch_lease(lease_path)
        if not _workspace_dispatch_lease_matches(current, acquisition):
            return None
        now = utc_now()
        current["last_heartbeat_at"] = now
        current["heartbeat_count"] = int(current.get("heartbeat_count", 0) or 0) + 1
        current["current_activity"] = activity
        if selected_project is not None:
            current["selected_project"] = selected_project
        _write_workspace_dispatch_lease(lease_path, current)
        acquisition["lease"] = current
        return current


def start_workspace_dispatch_lease_heartbeat(acquisition: dict) -> tuple[threading.Event, threading.Thread] | None:
    if not acquisition.get("acquired"):
        return None
    lease = acquisition.get("lease") if isinstance(acquisition.get("lease"), dict) else {}
    stale_after_seconds = _coerce_positive_int(
        lease.get("stale_after_seconds"),
        DEFAULT_WORKSPACE_DISPATCH_LEASE_STALE_SECONDS,
    )
    interval = max(1, min(30, stale_after_seconds // 3 if stale_after_seconds > 3 else 1))
    stop_event = threading.Event()

    def run_heartbeat() -> None:
        while not stop_event.wait(interval):
            heartbeat_workspace_dispatch_lease(acquisition, activity="workspace-dispatch-running")

    thread = threading.Thread(target=run_heartbeat, name="workspace-dispatch-lease-heartbeat", daemon=True)
    thread.start()
    return stop_event, thread


def release_workspace_dispatch_lease(acquisition: dict) -> dict:
    released_at = utc_now()
    if not acquisition.get("acquired"):
        return {
            "status": "not_acquired",
            "released_at": released_at,
            "message": "workspace dispatch lease was not acquired by this process",
        }
    lock = acquisition.get("_lock")
    if lock is None:
        lock = threading.Lock()
        acquisition["_lock"] = lock
    with lock:
        lease_dir = acquisition["lease_dir"]
        lease_path = acquisition["lease_path"]
        current = _read_workspace_dispatch_lease(lease_path)
        if current is None:
            if not lease_dir.exists():
                return {
                    "status": "already_released",
                    "released_at": released_at,
                    "message": "workspace dispatch lease was already released",
                }
            return {
                "status": "missing_metadata",
                "released_at": released_at,
                "message": "workspace dispatch lease metadata is missing; release left directory in place",
            }
        if not _workspace_dispatch_lease_matches(current, acquisition):
            return {
                "status": "not_owner",
                "released_at": released_at,
                "message": "workspace dispatch lease is now owned by another process",
                "current_holder": _workspace_dispatch_lease_snapshot(current),
            }
        shutil.rmtree(lease_dir, ignore_errors=True)
        return {
            "status": "released",
            "released_at": released_at,
            "message": "workspace dispatch lease released",
        }


def workspace_dispatch_lease_payload(
    workspace: Path,
    acquisition: dict,
    *,
    release: dict | None = None,
) -> dict:
    lease_path = acquisition.get("lease_path")
    path = _workspace_relative(workspace, lease_path) if isinstance(lease_path, Path) else None
    if not acquisition.get("acquired"):
        return {
            "schema_version": WORKSPACE_DISPATCH_LEASE_SCHEMA_VERSION,
            "status": acquisition.get("status", "held"),
            "acquired": False,
            "path": path,
            "assessment": acquisition.get("assessment"),
            "recovery": acquisition.get("recovery"),
        }
    lease = acquisition.get("lease") if isinstance(acquisition.get("lease"), dict) else {}
    return {
        "schema_version": WORKSPACE_DISPATCH_LEASE_SCHEMA_VERSION,
        "status": (release or {}).get("status") or acquisition.get("status", "acquired"),
        "acquired": True,
        "path": path,
        "owner_pid": lease.get("owner_pid"),
        "started_at": lease.get("started_at"),
        "last_heartbeat_at": lease.get("last_heartbeat_at"),
        "heartbeat_count": lease.get("heartbeat_count"),
        "selected_project": lease.get("selected_project"),
        "command_options": lease.get("command_options"),
        "stale_after_seconds": lease.get("stale_after_seconds"),
        "current_activity": lease.get("current_activity"),
        "recovered": bool(acquisition.get("recovery")),
        "recovery": acquisition.get("recovery"),
        "release": release,
    }


def write_workspace_dispatch_report(workspace: Path, payload: dict) -> str:
    report_path = _workspace_dispatch_report_path(workspace)
    json_path = report_path.with_suffix(".json")
    payload["dispatch_report"] = _workspace_relative(workspace, report_path)
    payload["dispatch_report_json"] = _workspace_relative(workspace, json_path)
    lines = [
        "# Workspace Drive Dispatch Report",
        "",
        f"- Workspace: `{payload['workspace']}`",
        f"- Status: `{payload['status']}`",
        f"- Started: {payload['started_at']}",
        f"- Finished: {payload['finished_at']}",
        f"- Projects scanned: `{len(payload.get('queue', []))}`",
        f"- Eligible projects: `{payload.get('eligible_count', 0)}`",
        f"- Selected project: `{(payload.get('selected') or {}).get('project') or 'none'}`",
        f"- Message: {payload['message']}",
        "",
        "## Workspace Dispatch Lease",
        "",
    ]
    lease = payload.get("lease") if isinstance(payload.get("lease"), dict) else {}
    if not lease:
        lines.append("No workspace dispatch lease evidence was recorded.")
    else:
        lines.extend(
            [
                f"- Status: `{lease.get('status')}`",
                f"- Acquired: `{str(bool(lease.get('acquired'))).lower()}`",
                f"- Path: `{lease.get('path')}`",
                f"- Owner pid: `{lease.get('owner_pid') or 'unknown'}`",
                f"- Stale after seconds: `{lease.get('stale_after_seconds') or 'unknown'}`",
            ]
        )
        selected_project = lease.get("selected_project")
        if isinstance(selected_project, dict):
            lines.append(f"- Lease selected project: `{selected_project.get('project')}`")
        assessment = lease.get("assessment") if isinstance(lease.get("assessment"), dict) else {}
        if assessment.get("reason"):
            lines.append(f"- Lease reason: `{assessment.get('reason')}`")
        release = lease.get("release") if isinstance(lease.get("release"), dict) else {}
        if release.get("released_at"):
            lines.append(f"- Released: {release.get('released_at')}")
        lines.extend(
            [
                "",
                "Machine-readable lease:",
                "",
                "```json",
                json.dumps(lease, indent=2, sort_keys=True),
                "```",
                "",
            ]
        )
    lines.extend(
        [
        "## Dispatch Queue",
        "",
        ]
    )
    if not payload.get("queue"):
        lines.append("No projects were discovered.")
    for item in payload.get("queue", []):
        lines.append(
            f"- `{item.get('index')}` `{item.get('project')}` `{item.get('dispatch_status')}` "
            f"eligible=`{str(bool(item.get('eligible'))).lower()}` root=`{item.get('root')}`"
        )
        summary = item.get("summary") if isinstance(item.get("summary"), dict) else {}
        next_task = summary.get("next_task") if isinstance(summary.get("next_task"), dict) else None
        if next_task:
            lines.append(f"  - Next task: `{next_task.get('id')}` {next_task.get('title')}")
        for reason in item.get("skip_reasons", []):
            lines.append(f"  - Skip `{reason.get('code')}`: {reason.get('message')}")

    drive = payload.get("drive") if isinstance(payload.get("drive"), dict) else None
    lines.extend(["", "## Selected Drive", ""])
    if not drive:
        lines.append("No project drive was started.")
    else:
        lines.extend(
            [
                f"- Status: `{drive.get('status')}`",
                f"- Tasks run: `{len(drive.get('results', []))}`",
                f"- Continuations: `{len(drive.get('continuations', []))}`",
                f"- Drive report: `{drive.get('drive_report')}`",
            ]
        )

    machine_payload = {key: value for key, value in payload.items() if key != "drive"}
    if drive:
        machine_payload["drive"] = {
            "project": drive.get("project"),
            "root": drive.get("root"),
            "status": drive.get("status"),
            "message": drive.get("message"),
            "drive_report": drive.get("drive_report"),
            "drive_report_json": drive.get("drive_report_json"),
            "result_count": len(drive.get("results", [])),
            "continuation_count": len(drive.get("continuations", [])),
        }
    lines.extend(
        [
            "",
            "## Machine-Readable Dispatch",
            "",
            "```json",
            json.dumps(machine_payload, indent=2, sort_keys=True),
            "```",
            "",
        ]
    )
    report_path.write_text("\n".join(lines), encoding="utf-8")
    write_json(json_path, payload)
    return payload["dispatch_report"]


def workspace_drive_dispatch(args: argparse.Namespace) -> tuple[int, dict]:
    workspace = Path(args.workspace).resolve()
    started_at = utc_now()
    acquisition = acquire_workspace_dispatch_lease(workspace, args)
    if not acquisition.get("acquired"):
        status = "lease_held" if acquisition.get("status") == "held" else "lease_unavailable"
        assessment = acquisition.get("assessment") if isinstance(acquisition.get("assessment"), dict) else {}
        message = assessment.get("message") or "workspace dispatch lease is not available"
        payload = {
            "schema_version": 1,
            "kind": "engineering-harness.workspace-drive-dispatch",
            "workspace": str(workspace),
            "status": status,
            "message": message,
            "started_at": started_at,
            "finished_at": utc_now(),
            "limits": _workspace_dispatch_limits(args),
            "queue": [],
            "eligible_count": 0,
            "skipped_count": 0,
            "selected": None,
            "drive": None,
            "lease": workspace_dispatch_lease_payload(workspace, acquisition),
        }
        write_workspace_dispatch_report(workspace, payload)
        return 1, payload

    heartbeat = start_workspace_dispatch_lease_heartbeat(acquisition)
    payload: dict | None = None
    drive_exit_code = 0
    try:
        heartbeat_workspace_dispatch_lease(acquisition, activity="workspace-dispatch-scanning")
        queue = build_workspace_dispatch_queue(workspace, args)
        heartbeat_workspace_dispatch_lease(acquisition, activity="workspace-dispatch-queue-built")
        selected = next((item for item in queue if item.get("eligible")), None)
        drive_payload = None
        if selected is None:
            status = "no_eligible_project"
            message = "No eligible project drive was found."
        else:
            selected["selected"] = True
            selected["dispatch_status"] = "dispatched"
            selected_project = {
                "project": selected.get("project"),
                "root": selected.get("root"),
                "queue_index": selected.get("index"),
            }
            heartbeat_workspace_dispatch_lease(
                acquisition,
                activity="workspace-dispatch-selected",
                selected_project=selected_project,
            )
            for item in queue:
                if item is selected or not item.get("eligible"):
                    continue
                item["dispatch_status"] = "skipped"
                _add_dispatch_skip(
                    item,
                    "one_project_per_invocation",
                    "another eligible project was selected first in deterministic queue order",
                    selected_project=selected.get("project"),
                    selected_root=selected.get("root"),
                )
            drive_args = _workspace_drive_args(args, Path(str(selected["root"])))
            heartbeat_workspace_dispatch_lease(acquisition, activity="workspace-dispatch-driving")
            drive_exit_code, drive_payload = run_project_drive(Path(str(selected["root"])), drive_args)
            heartbeat_workspace_dispatch_lease(acquisition, activity="workspace-dispatch-drive-finished")
            selected["drive_exit_code"] = drive_exit_code
            selected["drive_status"] = drive_payload.get("status")
            selected["drive_report"] = drive_payload.get("drive_report")
            selected["drive_report_json"] = drive_payload.get("drive_report_json")
            status = "dispatched" if drive_exit_code == 0 else "drive_failed"
            message = f"Dispatched project {selected['project']}: {drive_payload.get('status')}"

        eligible_count = sum(1 for item in queue if item.get("eligible"))
        skipped_count = sum(1 for item in queue if item.get("dispatch_status") == "skipped")
        payload = {
            "schema_version": 1,
            "kind": "engineering-harness.workspace-drive-dispatch",
            "workspace": str(workspace),
            "status": status,
            "message": message,
            "started_at": started_at,
            "finished_at": utc_now(),
            "limits": _workspace_dispatch_limits(args),
            "queue": queue,
            "eligible_count": eligible_count,
            "skipped_count": skipped_count,
            "selected": (
                {
                    "project": selected.get("project"),
                    "root": selected.get("root"),
                    "queue_index": selected.get("index"),
                    "drive_status": selected.get("drive_status"),
                    "drive_exit_code": selected.get("drive_exit_code"),
                    "drive_report": selected.get("drive_report"),
                    "drive_report_json": selected.get("drive_report_json"),
                }
                if selected
                else None
            ),
            "drive": drive_payload,
        }
        return drive_exit_code, payload
    finally:
        if heartbeat is not None:
            stop_event, thread = heartbeat
            stop_event.set()
            thread.join(timeout=1)
        release = release_workspace_dispatch_lease(acquisition)
        if payload is not None:
            payload["finished_at"] = utc_now()
            payload["lease"] = workspace_dispatch_lease_payload(workspace, acquisition, release=release)
            write_workspace_dispatch_report(workspace, payload)


def cmd_workspace_drive(args: argparse.Namespace) -> int:
    if args.max_tasks < 1:
        raise ValueError("--max-tasks must be at least 1")
    if args.max_continuations < 0 or args.max_self_iterations < 0:
        raise ValueError("--max-continuations and --max-self-iterations must be non-negative")
    if args.lease_stale_after_seconds is not None and args.lease_stale_after_seconds < 1:
        raise ValueError("--lease-stale-after-seconds must be at least 1")
    exit_code, payload = workspace_drive_dispatch(args)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Workspace dispatch: {payload['status']} - {payload['message']}")
        print(f"Projects scanned: {len(payload.get('queue', []))}")
        print(f"Eligible projects: {payload.get('eligible_count', 0)}")
        selected = payload.get("selected") or {}
        print(f"Selected project: {selected.get('project') or 'none'}")
        print(f"Dispatch report: {payload['dispatch_report']}")
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Goal-driven engineering harness")
    subparsers = parser.add_subparsers(dest="command", required=True)

    profiles = subparsers.add_parser("profiles", help="List built-in project profiles")
    profiles.add_argument("--json", action="store_true")
    profiles.set_defaults(func=cmd_profiles)

    scan = subparsers.add_parser("scan", help="Discover projects in a workspace")
    scan.add_argument("--workspace", type=Path, default=Path.cwd())
    scan.add_argument("--max-depth", type=int, default=3)
    scan.add_argument("--json", action="store_true")
    scan.set_defaults(func=cmd_scan)

    init = subparsers.add_parser("init", help="Initialize .engineering in a project")
    init.add_argument("--project-root", type=Path, required=True)
    init.add_argument("--profile", required=True)
    init.add_argument("--name", default=None)
    init.add_argument("--force", action="store_true")
    init.add_argument("--json", action="store_true")
    init.set_defaults(func=cmd_init)

    plan_goal = subparsers.add_parser("plan-goal", help="Propose or materialize a starter roadmap from a high-level goal")
    plan_goal.add_argument("--project-root", type=Path, required=True)
    plan_goal.add_argument("--name", default=None)
    plan_goal.add_argument("--profile", required=True)
    goal_source = plan_goal.add_mutually_exclusive_group(required=True)
    goal_source.add_argument("--goal", default=None)
    goal_source.add_argument("--goal-file", type=Path, default=None)
    plan_goal.add_argument("--blueprint", default=None)
    plan_goal.add_argument("--constraint", action="append", default=None)
    plan_goal.add_argument("--experience-kind", default=None)
    plan_goal.add_argument("--stage-count", type=int, default=DEFAULT_GOAL_STAGE_COUNT)
    plan_goal.add_argument("--materialize", action="store_true")
    plan_goal.add_argument("--force", action="store_true")
    plan_goal.add_argument("--json", action="store_true")
    plan_goal.set_defaults(func=cmd_plan_goal)

    workspace_drive = subparsers.add_parser(
        "workspace-drive",
        help="Dispatch at most one eligible project drive from a workspace queue",
    )
    workspace_drive.add_argument("--workspace", type=Path, default=Path.cwd())
    workspace_drive.add_argument("--max-depth", type=int, default=3)
    workspace_drive.add_argument("--max-tasks", type=int, default=1)
    workspace_drive.add_argument("--time-budget-seconds", type=int, default=0)
    workspace_drive.add_argument("--rolling", action="store_true")
    workspace_drive.add_argument("--self-iterate", action="store_true")
    workspace_drive.add_argument("--max-continuations", type=int, default=1)
    workspace_drive.add_argument("--max-self-iterations", type=int, default=1)
    workspace_drive.add_argument("--continuation-batch-size", type=int, default=1)
    workspace_drive.add_argument("--no-progress-limit", type=int, default=2)
    workspace_drive.add_argument("--lease-stale-after-seconds", type=int, default=None)
    workspace_drive.add_argument("--allow-live", action="store_true")
    workspace_drive.add_argument("--allow-manual", action="store_true")
    workspace_drive.add_argument("--allow-agent", action="store_true")
    workspace_drive.add_argument("--json", action="store_true")
    workspace_drive.set_defaults(func=cmd_workspace_drive)

    for name, help_text, func in [
        ("status", "Show project or workspace status", cmd_status),
        ("validate", "Validate the engineering roadmap schema and task commands", cmd_validate),
        ("next", "Show the next selected task", cmd_next),
        ("run", "Run the next or selected task acceptance checks", cmd_run),
        ("advance", "Materialize the next continuation milestone into the roadmap", cmd_advance),
        ("frontend-tasks", "Propose or materialize frontend roadmap tasks from the experience plan", cmd_frontend_tasks),
        ("self-iterate", "Assess current state and append the next continuation stage", cmd_self_iterate),
        ("pause", "Pause future drive scheduling for this project", cmd_pause),
        ("resume", "Clear pause or cancel state so a drive can continue", cmd_resume),
        ("cancel", "Cancel future drive scheduling for this project until resumed", cmd_cancel),
        ("approvals", "List pending or historical approval gates", cmd_approvals),
        ("approve", "Approve one or all pending approval gates", cmd_approve),
        ("drive", "Continuously run pending roadmap tasks until complete, blocked, failed, or out of budget", cmd_drive),
    ]:
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("--project-root", type=Path, default=None)
        command.add_argument("--workspace", type=Path, default=Path.cwd())
        command.add_argument("--project", default=None)
        command.add_argument("--max-depth", type=int, default=3)
        command.add_argument("--json", action="store_true")
        if name == "run":
            command.add_argument("--task", default=None)
            command.add_argument("--max-tasks", type=int, default=1)
            command.add_argument("--dry-run", action="store_true")
            command.add_argument("--allow-live", action="store_true")
            command.add_argument("--allow-manual", action="store_true")
            command.add_argument("--allow-agent", action="store_true")
            command.add_argument("--commit-after-task", action="store_true")
            command.add_argument("--push-after-task", action="store_true")
            command.add_argument("--git-remote", default="origin")
            command.add_argument("--git-branch", default=None)
            command.add_argument("--git-message-template", default="chore(engineering): complete {task_id}")
        if name == "advance":
            command.add_argument("--max-new-milestones", type=int, default=1)
            command.add_argument("--reason", default="manual_advance")
        if name == "frontend-tasks":
            command.add_argument("--materialize", action="store_true")
            command.add_argument("--milestone-id", default="frontend-visualization")
            command.add_argument("--reason", default="manual_frontend_task_generation")
        if name == "self-iterate":
            command.add_argument("--reason", default="manual_self_iteration")
            command.add_argument("--allow-live", action="store_true")
            command.add_argument("--allow-agent", action="store_true")
        if name in {"pause", "resume", "cancel"}:
            command.add_argument("--reason", default=f"manual_{name}")
        if name == "approvals":
            command.add_argument("--status", default="pending")
            command.add_argument("--all", action="store_true")
        if name == "approve":
            command.add_argument("approval_id", nargs="?")
            command.add_argument("--all", action="store_true")
            command.add_argument("--approved-by", default="local")
            command.add_argument("--reason", default="manual approval")
        if name == "drive":
            command.add_argument("--max-tasks", type=int, default=100)
            command.add_argument("--time-budget-seconds", type=int, default=0)
            command.add_argument("--rolling", action="store_true")
            command.add_argument("--self-iterate", action="store_true")
            command.add_argument("--max-continuations", type=int, default=20)
            command.add_argument("--max-self-iterations", type=int, default=20)
            command.add_argument("--continuation-batch-size", type=int, default=1)
            command.add_argument("--no-progress-limit", type=int, default=2)
            command.add_argument("--allow-live", action="store_true")
            command.add_argument("--allow-manual", action="store_true")
            command.add_argument("--allow-agent", action="store_true")
            command.add_argument("--stop-after-each", action="store_true")
            command.add_argument("--commit-after-task", action="store_true")
            command.add_argument("--push-after-task", action="store_true")
            command.add_argument("--git-remote", default="origin")
            command.add_argument("--git-branch", default=None)
            command.add_argument("--git-message-template", default="chore(engineering): complete {task_id}")
        command.set_defaults(func=func)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
