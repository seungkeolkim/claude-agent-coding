"""
WFC pipeline 제어 흐름 통합 테스트.

run_subtask_pipeline을 직접 호출하되, run_agent를 mock하여
reviewer reject / reporter fail / replan 등의 시나리오를 검증한다.
"""

import json
import os
from unittest.mock import patch, call

import pytest

from conftest import _create_task_json
from workflow_controller import (
    load_json,
    run_subtask_pipeline,
    update_task_counter,
    update_task_field,
)


# ─── mock용 agent 결과 팩토리 ───

def _agent_success(action="completed", **extra):
    """성공 결과를 반환하는 mock 값."""
    result = {"action": action}
    result.update(extra)
    return True, result


def _agent_rejected():
    """reviewer rejected 결과."""
    return True, {"action": "rejected", "reason": "코드 품질 미흡"}


def _agent_reporter_pass():
    """reporter 통과 결과."""
    return True, {"action": "completed", "verdict": "pass"}


def _agent_reporter_fail():
    """reporter 실패 판정 결과."""
    return True, {"action": "completed", "verdict": "fail", "reason": "테스트 실패"}


def _agent_reporter_needs_replan():
    """reporter replan 요청 결과."""
    return True, {"action": "completed", "needs_replan": True, "reason": "설계 변경 필요"}


def _agent_failure():
    """agent 실행 자체 실패."""
    return False, None


class TestNormalPipeline:
    """정상 pipeline 흐름."""

    @patch("workflow_controller.run_agent")
    def test_all_pass(self, mock_run_agent, test_project):
        """모든 agent가 통과하면 True를 반환한다."""
        task_file = _create_task_json(
            test_project["tasks_dir"], "00001",
            project_name=test_project["name"],
        )
        pipeline = ["coder", "reviewer", "reporter"]

        mock_run_agent.side_effect = [
            _agent_success(),                # coder
            _agent_success(action="approved"),  # reviewer
            _agent_reporter_pass(),          # reporter
        ]

        result = run_subtask_pipeline(
            test_project["dir"], test_project["name"], "00001", "00001-1",
            task_file, pipeline, dummy=True,
        )

        assert result is True
        assert mock_run_agent.call_count == 3

    @patch("workflow_controller.run_agent")
    def test_agent_failure_stops_pipeline(self, mock_run_agent, test_project):
        """agent 실행 실패 시 즉시 False를 반환한다."""
        task_file = _create_task_json(
            test_project["tasks_dir"], "00001",
            project_name=test_project["name"],
        )
        pipeline = ["coder", "reviewer", "reporter"]

        mock_run_agent.side_effect = [
            _agent_success(),  # coder 통과
            _agent_failure(),  # reviewer 실행 실패
        ]

        result = run_subtask_pipeline(
            test_project["dir"], test_project["name"], "00001", "00001-1",
            task_file, pipeline, dummy=True,
        )

        assert result is False
        assert mock_run_agent.call_count == 2  # reporter까지 가지 않음


class TestReviewerReject:
    """reviewer 거절 → coder 루프백 시나리오."""

    @patch("workflow_controller.run_agent")
    def test_reject_then_pass(self, mock_run_agent, test_project):
        """reviewer 1회 거절 후 통과 → retry 카운터 1, 최종 성공."""
        task_file = _create_task_json(
            test_project["tasks_dir"], "00001",
            project_name=test_project["name"],
        )
        pipeline = ["coder", "reviewer", "reporter"]

        mock_run_agent.side_effect = [
            # 1차 시도
            _agent_success(),                  # coder
            _agent_rejected(),                 # reviewer reject
            # 2차 시도 (루프백)
            _agent_success(),                  # coder (재실행)
            _agent_success(action="approved"), # reviewer 통과
            _agent_reporter_pass(),            # reporter 통과
        ]

        result = run_subtask_pipeline(
            test_project["dir"], test_project["name"], "00001", "00001-1",
            task_file, pipeline, dummy=True,
        )

        assert result is True
        assert mock_run_agent.call_count == 5

        # retry 카운터 확인
        task = load_json(task_file)
        assert task["counters"]["current_subtask_retry"] == 1

    @patch("workflow_controller.run_agent")
    def test_multiple_rejects(self, mock_run_agent, test_project):
        """reviewer 3회 연속 거절 → retry 카운터 3."""
        task_file = _create_task_json(
            test_project["tasks_dir"], "00001",
            project_name=test_project["name"],
        )
        pipeline = ["coder", "reviewer", "reporter"]

        mock_run_agent.side_effect = [
            _agent_success(), _agent_rejected(),  # 1차
            _agent_success(), _agent_rejected(),  # 2차
            _agent_success(), _agent_rejected(),  # 3차
            _agent_success(), _agent_success(action="approved"),  # 4차 통과
            _agent_reporter_pass(),
        ]

        result = run_subtask_pipeline(
            test_project["dir"], test_project["name"], "00001", "00001-1",
            task_file, pipeline, dummy=True,
        )

        assert result is True
        task = load_json(task_file)
        assert task["counters"]["current_subtask_retry"] == 3


