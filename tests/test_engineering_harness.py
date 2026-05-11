from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from engineering_harness.core import Harness, discover_projects, init_project
from engineering_harness.executors import ExecutorMetadata, ExecutorRegistry, ExecutorResult
from engineering_harness.cli import main as cli_main
from engineering_harness.profiles import list_profiles


ROADMAP_FIXTURES = Path(__file__).parent / "fixtures" / "roadmaps"


def validate_roadmap_fixture(tmp_path: Path, fixture_name: str) -> dict:
    project = tmp_path / Path(fixture_name).stem
    project.mkdir()
    engineering_dir = project / ".engineering"
    engineering_dir.mkdir()
    roadmap_path = engineering_dir / "roadmap.yaml"
    fixture_path = ROADMAP_FIXTURES / fixture_name
    roadmap_path.write_text(fixture_path.read_text(encoding="utf-8"), encoding="utf-8")

    return Harness(project).validate_roadmap()


def validate_roadmap_payload(tmp_path: Path, roadmap: dict) -> dict:
    project = tmp_path / "payload-project"
    project.mkdir()
    engineering_dir = project / ".engineering"
    engineering_dir.mkdir()
    roadmap_path = engineering_dir / "roadmap.yaml"
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    return Harness(project).validate_roadmap()


def roadmap_fixture_payload(fixture_name: str) -> dict:
    fixture_path = ROADMAP_FIXTURES / fixture_name
    return json.loads(fixture_path.read_text(encoding="utf-8"))


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


def test_harness_runs_task_and_writes_matching_manifest(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0]["command"] = (
        "python3 -c \"from pathlib import Path; Path('done.txt').write_text('done'); print('manifest ok')\""
    )
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "harness@example.invalid"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Harness Test"], cwd=project, check=True)
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=project, check=True, capture_output=True, text=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=project, text=True).strip()

    harness = Harness(project)
    result = harness.run_task(harness.next_task())

    assert result["status"] == "passed"
    report_path = project / result["report"]
    manifest_path = project / result["manifest"]
    assert manifest_path == report_path.with_suffix(".json")
    assert manifest_path.exists()

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["project"] == "agent-project"
    assert manifest["task_id"] == "tests"
    assert manifest["task"]["id"] == "tests"
    assert manifest["milestone"]["id"] == "baseline"
    assert manifest["status"] == "passed"
    assert manifest["message"] == result["message"]
    assert manifest["started_at"]
    assert manifest["finished_at"]
    assert manifest["attempt"] == 1
    assert manifest["report_path"] == result["report"]
    assert manifest["manifest_path"] == result["manifest"]
    assert manifest["artifacts"] == [
        {"kind": "markdown_report", "path": result["report"]},
        {"kind": "json_manifest", "path": result["manifest"]},
    ]
    assert manifest["runs"][0]["phase"] == "acceptance-1"
    assert manifest["runs"][0]["executor"] == "shell"
    assert manifest["runs"][0]["status"] == "passed"
    assert manifest["runs"][0]["returncode"] == 0
    assert manifest["runs"][0]["stdout"]["bytes"] > 0
    assert manifest["runs"][0]["stdout"]["sha256"]
    assert manifest["runs"][0]["executor_metadata"]["schema_version"] == 1
    assert manifest["runs"][0]["executor_metadata"]["id"] == "shell"
    assert manifest["runs"][0]["executor_metadata"]["kind"] == "process"
    assert manifest["runs"][0]["executor_metadata"]["input_mode"] == "command"
    assert manifest["runs"][0]["executor_metadata"]["uses_command_policy"] is True
    assert manifest["runs"][0]["executor_result"]["schema_version"] == 1
    assert manifest["runs"][0]["executor_result"]["status"] == "passed"
    assert manifest["runs"][0]["executor_result"]["returncode"] == 0
    assert manifest["runs"][0]["executor_result"]["stdout"] == manifest["runs"][0]["stdout"]
    assert manifest["runs"][0]["executor_result"]["stderr"] == manifest["runs"][0]["stderr"]
    assert result["runs"][0]["executor_metadata"]["id"] == "shell"
    assert result["runs"][0]["executor_result"]["status"] == "passed"
    assert manifest["safety"]["git_preflight"]["status"] == "clean"
    assert manifest["safety"]["file_scope_guard"]["status"] == "passed"
    assert manifest["git"]["is_repository"] is True
    assert manifest["git"]["head"] == head
    assert manifest["git"]["dirty_before_paths"] == []
    assert manifest["git"]["dirty_after_paths"] == ["done.txt"]
    assert any(
        decision["kind"] == "command_policy" and decision["outcome"] == "allowed"
        for decision in manifest["policy_decisions"]
    )

    state = json.loads((project / ".engineering/state/harness-state.json").read_text(encoding="utf-8"))
    assert state["tasks"]["tests"]["last_manifest"] == result["manifest"]


