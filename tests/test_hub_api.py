"""
HubAPI 통합 테스트.

submit / list / approve / reject / cancel / pending / feedback /
notifications / create_project 검증.
실제 파일시스템 기반으로 동작을 확인한다.
"""

import json
import os
import shutil
from datetime import datetime

import pytest
import yaml

from hub_api.core import HubAPI
from hub_api.protocol import dispatch, Request, ErrorCode
from notification import emit_notification, get_unread_count


def _test_project_name(label: str) -> str:
    """테스트용 프로젝트 이름을 생성한다.

    형식: test-{label}-YYMMDD-HHmmss
    - 'test-' 접두사로 테스트임을 명시
    - label로 어떤 테스트인지 식별
    - 타임스탬프로 생성 시점 확인

    Args:
        label: 테스트 목적을 나타내는 짧은 식별자 (영문소문자, 하이픈)
    """
    timestamp = datetime.now().strftime("%y%m%d-%H%M%S")
    return f"test-{label}-{timestamp}"


class TestSubmit:
    """task 제출."""

    def test_submit_creates_task_and_ready(self, test_project, agent_hub_root):
        """submit이 task JSON + .ready 파일을 생성한다."""
        api = HubAPI(agent_hub_root)
        result = api.submit(
            test_project["name"],
            title="새 기능 구현",
            description="단위변환기에 무게 단위를 추가한다.",
        )

        assert result.task_id == "00001"
        assert result.project == test_project["name"]
        assert os.path.exists(result.file_path)

        # .ready sentinel 존재 확인
        ready_path = os.path.join(test_project["tasks_dir"], "00001.ready")
        assert os.path.exists(ready_path)

        # task JSON 내용 확인
        with open(result.file_path) as f:
            task = json.load(f)
        assert task["status"] == "submitted"
        assert task["title"] == "새 기능 구현"
        assert task["counters"]["total_agent_invocations"] == 0

    def test_submit_auto_increments_id(self, test_project, agent_hub_root):
        """연속 submit 시 task_id가 자동 증가한다."""
        api = HubAPI(agent_hub_root)
        r1 = api.submit(test_project["name"], "task 1", "desc 1")
        r2 = api.submit(test_project["name"], "task 2", "desc 2")
        r3 = api.submit(test_project["name"], "task 3", "desc 3")

        assert r1.task_id == "00001"
        assert r2.task_id == "00002"
        assert r3.task_id == "00003"

    def test_submit_with_config_override(self, test_project, agent_hub_root):
        """config_override가 task JSON에 저장된다."""
        api = HubAPI(agent_hub_root)
        override = {"limits": {"max_retry_per_subtask": 10}}
        result = api.submit(
            test_project["name"], "task override", "desc",
            config_override=override,
        )

        with open(result.file_path) as f:
            task = json.load(f)
        assert task["config_override"]["limits"]["max_retry_per_subtask"] == 10


class TestListTasks:
    """task 목록 조회."""

    def test_list_all(self, test_project, agent_hub_root):
        """전체 task 목록 조회."""
        api = HubAPI(agent_hub_root)
        api.submit(test_project["name"], "task A", "desc")
        api.submit(test_project["name"], "task B", "desc")

        tasks = api.list_tasks(project=test_project["name"])
        assert len(tasks) == 2
        assert tasks[0].task_id == "00001"
        assert tasks[1].task_id == "00002"

    def test_list_filter_by_status(self, test_project, agent_hub_root):
        """status 필터링."""
        api = HubAPI(agent_hub_root)
        r1 = api.submit(test_project["name"], "task A", "desc")

        # task A의 status를 in_progress로 변경
        with open(r1.file_path) as f:
            task = json.load(f)
        task["status"] = "in_progress"
        with open(r1.file_path, "w") as f:
            json.dump(task, f, ensure_ascii=False, indent=2)

        api.submit(test_project["name"], "task B", "desc")  # submitted 상태

        submitted = api.list_tasks(project=test_project["name"], status="submitted")
        assert len(submitted) == 1
        assert submitted[0].title == "task B"

    def test_list_empty(self, test_project, agent_hub_root):
        """task가 없을 때 빈 리스트."""
        api = HubAPI(agent_hub_root)
        tasks = api.list_tasks(project=test_project["name"])
        assert tasks == []


