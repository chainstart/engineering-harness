from __future__ import annotations

import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from engineering_harness.goal_intake import GoalIntakeValidationError, normalize_goal_intake, validate_goal_intake
from engineering_harness.goal_planner import plan_goal_roadmap
from engineering_harness.core import Harness, discover_projects, init_project, utc_now
from engineering_harness.executors import (
    DAGGER_ENABLE_ENV,
    DaggerExecutorAdapter,
    ExecutorInvocation,
    ExecutorMetadata,
    ExecutorRegistry,
    ExecutorResult,
    ShellExecutorAdapter,
    default_executor_registry,
)
from engineering_harness.cli import main as cli_main
from engineering_harness.policy_compat import (
    evaluate_opa_policy_input,
    export_policy_input_for_opa,
    serialize_policy_input_for_opa,
)
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


def status_summary_for_roadmap(tmp_path: Path, project_name: str, roadmap: dict) -> dict:
    project = tmp_path / project_name
    project.mkdir()
    engineering_dir = project / ".engineering"
    engineering_dir.mkdir()
    roadmap_path = engineering_dir / "roadmap.yaml"
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    return Harness(project).status_summary()


def roadmap_fixture_payload(fixture_name: str) -> dict:
    fixture_path = ROADMAP_FIXTURES / fixture_name
    return json.loads(fixture_path.read_text(encoding="utf-8"))


def continuation_tasks(roadmap: dict) -> list[dict]:
    return [
        task
        for stage in roadmap.get("continuation", {}).get("stages", [])
        for task in stage.get("tasks", [])
    ]


def task_manifest(project: Path, result: dict) -> dict:
    return json.loads((project / result["manifest"]).read_text(encoding="utf-8"))


def harness_state(project: Path) -> dict:
    return json.loads((project / ".engineering/state/harness-state.json").read_text(encoding="utf-8"))


def write_harness_state(project: Path, state: dict) -> None:
    (project / ".engineering/state/harness-state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")


def unused_pid() -> int:
    pid = 999_999
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return pid
        except PermissionError:
            pid += 1
            continue
        pid += 1


def init_git_repo(project: Path, message: str = "initial") -> None:
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "harness@example.invalid"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Harness Test"], cwd=project, check=True)
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", message], cwd=project, check=True, capture_output=True, text=True)


def report_policy_evidence(project: Path, result: dict) -> dict:
    report = (project / result["report"]).read_text(encoding="utf-8")
    section = report.split("## Policy Decisions", 1)[1]
    block = section.split("```json", 1)[1].split("```", 1)[0]
    return json.loads(block)


def drive_report_goal_gap_retrospective(project: Path, report_path: str) -> dict:
    report = (project / report_path).read_text(encoding="utf-8")
    section = report.split("## Goal-Gap Retrospective", 1)[1]
    block = section.split("```json", 1)[1].split("```", 1)[0]
    return json.loads(block)


def policy_decision(manifest: dict, kind: str, **matches) -> dict:
    for decision in manifest["policy_decisions"]:
        if decision.get("kind") != kind:
            continue
        if all(decision.get(key) == value for key, value in matches.items()):
            return decision
    raise AssertionError(f"missing policy decision {kind} matching {matches}")


def roadmap_without_experience(project_name: str, *, profile: str = "python-agent", task_title: str) -> dict:
    return {
        "version": 1,
        "project": project_name,
        "profile": profile,
        "milestones": [
            {
                "id": "baseline",
                "title": "Baseline",
                "objective": task_title,
                "tasks": [
                    {
                        "id": "baseline-task",
                        "title": task_title,
                        "status": "pending",
                        "acceptance": [
                            {
                                "name": "baseline validates",
                                "command": "python3 -c \"print('ok')\"",
                                "timeout_seconds": 30,
                            }
                        ],
                    }
                ],
            }
        ],
    }


def test_profiles_are_available():
    profile_ids = {item["id"] for item in list_profiles()}

    assert "evm-protocol" in profile_ids
    assert "python-agent" in profile_ids
    assert "trading-research" in profile_ids


def test_goal_intake_normalizes_local_contract_shape():
    contract = normalize_goal_intake(
        project_name="  Autonomous Report Worker  ",
        profile=" Python-Agent ",
        goal_text="Build a local autonomous report generator.\nInclude deterministic tests.",
        blueprint_path=Path("docs/blueprint.md"),
        constraints=[" local only ", "no private keys", "local only"],
        desired_experience_kind="dashboard",
    )

    assert contract["schema_version"] == 1
    assert contract["kind"] == "engineering-harness.goal-intake.v1"
    assert contract["project"] == {
        "name": "Autonomous Report Worker",
        "slug": "autonomous-report-worker",
        "profile": "python-agent",
    }
    assert contract["goal"] == {
        "text": "Build a local autonomous report generator. Include deterministic tests.",
    }
    assert contract["blueprint"] == {"path": "docs/blueprint.md", "provided": True}
    assert contract["constraints"] == ["local only", "no private keys"]
    assert contract["experience"] == {"kind": "dashboard", "provided": True}
    assert contract["safety"]["mode"] == "local-only"
    assert contract["safety"]["allow_live_services"] is False
    assert contract["safety"]["blocked_requirements"] == []
    assert contract["roadmap_seed"] == {
        "project": "Autonomous Report Worker",
        "profile": "python-agent",
        "goal": "Build a local autonomous report generator. Include deterministic tests.",
        "blueprint_path": "docs/blueprint.md",
        "constraints": ["local only", "no private keys"],
        "experience_kind": "dashboard",
    }


def test_goal_intake_validation_rejects_empty_goal():
    result = validate_goal_intake(
        project_name="agent-project",
        profile="python-agent",
        goal_text=" \n\t ",
        constraints=[],
        desired_experience_kind="api only",
    )

    assert result["status"] == "failed"
    assert result["goal_intake"] is None
    assert result["errors"] == ["`goal_text` is required"]

    with pytest.raises(GoalIntakeValidationError) as excinfo:
        normalize_goal_intake(project_name="agent-project", profile="python-agent", goal_text="")
    assert excinfo.value.errors == ["`goal_text` is required"]


@pytest.mark.parametrize(
    ("goal_text", "constraints", "expected_match"),
    [
        ("Deploy to production after generating the roadmap.", [], "Deploy to production"),
        ("Build a trading agent that can place live orders.", [], "place live orders"),
        ("Build a local dashboard.", ["Must execute real trades during acceptance."], "execute real trades"),
    ],
)
def test_goal_intake_rejects_unsafe_live_service_requirements(goal_text, constraints, expected_match):
    result = validate_goal_intake(
        project_name="agent-project",
        profile="python-agent",
        goal_text=goal_text,
        constraints=constraints,
        desired_experience_kind="dashboard",
    )

    assert result["status"] == "failed"
    assert result["goal_intake"] is None
    assert result["blocked_requirements"]
    assert any(expected_match in item["match"] for item in result["blocked_requirements"])
    assert any("unsafe live-service requirement" in error for error in result["errors"])


def test_goal_intake_validation_rejects_nonlocal_blueprint_and_unknown_kind():
    result = validate_goal_intake(
        project_name="agent-project",
        profile="python-agent",
        goal_text="Build a local API planner.",
        blueprint_path="https://example.com/blueprint.md",
        desired_experience_kind="desktop-app",
    )

    assert result["status"] == "failed"
    assert "`blueprint_path` must be a local path, not a URL" in result["errors"]
    assert any("desired_experience_kind `desktop-app` is not supported" in error for error in result["errors"])


