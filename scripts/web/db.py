"""
SQLite 데이터베이스 레이어.

Task JSON 파일이 source of truth이고, 이 DB는 조회/집계용 캐시.
파일→DB 단방향 sync만 수행한다.
"""

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
# 스키마 정의
# ═══════════════════════════════════════════════════════════

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    name TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'idle',
    current_task_id TEXT,
    last_error_task_id TEXT,
    last_updated TEXT,
    config_overrides TEXT,
    synced_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT NOT NULL,
    project TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'submitted',
    submitted_via TEXT DEFAULT 'cli',
    submitted_at TEXT,
    branch TEXT,
    pr_url TEXT,
    current_subtask TEXT,
    plan_version INTEGER DEFAULT 0,
    counters TEXT,
    human_interaction TEXT,
    summary TEXT,
    pipeline_stage TEXT,
    pipeline_stage_detail TEXT,
    pipeline_stage_updated_at TEXT,
    failure_reason TEXT,
    file_path TEXT NOT NULL,
    file_mtime REAL NOT NULL,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (project, task_id)
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_submitted ON tasks(submitted_at DESC);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    event_type TEXT NOT NULL,
    task_id TEXT,
    message TEXT NOT NULL,
    details TEXT,
    created_at TEXT NOT NULL,
    read INTEGER NOT NULL DEFAULT 0,
    synced_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_noti_unread ON notifications(read, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_noti_project ON notifications(project);

CREATE TABLE IF NOT EXISTS chatbot_sessions (
    session_id TEXT PRIMARY KEY,
    frontend TEXT NOT NULL DEFAULT 'chatbot',
    created_at TEXT,
    updated_at TEXT,
    turn_count INTEGER DEFAULT 0,
    file_path TEXT NOT NULL,
    synced_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_updated ON chatbot_sessions(updated_at DESC);
"""


# ═══════════════════════════════════════════════════════════
# Database 클래스
# ═══════════════════════════════════════════════════════════

class Database:
    """SQLite 데이터베이스 관리자. WAL 모드로 동시 읽기/쓰기 지원."""

    def __init__(self, db_path: str):
        """DB 파일 경로를 받아 초기화. 디렉토리가 없으면 생성."""
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_schema()

    def _init_schema(self):
        """스키마를 생성하고 마이그레이션을 적용한다."""
        with self.connect() as conn:
            conn.executescript(SCHEMA_SQL)
            # 버전 기록 (이미 있으면 무시)
            existing = conn.execute(
                "SELECT version FROM schema_version WHERE version = ?",
                (SCHEMA_VERSION,),
            ).fetchone()
            if not existing:
                conn.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (SCHEMA_VERSION, _now()),
                )
            # 마이그레이션: 기존 DB에 새 컬럼이 없으면 추가
            self._migrate(conn)

    def _migrate(self, conn):
        """기존 DB에 새 컬럼을 안전하게 추가한다."""
        # tasks 테이블의 컬럼 목록 조회
        columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        migrations = [
            ("pipeline_stage", "TEXT"),
            ("pipeline_stage_detail", "TEXT"),
            ("pipeline_stage_updated_at", "TEXT"),
            ("failure_reason", "TEXT"),
        ]
        for col_name, col_type in migrations:
            if col_name not in columns:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {col_name} {col_type}")
                logger.info("마이그레이션: tasks.%s 컬럼 추가", col_name)

        # projects 테이블 마이그레이션
        proj_columns = {row[1] for row in conn.execute("PRAGMA table_info(projects)").fetchall()}
        if "lifecycle" not in proj_columns:
            conn.execute("ALTER TABLE projects ADD COLUMN lifecycle TEXT NOT NULL DEFAULT 'active'")
            logger.info("마이그레이션: projects.lifecycle 컬럼 추가")
        if "wfc_pid" not in proj_columns:
            conn.execute("ALTER TABLE projects ADD COLUMN wfc_pid INTEGER")
            logger.info("마이그레이션: projects.wfc_pid 컬럼 추가")

    @contextmanager
    def connect(self):
        """WAL 모드 커넥션을 반환하는 context manager."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ─── Projects CRUD ───

    def upsert_project(self, name: str, status: str, current_task_id: str = None,
                       last_error_task_id: str = None, last_updated: str = None,
                       config_overrides: dict = None, lifecycle: str = "active",
                       wfc_pid: int = None):
        """프로젝트 상태를 삽입하거나 갱신한다."""
        overrides_json = json.dumps(config_overrides, ensure_ascii=False) if config_overrides else None
        with self.connect() as conn:
            conn.execute("""
                INSERT INTO projects (name, status, current_task_id, last_error_task_id,
                                      last_updated, config_overrides, lifecycle, wfc_pid,
                                      synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    status=excluded.status,
                    current_task_id=excluded.current_task_id,
                    last_error_task_id=excluded.last_error_task_id,
                    last_updated=excluded.last_updated,
                    config_overrides=excluded.config_overrides,
                    lifecycle=excluded.lifecycle,
                    wfc_pid=excluded.wfc_pid,
                    synced_at=excluded.synced_at
            """, (name, status, current_task_id, last_error_task_id,
                  last_updated, overrides_json, lifecycle, wfc_pid, _now()))

    def get_projects(self) -> list[dict]:
        """모든 프로젝트를 조회한다."""
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM projects ORDER BY name").fetchall()
            return [dict(r) for r in rows]

    def get_project(self, name: str) -> dict | None:
        """프로젝트 하나를 조회한다."""
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone()
            return dict(row) if row else None

    # ─── Tasks CRUD ───

    def upsert_task(self, task_data: dict, file_path: str, file_mtime: float):
        """task 메타데이터를 삽입하거나 갱신한다."""
        with self.connect() as conn:
            conn.execute("""
                INSERT INTO tasks (task_id, project, title, description, status,
                                   submitted_via, submitted_at, branch, pr_url,
                                   current_subtask, plan_version, counters,
                                   human_interaction, summary,
                                   pipeline_stage, pipeline_stage_detail,
                                   pipeline_stage_updated_at, failure_reason,
                                   file_path, file_mtime, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project, task_id) DO UPDATE SET
                    title=excluded.title,
                    description=excluded.description,
                    status=excluded.status,
                    submitted_via=excluded.submitted_via,
                    submitted_at=excluded.submitted_at,
                    branch=excluded.branch,
                    pr_url=excluded.pr_url,
                    current_subtask=excluded.current_subtask,
                    plan_version=excluded.plan_version,
                    counters=excluded.counters,
                    human_interaction=excluded.human_interaction,
                    summary=excluded.summary,
                    pipeline_stage=excluded.pipeline_stage,
                    pipeline_stage_detail=excluded.pipeline_stage_detail,
                    pipeline_stage_updated_at=excluded.pipeline_stage_updated_at,
                    failure_reason=excluded.failure_reason,
                    file_path=excluded.file_path,
                    file_mtime=excluded.file_mtime,
                    synced_at=excluded.synced_at
            """, (
                task_data.get("task_id", ""),
                task_data.get("project_name", ""),
                task_data.get("title", ""),
                task_data.get("description", ""),
                task_data.get("status", "submitted"),
                task_data.get("submitted_via", "cli"),
                task_data.get("submitted_at"),
                task_data.get("branch"),
                task_data.get("pr_url"),
                task_data.get("current_subtask"),
                task_data.get("plan_version", 0),
                json.dumps(task_data.get("counters"), ensure_ascii=False) if task_data.get("counters") else None,
                json.dumps(task_data.get("human_interaction"), ensure_ascii=False) if task_data.get("human_interaction") else None,
                task_data.get("summary"),
                task_data.get("pipeline_stage"),
                task_data.get("pipeline_stage_detail"),
                task_data.get("pipeline_stage_updated_at"),
                task_data.get("failure_reason"),
                file_path,
                file_mtime,
                _now(),
            ))

    def get_tasks(self, project: str = None, status: str = None) -> list[dict]:
        """task 목록을 조회한다. project, status로 필터 가능."""
        query = "SELECT * FROM tasks WHERE 1=1"
        params = []
        if project:
            query += " AND project = ?"
            params.append(project)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY submitted_at DESC"
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def get_task(self, project: str, task_id: str) -> dict | None:
        """task 하나를 조회한다."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE project = ? AND task_id = ?",
                (project, task_id),
            ).fetchone()
            return dict(row) if row else None

    def get_task_mtime(self, project: str, task_id: str) -> float | None:
        """task의 마지막 sync된 file_mtime을 반환한다."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT file_mtime FROM tasks WHERE project = ? AND task_id = ?",
                (project, task_id),
            ).fetchone()
            return row["file_mtime"] if row else None

    def delete_task(self, project: str, task_id: str):
        """DB에서 task를 삭제한다 (파일이 삭제된 경우)."""
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM tasks WHERE project = ? AND task_id = ?",
                (project, task_id),
            )

    def get_task_count_by_status(self, project: str = None) -> dict:
        """상태별 task 개수를 집계한다."""
        query = "SELECT status, COUNT(*) as cnt FROM tasks"
        params = []
        if project:
            query += " WHERE project = ?"
            params.append(project)
        query += " GROUP BY status"
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
            return {r["status"]: r["cnt"] for r in rows}

    # ─── Notifications CRUD ───

    def insert_notification(self, project: str, event_type: str, task_id: str,
                            message: str, details: dict = None,
                            created_at: str = None, read: bool = False):
        """알림을 삽입한다."""
        with self.connect() as conn:
            conn.execute("""
                INSERT INTO notifications (project, event_type, task_id, message,
                                           details, created_at, read, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                project, event_type, task_id, message,
                json.dumps(details, ensure_ascii=False) if details else None,
                created_at or _now(),
                1 if read else 0,
                _now(),
            ))

    def get_notifications(self, project: str = None, limit: int = 50,
                          unread_only: bool = False) -> list[dict]:
        """알림 목록을 조회한다."""
        query = "SELECT * FROM notifications WHERE 1=1"
        params = []
        if project:
            query += " AND project = ?"
            params.append(project)
        if unread_only:
            query += " AND read = 0"
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def get_unread_count(self, project: str = None) -> int:
        """미읽은 알림 개수를 반환한다."""
        query = "SELECT COUNT(*) as cnt FROM notifications WHERE read = 0"
        params = []
        if project:
            query += " AND project = ?"
            params.append(project)
        with self.connect() as conn:
            row = conn.execute(query, params).fetchone()
            return row["cnt"]

    def get_max_notification_created_at(self, project: str) -> str | None:
        """프로젝트의 가장 최신 알림 created_at을 반환한다."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT MAX(created_at) as max_at FROM notifications WHERE project = ?",
                (project,),
            ).fetchone()
            return row["max_at"] if row else None

    def mark_notifications_read(self, project: str, up_to_timestamp: str = None):
        """알림을 읽음 처리한다."""
        if up_to_timestamp:
            with self.connect() as conn:
                conn.execute(
                    "UPDATE notifications SET read = 1 WHERE project = ? AND created_at <= ?",
                    (project, up_to_timestamp),
                )
        else:
            with self.connect() as conn:
                conn.execute(
                    "UPDATE notifications SET read = 1 WHERE project = ?",
                    (project,),
                )

    # ─── Chatbot Sessions CRUD ───

    def upsert_session(self, session_id: str, frontend: str, created_at: str,
                       updated_at: str, turn_count: int, file_path: str):
        """챗봇 세션 메타데이터를 삽입하거나 갱신한다."""
        with self.connect() as conn:
            conn.execute("""
                INSERT INTO chatbot_sessions (session_id, frontend, created_at,
                                              updated_at, turn_count, file_path, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    updated_at=excluded.updated_at,
                    turn_count=excluded.turn_count,
                    synced_at=excluded.synced_at
            """, (session_id, frontend, created_at, updated_at, turn_count, file_path, _now()))

    def get_sessions(self, frontend: str = None, limit: int = 50) -> list[dict]:
        """챗봇 세션 목록을 조회한다."""
        query = "SELECT * FROM chatbot_sessions WHERE 1=1"
        params = []
        if frontend:
            query += " AND frontend = ?"
            params.append(frontend)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]


# ═══════════════════════════════════════════════════════════
# 유틸리티
# ═══════════════════════════════════════════════════════════

def _now() -> str:
    """현재 시각을 ISO 8601 형식으로 반환한다."""
    return datetime.now(timezone.utc).isoformat()