class TestApproveReject:
    """approve / reject."""

    def _make_waiting_task(self, api, project_name):
        """waiting_for_human 상태의 task를 만든다."""
        result = api.submit(project_name, "승인 대기 task", "desc")
        with open(result.file_path) as f:
            task = json.load(f)
        task["status"] = "waiting_for_human"
        task["human_interaction"] = {
            "type": "plan_review",
            "message": "plan을 확인해 주세요",
            "options": ["approve", "modify"],
            "requested_at": task["submitted_at"],
            "payload_path": None,
            "response": None,
        }
        with open(result.file_path, "w") as f:
            json.dump(task, f, ensure_ascii=False, indent=2)
        return result

    def test_approve(self, test_project, agent_hub_root):
        """approve가 status를 planned로 변경한다."""
        api = HubAPI(agent_hub_root)
        result = self._make_waiting_task(api, test_project["name"])

        success = api.approve(test_project["name"], "00001", message="LGTM")
        assert success is True

        with open(result.file_path) as f:
            task = json.load(f)
        assert task["status"] == "planned"
        assert task["human_interaction"]["response"]["action"] == "approve"

    def test_reject(self, test_project, agent_hub_root):
        """reject가 status를 needs_replan으로 변경한다."""
        api = HubAPI(agent_hub_root)
        result = self._make_waiting_task(api, test_project["name"])

        success = api.reject(
            test_project["name"], "00001", message="API 설계를 변경해 주세요",
        )
        assert success is True

        with open(result.file_path) as f:
            task = json.load(f)
        assert task["status"] == "needs_replan"
        assert task["human_interaction"]["response"]["action"] == "modify"
        assert "API 설계" in task["human_interaction"]["response"]["message"]

    def test_approve_non_waiting_task(self, test_project, agent_hub_root):
        """waiting_for_human이 아닌 task에 approve하면 False."""
        api = HubAPI(agent_hub_root)
        api.submit(test_project["name"], "일반 task", "desc")

        success = api.approve(test_project["name"], "00001")
        assert success is False


class TestPending:
    """pending (human interaction 조회)."""

    def test_pending_empty(self, test_project, agent_hub_root):
        """대기 중인 항목이 없을 때 빈 리스트."""
        api = HubAPI(agent_hub_root)
        api.submit(test_project["name"], "일반 task", "desc")

        pending = api.pending(project=test_project["name"])
        assert pending == []

    def test_pending_with_waiting_task(self, test_project, agent_hub_root):
        """waiting_for_human task가 있으면 조회된다."""
        api = HubAPI(agent_hub_root)
        result = api.submit(test_project["name"], "승인 대기", "desc")

        with open(result.file_path) as f:
            task = json.load(f)
        task["status"] = "waiting_for_human"
        task["human_interaction"] = {
            "type": "plan_review",
            "message": "확인 요청",
            "options": ["approve", "modify"],
            "requested_at": task["submitted_at"],
            "response": None,
        }
        with open(result.file_path, "w") as f:
            json.dump(task, f, ensure_ascii=False, indent=2)

        pending = api.pending(project=test_project["name"])
        assert len(pending) == 1
        assert pending[0].task_id == "00001"
        assert pending[0].interaction_type == "plan_review"


class TestCancel:
    """task 취소."""

    def test_cancel_submitted_task(self, test_project, agent_hub_root):
        """submitted 상태 task를 직접 취소한다."""
        api = HubAPI(agent_hub_root)
        result = api.submit(test_project["name"], "취소될 task", "desc")

        success = api.cancel(test_project["name"], "00001")
        assert success is True

        with open(result.file_path) as f:
            task = json.load(f)
        assert task["status"] == "cancelled"

        # .ready sentinel도 삭제됨
        ready_path = os.path.join(test_project["tasks_dir"], "00001.ready")
        assert not os.path.exists(ready_path)

    def test_cancel_completed_task(self, test_project, agent_hub_root):
        """이미 완료된 task는 취소 불가."""
        api = HubAPI(agent_hub_root)
        result = api.submit(test_project["name"], "완료된 task", "desc")

        with open(result.file_path) as f:
            task = json.load(f)
        task["status"] = "completed"
        with open(result.file_path, "w") as f:
            json.dump(task, f, ensure_ascii=False, indent=2)

        success = api.cancel(test_project["name"], "00001")
        assert success is False

    def test_cancel_in_progress_creates_command(self, test_project, agent_hub_root):
        """in_progress task는 .command 파일을 생성한다."""
        api = HubAPI(agent_hub_root)
        result = api.submit(test_project["name"], "실행 중 task", "desc")

        with open(result.file_path) as f:
            task = json.load(f)
        task["status"] = "in_progress"
        with open(result.file_path, "w") as f:
            json.dump(task, f, ensure_ascii=False, indent=2)

        success = api.cancel(test_project["name"], "00001")
        assert success is True

        # .command 파일 생성 확인
        cmd_path = os.path.join(test_project["commands_dir"], "cancel-00001.command")
        assert os.path.exists(cmd_path)


