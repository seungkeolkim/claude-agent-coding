"""
파일→DB 동기화 엔진.

Task JSON 파일(source of truth)의 변경을 감지하여 SQLite DB에 반영한다.
mtime 기반 delta sync로 변경된 파일만 읽는다.
"""

import glob
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Callable

from scripts.web.db import Database

logger = logging.getLogger(__name__)


class FileSyncer:
    """파일 기반 상태를 SQLite DB로 동기화하는 엔진."""

    def __init__(self, db: Database, projects_dir: str, session_history_dir: str,
                 on_change: Callable[[dict], None] = None):
        """
        Args:
            db: Database 인스턴스
            projects_dir: projects/ 디렉토리 절대 경로
            session_history_dir: session_history/ 디렉토리 절대 경로
            on_change: 변경 감지 시 호출되는 콜백 (SSE 이벤트 발행용)
        """
        self.db = db
        self.projects_dir = projects_dir
        self.session_history_dir = session_history_dir
        self.on_change = on_change

        # mtime 캐시: {file_path: last_known_mtime}
        self._mtime_cache: dict[str, float] = {}

        # agent_hub_root: telegram bridge command queue를 쓰기 위해 projects_dir 상위에서 도출.
        self._agent_hub_root = os.path.dirname(os.path.abspath(projects_dir))

        # 백그라운드 스레드
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # ─── 전체 sync ───

    def sync_all(self):
        """모든 프로젝트와 세션을 동기화한다."""
        if not os.path.isdir(self.projects_dir):
            return

        # 현재 폴더에 존재하는 프로젝트 목록
        existing_dirs = set()
        for name in sorted(os.listdir(self.projects_dir)):
            project_dir = os.path.join(self.projects_dir, name)
            if os.path.isdir(project_dir):
                existing_dirs.add(name)
                self.sync_project(name)

        # DB에 있지만 폴더가 없는 프로젝트 → closed 처리
        db_projects = self.db.get_projects()
        for p in db_projects:
            if p["name"] not in existing_dirs and p.get("lifecycle", "active") != "closed":
                self.db.upsert_project(
                    name=p["name"],
                    status="idle",
                    lifecycle="closed",
                )
                logger.info("폴더 소실 감지 → 프로젝트 '%s' closed 처리", p["name"])
                if self.on_change:
                    self.on_change({"type": "project_updated", "project": p["name"]})
                # Telegram bridge에도 topic close 요청 (bridge 미기동이어도 큐에 남는다).
                self._enqueue_telegram_close(p["name"])

        self.sync_sessions()

    def sync_project(self, name: str):
        """프로젝트 하나를 동기화한다."""
        project_dir = os.path.join(self.projects_dir, name)
        if not os.path.isdir(project_dir):
            return

        changed = False
        if self._sync_project_state(name, project_dir):
            changed = True
        if self._sync_tasks(name, project_dir):
            changed = True
        if self._sync_notifications(name, project_dir):
            changed = True

        if changed and self.on_change:
            self.on_change({"type": "project_updated", "project": name})

    # ─── 프로젝트 상태 sync ───

    def _sync_project_state(self, name: str, project_dir: str) -> bool:
        """project_state.json을 DB에 반영한다. 변경 시 True."""
        state_path = os.path.join(project_dir, "project_state.json")
        if not os.path.isfile(state_path):
            # state 파일이 없어도 프로젝트 디렉토리는 존재 → idle로 등록
            self.db.upsert_project(name, status="idle")
            return True

        mtime = os.path.getmtime(state_path)
        if self._mtime_cache.get(state_path) == mtime:
            return False

        try:
            with open(state_path) as f:
                state = json.load(f)
            self.db.upsert_project(
                name=name,
                status=state.get("status", "idle"),
                current_task_id=state.get("current_task_id"),
                last_error_task_id=state.get("last_error_task_id"),
                last_updated=state.get("last_updated"),
                config_overrides=state.get("overrides"),
                lifecycle=state.get("lifecycle", "active"),
                wfc_pid=state.get("wfc_pid"),
            )
            self._mtime_cache[state_path] = mtime
            return True
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("project_state.json 읽기 실패 (%s): %s", name, e)
            return False

    # ─── Task sync ───

    def _sync_tasks(self, name: str, project_dir: str) -> bool:
        """tasks/ 디렉토리의 task JSON 파일들을 DB에 반영한다."""
        tasks_dir = os.path.join(project_dir, "tasks")
        if not os.path.isdir(tasks_dir):
            return False

        changed = False
        # 현재 파일 목록
        task_files = glob.glob(os.path.join(tasks_dir, "*.json"))
        current_task_ids = set()

        for file_path in task_files:
            filename = os.path.basename(file_path)
            # task_id 추출: 00001-slug.json → 00001
            task_id = filename.split("-", 1)[0] if "-" in filename else filename.replace(".json", "")
            current_task_ids.add(task_id)

            mtime = os.path.getmtime(file_path)
            if self._mtime_cache.get(file_path) == mtime:
                continue

            try:
                with open(file_path) as f:
                    task_data = json.load(f)
                self.db.upsert_task(task_data, file_path, mtime)
                self._mtime_cache[file_path] = mtime
                changed = True

                if self.on_change:
                    self.on_change({
                        "type": "task_updated",
                        "project": name,
                        "task_id": task_id,
                        "status": task_data.get("status"),
                    })
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("task 파일 읽기 실패 (%s): %s", file_path, e)

        # DB에는 있지만 파일에서 삭제된 task 정리
        db_tasks = self.db.get_tasks(project=name)
        for db_task in db_tasks:
            if db_task["task_id"] not in current_task_ids:
                self.db.delete_task(name, db_task["task_id"])
                # mtime 캐시에서도 제거
                cached_path = db_task.get("file_path")
                if cached_path:
                    self._mtime_cache.pop(cached_path, None)
                changed = True

        return changed

    # ─── 알림 sync ───

    def _sync_notifications(self, name: str, project_dir: str) -> bool:
        """notifications.json의 신규 항목을 DB에 추가한다."""
        noti_path = os.path.join(project_dir, "notifications.json")
        if not os.path.isfile(noti_path):
            return False

        mtime = os.path.getmtime(noti_path)
        if self._mtime_cache.get(noti_path) == mtime:
            return False

        try:
            with open(noti_path) as f:
                notifications = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("notifications.json 읽기 실패 (%s): %s", name, e)
            return False

        # DB에서 가장 최근 알림의 created_at을 가져와서 그 이후 것만 삽입
        max_created_at = self.db.get_max_notification_created_at(name)
        new_count = 0

        for noti in notifications:
            created_at = noti.get("created_at", "")
            # max_created_at 이후의 알림만 삽입
            if max_created_at and created_at <= max_created_at:
                continue

            self.db.insert_notification(
                project=name,
                event_type=noti.get("event_type", ""),
                task_id=noti.get("task_id"),
                message=noti.get("message", ""),
                details=noti.get("details"),
                created_at=created_at,
                read=noti.get("read", False),
            )
            new_count += 1

            if self.on_change:
                self.on_change({
                    "type": "notification",
                    "project": name,
                    "event_type": noti.get("event_type"),
                    "task_id": noti.get("task_id"),
                    "message": noti.get("message", ""),
                })

        self._mtime_cache[noti_path] = mtime
        return new_count > 0

    # ─── 세션 sync ───

    def sync_sessions(self):
        """session_history/ 내의 세션 메타데이터를 DB에 반영한다."""
        if not os.path.isdir(self.session_history_dir):
            return

        for frontend in os.listdir(self.session_history_dir):
            frontend_dir = os.path.join(self.session_history_dir, frontend)
            if not os.path.isdir(frontend_dir):
                continue

            for filename in os.listdir(frontend_dir):
                if not filename.endswith(".json"):
                    continue

                file_path = os.path.join(frontend_dir, filename)
                mtime = os.path.getmtime(file_path)
                if self._mtime_cache.get(file_path) == mtime:
                    continue

                try:
                    with open(file_path) as f:
                        session = json.load(f)
                    self.db.upsert_session(
                        session_id=session.get("session_id", filename.replace(".json", "")),
                        frontend=session.get("frontend", frontend),
                        created_at=session.get("created_at"),
                        updated_at=session.get("updated_at"),
                        turn_count=session.get("turn_count", 0),
                        file_path=file_path,
                    )
                    self._mtime_cache[file_path] = mtime
                except (json.JSONDecodeError, OSError) as e:
                    logger.warning("세션 파일 읽기 실패 (%s): %s", file_path, e)

    # ─── 백그라운드 폴링 ───

    def start_background_sync(self, interval_seconds: float = 2.0):
        """백그라운드 스레드에서 주기적으로 sync를 수행한다."""
        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._background_loop,
            args=(interval_seconds,),
            daemon=True,
            name="file-syncer",
        )
        self._thread.start()
        logger.info("FileSyncer 백그라운드 sync 시작 (주기: %.1f초)", interval_seconds)

    def stop_background_sync(self):
        """백그라운드 sync를 중단한다."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("FileSyncer 백그라운드 sync 중단")

    def _background_loop(self, interval: float):
        """주기적으로 sync_all()을 호출하는 루프."""
        while not self._stop_event.is_set():
            try:
                self.sync_all()
            except Exception as e:
                logger.error("sync_all 실패: %s", e)
            self._stop_event.wait(interval)

    # ─── Telegram bridge hook ───

    def _enqueue_telegram_close(self, project: str) -> None:
        """Telegram bridge에 close_topic 명령 파일을 기록한다.

        폴더 소실 감지 경로(HubAPI를 거치지 않는 close)에서 호출된다.
        bridge 미기동이어도 queue에 쌓여 다음 기동 시 소비된다. fire-and-forget.
        """
        try:
            cmd_dir = os.path.join(self._agent_hub_root, "data", "telegram_commands")
            os.makedirs(cmd_dir, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            cid = uuid.uuid4().hex[:8]
            path = os.path.join(cmd_dir, f"{ts}_{cid}_close_topic.json")
            payload = {
                "action": "close_topic",
                "project": project,
                "requested_at": datetime.now(timezone.utc).isoformat(),
            }
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp, path)
        except Exception as e:
            logger.warning("telegram close_topic enqueue 실패 (%s): %s", project, e)
