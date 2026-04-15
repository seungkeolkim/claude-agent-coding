#!/usr/bin/env python3
"""
safety limit 체크 스크립트.

agent 실행 전에 호출하여, task의 현재 카운터가 설정된 한계를 초과하지 않았는지 검증한다.
초과 시 비정상 종료(exit 1)하여 agent 실행을 차단한다.

사용법:
    python3 check_safety_limits.py \
        --config <config.yaml> \
        --project-yaml <project.yaml> \
        --task-file <task.json> \
        --agent-type <agent_type>

체크 항목:
    - max_retry_per_subtask: 현재 subtask의 retry 횟수 제한
    - max_replan_count: 전체 task의 replan 횟수 제한
    - max_total_agent_invocations: 전체 agent 호출 총횟수 제한
    - max_task_duration_hours: task 시작 이후 경과 시간 제한
    - max_subtask_count: subtask 개수 제한
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml


def load_yaml(path):
    """YAML 파일을 읽어 dict로 반환한다."""
    with open(path) as f:
        return yaml.safe_load(f) or {}


def load_json(path):
    """JSON 파일을 읽어 dict로 반환한다."""
    with open(path) as f:
        return json.load(f)


def resolve_effective_limits(config, project_yaml, task):
    """
    4계층 설정에서 limits를 해소한다.
    config.yaml(default_limits) → project.yaml(limits) → task.config_override(limits)
    """
    # 1계층: config.yaml의 기본값
    limits = dict(config.get("default_limits", {}))

    # 2계층: project.yaml에서 override
    project_limits = project_yaml.get("limits", {})
    if project_limits:
        limits.update(project_limits)

    # 3계층: project_state.json은 여기서는 생략 (WFC가 처리할 영역)

    # 4계층: task.config_override에서 override
    task_override_limits = task.get("config_override", {}).get("limits", {})
    if task_override_limits:
        limits.update(task_override_limits)

    return limits


def check_limits(limits, task, agent_type):
    """
    현재 task 상태가 limits를 초과하는지 검사한다.
    초과 시 에러 메시지 리스트를 반환한다.
    """
    errors = []
    counters = task.get("counters", {})

    # max_total_agent_invocations 체크
    max_invocations = limits.get("max_total_agent_invocations", 30)
    current_invocations = counters.get("total_agent_invocations", 0)
    if current_invocations >= max_invocations:
        errors.append(
            f"총 agent 호출 횟수 초과: {current_invocations}/{max_invocations}"
        )

    # max_retry_per_subtask 체크
    max_retry = limits.get("max_retry_per_subtask", 3)
    current_retry = counters.get("current_subtask_retry", 0)
    if current_retry >= max_retry:
        errors.append(
            f"subtask retry 횟수 초과: {current_retry}/{max_retry}"
        )

    # max_replan_count 체크
    max_replan = limits.get("max_replan_count", 2)
    current_replan = counters.get("replan_count", 0)
    if current_replan >= max_replan and agent_type == "planner":
        errors.append(
            f"replan 횟수 초과: {current_replan}/{max_replan}"
        )

    # max_subtask_count 체크
    # current_subtask가 이미 completed_subtasks에 포함된 경우(마지막 subtask가
    # 방금 완료되고 아직 current가 비워지기 전 등)에는 이중 집계하지 않는다.
    max_subtasks = limits.get("max_subtask_count", 5)
    completed_list = task.get("completed_subtasks", []) or []
    completed = len(completed_list)
    current_id = task.get("current_subtask")
    current = 1 if current_id and current_id not in completed_list else 0
    total_subtasks = completed + current
    if total_subtasks > max_subtasks:
        errors.append(
            f"subtask 개수 초과: {total_subtasks}/{max_subtasks}"
        )

    # max_task_duration_hours 체크 (사람 대기 시간 제외)
    max_hours = limits.get("max_task_duration_hours", 4)
    submitted_at = task.get("submitted_at")
    if submitted_at:
        try:
            start_time = datetime.fromisoformat(submitted_at.replace("Z", "+00:00"))
            total_elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
            human_wait = counters.get("human_wait_seconds", 0)
            active_elapsed = (total_elapsed - human_wait) / 3600
            if active_elapsed > max_hours:
                errors.append(
                    f"task 지속 시간 초과: {active_elapsed:.1f}h/{max_hours}h "
                    f"(총 {total_elapsed/3600:.1f}h - 대기 {human_wait/3600:.1f}h)"
                )
        except (ValueError, TypeError):
            pass  # 파싱 실패 시 무시

    return errors


def increment_invocation_counter(task_file_path, task):
    """
    agent 실행 전에 total_agent_invocations 카운터를 1 증가시킨다.
    """
    counters = task.get("counters", {})
    counters["total_agent_invocations"] = counters.get("total_agent_invocations", 0) + 1
    task["counters"] = counters

    with open(task_file_path, "w") as f:
        json.dump(task, f, ensure_ascii=False, indent=2)
        f.write("\n")


def main():
    parser = argparse.ArgumentParser(description="Safety limit 체크")
    parser.add_argument("--config", required=True, help="config.yaml 경로")
    parser.add_argument("--project-yaml", required=True, help="project.yaml 경로")
    parser.add_argument("--task-file", required=True, help="task JSON 경로")
    parser.add_argument("--agent-type", required=True, help="실행할 agent 타입")
    args = parser.parse_args()

    config = load_yaml(args.config)
    project_yaml = load_yaml(args.project_yaml)
    task = load_json(args.task_file)

    # effective limits 해소
    limits = resolve_effective_limits(config, project_yaml, task)

    # 한계 체크
    errors = check_limits(limits, task, args.agent_type)

    if errors:
        print("[SAFETY] ❌ 안전 제한 초과로 agent 실행을 차단합니다:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        print(f"[SAFETY] task: {task.get('task_id', 'UNKNOWN')}", file=sys.stderr)
        print(f"[SAFETY] agent: {args.agent_type}", file=sys.stderr)
        print(f"[SAFETY] effective limits: {json.dumps(limits, ensure_ascii=False)}", file=sys.stderr)
        sys.exit(1)

    # 통과 시 카운터 증가
    increment_invocation_counter(args.task_file, task)

    print(f"[SAFETY] ✓ 통과 (invocations: {task['counters']['total_agent_invocations']}/{limits.get('max_total_agent_invocations', 30)})")


if __name__ == "__main__":
    main()
