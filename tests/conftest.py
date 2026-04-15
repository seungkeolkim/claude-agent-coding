"""
Agent Hub 테스트 공통 fixture.

임시 프로젝트를 생성하고 테스트 종료 시 정리한다.
프로젝트명: test_{YYMMDD-HHmmss}_{random4자리}
"""

import json
import os
import random
import shutil
import sys
from datetime import datetime, timezone

import pytest
import yaml

# scripts/ 모듈 import 가능하도록 path 추가
AGENT_HUB_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(AGENT_HUB_ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)


def _generate_project_name():
    """테스트용 프로젝트명을 생성한다. test_YYMMDD-HHmmss_RRRR 형태."""
    timestamp = datetime.now().strftime("%y%m%d-%H%M%S")
    rand_suffix = f"{random.randint(1000, 9999)}"
    return f"test_{timestamp}_{rand_suffix}"


# ─── 최소 config.yaml dict ───

def _minimal_config(agent_hub_root):
    """테스트용 최소 config dict를 반환한다."""
    return {
        "claude": {
            "planner_model": "sonnet",
            "coder_model": "sonnet",
            "reviewer_model": "sonnet",
            "setup_model": "sonnet",
            "unit_tester_model": "sonnet",
            "e2e_tester_model": "sonnet",
            "reporter_model": "sonnet",
            "max_turns_per_session": 10,
            "usage_thresholds": {
                "new_task": 0.70,
                "new_subtask": 0.80,
                "new_agent_stage": 0.90,
            },
            "usage_check_interval_seconds": 5,
        },
        "default_limits": {
            "max_subtask_count": 5,
            "max_retry_per_subtask": 3,
            "max_replan_count": 2,
            "max_total_agent_invocations": 30,
            "max_task_duration_hours": 4,
        },
        "default_human_review_policy": {
            "review_plan": False,
            "review_replan": False,
            "review_before_merge": False,
            "auto_approve_timeout_hours": 24,
        },
        "default_task_queue": {
            "wait_for_prev_task_done": True,
        },
        "logging": {
            "level": "debug",
            "archive_completed_tasks": False,
            "keep_session_logs": True,
        },
        "notification": {
            "channel": "cli",
            "events": {
                "task_completed": True,
                "task_failed": True,
                "pr_created": True,
                "plan_review_requested": True,
                "replan_review_requested": True,
                "escalation": True,
            },
            "re_notification_interval_hours": 0,
        },
    }


def _minimal_project_yaml(project_name, codebase_path):
    """테스트용 최소 project.yaml dict를 반환한다."""
    return {
        "project": {
            "name": project_name,
            "description": f"테스트용 임시 프로젝트 ({project_name})",
        },
        "codebase": {
            "path": codebase_path,
            "language": "python",
            "description": "테스트용 코드베이스",
        },
        "git": {
            "enabled": False,
            "remote": "origin",
            "default_branch": "main",
            "branch_prefix": "agent/",
            "merge_strategy": "require_human",
            "author_name": "Test Agent",
            "author_email": "test@agent.hub",
        },
        "testing": {
            "unit_test": {
                "enabled": False,
                "command": "echo 'no tests'",
            },
            "e2e_test": {
                "enabled": False,
            },
        },
        "pipeline": ["planner", "coder", "reviewer", "reporter"],
    }