def test_plan_goal_cli_proposes_starter_roadmap_without_writing(tmp_path, capsys):
    project = tmp_path / "planner-project"

    exit_code = cli_main(
        [
            "plan-goal",
            "--project-root",
            str(project),
            "--name",
            "Local Report CLI",
            "--profile",
            "python-agent",
            "--goal",
            "Build a CLI tool that generates local reports and documented command output.",
            "--experience-kind",
            "cli",
            "--constraint",
            "Keep examples deterministic.",
            "--stage-count",
            "3",
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    roadmap = payload["roadmap"]
    stages = roadmap["continuation"]["stages"]

    assert exit_code == 0
    assert payload["status"] == "proposed"
    assert payload["materialized"] is False
    assert payload["stage_count"] == 3
    assert not (project / ".engineering/roadmap.yaml").exists()
    assert roadmap["project"] == "Local Report CLI"
    assert roadmap["experience"]["kind"] == "cli-only"
    assert roadmap["experience"]["derived"] is False
    assert roadmap["goal"]["constraints"] == ["Keep examples deterministic."]
    assert [stage["id"] for stage in stages] == [
        "stage-1-local-slice",
        "stage-2-experience-validation",
        "stage-3-policy-evidence-observability",
    ]
    assert roadmap["milestones"][0]["id"] == "baseline"
    assert roadmap["milestones"][0]["tasks"][0]["acceptance"]
    for stage in stages:
        task = stage["tasks"][0]
        assert task["file_scope"]
        assert task["implementation"][0]["executor"] == "codex"
        assert task["repair"][0]["executor"] == "codex"
        assert task["acceptance"][0]["command"].startswith("python3 ")
        assert task["e2e"][0]["command"].startswith("python3 ")
    assert payload["goal_intake"]["safety"]["allow_live_services"] is False


def test_plan_goal_cli_materializes_goal_file_roadmap_and_validates(tmp_path, capsys):
    project = tmp_path / "autonomous-worker"
    project.mkdir()
    goal_file = tmp_path / "goal.txt"
    goal_file.write_text("Build an autonomous dashboard worker for local research artifacts.", encoding="utf-8")

    exit_code = cli_main(
        [
            "plan-goal",
            "--project-root",
            str(project),
            "--name",
            "Autonomous Dashboard Worker",
            "--profile",
            "python-agent",
            "--goal-file",
            str(goal_file),
            "--blueprint",
            "docs/blueprint.md",
            "--materialize",
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert payload["status"] == "materialized"
    assert payload["materialized"] is True
    assert roadmap["generated_by"] == "engineering-harness-goal-roadmap-planner"
    assert roadmap["goal"]["blueprint"] == "docs/blueprint.md"
    assert roadmap["experience"]["kind"] == "dashboard"
    assert roadmap["continuation"]["enabled"] is True
    assert [stage["id"] for stage in roadmap["continuation"]["stages"]] == [
        "stage-1-local-slice",
        "stage-2-experience-validation",
        "stage-3-policy-evidence-observability",
        "stage-4-unattended-drive-readiness",
    ]
    assert roadmap["continuation"]["stages"][0]["tasks"][0]["implementation"][0]["executor"] == "codex"
    assert roadmap["self_iteration"]["enabled"] is True
    assert Harness(project).validate_roadmap()["status"] == "passed"

    assert cli_main(
        [
            "plan-goal",
            "--project-root",
            str(project),
            "--name",
            "Autonomous Dashboard Worker",
            "--profile",
            "python-agent",
            "--goal-file",
            str(goal_file),
            "--materialize",
            "--json",
        ]
    ) == 2


def test_goal_planner_generates_python_behavioral_and_journey_gates(tmp_path):
    proposal = plan_goal_roadmap(
        project_root=tmp_path / "gated-python-agent",
        project_name="Gated Dashboard Agent",
        profile="python-agent",
        goal_text="Build a local dashboard agent that reads reports and lets an operator inspect E2E evidence.",
        desired_experience_kind="dashboard",
    )
    task = continuation_tasks(proposal["roadmap"])[0]

    acceptance_names = [item["name"] for item in task["acceptance"]]
    acceptance_commands = [item["command"] for item in task["acceptance"]]
    e2e_names = [item["name"] for item in task["e2e"]]
    e2e_commands = [item["command"] for item in task["e2e"]]
    implementation_prompt = task["implementation"][0]["prompt"]

    assert acceptance_names[0] == "python behavioral tests pass"
    assert acceptance_commands[0] == "python3 -m pytest tests -q"
    assert acceptance_names[-1] == "roadmap task contract smoke"
    assert "generated task contract is locally checkable" not in acceptance_names
    assert any(command == "python3 -m pytest tests/e2e -q" for command in e2e_commands)
    assert any("journey evidence" in name for name in e2e_names)
    assert "tests/e2e/" in implementation_prompt
    assert "docs/e2e/" in implementation_prompt
    assert "python3 -m pytest tests -q" in implementation_prompt
    assert validate_roadmap_payload(tmp_path, proposal["roadmap"])["status"] == "passed"


def test_goal_planner_generates_node_frontend_npm_gates(tmp_path):
    proposal = plan_goal_roadmap(
        project_root=tmp_path / "gated-node-frontend",
        project_name="Gated Frontend",
        profile="node-frontend",
        goal_text="Build an operator dashboard frontend with local journey evidence.",
        desired_experience_kind="dashboard",
    )
    task = continuation_tasks(proposal["roadmap"])[0]

    assert task["acceptance"][0]["name"] == "frontend unit and integration tests pass"
    assert task["acceptance"][0]["command"] == "npm test"
    assert task["e2e"][0]["command"] == "npm run e2e"
    assert "local npm `e2e` script" in task["implementation"][0]["prompt"]
    assert "roadmap task contract smoke" == task["acceptance"][-1]["name"]
    assert validate_roadmap_payload(tmp_path, proposal["roadmap"])["status"] == "passed"


def test_goal_planner_generates_cli_and_api_documented_example_gates(tmp_path):
    cli_proposal = plan_goal_roadmap(
        project_root=tmp_path / "gated-cli",
        project_name="Gated CLI",
        profile="python-agent",
        goal_text="Build a CLI tool that writes a deterministic local report.",
        desired_experience_kind="cli",
    )
    api_proposal = plan_goal_roadmap(
        project_root=tmp_path / "gated-api",
        project_name="Gated API",
        profile="python-agent",
        goal_text="Build a local API with a documented client example.",
        desired_experience_kind="api",
    )

    cli_task = continuation_tasks(cli_proposal["roadmap"])[0]
    api_task = continuation_tasks(api_proposal["roadmap"])[0]

    assert any("CLI example" in item["name"] for item in cli_task["acceptance"])
    assert any("examples/" in item.get("guidance", "") for item in cli_task["acceptance"])
    assert "tests/cli/" in cli_task["implementation"][0]["prompt"]
    assert any("API example" in item["name"] for item in api_task["acceptance"])
    assert any("tests/api/" in item.get("guidance", "") for item in api_task["acceptance"])
    assert "request" in api_task["implementation"][0]["prompt"]


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
    assert manifest["policy_input"]["schema_version"] == 1
    assert manifest["policy_input"]["project"]["name"] == "agent-project"
    assert manifest["policy_input"]["task"]["id"] == "tests"
    assert manifest["policy_input"]["file_scope"]["patterns"] == ["**"]
    assert manifest["policy_input"]["approvals"] == {
        "allow_manual": False,
        "allow_agent": False,
        "manual_required": False,
        "agent_required": False,
        "executor_agent_required": False,
    }
    assert manifest["policy_input"]["live"]["allow_live"] is False

    command_policy = policy_decision(manifest, "command_policy", outcome="allowed")
    assert command_policy["schema_version"] == 1
    assert command_policy["effect"] == "allow"
    assert command_policy["severity"] == "info"
    assert command_policy["input"]["phase"] == "acceptance"
    assert command_policy["input"]["command"]["executor"] == "shell"
    assert command_policy["input"]["executor"]["uses_command_policy"] is True
    assert policy_decision(manifest, "executor_policy", outcome="allowed")["executor"] == "shell"
    assert policy_decision(manifest, "executor_approval", outcome="allowed")["reason"] == "executor approval not required"
    assert policy_decision(manifest, "manual_approval", outcome="allowed")["reason"] == "manual approval not required"
    assert policy_decision(manifest, "live_approval", outcome="allowed")["reason"] == "live approval not required"
    assert policy_decision(manifest, "git_preflight", outcome="allowed")["status"] == "clean"
    assert policy_decision(manifest, "file_scope_guard", outcome="allowed")["status"] == "passed"
    summary = manifest["policy_decision_summary"]
    assert summary["total"] == len(manifest["policy_decisions"]) == 8
    assert summary["by_kind"] == {
        "agent_approval": 1,
        "command_policy": 1,
        "executor_approval": 1,
        "executor_policy": 1,
        "file_scope_guard": 1,
        "git_preflight": 1,
        "live_approval": 1,
        "manual_approval": 1,
    }
    assert summary["by_outcome"] == {"allowed": 8}
    assert summary["blocking"] == []
    report_evidence = report_policy_evidence(project, result)
    assert report_evidence["policy_decision_summary"] == summary
    assert report_evidence["policy_decisions"] == manifest["policy_decisions"]
    index = Harness(project).manifest_index()
    assert index["policy_decision_summary"]["total"] == summary["total"]
    assert index["manifests"][0]["policy_decision_summary"] == summary
    assert Harness(project).status_summary()["manifest_index"]["policy_decision_summary"]["total"] == summary["total"]

    state = json.loads((project / ".engineering/state/harness-state.json").read_text(encoding="utf-8"))
    assert state["tasks"]["tests"]["last_manifest"] == result["manifest"]


def test_opa_policy_input_export_shape(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0]["command"] = "python3 -c \"print('ok')\""
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    result = harness.run_task(harness.next_task())
    manifest = task_manifest(project, result)

    exported = export_policy_input_for_opa(manifest["policy_input"])

    assert exported["schema_version"] == 1
    assert exported["kind"] == "opa_rego_policy_input_export"
    assert exported["target"] == "opa-rego"
    assert exported["policy_input_schema_version"] == 1
    assert exported["policy_decision_schema_version"] == 1
    assert exported["authoritative_engine"] == "python"
    assert exported["external_evaluation"] == {
        "enabled": False,
        "decision_mode": "advisory",
        "runtime_dependency": None,
    }
    assert exported["rego"] == {
        "package": "engineering_harness.policy.v1",
        "entrypoint": "data.engineering_harness.policy.v1.decisions",
        "input_path": "input.policy_input",
    }
    assert exported["policy_input"] == manifest["policy_input"]

    exported["policy_input"]["task"]["id"] = "mutated"
    assert manifest["policy_input"]["task"]["id"] == "tests"
    serialized = json.loads(serialize_policy_input_for_opa(manifest["policy_input"]))
    assert serialized["policy_input"]["task"]["id"] == "tests"


def test_opa_policy_input_evaluation_stub_is_disabled_by_default():
    policy_input = {
        "schema_version": 1,
        "project": {"name": "agent-project"},
        "task": {"id": "tests"},
    }
    called = False

    def evaluator(_exported: dict) -> dict:
        nonlocal called
        called = True
        raise AssertionError("disabled OPA/Rego stub must not call evaluator")

    result = evaluate_opa_policy_input(policy_input, evaluator=evaluator)

    assert called is False
    assert result["enabled"] is False
    assert result["status"] == "disabled"
    assert result["authoritative"] is False
    assert result["authoritative_engine"] == "python"
    assert result["decision_mode"] == "disabled"
    assert result["decisions"] == []
    assert result["export"]["external_evaluation"]["enabled"] is False

    enabled_without_runtime = evaluate_opa_policy_input(policy_input, enabled=True)
    assert enabled_without_runtime["status"] == "not_configured"
    assert enabled_without_runtime["decisions"] == []


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
    assert index["policy_decision_summary"]["by_kind"]["command_policy"] == 2
    assert index["policy_decision_summary"]["by_kind"]["file_scope_guard"] == 2
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


def test_dagger_executor_is_discoverable_and_validates_command_payload(tmp_path):
    registry = default_executor_registry()
    assert "dagger" in registry.ids()
    assert registry.metadata_for("dagger")["adapter"] == "builtin.dagger"

    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0] = {
        "name": "dagger smoke",
        "executor": "dagger",
        "command": "call test --source=.",
    }
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    assert Harness(project).validate_roadmap()["status"] == "passed"

    roadmap["milestones"][0]["tasks"][0]["acceptance"][0].pop("command")
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")
    invalid = Harness(project).validate_roadmap()

    assert invalid["status"] == "failed"
    assert any("dagger command is required" in error for error in invalid["errors"])


def test_dagger_executor_selection_blocks_until_explicitly_enabled(tmp_path, monkeypatch):
    monkeypatch.delenv(DAGGER_ENABLE_ENV, raising=False)
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0] = {
        "name": "dagger smoke",
        "executor": "dagger",
        "command": "call test --source=.",
    }
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    result = harness.run_task(harness.next_task())

    assert result["status"] == "blocked"
    assert DAGGER_ENABLE_ENV in result["message"]
    run = result["runs"][0]
    assert run["executor"] == "dagger"
    assert run["command"] == "dagger call test --source=. <task:tests>"
    assert run["executor_metadata"]["kind"] == "container"
    assert run["executor_metadata"]["capabilities"][-1] == "requires_explicit_configuration"
    assert run["executor_result"]["status"] == "blocked"
    assert run["executor_result"]["metadata"] == {
        "configured": False,
        "required_environment": DAGGER_ENABLE_ENV,
    }


def test_dagger_executor_invokes_local_cli_when_enabled(tmp_path, monkeypatch):
    calls = []

    def fake_run(args, cwd, text, capture_output, timeout, env):
        calls.append(
            {
                "args": args,
                "cwd": cwd,
                "text": text,
                "capture_output": capture_output,
                "timeout": timeout,
                "env": env,
            }
        )
        return SimpleNamespace(returncode=0, stdout="dagger ok\n", stderr="")

    monkeypatch.setenv(DAGGER_ENABLE_ENV, "1")
    monkeypatch.setattr("engineering_harness.executors.subprocess.run", fake_run)
    invocation = ExecutorInvocation(
        project_root=tmp_path,
        task_id="dagger-task",
        name="dagger smoke",
        command="dagger call smoke --source=.",
        prompt=None,
        timeout_seconds=15,
    )

    result = DaggerExecutorAdapter().execute(invocation)

    assert result.status == "passed"
    assert result.stdout == "dagger ok\n"
    assert result.metadata == {"configured": True}
    assert calls[0]["args"] == ["dagger", "call", "smoke", "--source=."]
    assert calls[0]["cwd"] == tmp_path
    assert calls[0]["env"]["ENGINEERING_HARNESS"] == "1"
    assert calls[0]["env"][DAGGER_ENABLE_ENV] == "1"


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


def test_policy_decision_schema_records_denied_command(tmp_path):
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
    manifest = task_manifest(project, result)
    command_policy = policy_decision(manifest, "command_policy", outcome="denied")
    assert command_policy["effect"] == "deny"
    assert command_policy["severity"] == "error"
    assert command_policy["reason"] == "command prefix is not allowlisted"
    assert command_policy["input"]["command"]["command"] == "curl https://example.com"
    assert command_policy["input"]["executor"]["id"] == "shell"
    assert "python3 " in command_policy["metadata"]["allowed_prefixes"]
    assert manifest["policy_decision_summary"]["blocking"][0]["kind"] == "command_policy"
    assert manifest["policy_decision_summary"]["blocking"][0]["reason"] == "command prefix is not allowlisted"
    report_evidence = report_policy_evidence(project, result)
    assert report_evidence["policy_decision_summary"] == manifest["policy_decision_summary"]
    assert report_evidence["policy_decisions"] == manifest["policy_decisions"]
    index = Harness(project).manifest_index()
    assert index["policy_decision_summary"]["blocking"][0]["kind"] == "command_policy"


def test_policy_decision_schema_records_live_command_approval_gate(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0]["command"] = "python3 -c \"print('live')\" --live"
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    result = harness.run_task(harness.next_task())

    assert result["status"] == "blocked"
    manifest = task_manifest(project, result)
    command_policy = policy_decision(manifest, "command_policy", outcome="requires_approval")
    assert command_policy["effect"] == "requires_approval"
    assert command_policy["severity"] == "approval"
    assert command_policy["requires_approval"] is True
    assert command_policy["approval_flag"] == "--allow-live"
    assert command_policy["input"]["live"]["detected"] is True
    assert command_policy["input"]["live"]["matched_patterns"] == ["--live"]
    live = policy_decision(manifest, "live_approval", outcome="requires_approval")
    assert live["effect"] == "requires_approval"
    assert live["severity"] == "approval"
    assert live["requires_approval"] is True
    assert live["approval_flag"] == "--allow-live"
    assert live["metadata"]["matched_live_patterns"] == ["--live"]
    assert [item["kind"] for item in manifest["policy_decision_summary"]["requires_approval"]] == [
        "command_policy",
        "live_approval",
    ]
    report_evidence = report_policy_evidence(project, result)
    assert report_evidence["policy_decision_summary"] == manifest["policy_decision_summary"]
    assert report_evidence["policy_decisions"] == manifest["policy_decisions"]


def test_policy_decision_schema_records_manual_approval_gate(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["manual_approval_required"] = True
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0]["command"] = "python3 -c \"print('ok')\""
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    result = harness.run_task(harness.next_task())

    assert result["status"] == "blocked"
    manifest = task_manifest(project, result)
    manual = policy_decision(manifest, "manual_approval", outcome="requires_approval")
    assert manual["effect"] == "requires_approval"
    assert manual["requires_approval"] is True
    assert manual["approval_flag"] == "--allow-manual"
    assert manual["input"]["approvals"]["manual_required"] is True
    assert manual["input"]["approvals"]["allow_manual"] is False
    report_evidence = report_policy_evidence(project, result)
    assert report_evidence["policy_decision_summary"] == manifest["policy_decision_summary"]
    assert report_evidence["policy_decisions"] == manifest["policy_decisions"]


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


def test_phase_level_state_is_durable_and_ordered_for_repairing_e2e_task(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    task = roadmap["milestones"][0]["tasks"][0]
    task["max_task_iterations"] = 2
    task["implementation"] = [
        {
            "name": "write implementation marker",
            "command": "python3 -c \"from pathlib import Path; Path('implemented.txt').write_text('ok')\"",
        }
    ]
    task["repair"] = [
        {
            "name": "repair acceptance marker",
            "command": "python3 -c \"from pathlib import Path; Path('repair.txt').write_text('fixed')\"",
        }
    ]
    task["acceptance"][0] = {
        "name": "accepts after repair",
        "command": (
            "python3 -c \"from pathlib import Path; "
            "assert Path('implemented.txt').read_text() == 'ok'; "
            "assert Path('repair.txt').read_text() == 'fixed'\""
        ),
    }
    task["e2e"] = [
        {
            "name": "simulate repaired user path",
            "command": "python3 -c \"from pathlib import Path; Path('e2e.txt').write_text('ok')\"",
        }
    ]
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    result = harness.run_task(harness.next_task(), allow_agent=True)

    assert result["status"] == "passed"
    state = json.loads((project / ".engineering/state/harness-state.json").read_text(encoding="utf-8"))
    task_state = state["tasks"]["tests"]
    history = task_state["phase_history"]

    assert [item["sequence"] for item in history] == list(range(1, len(history) + 1))
    assert [(item["phase"], item["event"]) for item in history] == [
        ("implementation", "before"),
        ("implementation", "after"),
        ("acceptance-1", "before"),
        ("acceptance-1", "after"),
        ("repair-1", "before"),
        ("repair-1", "after"),
        ("acceptance-2", "before"),
        ("acceptance-2", "after"),
        ("e2e", "before"),
        ("e2e", "after"),
        ("file-scope-guard", "before"),
        ("file-scope-guard", "after"),
        ("manifest-writing", "before"),
        ("manifest-writing", "after"),
        ("final-result", "before"),
        ("final-result", "after"),
    ]

    after_by_phase = {item["phase"]: item for item in history if item["event"] == "after"}
    assert after_by_phase["implementation"]["status"] == "passed"
    assert after_by_phase["acceptance-1"]["status"] == "failed"
    assert after_by_phase["acceptance-1"]["runs"][0]["returncode"] != 0
    assert after_by_phase["repair-1"]["status"] == "passed"
    assert after_by_phase["acceptance-2"]["status"] == "passed"
    assert after_by_phase["e2e"]["status"] == "passed"
    assert after_by_phase["file-scope-guard"]["status"] == "skipped"
    assert after_by_phase["manifest-writing"]["metadata"]["manifest_path"] == result["manifest"]
    assert after_by_phase["final-result"]["status"] == "passed"
    assert task_state["current_phase"] is None
    assert task_state["last_phase_event"]["phase"] == "final-result"
    assert task_state["phase_states"]["acceptance-1"]["status"] == "failed"


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
    manifest = task_manifest(project, result)
    file_scope = policy_decision(manifest, "file_scope_guard", outcome="denied")
    assert file_scope["effect"] == "deny"
    assert file_scope["status"] == "failed"
    assert file_scope["input"]["file_scope"]["patterns"] == ["src/**"]
    assert file_scope["metadata"]["violations"] == ["outside.txt"]
    assert manifest["policy_decision_summary"]["blocking"][0]["kind"] == "file_scope_guard"
    report_evidence = report_policy_evidence(project, result)
    assert report_evidence["policy_decision_summary"] == manifest["policy_decision_summary"]
    assert report_evidence["policy_decisions"] == manifest["policy_decisions"]
    index = Harness(project).manifest_index()
    assert index["policy_decision_summary"]["blocking"][0]["kind"] == "file_scope_guard"


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


def test_policy_decision_schema_records_dirty_worktree_warning(tmp_path):
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "harness@example.invalid"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Harness Test"], cwd=project, check=True)
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0]["command"] = "python3 -c \"print('ok')\""
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=project, check=True, capture_output=True, text=True)
    (project / "preexisting.txt").write_text("dirty before task", encoding="utf-8")

    harness = Harness(project)
    result = harness.run_task(harness.next_task())

    assert result["status"] == "passed"
    manifest = task_manifest(project, result)
    warning = policy_decision(manifest, "git_preflight", outcome="warning")
    assert warning["effect"] == "warn"
    assert warning["severity"] == "warning"
    assert warning["status"] == "dirty"
    assert warning["input"]["worktree"]["dirty_before_paths"] == ["preexisting.txt"]
    assert warning["metadata"]["dirty_before_paths"] == ["preexisting.txt"]


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

    harness = Harness(project)
    result = harness.validate_roadmap()

    assert result["status"] == "passed"
    assert result["errors"] == []
    assert harness.status_summary()["experience"]["source"] == "derived"


@pytest.mark.parametrize(
    ("project_name", "roadmap_updates", "task_title", "expected_kind", "expected_persona", "auth_required"),
    [
        (
            "autonomous-worker",
            {},
            "Run the autonomous research worker and inspect latest artifacts.",
            "dashboard",
            "operator",
            False,
        ),
        (
            "student-review",
            {},
            "Build the student submission review workflow with reviewer comments and revision decisions.",
            "submission-review",
            "student",
            True,
        ),
        (
            "role-operations",
            {},
            "Define a multi-role admin operator approver flow with login, permissions, and audit log.",
            "multi-role-app",
            "admin",
            True,
        ),
        (
            "api-service",
            {"project_kind": "api"},
            "Validate REST API OpenAPI endpoints with a documented client example.",
            "api-only",
            "api client",
            False,
        ),
        (
            "cli-tool",
            {"project_kind": "cli"},
            "Validate the CLI command-line journey and documented command output.",
            "cli-only",
            "developer",
            False,
        ),
    ],
)
def test_default_frontend_experience_planner_derives_common_cases(
    tmp_path,
    project_name,
    roadmap_updates,
    task_title,
    expected_kind,
    expected_persona,
    auth_required,
):
    roadmap = roadmap_without_experience(project_name, task_title=task_title)
    roadmap.update(roadmap_updates)

    summary = status_summary_for_roadmap(tmp_path, project_name, roadmap)
    experience = summary["experience"]

    assert experience["source"] == "derived"
    assert experience["derived"] is True
    assert experience["kind"] == expected_kind
    assert experience["recommendation"] == expected_kind
    assert expected_persona in experience["personas"]
    assert experience["auth"]["required"] is auth_required
    assert experience["primary_surfaces"]
    assert experience["e2e_journeys"]


@pytest.mark.parametrize(
    ("fixture_name", "expected_kind", "expected_scope", "expected_term"),
    [
        ("valid/dashboard.json", "dashboard", "frontend/**", "artifact"),
        ("valid/submission-review.json", "submission-review", "frontend/**", "revision"),
        ("valid/multi-role-app.json", "multi-role-app", "frontend/**", "audit"),
        ("valid/api-only.json", "api-only", "openapi/**", "openapi"),
        ("valid/cli-only.json", "cli-only", "cli/**", "documented commands"),
    ],
)
def test_frontend_task_generator_proposes_kind_specific_tasks(
    tmp_path,
    fixture_name,
    expected_kind,
    expected_scope,
    expected_term,
):
    project = tmp_path / expected_kind
    project.mkdir()
    engineering_dir = project / ".engineering"
    engineering_dir.mkdir()
    roadmap_path = engineering_dir / "roadmap.yaml"
    roadmap_path.write_text((ROADMAP_FIXTURES / fixture_name).read_text(encoding="utf-8"), encoding="utf-8")

    result = Harness(project).frontend_task_plan()

    assert result["status"] == "proposed"
    assert result["materialized"] is False
    assert result["experience"]["kind"] == expected_kind
    assert result["milestone"]["id"] == "frontend-visualization"
    assert result["milestone"]["generated_by"] == "engineering-harness-frontend-task-generator"
    assert len(result["tasks"]) == 1 + len(result["experience"]["e2e_journeys"])

    contract_task = result["tasks"][0]
    assert contract_task["frontend"]["task_kind"] == "experience-contract"
    assert contract_task["file_scope"] == ["docs/**", "tests/**", "templates/**"]
    assert contract_task["acceptance"][0]["command"].startswith("python3 ")
    assert contract_task["e2e"]

    journey_task = next(task for task in result["tasks"] if task["frontend"]["task_kind"] == "journey-check")
    assert expected_scope in journey_task["file_scope"]
    assert journey_task["acceptance"][0]["command"].startswith("python3 ")
    assert journey_task["e2e"][0]["command"].startswith("python3 ")
    assert expected_term in json.dumps(journey_task).lower()
    assert "use existing project conventions" in journey_task["frontend"]["stack_policy"]


def test_frontend_task_generator_materializes_derived_plan_and_validates(tmp_path):
    project = tmp_path / "api-service"
    project.mkdir()
    engineering_dir = project / ".engineering"
    engineering_dir.mkdir()
    roadmap_path = engineering_dir / "roadmap.yaml"
    roadmap = roadmap_without_experience(
        "api-service",
        task_title="Validate REST API OpenAPI endpoints with a documented client example.",
    )
    roadmap["project_kind"] = "api"
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    proposal = Harness(project).frontend_task_plan()
    assert proposal["status"] == "proposed"
    assert proposal["experience"]["source"] == "derived"
    assert "frontend-visualization" not in roadmap_path.read_text(encoding="utf-8")

    result = Harness(project).materialize_frontend_tasks(reason="test")

    assert result["status"] == "materialized"
    assert result["materialized"] is True
    assert result["tasks_added"] == 2
    updated = json.loads(roadmap_path.read_text(encoding="utf-8"))
    generated = updated["milestones"][-1]
    assert generated["id"] == "frontend-visualization"
    assert generated["experience_kind"] == "api-only"
    assert generated["experience_source"] == "derived"
    assert generated["tasks"][1]["file_scope"] == [
        "src/**",
        "api/**",
        "openapi/**",
        "docs/**",
        "examples/**",
        "tests/**",
        "templates/**",
        "package.json",
        "pyproject.toml",
    ]
    assert generated["tasks"][1]["frontend"]["candidate_check_paths"]
    assert Harness(project).validate_roadmap()["status"] == "passed"

    log_path = project / ".engineering/state/decision-log.jsonl"
    assert "frontend_task_generation" in log_path.read_text(encoding="utf-8")
    assert Harness(project).materialize_frontend_tasks()["status"] == "skipped"


def test_frontend_tasks_cli_proposes_by_default_and_materializes_on_flag(tmp_path):
    project = tmp_path / "cli-tool"
    project.mkdir()
    init_project(project, "python-agent", name="cli-tool")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["title"] = "Validate CLI command output and generated reports."
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0]["command"] = "python3 -c \"print('ok')\""
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")
    before = roadmap_path.read_text(encoding="utf-8")

    propose_exit = cli_main(["frontend-tasks", "--project-root", str(project), "--json"])
    after_proposal = roadmap_path.read_text(encoding="utf-8")
    materialize_exit = cli_main(["frontend-tasks", "--project-root", str(project), "--materialize", "--json"])

    assert propose_exit == 0
    assert after_proposal == before
    assert materialize_exit == 0
    updated = json.loads(roadmap_path.read_text(encoding="utf-8"))
    generated = updated["milestones"][-1]
    assert generated["id"] == "frontend-visualization"
    assert generated["experience_kind"] == "cli-only"
    assert any("cli/**" in task["file_scope"] for task in generated["tasks"])


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
    assert policy_decision(manifest, "executor_policy", outcome="allowed")["executor"] == "codex"
    executor_approval = policy_decision(manifest, "executor_approval", outcome="requires_approval")
    assert executor_approval["effect"] == "requires_approval"
    assert executor_approval["approval_flag"] == "--allow-agent"
    assert executor_approval["requires_approval"] is True


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


