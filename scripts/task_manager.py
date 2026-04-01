#!/usr/bin/env python3
"""Task Manager — 유일한 외부 인터페이스.

CLI submit 명령을 받아 task JSON을 생성하고,
작업 큐를 관리하며, 완료/실패/에스컬레이션 알림을 처리한다.
24시간 상주 프로세스.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml


def load_config(config_path: str = "config.yaml") -> dict:
    """config.yaml을 읽어 딕셔너리로 반환한다."""
    with open(config_path, "r", encoding="utf-8") as config_file:
        return yaml.safe_load(config_file)


def generate_task_id(workspace_dir: str) -> str:
    """tasks/ 디렉토리의 기존 task 파일을 확인하여 다음 task ID를 생성한다."""
    tasks_dir = Path(workspace_dir) / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)

    existing_ids = []
    for filename in tasks_dir.glob("TASK-*.json"):
        # TASK-042.json에서 숫자 부분 추출 (plan, subtask 파일 제외)
        stem = filename.stem
        if stem.count("-") == 1:
            try:
                task_number = int(stem.split("-")[1])
                existing_ids.append(task_number)
            except ValueError:
                continue

    next_number = max(existing_ids, default=0) + 1
    return f"TASK-{next_number:03d}"


def create_task(
    title: str,
    description: str,
    config: dict,
    workspace_dir: str,
    attachments: list | None = None,
    config_override: dict | None = None,
) -> dict:
    """새 task JSON을 생성하고 파일에 저장한다."""
    task_id = generate_task_id(workspace_dir)
    tasks_dir = Path(workspace_dir) / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)

    # 첨부 파일 디렉토리 생성
    if attachments:
        attachments_dir = Path(workspace_dir) / "attachments" / task_id
        attachments_dir.mkdir(parents=True, exist_ok=True)

    task = {
        "task_id": task_id,
        "title": title,
        "description": description,
        "submitted_via": "cli",
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "status": "queued",
        "branch": f"feature/{task_id}" if config.get("git", {}).get("enabled", True) else None,
        "attachments": attachments or [],
        "testing": config_override.get("testing", config.get("testing", {}))
        if config_override
        else config.get("testing", {}),
        "human_review_policy": config_override.get(
            "human_review_policy", config.get("human_review_policy", {})
        )
        if config_override
        else config.get("human_review_policy", {}),
        "plan_version": 0,
        "current_subtask": None,
        "completed_subtasks": [],
        "counters": {
            "total_agent_invocations": 0,
            "replan_count": 0,
            "current_subtask_retry": 0,
        },
        "limits": config_override.get("limits", config.get("limits", {}))
        if config_override
        else config.get("limits", {}),
        "config_override": config_override or {},
        "human_interaction": None,
        "mid_task_feedback": [],
        "escalation_reason": None,
    }

    # atomic write: tmp 파일에 쓴 뒤 rename
    task_file = tasks_dir / f"{task_id}.json"
    tmp_file = tasks_dir / f"{task_id}.json.tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(task, f, ensure_ascii=False, indent=2)
    os.rename(tmp_file, task_file)

    # sentinel 파일 생성 (Workflow Controller 트리거)
    sentinel_file = tasks_dir / f"{task_id}.ready"
    sentinel_file.touch()

    return task


def main():
    """Task Manager 메인 루프."""
    config = load_config()
    workspace_dir = config.get("executor", {}).get("workspace_dir", "workspaces/default")

    print(f"[Task Manager] 시작됨. workspace: {workspace_dir}")
    print("[Task Manager] CLI 입력 대기 중... (Ctrl+C로 종료)")

    # TODO: CLI 인터페이스 구현 (submit, status, approve, reject 등)
    # TODO: 작업 큐 관리 로직
    # TODO: human interaction 대기/처리 로직
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[Task Manager] 종료됨.")


if __name__ == "__main__":
    main()
