"""
프로토콜 레이어 — 통일된 Request/Response envelope + dispatch.

CLI는 HubAPI를 직접 호출하지만,
Chatbot/메신저/웹은 이 프로토콜 레이어를 통해 통일된 형식으로 통신한다.

사용 예:
    from hub_api.protocol import dispatch, Request

    request = Request(action="submit", project="my-app",
                      params={"title": "로그인 구현"}, source="chatbot")
    response = dispatch(hub_api, request)
    # response.success, response.data, response.error, response.message
"""

import base64
import os
import tempfile
from dataclasses import dataclass, field, asdict
from typing import Any, Optional


# ═══════════════════════════════════════════════════════════
# 에러 코드 정의
# ═══════════════════════════════════════════════════════════

class ErrorCode:
    """표준 에러 코드. 프론트엔드가 에러 유형별 처리를 할 수 있도록."""
    # 요청 오류
    INVALID_ACTION = "INVALID_ACTION"           # 존재하지 않는 action
    MISSING_PARAM = "MISSING_PARAM"             # 필수 파라미터 누락
    INVALID_PARAM = "INVALID_PARAM"             # 파라미터 값 오류

    # 리소스 오류
    PROJECT_NOT_FOUND = "PROJECT_NOT_FOUND"     # 프로젝트 없음
    TASK_NOT_FOUND = "TASK_NOT_FOUND"           # task 없음

    # 상태 오류
    INVALID_STATE = "INVALID_STATE"             # 현재 상태에서 불가능한 작업

    # 시스템 오류
    INTERNAL_ERROR = "INTERNAL_ERROR"           # 예상치 못한 에러


# ═══════════════════════════════════════════════════════════
# Request / Response envelope
# ═══════════════════════════════════════════════════════════

@dataclass
class Request:
    """통일된 요청 형식."""
    action: str                                 # 필수: submit, list, approve 등
    project: Optional[str] = None               # action에 따라 필수/선택
    params: dict = field(default_factory=dict)   # action별 파라미터
    attachments: list = field(default_factory=list)  # [{filename, data_base64, type, description}]
    source: str = "unknown"                     # cli | chatbot | slack | telegram

    @classmethod
    def from_dict(cls, d: dict) -> "Request":
        """dict에서 Request를 생성한다. 외부 입력 파싱용."""
        return cls(
            action=d.get("action", ""),
            project=d.get("project"),
            params=d.get("params", {}),
            attachments=d.get("attachments", []),
            source=d.get("source", "unknown"),
        )


@dataclass
class Response:
    """통일된 응답 형식."""
    success: bool
    data: Any = None                            # 성공 시 결과 데이터
    error: Optional[dict] = None                # 실패 시 {"code": "...", "message": "..."}
    message: str = ""                           # 사람이 읽을 요약 메시지

    def to_dict(self) -> dict:
        """JSON 직렬화 가능한 dict로 변환한다."""
        result = {
            "success": self.success,
            "data": self.data,
            "error": self.error,
            "message": self.message,
        }
        # data가 dataclass면 dict로 변환
        if hasattr(self.data, "__dataclass_fields__"):
            result["data"] = asdict(self.data)
        elif isinstance(self.data, list):
            result["data"] = [
                asdict(item) if hasattr(item, "__dataclass_fields__") else item
                for item in self.data
            ]
        return result


def _ok(data: Any, message: str) -> Response:
    """성공 응답을 생성한다."""
    return Response(success=True, data=data, message=message)


def _error(code: str, message: str) -> Response:
    """에러 응답을 생성한다."""
    return Response(
        success=False,
        error={"code": code, "message": message},
        message=message,
    )


# ═══════════════════════════════════════════════════════════
# 첨부파일 처리
# ═══════════════════════════════════════════════════════════

