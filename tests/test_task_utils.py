"""
workflow_controller.py의 task 유틸리티 함수 단위 테스트.

load_json, save_json, update_task_field, update_task_counter 검증.
"""

import json
import os

from conftest import _create_task_json


def test_load_json(test_project):
    """load_json이 JSON을 올바르게 읽는다."""
    from workflow_controller import load_json

    task_file = _create_task_json(
        test_project["tasks_dir"], "00001",
        project_name=test_project["name"],
    )
    data = load_json(task_file)
    assert data["task_id"] == "00001"
    assert data["status"] == "submitted"


def test_save_json(test_project):
    """save_json이 JSON을 올바르게 저장한다."""
    from workflow_controller import load_json, save_json

    path = os.path.join(test_project["dir"], "test_save.json")
    save_json(path, {"key": "value", "num": 42})

    data = load_json(path)
    assert data["key"] == "value"
    assert data["num"] == 42


def test_update_task_field(test_project):
    """update_task_field가 특정 필드를 업데이트한다."""
    from workflow_controller import load_json, update_task_field

    task_file = _create_task_json(
        test_project["tasks_dir"], "00001",
        project_name=test_project["name"],
    )

    # status 변경
    result = update_task_field(task_file, "status", "in_progress")
    assert result["status"] == "in_progress"

    # 파일에도 반영되었는지 확인
    data = load_json(task_file)
    assert data["status"] == "in_progress"


def test_update_task_field_nested(test_project):
    """update_task_field가 중첩 필드도 업데이트할 수 있다."""
    from workflow_controller import load_json, update_task_field

    task_file = _create_task_json(
        test_project["tasks_dir"], "00001",
        project_name=test_project["name"],
    )

    # current_subtask 설정
    update_task_field(task_file, "current_subtask", "00001-1")
    data = load_json(task_file)
    assert data["current_subtask"] == "00001-1"

    # completed_subtasks 리스트 설정
    update_task_field(task_file, "completed_subtasks", ["00001-1", "00001-2"])
    data = load_json(task_file)
    assert data["completed_subtasks"] == ["00001-1", "00001-2"]


def test_update_task_counter_increment(test_project):
    """update_task_counter가 increment=True로 카운터를 1 증가시킨다."""
    from workflow_controller import load_json, update_task_counter

    task_file = _create_task_json(
        test_project["tasks_dir"], "00001",
        project_name=test_project["name"],
    )

    # 0 → 1
    result = update_task_counter(task_file, "current_subtask_retry", increment=True)
    assert result["counters"]["current_subtask_retry"] == 1

    # 1 → 2
    result = update_task_counter(task_file, "current_subtask_retry", increment=True)
    assert result["counters"]["current_subtask_retry"] == 2

    # 파일에도 반영
    data = load_json(task_file)
    assert data["counters"]["current_subtask_retry"] == 2


def test_update_task_counter_set_value(test_project):
    """update_task_counter가 value로 직접 설정한다."""
    from workflow_controller import load_json, update_task_counter

    task_file = _create_task_json(
        test_project["tasks_dir"], "00001",
        project_name=test_project["name"],
    )

    result = update_task_counter(task_file, "replan_count", value=5)
    assert result["counters"]["replan_count"] == 5

    # 파일에도 반영
    data = load_json(task_file)
    assert data["counters"]["replan_count"] == 5


def test_update_task_counter_reset(test_project):
    """value=0으로 카운터를 리셋할 수 있다."""
    from workflow_controller import update_task_counter

    task_file = _create_task_json(
        test_project["tasks_dir"], "00001",
        project_name=test_project["name"],
    )

    # 증가 후 리셋
    update_task_counter(task_file, "current_subtask_retry", increment=True)
    update_task_counter(task_file, "current_subtask_retry", increment=True)
    result = update_task_counter(task_file, "current_subtask_retry", value=0)
    assert result["counters"]["current_subtask_retry"] == 0
