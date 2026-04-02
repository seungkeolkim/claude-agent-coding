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
    - testing 전부 disabled 상태: Coder → Reviewer → git commit → 다음 subtask
    - testing 하나라도 enabled: Coder → Reviewer → Reporter → git commit → 다음 subtask
    - git 자동화: task별 브랜치 생성, subtask별 커밋, auto_merge 시 머지
    - usage threshold 미포함 (추후)
"""

import argparse
import glob as glob_module
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import yaml

# ─── 색상 출력 (터미널용) ───
GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
RED = "\033[0;31m"
CYAN = "\033[0;36m"
NC = "\033[0m"

# ─── 로거 ───
# 파일 로거는 setup_file_logger()에서 초기화된다 (project_dir 필요)
_file_logger = None


def setup_file_logger(project_dir):
    """
    WFC rotation 파일 로거를 초기화한다.
    로그 위치: projects/{name}/logs/wfc.log
    100MB x 최대 10개 rotation.
    """
    global _file_logger
    log_dir = os.path.join(project_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)

    _file_logger = logging.getLogger("wfc")
    _file_logger.setLevel(logging.DEBUG)

    # 기존 핸들러 제거 (중복 방지)
    _file_logger.handlers.clear()

    handler = RotatingFileHandler(
        os.path.join(log_dir, "wfc.log"),
        maxBytes=100 * 1024 * 1024,  # 100MB
        backupCount=10,
        encoding="utf-8",
    )
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    _file_logger.addHandler(handler)


def _log_to_file(level, msg):
    """파일 로거가 초기화되어 있으면 기록한다."""
    if _file_logger:
        _file_logger.log(level, msg)


def log_info(msg):
    print(f"{GREEN}[WFC]{NC} {msg}")
    _log_to_file(logging.INFO, msg)


def log_warn(msg):
    print(f"{YELLOW}[WFC]{NC} {msg}")
    _log_to_file(logging.WARNING, msg)


def log_error(msg):
    print(f"{RED}[WFC]{NC} {msg}", file=sys.stderr)
    _log_to_file(logging.ERROR, msg)


def log_step(msg):
    print(f"\n{CYAN}{'═' * 60}{NC}")
    print(f"{CYAN}[WFC] {msg}{NC}")
    print(f"{CYAN}{'═' * 60}{NC}\n")
    _log_to_file(logging.INFO, f"{'═' * 40} {msg} {'═' * 40}")


# ─── 파이프라인 단계 넘버링 (run_claude_agent.sh와 동일) ───
STEP_NUMBER = {
    "planner": "01",
    "coder": "02",
    "reviewer": "03",
    "setup": "04",
    "unit_tester": "05",
    "e2e_tester": "06",
    "reporter": "07",
    "summarizer": "08",
}

STEP_NAME = {
    "planner": "planner",
    "coder": "coder",
    "reviewer": "reviewer",
    "setup": "setup",
    "unit_tester": "unit-tester",
    "e2e_tester": "e2e-tester",
    "reporter": "reporter",
    "summarizer": "summarizer",
}


def load_json(path):
    """JSON 파일을 읽어 dict로 반환한다."""
    with open(path) as f:
        return json.load(f)


def extract_agent_result(raw):
    """
    claude -p --output-format json의 wrapper에서 실제 agent 결과를 추출한다.

    claude -p 출력 구조:
      { "type": "result", "result": "... agent 텍스트 응답 ...", ... }
    agent 응답 안에 ```json ... ``` 코드블록이 있으면 그 JSON을 파싱하여 반환.
    코드블록이 없으면 result 텍스트 전체를 JSON 파싱 시도.
    wrapper가 아니면(dummy 등) 그대로 반환.
    """
    # dummy 모드 등 wrapper가 아닌 경우: action 키가 이미 있으면 그대로 반환
    if "action" in raw or "subtasks" in raw:
        return raw

    # claude -p wrapper인 경우: result 필드에서 추출
    result_text = raw.get("result", "")
    if not result_text:
        return raw

    # ```json ... ``` 코드블록 추출
    json_block_match = re.search(r"```json\s*\n(.*?)\n```", result_text, re.DOTALL)
    if json_block_match:
        return json.loads(json_block_match.group(1))

    # 코드블록 없으면 텍스트 전체를 JSON으로 파싱 시도
    try:
        return json.loads(result_text)
    except json.JSONDecodeError:
        # JSON이 아닌 순수 텍스트 응답 — 그대로 dict 감싸서 반환
        return {"result_text": result_text}


def save_json(path, data):
    """dict를 JSON 파일로 저장한다."""
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def load_yaml(path):
    """YAML 파일을 읽어 dict로 반환한다."""
    with open(path) as f:
        return yaml.safe_load(f) or {}


# ─── 4계층 설정 merge ───


def _deep_merge(base, override):
    """
    base dict 위에 override dict를 재귀적으로 덮어쓴다.
    override에 없는 키는 base 값을 유지한다.
    """
    merged = dict(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def resolve_effective_config(config, project_yaml, project_state, task):
    """
    4계층 설정 merge를 수행하여 effective config를 생성한다.

    계층 순서 (뒤의 것이 앞의 것을 덮어씀):
      1. config.yaml (시스템 기본값)
      2. project.yaml (프로젝트 정적 설정)
      3. project_state.json의 overrides (프로젝트 동적 설정)
      4. task.config_override (task 단위 일시 변경)

    반환: 최종 effective config dict
    키 구조: testing, git, claude, limits, human_review_policy, notification 등
    """
    # 1계층: config.yaml 시스템 기본값
    # config.yaml은 default_limits, default_human_review_policy 키를 사용
    effective = {}
    effective["claude"] = dict(config.get("claude", {}))
    effective["limits"] = dict(config.get("default_limits", {}))
    effective["human_review_policy"] = dict(config.get("default_human_review_policy", {}))
    effective["notification"] = dict(config.get("notification", {}))
    effective["logging"] = dict(config.get("logging", {}))
    # testing, git, codebase, project는 config.yaml에 기본값 없음 (프로젝트별 설정)
    effective["testing"] = {}
    effective["git"] = {}
    effective["codebase"] = {}
    effective["project"] = {}

    # 2계층: project.yaml 정적 설정으로 덮어쓰기
    for key in ["testing", "git", "codebase", "project", "claude",
                "limits", "human_review_policy", "notification"]:
        project_value = project_yaml.get(key, {})
        if project_value:
            effective[key] = _deep_merge(effective[key], project_value)

    # 3계층: project_state.json의 overrides로 덮어쓰기
    overrides = project_state.get("overrides", {})
    for key, value in overrides.items():
        if key in effective and isinstance(value, dict):
            effective[key] = _deep_merge(effective[key], value)
        else:
            effective[key] = value

    # 4계층: task.config_override로 덮어쓰기
    task_overrides = task.get("config_override", {})
    for key, value in task_overrides.items():
        if key in effective and isinstance(value, dict):
            effective[key] = _deep_merge(effective[key], value)
        else:
            effective[key] = value

    return effective


# ─── project_state.json 헬퍼 ───


def update_project_state(project_dir, status, current_task_id=None, last_error=None):
    """
    project_state.json의 WFC 관련 필드를 갱신한다.
    기존 파일이 있으면 merge, 없으면 새로 생성한다.
    TM이 이 파일을 읽어 프로젝트 상태를 파악한다.
    """
    state_path = os.path.join(project_dir, "project_state.json")

    if os.path.exists(state_path):
        try:
            state = load_json(state_path)
        except (json.JSONDecodeError, OSError):
            state = {"project_name": os.path.basename(project_dir)}
    else:
        state = {"project_name": os.path.basename(project_dir)}

    state["status"] = status
    state["current_task_id"] = current_task_id
    state["last_updated"] = datetime.now(timezone.utc).isoformat()

    if last_error:
        state["last_error_task_id"] = last_error

    save_json(state_path, state)
    log_info(f"project_state.json 갱신: status={status}, task={current_task_id}")


# ─── human_review_policy 헬퍼 ───


def request_human_review(task_file, task_id, review_type, plan_path, subtask_count):
    """
    task JSON에 human_interaction을 기록하고 status를 waiting_for_human으로 변경한다.
    review_type: "plan_review" | "replan_review"
    """
    task = load_json(task_file)

    task["status"] = "waiting_for_human"
    task["human_interaction"] = {
        "type": review_type,
        "message": f"Plan을 확인해주세요. subtask {subtask_count}개 생성됨.",
        "payload_path": plan_path,
        "options": ["approve", "modify", "cancel"],
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "response": None,
    }

    save_json(task_file, task)
    log_info(f"human review 요청: type={review_type}, task={task_id}")


def wait_for_human_response(task_file, project_dir, task_id, timeout_hours,
                            poll_interval=10):
    """
    task JSON의 human_interaction.response가 채워질 때까지 폴링한다.
    timeout_hours 초과 시 자동 승인.
    commands/ 디렉토리의 cancel 명령도 감시한다.

    반환: "approve" | "modify" | "cancel" | "timeout"
    """
    # project_state.json에 waiting_for_human 상태 기록
    update_project_state(project_dir, status="waiting_for_human",
                         current_task_id=task_id)

    start_time = time.time()
    timeout_seconds = timeout_hours * 3600

    log_step(f"사용자 응답 대기 중 (timeout: {timeout_hours}h)")
    log_info("승인: ./run_agent.sh approve {task_id} --project {project}")
    log_info("거부: ./run_agent.sh reject {task_id} --project {project} --message '사유'")

    commands_dir = os.path.join(project_dir, "commands")

    while True:
        elapsed = time.time() - start_time

        # 타임아웃 확인
        if elapsed >= timeout_seconds:
            log_warn(f"자동 승인 (timeout {timeout_hours}h 초과)")
            # 자동 승인 기록
            task = load_json(task_file)
            hi = task.get("human_interaction", {})
            if hi and not hi.get("response"):
                hi["response"] = {
                    "action": "approve",
                    "message": f"자동 승인 (timeout {timeout_hours}h 초과)",
                    "attachments": [],
                    "responded_at": datetime.now(timezone.utc).isoformat(),
                }
                task["human_interaction"] = hi
                task["status"] = "planned"
                save_json(task_file, task)
            return "timeout"

        # cancel 명령 확인
        if os.path.isdir(commands_dir):
            cancel_pattern = os.path.join(commands_dir, f"cancel-{task_id}.command")
            cancel_files = glob_module.glob(cancel_pattern)
            if cancel_files:
                log_warn(f"cancel 명령 감지: task {task_id}")
                for cf in cancel_files:
                    os.unlink(cf)
                task = load_json(task_file)
                task["status"] = "cancelled"
                save_json(task_file, task)
                return "cancel"

        # 응답 확인
        task = load_json(task_file)
        hi = task.get("human_interaction", {})
        response = hi.get("response") if hi else None

        if response:
            action = response.get("action", "approve")
            log_info(f"사용자 응답 수신: action={action}")

            if action == "approve":
                # hub_api가 이미 status를 "planned"로 변경했음
                return "approve"
            elif action == "modify":
                # hub_api가 이미 status를 "needs_replan"으로 변경했음
                return "modify"
            elif action == "cancel":
                return "cancel"
            else:
                log_warn(f"알 수 없는 action: {action}, approve로 처리")
                return "approve"

        # 대기
        time.sleep(poll_interval)


# ─── Git 헬퍼 ───


def ensure_gh_auth(token, codebase_path=None):
    """
    gh CLI 인증 상태를 확인하고, 필요하면 token으로 로그인한다.
    codebase_path가 주어지면 해당 repo에 대한 접근 권한도 확인한다.
    git 관련 작업 전에 매번 호출해야 한다.
    """
    # 1. gh 인증 상태 확인
    check = subprocess.run(
        ["gh", "auth", "status"],
        capture_output=True, text=True,
    )
    if check.returncode != 0:
        if not token:
            log_error("[git] gh CLI 미인증 상태이고 auth_token이 설정되지 않았습니다.")
            raise RuntimeError("gh 인증 필요: project.yaml의 git.auth_token을 설정하세요.")
        log_info("[git] gh CLI 인증 시도...")
        result = subprocess.run(
            ["gh", "auth", "login", "--with-token"],
            input=token, capture_output=True, text=True,
        )
        if result.returncode != 0:
            log_error(f"[git] gh 인증 실패: {result.stderr.strip()}")
            raise RuntimeError("gh auth login 실패")
        log_info("[git] gh CLI 인증 완료")

    # 2. repo 접근 권한 확인
    if codebase_path:
        repo_check = subprocess.run(
            ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
            cwd=codebase_path, capture_output=True, text=True,
        )
        if repo_check.returncode != 0:
            log_error(f"[git] repo 접근 실패: {repo_check.stderr.strip()}")
            raise RuntimeError("gh repo 접근 권한 없음. token 권한을 확인하세요.")
        repo_name = repo_check.stdout.strip()
        log_info(f"[git] gh 인증 확인됨 (repo: {repo_name})")


def git_run(codebase_path, *args):
    """codebase 디렉토리에서 git 명령을 실행하고 stdout을 반환한다."""
    cmd = ["git"] + list(args)
    log_info(f"[git] {' '.join(cmd)}")
    result = subprocess.run(
        cmd, cwd=codebase_path,
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log_error(f"[git] 실패: {result.stderr.strip()}")
        raise RuntimeError(f"git 명령 실패: {' '.join(cmd)}\n{result.stderr.strip()}")
    return result.stdout.strip()


def git_has_changes(codebase_path):
    """커밋할 변경사항이 있는지 확인한다."""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=codebase_path, capture_output=True, text=True,
    )
    return bool(result.stdout.strip())


def git_create_task_branch(codebase_path, branch_name, default_branch):
    """
    task용 feature 브랜치를 생성하고 checkout한다.
    이미 존재하면 checkout만 한다.
    """
    # 이미 존재하는 브랜치인지 확인
    existing = subprocess.run(
        ["git", "branch", "--list", branch_name],
        cwd=codebase_path, capture_output=True, text=True,
    )
    if existing.stdout.strip():
        log_info(f"[git] 기존 브랜치로 checkout: {branch_name}")
        git_run(codebase_path, "checkout", branch_name)
    else:
        log_info(f"[git] 새 브랜치 생성: {branch_name} (from {default_branch})")
        git_run(codebase_path, "checkout", "-b", branch_name, default_branch)

    return branch_name


def git_push(codebase_path, remote, branch=None):
    """현재 브랜치를 원격에 push한다. upstream이 없으면 자동 설정."""
    if branch:
        git_run(codebase_path, "push", "--set-upstream", remote, branch)
    else:
        git_run(codebase_path, "push", remote)
    log_info(f"[git] push 완료: {remote} {branch or ''}")


def git_commit_subtask(codebase_path, task_id, subtask_id, subtask_title,
                       author_name, author_email, remote=None, branch=None):
    """
    subtask 완료 후 변경사항을 커밋하고 push한다.
    변경사항이 없으면 스킵한다.
    """
    if not git_has_changes(codebase_path):
        log_info(f"[git] 변경사항 없음 — 커밋 스킵 ({subtask_id})")
        return False

    git_run(codebase_path, "add", "-A")
    commit_msg = f"[{task_id}] {subtask_id}: {subtask_title}"
    git_run(
        codebase_path, "commit",
        "-m", commit_msg,
        "--author", f"{author_name} <{author_email}>",
    )
    log_info(f"[git] 커밋 완료: {commit_msg}")

    if remote:
        git_push(codebase_path, remote, branch)

    return True


def git_create_pr(codebase_path, task_branch, target_branch, pr_title, pr_body):
    """GitHub PR을 생성하고 PR URL을 반환한다."""
    cmd = [
        "gh", "pr", "create",
        "--title", pr_title,
        "--body", pr_body,
        "--base", target_branch,
        "--head", task_branch,
    ]
    log_info(f"[git] PR 생성: {task_branch} → {target_branch}")
    result = subprocess.run(
        cmd, cwd=codebase_path,
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log_error(f"[git] PR 생성 실패: {result.stderr.strip()}")
        raise RuntimeError(f"gh pr create 실패: {result.stderr.strip()}")
    pr_url = result.stdout.strip()
    log_info(f"[git] PR 생성 완료: {pr_url}")
    return pr_url


def git_merge_pr(codebase_path, pr_url):
    """GitHub PR을 머지한다."""
    # TODO: merge conflict 에러 처리 (Phase 2에서 구현)
    cmd = ["gh", "pr", "merge", pr_url, "--merge", "--delete-branch"]
    log_info(f"[git] PR 머지: {pr_url}")
    result = subprocess.run(
        cmd, cwd=codebase_path,
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log_error(f"[git] PR 머지 실패: {result.stderr.strip()}")
        raise RuntimeError(f"gh pr merge 실패: {result.stderr.strip()}")
    log_info("[git] PR 머지 완료")


def determine_pipeline(effective_config):
    """
    effective config의 testing 설정을 읽고 이번 subtask의 agent pipeline을 결정한다.
    testing이 전부 disabled면: [coder, reviewer] → 바로 커밋
    testing이 하나라도 enabled면: reporter 포함
    """
    testing = effective_config.get("testing", {})
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
        # task-level agent: planner=00, 그 외=99
        task_level_seq = "00" if agent_type == "planner" else "99"
        log_file = os.path.join(log_dir, f"{task_id}_{task_level_seq}_{step_num}-{step_name}.json")

    if not os.path.exists(log_file):
        log_error(f"로그 파일을 찾을 수 없음: {log_file}")
        return False, None

    try:
        raw = load_json(log_file)
        result_data = extract_agent_result(raw)
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


def get_task_internal_dir(project_dir, task_id):
    """WFC 내부 산출물 디렉토리 경로를 반환하고, 없으면 생성한다."""
    internal_dir = os.path.join(project_dir, "tasks", task_id)
    os.makedirs(internal_dir, exist_ok=True)
    return internal_dir


def create_subtask_files(project_dir, task_id, plan_data):
    """
    planner 결과에서 subtask JSON 파일들을 생성한다.
    tasks/{task_id}/subtask-{순번}.json 형식으로 저장.
    """
    internal_dir = get_task_internal_dir(project_dir, task_id)
    subtasks = plan_data.get("subtasks", [])

    for i, subtask in enumerate(subtasks):
        subtask_id = subtask.get("subtask_id", "")
        seq = str(i + 1).zfill(2)
        subtask_file = os.path.join(internal_dir, f"subtask-{seq}.json")

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
    """plan JSON을 tasks/{task_id}/plan.json에 저장한다."""
    internal_dir = get_task_internal_dir(project_dir, task_id)
    plan_file = os.path.join(internal_dir, "plan.json")
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
    config_yaml_path = os.path.join(agent_hub_root, "config.yaml")

    # 파일 로거 초기화 (rotation: 100MB x 10)
    setup_file_logger(project_dir)

    # 설정 파일 로드
    if not os.path.exists(project_yaml_path):
        log_error(f"project.yaml을 찾을 수 없음: {project_yaml_path}")
        sys.exit(1)

    project_yaml = load_yaml(project_yaml_path)

    config = {}
    if os.path.exists(config_yaml_path):
        config = load_yaml(config_yaml_path)
    else:
        log_warn(f"config.yaml을 찾을 수 없음: {config_yaml_path} — 시스템 기본값 없이 진행")

    # project_state.json 로드 (overrides 등)
    state_path = os.path.join(project_dir, "project_state.json")
    project_state = {}
    if os.path.exists(state_path):
        try:
            project_state = load_json(state_path)
        except (json.JSONDecodeError, OSError):
            log_warn("project_state.json 파싱 실패 — overrides 없이 진행")

    # task 파일 찾기
    task_file = find_task_file(tasks_dir, args.task)
    if not task_file:
        log_error(f"task 파일을 찾을 수 없음: {tasks_dir}/{args.task}[-*].json")
        sys.exit(1)

    task = load_json(task_file)
    task_id = task.get("task_id", args.task)

    log_step(f"파이프라인 시작: project={args.project} task={task_id}")

    # project_state.json 갱신 (TM 연동용)
    update_project_state(project_dir, status="running", current_task_id=task_id)

    # 4계층 설정 merge → effective config
    effective = resolve_effective_config(config, project_yaml, project_state, task)
    log_info(f"effective config 생성 완료 (4계층 merge)")

    # pipeline 구성 결정
    pipeline = determine_pipeline(effective)
    log_info(f"pipeline 구성: {' → '.join(pipeline)}")

    if args.dry_run:
        log_warn("DRY-RUN: 실제 실행 없이 pipeline 구성만 확인")
        return

    # ─── Git 설정 로드 (effective config에서) ───
    git_config = effective.get("git", {})
    git_enabled = git_config.get("enabled", False) and not args.dummy
    codebase_path = effective.get("codebase", {}).get("path", "")
    default_branch = effective.get("project", {}).get("default_branch", "main")
    task_branch = None

    # ─── Git provider 인증 ───
    if git_enabled:
        git_provider = git_config.get("provider", "github")
        auth_token = git_config.get("auth_token", "")
        if git_provider == "github":
            ensure_gh_auth(auth_token, codebase_path=codebase_path)
        elif git_provider != "github":
            log_warn(f"[git] provider '{git_provider}'는 아직 미구현. github만 지원합니다.")

    if args.dummy:
        log_info("DUMMY 모드: git 작업 스킵")

    # ─── Phase 1: Planner ───
    log_step("Phase 1: Planner 실행")

    success, plan_data = run_agent(
        agent_hub_root, "planner", args.project, task_id,
        dummy=args.dummy,
    )

    if not success or not plan_data:
        log_error("Planner 실패. 파이프라인 중단.")
        update_task_field(task_file, "status", "failed")
        update_project_state(project_dir, status="idle", last_error=task_id)
        sys.exit(1)

    # plan 저장 및 subtask 파일 생성
    save_plan_file(project_dir, task_id, plan_data)
    subtasks = create_subtask_files(project_dir, task_id, plan_data)

    if not subtasks:
        log_error("Planner가 subtask를 생성하지 않았습니다.")
        update_task_field(task_file, "status", "failed")
        update_project_state(project_dir, status="idle", last_error=task_id)
        sys.exit(1)

    log_info(f"subtask {len(subtasks)}개 생성됨")

    # ─── Human Review: Plan 승인 대기 ───
    human_review = effective.get("human_review_policy", {})
    if human_review.get("review_plan", False) and not args.dummy:
        plan_path = os.path.join("tasks", task_id, "plan.json")
        request_human_review(task_file, task_id, "plan_review", plan_path, len(subtasks))

        timeout_hours = human_review.get("auto_approve_timeout_hours", 24)
        result = wait_for_human_response(
            task_file, project_dir, task_id, timeout_hours,
        )

        if result == "cancel":
            log_warn("사용자가 plan을 취소했습니다.")
            update_task_field(task_file, "status", "cancelled")
            update_project_state(project_dir, status="idle")
            sys.exit(0)
        elif result == "modify":
            log_warn("사용자가 plan 수정을 요청했습니다. replan 실행.")
            # modify 시 needs_replan으로 설정됨 — planner 재실행
            update_task_counter(task_file, "replan_count", increment=True)
            success, plan_data = run_agent(
                agent_hub_root, "planner", args.project, task_id,
                dummy=args.dummy,
            )
            if not success or not plan_data:
                log_error("Re-plan 실패. 파이프라인 중단.")
                update_task_field(task_file, "status", "failed")
                update_project_state(project_dir, status="idle", last_error=task_id)
                sys.exit(1)
            save_plan_file(project_dir, task_id, plan_data)
            subtasks = create_subtask_files(project_dir, task_id, plan_data)
            if not subtasks:
                log_error("Re-plan 후 subtask가 없습니다.")
                update_task_field(task_file, "status", "failed")
                update_project_state(project_dir, status="idle", last_error=task_id)
                sys.exit(1)
            log_info(f"re-plan 완료: subtask {len(subtasks)}개 재생성")

        # 승인 후 running 상태로 복귀
        update_project_state(project_dir, status="running", current_task_id=task_id)
        update_task_field(task_file, "status", "in_progress")

    # ─── Git: task 브랜치 생성 (Planner 후) ───
    if git_enabled:
        log_step("Git: task 브랜치 생성")
        # 우선순위: task JSON branch_name > planner 출력 branch_name > fallback
        branch_name = (
            task.get("branch_name")
            or plan_data.get("branch_name")
            or f"feature/{task_id}"
        )
        # feature/{task_id}- 접두사 보장
        # planner가 "feature/00003-temperature-converter" 등을 줄 수 있음
        # 접두사가 없으면 자동 추가
        prefix = f"feature/{task_id}-"
        if not branch_name.startswith(prefix):
            # "feature/"만 있는 경우 (예: "feature/temperature-converter")
            if branch_name.startswith("feature/"):
                suffix = branch_name[len("feature/"):]
                # task_id로 시작하면 그대로, 아니면 task_id- 추가
                if not suffix.startswith(task_id):
                    branch_name = f"feature/{task_id}-{suffix}"
            else:
                branch_name = f"feature/{task_id}-{branch_name}"
        task_branch = git_create_task_branch(codebase_path, branch_name, default_branch)
        update_task_field(task_file, "branch", task_branch)

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
                    update_project_state(project_dir, status="idle", last_error=task_id)
                    sys.exit(1)

                save_plan_file(project_dir, task_id, plan_data)
                new_subtasks = create_subtask_files(project_dir, task_id, plan_data)

                # ─── Human Review: Replan 승인 대기 ───
                if human_review.get("review_replan", False) and not args.dummy:
                    plan_path = os.path.join("tasks", task_id, "plan.json")
                    request_human_review(
                        task_file, task_id, "replan_review",
                        plan_path, len(new_subtasks),
                    )
                    timeout_hours = human_review.get("auto_approve_timeout_hours", 24)
                    replan_result = wait_for_human_response(
                        task_file, project_dir, task_id, timeout_hours,
                    )
                    if replan_result == "cancel":
                        log_warn("사용자가 replan을 취소했습니다.")
                        update_task_field(task_file, "status", "cancelled")
                        update_project_state(project_dir, status="idle")
                        sys.exit(0)
                    elif replan_result == "modify":
                        log_error("replan 후 재수정 요청. 에스컬레이션이 필요합니다.")
                        update_task_field(task_file, "status", "escalated")
                        update_project_state(project_dir, status="idle", last_error=task_id)
                        sys.exit(1)
                    # approve/timeout → 계속 진행
                    update_project_state(project_dir, status="running",
                                         current_task_id=task_id)
                    update_task_field(task_file, "status", "in_progress")

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
                    git_enabled=git_enabled, git_config=git_config,
                    codebase_path=codebase_path, task_branch=task_branch,
                    default_branch=default_branch,
                )
                return
            else:
                log_error(f"subtask {subtask_id} 실패. 파이프라인 중단.")
                update_task_field(task_file, "status", "failed")
                update_project_state(project_dir, status="idle", last_error=task_id)
                sys.exit(1)

        # ─── Git: subtask 커밋 + push ───
        if git_enabled:
            git_remote = git_config.get("remote", "origin")
            subtask_title = subtask.get("title", subtask_id)
            git_commit_subtask(
                codebase_path, task_id, subtask_id, subtask_title,
                git_config.get("author_name", "Agent Hub"),
                git_config.get("author_email", "agent@hub"),
                remote=git_remote, branch=task_branch,
            )

        completed_subtasks.append(subtask_id)
        update_task_field(task_file, "completed_subtasks", completed_subtasks)
        log_info(f"subtask {subtask_id} 완료 ({i+1}/{len(subtasks)})")

    # ─── Phase 3: Summarizer + PR ───
    finalize_task(
        agent_hub_root, args.project, task_id, task_file,
        completed_subtasks, git_enabled, git_config,
        codebase_path, task_branch, default_branch, args.dummy,
    )


def run_pipeline_from_subtasks(agent_hub_root, project_name, task_id, task_file,
                                subtasks, pipeline, dummy, already_completed,
                                git_enabled=False, git_config=None,
                                codebase_path=None, task_branch=None,
                                default_branch="main"):
    """re-plan 후 남은 subtask들을 실행한다."""
    completed_subtasks = list(already_completed)
    git_config = git_config or {}

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
            project_dir = os.path.join(agent_hub_root, "projects", project_name)
            update_project_state(project_dir, status="idle", last_error=task_id)
            sys.exit(1)

        # ─── Git: subtask 커밋 + push ───
        if git_enabled:
            git_remote = git_config.get("remote", "origin")
            subtask_title = subtask.get("title", subtask_id)
            git_commit_subtask(
                codebase_path, task_id, subtask_id, subtask_title,
                git_config.get("author_name", "Agent Hub"),
                git_config.get("author_email", "agent@hub"),
                remote=git_remote, branch=task_branch,
            )

        completed_subtasks.append(subtask_id)
        update_task_field(task_file, "completed_subtasks", completed_subtasks)

    # ─── Phase 3: Summarizer + PR (re-plan 경로) ───
    finalize_task(
        agent_hub_root, project_name, task_id, task_file,
        completed_subtasks, git_enabled, git_config,
        codebase_path, task_branch, default_branch, dummy,
    )


def finalize_task(agent_hub_root, project_name, task_id, task_file,
                  completed_subtasks, git_enabled, git_config,
                  codebase_path, task_branch, default_branch, dummy):
    """
    Subtask loop 완료 후 Summarizer 실행 + PR 생성/머지 + task 상태 업데이트.
    run_pipeline()과 run_pipeline_from_subtasks() 양쪽에서 호출된다.
    """
    # ─── Summarizer 실행 ───
    log_step("Summarizer 실행")
    success, summary_data = run_agent(
        agent_hub_root, "summarizer", project_name, task_id,
        dummy=dummy,
    )

    pr_title = f"[{task_id}] Task completed"
    pr_body = f"Automated PR for task {task_id}"
    task_summary = ""

    if success and summary_data:
        raw_title = summary_data.get("pr_title", pr_title)
        # task ID 접두사 보장: [00001] Add feature...
        if not raw_title.startswith(f"[{task_id}]"):
            pr_title = f"[{task_id}] {raw_title}"
        else:
            pr_title = raw_title
        pr_body = summary_data.get("pr_body", pr_body)
        task_summary = summary_data.get("task_summary", "")
        update_task_field(task_file, "summary", task_summary)
    else:
        log_warn("Summarizer 실패 — 기본 PR 메시지를 사용합니다.")

    # ─── PR 생성 + 머지 ───
    if git_enabled and task_branch:
        pr_target = git_config.get("pr_target_branch", default_branch)
        auto_merge = git_config.get("auto_merge", False)

        # PR 작업 전 gh 인증 재확인
        git_provider = git_config.get("provider", "github")
        if git_provider == "github":
            ensure_gh_auth(git_config.get("auth_token", ""), codebase_path=codebase_path)

        log_step("Git: PR 생성")
        try:
            pr_url = git_create_pr(codebase_path, task_branch, pr_target, pr_title, pr_body)
            update_task_field(task_file, "pr_url", pr_url)

            if auto_merge:
                log_step("Git: PR 자동 머지")
                git_merge_pr(codebase_path, pr_url)
                update_task_field(task_file, "status", "completed")
            else:
                log_info(f"[git] auto_merge=false — PR 생성 완료. 수동 머지 대기: {pr_url}")
                update_task_field(task_file, "status", "pending_review")
        except RuntimeError as e:
            log_error(f"PR 처리 실패: {e}")
            update_task_field(task_file, "status", "failed")
            finalize_project_dir = os.path.join(agent_hub_root, "projects", project_name)
            update_project_state(finalize_project_dir, status="idle", last_error=task_id)
            sys.exit(1)
    else:
        update_task_field(task_file, "status", "completed")

    update_task_field(task_file, "current_subtask", None)

    # project_state.json 갱신 (TM 연동용)
    finalize_project_dir = os.path.join(agent_hub_root, "projects", project_name)
    update_project_state(finalize_project_dir, status="idle")

    log_step("파이프라인 완료")
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