def _create_task_json(tasks_dir, task_id, title="테스트 task",
                      description="테스트용", status="submitted",
                      project_name="test", config_override=None):
    """task JSON 파일을 생성하고 경로를 반환한다."""
    now = datetime.now(timezone.utc).isoformat()
    task_data = {
        "task_id": task_id,
        "project_name": project_name,
        "title": title,
        "description": description,
        "submitted_via": "test",
        "submitted_at": now,
        "status": status,
        "branch": None,
        "attachments": [],
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

    os.makedirs(tasks_dir, exist_ok=True)
    slug = title.replace(" ", "-")[:30]
    task_file = os.path.join(tasks_dir, f"{task_id}-{slug}.json")
    with open(task_file, "w") as f:
        json.dump(task_data, f, ensure_ascii=False, indent=2)
        f.write("\n")

    return task_file


def _enqueue_task(project_dir, task_id, priority="default"):
    """task_id를 priority queue에 등록한다. (.ready sentinel 후속 방식)"""
    # hub_api는 conftest 상단에서 sys.path 추가했으므로 import 가능
    from hub_api import queue_helpers
    queue_helpers.append_to_queue(project_dir, priority, task_id)


# 레거시 호환 alias — 신규 테스트에서는 _enqueue_task 사용 권장.
def _create_ready_sentinel(tasks_dir, task_id):
    """호환용: tasks_dir 상위 디렉토리를 project_dir로 간주하여 default queue에 등록."""
    project_dir = os.path.dirname(tasks_dir.rstrip("/"))
    _enqueue_task(project_dir, task_id, "default")
    return None


# ═══════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════


@pytest.fixture(autouse=True)
def _disable_telegram_command_enqueue(monkeypatch):
    """테스트가 실제 telegram_commands/ 에 명령을 흘리지 못하도록 봉쇄.

    개별 테스트에서 create_project / close_project / reopen_project를 호출하면
    bridge가 동작 중일 경우 실 Telegram 그룹에 forum topic이 우후죽순 생긴다.
    테스트 동안에는 enqueue 함수를 no-op으로 패치한다.
    """
    from hub_api import core as _core
    monkeypatch.setattr(_core, "_enqueue_telegram_command", lambda *a, **kw: None)


@pytest.fixture
def agent_hub_root():
    """Agent Hub 루트 디렉토리 경로."""
    return AGENT_HUB_ROOT


@pytest.fixture
def test_project_name():
    """유니크한 테스트 프로젝트명을 생성한다."""
    return _generate_project_name()


@pytest.fixture
def test_project(agent_hub_root, test_project_name):
    """
    임시 프로젝트를 생성하고 테스트 후 삭제한다.

    Returns:
        dict: {
            "name": 프로젝트명,
            "dir": 프로젝트 디렉토리 경로,
            "tasks_dir": tasks/ 경로,
            "config": config dict,
            "project_yaml": project.yaml dict,
        }
    """
    project_dir = os.path.join(agent_hub_root, "projects", test_project_name)
    tasks_dir = os.path.join(project_dir, "tasks")
    logs_dir = os.path.join(project_dir, "logs")
    commands_dir = os.path.join(project_dir, "commands")

    os.makedirs(tasks_dir, exist_ok=True)
    os.makedirs(logs_dir, exist_ok=True)
    os.makedirs(commands_dir, exist_ok=True)

    # project.yaml 생성
    project_yaml_data = _minimal_project_yaml(test_project_name, agent_hub_root)
    project_yaml_path = os.path.join(project_dir, "project.yaml")
    with open(project_yaml_path, "w") as f:
        yaml.dump(project_yaml_data, f, default_flow_style=False, allow_unicode=True)

    # project_state.json 생성
    state_data = {
        "project_name": test_project_name,
        "status": "idle",
        "current_task_id": None,
        "last_error_task_id": None,
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "overrides": {},
        "update_history": [],
    }
    state_path = os.path.join(project_dir, "project_state.json")
    with open(state_path, "w") as f:
        json.dump(state_data, f, ensure_ascii=False, indent=2)
        f.write("\n")

    config = _minimal_config(agent_hub_root)

    yield {
        "name": test_project_name,
        "dir": project_dir,
        "tasks_dir": tasks_dir,
        "logs_dir": logs_dir,
        "commands_dir": commands_dir,
        "config": config,
        "project_yaml": project_yaml_data,
    }

    # teardown: 프로젝트 디렉토리 삭제
    if os.path.exists(project_dir):
        shutil.rmtree(project_dir)


@pytest.fixture
def test_task(test_project):
    """
    테스트 프로젝트에 task 하나를 생성한다.

    Returns:
        dict: {
            "task_id": str,
            "task_file": 파일 경로,
            "ready_path": .ready sentinel 경로,
        }
    """
    task_id = "00001"
    task_file = _create_task_json(
        test_project["tasks_dir"], task_id,
        title="테스트-task-unit",
        project_name=test_project["name"],
    )
    _enqueue_task(test_project["dir"], task_id, "default")

    return {
        "task_id": task_id,
        "task_file": task_file,
    }


@pytest.fixture
def hub_api(agent_hub_root):
    """HubAPI 인스턴스를 반환한다."""
    from hub_api.core import HubAPI
    return HubAPI(agent_hub_root)


@pytest.fixture(autouse=True, scope="session")
def cleanup_leftover_test_projects():
    """테스트 세션 종료 후 남아있는 test 프로젝트 잔재를 정리한다.

    E2E 테스트에서 TM/WFC가 프로젝트 디렉토리에 추가 파일(logs, project_state.json)을
    생성할 수 있으며, fixture teardown과의 race condition으로 잔재가 남을 수 있다.
    세션 종료 시 test_ 또는 test- 접두사로 시작하는 프로젝트를 모두 삭제한다.
    """
    yield

    # 세션 종료 후 정리
    projects_dir = os.path.join(AGENT_HUB_ROOT, "projects")
    if not os.path.isdir(projects_dir):
        return

    import re
    # conftest의 _generate_project_name()이 생성하는 패턴: test_YYMMDD-HHmmss_RRRR
    # test_hub_api.py의 _test_project_name()이 생성하는 패턴: test-{label}-YYMMDD-HHmmss
    # 이 두 패턴만 매칭하여 삭제. 사용자가 만든 프로젝트(test-web 등)는 보존.
    test_project_pattern = re.compile(
        r"^test_\d{6}-\d{6}_\d{4}$"       # conftest: test_260406-021638_8285
        r"|^test-[\w-]+-\d{6}-\d{6}$"     # test_hub_api: test-basic-260406-012345
        r"|^a$"                            # 단일 문자 테스트 프로젝트
    )
    for project_name in os.listdir(projects_dir):
        if test_project_pattern.match(project_name):
            project_path = os.path.join(projects_dir, project_name)
            if os.path.isdir(project_path):
                shutil.rmtree(project_path, ignore_errors=True)