class TestReporterFail:
    """reporter 실패 판정 → coder 루프백 시나리오."""

    @patch("workflow_controller.run_agent")
    def test_reporter_fail_then_pass(self, mock_run_agent, test_project):
        """reporter 1회 실패 후 재시도 성공."""
        task_file = _create_task_json(
            test_project["tasks_dir"], "00001",
            project_name=test_project["name"],
        )
        pipeline = ["coder", "reviewer", "reporter"]

        mock_run_agent.side_effect = [
            # 1차
            _agent_success(),                  # coder
            _agent_success(action="approved"), # reviewer
            _agent_reporter_fail(),            # reporter fail
            # 2차 (루프백)
            _agent_success(),                  # coder
            _agent_success(action="approved"), # reviewer
            _agent_reporter_pass(),            # reporter pass
        ]

        result = run_subtask_pipeline(
            test_project["dir"], test_project["name"], "00001", "00001-1",
            task_file, pipeline, dummy=True,
        )

        assert result is True
        task = load_json(task_file)
        assert task["counters"]["current_subtask_retry"] == 1


class TestReporterNeedsReplan:
    """reporter가 replan을 요청하는 시나리오."""

    @patch("workflow_controller.run_agent")
    def test_needs_replan(self, mock_run_agent, test_project):
        """reporter가 needs_replan=True → _needs_replan 플래그 설정, False 반환."""
        task_file = _create_task_json(
            test_project["tasks_dir"], "00001",
            project_name=test_project["name"],
        )
        pipeline = ["coder", "reviewer", "reporter"]

        mock_run_agent.side_effect = [
            _agent_success(),                  # coder
            _agent_success(action="approved"), # reviewer
            _agent_reporter_needs_replan(),    # reporter → replan 요청
        ]

        result = run_subtask_pipeline(
            test_project["dir"], test_project["name"], "00001", "00001-1",
            task_file, pipeline, dummy=True,
        )

        assert result is False  # replan 필요 → 실패로 반환

        task = load_json(task_file)
        assert task.get("_needs_replan") is True


class TestMixedScenarios:
    """복합 시나리오."""

    @patch("workflow_controller.run_agent")
    def test_reviewer_reject_then_reporter_fail(self, mock_run_agent, test_project):
        """reviewer 거절 1회 + reporter 실패 1회 → 최종 성공."""
        task_file = _create_task_json(
            test_project["tasks_dir"], "00001",
            project_name=test_project["name"],
        )
        pipeline = ["coder", "reviewer", "reporter"]

        mock_run_agent.side_effect = [
            # 1차: reviewer 거절
            _agent_success(), _agent_rejected(),
            # 2차: reviewer 통과, reporter 실패
            _agent_success(), _agent_success(action="approved"), _agent_reporter_fail(),
            # 3차: 전체 통과
            _agent_success(), _agent_success(action="approved"), _agent_reporter_pass(),
        ]

        result = run_subtask_pipeline(
            test_project["dir"], test_project["name"], "00001", "00001-1",
            task_file, pipeline, dummy=True,
        )

        assert result is True
        task = load_json(task_file)
        # reviewer reject 1회 + reporter fail에서 루프백 시 retry도 1회 = 총 2회
        # 첫 번째 reject로 1, 두 번째 pass 후 reporter fail로 다시 1 → 재귀라서 리셋 안됨
        # 실제로: reject → retry=1, reporter fail → retry=2
        assert task["counters"]["current_subtask_retry"] == 2

    @patch("workflow_controller.run_agent")
    def test_coder_failure_mid_pipeline(self, mock_run_agent, test_project):
        """coder가 실행 자체에 실패하면 즉시 중단."""
        task_file = _create_task_json(
            test_project["tasks_dir"], "00001",
            project_name=test_project["name"],
        )
        pipeline = ["coder", "reviewer", "reporter"]

        mock_run_agent.side_effect = [
            _agent_failure(),  # coder 실패
        ]

        result = run_subtask_pipeline(
            test_project["dir"], test_project["name"], "00001", "00001-1",
            task_file, pipeline, dummy=True,
        )

        assert result is False
        assert mock_run_agent.call_count == 1


class TestPipelineAgentCallOrder:
    """agent 호출 순서 검증."""

    @patch("workflow_controller.run_agent")
    def test_call_order(self, mock_run_agent, test_project):
        """pipeline에 정의된 순서대로 agent가 호출된다."""
        task_file = _create_task_json(
            test_project["tasks_dir"], "00001",
            project_name=test_project["name"],
        )
        pipeline = ["coder", "reviewer", "reporter"]

        mock_run_agent.side_effect = [
            _agent_success(),
            _agent_success(action="approved"),
            _agent_reporter_pass(),
        ]

        run_subtask_pipeline(
            test_project["dir"], test_project["name"], "00001", "00001-1",
            task_file, pipeline, dummy=True,
        )

        # 호출 순서 검증
        agent_types = [c.args[1] for c in mock_run_agent.call_args_list]
        assert agent_types == ["coder", "reviewer", "reporter"]

    @patch("workflow_controller.run_agent")
    def test_reject_loopback_restarts_from_coder(self, mock_run_agent, test_project):
        """reviewer reject 시 coder부터 다시 시작한다 (reviewer/reporter만 재시작 아님)."""
        task_file = _create_task_json(
            test_project["tasks_dir"], "00001",
            project_name=test_project["name"],
        )
        pipeline = ["coder", "reviewer", "reporter"]

        mock_run_agent.side_effect = [
            _agent_success(), _agent_rejected(),                     # 1차
            _agent_success(), _agent_success(action="approved"),     # 2차
            _agent_reporter_pass(),
        ]

        run_subtask_pipeline(
            test_project["dir"], test_project["name"], "00001", "00001-1",
            task_file, pipeline, dummy=True,
        )

        agent_types = [c.args[1] for c in mock_run_agent.call_args_list]
        # coder → reviewer(reject) → coder → reviewer → reporter
        assert agent_types == ["coder", "reviewer", "coder", "reviewer", "reporter"]
