"""
웹 DB + Sync 레이어 테스트.

DB 스키마 생성, CRUD, FileSyncer의 파일→DB 동기화를 검증한다.
"""

import json
import os
import tempfile
import time

import pytest

# 프로젝트 루트를 sys.path에 추가
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.web.db import Database
from scripts.web.syncer import FileSyncer


# ═══════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════

@pytest.fixture
def tmp_dir():
    """테스트용 임시 디렉토리."""
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
def db(tmp_dir):
    """테스트용 Database 인스턴스."""
    db_path = os.path.join(tmp_dir, "data", "test.db")
    return Database(db_path)


@pytest.fixture
def projects_dir(tmp_dir):
    """테스트용 projects/ 디렉토리."""
    d = os.path.join(tmp_dir, "projects")
    os.makedirs(d)
    return d


@pytest.fixture
def session_dir(tmp_dir):
    """테스트용 session_history/ 디렉토리."""
    d = os.path.join(tmp_dir, "session_history")
    os.makedirs(d)
    return d


@pytest.fixture
def syncer(db, projects_dir, session_dir):
    """테스트용 FileSyncer 인스턴스."""
    return FileSyncer(db, projects_dir, session_dir)


def _make_project(projects_dir, name, state=None):
    """테스트용 프로젝트 디렉토리와 state 파일을 생성한다."""
    project_dir = os.path.join(projects_dir, name)
    os.makedirs(os.path.join(project_dir, "tasks"), exist_ok=True)
    if state:
        with open(os.path.join(project_dir, "project_state.json"), "w") as f:
            json.dump(state, f)
    return project_dir


def _make_task(project_dir, task_id, slug="test", **overrides):
    """테스트용 task JSON 파일을 생성한다."""
    task_data = {
        "task_id": task_id,
        "project_name": os.path.basename(project_dir),
        "title": f"Task {task_id}",
        "description": "테스트 task",
        "status": "submitted",
        "submitted_via": "cli",
        "submitted_at": "2026-04-06T10:00:00Z",
        "branch": None,
        "pr_url": None,
        "current_subtask": None,
        "plan_version": 0,
        "counters": {"total_agent_invocations": 0},
        "human_interaction": None,
        "summary": None,
    }
    task_data.update(overrides)
    file_path = os.path.join(project_dir, "tasks", f"{task_id}-{slug}.json")
    with open(file_path, "w") as f:
        json.dump(task_data, f)
    return file_path


def _make_notifications(project_dir, notifications):
    """테스트용 notifications.json을 생성한다."""
    noti_path = os.path.join(project_dir, "notifications.json")
    with open(noti_path, "w") as f:
        json.dump(notifications, f)
    return noti_path


# ═══════════════════════════════════════════════════════════
# Database 단위 테스트
# ═══════════════════════════════════════════════════════════