class TestFeedback:
    """mid-task feedback."""

    def test_feedback_appended(self, test_project, agent_hub_root):
        """피드백이 mid_task_feedback에 추가된다."""
        api = HubAPI(agent_hub_root)
        result = api.submit(test_project["name"], "피드백 대상 task", "desc")

        api.feedback(test_project["name"], "00001", "에러 처리를 추가해 주세요")
        api.feedback(test_project["name"], "00001", "타입 힌트도 부탁합니다")

        with open(result.file_path) as f:
            task = json.load(f)
        assert len(task["mid_task_feedback"]) == 2
        assert "에러 처리" in task["mid_task_feedback"][0]["message"]
        assert "타입 힌트" in task["mid_task_feedback"][1]["message"]


class TestNotifications:
    """HubAPI.notifications() 통합 테스트."""

    def test_notifications_query(self, test_project, agent_hub_root):
        """알림이 올바르게 조회된다."""
        project_dir = test_project["dir"]
        emit_notification(project_dir, "task_completed", "00001", "task 완료")
        emit_notification(project_dir, "task_failed", "00002", "task 실패")

        api = HubAPI(agent_hub_root)
        notis = api.notifications(project=test_project["name"])

        assert len(notis) == 2
        # 최신 순 정렬
        assert notis[0]["event_type"] == "task_failed"
        assert notis[1]["event_type"] == "task_completed"

    def test_notifications_unread_only(self, test_project, agent_hub_root):
        """unread_only 필터."""
        project_dir = test_project["dir"]
        emit_notification(project_dir, "task_completed", "00001", "완료")
        from notification import mark_notifications_read
        mark_notifications_read(project_dir)
        emit_notification(project_dir, "task_failed", "00002", "실패")

        api = HubAPI(agent_hub_root)
        notis = api.notifications(
            project=test_project["name"], unread_only=True,
        )
        assert len(notis) == 1
        assert notis[0]["task_id"] == "00002"


# ═══════════════════════════════════════════════════════════
# 프로젝트 생성
# ═══════════════════════════════════════════════════════════