def test_drive_pause_resume_and_cancel_controls_are_durable(tmp_path):
    project = tmp_path / "paused-project"
    project.mkdir()
    init_project(project, "python-agent", name="paused-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0]["command"] = (
        "python3 -c \"from pathlib import Path; Path('pause-marker.txt').write_text('ok')\""
    )
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    assert cli_main(["pause", "--project-root", str(project), "--reason", "test_pause"]) == 0
    assert cli_main(["drive", "--project-root", str(project)]) == 0
    assert not (project / "pause-marker.txt").exists()

    state = json.loads((project / ".engineering/state/harness-state.json").read_text(encoding="utf-8"))
    assert state["drive_control"]["status"] == "paused"
    assert state["drive_control"]["pause_requested"] is True
    assert "tests" not in state.get("tasks", {})

    assert cli_main(["resume", "--project-root", str(project), "--reason", "test_resume"]) == 0
    assert cli_main(["drive", "--project-root", str(project)]) == 0
    assert (project / "pause-marker.txt").read_text(encoding="utf-8") == "ok"

    state = json.loads((project / ".engineering/state/harness-state.json").read_text(encoding="utf-8"))
    assert state["drive_control"]["status"] == "idle"
    assert state["drive_control"]["last_drive_status"] == "completed"
    assert state["tasks"]["tests"]["status"] == "passed"

    cancelled = tmp_path / "cancelled-project"
    cancelled.mkdir()
    init_project(cancelled, "python-agent", name="cancelled-project")
    cancelled_roadmap_path = cancelled / ".engineering/roadmap.yaml"
    cancelled_roadmap = json.loads(cancelled_roadmap_path.read_text(encoding="utf-8"))
    cancelled_roadmap["milestones"][0]["tasks"][0]["acceptance"][0]["command"] = (
        "python3 -c \"from pathlib import Path; Path('cancel-marker.txt').write_text('ok')\""
    )
    cancelled_roadmap_path.write_text(json.dumps(cancelled_roadmap), encoding="utf-8")

    assert cli_main(["cancel", "--project-root", str(cancelled), "--reason", "test_cancel"]) == 0
    assert cli_main(["drive", "--project-root", str(cancelled)]) == 1
    assert not (cancelled / "cancel-marker.txt").exists()
    state = json.loads((cancelled / ".engineering/state/harness-state.json").read_text(encoding="utf-8"))
    assert state["drive_control"]["status"] == "cancelled"
    assert state["drive_control"]["cancel_requested"] is True


