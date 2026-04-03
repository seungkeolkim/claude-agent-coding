"""
notification.py 단위 테스트.

emit / get / mark_read / unread_count / format 함수 검증.
"""

import json
import os
import time

from notification import (
    emit_notification,
    format_notification_cli,
    format_notification_plain,
    get_notifications,
    get_unread_count,
    mark_notifications_read,
)


class TestEmitNotification:
    """emit_notification 함수 테스트."""

    def test_emit_creates_file(self, test_project):
        """notifications.json이 없을 때 새로 생성된다."""
        project_dir = test_project["dir"]
        result = emit_notification(
            project_dir, "task_completed", "00001",
            "task 완료", details={"pr_url": "http://example.com"},
        )

        noti_path = os.path.join(project_dir, "notifications.json")
        assert os.path.exists(noti_path)

        with open(noti_path) as f:
            data = json.load(f)
        assert len(data) == 1
        assert data[0]["event_type"] == "task_completed"
        assert data[0]["task_id"] == "00001"
        assert data[0]["read"] is False

    def test_emit_appends(self, test_project):
        """기존 알림에 추가된다."""
        project_dir = test_project["dir"]
        emit_notification(project_dir, "task_completed", "00001", "첫 번째")
        emit_notification(project_dir, "task_failed", "00002", "두 번째")

        noti_path = os.path.join(project_dir, "notifications.json")
        with open(noti_path) as f:
            data = json.load(f)
        assert len(data) == 2
        assert data[0]["event_type"] == "task_completed"
        assert data[1]["event_type"] == "task_failed"

    def test_emit_returns_notification(self, test_project):
        """반환값이 올바른 구조를 갖는다."""
        result = emit_notification(
            test_project["dir"], "escalation", "00003",
            "에스컬레이션 발생",
        )
        assert result["event_type"] == "escalation"
        assert result["task_id"] == "00003"
        assert "created_at" in result
        assert result["read"] is False

    def test_emit_all_event_types(self, test_project):
        """6가지 이벤트 타입 모두 정상 기록된다."""
        project_dir = test_project["dir"]
        event_types = [
            "task_completed", "task_failed", "pr_created",
            "plan_review_requested", "replan_review_requested", "escalation",
        ]
        for i, event_type in enumerate(event_types):
            emit_notification(project_dir, event_type, f"0000{i}", f"msg-{i}")

        notifications = get_notifications(project_dir)
        assert len(notifications) == 6
        stored_types = {n["event_type"] for n in notifications}
        assert stored_types == set(event_types)


class TestGetNotifications:
    """get_notifications 함수 테스트."""

    def test_empty_project(self, test_project):
        """알림이 없을 때 빈 리스트를 반환한다."""
        result = get_notifications(test_project["dir"])
        assert result == []

    def test_unread_only(self, test_project):
        """unread_only 필터가 동작한다."""
        project_dir = test_project["dir"]
        emit_notification(project_dir, "task_completed", "00001", "완료")
        emit_notification(project_dir, "task_failed", "00002", "실패")

        # 하나를 읽음 처리
        mark_notifications_read(project_dir)

        # 새 알림 추가
        emit_notification(project_dir, "escalation", "00003", "에스컬레이션")

        unread = get_notifications(project_dir, unread_only=True)
        assert len(unread) == 1
        assert unread[0]["event_type"] == "escalation"

    def test_limit(self, test_project):
        """limit 파라미터가 동작한다."""
        project_dir = test_project["dir"]
        for i in range(5):
            emit_notification(project_dir, "task_completed", f"{i:05d}", f"msg-{i}")

        result = get_notifications(project_dir, limit=3)
        assert len(result) == 3

    def test_since_filter(self, test_project):
        """since 파라미터로 시간 기반 필터링이 동작한다."""
        project_dir = test_project["dir"]
        emit_notification(project_dir, "task_completed", "00001", "오래된 알림")
        time.sleep(0.05)

        # since 시각 기록
        from datetime import datetime, timezone
        since = datetime.now(timezone.utc).isoformat()
        time.sleep(0.05)

        emit_notification(project_dir, "task_failed", "00002", "새 알림")

        result = get_notifications(project_dir, since=since)
        assert len(result) == 1
        assert result[0]["task_id"] == "00002"


class TestMarkNotificationsRead:
    """mark_notifications_read 함수 테스트."""

    def test_mark_all_read(self, test_project):
        """전체 읽음 처리가 동작한다."""
        project_dir = test_project["dir"]
        emit_notification(project_dir, "task_completed", "00001", "1")
        emit_notification(project_dir, "task_failed", "00002", "2")

        assert get_unread_count(project_dir) == 2

        mark_notifications_read(project_dir)

        assert get_unread_count(project_dir) == 0

    def test_mark_read_up_to_timestamp(self, test_project):
        """up_to_timestamp까지만 읽음 처리된다."""
        project_dir = test_project["dir"]
        emit_notification(project_dir, "task_completed", "00001", "1")
        time.sleep(0.05)

        from datetime import datetime, timezone
        cutoff = datetime.now(timezone.utc).isoformat()
        time.sleep(0.05)

        emit_notification(project_dir, "task_failed", "00002", "2")

        mark_notifications_read(project_dir, up_to_timestamp=cutoff)

        assert get_unread_count(project_dir) == 1


class TestFormatNotification:
    """포맷 함수 테스트."""

    def test_format_plain(self, test_project):
        """plain 포맷이 색상 코드 없이 출력된다."""
        noti = emit_notification(
            test_project["dir"], "task_completed", "00042", "완료됨",
        )
        text = format_notification_plain(noti, project_name="my-project")
        assert "[완료]" in text
        assert "task 00042" in text
        assert "완료됨" in text
        assert "my-project" in text
        # ANSI 색상 코드 없음
        assert "\033[" not in text

    def test_format_cli_has_color(self, test_project):
        """CLI 포맷에 색상 코드가 포함된다."""
        noti = emit_notification(
            test_project["dir"], "task_failed", "00001", "실패",
        )
        text = format_notification_cli(noti)
        assert "\033[" in text  # ANSI 코드 존재
        assert "task 00001" in text

    def test_format_unknown_event_type(self, test_project):
        """알 수 없는 이벤트 타입도 오류 없이 포맷된다."""
        noti = {
            "event_type": "unknown_type",
            "task_id": "00099",
            "message": "미지의 이벤트",
            "created_at": "2026-04-03T12:00:00+00:00",
            "read": False,
        }
        text = format_notification_plain(noti)
        assert "task 00099" in text
