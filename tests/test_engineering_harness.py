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