def _resolve_attachments(attachments: list, project_dir: str) -> list:
    """
    프로토콜 첨부파일을 HubAPI 형식으로 변환한다.
    - base64 데이터가 있으면 임시 파일로 저장
    - 로컬 경로가 있으면 그대로 전달
    """
    resolved = []
    for att in attachments:
        filename = att.get("filename", "attachment")
        att_type = att.get("type", "reference")
        description = att.get("description", "")

        # base64 데이터 → 임시 파일로 저장
        if att.get("data_base64"):
            data = base64.b64decode(att["data_base64"])
            tmp_dir = os.path.join(project_dir, "attachments", "_tmp")
            os.makedirs(tmp_dir, exist_ok=True)
            tmp_path = os.path.join(tmp_dir, filename)
            with open(tmp_path, "wb") as f:
                f.write(data)
            resolved.append({
                "path": tmp_path,
                "filename": filename,
                "type": att_type,
                "description": description,
            })
        elif att.get("path"):
            # 로컬 경로 그대로
            resolved.append({
                "path": att["path"],
                "filename": filename,
                "type": att_type,
                "description": description,
            })

    return resolved


# ═══════════════════════════════════════════════════════════
# 필수 파라미터 검증 헬퍼
# ═══════════════════════════════════════════════════════════

def _require_project(request: Request) -> Optional[Response]:
    """project 필수인 action에서 누락 시 에러 응답을 반환한다."""
    if not request.project:
        return _error(ErrorCode.MISSING_PARAM, "project는 필수입니다.")
    return None


def _require_params(request: Request, *keys: str) -> Optional[Response]:
    """필수 params 키가 누락되었으면 에러 응답을 반환한다."""
    for key in keys:
        if key not in request.params or request.params[key] is None:
            return _error(ErrorCode.MISSING_PARAM, f"'{key}' 파라미터는 필수입니다.")
    return None


# ═══════════════════════════════════════════════════════════
# Action 핸들러
# ═══════════════════════════════════════════════════════════

def _handle_submit(api, request: Request) -> Response:
    """task 제출."""
    if err := _require_project(request):
        return err
    if err := _require_params(request, "title"):
        return err

    attachments = None
    if request.attachments:
        project_dir = os.path.join(api.projects_dir, request.project)
        attachments = _resolve_attachments(request.attachments, project_dir)

    result = api.submit(
        project=request.project,
        title=request.params["title"],
        description=request.params.get("description", ""),
        attachments=attachments,
        config_override=request.params.get("config_override"),
        source=request.source,
    )
    return _ok(result, f"task {result.task_id} 제출 완료")


def _handle_get_task(api, request: Request) -> Response:
    """단건 task 조회."""
    if err := _require_project(request):
        return err
    if err := _require_params(request, "task_id"):
        return err

    task = api.get_task(request.project, request.params["task_id"])
    return _ok(task, f"task {request.params['task_id']} 조회 완료")


def _handle_list(api, request: Request) -> Response:
    """task 목록 조회."""
    tasks = api.list_tasks(
        project=request.project,
        status=request.params.get("status"),
    )
    return _ok(tasks, f"{len(tasks)}개 task 조회")


def _handle_pending(api, request: Request) -> Response:
    """승인 대기 항목 조회."""
    items = api.pending(project=request.project)
    return _ok(items, f"{len(items)}개 대기 중")


def _handle_approve(api, request: Request) -> Response:
    """plan/replan 승인."""
    if err := _require_project(request):
        return err
    if err := _require_params(request, "task_id"):
        return err

    ok = api.approve(
        request.project,
        request.params["task_id"],
        message=request.params.get("message"),
    )
    if ok:
        return _ok(True, f"task {request.params['task_id']} 승인 완료")
    return _error(ErrorCode.INVALID_STATE, "승인 대기 상태가 아닙니다.")


def _handle_reject(api, request: Request) -> Response:
    """plan/replan 거부."""
    if err := _require_project(request):
        return err
    if err := _require_params(request, "task_id", "message"):
        return err

    ok = api.reject(
        request.project,
        request.params["task_id"],
        message=request.params["message"],
    )
    if ok:
        return _ok(True, f"task {request.params['task_id']} 수정 요청 완료")
    return _error(ErrorCode.INVALID_STATE, "승인 대기 상태가 아닙니다.")


