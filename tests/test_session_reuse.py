"""
Claude 세션 재사용 (agent 세션 ID 발급) 테스트.

allocate_session_id.py가 다음 동작을 보장하는지 검증:
1. 신규 agent_type 요청 시 UUID를 발급하고 "new" 모드를 반환
2. 이미 발급된 agent_type은 같은 UUID를 "resume" 모드로 반환
3. 여러 agent_type이 서로 다른 UUID를 받는다
4. task JSON 파일에 agent_sessions 필드가 누적된다
5. 이미 agent_sessions 필드가 있는 task JSON에도 호환
"""

import json
import os
import subprocess
import uuid

import pytest


SCRIPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts",
)
ALLOCATOR = os.path.join(SCRIPTS_DIR, "allocate_session_id.py")


def _run_allocator(task_file: str, agent_type: str) -> tuple[str, str]:
    """allocator를 subprocess로 실행하고 (session_id, mode)를 파싱해 반환."""
    result = subprocess.run(
        ["python3", ALLOCATOR, task_file, agent_type],
        capture_output=True, text=True, check=True,
    )
    parts = result.stdout.strip().split()
    assert len(parts) == 2, f"예상치 못한 출력: {result.stdout!r}"
    return parts[0], parts[1]


def _write_task(path: str, task_data: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(task_data, f, ensure_ascii=False, indent=2)


def _read_task(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class TestAllocateSessionId:
    def test_new_agent_returns_new_mode(self, tmp_path):
        """처음 보는 agent_type은 UUID를 새로 발급하고 mode=new."""
        task_file = str(tmp_path / "task.json")
        _write_task(task_file, {"task_id": "00001"})

        session_id, mode = _run_allocator(task_file, "coder")

        assert mode == "new"
        # 유효한 UUID 형식인지 확인
        uuid.UUID(session_id)  # 실패 시 ValueError

    def test_same_agent_returns_resume_mode(self, tmp_path):
        """두 번째 호출 시 같은 UUID + mode=resume."""
        task_file = str(tmp_path / "task.json")
        _write_task(task_file, {"task_id": "00001"})

        id1, mode1 = _run_allocator(task_file, "coder")
        id2, mode2 = _run_allocator(task_file, "coder")

        assert mode1 == "new"
        assert mode2 == "resume"
        assert id1 == id2

    def test_different_agents_get_different_ids(self, tmp_path):
        """agent_type이 다르면 UUID가 달라진다."""
        task_file = str(tmp_path / "task.json")
        _write_task(task_file, {"task_id": "00001"})

        coder_id, _ = _run_allocator(task_file, "coder")
        reviewer_id, _ = _run_allocator(task_file, "reviewer")

        assert coder_id != reviewer_id

    def test_task_json_accumulates_sessions(self, tmp_path):
        """여러 agent_type 호출 후 task JSON의 agent_sessions에 모두 기록된다."""
        task_file = str(tmp_path / "task.json")
        _write_task(task_file, {"task_id": "00001", "title": "t"})

        _run_allocator(task_file, "coder")
        _run_allocator(task_file, "reviewer")
        _run_allocator(task_file, "planner")

        task = _read_task(task_file)
        sessions = task.get("agent_sessions", {})
        assert set(sessions.keys()) == {"coder", "reviewer", "planner"}
        # 원본 필드 보존
        assert task["task_id"] == "00001"
        assert task["title"] == "t"

    def test_preserves_existing_agent_sessions(self, tmp_path):
        """이미 있는 agent_sessions 필드를 덮어쓰지 않는다."""
        task_file = str(tmp_path / "task.json")
        preset_id = str(uuid.uuid4())
        _write_task(task_file, {
            "task_id": "00001",
            "agent_sessions": {"coder": preset_id},
        })

        coder_id, mode = _run_allocator(task_file, "coder")
        assert coder_id == preset_id
        assert mode == "resume"

        # 다른 agent 추가해도 기존 ID 유지
        reviewer_id, _ = _run_allocator(task_file, "reviewer")
        task = _read_task(task_file)
        assert task["agent_sessions"]["coder"] == preset_id
        assert task["agent_sessions"]["reviewer"] == reviewer_id

    def test_usage_error_on_missing_args(self, tmp_path):
        """인자가 부족하면 exit code 2로 실패."""
        result = subprocess.run(
            ["python3", ALLOCATOR],
            capture_output=True, text=True,
        )
        assert result.returncode == 2
