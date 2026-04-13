"""Priority Queue 파일 조작 헬퍼.

프로젝트별로 3개의 queue 파일(task_queue_critical.json, task_queue_urgent.json,
task_queue_default.json)을 관리한다. 각 파일은 순수 task_id 문자열 배열.

모든 read-modify-write 연산은 fcntl.flock으로 보호한다.
Source of truth는 task JSON 파일이며, queue 파일은 실행 순서/우선순위만 표현한다.
"""

import fcntl
import json
import os
from pathlib import Path
from typing import Optional

# 우선순위 순서대로 정의 (앞에 있을수록 먼저 실행)
PRIORITIES = ("critical", "urgent", "default")
DEFAULT_PRIORITY = "default"


def queue_file_path(project_dir, priority: str) -> str:
    """주어진 priority의 queue 파일 경로를 반환한다."""
    if priority not in PRIORITIES:
        raise ValueError(f"잘못된 priority: {priority!r}. 허용: {PRIORITIES}")
    return os.path.join(str(project_dir), f"task_queue_{priority}.json")


def ensure_queue_files(project_dir) -> None:
    """프로젝트 디렉토리에 3개의 queue 파일이 없으면 빈 배열로 생성한다."""
    project_dir = str(project_dir)
    os.makedirs(project_dir, exist_ok=True)
    for priority in PRIORITIES:
        path = queue_file_path(project_dir, priority)
        if not os.path.exists(path):
            # 배타 lock으로 동시 생성 방지
            with open(path, "w") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    f.write("[]\n")
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _read_queue_locked(fp) -> list:
    """이미 lock이 걸린 file pointer로부터 queue 내용을 읽는다."""
    fp.seek(0)
    content = fp.read()
    if not content.strip():
        return []
    return json.loads(content)


def _write_queue_locked(fp, ids: list) -> None:
    """이미 lock이 걸린 file pointer에 queue 내용을 덮어쓴다."""
    fp.seek(0)
    fp.truncate(0)
    json.dump(ids, fp, ensure_ascii=False, indent=2)
    fp.write("\n")
    fp.flush()
    os.fsync(fp.fileno())


def append_to_queue(project_dir, priority: str, task_id: str) -> None:
    """queue 파일 끝에 task_id를 추가한다. flock으로 race 보호.

    이미 존재하면 중복 추가하지 않는다.
    """
    ensure_queue_files(project_dir)
    path = queue_file_path(project_dir, priority)
    with open(path, "r+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            ids = _read_queue_locked(f)
            if task_id not in ids:
                ids.append(task_id)
                _write_queue_locked(f, ids)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def remove_from_queue(project_dir, priority: str, task_id: str) -> bool:
    """지정된 priority queue에서 task_id를 제거한다.

    Returns:
        제거됐으면 True, 원래 없었으면 False.
    """
    ensure_queue_files(project_dir)
    path = queue_file_path(project_dir, priority)
    with open(path, "r+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            ids = _read_queue_locked(f)
            if task_id not in ids:
                return False
            ids = [tid for tid in ids if tid != task_id]
            _write_queue_locked(f, ids)
            return True
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def remove_task_from_all_queues(project_dir, task_id: str) -> Optional[str]:
    """모든 priority queue에서 task_id를 찾아 제거한다.

    Returns:
        제거된 priority ("critical"/"urgent"/"default"). 없었으면 None.
    """
    for priority in PRIORITIES:
        if remove_from_queue(project_dir, priority, task_id):
            return priority
    return None


def peek_next_task(project_dir) -> Optional[tuple]:
    """실행할 다음 task를 priority 순으로 찾는다. queue를 변경하지 않는다.

    critical → urgent → default 순으로 순회하며, 각 queue에서
    첫 번째 id를 반환한다 (각 queue 내부는 append 순서 = id 순서).

    Returns:
        (priority, task_id) 튜플. 모든 queue가 비어있으면 None.
    """
    ensure_queue_files(project_dir)
    for priority in PRIORITIES:
        path = queue_file_path(project_dir, priority)
        with open(path, "r") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            try:
                ids = _read_queue_locked(f)
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        if ids:
            return priority, ids[0]
    return None


def pop_task(project_dir, priority: str, task_id: str) -> bool:
    """queue에서 특정 task_id를 제거한다 (flock 보호).

    remove_from_queue와 동일. TM이 peek 후 "이 id를 꺼냈다"는
    의미를 명시적으로 나타내기 위한 별칭.
    """
    return remove_from_queue(project_dir, priority, task_id)


def read_queue(project_dir, priority: str) -> list:
    """queue 내용을 읽어 반환한다 (flock shared lock).

    주로 디버깅/테스트 용도.
    """
    ensure_queue_files(project_dir)
    path = queue_file_path(project_dir, priority)
    with open(path, "r") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_SH)
        try:
            return _read_queue_locked(f)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def migrate_ready_sentinels(project_dir) -> list:
    """tasks/ 디렉토리의 레거시 .ready 파일을 default queue로 이주시킨다.

    각 .ready 파일에 대해:
      1. task_id 추출
      2. default queue에 append (이미 있으면 skip)
      3. .ready 파일 삭제

    priority별 정보가 없는 과거 제출 건이므로 default로 통일한다.
    E2E handoff용 .ready 파일(handoffs/ 디렉토리)은 대상이 아니다.

    Returns:
        이주된 task_id 목록.
    """
    project_dir = str(project_dir)
    tasks_dir = os.path.join(project_dir, "tasks")
    if not os.path.isdir(tasks_dir):
        return []

    migrated = []
    ready_files = sorted(Path(tasks_dir).glob("*.ready"))
    for ready_file in ready_files:
        task_id = ready_file.stem
        # task JSON 존재 여부 확인 (없으면 orphan이므로 .ready만 제거)
        has_json = bool(
            list(Path(tasks_dir).glob(f"{task_id}-*.json"))
        ) or (Path(tasks_dir) / f"{task_id}.json").exists()

        if has_json:
            append_to_queue(project_dir, DEFAULT_PRIORITY, task_id)
            migrated.append(task_id)

        try:
            os.unlink(ready_file)
        except FileNotFoundError:
            pass

    return migrated