class TestDatabase:
    """Database CRUD 테스트."""

    def test_schema_creation(self, db):
        """스키마가 정상 생성되는지 확인."""
        with db.connect() as conn:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            table_names = [t["name"] for t in tables]
        assert "projects" in table_names
        assert "tasks" in table_names
        assert "notifications" in table_names
        assert "chatbot_sessions" in table_names
        assert "schema_version" in table_names

    def test_schema_version(self, db):
        """스키마 버전이 기록되는지 확인."""
        with db.connect() as conn:
            row = conn.execute("SELECT version FROM schema_version").fetchone()
        assert row["version"] == 1

    def test_double_init_is_idempotent(self, tmp_dir):
        """Database를 두 번 초기화해도 문제없는지 확인."""
        db_path = os.path.join(tmp_dir, "data", "test2.db")
        db1 = Database(db_path)
        db2 = Database(db_path)
        with db2.connect() as conn:
            rows = conn.execute("SELECT COUNT(*) as cnt FROM schema_version").fetchone()
        assert rows["cnt"] == 1

    # ─── Projects ───

    def test_upsert_and_get_project(self, db):
        """프로젝트 upsert 후 조회."""
        db.upsert_project("my-app", status="running", current_task_id="00001")
        p = db.get_project("my-app")
        assert p["name"] == "my-app"
        assert p["status"] == "running"
        assert p["current_task_id"] == "00001"

    def test_upsert_project_update(self, db):
        """프로젝트 upsert로 갱신."""
        db.upsert_project("my-app", status="idle")
        db.upsert_project("my-app", status="running", current_task_id="00002")
        p = db.get_project("my-app")
        assert p["status"] == "running"
        assert p["current_task_id"] == "00002"

    def test_get_projects(self, db):
        """프로젝트 목록 조회."""
        db.upsert_project("aaa", status="idle")
        db.upsert_project("bbb", status="running")
        projects = db.get_projects()
        assert len(projects) == 2
        assert projects[0]["name"] == "aaa"

    def test_get_project_not_found(self, db):
        """존재하지 않는 프로젝트 조회 시 None."""
        assert db.get_project("nonexistent") is None

    # ─── Tasks ───

    def test_upsert_and_get_task(self, db):
        """task upsert 후 조회."""
        task_data = {
            "task_id": "00001",
            "project_name": "my-app",
            "title": "README 업데이트",
            "status": "submitted",
        }
        db.upsert_task(task_data, "/path/to/00001.json", 1234.0)
        t = db.get_task("my-app", "00001")
        assert t["title"] == "README 업데이트"
        assert t["status"] == "submitted"
        assert t["file_mtime"] == 1234.0

    def test_upsert_task_update(self, db):
        """task upsert로 상태 갱신."""
        task_data = {"task_id": "00001", "project_name": "my-app", "title": "Test", "status": "submitted"}
        db.upsert_task(task_data, "/path", 100.0)
        task_data["status"] = "completed"
        db.upsert_task(task_data, "/path", 200.0)
        t = db.get_task("my-app", "00001")
        assert t["status"] == "completed"

    def test_get_tasks_filter_by_project(self, db):
        """프로젝트별 task 필터."""
        db.upsert_task({"task_id": "00001", "project_name": "app-a", "title": "A1", "status": "submitted", "submitted_at": "2026-04-01"}, "/a", 1.0)
        db.upsert_task({"task_id": "00001", "project_name": "app-b", "title": "B1", "status": "submitted", "submitted_at": "2026-04-02"}, "/b", 1.0)
        tasks = db.get_tasks(project="app-a")
        assert len(tasks) == 1
        assert tasks[0]["project"] == "app-a"

    def test_get_tasks_filter_by_status(self, db):
        """상태별 task 필터."""
        db.upsert_task({"task_id": "00001", "project_name": "app", "title": "T1", "status": "submitted", "submitted_at": "2026-04-01"}, "/a", 1.0)
        db.upsert_task({"task_id": "00002", "project_name": "app", "title": "T2", "status": "completed", "submitted_at": "2026-04-02"}, "/b", 1.0)
        tasks = db.get_tasks(status="completed")
        assert len(tasks) == 1
        assert tasks[0]["task_id"] == "00002"

    def test_delete_task(self, db):
        """task 삭제."""
        db.upsert_task({"task_id": "00001", "project_name": "app", "title": "T", "status": "submitted"}, "/a", 1.0)
        db.delete_task("app", "00001")
        assert db.get_task("app", "00001") is None

    def test_task_count_by_status(self, db):
        """상태별 task 개수 집계."""
        db.upsert_task({"task_id": "00001", "project_name": "app", "title": "T1", "status": "submitted"}, "/a", 1.0)
        db.upsert_task({"task_id": "00002", "project_name": "app", "title": "T2", "status": "submitted"}, "/b", 1.0)
        db.upsert_task({"task_id": "00003", "project_name": "app", "title": "T3", "status": "completed"}, "/c", 1.0)
        counts = db.get_task_count_by_status()
        assert counts["submitted"] == 2
        assert counts["completed"] == 1

    # ─── Notifications ───

    def test_insert_and_get_notifications(self, db):
        """알림 삽입 후 조회."""
        db.insert_notification("my-app", "task_completed", "00001", "완료!", created_at="2026-04-06T10:00:00Z")
        db.insert_notification("my-app", "pr_created", "00001", "PR 생성", created_at="2026-04-06T10:01:00Z")
        notis = db.get_notifications("my-app")
        assert len(notis) == 2
        # 최신순
        assert notis[0]["event_type"] == "pr_created"

    def test_get_notifications_unread_only(self, db):
        """미읽은 알림만 조회."""
        db.insert_notification("app", "e1", "001", "msg1", created_at="2026-04-06T10:00:00Z", read=True)
        db.insert_notification("app", "e2", "002", "msg2", created_at="2026-04-06T10:01:00Z", read=False)
        notis = db.get_notifications("app", unread_only=True)
        assert len(notis) == 1
        assert notis[0]["task_id"] == "002"

    def test_unread_count(self, db):
        """미읽은 알림 개수."""
        db.insert_notification("app", "e1", "001", "m1", created_at="t1", read=False)
        db.insert_notification("app", "e2", "002", "m2", created_at="t2", read=False)
        db.insert_notification("app", "e3", "003", "m3", created_at="t3", read=True)
        assert db.get_unread_count("app") == 2

    def test_mark_notifications_read(self, db):
        """알림 읽음 처리."""
        db.insert_notification("app", "e1", "001", "m1", created_at="2026-04-06T10:00:00Z")
        db.insert_notification("app", "e2", "002", "m2", created_at="2026-04-06T10:01:00Z")
        db.mark_notifications_read("app")
        assert db.get_unread_count("app") == 0

    def test_mark_notifications_read_with_timestamp(self, db):
        """특정 시점까지만 읽음 처리."""
        db.insert_notification("app", "e1", "001", "m1", created_at="2026-04-06T10:00:00Z")
        db.insert_notification("app", "e2", "002", "m2", created_at="2026-04-06T10:01:00Z")
        db.mark_notifications_read("app", up_to_timestamp="2026-04-06T10:00:00Z")
        assert db.get_unread_count("app") == 1

    def test_max_notification_created_at(self, db):
        """가장 최신 알림의 created_at."""
        db.insert_notification("app", "e1", "001", "m1", created_at="2026-04-06T10:00:00Z")
        db.insert_notification("app", "e2", "002", "m2", created_at="2026-04-06T10:01:00Z")
        assert db.get_max_notification_created_at("app") == "2026-04-06T10:01:00Z"

    def test_max_notification_created_at_empty(self, db):
        """알림이 없을 때 None."""
        assert db.get_max_notification_created_at("app") is None

    # ─── Sessions ───

    def test_upsert_and_get_sessions(self, db):
        """세션 upsert 후 조회."""
        db.upsert_session("sess_001", "chatbot", "2026-04-06T10:00:00Z",
                          "2026-04-06T10:30:00Z", 5, "/path/sess_001.json")
        sessions = db.get_sessions()
        assert len(sessions) == 1
        assert sessions[0]["session_id"] == "sess_001"
        assert sessions[0]["turn_count"] == 5

    def test_upsert_session_update(self, db):
        """세션 turn_count 갱신."""
        db.upsert_session("sess_001", "chatbot", "2026-04-06T10:00:00Z",
                          "2026-04-06T10:30:00Z", 5, "/path")
        db.upsert_session("sess_001", "chatbot", "2026-04-06T10:00:00Z",
                          "2026-04-06T11:00:00Z", 10, "/path")
        sessions = db.get_sessions()
        assert sessions[0]["turn_count"] == 10

    def test_get_sessions_filter_by_frontend(self, db):
        """frontend별 세션 필터."""
        db.upsert_session("s1", "chatbot", "t1", "t2", 1, "/a")
        db.upsert_session("s2", "web", "t1", "t2", 1, "/b")
        assert len(db.get_sessions(frontend="web")) == 1


