"""queue_helpers 단위 테스트.

Priority Queue 파일 조작 헬퍼의 기본 동작을 검증한다.
"""

import json
import os
import threading

import pytest

from hub_api import queue_helpers as qh


# ═══════════════════════════════════════════════════════════
# ensure_queue_files
# ═══════════════════════════════════════════════════════════


def test_ensure_queue_files_creates_three_empty_files(tmp_path):
    """ensure_queue_files 호출 시 3개 priority 파일이 빈 배열로 생성된다."""
    qh.ensure_queue_files(tmp_path)

    for priority in qh.PRIORITIES:
        path = tmp_path / f"task_queue_{priority}.json"
        assert path.exists()
        assert json.loads(path.read_text()) == []


def test_ensure_queue_files_idempotent(tmp_path):
    """이미 파일이 있으면 덮어쓰지 않는다 (기존 내용 유지)."""
    qh.ensure_queue_files(tmp_path)
    qh.append_to_queue(tmp_path, "default", "00001")

    # 두 번째 호출은 기존 내용을 보존해야 함
    qh.ensure_queue_files(tmp_path)

    assert qh.read_queue(tmp_path, "default") == ["00001"]


# ═══════════════════════════════════════════════════════════
# append_to_queue / remove_from_queue
# ═══════════════════════════════════════════════════════════


def test_append_to_queue_appends_in_order(tmp_path):
    qh.append_to_queue(tmp_path, "default", "00001")
    qh.append_to_queue(tmp_path, "default", "00002")
    qh.append_to_queue(tmp_path, "default", "00003")

    assert qh.read_queue(tmp_path, "default") == ["00001", "00002", "00003"]


def test_append_to_queue_dedup(tmp_path):
    """같은 id를 두 번 append해도 한 번만 들어간다."""
    qh.append_to_queue(tmp_path, "default", "00001")
    qh.append_to_queue(tmp_path, "default", "00001")

    assert qh.read_queue(tmp_path, "default") == ["00001"]


def test_append_to_queue_invalid_priority(tmp_path):
    with pytest.raises(ValueError):
        qh.append_to_queue(tmp_path, "bogus", "00001")


def test_remove_from_queue_returns_true_if_present(tmp_path):
    qh.append_to_queue(tmp_path, "default", "00001")
    qh.append_to_queue(tmp_path, "default", "00002")

    assert qh.remove_from_queue(tmp_path, "default", "00001") is True
    assert qh.read_queue(tmp_path, "default") == ["00002"]


def test_remove_from_queue_returns_false_if_absent(tmp_path):
    qh.ensure_queue_files(tmp_path)
    assert qh.remove_from_queue(tmp_path, "default", "99999") is False


def test_remove_task_from_all_queues(tmp_path):
    qh.append_to_queue(tmp_path, "urgent", "00005")
    result = qh.remove_task_from_all_queues(tmp_path, "00005")
    assert result == "urgent"
    assert qh.read_queue(tmp_path, "urgent") == []

    # 없는 id
    assert qh.remove_task_from_all_queues(tmp_path, "99999") is None


# ═══════════════════════════════════════════════════════════
# peek_next_task / pop_task
# ═══════════════════════════════════════════════════════════


def test_peek_next_task_empty(tmp_path):
    assert qh.peek_next_task(tmp_path) is None


def test_peek_next_task_priority_order(tmp_path):
    """critical > urgent > default 순으로 꺼낸다."""
    qh.append_to_queue(tmp_path, "default", "00001")
    qh.append_to_queue(tmp_path, "urgent", "00002")
    qh.append_to_queue(tmp_path, "critical", "00003")

    assert qh.peek_next_task(tmp_path) == ("critical", "00003")


def test_peek_next_task_within_priority_is_fifo(tmp_path):
    """같은 priority 내에서는 append 순서대로 꺼낸다."""
    qh.append_to_queue(tmp_path, "default", "00005")
    qh.append_to_queue(tmp_path, "default", "00003")
    qh.append_to_queue(tmp_path, "default", "00007")

    # 첫 번째는 가장 먼저 append된 00005
    assert qh.peek_next_task(tmp_path) == ("default", "00005")


def test_pop_task_removes_id(tmp_path):
    qh.append_to_queue(tmp_path, "critical", "00001")
    qh.append_to_queue(tmp_path, "critical", "00002")

    assert qh.pop_task(tmp_path, "critical", "00001") is True
    assert qh.peek_next_task(tmp_path) == ("critical", "00002")


def test_pop_then_peek_advances_priority(tmp_path):
    """critical을 전부 pop하면 urgent가 다음, urgent도 다 비면 default."""
    qh.append_to_queue(tmp_path, "critical", "00001")
    qh.append_to_queue(tmp_path, "urgent", "00002")
    qh.append_to_queue(tmp_path, "default", "00003")

    assert qh.peek_next_task(tmp_path) == ("critical", "00001")
    qh.pop_task(tmp_path, "critical", "00001")
    assert qh.peek_next_task(tmp_path) == ("urgent", "00002")
    qh.pop_task(tmp_path, "urgent", "00002")
    assert qh.peek_next_task(tmp_path) == ("default", "00003")
    qh.pop_task(tmp_path, "default", "00003")
    assert qh.peek_next_task(tmp_path) is None


# ═══════════════════════════════════════════════════════════
# 동시성 (flock)
# ═══════════════════════════════════════════════════════════


def test_concurrent_append_no_loss(tmp_path):
    """여러 스레드가 동시에 append해도 전부 반영된다."""
    qh.ensure_queue_files(tmp_path)
    n_threads = 10
    ids_per_thread = 5

    def worker(tid):
        for i in range(ids_per_thread):
            qh.append_to_queue(tmp_path, "default", f"t{tid}-i{i}")

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    result = qh.read_queue(tmp_path, "default")
    assert len(result) == n_threads * ids_per_thread
    assert len(set(result)) == len(result)


# ═══════════════════════════════════════════════════════════
# migrate_ready_sentinels
# ═══════════════════════════════════════════════════════════


def test_migrate_ready_sentinels_moves_to_default(tmp_path):
    """tasks/{id}.ready 파일을 default queue로 이주시키고 .ready는 삭제한다."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()

    # task JSON 2개 + .ready sentinel 2개
    for tid in ("00001", "00002"):
        (tasks_dir / f"{tid}-test.json").write_text("{}")
        (tasks_dir / f"{tid}.ready").write_text("")

    migrated = qh.migrate_ready_sentinels(tmp_path)

    assert sorted(migrated) == ["00001", "00002"]
    assert qh.read_queue(tmp_path, "default") == ["00001", "00002"]

    # .ready 파일은 제거됨
    assert not (tasks_dir / "00001.ready").exists()
    assert not (tasks_dir / "00002.ready").exists()


def test_migrate_ready_sentinels_removes_orphan(tmp_path):
    """task JSON 없는 orphan .ready는 queue에 추가하지 않고 파일만 지운다."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "00099.ready").write_text("")

    migrated = qh.migrate_ready_sentinels(tmp_path)

    assert migrated == []
    assert qh.read_queue(tmp_path, "default") == []
    assert not (tasks_dir / "00099.ready").exists()


def test_migrate_ready_sentinels_no_tasks_dir(tmp_path):
    """tasks/ 디렉토리가 없으면 빈 리스트 반환."""
    assert qh.migrate_ready_sentinels(tmp_path) == []
