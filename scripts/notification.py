"""
알림 시스템 모듈 — Phase 1.4

이벤트 발생 시 프로젝트별 notifications.json에 알림을 기록하고,
TM이 폴링하여 터미널에 출력한다.

알림 이벤트 종류:
    task_completed          — task 완료 (PR URL 포함)
    task_failed             — task 실패 (에러 요약)
    pr_created              — PR 생성됨
    pr_merged               — PR 머지 완료
    pr_merge_failed         — PR 머지 실패 (merge conflict 등 사용자 개입 필요)
    plan_review_requested   — plan 승인 대기
    replan_review_requested — replan 승인 대기
    escalation              — 에스컬레이션 발생

저장 위치: projects/{name}/notifications.json
"""

import json
import os
import tempfile
from datetime import datetime, timezone


# ─── 색상 코드 (터미널용) ───
GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
RED = "\033[0;31m"
CYAN = "\033[0;36m"
MAGENTA = "\033[0;35m"
BOLD = "\033[1m"
NC = "\033[0m"

# 이벤트별 아이콘 및 색상 매핑
EVENT_STYLES = {
    "task_completed": {"color": GREEN, "label": "완료"},
    "task_failed": {"color": RED, "label": "실패"},
    "pr_created": {"color": CYAN, "label": "PR 생성"},
    "pr_merged": {"color": GREEN, "label": "PR 머지"},
    "pr_merge_failed": {"color": RED, "label": "PR 머지 실패"},
    "plan_review_requested": {"color": YELLOW, "label": "승인 요청"},
    "replan_review_requested": {"color": YELLOW, "label": "재계획 승인 요청"},
    "escalation": {"color": RED, "label": "에스컬레이션"},
}


def emit_notification(project_dir, event_type, task_id, message, details=None):
    """
    알림 이벤트를 notifications.json에 추가한다.

    Args:
        project_dir: 프로젝트 디렉토리 경로 (projects/{name})
        event_type: 이벤트 종류 (task_completed, task_failed 등)
        task_id: task ID
        message: 사람이 읽을 수 있는 알림 메시지
        details: 추가 정보 dict (pr_url, error_summary 등)
    """
    notifications_path = os.path.join(project_dir, "notifications.json")

    notification = {
        "event_type": event_type,
        "task_id": task_id,
        "message": message,
        "details": details or {},
        "created_at": datetime.now(timezone.utc).isoformat(),
        "read": False,
    }

    # 기존 알림 로드 (없으면 빈 리스트)
    notifications = _load_notifications(notifications_path)
    notifications.append(notification)

    _save_json_atomic(notifications_path, notifications)

    return notification


def get_notifications(project_dir, since=None, unread_only=False, limit=None):
    """
    프로젝트의 알림 목록을 조회한다.

    Args:
        project_dir: 프로젝트 디렉토리 경로
        since: 이 시각 이후의 알림만 반환 (ISO 8601 문자열)
        unread_only: True이면 read=False인 알림만
        limit: 최대 반환 개수 (최신 순)
    """
    notifications_path = os.path.join(project_dir, "notifications.json")
    notifications = _load_notifications(notifications_path)

    # 필터링
    if since:
        notifications = [n for n in notifications if n.get("created_at", "") > since]

    if unread_only:
        notifications = [n for n in notifications if not n.get("read", False)]

    # 최신 순 정렬 후 limit 적용
    notifications.sort(key=lambda n: n.get("created_at", ""), reverse=True)
    if limit:
        notifications = notifications[:limit]

    return notifications


def mark_notifications_read(project_dir, up_to_timestamp=None):
    """
    알림을 읽음 처리한다.

    Args:
        project_dir: 프로젝트 디렉토리 경로
        up_to_timestamp: 이 시각까지의 알림을 읽음 처리. None이면 전부.
    """
    notifications_path = os.path.join(project_dir, "notifications.json")
    notifications = _load_notifications(notifications_path)

    changed = False
    for notification in notifications:
        if notification.get("read"):
            continue
        if up_to_timestamp and notification.get("created_at", "") > up_to_timestamp:
            continue
        notification["read"] = True
        changed = True

    if changed:
        _save_json_atomic(notifications_path, notifications)


def get_unread_count(project_dir):
    """프로젝트의 안 읽은 알림 개수를 반환한다."""
    notifications_path = os.path.join(project_dir, "notifications.json")
    notifications = _load_notifications(notifications_path)
    return sum(1 for n in notifications if not n.get("read", False))


def format_notification_cli(notification, project_name=None):
    """
    알림을 터미널 출력용 문자열로 포맷한다.

    Args:
        notification: 알림 dict
        project_name: 프로젝트명 (표시용, 없으면 생략)
    Returns:
        색상 코드 포함 문자열
    """
    event_type = notification.get("event_type", "unknown")
    style = EVENT_STYLES.get(event_type, {"color": NC, "label": event_type})
    color = style["color"]
    label = style["label"]

    task_id = notification.get("task_id", "?")
    message = notification.get("message", "")
    created_at = notification.get("created_at", "")

    # 시간 포맷: ISO → HH:MM
    time_str = ""
    if created_at:
        try:
            dt = datetime.fromisoformat(created_at)
            time_str = dt.strftime("%H:%M")
        except (ValueError, TypeError):
            time_str = created_at[:16]

    # 프로젝트명 포함 여부
    proj_prefix = f"[{project_name}] " if project_name else ""

    # 포맷: [HH:MM] [프로젝트] [라벨] task 00042: 메시지
    return (
        f"{BOLD}[{time_str}]{NC} "
        f"{proj_prefix}"
        f"{color}[{label}]{NC} "
        f"task {task_id}: {message}"
    )


def format_notification_plain(notification, project_name=None):
    """
    알림을 로그 파일용 plain text로 포맷한다 (색상 코드 없음).
    """
    event_type = notification.get("event_type", "unknown")
    style = EVENT_STYLES.get(event_type, {"color": "", "label": event_type})
    label = style["label"]

    task_id = notification.get("task_id", "?")
    message = notification.get("message", "")
    created_at = notification.get("created_at", "")

    proj_prefix = f"[{project_name}] " if project_name else ""

    return f"[{created_at}] {proj_prefix}[{label}] task {task_id}: {message}"


# ═══════════════════════════════════════════════════════════
# 내부 헬퍼
# ═══════════════════════════════════════════════════════════


def _load_notifications(path):
    """notifications.json을 읽어 리스트로 반환한다. 파일 없으면 빈 리스트."""
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        return []
    except (json.JSONDecodeError, OSError):
        return []


def _save_json_atomic(path, data):
    """JSON 파일을 atomic하게 저장한다."""
    dir_path = os.path.dirname(path)
    os.makedirs(dir_path, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
