"""Priority Queue 통합 테스트.

hub_api (submit/cancel) + task_manager.find_next_task()가 priority queue
파일을 통해 올바른 순서로 task를 처리하는지 검증한다.

핸드오프 문서 `docs_for_claude/014-handoff-priority-queue.md`의 테스트
시나리오 1~8을 커버한다.
"""

import os
import threading

import pytest

from hub_api.core import HubAPI
from hub_api import queue_helpers


# ═══════════════════════════════════════════════════════════
# 헬퍼: find_next_task를 직접 호출하기 위한 최소 TM 인스턴스
# ═══════════════════════════════════════════════════════════


class _FakeTMForQueue:
    """find_next_task 로직 테스트용 경량 인스턴스.

    실제 TaskManager를 import하면 전역 상태/시그널 등록 때문에 무겁다.
    find_next_task 로직만 분리 테스트하기 위해 필요한 속성만 재현한다.
    """

    def __init__(self, projects_dir):
        self._projects_dir = projects_dir

    # TaskManager의 메서드를 bound 방식으로 재사용
    find_next_task = None  # 아래에서 주입
    _load_task_json = None


def _build_fake_tm(agent_hub_root):
    """TaskManager에서 find_next_task/_load_task_json 메서드만 빌려온다."""
    from task_manager import TaskManager

    tm = _FakeTMForQueue(os.path.join(agent_hub_root, "projects"))
    tm.find_next_task = TaskManager.find_next_task.__get__(tm)
    tm._load_task_json = TaskManager._load_task_json.__get__(tm)
    return tm


# ═══════════════════════════════════════════════════════════
# 시나리오 1~3: priority 순서 확인
# ═══════════════════════════════════════════════════════════


class TestPriorityOrder:
    """submit priority에 따른 실행 순서 검증."""

    def test_three_defaults_execute_in_id_order(self, test_project, agent_hub_root):
        """시나리오 1: submit 3개 (default) → id 순서대로 실행."""
        api = HubAPI(agent_hub_root)
        r1 = api.submit(test_project["name"], "t1", "d1")
        r2 = api.submit(test_project["name"], "t2", "d2")
        r3 = api.submit(test_project["name"], "t3", "d3")

        assert queue_helpers.read_queue(test_project["dir"], "default") == [
            r1.task_id, r2.task_id, r3.task_id,
        ]

        tm = _build_fake_tm(agent_hub_root)
        assert tm.find_next_task(test_project["name"]) == ("default", r1.task_id)

    def test_critical_jumps_ahead_of_default(self, test_project, agent_hub_root):
        """시나리오 2: default 2개 먼저 + critical 1개 → critical 먼저 실행."""
        api = HubAPI(agent_hub_root)
        d1 = api.submit(test_project["name"], "d1", "")
        d2 = api.submit(test_project["name"], "d2", "")
        c1 = api.submit(test_project["name"], "c1", "", priority="critical")

        tm = _build_fake_tm(agent_hub_root)
        # critical이 가장 앞
        assert tm.find_next_task(test_project["name"]) == ("critical", c1.task_id)

    def test_critical_urgent_default_order(self, test_project, agent_hub_root):
        """시나리오 3: critical → urgent → default 순으로 소비된다."""
        api = HubAPI(agent_hub_root)
        c1 = api.submit(test_project["name"], "critical", "", priority="critical")
        u1 = api.submit(test_project["name"], "urgent", "", priority="urgent")
        d1 = api.submit(test_project["name"], "default", "")

        tm = _build_fake_tm(agent_hub_root)
        project_dir = test_project["dir"]

        # 1. critical
        priority, tid = tm.find_next_task(test_project["name"])
        assert (priority, tid) == ("critical", c1.task_id)
        queue_helpers.pop_task(project_dir, priority, tid)

        # 2. urgent
        priority, tid = tm.find_next_task(test_project["name"])
        assert (priority, tid) == ("urgent", u1.task_id)
        queue_helpers.pop_task(project_dir, priority, tid)

        # 3. default
        priority, tid = tm.find_next_task(test_project["name"])
        assert (priority, tid) == ("default", d1.task_id)
        queue_helpers.pop_task(project_dir, priority, tid)

        # 4. empty
        assert tm.find_next_task(test_project["name"]) is None


# ═══════════════════════════════════════════════════════════
# 시나리오 4~5: cancel이 queue에서 제거
# ═══════════════════════════════════════════════════════════


