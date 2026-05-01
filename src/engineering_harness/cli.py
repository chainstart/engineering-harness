from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .core import COMPLETED_STATUSES, Harness, discover_projects, init_project, project_from_root, slug_now, utc_now
from .profiles import list_profiles


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
                print(f"{summary['project']}: next={next_id} root={summary['root']}")
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
) -> None:
    commit_after_task = bool(getattr(args, "commit_after_task", False) or getattr(args, "push_after_task", False))
    if dry_run or not commit_after_task or result["status"] not in COMPLETED_STATUSES:
        return
    result["git"] = harness.git_checkpoint(
        task,
        push=bool(getattr(args, "push_after_task", False)),
        remote=str(getattr(args, "git_remote", "origin")),
        branch=getattr(args, "git_branch", None),
        message_template=str(getattr(args, "git_message_template", "chore(engineering): complete {task_id}")),
    )


def write_drive_report(harness: Harness, payload: dict) -> str:
    report_dir = harness.report_dir / "drives"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{slug_now()}-drive.md"
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
    lines.extend(["", "## Final Status", "", "```json", json.dumps(payload["final_status"], indent=2, sort_keys=True), "```", ""])
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return str(report_path.relative_to(harness.project_root))


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


def cmd_drive(args: argparse.Namespace) -> int:
    root = resolve_project_root(args)
    harness = Harness(root)
    started_at = utc_now()
    deadline = time.monotonic() + args.time_budget_seconds if args.time_budget_seconds else None
    results = []
    continuations = []
    continuation_count = 0
    no_progress_count = 0
    status = "completed"
    message = "No pending task."

    while True:
        if deadline is not None and time.monotonic() >= deadline:
            status = "timeout"
            message = "Time budget expired."
            break
        if len(results) >= args.max_tasks:
            status = "budget_exhausted"
            message = f"Task budget exhausted after {args.max_tasks} task(s)."
            break
        task = harness.next_task()
        if task is None:
            if not args.rolling:
                status = "completed"
                message = "Roadmap queue is empty."
                break
            if continuation_count >= args.max_continuations:
                status = "budget_exhausted"
                message = f"Continuation budget exhausted after {args.max_continuations} continuation(s)."
                break
            continuation = harness.advance_roadmap(
                max_new_milestones=args.continuation_batch_size,
                reason="rolling_drive_queue_empty",
            )
            continuations.append(continuation)
            if continuation["status"] == "advanced" and continuation.get("tasks_added", 0) > 0:
                continuation_count += 1
                no_progress_count = 0
                harness = Harness(root)
                continue
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
        result = harness.run_task(
            task,
            allow_live=args.allow_live,
            allow_manual=args.allow_manual,
            allow_agent=args.allow_agent,
        )
        maybe_checkpoint_task(harness, task, result, args)
        results.append(result)
        if result["status"] not in COMPLETED_STATUSES:
            status = result["status"]
            message = f"Stopped at task {task.id}: {result['message']}"
            break
        if args.stop_after_each:
            status = "paused"
            message = f"Stopped after task {task.id} because --stop-after-each was set."
            break

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
        "final_status": final_status,
    }
    payload["drive_report"] = write_drive_report(harness, payload)

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Drive status: {status} - {message}")
        print(f"Tasks run: {len(results)}")
        print(f"Continuations: {len(continuations)}")
        print(f"Drive report: {payload['drive_report']}")
        next_task = final_status.get("next_task")
        print(f"Next task: {next_task['id'] if next_task else 'none'}")

    return 0 if status in {"completed", "paused", "budget_exhausted"} else 1


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

    for name, help_text, func in [
        ("status", "Show project or workspace status", cmd_status),
        ("next", "Show the next selected task", cmd_next),
        ("run", "Run the next or selected task acceptance checks", cmd_run),
        ("advance", "Materialize the next continuation milestone into the roadmap", cmd_advance),
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
        if name == "drive":
            command.add_argument("--max-tasks", type=int, default=100)
            command.add_argument("--time-budget-seconds", type=int, default=0)
            command.add_argument("--rolling", action="store_true")
            command.add_argument("--max-continuations", type=int, default=20)
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
