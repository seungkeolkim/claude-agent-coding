"""
E2E 테스트 — run_agent.sh를 실제 subprocess로 실행.

dummy / force_result 모드로 shell script 레이어까지 검증한다.
실제 claude CLI는 호출하지 않으며, run_claude_agent.sh의 dummy/force-result
경로만 통과하여 결과 JSON이 올바르게 생성되는지 확인한다.
"""

import json
import os
import subprocess

import pytest
import yaml

from conftest import _create_task_json, _create_ready_sentinel, _minimal_config


AGENT_HUB_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ensure_config_yaml():
    """config.yaml이 있는지 확인한다. 없으면 테스트용으로 생성."""
    config_path = os.path.join(AGENT_HUB_ROOT, "config.yaml")
    if os.path.exists(config_path):
        return config_path
    # 테스트 환경에 config.yaml이 없으면 스킵
    pytest.skip("config.yaml이 없어 shell E2E 테스트 스킵")


class TestRunAgentShDummy:
    """run_agent.sh --dummy 모드로 각 agent 실행."""

    @pytest.fixture(autouse=True)
    def _setup(self, test_project):
        """테스트 프로젝트에 task와 subtask를 준비한다."""
        _ensure_config_yaml()
        self.project = test_project
        self.task_id = "00001"
        self.task_file = _create_task_json(
            test_project["tasks_dir"], self.task_id,
            title="shell-dummy-test",
            project_name=test_project["name"],
        )

        # subtask 디렉토리 + subtask JSON 생성
        subtask_dir = os.path.join(test_project["tasks_dir"], self.task_id)
        os.makedirs(subtask_dir, exist_ok=True)
        subtask_data = {
            "subtask_id": f"{self.task_id}-1",
            "title": "테스트 subtask",
            "primary_responsibility": "더미 테스트",
            "guidance": "더미",
        }
        subtask_file = os.path.join(subtask_dir, "subtask-01.json")
        with open(subtask_file, "w") as f:
            json.dump(subtask_data, f, ensure_ascii=False, indent=2)

        # logs 디렉토리 생성
        logs_dir = os.path.join(test_project["dir"], "logs", self.task_id)
        os.makedirs(logs_dir, exist_ok=True)

    def _run_agent(self, agent_type, subtask=None, force_result=None):
        """run_agent.sh를 subprocess로 실행하고 결과를 반환한다."""
        cmd = [
            os.path.join(AGENT_HUB_ROOT, "run_agent.sh"),
            "run", agent_type,
            "--project", self.project["name"],
            "--task", self.task_id,
            "--dummy",
        ]
        if subtask:
            cmd.extend(["--subtask", subtask])
        if force_result:
            cmd.extend(["--force-result", force_result])

        result = subprocess.run(
            cmd, cwd=AGENT_HUB_ROOT,
            capture_output=True, text=True, timeout=30,
        )
        return result

    def test_planner_dummy(self):
        """planner dummy가 subtasks를 포함한 JSON을 출력한다."""
        result = self._run_agent("planner")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        # stdout에서 JSON 추출 (더미 결과는 JSON을 stdout에 tee)
        assert "subtasks" in result.stdout

    def test_coder_dummy(self):
        """coder dummy가 정상 종료한다."""
        result = self._run_agent("coder", subtask=f"{self.task_id}-1")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "changes_made" in result.stdout or "code_complete" in result.stdout or "dummy" in result.stdout.lower()

    def test_reviewer_dummy(self):
        """reviewer dummy가 approved를 반환한다."""
        result = self._run_agent("reviewer", subtask=f"{self.task_id}-1")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "approved" in result.stdout.lower() or "action" in result.stdout

    def test_reporter_dummy(self):
        """reporter dummy가 verdict를 포함한 결과를 반환한다."""
        result = self._run_agent("reporter", subtask=f"{self.task_id}-1")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "verdict" in result.stdout or "report" in result.stdout.lower()

    def test_memory_updater_dummy(self):
        """memory_updater dummy가 updated=false 결과 JSON을 반환한다.

        dummy는 실제로 PROJECT_NOTES.md를 수정하지 않고 스키마에 맞는 JSON만 출력한다.
        """
        result = self._run_agent("memory_updater")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "memory_update_complete" in result.stdout
        assert "updated" in result.stdout