class TestCreateProject:
    """프로젝트 생성 — HubAPI.create_project()."""

    def test_create_project_basic(self, agent_hub_root, tmp_path):
        """기본 프로젝트 생성: 디렉토리, project.yaml, project_state.json이 생성된다."""
        api = HubAPI(agent_hub_root)
        project_name = _test_project_name("basic")
        codebase = str(tmp_path / "my-codebase")

        result = api.create_project(
            name=project_name,
            description="기본 생성 테스트",
            codebase_path=codebase,
        )
        try:
            assert result.project_name == project_name
            assert os.path.isdir(result.project_directory)
            assert os.path.isfile(result.project_yaml_path)
            assert os.path.isfile(result.project_state_path)

            # runtime 디렉토리 확인
            for subdir in ["tasks", "handoffs", "commands", "logs", "archive", "attachments"]:
                assert os.path.isdir(os.path.join(result.project_directory, subdir))

            # codebase 디렉토리 자동 생성 확인
            assert os.path.isdir(codebase)
        finally:
            shutil.rmtree(result.project_directory, ignore_errors=True)

    def test_create_project_yaml_content(self, agent_hub_root, tmp_path):
        """project.yaml에 입력 값이 올바르게 기록된다."""
        api = HubAPI(agent_hub_root)
        project_name = _test_project_name("yaml")
        codebase = str(tmp_path / "codebase-yaml-test")

        result = api.create_project(
            name=project_name,
            description="YAML 내용 테스트",
            codebase_path=codebase,
        )
        try:
            with open(result.project_yaml_path, encoding="utf-8") as f:
                config = yaml.safe_load(f)

            assert config["project"]["name"] == project_name
            assert config["project"]["description"] == "YAML 내용 테스트"
            assert config["codebase"]["path"] == codebase
        finally:
            shutil.rmtree(result.project_directory, ignore_errors=True)

    def test_create_project_unconfigured_placeholders(self, agent_hub_root, tmp_path):
        """git_settings 미지정 시 __UNCONFIGURED__ 플레이스홀더가 채워진다."""
        api = HubAPI(agent_hub_root)
        project_name = _test_project_name("placeholder")
        codebase = str(tmp_path / "codebase-placeholder")

        result = api.create_project(
            name=project_name,
            description="플레이스홀더 테스트",
            codebase_path=codebase,
        )
        try:
            with open(result.project_yaml_path, encoding="utf-8") as f:
                config = yaml.safe_load(f)

            # git 설정 미지정 → author_name, author_email이 플레이스홀더
            assert config["git"]["author_name"] == "__UNCONFIGURED__"
            assert config["git"]["author_email"] == "__UNCONFIGURED__"
            assert config["git"]["enabled"] is False
        finally:
            shutil.rmtree(result.project_directory, ignore_errors=True)

    def test_create_project_with_git_settings(self, agent_hub_root, tmp_path):
        """git 설정이 project.yaml에 반영된다."""
        api = HubAPI(agent_hub_root)
        project_name = _test_project_name("git")
        codebase = str(tmp_path / "codebase-git")

        git_settings = {
            "enabled": True,
            "remote": "upstream",
            "author_name": "my-bot",
            "author_email": "bot@test.com",
            "auto_merge": True,
            "pr_target_branch": "develop",
        }
        result = api.create_project(
            name=project_name,
            description="git 설정 테스트",
            codebase_path=codebase,
            git_settings=git_settings,
        )
        try:
            with open(result.project_yaml_path, encoding="utf-8") as f:
                config = yaml.safe_load(f)

            assert config["git"]["enabled"] is True
            assert config["git"]["remote"] == "upstream"
            assert config["git"]["author_name"] == "my-bot"
            assert config["git"]["auto_merge"] is True
            assert config["project"]["default_branch"] == "develop"
        finally:
            shutil.rmtree(result.project_directory, ignore_errors=True)

    def test_create_project_state_initialized(self, agent_hub_root, tmp_path):
        """project_state.json이 올바른 초기 상태로 생성된다."""
        api = HubAPI(agent_hub_root)
        project_name = _test_project_name("state")
        codebase = str(tmp_path / "codebase-state")

        result = api.create_project(
            name=project_name,
            description="상태 초기화 테스트",
            codebase_path=codebase,
        )
        try:
            with open(result.project_state_path, encoding="utf-8") as f:
                state = json.load(f)

            assert state["project_name"] == project_name
            assert state["status"] == "idle"
            assert state["current_task_id"] is None
            assert state["overrides"] == {}
        finally:
            shutil.rmtree(result.project_directory, ignore_errors=True)

    def test_create_project_invalid_name_uppercase(self, agent_hub_root, tmp_path):
        """대문자가 포함된 이름이면 ValueError."""
        api = HubAPI(agent_hub_root)
        with pytest.raises(ValueError, match="잘못된 프로젝트 이름"):
            api.create_project("INVALID", "desc", str(tmp_path))

    def test_create_project_invalid_name_special_chars(self, agent_hub_root, tmp_path):
        """특수문자가 포함된 이름이면 ValueError."""
        api = HubAPI(agent_hub_root)
        with pytest.raises(ValueError, match="잘못된 프로젝트 이름"):
            api.create_project("my_project!", "desc", str(tmp_path))

    def test_create_project_invalid_name_starts_with_hyphen(self, agent_hub_root, tmp_path):
        """하이픈으로 시작하는 이름이면 ValueError."""
        api = HubAPI(agent_hub_root)
        with pytest.raises(ValueError, match="잘못된 프로젝트 이름"):
            api.create_project("-bad-name", "desc", str(tmp_path))

    def test_create_project_duplicate(self, agent_hub_root, tmp_path):
        """이미 존재하는 프로젝트명이면 FileExistsError."""
        api = HubAPI(agent_hub_root)
        project_name = _test_project_name("duplicate")
        codebase = str(tmp_path / "codebase-dup")

        # 먼저 프로젝트 생성
        result = api.create_project(project_name, "first", codebase)
        try:
            # 같은 이름으로 재생성 시도
            with pytest.raises(FileExistsError, match="이미 존재"):
                api.create_project(project_name, "second", str(tmp_path / "other"))
        finally:
            shutil.rmtree(result.project_directory, ignore_errors=True)

    def test_create_project_relative_path(self, agent_hub_root):
        """상대경로이면 ValueError."""
        api = HubAPI(agent_hub_root)
        project_name = _test_project_name("relative-path")
        with pytest.raises(ValueError, match="절대경로"):
            api.create_project(project_name, "desc", "relative/path")

    def test_create_project_codebase_not_directory(self, agent_hub_root, tmp_path):
        """codebase_path가 파일이면 ValueError."""
        api = HubAPI(agent_hub_root)
        project_name = _test_project_name("not-directory")
        # 파일을 먼저 생성
        file_path = tmp_path / "not-a-dir"
        file_path.write_text("file content")

        with pytest.raises(ValueError, match="디렉토리가 아닙니다"):
            api.create_project(project_name, "desc", str(file_path))

    def test_create_project_creates_nested_codebase(self, agent_hub_root, tmp_path):
        """존재하지 않는 중첩 경로도 자동 생성한다."""
        api = HubAPI(agent_hub_root)
        project_name = _test_project_name("nested")
        nested_path = str(tmp_path / "a" / "b" / "c" / "codebase")
        assert not os.path.exists(nested_path)

        result = api.create_project(
            name=project_name,
            description="중첩 경로 테스트",
            codebase_path=nested_path,
        )
        try:
            assert os.path.isdir(nested_path)
        finally:
            shutil.rmtree(result.project_directory, ignore_errors=True)

    def test_create_project_existing_codebase(self, agent_hub_root, tmp_path):
        """이미 존재하는 codebase 디렉토리도 정상 처리한다."""
        api = HubAPI(agent_hub_root)
        project_name = _test_project_name("existing-codebase")
        codebase = str(tmp_path / "existing-codebase")
        os.makedirs(codebase)

        result = api.create_project(
            name=project_name,
            description="기존 codebase 테스트",
            codebase_path=codebase,
        )
        try:
            assert os.path.isdir(result.project_directory)
        finally:
            shutil.rmtree(result.project_directory, ignore_errors=True)

    def test_create_project_single_char_name(self, agent_hub_root, tmp_path):
        """단일 문자 프로젝트 이름도 유효하다."""
        api = HubAPI(agent_hub_root)
        codebase = str(tmp_path / "codebase-single")

        result = api.create_project(
            name="a",
            description="단일 문자 이름 테스트",
            codebase_path=codebase,
        )
        try:
            assert result.project_name == "a"
        finally:
            shutil.rmtree(result.project_directory, ignore_errors=True)