def test_drive_watchdog_heartbeat_records_running_owner_and_task(tmp_path):
    project = tmp_path / "watchdog-running-project"
    project.mkdir()
    init_project(project, "python-agent", name="watchdog-running-project")

    harness = Harness(project)
    start = harness.start_drive()
    task = harness.next_task()
    assert start["started"] is True
    assert task is not None

    heartbeat = harness.drive_heartbeat(
        activity="acceptance-1:command",
        message="running acceptance command",
        task=task,
        phase="acceptance-1",
    )

    assert heartbeat is not None
    summary = Harness(project).status_summary()["drive_control"]
    assert summary["status"] == "running"
    assert summary["active"] is True
    assert summary["pid"] == os.getpid()
    assert summary["started_at"]
    assert summary["last_heartbeat_at"]
    assert summary["heartbeat_count"] >= 2
    assert summary["current_activity"] == "acceptance-1:command"
    assert summary["current_task"]["id"] == "tests"
    assert summary["current_task"]["phase"] == "acceptance-1"
    assert summary["last_progress_message"] == "running acceptance command"
    assert summary["watchdog"]["status"] == "running"
    assert summary["watchdog"]["stale"] is False

    Harness(project).finish_drive(status="completed", message="test complete")


def test_drive_watchdog_detects_stale_pid_and_resume_recovers(tmp_path):
    project = tmp_path / "watchdog-stale-pid-project"
    project.mkdir()
    init_project(project, "python-agent", name="watchdog-stale-pid-project")

    harness = Harness(project)
    assert harness.start_drive()["started"] is True
    state = harness_state(project)
    state["drive_control"]["pid"] = unused_pid()
    state["drive_control"]["last_heartbeat_at"] = utc_now()
    write_harness_state(project, state)

    summary = Harness(project).status_summary()["drive_control"]
    assert summary["status"] == "stale"
    assert summary["active"] is False
    assert summary["watchdog"]["stale"] is True
    assert summary["watchdog"]["reason"] == "pid_gone"

    start = Harness(project).start_drive()
    assert start["started"] is False
    assert start["status"] == "stale"
    state = harness_state(project)
    assert state["drive_control"]["status"] == "stale"
    assert state["drive_control"]["stale_reason"] == "pid_gone"

    resumed = Harness(project).set_drive_control("resume", reason="recover stale pid")
    assert resumed["status"] == "idle"
    assert resumed["drive_control"]["active"] is False
    assert resumed["drive_control"]["pid"] is None
    assert resumed["drive_control"]["stale_reason"] is None

    restarted = Harness(project).start_drive()
    assert restarted["started"] is True
    Harness(project).finish_drive(status="completed", message="recovered")