def _handle_feedback(api, request: Request) -> Response:
    """실행 중 task에 피드백."""
    if err := _require_project(request):
        return err
    if err := _require_params(request, "task_id", "message"):
        return err

    api.feedback(
        request.project,
        request.params["task_id"],
        message=request.params["message"],
    )
    return _ok(True, f"task {request.params['task_id']}에 피드백 추가 완료")


def _handle_config(api, request: Request) -> Response:
    """프로젝트 설정 동적 변경."""
    if err := _require_project(request):
        return err
    if err := _require_params(request, "changes"):
        return err

    overrides = api.config(request.project, request.params["changes"])
    return _ok(overrides, f"{request.project} 설정 변경 완료")


def _handle_pause(api, request: Request) -> Response:
    """프로젝트 또는 task 일시정지."""
    if err := _require_project(request):
        return err

    api.pause(request.project, task_id=request.params.get("task_id"))
    target = f"task {request.params['task_id']}" if request.params.get("task_id") else request.project
    return _ok(True, f"{target} 일시정지 요청 전달")


def _handle_resume(api, request: Request) -> Response:
    """프로젝트 또는 task 재개."""
    if err := _require_project(request):
        return err

    api.resume(request.project, task_id=request.params.get("task_id"))
    target = f"task {request.params['task_id']}" if request.params.get("task_id") else request.project
    return _ok(True, f"{target} 재개 요청 전달")


def _handle_cancel(api, request: Request) -> Response:
    """task 취소."""
    if err := _require_project(request):
        return err
    if err := _require_params(request, "task_id"):
        return err

    ok = api.cancel(request.project, request.params["task_id"])
    if ok:
        return _ok(True, f"task {request.params['task_id']} 취소 완료")
    return _error(ErrorCode.INVALID_STATE, "취소할 수 없는 상태입니다.")


def _handle_status(api, request: Request) -> Response:
    """시스템 상태 조회."""
    status = api.status()
    return _ok(status, "시스템 상태 조회 완료")


def _handle_notifications(api, request: Request) -> Response:
    """알림 목록 조회."""
    notifications = api.notifications(
        project=request.project,
        limit=request.params.get("limit", 20),
        unread_only=request.params.get("unread_only", False),
    )
    return _ok(notifications, f"{len(notifications)}개 알림 조회")


def _handle_mark_notification_read(api, request: Request) -> Response:
    """알림 읽음 처리."""
    if err := _require_project(request):
        return err

    api.mark_notification_read(
        request.project,
        up_to_timestamp=request.params.get("up_to_timestamp"),
    )
    return _ok(True, f"{request.project} 알림 읽음 처리 완료")


# ═══════════════════════════════════════════════════════════
# Action 레지스트리
# ═══════════════════════════════════════════════════════════