class TestRunAgentShForceResult:
    """run_agent.sh --force-result 모드."""

    @pytest.fixture(autouse=True)
    def _setup(self, test_project):
        """테스트 환경 준비."""
        _ensure_config_yaml()
        self.project = test_project
        self.task_id = "00001"
        self.task_file = _create_task_json(
            test_project["tasks_dir"], self.task_id,
            title="shell-force-test",
            project_name=test_project["name"],
        )

        # subtask 준비
        subtask_dir = os.path.join(test_project["tasks_dir"], self.task_id)
        os.makedirs(subtask_dir, exist_ok=True)
        subtask_data = {
            "subtask_id": f"{self.task_id}-1",
            "title": "force-result subtask",
            "primary_responsibility": "force test",
            "guidance": "test",
        }
        with open(os.path.join(subtask_dir, "subtask-01.json"), "w") as f:
            json.dump(subtask_data, f, ensure_ascii=False, indent=2)

        logs_dir = os.path.join(test_project["dir"], "logs", self.task_id)
        os.makedirs(logs_dir, exist_ok=True)

    def _run_force(self, agent_type, force_result, subtask=None):
        """force-result 모드로 agent를 실행한다."""
        cmd = [
            os.path.join(AGENT_HUB_ROOT, "run_agent.sh"),
            "run", agent_type,
            "--project", self.project["name"],
            "--task", self.task_id,
            "--force-result", force_result,
        ]
        if subtask:
            cmd.extend(["--subtask", subtask])
        return subprocess.run(
            cmd, cwd=AGENT_HUB_ROOT,
            capture_output=True, text=True, timeout=30,
        )

    def test_reviewer_reject(self):
        """reviewer:reject가 rejected action을 반환한다."""
        result = self._run_force(
            "reviewer", "reject", subtask=f"{self.task_id}-1",
        )
        assert result.returncode == 0
        assert '"action": "rejected"' in result.stdout

    def test_reviewer_approve(self):
        """reviewer:approve가 approved action을 반환한다."""
        result = self._run_force(
            "reviewer", "approve", subtask=f"{self.task_id}-1",
        )
        assert result.returncode == 0
        assert '"action": "approved"' in result.stdout

    def test_reporter_pass(self):
        """reporter:pass가 verdict=pass를 반환한다."""
        result = self._run_force(
            "reporter", "pass", subtask=f"{self.task_id}-1",
        )
        assert result.returncode == 0
        assert '"verdict": "pass"' in result.stdout

    def test_reporter_fail(self):
        """reporter:fail이 verdict=fail을 반환한다."""
        result = self._run_force(
            "reporter", "fail", subtask=f"{self.task_id}-1",
        )
        assert result.returncode == 0
        assert '"verdict": "fail"' in result.stdout

    def test_reporter_replan(self):
        """reporter:replan이 needs_replan=true를 반환한다."""
        result = self._run_force(
            "reporter", "replan", subtask=f"{self.task_id}-1",
        )
        assert result.returncode == 0
        assert '"needs_replan": true' in result.stdout

    def test_invalid_force_result(self):
        """지원하지 않는 force-result 조합은 exit 1."""
        result = self._run_force(
            "coder", "invalid_option", subtask=f"{self.task_id}-1",
        )
        assert result.returncode != 0

    def test_force_result_creates_log_file(self):
        """force-result 실행 시 로그 파일이 생성된다."""
        result = self._run_force(
            "reviewer", "reject", subtask=f"{self.task_id}-1",
        )
        assert result.returncode == 0

        # 로그 디렉토리에 결과 JSON 파일이 생성되었는지 확인
        logs_dir = os.path.join(self.project["dir"], "logs", self.task_id)
        log_files = [f for f in os.listdir(logs_dir) if f.endswith(".json")]
        assert len(log_files) >= 1, f"로그 파일이 생성되지 않음: {os.listdir(logs_dir)}"

        # 로그 파일 내용이 유효한 JSON인지 확인
        with open(os.path.join(logs_dir, log_files[0])) as f:
            data = json.load(f)
        assert data["action"] == "rejected"