def test_manifest_index_lists_multiple_task_run_manifests_deterministically(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    first_task = roadmap["milestones"][0]["tasks"][0]
    first_task["acceptance"][0]["command"] = "python3 -c \"print('first')\""
    roadmap["milestones"][0]["tasks"].append(
        {
            "id": "zz-second",
            "title": "Second indexed task",
            "file_scope": ["tests/**"],
            "acceptance": [{"name": "second", "command": "python3 -c \"print('second')\""}],
        }
    )
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    first_result = harness.run_task(harness.next_task())
    harness = Harness(project)
    second_result = harness.run_task(harness.next_task())

    index_path = project / ".engineering/reports/tasks/manifest-index.json"
    index = Harness(project).manifest_index()
    reread_index = Harness(project).manifest_index()
    on_disk_index = json.loads(index_path.read_text(encoding="utf-8"))

    assert first_result["status"] == "passed"
    assert second_result["status"] == "passed"
    assert index_path.exists()
    assert index == reread_index == on_disk_index
    assert index["kind"] == "engineering-harness.task-run-manifest-index"
    assert index["manifest_index_path"] == ".engineering/reports/tasks/manifest-index.json"
    assert index["manifest_count"] == 2
    assert index["status_counts"] == {"passed": 2}
    assert [item["task_id"] for item in index["manifests"]] == ["tests", "zz-second"]
    assert [item["manifest_path"] for item in index["manifests"]] == [
        first_result["manifest"],
        second_result["manifest"],
    ]
    assert index["latest_manifest"] == second_result["manifest"]
    assert index["latest_by_task"] == {
        "tests": first_result["manifest"],
        "zz-second": second_result["manifest"],
    }
    assert Harness(project).status_summary()["manifest_index"]["manifest_count"] == 2


def test_custom_executor_registry_preserves_task_semantics_and_normalizes_result(tmp_path):
    class NoopExecutor:
        metadata = ExecutorMetadata(
            id="noop",
            name="Noop",
            kind="test",
            adapter="test.noop",
            input_mode="prompt",
            capabilities=("stdout",),
        )

        def display_command(self, invocation):
            return f"noop <task:{invocation.task_id}>"

        def execute(self, invocation):
            return ExecutorResult(
                status="passed",
                returncode=0,
                started_at="2024-01-01T00:00:00Z",
                finished_at="2024-01-01T00:00:01Z",
                stdout=f"prompt={invocation.prompt}",
                stderr="",
                metadata={"adapter": "noop"},
            )

    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0] = {
        "name": "future adapter",
        "executor": "noop",
        "prompt": "preserve the roadmap command shape",
    }
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    registry = ExecutorRegistry((NoopExecutor(),))
    harness = Harness(project, executor_registry=registry)

    assert harness.validate_roadmap()["status"] == "passed"
    result = harness.run_task(harness.next_task())

    assert result["status"] == "passed"
    assert result["runs"][0]["executor"] == "noop"
    assert result["runs"][0]["command"] == "noop <task:tests>"

    manifest = json.loads((project / result["manifest"]).read_text(encoding="utf-8"))
    run = manifest["runs"][0]
    assert run["executor"] == "noop"
    assert run["executor_metadata"]["adapter"] == "test.noop"
    assert run["executor_metadata"]["input_mode"] == "prompt"
    assert run["executor_result"]["status"] == "passed"
    assert run["executor_result"]["metadata"] == {"adapter": "noop"}
    assert any(
        decision["kind"] == "executor_policy" and decision["executor"] == "noop" and decision["outcome"] == "allowed"
        for decision in manifest["policy_decisions"]
    )