# Chatbot 시스템 프롬프트 생성 시 이 레지스트리를 참조
ACTION_REGISTRY = {
    "submit": {
        "handler": _handle_submit,
        "description": "새 task를 제출한다.",
        "required_params": ["title"],
        "optional_params": ["description", "config_override"],
        "requires_project": True,
    },
    "get_task": {
        "handler": _handle_get_task,
        "description": "단건 task를 상세 조회한다.",
        "required_params": ["task_id"],
        "optional_params": [],
        "requires_project": True,
    },
    "list": {
        "handler": _handle_list,
        "description": "task 목록을 조회한다.",
        "required_params": [],
        "optional_params": ["status"],
        "requires_project": False,
    },
    "pending": {
        "handler": _handle_pending,
        "description": "승인 대기 중인 항목을 조회한다.",
        "required_params": [],
        "optional_params": [],
        "requires_project": False,
    },
    "approve": {
        "handler": _handle_approve,
        "description": "plan/replan을 승인한다.",
        "required_params": ["task_id"],
        "optional_params": ["message"],
        "requires_project": True,
    },
    "reject": {
        "handler": _handle_reject,
        "description": "plan/replan을 거부(수정 요청)한다.",
        "required_params": ["task_id", "message"],
        "optional_params": [],
        "requires_project": True,
    },
    "feedback": {
        "handler": _handle_feedback,
        "description": "실행 중인 task에 피드백을 추가한다.",
        "required_params": ["task_id", "message"],
        "optional_params": [],
        "requires_project": True,
    },
    "config": {
        "handler": _handle_config,
        "description": "프로젝트 설정을 동적으로 변경한다.",
        "required_params": ["changes"],
        "optional_params": [],
        "requires_project": True,
    },
    "pause": {
        "handler": _handle_pause,
        "description": "프로젝트 또는 task를 일시정지한다.",
        "required_params": [],
        "optional_params": ["task_id"],
        "requires_project": True,
    },
    "resume": {
        "handler": _handle_resume,
        "description": "프로젝트 또는 task를 재개한다.",
        "required_params": [],
        "optional_params": ["task_id"],
        "requires_project": True,
    },
    "cancel": {
        "handler": _handle_cancel,
        "description": "task를 취소한다.",
        "required_params": ["task_id"],
        "optional_params": [],
        "requires_project": True,
    },
    "status": {
        "handler": _handle_status,
        "description": "시스템 전체 상태를 조회한다.",
        "required_params": [],
        "optional_params": [],
        "requires_project": False,
    },
    "notifications": {
        "handler": _handle_notifications,
        "description": "알림 목록을 조회한다.",
        "required_params": [],
        "optional_params": ["limit", "unread_only"],
        "requires_project": False,
    },
    "mark_notification_read": {
        "handler": _handle_mark_notification_read,
        "description": "알림을 읽음 처리한다.",
        "required_params": [],
        "optional_params": ["up_to_timestamp"],
        "requires_project": True,
    },
}


# ═══════════════════════════════════════════════════════════
# dispatch — 단일 진입점
# ═══════════════════════════════════════════════════════════

def dispatch(api, request: Request) -> Response:
    """
    Request를 받아 적절한 HubAPI 메서드를 호출하고 Response를 반환한다.
    모든 프론트엔드(Chatbot, 메신저, 웹)의 단일 진입점.
    """
    if not request.action:
        return _error(ErrorCode.INVALID_ACTION, "action이 비어 있습니다.")

    entry = ACTION_REGISTRY.get(request.action)
    if not entry:
        available = ", ".join(sorted(ACTION_REGISTRY.keys()))
        return _error(
            ErrorCode.INVALID_ACTION,
            f"알 수 없는 action: '{request.action}'. 사용 가능: {available}",
        )

    handler = entry["handler"]

    try:
        return handler(api, request)
    except FileNotFoundError as e:
        # 프로젝트/task 없음 → 적절한 에러 코드로 변환
        msg = str(e)
        if "프로젝트" in msg:
            return _error(ErrorCode.PROJECT_NOT_FOUND, msg)
        return _error(ErrorCode.TASK_NOT_FOUND, msg)
    except Exception as e:
        return _error(ErrorCode.INTERNAL_ERROR, f"내부 오류: {e}")


# ═══════════════════════════════════════════════════════════
# 유틸리티 — Chatbot 시스템 프롬프트 생성용
# ═══════════════════════════════════════════════════════════

def get_action_descriptions() -> str:
    """
    Chatbot 시스템 프롬프트에 포함할 action 목록 텍스트를 생성한다.
    각 action의 설명, 필수/선택 파라미터, project 필요 여부를 포함.
    """
    lines = []
    for action_name, entry in sorted(ACTION_REGISTRY.items()):
        desc = entry["description"]
        required = entry["required_params"]
        optional = entry["optional_params"]
        needs_project = entry["requires_project"]

        parts = [f"- **{action_name}**: {desc}"]
        if needs_project:
            parts.append("  project: 필수")
        if required:
            parts.append(f"  필수 params: {', '.join(required)}")
        if optional:
            parts.append(f"  선택 params: {', '.join(optional)}")
        lines.append("\n".join(parts))

    return "\n".join(lines)