class TestCancelRemovesFromQueue:

    def test_cancel_submitted_removes_from_queue(self, test_project, agent_hub_root):
        """시나리오 5: submit 직후 cancel → queue에 추가 후 바로 제거."""
        api = HubAPI(agent_hub_root)
        result = api.submit(test_project["name"], "cancel-me", "")

        assert queue_helpers.read_queue(test_project["dir"], "default") == [result.task_id]

        api.cancel(test_project["name"], result.task_id)

        assert queue_helpers.read_queue(test_project["dir"], "default") == []

    def test_cancel_urgent_removes_from_urgent_queue(self, test_project, agent_hub_root):
        """urgent priority로 submit한 것도 cancel 시 urgent queue에서 제거된다."""
        api = HubAPI(agent_hub_root)
        result = api.submit(test_project["name"], "urgent-cancel", "", priority="urgent")

        assert queue_helpers.read_queue(test_project["dir"], "urgent") == [result.task_id]

        api.cancel(test_project["name"], result.task_id)

        assert queue_helpers.read_queue(test_project["dir"], "urgent") == []


# ═══════════════════════════════════════════════════════════
# 시나리오 6: 동시 submit race
# ═══════════════════════════════════════════════════════════


class TestConcurrentSubmit:

    def test_concurrent_queue_appends_no_loss(self, test_project, agent_hub_root):
        """시나리오 6: 여러 스레드가 동시에 queue에 append해도 전부 반영된다 (flock).

        _next_task_id의 race는 priority queue 범위 밖이므로, 여기서는
        queue append 자체의 동시성만 검증한다.
        """
        project_dir = test_project["dir"]
        queue_helpers.ensure_queue_files(project_dir)

        n_threads = 10
        ids_per_thread = 5

        def worker(tid):
            for i in range(ids_per_thread):
                queue_helpers.append_to_queue(
                    project_dir, "default", f"t{tid}-i{i}"
                )

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        queue = queue_helpers.read_queue(project_dir, "default")
        assert len(queue) == n_threads * ids_per_thread
        assert len(set(queue)) == len(queue)  # 중복 없음


# ═══════════════════════════════════════════════════════════
# 시나리오 7: cancelled task를 queue에서 만나면 skip
# ═══════════════════════════════════════════════════════════


class TestCancelledSkip:

    def test_find_next_task_skips_cancelled_and_cleans(
        self, test_project, agent_hub_root
    ):
        """시나리오 7: queue에 남아있는 cancelled task는 skip + 정리."""
        api = HubAPI(agent_hub_root)
        r1 = api.submit(test_project["name"], "t1", "")
        r2 = api.submit(test_project["name"], "t2", "")

        # r1을 cancelled 상태로 직접 바꿔놓음 (queue에는 그대로 남겨둠)
        import json
        task_files = list(os.listdir(test_project["tasks_dir"]))
        r1_file = [f for f in task_files if f.startswith(r1.task_id)][0]
        r1_path = os.path.join(test_project["tasks_dir"], r1_file)
        with open(r1_path) as f:
            data = json.load(f)
        data["status"] = "cancelled"
        with open(r1_path, "w") as f:
            json.dump(data, f)
        # queue에는 r1이 여전히 남아 있어야 함 (인위적으로 inconsistent 상태)
        queue_helpers.append_to_queue(test_project["dir"], "default", r1.task_id)

        tm = _build_fake_tm(agent_hub_root)
        next_task = tm.find_next_task(test_project["name"])

        # r1은 skip되고 r2가 반환된다
        assert next_task == ("default", r2.task_id)
        # r1은 queue에서 제거되었다
        assert r1.task_id not in queue_helpers.read_queue(
            test_project["dir"], "default"
        )


# ═══════════════════════════════════════════════════════════
# 시나리오 8: .ready 레거시 이주
# ═══════════════════════════════════════════════════════════


class TestReadyMigration:

    def test_legacy_ready_migrated_to_default_queue(self, test_project, agent_hub_root):
        """시나리오 8: 기존 .ready 파일 → default queue 이주 + .ready 삭제."""
        tasks_dir = test_project["tasks_dir"]

        # 레거시 상태 재현: task JSON 있고 .ready도 있음
        from tests.conftest import _create_task_json
        _create_task_json(tasks_dir, "00001", title="legacy", project_name=test_project["name"])
        ready_path = os.path.join(tasks_dir, "00001.ready")
        with open(ready_path, "w") as f:
            f.write("")

        # queue 파일은 아직 없음 (구 버전 프로젝트 가정)
        default_queue = os.path.join(test_project["dir"], "task_queue_default.json")
        if os.path.exists(default_queue):
            os.unlink(default_queue)

        # 마이그레이션 실행
        migrated = queue_helpers.migrate_ready_sentinels(test_project["dir"])

        assert migrated == ["00001"]
        assert queue_helpers.read_queue(test_project["dir"], "default") == ["00001"]
        assert not os.path.exists(ready_path)