def test_drive_watchdog_detects_stale_heartbeat_and_resume_recovers(tmp_path):
    project = tmp_path / "watchdog-stale-heartbeat-project"
    project.mkdir()
    init_project(project, "python-agent", name="watchdog-stale-heartbeat-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["drive_watchdog"] = {"stale_after_seconds": 5}
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    harness = Harness(project)
    assert harness.start_drive()["started"] is True
    state = harness_state(project)
    state["drive_control"]["pid"] = os.getpid()
    state["drive_control"]["last_heartbeat_at"] = "2000-01-01T00:00:00Z"
    write_harness_state(project, state)

    summary = Harness(project).status_summary()["drive_control"]
    assert summary["status"] == "stale"
    assert summary["watchdog"]["stale"] is True
    assert summary["watchdog"]["reason"] == "heartbeat_stale"
    assert summary["watchdog"]["threshold_seconds"] == 5
    assert summary["watchdog"]["heartbeat_age_seconds"] > 5

    start = Harness(project).start_drive()
    assert start["started"] is False
    assert start["status"] == "stale"

    resumed = Harness(project).set_drive_control("resume", reason="recover stale heartbeat")
    assert resumed["status"] == "idle"
    assert resumed["drive_control"]["stale_reason"] is None

    restarted = Harness(project).start_drive()
    assert restarted["started"] is True
    Harness(project).finish_drive(status="completed", message="recovered")


def test_drive_watchdog_status_output_includes_heartbeat_metadata(tmp_path, capsys):
    project = tmp_path / "watchdog-status-project"
    project.mkdir()
    init_project(project, "python-agent", name="watchdog-status-project")

    harness = Harness(project)
    assert harness.start_drive()["started"] is True
    harness.drive_heartbeat(activity="drive-loop", message="status test heartbeat", clear_task=True)

    assert cli_main(["status", "--project-root", str(project), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    drive_control = payload["drive_control"]
    assert drive_control["status"] == "running"
    assert drive_control["pid"] == os.getpid()
    assert drive_control["current_activity"] == "drive-loop"
    assert drive_control["last_progress_message"] == "status test heartbeat"
    assert drive_control["watchdog"]["status"] == "running"
    assert drive_control["watchdog"]["stale"] is False

    assert cli_main(["status", "--project-root", str(project)]) == 0
    text = capsys.readouterr().out
    assert "Drive control: running" in text
    assert "Drive watchdog: running" in text
    assert "Drive activity: drive-loop" in text
    assert "Drive progress: status test heartbeat" in text

    Harness(project).finish_drive(status="completed", message="status output checked")


@pytest.mark.parametrize(
    ("gate", "approval_kind", "decision_kind", "marker"),
    [
        ("manual", "manual", "manual_approval", "manual-marker.txt"),
        ("live", "live", "live_approval", "live-marker.txt"),
        ("agent", "agent", "agent_approval", "agent-marker.txt"),
    ],
)
def test_approval_queue_unblocks_manual_live_and_agent_gates(
    tmp_path,
    gate,
    approval_kind,
    decision_kind,
    marker,
):
    project = tmp_path / f"{gate}-approval-project"
    project.mkdir()
    init_project(project, "python-agent", name=f"{gate}-approval-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    task = roadmap["milestones"][0]["tasks"][0]
    task["acceptance"][0]["command"] = (
        f"python3 -c \"from pathlib import Path; Path('{marker}').write_text('ok')\""
    )
    if gate == "manual":
        task["manual_approval_required"] = True
    elif gate == "agent":
        task["agent_approval_required"] = True
    elif gate == "live":
        task["acceptance"][0]["command"] += " --live"
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    assert cli_main(["drive", "--project-root", str(project)]) == 1
    assert not (project / marker).exists()
    assert cli_main(["approvals", "--project-root", str(project), "--json"]) == 0

    state = json.loads((project / ".engineering/state/harness-state.json").read_text(encoding="utf-8"))
    pending = [
        item
        for item in state["approval_queue"]["items"].values()
        if item["status"] == "pending"
    ]
    assert len(pending) == 1
    approval = pending[0]
    assert approval["approval_kind"] == approval_kind
    assert approval["decision_kind"] == decision_kind
    assert state["tasks"]["tests"]["status"] == "blocked"
    assert state["tasks"]["tests"]["attempts"] == 0

    assert cli_main(["approve", "--project-root", str(project), approval["id"], "--reason", "test approval"]) == 0
    state = json.loads((project / ".engineering/state/harness-state.json").read_text(encoding="utf-8"))
    assert state["approval_queue"]["items"][approval["id"]]["status"] == "approved"
    assert state["tasks"]["tests"]["status"] == "pending"

    assert cli_main(["drive", "--project-root", str(project)]) == 0
    assert (project / marker).read_text(encoding="utf-8") == "ok"
    state = json.loads((project / ".engineering/state/harness-state.json").read_text(encoding="utf-8"))
    assert state["approval_queue"]["items"][approval["id"]]["status"] == "consumed"
    assert state["tasks"]["tests"]["status"] == "passed"


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
    state = json.loads((project / ".engineering/state/harness-state.json").read_text(encoding="utf-8"))
    checkpoint_events = [
        event for event in state["tasks"]["tests"]["phase_history"] if event["phase"] == "checkpoint-intent"
    ]
    assert [(event["event"], event["status"]) for event in checkpoint_events] == [
        ("before", "running"),
        ("after", "committed"),
    ]
    assert checkpoint_events[-1]["metadata"]["commit"]


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
                        "e2e": [{"name": "journey ok", "command": "python3 -c \"print('e2e')\""}],
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
    assert updated["milestones"][0]["tasks"][0]["e2e"][0]["name"] == "journey ok"


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


def test_drive_report_goal_gap_retrospective_budget_exhausted(tmp_path, capsys):
    project = tmp_path / "goal-gap-budget-project"
    project.mkdir()
    init_project(project, "python-agent", name="goal-gap-budget-project")
    (project / "tests").mkdir()
    (project / "tests/test_goal_gap.py").write_text("def test_marker():\n    assert True\n", encoding="utf-8")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"] = [
        {
            "id": "first-task",
            "title": "First Task",
            "status": "pending",
            "file_scope": ["tests/**"],
            "acceptance": [{"name": "first ok", "command": "python3 -c \"print('first ok')\""}],
        },
        {
            "id": "second-task",
            "title": "Second Task",
            "status": "pending",
            "file_scope": ["tests/**"],
            "acceptance": [{"name": "second ok", "command": "python3 -c \"print('second ok')\""}],
        },
    ]
    roadmap["self_iteration"] = {
        "enabled": True,
        "objective": "Plan the next reliability hardening stage.",
        "planner": {"name": "local planner", "command": "python3 -c \"print('planner placeholder')\""},
    }
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")
    init_git_repo(project)

    exit_code = cli_main(["drive", "--project-root", str(project), "--max-tasks", "1", "--json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "budget_exhausted"
    retrospective = payload["goal_gap_retrospective"]
    assert retrospective["kind"] == "engineering-harness.goal-gap-retrospective"
    assert retrospective["trigger"]["stop_class"] == "budget_exhausted"
    assert retrospective["task_counts"]["pending"] == 1
    assert retrospective["evidence"]["manifest_index"]["manifest_count"] == 1
    assert retrospective["evidence"]["latest_reports"]["task_reports"]["included_count"] == 1
    assert retrospective["evidence"]["tests"]["total_count"] == 1
    assert retrospective["evidence"]["git"]["is_repository"] is True
    assert retrospective["request_self_iteration"]["recommended"] is False
    assert retrospective["request_self_iteration"]["blocked_by"] == ["budget_exhausted", "pending_task_queue"]
    risk_ids = {item["id"] for item in retrospective["remaining_risks"]}
    assert {"budget_exhausted", "pending_roadmap_tasks"}.issubset(risk_ids)
    theme_ids = {item["id"] for item in retrospective["likely_next_stage_themes"]}
    assert "drain-queued-tasks" in theme_ids
    assert drive_report_goal_gap_retrospective(project, payload["drive_report"]) == retrospective
    sidecar = json.loads((project / payload["drive_report_json"]).read_text(encoding="utf-8"))
    assert sidecar["goal_gap_retrospective"] == retrospective


def test_drive_report_goal_gap_retrospective_queue_empty_requests_self_iteration(tmp_path, capsys):
    project = tmp_path / "goal-gap-empty-project"
    project.mkdir()
    init_project(project, "python-agent", name="goal-gap-empty-project")
    (project / "tests").mkdir()
    (project / "tests/test_goal_gap.py").write_text("def test_marker():\n    assert True\n", encoding="utf-8")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0]["command"] = "python3 -c \"print('queue ok')\""
    roadmap["continuation"] = {"enabled": True, "goal": "Continue reliability hardening.", "stages": []}
    roadmap["self_iteration"] = {
        "enabled": True,
        "objective": "Plan the next reliability hardening stage.",
        "planner": {"name": "local planner", "command": "python3 -c \"print('planner placeholder')\""},
    }
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")
    init_git_repo(project)

    exit_code = cli_main(["drive", "--project-root", str(project), "--json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "completed"
    assert payload["message"] == "Roadmap queue is empty."
    retrospective = payload["goal_gap_retrospective"]
    assert retrospective["trigger"]["stop_class"] == "queue_empty"
    assert retrospective["request_self_iteration"]["recommended"] is True
    assert retrospective["request_self_iteration"]["blocked_by"] == []
    assert retrospective["evidence"]["self_iteration_context_packs"]["included_count"] == 0
    risk_ids = {item["id"] for item in retrospective["remaining_risks"]}
    assert {"roadmap_queue_empty", "self_iteration_context_not_refreshed"}.issubset(risk_ids)
    theme_ids = {item["id"] for item in retrospective["likely_next_stage_themes"]}
    assert "request-self-iteration" in theme_ids
    report_text = (project / payload["drive_report"]).read_text(encoding="utf-8")
    assert "Request self-iteration: `yes`" in report_text
    assert drive_report_goal_gap_retrospective(project, payload["drive_report"]) == retrospective
    sidecar = json.loads((project / payload["drive_report_json"]).read_text(encoding="utf-8"))
    assert sidecar["goal_gap_retrospective"] == retrospective


def test_drive_rolling_commit_after_task_checkpoints_materialization_before_generated_task(tmp_path, capsys):
    project = tmp_path / "rolling-checkpoint-project"
    project.mkdir()
    init_project(project, "python-agent", name="rolling-checkpoint-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"] = []
    roadmap["continuation"] = {
        "enabled": True,
        "goal": "Continue with generated tasks.",
        "stages": [
            {
                "id": "stage-a",
                "title": "Stage A",
                "objective": "Create an in-scope generated marker.",
                "tasks": [
                    {
                        "id": "generated-test",
                        "title": "Generated Test",
                        "file_scope": ["generated.txt"],
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
    init_git_repo(project)

    exit_code = cli_main(
        [
            "drive",
            "--project-root",
            str(project),
            "--rolling",
            "--max-continuations",
            "1",
            "--max-tasks",
            "1",
            "--commit-after-task",
            "--json",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    materialization = payload["continuations"][0]["materialization_checkpoint"]
    task_git = payload["results"][0]["git"]
    assert materialization["status"] == "committed"
    assert materialization["dirty_before_paths"] == []
    assert materialization["materialization_paths"] == [".engineering/roadmap.yaml"]
    assert task_git["status"] == "committed"
    subjects = subprocess.check_output(
        ["git", "log", "--format=%s", "-3"],
        cwd=project,
        text=True,
    ).splitlines()
    assert subjects == [
        "chore(engineering): complete generated-test",
        "chore(engineering): materialize roadmap continuation: stage-a",
        "initial",
    ]
    assert subprocess.check_output(["git", "status", "--porcelain"], cwd=project, text=True).strip() == ""
    report = project / payload["drive_report"]
    report_text = report.read_text(encoding="utf-8")
    assert "Materialization checkpoint: `committed`" in report_text
    assert "No task checkpoint deferral was recorded." in report_text


def test_drive_rolling_commit_after_task_defers_when_materialization_has_user_dirtiness(tmp_path, capsys):
    project = tmp_path / "rolling-checkpoint-dirty-project"
    project.mkdir()
    init_project(project, "python-agent", name="rolling-checkpoint-dirty-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["milestones"] = []
    roadmap["continuation"] = {
        "enabled": True,
        "goal": "Continue with generated tasks.",
        "stages": [
            {
                "id": "stage-a",
                "title": "Stage A",
                "objective": "Create an in-scope generated marker.",
                "tasks": [
                    {
                        "id": "generated-test",
                        "title": "Generated Test",
                        "file_scope": ["generated.txt"],
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
    init_git_repo(project)
    (project / "user.txt").write_text("user change", encoding="utf-8")

    exit_code = cli_main(
        [
            "drive",
            "--project-root",
            str(project),
            "--rolling",
            "--max-continuations",
            "1",
            "--max-tasks",
            "1",
            "--commit-after-task",
            "--json",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    materialization = payload["continuations"][0]["materialization_checkpoint"]
    task_git = payload["results"][0]["git"]
    assert materialization["status"] == "deferred"
    assert materialization["reason"] == "preexisting_dirty_worktree"
    assert materialization["dirty_before_paths"] == ["user.txt"]
    assert task_git["status"] == "deferred"
    assert task_git["reason"] == "roadmap_materialization_checkpoint_deferred"
    assert "pre-existing user changes" in task_git["message"]
    subjects = subprocess.check_output(["git", "log", "--format=%s"], cwd=project, text=True).splitlines()
    assert subjects == ["initial"]
    dirty_paths = subprocess.check_output(["git", "status", "--porcelain"], cwd=project, text=True)
    assert ".engineering/roadmap.yaml" in dirty_paths
    assert "generated.txt" in dirty_paths
    assert "user.txt" in dirty_paths
    state = json.loads((project / ".engineering/state/harness-state.json").read_text(encoding="utf-8"))
    checkpoint_events = [
        event for event in state["tasks"]["generated-test"]["phase_history"] if event["phase"] == "checkpoint-intent"
    ]
    assert [(event["event"], event["status"]) for event in checkpoint_events] == [
        ("before", "running"),
        ("after", "deferred"),
    ]
    report = project / payload["drive_report"]
    report_text = report.read_text(encoding="utf-8")
    assert "Materialization checkpoint: `deferred`" in report_text
    assert "Dirty before materialization: `user.txt`" in report_text
    assert "Task `generated-test` checkpoint deferred" in report_text


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


def self_iteration_guard_project(tmp_path: Path, *, max_stages_per_iteration: int = 1) -> tuple[Path, Path]:
    project = tmp_path / "agent-project"
    project.mkdir()
    init_project(project, "python-agent", name="agent-project")
    roadmap_path = project / ".engineering/roadmap.yaml"
    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["continuation"] = {"enabled": True, "goal": "Continue autonomously.", "stages": []}
    roadmap["self_iteration"] = {
        "enabled": True,
        "objective": "Add the next generated test stage.",
        "max_stages_per_iteration": max_stages_per_iteration,
        "planner": {"name": "test planner", "command": "python3 planner.py"},
    }
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")
    return project, roadmap_path


def valid_self_iteration_stage(stage_id: str = "guard-stage", task_id: str = "guard-task") -> dict:
    return {
        "id": stage_id,
        "title": "Guard Stage",
        "objective": "Add a locally verifiable generated task.",
        "tasks": [
            {
                "id": task_id,
                "title": "Guard Task",
                "file_scope": ["src/**", "tests/**", "docs/**"],
                "implementation": [
                    {
                        "name": "implement guard task",
                        "executor": "codex",
                        "prompt": "Implement the focused local guard task.",
                    }
                ],
                "repair": [
                    {
                        "name": "repair guard task",
                        "executor": "codex",
                        "prompt": "Repair the focused local guard task if validation fails.",
                    }
                ],
                "acceptance": [
                    {
                        "name": "local guard smoke",
                        "command": "python3 -c \"print('guard ok')\"",
                        "timeout_seconds": 30,
                    }
                ],
            }
        ],
    }


def write_self_iteration_guard_planner(project: Path, stages: list[dict], *, mutation: str = "") -> None:
    stages_json = json.dumps(stages, indent=2)
    (project / "planner.py").write_text(
        f"""
import json
from pathlib import Path

roadmap_path = Path(".engineering/roadmap.yaml")
roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
{mutation}
continuation = roadmap.setdefault("continuation", {{"enabled": True, "goal": "Continue autonomously.", "stages": []}})
continuation["enabled"] = True
continuation.setdefault("stages", []).extend({stages_json})
roadmap_path.write_text(json.dumps(roadmap, indent=2), encoding="utf-8")
""".strip()
        + "\n",
        encoding="utf-8",
    )


def self_iteration_context_pack_project(tmp_path: Path) -> tuple[Path, Path]:
    project, roadmap_path = self_iteration_guard_project(tmp_path)
    (project / "docs").mkdir(exist_ok=True)
    (project / "docs/blueprint.md").write_text(
        "# Blueprint\n\nBuild from local reports. OPENAI_API_KEY=sk-context-secret\n",
        encoding="utf-8",
    )
    (project / "docs/operator.md").write_text("# Operator Notes\n\nUse local task reports.\n", encoding="utf-8")
    (project / "src").mkdir(exist_ok=True)
    (project / "src/app.py").write_text("def marker():\n    return 'context-pack'\n", encoding="utf-8")
    (project / "tests").mkdir(exist_ok=True)
    (project / "tests/test_app.py").write_text(
        "def test_marker():\n    assert 'context' in 'context-pack'\n",
        encoding="utf-8",
    )

    roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
    roadmap["goal"] = {
        "text": "Use current local reports to plan the next stage.",
        "blueprint": "docs/blueprint.md",
        "constraints": ["local-only"],
    }
    roadmap["milestones"][0]["tasks"][0]["acceptance"][0]["command"] = "python3 -c \"print('context manifest ok')\""
    roadmap["self_iteration"]["planner"] = {
        "name": "context pack planner",
        "executor": "context-planner",
        "prompt": "Read the context pack and append the next local stage.",
    }
    roadmap_path.write_text(json.dumps(roadmap), encoding="utf-8")

    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "harness@example.invalid"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Harness Test"], cwd=project, check=True)
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "initial context"], cwd=project, check=True, capture_output=True, text=True)

    assert cli_main(["drive", "--project-root", str(project), "--max-tasks", "1", "--json"]) == 0
    return project, roadmap_path


def context_pack_stage(stage_id: str = "context-pack-stage", task_id: str = "context-pack-task") -> dict:
    return {
        "id": stage_id,
        "title": "Context Pack Stage",
        "objective": "Use the bounded planner context to add a local follow-up task.",
        "tasks": [
            {
                "id": task_id,
                "title": "Context Pack Task",
                "file_scope": ["src/**", "tests/**", "docs/**"],
                "acceptance": [
                    {
                        "name": "context pack task smoke",
                        "command": "python3 -c \"print('context pack task ok')\"",
                        "timeout_seconds": 30,
                    }
                ],
            }
        ],
    }


def context_pack_planner_registry(captured: dict, stage_id: str = "context-pack-stage") -> ExecutorRegistry:
    class ContextPackPlanner:
        metadata = ExecutorMetadata(
            id="context-planner",
            name="Context Pack Planner",
            kind="process",
            adapter="test.context-pack-planner",
            input_mode="prompt",
            capabilities=("local_process", "stdout", "stderr"),
        )

        def display_command(self, invocation):
            return "context-pack-planner <prompt>"

        def execute(self, invocation):
            prompt = invocation.prompt or ""
            captured["prompt"] = prompt
            context_line = next(line for line in prompt.splitlines() if line.startswith("Planner context pack:"))
            context_path = Path(context_line.split(":", 1)[1].strip())
            context = json.loads(context_path.read_text(encoding="utf-8"))
            captured["context_path"] = context_path
            captured["context"] = context

            roadmap_path = invocation.project_root / ".engineering/roadmap.yaml"
            roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
            roadmap.setdefault("continuation", {"enabled": True, "stages": []}).setdefault("stages", []).append(
                context_pack_stage(stage_id=stage_id, task_id=f"{stage_id}-task")
            )
            roadmap_path.write_text(json.dumps(roadmap, indent=2), encoding="utf-8")
            return ExecutorResult(
                status="passed",
                returncode=0,
                started_at="2024-01-01T00:00:00Z",
                finished_at="2024-01-01T00:00:01Z",
                stdout="context planner ok",
                stderr="",
            )

    return ExecutorRegistry((ShellExecutorAdapter(), ContextPackPlanner()))


def test_self_iteration_context_pack_smoke(tmp_path):
    project, roadmap_path = self_iteration_context_pack_project(tmp_path)
    captured: dict = {}

    result = Harness(project, executor_registry=context_pack_planner_registry(captured)).run_self_iteration(
        reason="context-pack-smoke"
    )

    assert result["status"] == "planned"
    assert "Planner context pack:" in captured["prompt"]
    assert captured["context_path"] == project / result["context_pack"]["path"]
    context = captured["context"]
    assert context["kind"] == "engineering-harness.self-iteration-context-pack"
    assert context["manifests"]["recent_task_manifests"]
    assert context["reports"]["task_reports"]["files"]
    assert context["reports"]["drive_reports"]["files"]
    assert any(item["path"] == "tests/test_app.py" for item in context["test_inventory"]["files"])
    assert any(item["path"] == "src/app.py" for item in context["source_inventory"]["files"])
    assert context["docs"]["blueprint"]["path"] == "docs/blueprint.md"
    assert "OPENAI_API_KEY=[REDACTED]" in context["docs"]["blueprint"]["excerpt"]
    assert "sk-context-secret" not in json.dumps(context)
    assert context["git"]["is_repository"] is True
    assert context["git"]["recent_commits"]
    assert context["git"]["status"]["returncode"] == 0
    assert json.loads(roadmap_path.read_text(encoding="utf-8"))["continuation"]["stages"][0]["id"] == (
        "context-pack-stage"
    )


def test_self_iteration_context_pack_snapshot_report_and_result(tmp_path):
    project, _roadmap_path = self_iteration_context_pack_project(tmp_path)
    captured: dict = {}

    result = Harness(project, executor_registry=context_pack_planner_registry(captured, "context-pack-contract")).run_self_iteration(
        reason="context-pack-contract"
    )

    context_path = project / result["context_pack"]["path"]
    snapshot_path = project / result["snapshot"]
    report_path = project / result["report"]
    context = json.loads(context_path.read_text(encoding="utf-8"))
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    report = report_path.read_text(encoding="utf-8")

    assert snapshot["context_pack"] == result["context_pack"]
    assert snapshot["context_pack"]["summary"] == context["summary"]
    assert result["context_pack"]["summary"]["recent_manifest_count"] >= 1
    assert "## Planner Context Pack" in report
    assert result["context_pack"]["path"] in report
    assert '"manifest_count"' in report
    assert context["summary"]["source_file_count"] >= 1


def test_self_iteration_output_guard_accepts_valid_appended_stage(tmp_path):
    project, roadmap_path = self_iteration_guard_project(tmp_path)
    write_self_iteration_guard_planner(project, [valid_self_iteration_stage()])

    result = Harness(project).run_self_iteration(reason="guard-test")

    assert result["status"] == "planned"
    assert result["validation"]["status"] == "passed"
    assert result["validation"]["new_stage_ids"] == ["guard-stage"]
    updated = json.loads(roadmap_path.read_text(encoding="utf-8"))
    assert [stage["id"] for stage in updated["continuation"]["stages"]] == ["guard-stage"]
    report = Path(project, result["report"]).read_text(encoding="utf-8")
    assert "## Output Validation" in report
    assert "- Status: `passed`" in report


def test_self_iteration_output_guard_rejects_too_many_new_stages_and_restores(tmp_path):
    project, roadmap_path = self_iteration_guard_project(tmp_path, max_stages_per_iteration=1)
    before = json.loads(roadmap_path.read_text(encoding="utf-8"))
    write_self_iteration_guard_planner(
        project,
        [
            valid_self_iteration_stage("guard-stage-a", "guard-task-a"),
            valid_self_iteration_stage("guard-stage-b", "guard-task-b"),
        ],
    )

    result = Harness(project).run_self_iteration(reason="guard-test")

    assert result["status"] == "rejected"
    assert result["validation"]["status"] == "failed"
    assert any("expected exactly 1 new continuation stage(s), found 2" in error for error in result["validation"]["errors"])
    assert json.loads(roadmap_path.read_text(encoding="utf-8")) == before
    assert "found 2" in Path(project, result["report"]).read_text(encoding="utf-8")


def test_self_iteration_output_guard_rejects_malformed_task_gates_and_restores(tmp_path):
    project, roadmap_path = self_iteration_guard_project(tmp_path)
    before = json.loads(roadmap_path.read_text(encoding="utf-8"))
    stage = valid_self_iteration_stage()
    task = stage["tasks"][0]
    task.pop("file_scope")
    task["acceptance"] = [{"name": "missing command"}]
    task["repair"] = []
    write_self_iteration_guard_planner(project, [stage])

    result = Harness(project).run_self_iteration(reason="guard-test")

    assert result["status"] == "rejected"
    errors = "\n".join(result["validation"]["errors"])
    assert "file_scope" in errors
    assert "acceptance command" in errors
    assert "codex repair" in errors
    assert json.loads(roadmap_path.read_text(encoding="utf-8")) == before


def test_self_iteration_output_guard_rejects_unsafe_requirements_and_restores(tmp_path):
    project, roadmap_path = self_iteration_guard_project(tmp_path)
    before = json.loads(roadmap_path.read_text(encoding="utf-8"))
    stage = valid_self_iteration_stage()
    stage["tasks"][0]["acceptance"][0]["command"] = "python3 -c \"print('live')\" --live"
    stage["tasks"][0]["implementation"][0]["prompt"] = "Deploy to production with a paid service."
    write_self_iteration_guard_planner(project, [stage])

    result = Harness(project).run_self_iteration(reason="guard-test")

    assert result["status"] == "rejected"
    errors = "\n".join(result["validation"]["errors"])
    assert "unsafe command" in errors
    assert "production deployment" in errors
    assert "paid service" in errors
    assert json.loads(roadmap_path.read_text(encoding="utf-8")) == before


def test_self_iteration_output_guard_allows_negated_safety_requirements(tmp_path):
    project, roadmap_path = self_iteration_guard_project(tmp_path)
    stage = valid_self_iteration_stage()
    stage["tasks"][0]["implementation"][0]["prompt"] = (
        "Implement a local-only guard task free of external services, private keys, "
        "production deployments, mainnet writes, paid services, or live trading."
    )
    write_self_iteration_guard_planner(project, [stage])

    result = Harness(project).run_self_iteration(reason="guard-test")

    assert result["status"] == "planned"
    assert result["validation"]["status"] == "passed"
    updated = json.loads(roadmap_path.read_text(encoding="utf-8"))
    assert [stage["id"] for stage in updated["continuation"]["stages"]] == ["guard-stage"]


def test_self_iteration_output_guard_rejects_milestone_mutation_and_restores(tmp_path):
    project, roadmap_path = self_iteration_guard_project(tmp_path)
    before = json.loads(roadmap_path.read_text(encoding="utf-8"))
    write_self_iteration_guard_planner(
        project,
        [valid_self_iteration_stage()],
        mutation="roadmap['milestones'][0]['tasks'][0]['status'] = 'done'",
    )

    result = Harness(project).run_self_iteration(reason="guard-test")

    assert result["status"] == "rejected"
    assert any("mutated existing milestones" in error for error in result["validation"]["errors"])
    assert json.loads(roadmap_path.read_text(encoding="utf-8")) == before


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
