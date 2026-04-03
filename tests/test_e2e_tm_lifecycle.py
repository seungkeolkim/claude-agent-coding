"""
E2E 테스트 — TM full lifecycle.

TM 시작 → .ready 감지 → WFC spawn (dummy) → pipeline 완료 → task 상태 변경
까지의 전체 수명주기를 검증한다.

TM을 subprocess로 실행하고, HubAPI로 task를 submit한 뒤,
WFC(dummy)가 완료될 때까지 대기하여 최종 task 상태를 확인한다.
"""

import json
import os
import signal
import subprocess
import time

import pytest

from conftest import AGENT_HUB_ROOT
from hub_api.core import HubAPI


def _start_tm(dummy=True):
    """TM을 subprocess로 실행한다. Popen 객체를 반환."""
    config_path = os.path.join(AGENT_HUB_ROOT, "config.yaml")
    cmd = [
        "python3",
        os.path.join(AGENT_HUB_ROOT, "scripts", "task_manager.py"),
        "--config", config_path,
        "--polling-interval", "1",
    ]
    if dummy:
        cmd.append("--dummy")

    proc = subprocess.Popen(
        cmd,
        cwd=AGENT_HUB_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        # 프로세스 그룹 생성 (종료 시 자식 포함)
        preexec_fn=os.setsid,
    )
    return proc


def _stop_tm(proc, timeout=15):
    """TM 프로세스를 정상 종료한다."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=timeout)
    except (subprocess.TimeoutExpired, ProcessLookupError, OSError):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait(timeout=5)
        except (ProcessLookupError, OSError, subprocess.TimeoutExpired):
            pass


def _wait_task_status(task_file, target_statuses, timeout_sec=60):
    """task 파일을 폴링하여 target_statuses 중 하나가 되면 반환한다."""
    deadline = time.time() + timeout_sec
    last_status = None
    while time.time() < deadline:
        time.sleep(1)
        try:
            with open(task_file) as f:
                task = json.load(f)
            last_status = task.get("status", "")
            if last_status in target_statuses:
                return last_status, task
        except (json.JSONDecodeError, FileNotFoundError):
            continue
    return last_status, None


@pytest.fixture
def ensure_config():
    """config.yaml 존재를 확인한다."""
    config_path = os.path.join(AGENT_HUB_ROOT, "config.yaml")
    if not os.path.exists(config_path):
        pytest.skip("config.yaml이 없어 TM lifecycle 테스트 스킵")


class TestTMLifecycle:
    """TM 시작 → task 감지 → WFC 완료 full lifecycle."""

    @pytest.mark.timeout(90)
    def test_submit_and_complete(self, test_project, ensure_config):
        """
        task를 submit하면 TM이 감지하고, dummy WFC가 pipeline을 완료한다.
        """
        tm_proc = _start_tm(dummy=True)

        try:
            # TM 초기화 대기
            time.sleep(3)
            assert tm_proc.poll() is None, "TM이 즉시 종료됨"

            # task submit
            api = HubAPI(AGENT_HUB_ROOT)
            result = api.submit(
                test_project["name"],
                title="TM lifecycle test",
                description="TM이 dummy WFC를 spawn하여 pipeline을 완료하는 테스트",
            )

            # 완료 대기
            final_status, final_task = _wait_task_status(
                result.file_path, ("completed", "failed"), timeout_sec=60,
            )

            assert final_task is not None, (
                f"60초 내에 task가 완료되지 않음. 마지막 상태: {final_status}"
            )
            assert final_status in ("completed", "failed")

            # .ready sentinel이 소비되었는지 확인
            ready_path = os.path.join(
                test_project["tasks_dir"], f"{result.task_id}.ready",
            )
            assert not os.path.exists(ready_path), ".ready sentinel이 아직 남아있음"

        finally:
            _stop_tm(tm_proc)

    @pytest.mark.timeout(120)
    def test_sequential_tasks(self, test_project, ensure_config):
        """
        2개 task를 연속 submit하면 순차 처리된다.
        """
        tm_proc = _start_tm(dummy=True)

        try:
            time.sleep(3)

            api = HubAPI(AGENT_HUB_ROOT)
            r1 = api.submit(test_project["name"], "Sequential task 1", "desc 1")
            r2 = api.submit(test_project["name"], "Sequential task 2", "desc 2")

            # 첫 번째 task 완료 대기
            s1, t1 = _wait_task_status(
                r1.file_path, ("completed", "failed"), timeout_sec=60,
            )
            assert t1 is not None, f"task 1 미완료: {s1}"

            # 두 번째 task 완료 대기
            s2, t2 = _wait_task_status(
                r2.file_path, ("completed", "failed"), timeout_sec=60,
            )
            assert t2 is not None, f"task 2 미완료: {s2}"

        finally:
            _stop_tm(tm_proc)

    @pytest.mark.timeout(20)
    def test_tm_sigterm_shutdown(self, test_project, ensure_config):
        """TM이 SIGTERM에 정상 반응하여 종료한다."""
        tm_proc = _start_tm(dummy=True)

        try:
            time.sleep(3)
            assert tm_proc.poll() is None, "TM이 즉시 종료됨"

            # SIGTERM 전송
            os.killpg(os.getpgid(tm_proc.pid), signal.SIGTERM)

            # 종료 대기
            try:
                tm_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pytest.fail("TM이 SIGTERM 후 10초 내에 종료되지 않음")

            # 정상 종료 확인 (exit code 0)
            assert tm_proc.returncode == 0, (
                f"TM이 비정상 종료됨 (exit code: {tm_proc.returncode})"
            )

        finally:
            _stop_tm(tm_proc)

    @pytest.mark.timeout(90)
    def test_notification_on_completion(self, test_project, ensure_config):
        """task 완료 시 알림이 생성된다."""
        tm_proc = _start_tm(dummy=True)

        try:
            time.sleep(3)

            api = HubAPI(AGENT_HUB_ROOT)
            result = api.submit(
                test_project["name"],
                title="Notification test task",
                description="완료 시 알림 생성 검증",
            )

            # 완료 대기
            final_status, _ = _wait_task_status(
                result.file_path, ("completed", "failed"), timeout_sec=60,
            )
            assert final_status in ("completed", "failed")

            # 알림 확인
            from notification import get_notifications
            notifications = get_notifications(test_project["dir"])

            # 완료 또는 실패 알림이 하나 이상 있어야 함
            relevant = [
                n for n in notifications
                if n["event_type"] in ("task_completed", "task_failed")
                and n["task_id"] == result.task_id
            ]
            assert len(relevant) >= 1, (
                f"task {result.task_id}에 대한 완료/실패 알림이 없음. "
                f"전체 알림: {[n['event_type'] for n in notifications]}"
            )

        finally:
            _stop_tm(tm_proc)