def test_shell_executor_selection_uses_registered_adapter(tmp_path):
    executed = []

    class RecordingShellExecutor:
        metadata = ExecutorMetadata(
            id="shell",
            name="Recording Shell",
            kind="process",
            adapter="test.recording-shell",
            input_mode="command",
            capabilities=("stdout",),
            uses_command_policy=True,
        )

        def display_command(self, invocation):
            return f"recording-shell:{invocation.command}"

        def execute(self, invocation):
            executed.append(invocation)
            return ExecutorResult(
                status="passed",
                returncode=0,
                started_at="2024-01-01T00:00:00Z",
                finished_at="2024-01-01T00:00:01Z",
                stdout="shell adapter selected",
                stderr="",
                metadata={"selected": "shell"},
            )

    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0]["command"] = "python3 -c \"print('not run directly')\""
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project, executor_registry=ExecutorRegistry((RecordingShellExecutor(),)))
    result = harness.run_task(harness.next_task())

    assert result["status"] == "passed"
    assert len(executed) == 1
    assert executed[0].command == "python3 -c \"print('not run directly')\""
    assert result["runs"][0]["executor"] == "shell"
    assert result["runs"][0]["command"] == "recording-shell:python3 -c \"print('not run directly')\""
    assert result["runs"][0]["executor_metadata"]["adapter"] == "test.recording-shell"
    assert result["runs"][0]["executor_result"]["metadata"] == {"selected": "shell"}


def test_codex_executor_selection_uses_registered_adapter_and_preparation(tmp_path):
    prepared = []
    executed = []

    class RecordingCodexExecutor:
        metadata = ExecutorMetadata(
            id="codex",
            name="Recording Codex",
            kind="agent",
            adapter="test.recording-codex",
            input_mode="prompt",
            capabilities=("stdout",),
            requires_agent_approval=True,
        )

        def prepare_invocation(self, invocation, task_context):
            prepared.append((invocation.prompt, task_context.task_id, task_context.acceptance[0].name))
            return replace(invocation, prompt=f"prepared:{task_context.task_id}:{invocation.prompt}")

        def display_command(self, invocation):
            return f"recording-codex <task:{invocation.task_id}>"

        def execute(self, invocation):
            executed.append(invocation)
            return ExecutorResult(
                status="passed",
                returncode=0,
                started_at="2024-01-01T00:00:00Z",
                finished_at="2024-01-01T00:00:01Z",
                stdout=invocation.prompt or "",
                stderr="",
                metadata={"selected": "codex"},
            )

    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0] = {
        "name": "agent work",
        "executor": "codex",
        "prompt": "Do not change files.",
    }
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project, executor_registry=ExecutorRegistry((RecordingCodexExecutor(),)))
    result = harness.run_task(harness.next_task(), allow_agent=True)

    assert result["status"] == "passed"
    assert prepared[0] == ("Do not change files.", "tests", "agent work")
    assert len(executed) == 1
    assert executed[0].prompt == "prepared:tests:Do not change files."
    assert result["runs"][0]["executor"] == "codex"
    assert result["runs"][0]["command"] == "recording-codex <task:tests>"
    assert result["runs"][0]["executor_metadata"]["adapter"] == "test.recording-codex"
    assert result["runs"][0]["executor_result"]["metadata"] == {"selected": "codex"}


def test_manifest_index_keeps_repeated_task_runs_with_same_slug(tmp_path, monkeypatch):
    monkeypatch.setattr("engineering_harness.core.slug_now", lambda: "20240101T000000Z")
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0]["command"] = "python3 -c \"print('repeat')\""
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    task = harness.next_task()
    first_result = harness.run_task(task)
    second_result = Harness(project).run_task(task)
    index = Harness(project).manifest_index()

    assert first_result["status"] == "passed"
    assert second_result["status"] == "passed"
    assert first_result["manifest"] == ".engineering/reports/tasks/20240101T000000Z-tests.json"
    assert second_result["manifest"] == ".engineering/reports/tasks/20240101T000000Z-tests_2.json"
    assert [item["manifest_path"] for item in index["manifests"]] == [
        first_result["manifest"],
        second_result["manifest"],
    ]
    assert [item["attempt"] for item in index["manifests"]] == [1, 2]
    assert index["latest_by_task"] == {"tests": second_result["manifest"]}


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