class TestCreateProjectProtocol:
    """프로젝트 생성 — protocol dispatch 경유."""

    def test_dispatch_create_project(self, agent_hub_root, tmp_path):
        """protocol dispatch로 프로젝트 생성 성공."""
        api = HubAPI(agent_hub_root)
        project_name = _test_project_name("protocol")
        request = Request(
            action="create_project",
            params={
                "name": project_name,
                "description": "프로토콜 생성 테스트",
                "codebase_path": str(tmp_path / "proto-codebase"),
            },
            source="test",
        )
        response = dispatch(api, request)
        try:
            assert response.success is True
            assert "생성 완료" in response.message
        finally:
            project_dir = os.path.join(agent_hub_root, "projects", project_name)
            shutil.rmtree(project_dir, ignore_errors=True)

    def test_dispatch_create_project_missing_name(self, agent_hub_root):
        """name 파라미터 누락 시 MISSING_PARAM 에러."""
        api = HubAPI(agent_hub_root)
        request = Request(
            action="create_project",
            params={"description": "desc", "codebase_path": "/tmp/x"},
        )
        response = dispatch(api, request)
        assert response.success is False
        assert response.error["code"] == ErrorCode.MISSING_PARAM

    def test_dispatch_create_project_missing_codebase(self, agent_hub_root):
        """codebase_path 파라미터 누락 시 MISSING_PARAM 에러."""
        api = HubAPI(agent_hub_root)
        request = Request(
            action="create_project",
            params={"name": "foo", "description": "desc"},
        )
        response = dispatch(api, request)
        assert response.success is False
        assert response.error["code"] == ErrorCode.MISSING_PARAM

    def test_dispatch_create_project_invalid_name(self, agent_hub_root, tmp_path):
        """잘못된 이름이면 INVALID_PARAM 에러."""
        api = HubAPI(agent_hub_root)
        request = Request(
            action="create_project",
            params={
                "name": "INVALID",
                "description": "desc",
                "codebase_path": str(tmp_path),
            },
        )
        response = dispatch(api, request)
        assert response.success is False
        assert response.error["code"] == ErrorCode.INVALID_PARAM

    def test_dispatch_create_project_duplicate(self, agent_hub_root, tmp_path):
        """이미 존재하는 프로젝트면 PROJECT_ALREADY_EXISTS 에러."""
        api = HubAPI(agent_hub_root)
        project_name = _test_project_name("protocol-duplicate")
        # 먼저 프로젝트 생성
        first_request = Request(
            action="create_project",
            params={
                "name": project_name,
                "description": "first",
                "codebase_path": str(tmp_path / "first"),
            },
        )
        dispatch(api, first_request)
        try:
            # 같은 이름으로 재생성
            request = Request(
                action="create_project",
                params={
                    "name": project_name,
                    "description": "second",
                    "codebase_path": str(tmp_path / "second"),
                },
            )
            response = dispatch(api, request)
            assert response.success is False
            assert response.error["code"] == ErrorCode.PROJECT_ALREADY_EXISTS
        finally:
            project_dir = os.path.join(agent_hub_root, "projects", project_name)
            shutil.rmtree(project_dir, ignore_errors=True)

    def test_dispatch_create_project_with_git(self, agent_hub_root, tmp_path):
        """git_settings 포함 dispatch 성공."""
        api = HubAPI(agent_hub_root)
        project_name = _test_project_name("protocol-git")
        request = Request(
            action="create_project",
            params={
                "name": project_name,
                "description": "git 포함 프로토콜 테스트",
                "codebase_path": str(tmp_path / "git-codebase"),
                "git_settings": {
                    "enabled": True,
                    "remote": "origin",
                    "author_name": "proto-bot",
                    "author_email": "proto@test.com",
                },
            },
            source="chatbot",
        )
        response = dispatch(api, request)
        try:
            assert response.success is True
        finally:
            project_dir = os.path.join(agent_hub_root, "projects", project_name)
            shutil.rmtree(project_dir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════
# plan 조회
# ═══════════════════════════════════════════════════════════


class TestGetPlan:
    """task plan 조회."""

    def _create_plan(self, tasks_dir, task_id):
        """테스트용 plan.json을 생성하는 헬퍼."""
        plan_dir = os.path.join(tasks_dir, task_id)
        os.makedirs(plan_dir, exist_ok=True)
        plan_data = {
            "task_id": task_id,
            "plan_version": 1,
            "branch_name": f"feature/{task_id}-test",
            "strategy_note": "테스트 전략",
            "subtasks": [
                {
                    "subtask_id": f"{task_id}-1",
                    "title": "첫 번째 subtask",
                    "primary_responsibility": "기능 구현",
                    "guidance": ["가이드 1"],
                    "depends_on": [],
                },
                {
                    "subtask_id": f"{task_id}-2",
                    "title": "두 번째 subtask",
                    "primary_responsibility": "문서화",
                    "guidance": ["가이드 2"],
                    "depends_on": [f"{task_id}-1"],
                },
            ],
        }
        plan_path = os.path.join(plan_dir, "plan.json")
        with open(plan_path, "w") as f:
            json.dump(plan_data, f, ensure_ascii=False, indent=2)
        return plan_data

    def test_get_plan_returns_plan(self, test_project, agent_hub_root):
        """plan.json이 있으면 내용을 반환한다."""
        api = HubAPI(agent_hub_root)
        api.submit(test_project["name"], "plan 테스트", "설명")
        self._create_plan(test_project["tasks_dir"], "00001")

        plan = api.get_plan(test_project["name"], "00001")
        assert plan is not None
        assert plan["plan_version"] == 1
        assert len(plan["subtasks"]) == 2
        assert plan["subtasks"][0]["title"] == "첫 번째 subtask"

    def test_get_plan_no_plan_returns_none(self, test_project, agent_hub_root):
        """plan.json이 없으면 None을 반환한다."""
        api = HubAPI(agent_hub_root)
        api.submit(test_project["name"], "plan 없는 task", "설명")

        plan = api.get_plan(test_project["name"], "00001")
        assert plan is None

    def test_get_plan_nonexistent_task(self, test_project, agent_hub_root):
        """존재하지 않는 task면 FileNotFoundError."""
        api = HubAPI(agent_hub_root)
        with pytest.raises(FileNotFoundError):
            api.get_plan(test_project["name"], "99999")

    def test_get_plan_via_dispatch(self, test_project, agent_hub_root):
        """protocol dispatch로 plan 조회."""
        api = HubAPI(agent_hub_root)
        api.submit(test_project["name"], "dispatch plan", "설명")
        self._create_plan(test_project["tasks_dir"], "00001")

        request = Request(
            action="get_plan",
            project=test_project["name"],
            params={"task_id": "00001"},
        )
        response = dispatch(api, request)
        assert response.success is True
        assert response.data["plan_version"] == 1
        assert len(response.data["subtasks"]) == 2

    def test_get_plan_via_dispatch_no_plan(self, test_project, agent_hub_root):
        """plan이 없으면 dispatch에서 INVALID_STATE 에러."""
        api = HubAPI(agent_hub_root)
        api.submit(test_project["name"], "no plan task", "설명")

        request = Request(
            action="get_plan",
            project=test_project["name"],
            params={"task_id": "00001"},
        )
        response = dispatch(api, request)
        assert response.success is False
        assert response.error["code"] == ErrorCode.INVALID_STATE


# ═══════════════════════════════════════════════════════════
# resubmit + resume/pause 상태 검증
# ═══════════════════════════════════════════════════════════


class TestResubmit:
    """cancelled/failed task 재제출."""

    def test_resubmit_cancelled_task(self, test_project, agent_hub_root):
        """cancelled task를 재제출하면 새 task가 생성된다."""
        api = HubAPI(agent_hub_root)
        original = api.submit(test_project["name"], "원본 task", "원본 설명")
        api.cancel(test_project["name"], original.task_id)

        new_result = api.resubmit(test_project["name"], original.task_id)

        assert new_result.task_id == "00002"
        assert new_result.project == test_project["name"]

        # 새 task의 내용이 원본과 동일한지 확인
        with open(new_result.file_path) as f:
            new_task = json.load(f)
        assert new_task["title"] == "원본 task"
        assert new_task["description"] == "원본 설명"
        assert new_task["status"] == "submitted"

    def test_resubmit_failed_task(self, test_project, agent_hub_root):
        """failed task를 재제출할 수 있다."""
        api = HubAPI(agent_hub_root)
        original = api.submit(test_project["name"], "실패한 task", "설명")

        # status를 직접 failed로 변경
        with open(original.file_path) as f:
            task = json.load(f)
        task["status"] = "failed"
        with open(original.file_path, "w") as f:
            json.dump(task, f, ensure_ascii=False, indent=2)

        new_result = api.resubmit(test_project["name"], original.task_id)
        assert new_result.task_id == "00002"

    def test_resubmit_in_progress_task_raises(self, test_project, agent_hub_root):
        """in_progress task는 재제출할 수 없다."""
        api = HubAPI(agent_hub_root)
        original = api.submit(test_project["name"], "진행 중 task", "설명")

        with open(original.file_path) as f:
            task = json.load(f)
        task["status"] = "in_progress"
        with open(original.file_path, "w") as f:
            json.dump(task, f, ensure_ascii=False, indent=2)

        with pytest.raises(ValueError, match="재제출할 수 없습니다"):
            api.resubmit(test_project["name"], original.task_id)

    def test_resubmit_completed_task_raises(self, test_project, agent_hub_root):
        """completed task는 재제출할 수 없다."""
        api = HubAPI(agent_hub_root)
        original = api.submit(test_project["name"], "완료된 task", "설명")

        with open(original.file_path) as f:
            task = json.load(f)
        task["status"] = "completed"
        with open(original.file_path, "w") as f:
            json.dump(task, f, ensure_ascii=False, indent=2)

        with pytest.raises(ValueError, match="재제출할 수 없습니다"):
            api.resubmit(test_project["name"], original.task_id)

    def test_resubmit_preserves_config_override(self, test_project, agent_hub_root):
        """원본의 config_override가 새 task에 유지된다."""
        api = HubAPI(agent_hub_root)
        override = {"limits": {"max_retry_per_subtask": 10}}
        original = api.submit(
            test_project["name"], "config 포함 task", "설명",
            config_override=override,
        )
        api.cancel(test_project["name"], original.task_id)

        new_result = api.resubmit(test_project["name"], original.task_id)
        with open(new_result.file_path) as f:
            new_task = json.load(f)
        assert new_task["config_override"]["limits"]["max_retry_per_subtask"] == 10

    def test_resubmit_with_new_config_override(self, test_project, agent_hub_root):
        """resubmit 시 새 config_override를 지정하면 원본 대신 적용된다."""
        api = HubAPI(agent_hub_root)
        original = api.submit(
            test_project["name"], "override 교체", "설명",
            config_override={"limits": {"max_retry_per_subtask": 3}},
        )
        api.cancel(test_project["name"], original.task_id)

        new_override = {"limits": {"max_retry_per_subtask": 20}}
        new_result = api.resubmit(
            test_project["name"], original.task_id,
            config_override=new_override,
        )
        with open(new_result.file_path) as f:
            new_task = json.load(f)
        assert new_task["config_override"]["limits"]["max_retry_per_subtask"] == 20

    def test_resubmit_nonexistent_task(self, test_project, agent_hub_root):
        """존재하지 않는 task ID면 FileNotFoundError."""
        api = HubAPI(agent_hub_root)
        with pytest.raises(FileNotFoundError):
            api.resubmit(test_project["name"], "99999")

    def test_resubmit_via_dispatch(self, test_project, agent_hub_root):
        """protocol dispatch 경유 resubmit."""
        api = HubAPI(agent_hub_root)
        original = api.submit(test_project["name"], "dispatch 재제출", "설명")
        api.cancel(test_project["name"], original.task_id)

        request = Request(
            action="resubmit",
            project=test_project["name"],
            params={"task_id": original.task_id},
            source="test",
        )
        response = dispatch(api, request)
        assert response.success is True
        assert "재제출 완료" in response.message


class TestResumeStateValidation:
    """resume/pause 상태 검증."""

    def test_resume_cancelled_task_raises(self, test_project, agent_hub_root):
        """cancelled task에 resume하면 ValueError."""
        api = HubAPI(agent_hub_root)
        original = api.submit(test_project["name"], "취소 후 resume", "설명")
        api.cancel(test_project["name"], original.task_id)

        with pytest.raises(ValueError, match="cancelled.*resume할 수 없습니다"):
            api.resume(test_project["name"], task_id=original.task_id)

    def test_resume_completed_task_raises(self, test_project, agent_hub_root):
        """completed task에 resume하면 ValueError."""
        api = HubAPI(agent_hub_root)
        original = api.submit(test_project["name"], "완료 후 resume", "설명")

        with open(original.file_path) as f:
            task = json.load(f)
        task["status"] = "completed"
        with open(original.file_path, "w") as f:
            json.dump(task, f, ensure_ascii=False, indent=2)

        with pytest.raises(ValueError, match="completed.*resume할 수 없습니다"):
            api.resume(test_project["name"], task_id=original.task_id)

    def test_pause_cancelled_task_raises(self, test_project, agent_hub_root):
        """cancelled task에 pause하면 ValueError."""
        api = HubAPI(agent_hub_root)
        original = api.submit(test_project["name"], "취소 후 pause", "설명")
        api.cancel(test_project["name"], original.task_id)

        with pytest.raises(ValueError, match="cancelled.*pause할 수 없습니다"):
            api.pause(test_project["name"], task_id=original.task_id)

    def test_resume_without_task_id_always_succeeds(self, test_project, agent_hub_root):
        """task_id 없이 프로젝트 전체 resume은 상태 검증 없이 성공한다."""
        api = HubAPI(agent_hub_root)
        result = api.resume(test_project["name"])
        assert result is True
