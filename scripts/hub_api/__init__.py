"""
hub_api — Agent Hub 공통 인터페이스 레이어

CLI, 메신저, 웹 콘솔 모두에서 동일하게 사용하는 Python 라이브러리.
파일시스템 기반으로 task 생성, 조회, 승인/거부, 설정 변경 등을 수행한다.
"""

from hub_api.core import HubAPI
from hub_api.models import (
    SubmitResult,
    TaskSummary,
    HumanInteractionInfo,
    ProjectStatus,
    SystemStatus,
)

__all__ = [
    "HubAPI",
    "SubmitResult",
    "TaskSummary",
    "HumanInteractionInfo",
    "ProjectStatus",
    "SystemStatus",
]
