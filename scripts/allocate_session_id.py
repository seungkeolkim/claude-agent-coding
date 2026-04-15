"""
Claude 세션 ID를 (task, agent_type) 단위로 발급/조회한다.

task JSON의 `agent_sessions.{agent_type}` 필드에 UUID를 기록해,
같은 task 내 같은 agent_type의 모든 호출(여러 subtask · retry 포함)이
하나의 claude 세션을 재사용하도록 한다.

사용법:
    python3 allocate_session_id.py <task_file> <agent_type>

출력 (stdout 한 줄):
    <uuid> <mode>

    - mode = "new"    → 이번 호출에서 새로 발급했다. claude를 --session-id로 기동해야 한다.
    - mode = "resume" → 기존 세션이 있다. claude를 --resume으로 이어 붙여야 한다.

WFC는 task 파이프라인 내 같은 agent를 동시에 실행하지 않으므로,
이 스크립트가 task JSON을 동시에 쓸 일은 없어 별도 잠금 없이 안전하다.
"""

from __future__ import annotations

import json
import sys
import uuid


def allocate(task_file: str, agent_type: str) -> tuple[str, str]:
    """task JSON을 읽어 agent_type용 session_id를 반환한다.

    기존 세션이 있으면 (session_id, "resume"), 없으면 새 UUID를 발급해
    JSON에 기록한 뒤 (session_id, "new")를 반환한다.
    """
    with open(task_file, encoding="utf-8") as f:
        task = json.load(f)

    sessions = task.get("agent_sessions")
    if not isinstance(sessions, dict):
        sessions = {}
        task["agent_sessions"] = sessions

    existing = sessions.get(agent_type)
    if isinstance(existing, str) and existing:
        return existing, "resume"

    new_id = str(uuid.uuid4())
    sessions[agent_type] = new_id

    # 파일 갱신: 원자성을 위해 임시 파일 경유 후 rename
    import os
    tmp_path = task_file + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(task, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, task_file)

    return new_id, "new"


def main():
    if len(sys.argv) != 3:
        print("usage: allocate_session_id.py <task_file> <agent_type>", file=sys.stderr)
        sys.exit(2)

    task_file, agent_type = sys.argv[1], sys.argv[2]
    try:
        session_id, mode = allocate(task_file, agent_type)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[allocate_session_id] 실패: {exc}", file=sys.stderr)
        sys.exit(1)

    # shell에서 파싱하기 쉽게 공백 구분 한 줄로 출력
    print(f"{session_id} {mode}")


if __name__ == "__main__":
    main()
