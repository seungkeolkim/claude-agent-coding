"""
HubAPI 통합 테스트.

submit / list / approve / reject / cancel / pending / feedback / notifications 검증.
실제 파일시스템 기반으로 동작을 확인한다.
"""

import json
import os

import pytest

from hub_api.core import HubAPI
from notification import emit_notification, get_unread_count


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