def test_validate_roadmap_allows_missing_experience_for_backward_compatibility(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")

    result = Harness(project).validate_roadmap()

    assert result["status"] == "passed"
    assert result["errors"] == []


@pytest.mark.parametrize(
    "fixture_name",
    [
        "valid/api-only.json",
        "valid/dashboard.json",
        "valid/submission-review.json",
        "valid/multi-role-app.json",
        "valid/cli-only.json",
    ],
)
def test_valid_roadmap_fixtures_pass_validation(tmp_path, fixture_name):
    result = validate_roadmap_fixture(tmp_path, fixture_name)

    assert result["status"] == "passed"
    assert result["errors"] == []


@pytest.mark.parametrize(
    ("experience", "expected_error"),
    [
        ("dashboard", "top-level `experience` must be a mapping"),
        (
            {
                "kind": "desktop-app",
                "personas": ["operator"],
                "primary_surfaces": ["operator dashboard"],
                "auth": {"required": False, "roles": []},
                "e2e_journeys": [
                    {"id": "operator-checks-status", "persona": "operator", "goal": "Check status."}
                ],
            },
            "experience.kind `desktop-app` is not supported",
        ),
        (
            {
                "kind": "dashboard",
                "personas": [],
                "primary_surfaces": ["operator dashboard"],
                "auth": {"required": False, "roles": []},
                "e2e_journeys": [
                    {"id": "operator-checks-status", "persona": "operator", "goal": "Check status."}
                ],
            },
            "experience.personas must include at least one item",
        ),
        (
            {
                "kind": "dashboard",
                "personas": ["operator"],
                "primary_surfaces": [""],
                "auth": {"required": False, "roles": []},
                "e2e_journeys": [
                    {"id": "operator-checks-status", "persona": "operator", "goal": "Check status."}
                ],
            },
            "experience.primary_surfaces[0] must be a non-empty string",
        ),
        (
            {
                "kind": "multi-role-app",
                "personas": ["operator"],
                "primary_surfaces": ["operator dashboard"],
                "auth": {"required": True, "roles": []},
                "e2e_journeys": [
                    {"id": "operator-checks-status", "persona": "operator", "goal": "Check status."}
                ],
            },
            "experience.auth.roles must include at least one role when auth.required is true",
        ),
        (
            {
                "kind": "dashboard",
                "personas": ["operator"],
                "primary_surfaces": ["operator dashboard"],
                "auth": {"required": False, "roles": []},
                "e2e_journeys": [],
            },
            "experience.e2e_journeys must define at least one journey",
        ),
        (
            {
                "kind": "submission-review",
                "personas": ["student"],
                "primary_surfaces": ["submission portal"],
                "auth": {"required": True, "roles": ["student"]},
                "e2e_journeys": [
                    {"id": "reviewer-checks-work", "persona": "reviewer", "goal": "Review submitted work."}
                ],
            },
            "experience.e2e_journeys[0].persona `reviewer` must match one of experience.personas",
        ),
    ],
)
def test_invalid_experience_shapes_fail_validation(tmp_path, experience, expected_error):
    roadmap = roadmap_fixture_payload("valid/dashboard.json")
    roadmap["experience"] = experience

    result = validate_roadmap_payload(tmp_path, roadmap)

    assert result["status"] == "failed"
    assert result["error_count"] >= 1
    assert any(expected_error in error for error in result["errors"]), result["errors"]


@pytest.mark.parametrize(
    ("fixture_name", "expected_error"),
    [
        ("invalid/missing-acceptance.json", "must define at least one acceptance command"),
        ("invalid/duplicate-task-id.json", "duplicate task id: duplicate-fixture-task"),
        ("invalid/unknown-executor.json", "has unknown executor `spaceship`"),
        ("invalid/continuation-stage-without-tasks.json", "must define at least one task"),
    ],
)
def test_invalid_roadmap_fixtures_fail_validation(tmp_path, fixture_name, expected_error):
    result = validate_roadmap_fixture(tmp_path, fixture_name)

    assert result["status"] == "failed"
    assert result["error_count"] >= 1
    assert any(expected_error in error for error in result["errors"]), result["errors"]


def test_harness_blocks_codex_executor_without_agent_approval(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    task = roadmap["milestones"][0]["tasks"][0]
    task["acceptance"][0] = {"name": "agent work", "executor": "codex", "prompt": "Do not change files."}
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    result = harness.run_task(harness.next_task())

    assert result["status"] == "blocked"
    assert "allow-agent" in result["message"]
    assert result["runs"][0]["executor"] == "codex"
    assert result["runs"][0]["executor_metadata"]["id"] == "codex"
    assert result["runs"][0]["executor_metadata"]["kind"] == "agent"
    assert result["runs"][0]["executor_metadata"]["requires_agent_approval"] is True
    assert result["runs"][0]["executor_result"]["status"] == "blocked"

    manifest = json.loads((project / result["manifest"]).read_text(encoding="utf-8"))
    assert manifest["runs"][0]["executor_metadata"] == result["runs"][0]["executor_metadata"]
    assert manifest["runs"][0]["executor_result"]["status"] == "blocked"


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


def test_validate_allows_materialized_continuation_stage_task_ids(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"] = []
    roadmap["continuation"] = {
        "enabled": True,
        "goal": "Materialize and continue validating.",
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

    harness = Harness(project)
    advance = harness.advance_roadmap()
    validation = Harness(project).validate_roadmap()

    assert advance["status"] == "advanced"
    assert validation["status"] == "passed"


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
