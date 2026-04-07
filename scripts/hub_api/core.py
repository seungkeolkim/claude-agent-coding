"""
HubAPI — Agent Hub 공통 인터페이스 코어.

CLI, 메신저, 웹 콘솔 모두 이 클래스를 import하여 사용한다.
모든 상태는 파일시스템에 저장되며, DB나 네트워크 통신은 없다.
"""

import fcntl
import glob
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from typing import Optional

from hub_api.models import (
    CreateProjectResult,
    HumanInteractionInfo,
    ProjectStatus,
    SubmitResult,
    SystemStatus,
    TaskSummary,
)


class HubAPI:
    """Agent Hub 공통 인터페이스. 파일시스템 기반 task/project 관리."""

    def __init__(self, agent_hub_root: str):
        self.root = os.path.abspath(agent_hub_root)
        self.projects_dir = os.path.join(self.root, "projects")

        # notification 모듈 lazy import (scripts/ 내부)
        self._notification_module = None

    def _get_notification_module(self):
        """notification 모듈을 lazy import한다."""
        if self._notification_module is None:
            import importlib
            import sys as _sys
            scripts_dir = os.path.join(self.root, "scripts")
            if scripts_dir not in _sys.path:
                _sys.path.insert(0, scripts_dir)
            self._notification_module = importlib.import_module("notification")
        return self._notification_module

    # ═══════════════════════════════════════════════════════════
    # 내부 헬퍼
    # ═══════════════════════════════════════════════════════════

    def _project_dir(self, project: str) -> str:
        """프로젝트 디렉토리 경로를 반환한다. 존재하지 않으면 예외."""
        path = os.path.join(self.projects_dir, project)
        if not os.path.isdir(path):
            raise FileNotFoundError(f"프로젝트를 찾을 수 없음: {project}")
        return path

    def _tasks_dir(self, project: str) -> str:
        """프로젝트의 tasks/ 디렉토리 경로."""
        return os.path.join(self._project_dir(project), "tasks")

    def _commands_dir(self, project: str) -> str:
        """프로젝트의 commands/ 디렉토리 경로. 없으면 생성."""
        path = os.path.join(self._project_dir(project), "commands")
        os.makedirs(path, exist_ok=True)
        return path

    def _load_json(self, path: str) -> dict:
        """JSON 파일을 읽어 dict로 반환한다."""
        with open(path) as f:
            return json.load(f)

    def _save_json_atomic(self, path: str, data: dict):
        """
        dict를 JSON 파일로 atomic하게 저장한다.
        tmp 파일에 먼저 쓰고 os.replace로 교체하여 동시성 안전.
        """
        dir_path = os.path.dirname(path)
        fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp_path, path)
        except Exception:
            # 실패 시 tmp 정리
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    def _next_task_id(self, tasks_dir: str) -> str:
        """
        tasks/ 디렉토리에서 다음 task ID를 결정한다.
        기존 task 파일들의 최대 ID + 1. flock으로 동시성 보호.
        """
        os.makedirs(tasks_dir, exist_ok=True)

        # tasks 디렉토리에 advisory lock
        lock_path = os.path.join(tasks_dir, ".lock")
        lock_fd = open(lock_path, "w")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)

            max_id = 0
            for name in os.listdir(tasks_dir):
                # 파일명에서 숫자 접두사 추출: 00042-xxx.json → 42
                if name.endswith(".json"):
                    parts = name.split("-", 1)
                    try:
                        num = int(parts[0])
                        if num > max_id:
                            max_id = num
                    except ValueError:
                        continue

            return f"{max_id + 1:05d}"
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()

    def _find_task_file(self, tasks_dir: str, task_id: str) -> Optional[str]:
        """task ID로 task JSON 파일을 찾는다. 없으면 None."""
        pattern = os.path.join(tasks_dir, f"{task_id}*.json")
        matches = glob.glob(pattern)
        if not matches:
            return None
        # 정확히 task_id로 시작하는 파일 (00042.json 또는 00042-설명.json)
        for m in matches:
            basename = os.path.basename(m)
            prefix = basename.split("-", 1)[0].split(".")[0]
            if prefix == task_id:
                return m
        return matches[0] if matches else None

    def _get_project_lifecycle(self, project: str) -> str:
        """프로젝트의 lifecycle 상태를 반환한다. 필드가 없으면 'active'로 간주."""
        project_dir = os.path.join(self.projects_dir, project)
        state_path = os.path.join(project_dir, "project_state.json")
        if os.path.exists(state_path):
            try:
                state = self._load_json(state_path)
                return state.get("lifecycle", "active")
            except (json.JSONDecodeError, OSError):
                pass
        return "active"

    def _require_active_project(self, project: str) -> None:
        """프로젝트가 active 상태인지 검증한다. closed면 ValueError."""
        lifecycle = self._get_project_lifecycle(project)
        if lifecycle != "active":
            raise ValueError(
                f"프로젝트 '{project}'는 '{lifecycle}' 상태이므로 이 작업을 수행할 수 없습니다. "
                f"reopen_project로 다시 활성화하세요."
            )

    def _list_projects(self, include_closed: bool = False) -> list:
        """projects/ 하위의 프로젝트 이름을 반환한다.

        Args:
            include_closed: True이면 closed 프로젝트도 포함. 기본은 active만.
        """
        if not os.path.isdir(self.projects_dir):
            return []
        projects = []
        for name in sorted(os.listdir(self.projects_dir)):
            project_yaml = os.path.join(self.projects_dir, name, "project.yaml")
            if os.path.isfile(project_yaml):
                if not include_closed:
                    lifecycle = self._get_project_lifecycle(name)
                    if lifecycle != "active":
                        continue
                projects.append(name)
        return projects

    # ═══════════════════════════════════════════════════════════
    # 프로젝트 생성
    # ═══════════════════════════════════════════════════════════

    def _get_init_project_module(self):
        """init_project 모듈을 lazy import한다."""
        if not hasattr(self, "_init_project_module"):
            import importlib
            import sys as _sys
            scripts_dir = os.path.join(self.root, "scripts")
            if scripts_dir not in _sys.path:
                _sys.path.insert(0, scripts_dir)
            self._init_project_module = importlib.import_module("init_project")
        return self._init_project_module

    def create_project(
        self,
        name: str,
        description: str,
        codebase_path: str,
        git_settings: Optional[dict] = None,
    ) -> CreateProjectResult:
        """
        새 프로젝트를 생성한다.

        디렉토리 구조, project.yaml, project_state.json을 생성한다.
        설정되지 않은 필드는 __UNCONFIGURED__ 플레이스홀더가 들어가며,
        프로젝트 실행 전 반드시 사용자가 실제 값으로 교체해야 한다.

        Args:
            name: 프로젝트 이름 (영문소문자, 숫자, 하이픈)
            description: 프로젝트 설명
            codebase_path: 코드베이스 절대경로 (없으면 자동 생성)
            git_settings: git 연동 설정 dict. None이면 플레이스홀더 기본값 사용.
                keys: enabled, remote, author_name, author_email,
                      base_branch, pr_target_branch, merge_strategy

        Returns:
            CreateProjectResult

        Raises:
            ValueError: 이름 형식 오류 또는 상대경로
            FileExistsError: 이미 존재하는 프로젝트
        """
        init_project = self._get_init_project_module()

        # 1. 이름 유효성 검사
        if not init_project.PROJECT_NAME_PATTERN.match(name):
            raise ValueError(
                f"잘못된 프로젝트 이름: '{name}'. "
                "영문소문자, 숫자, 하이픈만 사용 가능. 하이픈으로 시작/끝 불가."
            )

        # 2. 중복 검사
        project_directory = os.path.join(self.projects_dir, name)
        if os.path.exists(project_directory):
            raise FileExistsError(
                f"프로젝트가 이미 존재합니다: {name}"
            )

        # 3. codebase_path 절대경로 검증
        expanded_codebase_path = os.path.expanduser(codebase_path)
        if not os.path.isabs(expanded_codebase_path):
            raise ValueError(
                f"codebase_path는 절대경로여야 합니다: '{codebase_path}'"
            )

        # 4. codebase 디렉토리가 없으면 자동 생성
        if not os.path.exists(expanded_codebase_path):
            os.makedirs(expanded_codebase_path, exist_ok=True)
        elif not os.path.isdir(expanded_codebase_path):
            raise ValueError(
                f"codebase_path가 디렉토리가 아닙니다: '{expanded_codebase_path}'"
            )

        # 5. git_settings 기본값 merge (미설정 필드는 플레이스홀더)
        placeholder = init_project.UNCONFIGURED_PLACEHOLDER
        default_git_settings = {
            "enabled": False,
            "remote": "origin",
            "author_name": placeholder,
            "author_email": placeholder,
            "base_branch": "main",
            "pr_target_branch": "main",
            "merge_strategy": "require_human",
        }
        if git_settings:
            default_git_settings.update(git_settings)
        effective_git_settings = default_git_settings

        # 6. 디렉토리 구조 생성
        from pathlib import Path
        project_root = Path(project_directory)
        init_project.create_project_directory_structure(project_root)

        # 7. project.yaml 생성
        yaml_path = init_project.generate_project_yaml(
            project_root, name, description,
            expanded_codebase_path, effective_git_settings,
        )

        # 8. project_state.json 초기화
        state_path = init_project.initialize_project_state(project_root, name)

        return CreateProjectResult(
            project_name=name,
            project_directory=str(project_directory),
            project_yaml_path=str(yaml_path),
            project_state_path=str(state_path),
        )

    # ═══════════════════════════════════════════════════════════
    # task 생명주기
    # ═══════════════════════════════════════════════════════════

    def submit(self, project: str, title: str, description: str,
               attachments: Optional[list] = None,
               config_override: Optional[dict] = None,
               source: str = "cli") -> SubmitResult:
        """
        새 task를 생성하고 .ready sentinel을 만든다.

        1. 다음 task_id 결정 (기존 최대 + 1)
        2. task JSON 생성 (atomic write)
        3. 첨부파일 복사 (있으면)
        4. .ready sentinel 생성 → TM이 감지하여 WFC spawn
        """
        self._require_active_project(project)

        tasks_dir = self._tasks_dir(project)
        task_id = self._next_task_id(tasks_dir)
        now = datetime.now(timezone.utc).isoformat()

        # 첨부파일 처리
        attachment_list = []
        if attachments:
            attach_dir = os.path.join(
                self._project_dir(project), "attachments", task_id
            )
            os.makedirs(attach_dir, exist_ok=True)

            for attach in attachments:
                src_path = attach.get("path", "")
                filename = attach.get("filename", os.path.basename(src_path))
                attach_type = attach.get("type", "reference")
                attach_desc = attach.get("description", "")

                if src_path and os.path.isfile(src_path):
                    dest = os.path.join(attach_dir, filename)
                    shutil.copy2(src_path, dest)

                attachment_list.append({
                    "filename": filename,
                    "path": f"attachments/{task_id}/{filename}",
                    "type": attach_type,
                    "description": attach_desc,
                })

        # task 제목을 slug로 변환하여 파일명에 사용
        slug = _make_slug(title)
        task_filename = f"{task_id}-{slug}.json" if slug else f"{task_id}.json"
        task_path = os.path.join(tasks_dir, task_filename)

        # task JSON 생성
        task_data = {
            "task_id": task_id,
            "project_name": project,
            "title": title,
            "description": description,
            "submitted_via": source,
            "submitted_at": now,
            "status": "submitted",
            "branch": None,
            "attachments": attachment_list,
            "plan_version": 0,
            "current_subtask": None,
            "completed_subtasks": [],
            "counters": {
                "total_agent_invocations": 0,
                "replan_count": 0,
                "current_subtask_retry": 0,
            },
            "config_override": config_override or {},
            "human_interaction": None,
            "mid_task_feedback": [],
            "escalation_reason": None,
            "summary": None,
            "pr_url": None,
        }

        self._save_json_atomic(task_path, task_data)

        # .ready sentinel 생성 → TM이 감지
        ready_path = os.path.join(tasks_dir, f"{task_id}.ready")
        with open(ready_path, "w") as f:
            f.write(now)

        return SubmitResult(
            task_id=task_id,
            project=project,
            file_path=task_path,
        )

    def get_task(self, project: str, task_id: str) -> dict:
        """
        단건 task를 조회한다.
        task JSON 전체를 dict로 반환. 없으면 FileNotFoundError.
        """
        tasks_dir = self._tasks_dir(project)
        task_file = self._find_task_file(tasks_dir, task_id)
        if not task_file:
            raise FileNotFoundError(f"task를 찾을 수 없음: {project}/{task_id}")
        return self._load_json(task_file)

    def list_tasks(self, project: Optional[str] = None,
                   status: Optional[str] = None,
                   include_closed: bool = False) -> list:
        """
        task 목록을 조회한다.
        project를 지정하면 해당 프로젝트만, 아니면 전체.
        status를 지정하면 해당 상태만 필터링.

        Args:
            include_closed: True이면 closed 프로젝트의 task도 포함.
        """
        projects = [project] if project else self._list_projects(include_closed=include_closed)
        results = []

        for proj in projects:
            try:
                tasks_dir = self._tasks_dir(proj)
            except FileNotFoundError:
                continue

            for task_file in sorted(glob.glob(os.path.join(tasks_dir, "*.json"))):
                try:
                    task = self._load_json(task_file)
                except (json.JSONDecodeError, OSError):
                    continue

                task_status = task.get("status", "")
                if status and task_status != status:
                    continue

                results.append(TaskSummary(
                    task_id=task.get("task_id", ""),
                    project=proj,
                    title=task.get("title", ""),
                    status=task_status,
                    submitted_at=task.get("submitted_at"),
                    current_subtask=task.get("current_subtask"),
                    pr_url=task.get("pr_url"),
                ))

        return results

    def cancel(self, project: str, task_id: str) -> bool:
        """
        task를 취소한다.
        실행 중이면 commands/cancel-{task_id}.command 파일을 생성하여 WFC에 전달.
        submitted/queued 상태면 직접 status 변경.
        """
        tasks_dir = self._tasks_dir(project)
        task_file = self._find_task_file(tasks_dir, task_id)
        if not task_file:
            raise FileNotFoundError(f"task를 찾을 수 없음: {project}/{task_id}")

        task = self._load_json(task_file)
        current_status = task.get("status", "")

        # 이미 완료/취소된 task
        if current_status in ("completed", "cancelled", "failed"):
            return False

        # 아직 실행 전이면 직접 취소
        if current_status in ("submitted", "queued", "waiting_for_human_plan_confirm"):
            task["status"] = "cancelled"
            self._save_json_atomic(task_file, task)
            # .ready 파일이 남아있으면 삭제
            ready_path = os.path.join(tasks_dir, f"{task_id}.ready")
            if os.path.exists(ready_path):
                os.unlink(ready_path)
            return True

        # 실행 중이면 .command 파일로 WFC에 전달
        cmd_dir = self._commands_dir(project)
        cmd_path = os.path.join(cmd_dir, f"cancel-{task_id}.command")
        cmd_data = {
            "action": "cancel",
            "task_id": task_id,
            "requested_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save_json_atomic(cmd_path, cmd_data)
        return True

    def get_plan(self, project: str, task_id: str) -> Optional[dict]:
        """
        task의 plan.json을 읽어 dict로 반환한다.

        plan이 아직 생성되지 않았으면 None을 반환한다.

        Args:
            project: 프로젝트명
            task_id: task ID

        Returns:
            plan dict 또는 None
        """
        tasks_dir = self._tasks_dir(project)
        task_file = self._find_task_file(tasks_dir, task_id)
        if not task_file:
            raise FileNotFoundError(f"task를 찾을 수 없음: {project}/{task_id}")

        # plan.json은 tasks/{task_id}/plan.json 에 위치
        plan_dir = os.path.join(tasks_dir, task_id)
        plan_path = os.path.join(plan_dir, "plan.json")

        if not os.path.isfile(plan_path):
            return None

        return self._load_json(plan_path)

    def resubmit(self, project: str, task_id: str,
                 config_override: Optional[dict] = None) -> SubmitResult:
        """
        cancelled/failed task를 새 task로 재제출한다.

        원본 task의 title, description, attachments를 복사하여 새 task를 생성한다.
        원본 task는 변경하지 않는다.

        Args:
            project: 프로젝트명
            task_id: 원본 task ID
            config_override: 새 task에 적용할 config_override (없으면 원본 것 사용)

        Returns:
            SubmitResult (새로 생성된 task 정보)

        Raises:
            FileNotFoundError: task를 찾을 수 없음
            ValueError: 재제출 가능한 상태가 아님 (cancelled/failed만 가능)
        """
        tasks_dir = self._tasks_dir(project)
        task_file = self._find_task_file(tasks_dir, task_id)
        if not task_file:
            raise FileNotFoundError(f"task를 찾을 수 없음: {project}/{task_id}")

        original_task = self._load_json(task_file)
        original_status = original_task.get("status", "")

        # cancelled/failed만 재제출 가능
        resubmittable_statuses = {"cancelled", "failed"}
        if original_status not in resubmittable_statuses:
            raise ValueError(
                f"task {task_id}는 '{original_status}' 상태이므로 재제출할 수 없습니다. "
                f"재제출 가능 상태: {', '.join(sorted(resubmittable_statuses))}"
            )

        # 원본의 title, description, attachments를 복사하여 새 task 생성
        return self.submit(
            project=project,
            title=original_task.get("title", ""),
            description=original_task.get("description", ""),
            attachments=original_task.get("attachments"),
            config_override=config_override or original_task.get("config_override", {}),
            source=original_task.get("submitted_via", "cli"),
        )

    # ═══════════════════════════════════════════════════════════
    # human interaction
    # ═══════════════════════════════════════════════════════════

    def pending(self, project: Optional[str] = None) -> list:
        """
        사용자 응답을 기다리는 항목 목록을 반환한다.
        - status가 'waiting_for_human_plan_confirm'이고 응답이 아직 없는 human interaction
        - status가 'waiting_for_human_pr_approve'인 task (PR 머지 대기 등)
        """
        projects = [project] if project else self._list_projects()
        results = []

        for proj in projects:
            try:
                tasks_dir = self._tasks_dir(proj)
            except FileNotFoundError:
                continue

            for task_file in sorted(glob.glob(os.path.join(tasks_dir, "*.json"))):
                try:
                    task = self._load_json(task_file)
                except (json.JSONDecodeError, OSError):
                    continue

                status = task.get("status")

                # waiting_for_human_pr_approve: PR 생성 완료, 수동 머지/리뷰 대기
                if status == "waiting_for_human_pr_approve":
                    results.append(HumanInteractionInfo(
                        task_id=task.get("task_id", ""),
                        project=proj,
                        interaction_type="waiting_for_human_pr_approve",
                        message=f"PR 리뷰/머지 대기: {task.get('title', '')}",
                        options=["approve", "reject"],
                        requested_at=task.get("pipeline_stage_updated_at"),
                        payload_path=None,
                    ))
                    continue

                # waiting_for_human_plan_confirm: 명시적 human interaction 요청
                if status != "waiting_for_human_plan_confirm":
                    continue

                hi = task.get("human_interaction")
                if not hi or hi.get("response"):
                    continue

                results.append(HumanInteractionInfo(
                    task_id=task.get("task_id", ""),
                    project=proj,
                    interaction_type=hi.get("type", ""),
                    message=hi.get("message", ""),
                    options=hi.get("options", []),
                    requested_at=hi.get("requested_at"),
                    payload_path=hi.get("payload_path"),
                ))

        return results

    def approve(self, project: str, task_id: str,
                message: Optional[str] = None,
                attachments: Optional[list] = None) -> bool:
        """
        plan/replan을 승인한다.
        task JSON의 human_interaction.response에 승인 기록.
        WFC가 이를 폴링하여 파이프라인 재개.
        """
        return self._respond_to_interaction(
            project, task_id, action="approve",
            message=message, attachments=attachments,
        )

    def reject(self, project: str, task_id: str,
               message: str,
               attachments: Optional[list] = None) -> bool:
        """
        plan/replan을 거부(수정 요청)한다.
        message는 필수 — 왜 거부하는지, 어떻게 수정할지.
        """
        return self._respond_to_interaction(
            project, task_id, action="modify",
            message=message, attachments=attachments,
        )

    def feedback(self, project: str, task_id: str,
                 message: str,
                 attachments: Optional[list] = None) -> bool:
        """
        실행 중인 task에 피드백을 추가한다.
        mid_task_feedback 배열에 append. WFC가 다음 agent 호출 전에 읽음.
        """
        tasks_dir = self._tasks_dir(project)
        task_file = self._find_task_file(tasks_dir, task_id)
        if not task_file:
            raise FileNotFoundError(f"task를 찾을 수 없음: {project}/{task_id}")

        task = self._load_json(task_file)

        feedback_entry = {
            "message": message,
            "attachments": attachments or [],
            "submitted_at": datetime.now(timezone.utc).isoformat(),
        }

        if "mid_task_feedback" not in task:
            task["mid_task_feedback"] = []
        task["mid_task_feedback"].append(feedback_entry)

        self._save_json_atomic(task_file, task)
        return True

    def _respond_to_interaction(self, project: str, task_id: str,
                                action: str, message: Optional[str] = None,
                                attachments: Optional[list] = None) -> bool:
        """human interaction에 응답을 기록하는 내부 헬퍼."""
        tasks_dir = self._tasks_dir(project)
        task_file = self._find_task_file(tasks_dir, task_id)
        if not task_file:
            raise FileNotFoundError(f"task를 찾을 수 없음: {project}/{task_id}")

        task = self._load_json(task_file)

        if task.get("status") != "waiting_for_human_plan_confirm":
            return False

        hi = task.get("human_interaction")
        if not hi:
            return False

        # 응답 기록
        hi["response"] = {
            "action": action,
            "message": message or "",
            "attachments": attachments or [],
            "responded_at": datetime.now(timezone.utc).isoformat(),
        }
        task["human_interaction"] = hi

        # 승인이면 상태를 planned로 되돌림 (WFC가 다음 단계 진행)
        if action == "approve":
            task["status"] = "planned"
        # 거부(modify)면 WFC가 replan 처리
        elif action == "modify":
            task["status"] = "needs_replan"

        self._save_json_atomic(task_file, task)
        return True

    # ═══════════════════════════════════════════════════════════
    # PR 리뷰 완료
    # ═══════════════════════════════════════════════════════════

    def complete_pr_review(self, project: str, task_id: str,
                           result: str, message: Optional[str] = None) -> bool:
        """
        PR 리뷰 결과를 반영하여 waiting_for_human_pr_approve 상태에서 탈출한다.

        Args:
            project: 프로젝트명
            task_id: task ID
            result: "merged" → completed, "rejected" → failed
            message: 선택적 코멘트

        Returns:
            True: 성공적으로 상태 전이
        """
        if result not in ("merged", "rejected"):
            raise ValueError(f"result는 'merged' 또는 'rejected'만 가능합니다: {result}")

        tasks_dir = self._tasks_dir(project)
        task_file = self._find_task_file(tasks_dir, task_id)
        if not task_file:
            raise FileNotFoundError(f"task를 찾을 수 없음: {project}/{task_id}")

        task = self._load_json(task_file)

        if task.get("status") != "waiting_for_human_pr_approve":
            return False

        # PR 리뷰 결과 기록
        task["pr_review_result"] = result
        task["pr_reviewed_at"] = datetime.now(timezone.utc).isoformat()
        if message:
            task["pr_review_message"] = message

        # 상태 전이
        if result == "merged":
            task["status"] = "completed"
        else:  # rejected
            task["status"] = "failed"
            task["failure_reason"] = f"PR 거부됨: {message or '사유 없음'}"

        self._save_json_atomic(task_file, task)
        return True

    # ═══════════════════════════════════════════════════════════
    # 프로젝트 lifecycle
    # ═══════════════════════════════════════════════════════════

    def close_project(self, project: str) -> bool:
        """
        프로젝트를 종료(closed)한다.

        모든 task가 종료 상태(completed, cancelled, failed, escalated)일 때만 가능.
        미완료 task가 있으면 ValueError.

        Returns:
            True: 성공적으로 closed
        """
        project_dir = self._project_dir(project)

        # 이미 closed면 무시
        if self._get_project_lifecycle(project) == "closed":
            return True

        # 미완료 task 확인
        terminal_statuses = {"completed", "cancelled", "failed", "escalated"}
        tasks_dir = os.path.join(project_dir, "tasks")
        if os.path.isdir(tasks_dir):
            for task_file in glob.glob(os.path.join(tasks_dir, "*.json")):
                try:
                    task = self._load_json(task_file)
                except (json.JSONDecodeError, OSError):
                    continue
                status = task.get("status", "")
                if status and status not in terminal_statuses:
                    raise ValueError(
                        f"프로젝트 '{project}'에 미완료 task가 있습니다 "
                        f"(task {task.get('task_id', '?')}: {status}). "
                        f"모든 task를 완료/취소한 후 다시 시도하세요."
                    )

        # lifecycle → closed
        state_path = os.path.join(project_dir, "project_state.json")
        if os.path.exists(state_path):
            state = self._load_json(state_path)
        else:
            state = {"project_name": project}

        state["lifecycle"] = "closed"
        state["status"] = "idle"
        state["current_task_id"] = None
        state["last_updated"] = datetime.now(timezone.utc).isoformat()
        self._save_json_atomic(state_path, state)
        return True

    def reopen_project(self, project: str) -> bool:
        """
        종료된(closed) 프로젝트를 다시 활성화한다.

        Returns:
            True: 성공적으로 active로 전환
        """
        project_dir = self._project_dir(project)

        # 이미 active면 무시
        if self._get_project_lifecycle(project) == "active":
            return True

        state_path = os.path.join(project_dir, "project_state.json")
        if os.path.exists(state_path):
            state = self._load_json(state_path)
        else:
            state = {"project_name": project}

        state["lifecycle"] = "active"
        state["last_updated"] = datetime.now(timezone.utc).isoformat()
        self._save_json_atomic(state_path, state)
        return True

    # ═══════════════════════════════════════════════════════════
    # 설정 & 제어
    # ═══════════════════════════════════════════════════════════

    def config(self, project: str, changes: dict) -> dict:
        """
        프로젝트 동적 설정을 변경한다.
        project_state.json의 overrides에 deep merge.
        update_history에 변경 이력 기록.

        changes 예시: {"testing": {"unit_test": {"enabled": True}}}
        """
        project_dir = self._project_dir(project)
        state_path = os.path.join(project_dir, "project_state.json")

        if os.path.exists(state_path):
            try:
                state = self._load_json(state_path)
            except (json.JSONDecodeError, OSError):
                state = {"project_name": project}
        else:
            state = {"project_name": project}

        # overrides에 deep merge
        overrides = state.get("overrides", {})
        overrides = _deep_merge(overrides, changes)
        state["overrides"] = overrides

        # update_history 기록
        if "update_history" not in state:
            state["update_history"] = []
        state["update_history"].append({
            "changes": changes,
            "at": datetime.now(timezone.utc).isoformat(),
        })

        state["last_updated"] = datetime.now(timezone.utc).isoformat()
        self._save_json_atomic(state_path, state)

        return overrides

    def pause(self, project: str, task_id: Optional[str] = None) -> bool:
        """프로젝트 또는 특정 task를 일시정지한다.

        task_id를 지정한 경우, 실행 중(in_progress)인 task만 일시정지 가능.
        종료된 task(completed, cancelled, failed)에는 False 반환.
        """
        if task_id:
            self._validate_task_is_active(project, task_id, "pause")
        return self._send_command(project, "pause", task_id)

    def resume(self, project: str, task_id: Optional[str] = None) -> bool:
        """프로젝트 또는 특정 task를 재개한다.

        task_id를 지정한 경우, 실행 중이거나 대기 중인 task만 재개 가능.
        종료된 task(completed, cancelled, failed)에는 ValueError.
        """
        if task_id:
            self._validate_task_is_active(project, task_id, "resume")
        return self._send_command(project, "resume", task_id)

    def _validate_task_is_active(self, project: str, task_id: str, action: str) -> None:
        """task가 활성 상태인지 검증한다. 종료된 task이면 ValueError."""
        terminal_statuses = {"completed", "cancelled", "failed"}
        tasks_dir = self._tasks_dir(project)
        task_file = self._find_task_file(tasks_dir, task_id)
        if not task_file:
            raise FileNotFoundError(f"task를 찾을 수 없음: {project}/{task_id}")

        task = self._load_json(task_file)
        current_status = task.get("status", "")
        if current_status in terminal_statuses:
            raise ValueError(
                f"task {task_id}는 '{current_status}' 상태이므로 {action}할 수 없습니다. "
                f"재실행하려면 resubmit을 사용하세요."
            )

    def _send_command(self, project: str, action: str,
                      task_id: Optional[str] = None) -> bool:
        """WFC에 .command 파일을 전달하는 내부 헬퍼."""
        cmd_dir = self._commands_dir(project)
        filename = f"{action}-{task_id}.command" if task_id else f"{action}.command"
        cmd_path = os.path.join(cmd_dir, filename)

        cmd_data = {
            "action": action,
            "task_id": task_id,
            "requested_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save_json_atomic(cmd_path, cmd_data)
        return True

    # ═══════════════════════════════════════════════════════════
    # 조회
    # ═══════════════════════════════════════════════════════════

    def status(self, include_closed: bool = False) -> SystemStatus:
        """
        시스템 전체 상태를 조회한다.
        TM 실행 여부 + 프로젝트별 상태.

        Args:
            include_closed: True이면 closed 프로젝트도 포함.
        """
        # TM PID 확인
        tm_running = False
        tm_pid = None
        pids_dir = os.path.join(self.root, ".pids")
        if os.path.isdir(pids_dir):
            for f in os.listdir(pids_dir):
                if f.startswith("task_manager.") and f.endswith(".pid"):
                    # 파일명에서 PID 추출: task_manager.12345.pid → 12345
                    try:
                        pid = int(f.split(".")[1])
                        # 프로세스 존재 확인
                        os.kill(pid, 0)
                        tm_running = True
                        tm_pid = pid
                    except (ValueError, IndexError, ProcessLookupError, PermissionError):
                        pass

        # pgrep fallback
        if not tm_running:
            try:
                import subprocess
                result = subprocess.run(
                    ["pgrep", "-f", "scripts/task_manager.py"],
                    capture_output=True, text=True,
                )
                if result.returncode == 0 and result.stdout.strip():
                    pid_str = result.stdout.strip().split("\n")[0]
                    tm_pid = int(pid_str)
                    tm_running = True
            except (subprocess.SubprocessError, ValueError):
                pass

        # 프로젝트별 상태
        projects = []
        for proj_name in self._list_projects(include_closed=include_closed):
            proj_dir = os.path.join(self.projects_dir, proj_name)
            state_path = os.path.join(proj_dir, "project_state.json")

            if os.path.exists(state_path):
                try:
                    state = self._load_json(state_path)
                    projects.append(ProjectStatus(
                        name=proj_name,
                        status=state.get("status", "unknown"),
                        lifecycle=state.get("lifecycle", "active"),
                        current_task_id=state.get("current_task_id"),
                        last_error_task_id=state.get("last_error_task_id"),
                        last_updated=state.get("last_updated"),
                    ))
                except (json.JSONDecodeError, OSError):
                    projects.append(ProjectStatus(name=proj_name, status="unknown"))
            else:
                projects.append(ProjectStatus(name=proj_name, status="idle"))

        return SystemStatus(
            tm_running=tm_running,
            tm_pid=tm_pid,
            projects=projects,
        )

    # ═══════════════════════════════════════════════════════════
    # 알림
    # ═══════════════════════════════════════════════════════════

    def notifications(self, project: Optional[str] = None,
                      limit: int = 20, unread_only: bool = False) -> list:
        """
        알림 목록을 조회한다.
        project를 지정하면 해당 프로젝트만, 아니면 전체.

        Returns:
            list[dict]: 알림 목록 (최신 순)
        """
        noti = self._get_notification_module()
        projects = [project] if project else self._list_projects()
        results = []

        for proj in projects:
            try:
                proj_dir = self._project_dir(proj)
            except FileNotFoundError:
                continue

            notifications = noti.get_notifications(
                proj_dir, unread_only=unread_only, limit=limit,
            )
            for n in notifications:
                n["project"] = proj
            results.extend(notifications)

        # 전체를 최신 순 정렬 + limit 적용
        results.sort(key=lambda n: n.get("created_at", ""), reverse=True)
        if limit:
            results = results[:limit]

        return results

    def mark_notification_read(self, project: str,
                               up_to_timestamp: Optional[str] = None) -> bool:
        """
        알림을 읽음 처리한다.
        up_to_timestamp까지의 알림을 읽음 처리. None이면 전부.
        """
        noti = self._get_notification_module()
        proj_dir = self._project_dir(project)
        noti.mark_notifications_read(proj_dir, up_to_timestamp=up_to_timestamp)
        return True


# ═══════════════════════════════════════════════════════════
# 유틸리티
# ═══════════════════════════════════════════════════════════


def _deep_merge(base: dict, override: dict) -> dict:
    """base dict 위에 override dict를 재귀적으로 덮어쓴다."""
    merged = dict(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _make_slug(title: str, max_len: int = 40) -> str:
    """
    task 제목을 파일명에 쓸 수 있는 slug로 변환한다.
    한국어/영문 모두 지원. 공백→하이픈, 특수문자 제거.
    """
    import re
    # 파일명에 사용 불가능한 문자 제거
    slug = re.sub(r'[<>:"/\\|?*]', '', title)
    # 공백을 하이픈으로
    slug = re.sub(r'\s+', '-', slug.strip())
    # 연속 하이픈 제거
    slug = re.sub(r'-+', '-', slug)
    # 길이 제한
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip('-')
    return slug