class TestTaskJsonTestScenario:
    """task JSON의 test_scenario 필드로 force_result를 제어하는 기능."""

    @pytest.fixture(autouse=True)
    def _setup(self, test_project):
        _ensure_config_yaml()
        self.project = test_project
        self.task_id = "00001"

        # test_scenario가 포함된 task JSON
        self.task_file = _create_task_json(
            test_project["tasks_dir"], self.task_id,
            title="scenario-test",
            project_name=test_project["name"],
        )

        # test_scenario 필드를 task JSON에 추가
        with open(self.task_file) as f:
            task = json.load(f)
        task["test_scenario"] = {
            "reviewer": {
                "force_result": "reject",
            },
        }
        with open(self.task_file, "w") as f:
            json.dump(task, f, ensure_ascii=False, indent=2)

        # subtask 준비
        subtask_dir = os.path.join(test_project["tasks_dir"], self.task_id)
        os.makedirs(subtask_dir, exist_ok=True)
        subtask_data = {
            "subtask_id": f"{self.task_id}-1",
            "title": "scenario subtask",
            "primary_responsibility": "test",
            "guidance": "test",
        }
        with open(os.path.join(subtask_dir, "subtask-01.json"), "w") as f:
            json.dump(subtask_data, f, ensure_ascii=False, indent=2)

        logs_dir = os.path.join(test_project["dir"], "logs", self.task_id)
        os.makedirs(logs_dir, exist_ok=True)

    def test_scenario_from_task_json(self):
        """task JSON의 test_scenario.reviewer.force_result가 적용된다."""
        cmd = [
            os.path.join(AGENT_HUB_ROOT, "run_agent.sh"),
            "run", "reviewer",
            "--project", self.project["name"],
            "--task", self.task_id,
            "--subtask", f"{self.task_id}-1",
            # --force-result 없이 실행 → task JSON에서 읽어야 함
        ]
        result = subprocess.run(
            cmd, cwd=AGENT_HUB_ROOT,
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0
        assert '"action": "rejected"' in result.stdout

    def test_scenario_at_attempt(self):
        """at_attempt로 특정 retry에서만 force_result가 발동한다."""
        # at_attempt=2로 설정: retry 1회 후(attempt=2)에만 reject
        with open(self.task_file) as f:
            task = json.load(f)
        task["test_scenario"] = {
            "reviewer": {
                "force_result": "reject",
                "at_attempt": 2,
            },
        }
        # attempt=1 (current_subtask_retry=0) → force_result 무시
        task["counters"]["current_subtask_retry"] = 0
        with open(self.task_file, "w") as f:
            json.dump(task, f, ensure_ascii=False, indent=2)

        cmd = [
            os.path.join(AGENT_HUB_ROOT, "run_agent.sh"),
            "run", "reviewer",
            "--project", self.project["name"],
            "--task", self.task_id,
            "--subtask", f"{self.task_id}-1",
            "--dummy",  # force_result가 빈 문자열이면 dummy 결과 사용
        ]
        result = subprocess.run(
            cmd, cwd=AGENT_HUB_ROOT,
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0
        # attempt=1이므로 at_attempt=2와 불일치 → rejected 아닌 dummy 결과
        assert '"action": "rejected"' not in result.stdout
