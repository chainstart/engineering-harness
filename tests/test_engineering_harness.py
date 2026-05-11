from __future__ import annotations

import json
import subprocess
from pathlib import Path

from engineering_harness.core import Harness, discover_projects, init_project
from engineering_harness.cli import main as cli_main
from engineering_harness.profiles import list_profiles


def test_profiles_are_available():
    profile_ids = {item["id"] for item in list_profiles()}

    assert "evm-protocol" in profile_ids
    assert "python-agent" in profile_ids
    assert "trading-research" in profile_ids


def test_init_project_creates_config_and_policy(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()

    result = init_project(project, "python-agent", name="agent-project")

    assert Path(result["roadmap"]).exists()
    assert (project / ".engineering/policies/command-allowlist.yaml").exists()
    harness = Harness(project)
    assert harness.status_summary()["project"] == "agent-project"


def test_harness_runs_task_and_writes_state(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0]["command"] = "python3 -c \"print('ok')\""
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    result = harness.run_task(harness.next_task())

    assert result["status"] == "passed"
    state = json.loads((project / ".engineering/state/harness-state.json").read_text(encoding="utf-8"))
    assert state["tasks"]["tests"]["status"] == "passed"
    assert (project / state["tasks"]["tests"]["last_report"]).exists()


def test_harness_blocks_non_allowlisted_command(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0]["command"] = "curl https://example.com"
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    result = harness.run_task(harness.next_task())

    assert result["status"] == "blocked"
    assert "allowlisted" in result["message"]


def test_harness_runs_implementation_before_acceptance(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    task = roadmap["milestones"][0]["tasks"][0]
    task["implementation"] = [
        {
            "name": "write implementation marker",
            "command": "python3 -c \"from pathlib import Path; Path('implemented.txt').write_text('ok')\"",
        }
    ]
    task["acceptance"][0]["command"] = "python3 -c \"from pathlib import Path; assert Path('implemented.txt').read_text() == 'ok'\""
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    result = harness.run_task(harness.next_task(), allow_agent=True)

    assert result["status"] == "passed"
    assert [run["phase"] for run in result["runs"]] == ["implementation", "acceptance-1"]


def test_harness_can_repair_after_failed_acceptance(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    task = roadmap["milestones"][0]["tasks"][0]
    task["max_task_iterations"] = 2
    task["repair"] = [
        {
            "name": "repair marker",
            "command": "python3 -c \"from pathlib import Path; Path('repair.txt').write_text('fixed')\"",
        }
    ]
    task["acceptance"][0]["command"] = "python3 -c \"from pathlib import Path; assert Path('repair.txt').read_text() == 'fixed'\""
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    result = harness.run_task(harness.next_task(), allow_agent=True)

    assert result["status"] == "passed"
    phases = [run["phase"] for run in result["runs"]]
    assert phases == ["acceptance-1", "repair-1", "acceptance-2"]


def test_harness_runs_e2e_after_acceptance(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    task = roadmap["milestones"][0]["tasks"][0]
    task["acceptance"][0]["command"] = "python3 -c \"from pathlib import Path; Path('accepted.txt').write_text('ok')\""
    task["e2e"] = [
        {
            "name": "simulate user path",
            "command": "python3 -c \"from pathlib import Path; assert Path('accepted.txt').read_text() == 'ok'; Path('e2e.txt').write_text('ok')\"",
        }
    ]
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    result = harness.run_task(harness.next_task())

    assert result["status"] == "passed"
    assert (project / "e2e.txt").read_text(encoding="utf-8") == "ok"
    assert [run["phase"] for run in result["runs"]] == ["acceptance-1", "e2e"]
    assert result["task"]["e2e"][0]["name"] == "simulate user path"


def test_harness_fails_task_when_e2e_fails(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    task = roadmap["milestones"][0]["tasks"][0]
    task["acceptance"][0]["command"] = "python3 -c \"print('accepted')\""
    task["e2e"] = [
        {
            "name": "failing user path",
            "command": "python3 -c \"raise SystemExit(7)\"",
        }
    ]
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    result = harness.run_task(harness.next_task())

    assert result["status"] == "failed"
    assert result["runs"][-1]["phase"] == "e2e"
    assert result["runs"][-1]["returncode"] == 7
    assert "Required e2e command failed" in result["message"]


def test_file_scope_guard_allows_in_scope_changes(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "harness@example.invalid"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Harness Test"], cwd=project, check=True)
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=project, check=True, capture_output=True, text=True)

    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    task = roadmap["milestones"][0]["tasks"][0]
    task["file_scope"] = ["src/**"]
    task["implementation"] = [
        {
            "name": "write scoped file",
            "command": "python3 -c \"from pathlib import Path; Path('src').mkdir(exist_ok=True); Path('src/ok.txt').write_text('ok')\"",
        }
    ]
    task["acceptance"][0]["command"] = "python3 -c \"from pathlib import Path; assert Path('src/ok.txt').read_text() == 'ok'\""
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    result = harness.run_task(harness.next_task(), allow_agent=True)

    assert result["status"] == "passed"
    assert result["safety"]["file_scope_guard"]["status"] == "passed"


def test_file_scope_guard_blocks_out_of_scope_changes(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "harness@example.invalid"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Harness Test"], cwd=project, check=True)
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=project, check=True, capture_output=True, text=True)

    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    task = roadmap["milestones"][0]["tasks"][0]
    task["file_scope"] = ["src/**"]
    task["implementation"] = [
        {
            "name": "write unscoped file",
            "command": "python3 -c \"from pathlib import Path; Path('outside.txt').write_text('outside')\"",
        }
    ]
    task["acceptance"][0]["command"] = "python3 -c \"print('acceptance ok')\""
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    result = harness.run_task(harness.next_task(), allow_agent=True)

    assert result["status"] == "failed"
    assert result["safety"]["file_scope_guard"]["violations"] == ["outside.txt"]
    assert "outside file_scope" in result["message"]


def test_file_scope_guard_blocks_changed_preexisting_dirty_out_of_scope_file(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "harness@example.invalid"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Harness Test"], cwd=project, check=True)
    (project / "outside.txt").write_text("tracked", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=project, check=True, capture_output=True, text=True)
    (project / "outside.txt").write_text("dirty before", encoding="utf-8")

    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    task = roadmap["milestones"][0]["tasks"][0]
    task["file_scope"] = ["src/**"]
    task["implementation"] = [
        {
            "name": "modify preexisting dirty file",
            "command": "python3 -c \"from pathlib import Path; Path('outside.txt').write_text('dirty after')\"",
        }
    ]
    task["acceptance"][0]["command"] = "python3 -c \"print('acceptance ok')\""
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    result = harness.run_task(harness.next_task(), allow_agent=True)

    assert result["status"] == "failed"
    assert result["safety"]["file_scope_guard"]["changed_preexisting_dirty_paths"] == ["outside.txt"]
    assert result["safety"]["file_scope_guard"]["violations"] == ["outside.txt"]


def test_git_checkpoint_refuses_dirty_worktree_that_predates_task(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "harness@example.invalid"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Harness Test"], cwd=project, check=True)
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=project, check=True, capture_output=True, text=True)

    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    task_payload = roadmap["milestones"][0]["tasks"][0]
    task_payload["acceptance"][0]["command"] = "python3 -c \"from pathlib import Path; Path('done.txt').write_text('done')\""
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "configure task"], cwd=project, check=True, capture_output=True, text=True)
    (project / "preexisting.txt").write_text("dirty before task", encoding="utf-8")

    harness = Harness(project)
    task = harness.next_task()
    result = harness.run_task(task)
    checkpoint = harness.git_checkpoint(task)

    assert result["status"] == "passed"
    assert result["safety"]["git_preflight"]["dirty_before_paths"] == ["preexisting.txt"]
    assert checkpoint["status"] == "skipped"
    assert "dirty worktree existed before the task" in checkpoint["message"]


def test_validate_roadmap_catches_missing_acceptance(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["acceptance"] = []
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    result = harness.validate_roadmap()

    assert result["status"] == "failed"
    assert any("acceptance" in error for error in result["errors"])


def test_harness_blocks_codex_executor_without_agent_approval(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    task = roadmap["milestones"][0]["tasks"][0]
    task["implementation"] = [{"name": "agent work", "executor": "codex", "prompt": "Do not change files."}]
    task["acceptance"][0]["command"] = "python3 -c \"print('ok')\""
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    result = harness.run_task(harness.next_task())

    assert result["status"] == "blocked"
    assert "allow-agent" in result["message"]


def test_discover_projects_finds_configured_and_candidate_projects(tmp_path):
    configured = tmp_path / "configured"
    configured.mkdir()
    init_project(configured, "python-agent", name="configured")
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "package.json").write_text('{"name": "candidate"}', encoding="utf-8")

    projects = discover_projects(tmp_path)
    by_root = {project.root: project for project in projects}

    assert by_root[configured.resolve()].configured is True
    assert by_root[candidate.resolve()].configured is False
    assert by_root[candidate.resolve()].profile == "node-frontend"


def test_drive_runs_until_roadmap_is_empty(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0]["command"] = "python3 -c \"print('ok')\""
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    exit_code = cli_main(["drive", "--project-root", str(project)])

    assert exit_code == 0
    state = json.loads((project / ".engineering/state/harness-state.json").read_text(encoding="utf-8"))
    assert state["tasks"]["tests"]["status"] == "passed"
    assert list((project / ".engineering/reports/tasks/drives").glob("*-drive.md"))


def test_drive_can_commit_after_each_completed_task(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0]["command"] = (
        "python3 -c \"from pathlib import Path; Path('done.txt').write_text('done')\""
    )
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "harness@example.invalid"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Harness Test"], cwd=project, check=True)
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=project, check=True, capture_output=True, text=True)

    exit_code = cli_main(["drive", "--project-root", str(project), "--commit-after-task"])

    assert exit_code == 0
    last_subject = subprocess.check_output(["git", "log", "-1", "--format=%s"], cwd=project, text=True).strip()
    assert last_subject == "chore(engineering): complete tests"
    assert subprocess.check_output(["git", "status", "--porcelain"], cwd=project, text=True).strip() == ""


def test_advance_materializes_next_continuation_stage(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"] = []
    roadmap["continuation"] = {
        "enabled": True,
        "goal": "Ship the full agent system.",
        "stages": [
            {
                "id": "stage-a",
                "title": "Stage A",
                "objective": "Create a generated validation task.",
                "tasks": [
                    {
                        "id": "generated-test",
                        "title": "Generated Test",
                        "file_scope": ["tests/**"],
                        "acceptance": [{"name": "ok", "command": "python3 -c \"print('ok')\""}],
                    }
                ],
            }
        ],
    }
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    exit_code = cli_main(["advance", "--project-root", str(project)])

    assert exit_code == 0
    updated = json.loads(roadmap_path.read_text(encoding="utf-8"))
    assert updated["milestones"][0]["id"] == "stage-a"
    assert updated["milestones"][0]["tasks"][0]["id"] == "generated-test"


def test_drive_rolling_advances_and_runs_generated_tasks(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"] = []
    roadmap["continuation"] = {
        "enabled": True,
        "goal": "Continue until generated stages are complete.",
        "stages": [
            {
                "id": "stage-a",
                "title": "Stage A",
                "objective": "Create a generated validation task.",
                "tasks": [
                    {
                        "id": "generated-test",
                        "title": "Generated Test",
                        "file_scope": ["tests/**"],
                        "acceptance": [
                            {
                                "name": "write marker",
                                "command": "python3 -c \"from pathlib import Path; Path('generated.txt').write_text('ok')\"",
                            }
                        ],
                    }
                ],
            }
        ],
    }
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    exit_code = cli_main(["drive", "--project-root", str(project), "--rolling", "--max-continuations", "2"])

    assert exit_code == 0
    assert (project / "generated.txt").read_text(encoding="utf-8") == "ok"
    state = json.loads((project / ".engineering/state/harness-state.json").read_text(encoding="utf-8"))
    assert state["tasks"]["generated-test"]["status"] == "passed"
    report = next((project / ".engineering/reports/tasks/drives").glob("*-drive.md"))
    assert "Continuations" in report.read_text(encoding="utf-8")


def test_drive_rolling_stops_when_continuation_is_exhausted(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"] = []
    roadmap["continuation"] = {"enabled": True, "goal": "No stages remain.", "stages": []}
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    exit_code = cli_main(["drive", "--project-root", str(project), "--rolling"])

    assert exit_code == 0
    report = next((project / ".engineering/reports/tasks/drives").glob("*-drive.md"))
    text = report.read_text(encoding="utf-8")
    assert "no unmaterialized continuation stage remains" in text


def test_self_iteration_shell_planner_adds_continuation_stage(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"] = []
    roadmap["continuation"] = {"enabled": True, "goal": "Continue autonomously.", "stages": []}
    roadmap["self_iteration"] = {
        "enabled": True,
        "objective": "Add the next generated test stage.",
        "planner": {"name": "test planner", "command": "python3 planner.py"},
    }
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")
    (project / "planner.py").write_text(
        """
import json
from pathlib import Path

roadmap_path = Path(".engineering/roadmap.yaml")
roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
continuation = roadmap.setdefault("continuation", {"enabled": True, "stages": []})
continuation["enabled"] = True
continuation.setdefault("stages", []).append(
    {
        "id": "self-stage",
        "title": "Self Stage",
        "objective": "Create a marker through a generated task.",
        "tasks": [
            {
                "id": "self-generated-test",
                "title": "Self Generated Test",
                "file_scope": ["tests/**"],
                "acceptance": [
                    {
                        "name": "write self marker",
                        "command": "python3 -c \\"from pathlib import Path; Path('self-generated.txt').write_text('ok')\\"",
                    }
                ],
            }
        ],
    }
)
roadmap_path.write_text(json.dumps(roadmap, indent=2), encoding="utf-8")
""".strip()
        + "\n",
        encoding="utf-8",
    )

    harness = Harness(project)
    result = harness.run_self_iteration(reason="test")

    assert result["status"] == "planned"
    updated = json.loads(roadmap_path.read_text(encoding="utf-8"))
    assert updated["continuation"]["stages"][0]["id"] == "self-stage"
    assert list((project / ".engineering/reports/tasks/assessments").glob("*-self-iteration.md"))


def test_drive_self_iterates_then_runs_generated_stage(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"] = []
    roadmap["continuation"] = {"enabled": True, "goal": "Continue autonomously.", "stages": []}
    roadmap["self_iteration"] = {
        "enabled": True,
        "objective": "Add the next generated test stage.",
        "planner": {"name": "test planner", "command": "python3 planner.py"},
    }
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")
    (project / "planner.py").write_text(
        """
import json
from pathlib import Path

roadmap_path = Path(".engineering/roadmap.yaml")
roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
continuation = roadmap.setdefault("continuation", {"enabled": True, "stages": []})
continuation["enabled"] = True
continuation.setdefault("stages", []).append(
    {
        "id": "drive-self-stage",
        "title": "Drive Self Stage",
        "objective": "Create a marker through a generated drive task.",
        "tasks": [
            {
                "id": "drive-self-generated-test",
                "title": "Drive Self Generated Test",
                "file_scope": ["tests/**"],
                "acceptance": [
                    {
                        "name": "write drive self marker",
                        "command": "python3 -c \\"from pathlib import Path; Path('drive-self-generated.txt').write_text('ok')\\"",
                    }
                ],
            }
        ],
    }
)
roadmap_path.write_text(json.dumps(roadmap, indent=2), encoding="utf-8")
""".strip()
        + "\n",
        encoding="utf-8",
    )

    exit_code = cli_main(
        [
            "drive",
            "--project-root",
            str(project),
            "--rolling",
            "--self-iterate",
            "--max-self-iterations",
            "1",
            "--max-continuations",
            "2",
            "--max-tasks",
            "1",
        ]
    )

    assert exit_code == 0
    assert (project / "drive-self-generated.txt").read_text(encoding="utf-8") == "ok"
    state = json.loads((project / ".engineering/state/harness-state.json").read_text(encoding="utf-8"))
    assert state["tasks"]["drive-self-generated-test"]["status"] == "passed"
    report = next((project / ".engineering/reports/tasks/drives").glob("*-drive.md"))
    text = report.read_text(encoding="utf-8")
    assert "Self Iterations" in text
    assert "drive-self-stage" in roadmap_path.read_text(encoding="utf-8")