# ═══════════════════════════════════════════════════════════
# FileSyncer 테스트
# ═══════════════════════════════════════════════════════════

class TestFileSyncer:
    """FileSyncer 동기화 테스트."""

    def test_sync_project_state(self, syncer, db, projects_dir):
        """project_state.json → DB sync."""
        _make_project(projects_dir, "my-app", state={
            "project_name": "my-app",
            "status": "running",
            "current_task_id": "00001",
            "last_updated": "2026-04-06T10:00:00Z",
        })
        syncer.sync_all()
        p = db.get_project("my-app")
        assert p["status"] == "running"
        assert p["current_task_id"] == "00001"

    def test_sync_project_without_state(self, syncer, db, projects_dir):
        """state 파일 없는 프로젝트는 idle로 등록."""
        _make_project(projects_dir, "empty-project")
        syncer.sync_all()
        p = db.get_project("empty-project")
        assert p["status"] == "idle"

    def test_sync_tasks(self, syncer, db, projects_dir):
        """task JSON → DB sync."""
        pd = _make_project(projects_dir, "my-app", state={"status": "idle"})
        _make_task(pd, "00001", title="첫 번째 task")
        _make_task(pd, "00002", title="두 번째 task", status="completed")
        syncer.sync_all()
        tasks = db.get_tasks(project="my-app")
        assert len(tasks) == 2

    def test_sync_task_update(self, syncer, db, projects_dir):
        """task 파일 수정 → DB 갱신."""
        pd = _make_project(projects_dir, "my-app", state={"status": "idle"})
        task_path = _make_task(pd, "00001", status="submitted")
        syncer.sync_all()

        # 파일 수정 (mtime 변경을 위해 잠시 대기)
        time.sleep(0.05)
        with open(task_path) as f:
            data = json.load(f)
        data["status"] = "completed"
        with open(task_path, "w") as f:
            json.dump(data, f)

        syncer.sync_all()
        t = db.get_task("my-app", "00001")
        assert t["status"] == "completed"

    def test_sync_task_deleted(self, syncer, db, projects_dir):
        """task 파일 삭제 → DB에서도 제거."""
        pd = _make_project(projects_dir, "my-app", state={"status": "idle"})
        task_path = _make_task(pd, "00001")
        syncer.sync_all()
        assert db.get_task("my-app", "00001") is not None

        os.remove(task_path)
        syncer.sync_all()
        assert db.get_task("my-app", "00001") is None

    def test_sync_notifications(self, syncer, db, projects_dir):
        """notifications.json → DB sync."""
        pd = _make_project(projects_dir, "my-app", state={"status": "idle"})
        _make_notifications(pd, [
            {"event_type": "task_completed", "task_id": "00001",
             "message": "완료", "created_at": "2026-04-06T10:00:00Z", "read": False},
            {"event_type": "pr_created", "task_id": "00001",
             "message": "PR", "created_at": "2026-04-06T10:01:00Z", "read": True},
        ])
        syncer.sync_all()
        notis = db.get_notifications("my-app")
        assert len(notis) == 2

    def test_sync_notifications_incremental(self, syncer, db, projects_dir):
        """알림 추가 시 신규 항목만 삽입."""
        pd = _make_project(projects_dir, "my-app", state={"status": "idle"})
        _make_notifications(pd, [
            {"event_type": "e1", "task_id": "001", "message": "m1",
             "created_at": "2026-04-06T10:00:00Z", "read": False},
        ])
        syncer.sync_all()
        assert len(db.get_notifications("my-app")) == 1

        # 알림 추가
        time.sleep(0.05)
        _make_notifications(pd, [
            {"event_type": "e1", "task_id": "001", "message": "m1",
             "created_at": "2026-04-06T10:00:00Z", "read": False},
            {"event_type": "e2", "task_id": "002", "message": "m2",
             "created_at": "2026-04-06T10:01:00Z", "read": False},
        ])
        syncer.sync_all()
        assert len(db.get_notifications("my-app")) == 2

    def test_sync_sessions(self, syncer, db, session_dir):
        """세션 파일 → DB sync."""
        chatbot_dir = os.path.join(session_dir, "chatbot")
        os.makedirs(chatbot_dir)
        session_data = {
            "session_id": "20260406_100000_abcd",
            "frontend": "chatbot",
            "created_at": "2026-04-06T10:00:00Z",
            "updated_at": "2026-04-06T10:30:00Z",
            "turn_count": 5,
            "history": [],
        }
        with open(os.path.join(chatbot_dir, "20260406_100000_abcd.json"), "w") as f:
            json.dump(session_data, f)

        syncer.sync_all()
        sessions = db.get_sessions()
        assert len(sessions) == 1
        assert sessions[0]["session_id"] == "20260406_100000_abcd"

    def test_delta_sync_skips_unchanged(self, syncer, db, projects_dir):
        """mtime 동일하면 skip (성능 확인)."""
        pd = _make_project(projects_dir, "my-app", state={"status": "idle"})
        _make_task(pd, "00001")
        syncer.sync_all()

        # 두 번째 sync에서는 변경 없음 → on_change 호출 안 됨
        changes = []
        syncer.on_change = lambda evt: changes.append(evt)
        syncer.sync_all()
        # task_updated 이벤트가 없어야 함
        task_events = [c for c in changes if c["type"] == "task_updated"]
        assert len(task_events) == 0

    def test_on_change_callback(self, syncer, db, projects_dir):
        """변경 시 on_change 콜백이 호출되는지 확인."""
        changes = []
        syncer.on_change = lambda evt: changes.append(evt)

        pd = _make_project(projects_dir, "my-app", state={"status": "running"})
        _make_task(pd, "00001")
        syncer.sync_all()

        event_types = [c["type"] for c in changes]
        assert "project_updated" in event_types
        assert "task_updated" in event_types

    def test_background_sync_starts_and_stops(self, syncer):
        """백그라운드 sync 시작/중단."""
        syncer.start_background_sync(interval_seconds=0.1)
        assert syncer._thread is not None
        assert syncer._thread.is_alive()

        syncer.stop_background_sync()
        assert not syncer._thread or not syncer._thread.is_alive()

    def test_sync_multiple_projects(self, syncer, db, projects_dir):
        """여러 프로젝트 동시 sync."""
        _make_project(projects_dir, "app-a", state={"status": "idle"})
        _make_project(projects_dir, "app-b", state={"status": "running", "current_task_id": "00001"})
        pd_b = os.path.join(projects_dir, "app-b")
        _make_task(pd_b, "00001")

        syncer.sync_all()
        assert len(db.get_projects()) == 2
        assert len(db.get_tasks(project="app-b")) == 1
        assert len(db.get_tasks(project="app-a")) == 0

    def test_corrupt_json_skipped(self, syncer, db, projects_dir):
        """깨진 JSON 파일은 건너뛴다."""
        pd = _make_project(projects_dir, "my-app", state={"status": "idle"})
        bad_path = os.path.join(pd, "tasks", "00001-bad.json")
        with open(bad_path, "w") as f:
            f.write("{invalid json")
        syncer.sync_all()
        # 에러 없이 프로젝트는 등록됨
        assert db.get_project("my-app") is not None
        assert len(db.get_tasks(project="my-app")) == 0
