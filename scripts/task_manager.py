#!/usr/bin/env python3
"""
Task Manager (TM) — Agent Hub 시스템의 상주 프로세스.

projects/ 디렉토리를 폴링하여 .ready sentinel이 있는 task를 감지하고,
프로젝트별로 순차적으로 Workflow Controller(WFC)를 spawn하여 파이프라인을 실행한다.

사용법 (run_system.sh에서 호출):
    python3 scripts/task_manager.py --config /path/to/config.yaml
    python3 scripts/task_manager.py --config /path/to/config.yaml --polling-interval 10

종료 시그널:
    SIGTERM  — 새 task spawn 중단, 실행 중 WFC 완료 대기 후 종료
    SIGUSR1  — 모든 WFC 강제종료(SIGKILL) 후 즉시 종료
"""

import argparse
import glob
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import yaml

from notification import (
    get_notifications,
    mark_notifications_read,
    format_notification_cli,
    format_notification_plain,
)
from usage_checker import wait_until_below_threshold

# ─── 색상 출력 (터미널용) ───
GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
RED = "\033[0;31m"
CYAN = "\033[0;36m"
NC = "\033[0m"

# ─── 로거 ───
_file_logger = None


def setup_file_logger(log_path):
    """
    TM rotation 파일 로거를 초기화한다.
    100MB x 최대 5개 rotation.
    """
    global _file_logger
    os.makedirs(os.path.dirname(log_path), exist_ok=True)

    _file_logger = logging.getLogger("task_manager")
    _file_logger.setLevel(logging.DEBUG)
    _file_logger.handlers.clear()

    handler = RotatingFileHandler(
        log_path,
        maxBytes=100 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    _file_logger.addHandler(handler)


def _log_to_file(level, msg):
    """파일 로거가 초기화되어 있으면 기록한다."""
    if _file_logger:
        _file_logger.log(level, msg)


def log_info(msg):
    """정보 로그를 출력한다."""
    print(f"{GREEN}[TM]{NC} {msg}", flush=True)
    _log_to_file(logging.INFO, msg)


def log_warn(msg):
    """경고 로그를 출력한다."""
    print(f"{YELLOW}[TM]{NC} {msg}", flush=True)
    _log_to_file(logging.WARNING, msg)


def log_error(msg):
    """에러 로그를 출력한다."""
    print(f"{RED}[TM]{NC} {msg}", file=sys.stderr, flush=True)
    _log_to_file(logging.ERROR, msg)


def load_json(path):
    """JSON 파일을 읽어 dict로 반환한다."""
    with open(path) as f:
        return json.load(f)


def save_json(path, data):
    """dict를 JSON 파일로 atomic하게 저장한다."""
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp_path, path)


def load_yaml(path):
    """YAML 파일을 읽어 dict로 반환한다."""
    with open(path) as f:
        return yaml.safe_load(f) or {}


