"""
hub_api 데이터 모델 정의.

HubAPI 메서드의 입출력에 사용되는 데이터클래스.
모든 프론트엔드(CLI, 메신저, 웹)에서 동일한 모델을 사용한다.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SubmitResult:
    """task submit 결과."""
    task_id: str
    project: str
    file_path: str
    status: str = "submitted"
    priority: str = "default"


@dataclass
class TaskSummary:
    """task 목록 조회 시 반환되는 요약 정보."""
    task_id: str
    project: str
    title: str
    status: str
    submitted_at: Optional[str] = None
    current_subtask: Optional[str] = None
    pr_url: Optional[str] = None


@dataclass
class HumanInteractionInfo:
    """사용자 응답을 기다리는 human interaction 정보."""
    task_id: str
    project: str
    interaction_type: str   # plan_review | replan_review | escalation
    message: str
    options: list = field(default_factory=list)
    requested_at: Optional[str] = None
    payload_path: Optional[str] = None
    pr_merge_error: Optional[str] = None  # waiting_for_human_pr_approve 상태에서 최근 머지 실패 에러 메시지


@dataclass
class ProjectStatus:
    """프로젝트 상태 정보."""
    name: str
    status: str                                  # idle | running
    lifecycle: str = "active"                    # active | closed
    current_task_id: Optional[str] = None
    last_error_task_id: Optional[str] = None
    last_updated: Optional[str] = None


@dataclass
class SystemStatus:
    """시스템 전체 상태."""
    tm_running: bool
    tm_pid: Optional[int] = None
    projects: list = field(default_factory=list)  # list[ProjectStatus]


@dataclass
class CreateProjectResult:
    """프로젝트 생성 결과."""
    project_name: str
    project_directory: str
    project_yaml_path: str
    project_state_path: str
