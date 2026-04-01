#!/usr/bin/env python3
"""Workflow Controller — 내부 파이프라인 제어.

task 1개에 대해 전체 파이프라인을 책임진다:
Planner → subtask loop (Coder → Reviewer → Setup → Test → Reporter) → Integration Test → PR

.ready sentinel 파일을 inotifywait로 감지하여 다음 agent를 기동한다.
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml


def load_config(config_path: str = "config.yaml") -> dict:
    """config.yaml을 읽어 딕셔너리로 반환한다."""
    with open(config_path, "r", encoding="utf-8") as config_file:
        return yaml.safe_load(config_file)


def load_task(task_file_path: str) -> dict:
    """task JSON 파일을 읽어 딕셔너리로 반환한다."""
    with open(task_file_path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_task(task: dict, task_file_path: str) -> None:
    """task 딕셔너리를 JSON 파일에 atomic write로 저장한다."""
    tmp_path = task_file_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(task, f, ensure_ascii=False, indent=2)
    os.rename(tmp_path, task_file_path)


def check_limits(task: dict) -> str | None:
    """안전 제한을 확인한다. 초과 시 사유 문자열, 정상이면 None을 반환한다."""
    counters = task["counters"]
    limits = task["limits"]

    if counters["total_agent_invocations"] >= limits["max_total_agent_invocations"]:
        return f"총 agent 호출 횟수 초과 ({counters['total_agent_invocations']}/{limits['max_total_agent_invocations']})"

    if counters["replan_count"] >= limits["max_replan_count"]:
        return f"re-plan 횟수 초과 ({counters['replan_count']}/{limits['max_replan_count']})"

    return None


def is_git_enabled(config: dict) -> bool:
    """config에서 git 사용 여부를 확인한다."""
    return config.get("git", {}).get("enabled", True)


def create_git_branch(task: dict, config: dict) -> None:
    """task용 feature 브랜치를 생성한다. git이 비활성화되어 있으면 건너뛴다."""
    if not is_git_enabled(config):
        print(f"[Workflow] git 비활성화 — branch 생성 건너뜀 (task: {task['task_id']})")
        return

    branch_name = task["branch"]
    default_branch = config["project"]["default_branch"]
    codebase_path = config["executor"]["codebase_path"]

    subprocess.run(["git", "checkout", default_branch], check=True, cwd=codebase_path)
    subprocess.run(["git", "checkout", "-b", branch_name], check=True, cwd=codebase_path)


def commit_subtask(task: dict, subtask_id: str, config: dict) -> None:
    """subtask 완료 후 변경사항을 커밋한다. git이 비활성화되어 있으면 건너뛴다."""
    if not is_git_enabled(config):
        print(f"[Workflow] git 비활성화 — commit 건너뜀 (subtask: {subtask_id})")
        return

    codebase_path = config["executor"]["codebase_path"]
    message = f"{task['task_id']} / {subtask_id} 완료"

    subprocess.run(["git", "add", "-A"], check=True, cwd=codebase_path)
    subprocess.run(["git", "commit", "-m", message], check=True, cwd=codebase_path)


def create_pull_request(task: dict, config: dict) -> None:
    """모든 subtask 완료 후 PR을 생성한다. git이 비활성화되어 있으면 건너뛴다."""
    if not is_git_enabled(config):
        print(f"[Workflow] git 비활성화 — PR 생성 건너뜀 (task: {task['task_id']})")
        return

    codebase_path = config["executor"]["codebase_path"]
    branch_name = task["branch"]
    target_branch = config["git"]["pr_target_branch"]
    remote = config["git"]["remote"]

    subprocess.run(["git", "push", "-u", remote, branch_name], check=True, cwd=codebase_path)
    # TODO: gh pr create 호출


def invoke_agent(
    agent_type: str,
    task: dict,
    subtask: dict | None,
    config: dict,
    workspace_dir: str,
) -> dict:
    """Claude Code 세션으로 agent를 기동하고 결과를 반환한다."""
    task["counters"]["total_agent_invocations"] += 1

    # TODO: run_claude_agent.sh를 통한 실제 agent 기동 구현
    # - agent_type에 맞는 프롬프트 로드 (config/agent_prompts/{agent_type}.md)
    # - task/subtask 컨텍스트 전달
    # - 세션 로그 저장
    # - 결과 JSON 파싱 및 반환

    print(f"[Workflow] agent 기동: {agent_type} (task: {task['task_id']})")
    return {"status": "not_implemented"}


def determine_pipeline_steps(task: dict) -> list[str]:
    """testing 설정에 따라 실행할 파이프라인 단계를 결정한다."""
    testing = task.get("testing", {})
    steps = ["coder", "reviewer"]

    # testing이 하나라도 활성화되어 있으면 Setup 필요
    any_test_enabled = (
        testing.get("unit_test", {}).get("enabled", False)
        or testing.get("e2e_test", {}).get("enabled", False)
    )

    if any_test_enabled:
        steps.append("setup")

    if testing.get("unit_test", {}).get("enabled", False):
        steps.append("unit_tester")

    if testing.get("e2e_test", {}).get("enabled", False):
        steps.append("e2e_tester")

    if any_test_enabled:
        steps.append("reporter")

    return steps


def run_subtask_pipeline(
    task: dict,
    subtask: dict,
    config: dict,
    workspace_dir: str,
) -> str:
    """단일 subtask에 대한 파이프라인을 실행한다. 최종 상태 문자열을 반환한다."""
    steps = determine_pipeline_steps(task)

    for step in steps:
        # 매 agent 호출 전 한도 확인
        limit_exceeded = check_limits(task)
        if limit_exceeded:
            return f"escalated: {limit_exceeded}"

        result = invoke_agent(step, task, subtask, config, workspace_dir)
        # TODO: 결과에 따른 루프백/다음 단계 분기 로직

    return "completed"


def watch_for_new_tasks(workspace_dir: str) -> None:
    """tasks/ 디렉토리의 .ready 파일을 감시하여 새 task를 처리한다."""
    tasks_dir = Path(workspace_dir) / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)

    # inotifywait로 .ready 파일 감시
    # TODO: inotifywait 기반 감시 루프 구현
    print(f"[Workflow] 감시 시작: {tasks_dir}")


def main():
    """Workflow Controller 메인 루프."""
    config = load_config()
    workspace_dir = config.get("executor", {}).get("workspace_dir", "workspaces/default")

    print(f"[Workflow Controller] 시작됨. workspace: {workspace_dir}")
    watch_for_new_tasks(workspace_dir)


if __name__ == "__main__":
    main()