class TaskManager:
    """
    Task Manager 상주 프로세스.

    projects/ 디렉토리를 폴링하여 .ready sentinel이 있는 task를 찾고,
    프로젝트별로 순차적으로 WFC를 spawn하여 파이프라인을 실행한다.
    """

    def __init__(self, agent_hub_root, config, polling_interval=2, dummy=False):
        """
        TaskManager를 초기화한다.

        Args:
            agent_hub_root: agent-hub 루트 디렉토리 절대경로
            config: config.yaml 내용 (dict)
            polling_interval: 폴링 주기 (초)
            dummy: True면 WFC를 --dummy 모드로 실행 (claude 호출 없이 더미 JSON 출력)
        """
        self._agent_hub_root = agent_hub_root
        self._config = config
        self._polling_interval = polling_interval
        self._dummy = dummy
        self._projects_dir = os.path.join(agent_hub_root, "projects")
        self._pids_dir = os.path.join(agent_hub_root, ".pids")

        # 프로젝트별 WFC 상태 추적 (인메모리)
        # {project_name: {"process": Popen|None, "current_task_id": str|None}}
        self._project_states = {}

        # 종료 플래그
        self._shutdown_requested = False
        self._force_shutdown_requested = False

        # 알림 폴링: 프로젝트별 마지막으로 확인한 알림 시각
        self._last_notification_check = {}  # {project_name: ISO timestamp}

        # PR Watcher 스레드
        self._pr_watcher_thread = None

    # ═══════════════════════════════════════════════════════════
    # 프로젝트 스캔
    # ═══════════════════════════════════════════════════════════

    def scan_projects(self):
        """
        projects/ 디렉토리에서 project.yaml이 존재하는 프로젝트 이름 목록을 반환한다.
        새로 발견된 프로젝트는 _project_states에 자동 등록한다.
        """
        if not os.path.isdir(self._projects_dir):
            return []

        project_names = []
        for entry in sorted(os.listdir(self._projects_dir)):
            project_dir = os.path.join(self._projects_dir, entry)
            project_yaml = os.path.join(project_dir, "project.yaml")
            if os.path.isdir(project_dir) and os.path.isfile(project_yaml):
                project_names.append(entry)
                # 새 프로젝트 자동 등록
                if entry not in self._project_states:
                    self._project_states[entry] = {
                        "process": None,
                        "current_task_id": None,
                    }
                    log_info(f"프로젝트 감지: {entry}")

        return project_names

    # ═══════════════════════════════════════════════════════════
    # task 큐 관리
    # ═══════════════════════════════════════════════════════════

    def find_ready_tasks(self, project_name):
        """
        projects/{name}/tasks/*.ready 파일을 찾아 task ID 목록을 반환한다.
        정렬하여 가장 낮은 번호부터 처리한다.
        """
        tasks_dir = os.path.join(self._projects_dir, project_name, "tasks")
        if not os.path.isdir(tasks_dir):
            return []

        ready_files = sorted(Path(tasks_dir).glob("*.ready"))
        task_ids = []
        for ready_file in ready_files:
            # 00003.ready → 00003
            task_id = ready_file.stem
            # task JSON 파일이 존재하는지 확인
            task_json_exists = (
                list(Path(tasks_dir).glob(f"{task_id}-*.json"))
                or (Path(tasks_dir) / f"{task_id}.json").exists()
            )
            if task_json_exists:
                task_ids.append(task_id)
            else:
                log_warn(f"[{project_name}] .ready 파일은 있으나 task JSON 없음: {task_id}")

        return task_ids

    def has_incomplete_tasks(self, project_name):
        """
        해당 프로젝트에 아직 완료되지 않은 task가 있는지 확인한다.
        WFC 파이프라인이 아직 실행 중인 상태만 미완료로 판단.
        waiting_for_human_pr_approve는 WFC가 끝난 상태 (PR 수동 머지 대기)이므로 블로킹하지 않는다.

        Returns:
            (bool, list[str]): (미완료 task 존재 여부, 미완료 task ID 목록)
        """
        tasks_dir = os.path.join(self._projects_dir, project_name, "tasks")
        if not os.path.isdir(tasks_dir):
            return False, []

        incomplete_statuses = {
            "in_progress", "planned", "running",
            "waiting_for_human_plan_confirm", "needs_replan",
        }
        incomplete_task_ids = []

        for entry in sorted(os.listdir(tasks_dir)):
            if not entry.endswith(".json"):
                continue
            # subtask 디렉토리 내 파일은 무시 (최상위 task JSON만)
            task_path = os.path.join(tasks_dir, entry)
            if not os.path.isfile(task_path):
                continue
            try:
                task = load_json(task_path)
                status = task.get("status", "")
                if status in incomplete_statuses:
                    # task ID 추출: "00042-로그인-구현.json" → "00042"
                    task_id = entry.split("-")[0].replace(".json", "")
                    incomplete_task_ids.append(task_id)
            except (json.JSONDecodeError, OSError):
                continue

        return bool(incomplete_task_ids), incomplete_task_ids

    def should_block_next_task(self, project_name):
        """
        wait_for_prev_task_done 설정에 따라 다음 task spawn을 블로킹해야 하는지 확인한다.
        프로젝트별 설정 > 시스템 기본값 순서로 참조한다.

        Returns:
            (bool, list[str]): (블로킹 여부, 미완료 task ID 목록)
        """
        # 시스템 기본값
        default_wait = self._config.get("default_task_queue", {}).get(
            "wait_for_prev_task_done", True
        )

        # 프로젝트별 override 확인
        project_yaml_path = os.path.join(
            self._projects_dir, project_name, "project.yaml"
        )
        wait_for_prev = default_wait
        try:
            project_config = load_yaml(project_yaml_path)
            project_wait = (
                project_config.get("task_queue", {}).get("wait_for_prev_task_done")
            )
            if project_wait is not None:
                wait_for_prev = project_wait
        except (OSError, yaml.YAMLError):
            pass

        if not wait_for_prev:
            return False, []

        has_incomplete, incomplete_ids = self.has_incomplete_tasks(project_name)
        return has_incomplete, incomplete_ids

    def consume_ready_sentinel(self, project_name, task_id):
        """
        .ready 파일을 삭제하여 중복 처리를 방지한다.
        """
        ready_path = os.path.join(
            self._projects_dir, project_name, "tasks", f"{task_id}.ready"
        )
        try:
            os.remove(ready_path)
            log_info(f"[{project_name}] sentinel 소비: {task_id}.ready")
        except FileNotFoundError:
            # 이미 삭제됨 (race condition 방어)
            pass

    # ═══════════════════════════════════════════════════════════
    # WFC 프로세스 관리
    # ═══════════════════════════════════════════════════════════

    def spawn_workflow_controller(self, project_name, task_id):
        """
        WFC를 subprocess로 spawn한다.
        stdout/stderr는 projects/{name}/logs/wfc_{task_id}.log에 리다이렉트한다.

        Returns:
            spawn된 Popen 객체
        """
        # 로그 디렉토리 생성
        log_dir = os.path.join(self._projects_dir, project_name, "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, f"wfc_{task_id}.log")

        # venv의 python3 경로를 사용 (activate_venv.sh가 설정한 환경)
        python_path = sys.executable

        cmd = [
            python_path,
            os.path.join(self._agent_hub_root, "scripts", "workflow_controller.py"),
            "--project", project_name,
            "--task", task_id,
        ]
        if self._dummy:
            cmd.append("--dummy")

        log_info(f"[{project_name}] WFC spawn: task {task_id}")
        log_info(f"[{project_name}] WFC 로그: {log_path}")

        log_file = open(log_path, "a", encoding="utf-8")
        # 로그 파일에 시작 구분자 기록
        log_file.write(f"\n{'=' * 60}\n")
        log_file.write(f"WFC 시작: task={task_id} at {datetime.now(timezone.utc).isoformat()}\n")
        log_file.write(f"명령: {' '.join(cmd)}\n")
        log_file.write(f"{'=' * 60}\n\n")
        log_file.flush()

        process = subprocess.Popen(
            cmd,
            cwd=self._agent_hub_root,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            # 프로세스 그룹 생성 (force kill 시 자식 포함 종료)
            preexec_fn=os.setsid,
        )

        # 인메모리 상태 갱신
        self._project_states[project_name]["process"] = process
        self._project_states[project_name]["current_task_id"] = task_id
        self._project_states[project_name]["_log_file"] = log_file

        # project_state.json 갱신
        self.update_project_state(project_name, "running", task_id, wfc_pid=process.pid)

        return process

    def check_workflow_controller(self, project_name):
        """
        해당 프로젝트의 WFC 프로세스 상태를 확인한다.
        완료(returncode != None)이면 정리하고, 다음 task를 탐색할 수 있게 한다.
        """
        state = self._project_states.get(project_name)
        if not state or not state["process"]:
            return

        process = state["process"]
        returncode = process.poll()

        if returncode is None:
            # 아직 실행 중
            return

        task_id = state["current_task_id"]

        # 로그 파일 닫기
        log_file = state.get("_log_file")
        if log_file:
            log_file.close()

        if returncode == 0:
            log_info(f"[{project_name}] WFC 완료: task {task_id} (성공)")
        else:
            log_error(f"[{project_name}] WFC 종료: task {task_id} (exit code: {returncode})")

        # 인메모리 상태 초기화
        state["process"] = None
        state["current_task_id"] = None
        state["_log_file"] = None

        # project_state.json 갱신
        last_error = task_id if returncode != 0 else None
        self.update_project_state(project_name, "idle", current_task_id=None, last_error=last_error)

    # ═══════════════════════════════════════════════════════════
    # 알림 폴링
    # ═══════════════════════════════════════════════════════════

    def poll_notifications(self, project_name):
        """
        프로젝트의 새 알림을 확인하고 터미널에 출력한다.
        마지막 확인 시각 이후의 알림만 출력.
        """
        project_dir = os.path.join(self._projects_dir, project_name)
        since = self._last_notification_check.get(project_name)

        new_notifications = get_notifications(
            project_dir, since=since, unread_only=True,
        )

        if not new_notifications:
            return

        # 오래된 순으로 출력 (get_notifications는 최신 순이므로 역순)
        for notification in reversed(new_notifications):
            cli_line = format_notification_cli(notification, project_name=project_name)
            print(cli_line, flush=True)

            # 파일 로거에만 기록 (stdout 중복 방지)
            plain_line = format_notification_plain(notification, project_name=project_name)
            _log_to_file(logging.INFO, f"[알림] {plain_line}")

        # 마지막 확인 시각 갱신
        latest_timestamp = max(
            n.get("created_at", "") for n in new_notifications
        )
        self._last_notification_check[project_name] = latest_timestamp

        # 출력한 알림을 읽음 처리
        mark_notifications_read(project_dir, up_to_timestamp=latest_timestamp)

    # ═══════════════════════════════════════════════════════════
    # 상태 관리
    # ═══════════════════════════════════════════════════════════

    def update_project_state(self, project_name, status, current_task_id=None,
                             wfc_pid=None, last_error=None):
        """
        projects/{name}/project_state.json을 갱신한다.
        기존 파일이 있으면 merge, 없으면 새로 생성한다.
        """
        state_path = os.path.join(
            self._projects_dir, project_name, "project_state.json"
        )

        if os.path.exists(state_path):
            try:
                state = load_json(state_path)
            except (json.JSONDecodeError, OSError):
                state = {"project_name": project_name}
        else:
            state = {"project_name": project_name}

        state["status"] = status
        state["current_task_id"] = current_task_id
        state["last_updated"] = datetime.now(timezone.utc).isoformat()

        if wfc_pid is not None:
            state["wfc_pid"] = wfc_pid
        elif status == "idle":
            state.pop("wfc_pid", None)

        if last_error:
            state["last_error_task_id"] = last_error

        save_json(state_path, state)

    # ═══════════════════════════════════════════════════════════
    # PID 파일 관리
    # ═══════════════════════════════════════════════════════════

    def write_pid_file(self):
        """TM 자신의 PID를 .pids/task_manager.{PID}.pid에 기록한다."""
        os.makedirs(self._pids_dir, exist_ok=True)
        pid = os.getpid()
        self._pid_file = os.path.join(self._pids_dir, f"task_manager.{pid}.pid")
        save_json(self._pid_file, {
            "pid": pid,
            "agent_type": "task_manager",
            "task_id": "system",
            "started_at": datetime.now(timezone.utc).isoformat(),
        })

    def remove_pid_file(self):
        """TM의 PID 파일을 삭제한다."""
        pid_file = getattr(self, "_pid_file", None)
        if not pid_file:
            return
        try:
            os.remove(pid_file)
        except FileNotFoundError:
            pass

    # ═══════════════════════════════════════════════════════════
    # 시그널 핸들링 및 종료
    # ═══════════════════════════════════════════════════════════

    def handle_shutdown_signal(self, signum, frame):
        """
        SIGTERM 핸들러.
        새 task spawn을 중단하고, 실행 중 WFC가 완료될 때까지 대기한 후 종료한다.
        """
        log_info("SIGTERM 수신 — 새 task spawn 중단, 실행 중 WFC 완료 대기...")
        self._shutdown_requested = True

    def handle_force_shutdown_signal(self, signum, frame):
        """
        SIGUSR1 핸들러.
        모든 WFC를 강제종료(SIGKILL)하고 즉시 종료한다.
        """
        log_warn("SIGUSR1 수신 — 모든 WFC 강제종료...")
        self._force_shutdown_requested = True
        self._shutdown_requested = True

    # ═══════════════════════════════════════════════════════════
    # PR Watcher — waiting_for_human_pr_approve task의 PR 상태 자동 감지
    # ═══════════════════════════════════════════════════════════

    def start_pr_watcher(self):
        """PR Watcher 백그라운드 스레드를 시작한다."""
        if self._dummy:
            log_info("PR Watcher: dummy 모드에서는 비활성")
            return
        self._pr_watcher_thread = threading.Thread(
            target=self._pr_watcher_loop,
            daemon=True,
            name="pr-watcher",
        )
        self._pr_watcher_thread.start()
        log_info("PR Watcher 스레드 시작 (주기: 60초)")

    def _pr_watcher_loop(self):
        """60초 주기로 waiting_for_human_pr_approve task들의 PR 상태를 확인한다."""
        while not self._shutdown_requested:
            try:
                self._check_pending_prs()
            except Exception as e:
                log_error(f"PR Watcher 오류: {e}")
            # 60초 대기 (0.5초 단위로 shutdown 체크)
            for _ in range(120):
                if self._shutdown_requested:
                    return
                time.sleep(0.5)

    def _check_pending_prs(self):
        """모든 프로젝트에서 waiting_for_human_pr_approve task를 찾아 PR 상태를 확인한다."""
        if not os.path.isdir(self._projects_dir):
            return

        for project_name in sorted(os.listdir(self._projects_dir)):
            if self._shutdown_requested:
                return

            tasks_dir = os.path.join(self._projects_dir, project_name, "tasks")
            if not os.path.isdir(tasks_dir):
                continue

            for task_file in glob.glob(os.path.join(tasks_dir, "*.json")):
                if self._shutdown_requested:
                    return

                try:
                    with open(task_file) as f:
                        task = json.load(f)
                except (json.JSONDecodeError, OSError):
                    continue

                if task.get("status") != "waiting_for_human_pr_approve":
                    continue

                pr_url = task.get("pr_url")
                if not pr_url:
                    continue

                self._check_pr_state(project_name, task, task_file, pr_url)

    def _check_pr_state(self, project_name, task, task_file, pr_url):
        """gh pr view로 PR 상태를 확인하고 task 상태를 전이한다."""
        task_id = task.get("task_id", "")
        try:
            result = subprocess.run(
                ["gh", "pr", "view", pr_url, "--json", "state"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                log_warn(f"[PR Watcher] PR 상태 조회 실패 ({project_name}/{task_id}): {result.stderr.strip()}")
                return

            pr_data = json.loads(result.stdout)
            pr_state = pr_data.get("state", "").upper()
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
            log_warn(f"[PR Watcher] PR 상태 조회 오류 ({project_name}/{task_id}): {e}")
            return

        if pr_state == "MERGED":
            log_info(f"[PR Watcher] PR 머지 감지: {project_name}/{task_id} → completed")
            task["status"] = "completed"
            task["pr_review_result"] = "merged"
            task["pr_reviewed_at"] = datetime.now(timezone.utc).isoformat()
            self._save_task_json(task_file, task)
            self._emit_pr_notification(project_name, task_id, "pr_merged", f"PR 자동 감지: 머지 완료")
        elif pr_state == "CLOSED":
            log_info(f"[PR Watcher] PR 닫힘 감지: {project_name}/{task_id} → failed")
            task["status"] = "failed"
            task["pr_review_result"] = "rejected"
            task["pr_reviewed_at"] = datetime.now(timezone.utc).isoformat()
            task["failure_reason"] = "PR이 닫힘 (PR Watcher 자동 감지)"
            self._save_task_json(task_file, task)
            self._emit_pr_notification(project_name, task_id, "pr_rejected", f"PR 자동 감지: 거부/닫힘")

    def _save_task_json(self, task_file, task):
        """task JSON을 원자적으로 저장한다."""
        import tempfile
        dir_name = os.path.dirname(task_file)
        try:
            fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
            with os.fdopen(fd, "w") as f:
                json.dump(task, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, task_file)
        except OSError as e:
            log_error(f"task JSON 저장 실패: {e}")

    def _emit_pr_notification(self, project_name, task_id, event_type, message):
        """PR 상태 변경 알림을 발행한다."""
        try:
            from notification import emit_notification
            project_dir = os.path.join(self._projects_dir, project_name)
            emit_notification(
                project_dir=project_dir,
                event_type=event_type,
                task_id=task_id,
                message=message,
            )
        except Exception as e:
            log_warn(f"PR 알림 발행 실패: {e}")

    def shutdown(self, force=False):
        """
        TM을 종료한다.

        Args:
            force: True면 WFC에 SIGKILL 전송, False면 완료 대기
        """
        if force:
            log_warn("강제종료: 모든 WFC 프로세스 SIGKILL")
            for project_name, state in self._project_states.items():
                process = state.get("process")
                if process and process.poll() is None:
                    try:
                        # 프로세스 그룹 전체를 종료 (자식 프로세스 포함)
                        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                        log_info(f"[{project_name}] WFC 강제종료: PID {process.pid}")
                    except (ProcessLookupError, OSError):
                        pass
                    # project_state.json 갱신
                    self.update_project_state(project_name, "idle", current_task_id=None)
                # 로그 파일 닫기
                log_file = state.get("_log_file")
                if log_file:
                    log_file.close()
        else:
            # graceful: 실행 중 WFC 완료 대기
            running_projects = [
                name for name, state in self._project_states.items()
                if state.get("process") and state["process"].poll() is None
            ]
            if running_projects:
                log_info(f"실행 중인 WFC 완료 대기: {', '.join(running_projects)}")
                for project_name in running_projects:
                    state = self._project_states[project_name]
                    process = state["process"]
                    try:
                        process.wait()
                        log_info(f"[{project_name}] WFC 완료됨")
                    except Exception as e:
                        log_error(f"[{project_name}] WFC 대기 중 오류: {e}")
                    # 로그 파일 닫기
                    log_file = state.get("_log_file")
                    if log_file:
                        log_file.close()
                    self.update_project_state(project_name, "idle", current_task_id=None)

        self.remove_pid_file()
        log_info("Task Manager 종료")

    # ═══════════════════════════════════════════════════════════
    # 메인 루프
    # ═══════════════════════════════════════════════════════════

    def run(self):
        """
        메인 폴링 루프.

        매 polling_interval마다:
        1. 프로젝트 목록 스캔 (새 프로젝트 자동 감지)
        2. 각 프로젝트에 대해:
           a. WFC 실행 중이면 → poll()로 완료 여부 확인
           b. WFC 없으면 → .ready task 탐색 → spawn
        3. shutdown 플래그 확인
        """
        # 시그널 핸들러 등록
        signal.signal(signal.SIGTERM, self.handle_shutdown_signal)
        signal.signal(signal.SIGUSR1, self.handle_force_shutdown_signal)
        signal.signal(signal.SIGINT, self.handle_shutdown_signal)

        # PID 파일 기록
        self.write_pid_file()

        log_info(f"Task Manager 시작 (PID: {os.getpid()}, 폴링 주기: {self._polling_interval}초)")
        log_info(f"agent-hub 루트: {self._agent_hub_root}")

        # PR Watcher 스레드 시작
        self.start_pr_watcher()

        # 초기 프로젝트 스캔
        projects = self.scan_projects()
        if projects:
            log_info(f"등록된 프로젝트: {', '.join(projects)}")
        else:
            log_warn("등록된 프로젝트 없음. 새 프로젝트가 추가되면 자동 감지합니다.")

        while not self._shutdown_requested:
            try:
                # force shutdown 확인
                if self._force_shutdown_requested:
                    self.shutdown(force=True)
                    return

                # 프로젝트 스캔 (새 프로젝트 자동 감지)
                self.scan_projects()

                # 각 프로젝트 처리
                for project_name, state in self._project_states.items():
                    if self._shutdown_requested:
                        break

                    # 새 알림 확인 및 출력
                    self.poll_notifications(project_name)

                    # WFC가 실행 중이면 완료 여부 확인
                    if state.get("process"):
                        self.check_workflow_controller(project_name)
                        continue

                    # 미완료 task가 있으면 다음 task spawn 블로킹
                    should_block, incomplete_ids = self.should_block_next_task(project_name)
                    if should_block:
                        # 첫 감지 시에만 로그 출력 (반복 로그 방지)
                        blocked_key = f"_blocked_logged_{project_name}"
                        if not getattr(self, blocked_key, False):
                            log_info(
                                f"[{project_name}] 큐 블로킹: 미완료 task {incomplete_ids} 완료 대기 중"
                            )
                            setattr(self, blocked_key, True)
                        continue
                    else:
                        # 블로킹 해제 시 플래그 초기화
                        blocked_key = f"_blocked_logged_{project_name}"
                        if getattr(self, blocked_key, False):
                            log_info(f"[{project_name}] 큐 블로킹 해제")
                            setattr(self, blocked_key, False)

                    # WFC가 없으면 .ready task 탐색
                    ready_tasks = self.find_ready_tasks(project_name)
                    if ready_tasks:
                        # usage check: 새 task spawn 전
                        if not self._dummy:
                            thresholds = self._config.get("claude", {}).get(
                                "usage_thresholds", {}
                            )
                            new_task_threshold = thresholds.get("new_task", 0.70)
                            check_interval = self._config.get("claude", {}).get(
                                "usage_check_interval_seconds", 60
                            )
                            wait_until_below_threshold(
                                new_task_threshold,
                                check_interval_seconds=check_interval,
                                level_name="new_task",
                                log_fn=log_info,
                            )

                        # 가장 앞의 task를 처리
                        task_id = ready_tasks[0]
                        self.consume_ready_sentinel(project_name, task_id)
                        self.spawn_workflow_controller(project_name, task_id)

                # 폴링 대기 (짧은 간격으로 나눠서 시그널 반응성 확보)
                for _ in range(self._polling_interval * 2):
                    if self._shutdown_requested:
                        break
                    time.sleep(0.5)

            except Exception as e:
                log_error(f"폴링 루프 오류: {e}")
                time.sleep(self._polling_interval)

        # shutdown 처리
        self.shutdown(force=self._force_shutdown_requested)


def main():
    """진입점. CLI 인자를 파싱하고 TaskManager를 실행한다."""
    parser = argparse.ArgumentParser(description="Task Manager — Agent Hub 상주 프로세스")
    parser.add_argument(
        "--config",
        default=None,
        help="config.yaml 경로 (기본: {agent_hub_root}/config.yaml)",
    )
    parser.add_argument(
        "--polling-interval",
        type=int,
        default=2,
        help="폴링 주기 (초, 기본: 2)",
    )
    parser.add_argument(
        "--dummy",
        action="store_true",
        help="WFC를 --dummy 모드로 실행 (claude 호출 없이 더미 JSON 출력)",
    )
    args = parser.parse_args()

    # agent-hub 루트 결정 (이 스크립트는 scripts/ 안에 있음)
    agent_hub_root = str(Path(__file__).resolve().parent.parent)

    # config.yaml 로드
    config_path = args.config or os.path.join(agent_hub_root, "config.yaml")
    if not os.path.exists(config_path):
        log_error(f"config.yaml을 찾을 수 없습니다: {config_path}")
        log_error("./create_config.sh 를 먼저 실행하세요.")
        sys.exit(1)

    config = load_yaml(config_path)

    # 파일 로거 초기화 (logs/task_manager.log)
    log_dir = os.path.join(agent_hub_root, "logs")
    setup_file_logger(os.path.join(log_dir, "task_manager.log"))

    # TaskManager 실행
    task_manager = TaskManager(agent_hub_root, config, args.polling_interval, dummy=args.dummy)
    task_manager.run()


if __name__ == "__main__":
    main()
