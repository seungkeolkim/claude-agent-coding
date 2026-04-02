#!/usr/bin/env python3
"""
Workflow Controller (WFC) — 단일 task의 파이프라인을 자동으로 실행한다.

Pipeline 흐름:
    Planner → subtask loop (Coder → Reviewer [→ 루프백] → Reporter [→ replan]) → 완료

사용법:
    python3 scripts/workflow_controller.py --project <name> --task <id>
    python3 scripts/workflow_controller.py --project <name> --task <id> --dummy
    python3 scripts/workflow_controller.py --project <name> --task <id> --dry-run

Phase 1.1 범위:
    - testing 전부 disabled 상태: Coder → Reviewer → (커밋 생략) → 다음 subtask
    - testing 하나라도 enabled: Coder → Reviewer → Reporter → 다음 subtask
    - git 자동화 미포함 (추후)
    - usage threshold 미포함 (추후)
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

# ─── 색상 출력 ───
GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
RED = "\033[0;31m"
CYAN = "\033[0;36m"
NC = "\033[0m"


def log_info(msg):
    print(f"{GREEN}[WFC]{NC} {msg}")


def log_warn(msg):
    print(f"{YELLOW}[WFC]{NC} {msg}")


def log_error(msg):
    print(f"{RED}[WFC]{NC} {msg}", file=sys.stderr)


def log_step(msg):
    print(f"\n{CYAN}{'═' * 60}{NC}")
    print(f"{CYAN}[WFC] {msg}{NC}")
    print(f"{CYAN}{'═' * 60}{NC}\n")


# ─── 파이프라인 단계 넘버링 (run_claude_agent.sh와 동일) ───
STEP_NUMBER = {
    "planner": "01",
    "coder": "02",
    "reviewer": "03",
    "setup": "04",
    "unit_tester": "05",
    "e2e_tester": "06",
    "reporter": "07",
}

STEP_NAME = {
    "planner": "planner",
    "coder": "coder",
    "reviewer": "reviewer",
    "setup": "setup",
    "unit_tester": "unit-tester",
    "e2e_tester": "e2e-tester",
    "reporter": "reporter",
}


def load_json(path):
    """JSON 파일을 읽어 dict로 반환한다."""
    with open(path) as f:
        return json.load(f)


def save_json(path, data):
    """dict를 JSON 파일로 저장한다."""
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def load_yaml(path):
    """YAML 파일을 읽어 dict로 반환한다."""
    with open(path) as f:
        return yaml.safe_load(f) or {}


def determine_pipeline(project_yaml):
    """
    testing 설정을 읽고 이번 subtask의 agent pipeline을 결정한다.
    testing이 전부 disabled면: [coder, reviewer] → 바로 커밋
    testing이 하나라도 enabled면: reporter 포함
    """
    testing = project_yaml.get("testing", {})
    unit_enabled = testing.get("unit_test", {}).get("enabled", False)
    e2e_enabled = testing.get("e2e_test", {}).get("enabled", False)
    integration_enabled = testing.get("integration_test", {}).get("enabled", False)

    pipeline = ["coder", "reviewer"]

    if unit_enabled or e2e_enabled or integration_enabled:
        # testing이 하나라도 enabled면 setup + 해당 tester + reporter
        pipeline.append("setup")
        if unit_enabled:
            pipeline.append("unit_tester")
        if e2e_enabled:
            pipeline.append("e2e_tester")
        pipeline.append("reporter")

    return pipeline


def run_agent(agent_hub_root, agent_type, project_name, task_id,
              subtask_id=None, dummy=False, force_result=None):
    """
    run_agent.sh를 호출하고 결과 JSON을 반환한다.
    반환값: (성공여부, 결과 dict 또는 None)
    """
    cmd = [
        os.path.join(agent_hub_root, "run_agent.sh"),
        "run", agent_type,
        "--project", project_name,
        "--task", task_id,
    ]

    if subtask_id:
        cmd.extend(["--subtask", subtask_id])

    if dummy:
        cmd.append("--dummy")

    if force_result:
        cmd.extend(["--force-result", force_result])

    log_info(f"실행: {' '.join(cmd)}")

    try:
        result = subprocess.run(
            cmd,
            cwd=agent_hub_root,
            capture_output=False,  # 터미널에 실시간 출력
            text=True,
            timeout=3600,  # 1시간 타임아웃
        )
    except subprocess.TimeoutExpired:
        log_error(f"{agent_type} 타임아웃 (1시간)")
        return False, None

    if result.returncode != 0:
        log_error(f"{agent_type} 실행 실패 (exit code: {result.returncode})")
        return False, None

    # 로그 파일에서 결과 JSON 읽기
    # 파일명 형식: run_claude_agent.sh와 동일
    #   task-level:    {task_id}_{step}-{name}.json
    #   subtask-level: {task_id}_{subtask_num}_{step}-{name}_attempt-{N}.json
    project_dir = os.path.join(agent_hub_root, "projects", project_name)
    log_dir = os.path.join(project_dir, "logs", task_id)

    step_num = STEP_NUMBER.get(agent_type, "99")
    step_name = STEP_NAME.get(agent_type, agent_type)

    if subtask_id:
        # subtask_id에서 순번 추출 (예: 00001-2 → 02)
        subtask_seq = subtask_id.split("-")[-1].zfill(2)
        # task JSON에서 retry count 읽기
        task_file = find_task_file(os.path.join(project_dir, "tasks"), task_id)
        if task_file:
            task_data = load_json(task_file)
            retry = task_data.get("counters", {}).get("current_subtask_retry", 0)
            attempt = retry + 1
        else:
            attempt = 1
        log_file = os.path.join(log_dir, f"{task_id}_{subtask_seq}_{step_num}-{step_name}_attempt-{attempt}.json")
    else:
        log_file = os.path.join(log_dir, f"{task_id}_{step_num}-{step_name}.json")

    if not os.path.exists(log_file):
        log_error(f"로그 파일을 찾을 수 없음: {log_file}")
        return False, None

    try:
        result_data = load_json(log_file)
        return True, result_data
    except (json.JSONDecodeError, Exception) as e:
        log_error(f"로그 파일 JSON 파싱 실패: {log_file} — {e}")
        return False, None


def find_task_file(tasks_dir, task_id):
    """tasks 디렉토리에서 task_id에 해당하는 파일을 glob으로 찾는다."""
    tasks_path = Path(tasks_dir)
    # task_id-*.json 패턴 먼저 시도
    matches = list(tasks_path.glob(f"{task_id}-*.json"))
    if not matches:
        # task_id.json 시도
        exact = tasks_path / f"{task_id}.json"
        if exact.exists():
            return str(exact)
        return None
    return str(matches[0])


def create_subtask_files(project_dir, task_id, plan_data):
    """
    planner 결과에서 subtask JSON 파일들을 생성한다.
    """
    tasks_dir = os.path.join(project_dir, "tasks")
    subtasks = plan_data.get("subtasks", [])

    for subtask in subtasks:
        subtask_id = subtask.get("subtask_id", "")
        subtask_file = os.path.join(tasks_dir, f"{subtask_id}.json")

        subtask_state = {
            "subtask_id": subtask_id,
            "task_id": task_id,
            "title": subtask.get("title", ""),
            "primary_responsibility": subtask.get("primary_responsibility", ""),
            "guidance": subtask.get("guidance", ""),
            "status": "pending",
            "retry_count": 0,
            "agent_results": [],
        }

        save_json(subtask_file, subtask_state)
        log_info(f"subtask 파일 생성: {subtask_file}")

    return subtasks


def save_plan_file(project_dir, task_id, plan_data):
    """plan JSON을 저장한다."""
    tasks_dir = os.path.join(project_dir, "tasks")
    plan_file = os.path.join(tasks_dir, f"{task_id}-plan.json")
    save_json(plan_file, plan_data)
    log_info(f"plan 파일 저장: {plan_file}")
    return plan_file


def update_task_counter(task_file, field, value=None, increment=False):
    """task JSON의 counters 필드를 업데이트한다."""
    task = load_json(task_file)
    counters = task.get("counters", {})

    if increment:
        counters[field] = counters.get(field, 0) + 1
    elif value is not None:
        counters[field] = value

    task["counters"] = counters
    save_json(task_file, task)
    return task


def update_task_field(task_file, field, value):
    """task JSON의 최상위 필드를 업데이트한다."""
    task = load_json(task_file)
    task[field] = value
    save_json(task_file, task)
    return task


def run_pipeline(args):
    """메인 파이프라인 실행 로직."""
    agent_hub_root = str(Path(__file__).resolve().parent.parent)
    project_dir = os.path.join(agent_hub_root, "projects", args.project)
    tasks_dir = os.path.join(project_dir, "tasks")
    project_yaml_path = os.path.join(project_dir, "project.yaml")

    # 프로젝트 설정 로드
    if not os.path.exists(project_yaml_path):
        log_error(f"project.yaml을 찾을 수 없음: {project_yaml_path}")
        sys.exit(1)

    project_yaml = load_yaml(project_yaml_path)

    # task 파일 찾기
    task_file = find_task_file(tasks_dir, args.task)
    if not task_file:
        log_error(f"task 파일을 찾을 수 없음: {tasks_dir}/{args.task}[-*].json")
        sys.exit(1)

    task = load_json(task_file)
    task_id = task.get("task_id", args.task)

    log_step(f"파이프라인 시작: project={args.project} task={task_id}")

    # pipeline 구성 결정
    pipeline = determine_pipeline(project_yaml)
    log_info(f"pipeline 구성: {' → '.join(pipeline)}")

    if args.dry_run:
        log_warn("DRY-RUN: 실제 실행 없이 pipeline 구성만 확인")
        return

    # ─── Phase 1: Planner ───
    log_step("Phase 1: Planner 실행")

    success, plan_data = run_agent(
        agent_hub_root, "planner", args.project, task_id,
        dummy=args.dummy,
    )

    if not success or not plan_data:
        log_error("Planner 실패. 파이프라인 중단.")
        update_task_field(task_file, "status", "failed")
        sys.exit(1)

    # plan 저장 및 subtask 파일 생성
    save_plan_file(project_dir, task_id, plan_data)
    subtasks = create_subtask_files(project_dir, task_id, plan_data)

    if not subtasks:
        log_error("Planner가 subtask를 생성하지 않았습니다.")
        update_task_field(task_file, "status", "failed")
        sys.exit(1)

    log_info(f"subtask {len(subtasks)}개 생성됨")

    # ─── Phase 2: Subtask Loop ───
    completed_subtasks = []

    for i, subtask in enumerate(subtasks):
        subtask_id = subtask.get("subtask_id", f"{task_id}-{i+1}")

        log_step(f"Subtask {i+1}/{len(subtasks)}: {subtask_id}")

        # task에 현재 subtask 기록
        update_task_field(task_file, "current_subtask", subtask_id)
        update_task_counter(task_file, "current_subtask_retry", value=0)

        # subtask pipeline 실행
        subtask_success = run_subtask_pipeline(
            agent_hub_root, args.project, task_id, subtask_id,
            task_file, pipeline, args.dummy,
        )

        if not subtask_success:
            # replan 필요 여부 확인
            task = load_json(task_file)
            if task.get("_needs_replan", False):
                # replan 카운터 확인 (safety limits가 체크하지만 여기서도 확인)
                replan_count = task["counters"].get("replan_count", 0)
                log_warn(f"replan 요청 (현재 {replan_count}회)")

                update_task_counter(task_file, "replan_count", increment=True)
                update_task_field(task_file, "_needs_replan", False)

                # Planner 재실행
                log_step("Re-plan: Planner 재실행")
                success, plan_data = run_agent(
                    agent_hub_root, "planner", args.project, task_id,
                    dummy=args.dummy,
                )

                if not success or not plan_data:
                    log_error("Re-plan 실패. 파이프라인 중단.")
                    update_task_field(task_file, "status", "failed")
                    sys.exit(1)

                save_plan_file(project_dir, task_id, plan_data)
                new_subtasks = create_subtask_files(project_dir, task_id, plan_data)

                # 새 plan의 subtask로 루프 재시작 (재귀 대신 함수 재호출)
                log_info(f"새 plan으로 subtask {len(new_subtasks)}개 재구성")
                subtasks_remaining = new_subtasks
                # 이미 완료된 subtask는 건너뜀
                update_task_field(task_file, "completed_subtasks", completed_subtasks)

                # 간단히 재귀 호출 대신 루프를 계속하기 위해 subtask 리스트 교체
                # 현재 구조에서는 re-plan 후 전체 함수를 다시 돌리는 것이 깔끔
                # 단, 무한 replan은 safety limits가 막아줌
                log_warn("re-plan 후 파이프라인을 처음부터 재시작합니다")
                run_pipeline_from_subtasks(
                    agent_hub_root, args.project, task_id, task_file,
                    new_subtasks, pipeline, args.dummy, completed_subtasks,
                )
                return
            else:
                log_error(f"subtask {subtask_id} 실패. 파이프라인 중단.")
                update_task_field(task_file, "status", "failed")
                sys.exit(1)

        completed_subtasks.append(subtask_id)
        update_task_field(task_file, "completed_subtasks", completed_subtasks)
        log_info(f"subtask {subtask_id} 완료 ({i+1}/{len(subtasks)})")

    # ─── Phase 3: 완료 ───
    log_step("파이프라인 완료")
    update_task_field(task_file, "status", "completed")
    update_task_field(task_file, "current_subtask", None)
    log_info(f"task {task_id} 완료. subtask {len(completed_subtasks)}개 처리됨.")


def run_pipeline_from_subtasks(agent_hub_root, project_name, task_id, task_file,
                                subtasks, pipeline, dummy, already_completed):
    """re-plan 후 남은 subtask들을 실행한다."""
    completed_subtasks = list(already_completed)

    for i, subtask in enumerate(subtasks):
        subtask_id = subtask.get("subtask_id", f"{task_id}-{i+1}")

        # 이미 완료된 subtask는 건너뜀
        if subtask_id in completed_subtasks:
            log_info(f"subtask {subtask_id} 이미 완료됨 — 건너뜀")
            continue

        log_step(f"Subtask (re-plan): {subtask_id}")

        update_task_field(task_file, "current_subtask", subtask_id)
        update_task_counter(task_file, "current_subtask_retry", value=0)

        subtask_success = run_subtask_pipeline(
            agent_hub_root, project_name, task_id, subtask_id,
            task_file, pipeline, dummy,
        )

        if not subtask_success:
            task = load_json(task_file)
            if task.get("_needs_replan", False):
                log_error("re-plan 후에도 실패. 에스컬레이션이 필요합니다.")
            update_task_field(task_file, "status", "failed")
            sys.exit(1)

        completed_subtasks.append(subtask_id)
        update_task_field(task_file, "completed_subtasks", completed_subtasks)

    log_step("파이프라인 완료 (re-plan 후)")
    update_task_field(task_file, "status", "completed")
    update_task_field(task_file, "current_subtask", None)
    log_info(f"task {task_id} 완료. subtask {len(completed_subtasks)}개 처리됨.")


def run_subtask_pipeline(agent_hub_root, project_name, task_id, subtask_id,
                          task_file, pipeline, dummy):
    """
    단일 subtask에 대해 pipeline을 실행한다.
    성공 시 True, 실패 시 False 반환.
    """
    for agent_type in pipeline:
        log_info(f"[{subtask_id}] {agent_type} 실행")

        success, result = run_agent(
            agent_hub_root, agent_type, project_name, task_id,
            subtask_id=subtask_id,
            dummy=dummy,
        )

        if not success:
            log_error(f"[{subtask_id}] {agent_type} 실행 실패")
            return False

        # 결과에 따른 분기
        action = result.get("action", "")

        # ─── Reviewer 거절 → Coder 루프백 ───
        if agent_type == "reviewer" and action == "rejected":
            log_warn(f"[{subtask_id}] reviewer 거절 — Coder 루프백")

            # retry 카운터 증가
            task = update_task_counter(task_file, "current_subtask_retry", increment=True)
            retry_count = task["counters"]["current_subtask_retry"]

            # safety limits는 run_claude_agent.sh에서 체크하므로 여기서는 로그만
            log_info(f"[{subtask_id}] retry {retry_count}회")

            # Coder부터 재실행 (재귀)
            return run_subtask_pipeline(
                agent_hub_root, project_name, task_id, subtask_id,
                task_file, pipeline, dummy,
            )

        # ─── Reporter: needs_replan ───
        if agent_type == "reporter" and result.get("needs_replan", False):
            log_warn(f"[{subtask_id}] reporter가 re-plan 요청")
            update_task_field(task_file, "_needs_replan", True)
            return False

        # ─── Reporter: 실패 (retry 가능) ───
        if agent_type == "reporter" and result.get("verdict") == "fail":
            log_warn(f"[{subtask_id}] reporter 실패 판정 — Coder 루프백")

            task = update_task_counter(task_file, "current_subtask_retry", increment=True)
            retry_count = task["counters"]["current_subtask_retry"]
            log_info(f"[{subtask_id}] retry {retry_count}회")

            return run_subtask_pipeline(
                agent_hub_root, project_name, task_id, subtask_id,
                task_file, pipeline, dummy,
            )

    log_info(f"[{subtask_id}] pipeline 완료")
    return True


def main():
    parser = argparse.ArgumentParser(description="Workflow Controller — 파이프라인 자동 실행")
    parser.add_argument("--project", required=True, help="프로젝트명")
    parser.add_argument("--task", required=True, help="task ID (5자리 숫자)")
    parser.add_argument("--dummy", action="store_true", help="모든 agent를 dummy 모드로 실행")
    parser.add_argument("--dry-run", action="store_true", help="pipeline 구성만 확인 (실행 안 함)")
    args = parser.parse_args()

    run_pipeline(args)


if __name__ == "__main__":
    main()
