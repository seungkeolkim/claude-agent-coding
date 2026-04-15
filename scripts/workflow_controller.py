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
    - git 자동화: task별 브랜치 생성, subtask별 커밋, merge_strategy에 따라 PR 처리
    - usage threshold 미포함 (추후)
"""

import argparse
import glob as glob_module
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

# ─── Graceful Shutdown ───
# SIGTERM 수신 시 wait_for_human_response() 폴링 루프에서 감지하여
# 현재 상태를 유지한 채 깨끗하게 종료한다.
# TM 재시작 시 --resume 플래그로 중단 지점부터 재개.
_shutdown_requested = False


def _handle_sigterm(signum, frame):
    """SIGTERM/SIGINT 핸들러. 종료 플래그만 세팅한다."""
    global _shutdown_requested
    _shutdown_requested = True

import yaml

from notification import emit_notification
from usage_checker import wait_until_below_threshold

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
    "memory_updater": "08",
    "summarizer": "09",
}

STEP_NAME = {
    "planner": "planner",
    "coder": "coder",
    "reviewer": "reviewer",
    "setup": "setup",
    "unit_tester": "unit-tester",
    "e2e_tester": "e2e-tester",
    "reporter": "reporter",
    "memory_updater": "memory-updater",
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


def request_human_review(task_file, task_id, review_type, plan_path, subtask_count,
                         project_dir=None):
    """
    task JSON에 human_interaction을 기록하고 status를 waiting_for_human_plan_confirm으로 변경한다.
    review_type: "plan_review" | "replan_review"
    project_dir: 프로젝트 디렉토리 (알림 발송용). None이면 task_file에서 추론.
    """
    task = load_json(task_file)

    task["status"] = "waiting_for_human_plan_confirm"
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

    # 알림 발송
    resolved_project_dir = project_dir or os.path.dirname(os.path.dirname(task_file))
    event_type = "plan_review_requested" if review_type == "plan_review" else "replan_review_requested"
    emit_notification(
        project_dir=resolved_project_dir,
        event_type=event_type,
        task_id=task_id,
        message=f"Plan을 확인해주세요. subtask {subtask_count}개 생성됨.",
        details={"plan_path": plan_path},
    )


def wait_for_human_response(task_file, project_dir, task_id, timeout_hours,
                            poll_interval=10, re_notification_interval_hours=0):
    """
    task JSON의 human_interaction.response가 채워질 때까지 폴링한다.
    timeout_hours 초과 시 자동 승인.
    commands/ 디렉토리의 cancel 명령도 감시한다.
    re_notification_interval_hours > 0이면 해당 간격마다 재알림 발송.

    반환: "approve" | "modify" | "cancel" | "timeout"
    """
    # project_state.json에 waiting_for_human_plan_confirm 상태 기록
    update_project_state(project_dir, status="waiting_for_human_plan_confirm",
                         current_task_id=task_id)

    start_time = time.time()
    timeout_seconds = timeout_hours * 3600

    # 재알림 추적
    re_notification_interval_seconds = re_notification_interval_hours * 3600
    last_re_notification_time = start_time  # 최초 알림은 request_human_review()에서 이미 발송

    log_step(f"사용자 응답 대기 중 (timeout: {timeout_hours}h)")
    log_info("승인: ./run_agent.sh approve {task_id} --project {project}")
    log_info("거부: ./run_agent.sh reject {task_id} --project {project} --message '사유'")
    if re_notification_interval_hours > 0:
        log_info(f"재알림: {re_notification_interval_hours}시간마다 재발송")

    commands_dir = os.path.join(project_dir, "commands")

    while True:
        # SIGTERM 종료 확인 — 상태는 이미 task JSON에 저장되어 있으므로
        # 추가 저장 없이 깨끗하게 종료한다. TM 재시작 시 --resume으로 재개.
        if _shutdown_requested:
            log_info("SIGTERM 수신 — 대기 상태 유지, WFC 종료")
            sys.exit(0)

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
                # 대기 시간 누적
                _accumulate_human_wait_seconds(task, hi)
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

            # 대기 시간을 counters에 누적 (safety limiter의 duration 계산에서 차감)
            _accumulate_human_wait_seconds(task, hi)
            save_json(task_file, task)

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

        # 재알림 확인
        if re_notification_interval_seconds > 0:
            since_last = time.time() - last_re_notification_time
            if since_last >= re_notification_interval_seconds:
                # 현재 human_interaction 정보 읽기
                task_for_renotify = load_json(task_file)
                hi_for_renotify = task_for_renotify.get("human_interaction", {})
                review_type = hi_for_renotify.get("type", "plan_review")
                event_type = ("plan_review_requested" if review_type == "plan_review"
                              else "replan_review_requested")
                hours_waiting = elapsed / 3600
                emit_notification(
                    project_dir=project_dir,
                    event_type=event_type,
                    task_id=task_id,
                    message=f"재알림: 승인 대기 중 ({hours_waiting:.1f}시간 경과)",
                    details={"is_re_notification": True},
                )
                last_re_notification_time = time.time()
                log_info(f"재알림 발송 ({hours_waiting:.1f}시간 경과)")

        # 대기 (0.5초 단위로 분할하여 SIGTERM 빠르게 감지)
        for _ in range(poll_interval * 2):
            if _shutdown_requested:
                log_info("SIGTERM 수신 — 대기 상태 유지, WFC 종료")
                sys.exit(0)
            time.sleep(0.5)


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


def git_head_sha(codebase_path):
    """현재 HEAD의 commit SHA를 반환한다."""
    return git_run(codebase_path, "rev-parse", "HEAD")


def git_reset_hard_to(codebase_path, sha):
    """
    worktree를 특정 SHA로 강제 리셋하고 untracked 파일/디렉토리를 제거한다.
    subtask 재시도 시 retry_mode="reset"에서 사용.
    """
    log_info(f"[git] subtask worktree 리셋: {sha[:8]}")
    git_run(codebase_path, "reset", "--hard", sha)
    git_run(codebase_path, "clean", "-fd")


def git_soft_reset_if_moved(codebase_path, expected_sha):
    """
    HEAD가 expected_sha에서 이동했으면 soft reset으로 되돌린다.
    Coder가 프롬프트 규칙을 어기고 git commit을 실행한 경우 방어.
    commit이 만든 스냅샷은 풀어서 staged 상태로 복원 → 이후 WFC가 정상적으로 처리.
    """
    current = git_head_sha(codebase_path)
    if current == expected_sha:
        return False
    log_warn(f"[git] HEAD 이동 감지 ({expected_sha[:8]} → {current[:8]}): Coder가 commit을 만든 것으로 보임. soft reset으로 되돌림.")
    git_run(codebase_path, "reset", "--soft", expected_sha)
    return True


def git_commit_worktree_no_push(codebase_path, message, author_name, author_email):
    """
    worktree의 모든 변경을 stage하고 커밋한다. push는 하지 않는다.
    변경이 없으면 False 반환.
    Reviewer가 approved를 낸 직후 WFC가 호출.
    """
    if not git_has_changes(codebase_path):
        log_info("[git] 변경사항 없음 — 커밋 스킵")
        return False
    git_run(codebase_path, "add", "-A")
    git_run(
        codebase_path, "commit",
        "-m", message,
        "--author", f"{author_name} <{author_email}>",
    )
    log_info(f"[git] 커밋 완료 (push 보류): {message}")
    return True


def validate_reviewer_output(result):
    """
    Reviewer output 스키마 검증.
    필수 필드가 없거나 형식이 틀리면 (False, 오류 메시지) 반환.
    """
    if not isinstance(result, dict):
        return False, "reviewer 출력이 dict가 아님"
    action = result.get("action")
    if action not in ("approved", "rejected"):
        return False, f"action은 'approved' 또는 'rejected'여야 함 (실제: {action!r})"
    if action == "approved":
        if not result.get("current_state_summary"):
            return False, "approved 시 current_state_summary 필요"
        return True, ""
    # rejected
    required = ["retry_mode", "current_state_summary", "what_is_wrong",
                "what_should_be", "actionable_instructions"]
    missing = [k for k in required if not result.get(k)]
    if missing:
        return False, f"rejected 필수 필드 누락: {missing}"
    if result["retry_mode"] not in ("reset", "continue"):
        return False, f"retry_mode는 'reset' 또는 'continue' (실제: {result['retry_mode']!r})"
    if not isinstance(result["actionable_instructions"], list):
        return False, "actionable_instructions는 list여야 함"
    return True, ""


def git_reset_to_base_branch(codebase_path, base_branch, remote="origin", token=None):
    """
    Planner 실행 직전에 codebase를 base_branch의 최신 상태로 강제 리셋한다.

    task는 프로젝트 단위로 직렬 실행되므로, 이 시점에 남아 있는 미커밋/미추적 변경은
    이전 task의 산출물이다. 정상 종료된 이전 task는 이미 remote에 push되어 PR로
    보존되어 있거나(merge_strategy=pr_and_continue) 머지 완료(auto_merge)이고,
    비정상 종료된 경우는 폐기 대상이므로 무조건 리셋해도 안전하다.

    절차:
      1. git reset --hard           (staged/unstaged 폐기)
      2. git clean -fd              (untracked 파일/디렉토리 제거)
      3. git checkout <base_branch> (base_branch로 이동)
      4. git fetch <remote> <base>  (remote 최신 상태 가져오기, 실패 시 경고만)
      5. git reset --hard <remote>/<base> (머지된 이전 task를 로컬에 반영)

    Args:
        codebase_path: 대상 git 저장소 경로.
        base_branch: project.yaml의 git.base_branch (예: "main").
        remote: remote 이름 (기본 "origin").
        token: fetch 시 http.extraheader로 인라인 주입할 PAT (선택).
               None이면 기본 자격 증명 경로 사용. VSCode askpass env는 항상 strip.
    """
    import base64
    log_info(f"[git] base_branch로 리셋: {base_branch}")

    # 1~3. 로컬 정리 후 base로 이동
    git_run(codebase_path, "reset", "--hard")
    git_run(codebase_path, "clean", "-fd")
    git_run(codebase_path, "checkout", base_branch)

    # 4. remote fetch (네트워크/권한 문제는 치명적이지 않으므로 실패해도 진행)
    env = _env_without_vscode_git()
    fetch_cmd = ["git"]
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        fetch_cmd += ["-c", f"http.extraheader=Authorization: Basic {basic}"]
    fetch_cmd += ["fetch", remote, base_branch]
    display = " ".join("<token-header>" if (token and "Basic " in a) else a for a in fetch_cmd)
    log_info(f"[git] {display}")
    result = subprocess.run(fetch_cmd, cwd=codebase_path, capture_output=True, text=True, env=env)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        if token:
            stderr = stderr.replace(token, "***")
        log_warn(f"[git] fetch 실패 (로컬 base_branch로 계속 진행): {stderr}")
        return

    # 5. remote에 fast-forward (이전 task의 머지 반영)
    git_run(codebase_path, "reset", "--hard", f"{remote}/{base_branch}")
    log_info(f"[git] base_branch 동기화 완료: {remote}/{base_branch}")


def git_wipe_and_recreate_task_branch(codebase_path, branch_name, base_branch):
    """
    replan 시 task 브랜치를 폐기하고 base_branch에서 다시 생성한다.
    이전 subtask의 commit들은 전부 사라진다.
    remote는 건드리지 않는다 (중간 push가 없으므로 리모트에 해당 브랜치가 없다는 전제).
    """
    log_info(f"[git] replan: task 브랜치 폐기 후 재생성 (base={base_branch})")
    git_run(codebase_path, "checkout", base_branch)
    existing = subprocess.run(
        ["git", "branch", "--list", branch_name],
        cwd=codebase_path, capture_output=True, text=True,
    )
    if existing.stdout.strip():
        git_run(codebase_path, "branch", "-D", branch_name)
    git_run(codebase_path, "checkout", "-b", branch_name, base_branch)
    return branch_name


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


def _env_without_vscode_git():
    """VSCode 터미널에서 상속된 git 인증 관련 env를 제거한 dict 반환.

    VSCode가 주입한 GIT_ASKPASS는 IPC socket(`VSCODE_GIT_IPC_HANDLE`)으로 크리덴셜을
    요청한다. WFC는 VSCode와 별도 프로세스라 socket이 끊기면 `ECONNREFUSED`로 실패한다.
    `-c http.extraheader`로 토큰을 직접 넣을 때도 askpass가 먼저 가로채지 않도록 제거.
    """
    env = os.environ.copy()
    for var in (
        "GIT_ASKPASS",
        "VSCODE_GIT_IPC_HANDLE",
        "VSCODE_GIT_ASKPASS_NODE",
        "VSCODE_GIT_ASKPASS_MAIN",
        "VSCODE_GIT_ASKPASS_EXTRA_ARGS",
        "SSH_ASKPASS",
    ):
        env.pop(var, None)
    return env


def git_push(codebase_path, remote, branch=None, token=None):
    """현재 브랜치를 원격에 push한다. upstream이 없으면 자동 설정.

    token이 주어지면 `http.extraheader`로 HTTP Basic 인증을 인라인 주입한다.
    - OS credential helper / VSCode askpass 경로를 우회하므로 다중 사용자 환경에서도
      동일한 service-account 토큰으로 push 가능.
    - 로그에는 토큰을 출력하지 않는다 (log_info는 cmd를 찍지 않고, 실패 stderr에서
      토큰을 마스킹).
    """
    import base64
    env = _env_without_vscode_git()

    cmd = ["git"]
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        cmd += ["-c", f"http.extraheader=Authorization: Basic {basic}"]
    cmd += ["push"]
    if branch:
        cmd += ["--set-upstream", remote, branch]
    else:
        cmd += [remote]

    # 표시용 cmd: 토큰이 들어간 -c 인자는 마스킹
    display = " ".join("<token-header>" if (token and "Basic " in a) else a for a in cmd)
    log_info(f"[git] {display}")

    result = subprocess.run(cmd, cwd=codebase_path, capture_output=True, text=True, env=env)
    if result.returncode != 0:
        stderr = result.stderr or ""
        if token:
            stderr = stderr.replace(token, "***")
        log_error(f"[git] push 실패: {stderr.strip()}")
        raise RuntimeError(f"git push 실패: {stderr.strip()}")
    log_info(f"[git] push 완료: {remote} {branch or ''}")


def _format_task_tag(task_id, requested_by):
    """커밋/PR 제목 접두사. requested_by 있으면 [task_id][user], 없으면 [task_id]."""
    if requested_by:
        return f"[{task_id}][{requested_by}]"
    return f"[{task_id}]"


def git_commit_subtask(codebase_path, task_id, subtask_id, subtask_title,
                       author_name, author_email, remote=None, branch=None,
                       token=None, requested_by=None):
    """
    subtask 완료 후 변경사항을 커밋하고 push한다.
    변경사항이 없으면 스킵한다.

    Args:
        token: push 시 http.extraheader로 인라인 주입할 OAuth 토큰 (선택).
        requested_by: 커밋 메시지에 박을 요청자 태그 (선택).
    """
    if not git_has_changes(codebase_path):
        log_info(f"[git] 변경사항 없음 — 커밋 스킵 ({subtask_id})")
        return False

    git_run(codebase_path, "add", "-A")
    tag = _format_task_tag(task_id, requested_by)
    commit_msg = f"{tag} {subtask_id}: {subtask_title}"
    git_run(
        codebase_path, "commit",
        "-m", commit_msg,
        "--author", f"{author_name} <{author_email}>",
    )
    log_info(f"[git] 커밋 완료: {commit_msg}")

    if remote:
        git_push(codebase_path, remote, branch, token=token)

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


def update_pipeline_stage(task_file, stage, detail=None):
    """
    파이프라인 진행 단계를 task JSON에 기록한다.
    웹 콘솔에서 현재 어느 단계인지 표시하기 위한 용도.

    stage 예: "planner", "coder", "reviewer", "git_push", "pr_create", "reporter"
    detail 예: "subtask 1/3", "attempt 2"
    """
    task = load_json(task_file)
    task["pipeline_stage"] = stage
    task["pipeline_stage_detail"] = detail
    task["pipeline_stage_updated_at"] = datetime.now(timezone.utc).isoformat()
    save_json(task_file, task)


def record_failure_reason(task_file, reason):
    """
    task 실패 시 원인을 기록한다.
    웹 콘솔에서 실패 원인을 확인할 수 있다.
    """
    task = load_json(task_file)
    task["failure_reason"] = reason
    save_json(task_file, task)


def _load_pipeline_context(args):
    """
    파이프라인 실행에 필요한 공통 컨텍스트를 로드한다.

    설정 파일 로드, effective config 생성, git 인증 등
    run_pipeline()과 run_pipeline_resume() 양쪽에서 사용.

    Returns:
        dict: 파이프라인 컨텍스트 (agent_hub_root, project_dir, task_file, task,
              effective, pipeline, git_config, git_enabled, codebase_path,
              default_branch, base_branch, config 등)
    """
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

    # 4계층 설정 merge → effective config
    effective = resolve_effective_config(config, project_yaml, project_state, task)
    log_info("effective config 생성 완료 (4계층 merge)")

    # pipeline 구성 결정
    pipeline = determine_pipeline(effective)
    log_info(f"pipeline 구성: {' → '.join(pipeline)}")

    # Git 설정
    git_config = effective.get("git", {})
    git_enabled = git_config.get("enabled", False) and not args.dummy
    codebase_path = effective.get("codebase", {}).get("path", "")
    default_branch = effective.get("project", {}).get("default_branch", "main")
    base_branch = git_config.get("base_branch", default_branch)

    # Git provider 인증
    auth_token = ""
    if git_enabled:
        git_provider = git_config.get("provider", "github")
        auth_token = git_config.get("auth_token", "")
        if not auth_token:
            auth_token = config.get("machines", {}).get("executor", {}).get("github_token", "")
        if git_provider == "github":
            ensure_gh_auth(auth_token, codebase_path=codebase_path)
        elif git_provider != "github":
            log_warn(f"[git] provider '{git_provider}'는 아직 미구현. github만 지원합니다.")

    return {
        "agent_hub_root": agent_hub_root,
        "project_dir": project_dir,
        "tasks_dir": tasks_dir,
        "task_file": task_file,
        "task": task,
        "task_id": task_id,
        "config": config,
        "effective": effective,
        "pipeline": pipeline,
        "git_config": git_config,
        "git_enabled": git_enabled,
        "auth_token": auth_token,
        "codebase_path": codebase_path,
        "default_branch": default_branch,
        "base_branch": base_branch,
    }


def run_pipeline(args):
    """메인 파이프라인 실행 로직."""
    ctx = _load_pipeline_context(args)
    agent_hub_root = ctx["agent_hub_root"]
    project_dir = ctx["project_dir"]
    task_file = ctx["task_file"]
    task = ctx["task"]
    task_id = ctx["task_id"]
    config = ctx["config"]
    effective = ctx["effective"]
    pipeline = ctx["pipeline"]
    git_config = ctx["git_config"]
    git_enabled = ctx["git_enabled"]
    auth_token = ctx.get("auth_token", "")
    codebase_path = ctx["codebase_path"]
    default_branch = ctx["default_branch"]
    base_branch = ctx["base_branch"]
    task_branch = None
    requested_by = task.get("requested_by")

    log_step(f"파이프라인 시작: project={args.project} task={task_id}")

    # project_state.json 갱신 (TM 연동용)
    update_project_state(project_dir, status="running", current_task_id=task_id)

    if args.dry_run:
        log_warn("DRY-RUN: 실제 실행 없이 pipeline 구성만 확인")
        return

    if args.dummy:
        log_info("DUMMY 모드: git 작업 스킵")

    # ─── Pre-Planner: codebase를 base_branch로 강제 리셋 ───
    # Planner가 이전 task의 미정리 작업 트리를 오염된 컨텍스트로 인식하지 않도록,
    # Planner 실행 전에 base_branch의 최신 상태로 되돌린다.
    # task는 프로젝트 단위로 직렬 실행되므로 남아 있는 변경은 모두 폐기 안전.
    if git_enabled and not args.dummy:
        update_pipeline_stage(task_file, "git_reset")
        git_remote = git_config.get("remote", "origin")
        try:
            git_reset_to_base_branch(codebase_path, base_branch,
                                     remote=git_remote, token=auth_token or None)
        except Exception as e:
            log_error(f"base_branch 리셋 실패: {e}")
            record_failure_reason(task_file, f"base_branch 리셋 실패: {e}")
            update_task_field(task_file, "status", "failed")
            update_project_state(project_dir, status="idle", last_error=task_id)
            emit_notification(
                project_dir=project_dir, event_type="task_failed", task_id=task_id,
                message=f"base_branch 리셋 실패: {e}",
            )
            sys.exit(1)

    # ─── Phase 1: Planner ───
    log_step("Phase 1: Planner 실행")
    update_pipeline_stage(task_file, "planner")

    success, plan_data = run_agent(
        agent_hub_root, "planner", args.project, task_id,
        dummy=args.dummy,
    )

    if not success or not plan_data:
        log_error("Planner 실패. 파이프라인 중단.")
        record_failure_reason(task_file, "Planner 실패")
        update_task_field(task_file, "status", "failed")
        update_project_state(project_dir, status="idle", last_error=task_id)
        emit_notification(
            project_dir=project_dir, event_type="task_failed", task_id=task_id,
            message="Planner 실패로 파이프라인 중단",
        )
        sys.exit(1)

    # plan 저장 및 subtask 파일 생성
    save_plan_file(project_dir, task_id, plan_data)
    subtasks = create_subtask_files(project_dir, task_id, plan_data)

    # task_type이 "memory_refresh"이면 subtask=[]가 정상 동작이다.
    # Planner는 codebase 탐색 후 "변경 없음"으로 계획을 종료하고,
    # 실제 작업은 finalize_task의 MemoryUpdater가 full-scan 모드로 담당한다.
    task_data = load_json(task_file)
    task_type = task_data.get("task_type", "feature")

    if not subtasks and task_type != "memory_refresh":
        log_error("Planner가 subtask를 생성하지 않았습니다.")
        record_failure_reason(task_file, "Planner가 subtask를 생성하지 않음")
        update_task_field(task_file, "status", "failed")
        update_project_state(project_dir, status="idle", last_error=task_id)
        emit_notification(
            project_dir=project_dir, event_type="task_failed", task_id=task_id,
            message="Planner가 subtask를 생성하지 않음",
        )
        sys.exit(1)

    if not subtasks and task_type == "memory_refresh":
        log_info("memory_refresh task: subtasks=[] — subtask loop를 건너뛰고 finalize로 진입")
    else:
        log_info(f"subtask {len(subtasks)}개 생성됨")

    # ─── Human Review: Plan 승인 대기 ───
    human_review = effective.get("human_review_policy", {})
    if human_review.get("review_plan", False) and not args.dummy:
        update_pipeline_stage(task_file, "plan_review", f"subtask {len(subtasks)}개")
        plan_path = os.path.join("tasks", task_id, "plan.json")
        request_human_review(task_file, task_id, "plan_review", plan_path, len(subtasks),
                             project_dir=project_dir)

        timeout_hours = human_review.get("auto_approve_timeout_hours", 24)
        notification_config = effective.get("notification", {})
        re_noti_hours = notification_config.get("re_notification_interval_hours", 0)
        result = wait_for_human_response(
            task_file, project_dir, task_id, timeout_hours,
            re_notification_interval_hours=re_noti_hours,
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
            if not subtasks and task_type != "memory_refresh":
                log_error("Re-plan 후 subtask가 없습니다.")
                update_task_field(task_file, "status", "failed")
                update_project_state(project_dir, status="idle", last_error=task_id)
                sys.exit(1)
            if subtasks:
                log_info(f"re-plan 완료: subtask {len(subtasks)}개 재생성")
            else:
                log_info("re-plan 완료: memory_refresh task — subtask 없음 (finalize로 진입)")

        # 승인 후 running 상태로 복귀
        update_project_state(project_dir, status="running", current_task_id=task_id)
        update_task_field(task_file, "status", "in_progress")

    # ─── Git: task 브랜치 생성 (Planner 후) ───
    if git_enabled:
        log_step("Git: task 브랜치 생성")
        update_pipeline_stage(task_file, "git_branch")
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
        task_branch = git_create_task_branch(codebase_path, branch_name, base_branch)
        update_task_field(task_file, "branch", task_branch)

    # ─── Usage threshold 설정 로드 ───
    claude_config = effective.get("claude", {})
    usage_thresholds = claude_config.get("usage_thresholds", {})
    usage_check_interval = claude_config.get("usage_check_interval_seconds", 60)

    # ─── Phase 2: Subtask Loop ───
    completed_subtasks = []

    for i, subtask in enumerate(subtasks):
        subtask_id = subtask.get("subtask_id", f"{task_id}-{i+1}")

        log_step(f"Subtask {i+1}/{len(subtasks)}: {subtask_id}")

        # usage check: 새 subtask 시작 전
        if not args.dummy:
            new_subtask_threshold = usage_thresholds.get("new_subtask", 0.80)
            wait_until_below_threshold(
                new_subtask_threshold,
                check_interval_seconds=usage_check_interval,
                level_name="new_subtask",
                log_fn=log_info,
            )

        # task에 현재 subtask 기록
        update_task_field(task_file, "current_subtask", subtask_id)
        update_task_counter(task_file, "current_subtask_retry", value=0)

        # subtask pipeline 실행 (Coder/Reviewer 승인 시 WFC가 내부에서 commit까지 수행, push는 안 함)
        subtask_success = run_subtask_pipeline(
            agent_hub_root, args.project, task_id, subtask_id,
            task_file, pipeline, args.dummy,
            usage_thresholds=usage_thresholds,
            usage_check_interval=usage_check_interval,
            codebase_path=codebase_path, git_enabled=git_enabled,
            git_config=git_config, requested_by=requested_by,
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
                        project_dir=project_dir,
                    )
                    timeout_hours = human_review.get("auto_approve_timeout_hours", 24)
                    replan_result = wait_for_human_response(
                        task_file, project_dir, task_id, timeout_hours,
                        re_notification_interval_hours=re_noti_hours,
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
                        emit_notification(
                            project_dir=project_dir, event_type="escalation", task_id=task_id,
                            message="replan 후 재수정 요청으로 에스컬레이션 발생",
                        )
                        sys.exit(1)
                    # approve/timeout → 계속 진행
                    update_project_state(project_dir, status="running",
                                         current_task_id=task_id)
                    update_task_field(task_file, "status", "in_progress")

                # ─── replan: task 브랜치 폐기 + base_branch에서 재생성 ───
                # 이전 subtask 성공분까지 모두 폐기 (사용자 정책). 사용자 알림은 emit.
                prior_subtask_count = len(completed_subtasks)
                if git_enabled and task_branch and not args.dummy:
                    try:
                        git_wipe_and_recreate_task_branch(codebase_path, task_branch, base_branch)
                    except RuntimeError as e:
                        log_error(f"[git] replan 시 브랜치 재생성 실패: {e}")
                        record_failure_reason(task_file, f"replan branch 재생성 실패: {e}")
                        update_task_field(task_file, "status", "failed")
                        update_project_state(project_dir, status="idle", last_error=task_id)
                        sys.exit(1)

                # replan으로 이전 subtask 성공분을 폐기 — completed 초기화.
                completed_subtasks = []
                update_task_field(task_file, "completed_subtasks", completed_subtasks)

                emit_notification(
                    project_dir=project_dir, event_type="replan_started", task_id=task_id,
                    message=f"replan 시작: 새 plan의 subtask {len(new_subtasks)}개 (이전 subtask {prior_subtask_count}개 폐기)",
                    details={"prior_completed_count": prior_subtask_count,
                             "new_subtask_count": len(new_subtasks)},
                )

                # 새 plan의 subtask로 루프 재시작
                log_info(f"새 plan으로 subtask {len(new_subtasks)}개 재구성 "
                         f"(이전 subtask {prior_subtask_count}개 폐기)")
                subtasks_remaining = new_subtasks
                log_warn("re-plan 후 파이프라인을 처음부터 재시작합니다")
                run_pipeline_from_subtasks(
                    agent_hub_root, args.project, task_id, task_file,
                    new_subtasks, pipeline, args.dummy, completed_subtasks,
                    git_enabled=git_enabled, git_config=git_config,
                    codebase_path=codebase_path, task_branch=task_branch,
                    default_branch=default_branch,
                    usage_thresholds=usage_thresholds,
                    usage_check_interval=usage_check_interval,
                )
                return
            else:
                log_error(f"subtask {subtask_id} 실패. 파이프라인 중단.")
                update_task_field(task_file, "status", "failed")
                update_project_state(project_dir, status="idle", last_error=task_id)
                emit_notification(
                    project_dir=project_dir, event_type="task_failed", task_id=task_id,
                    message=f"subtask {subtask_id} 실패로 파이프라인 중단",
                    details={"failed_subtask": subtask_id},
                )
                sys.exit(1)

        # Note: subtask commit은 run_subtask_pipeline 내부에서 Reviewer approved 시 이미 수행됨 (push 보류).
        # push는 finalize_task의 PR 생성 단계에서 1회 실행된다.

        completed_subtasks.append(subtask_id)
        update_task_field(task_file, "completed_subtasks", completed_subtasks)
        log_info(f"subtask {subtask_id} 완료 ({i+1}/{len(subtasks)})")

    # ─── Phase 3: Summarizer + PR ───
    update_pipeline_stage(task_file, "finalizing")
    finalize_task(
        agent_hub_root, args.project, task_id, task_file,
        completed_subtasks, git_enabled, git_config,
        codebase_path, task_branch, default_branch, args.dummy,
        requested_by=requested_by,
    )


def _load_subtasks_from_disk(project_dir, task_id):
    """
    디스���에서 plan.json과 subtask 파일들을 복구한다.

    resume 시 in-memory 상태를 복원하는 데 사용.

    Returns:
        tuple: (plan_data, subtasks) — plan_data는 plan.json dict,
               subtasks는 subtask dict 리스트
    """
    internal_dir = os.path.join(project_dir, "tasks", task_id)
    plan_path = os.path.join(internal_dir, "plan.json")

    if not os.path.isfile(plan_path):
        log_error(f"plan.json을 찾을 수 없음: {plan_path}")
        return None, []

    plan_data = load_json(plan_path)

    # subtask 파일 복구
    subtask_pattern = os.path.join(internal_dir, "subtask-*.json")
    subtask_files = sorted(glob_module.glob(subtask_pattern))
    if subtask_files:
        subtasks = [load_json(f) for f in subtask_files]
    else:
        # subtask 파일이 없으면 plan_data에서 복원
        subtasks = plan_data.get("subtasks", [])

    return plan_data, subtasks


def _accumulate_human_wait_seconds(task, human_interaction):
    """
    human_interaction의 requested_at ~ responded_at 차이를
    counters.human_wait_seconds에 누적한다.

    safety limiter가 task duration 계산 시 이 값을 차감하여
    사람의 응답 대기 시간을 제외한다.
    """
    requested_at = human_interaction.get("requested_at", "")
    response = human_interaction.get("response", {})
    responded_at = response.get("responded_at", "") if response else ""

    if not requested_at or not responded_at:
        return

    try:
        req_time = datetime.fromisoformat(requested_at)
        resp_time = datetime.fromisoformat(responded_at)
        if req_time.tzinfo is None:
            req_time = req_time.replace(tzinfo=timezone.utc)
        if resp_time.tzinfo is None:
            resp_time = resp_time.replace(tzinfo=timezone.utc)
        wait_seconds = max(0, (resp_time - req_time).total_seconds())

        counters = task.setdefault("counters", {})
        counters["human_wait_seconds"] = counters.get("human_wait_seconds", 0) + wait_seconds
        log_info(f"대기 시간 누적: +{wait_seconds:.0f}초 (총 {counters['human_wait_seconds']:.0f}초)")
    except (ValueError, TypeError):
        pass


def _calculate_remaining_timeout(human_interaction, default_timeout_hours):
    """
    human_interaction의 requested_at 기준으로 남은 타임아웃 시간을 계산한다.

    Returns:
        float: 남은 타임아웃 시간(hours). 최소 0.01h (36초).
    """
    requested_at = human_interaction.get("requested_at", "")
    if not requested_at:
        return default_timeout_hours

    try:
        req_time = datetime.fromisoformat(requested_at)
        # timezone-naive이면 UTC로 간주
        if req_time.tzinfo is None:
            req_time = req_time.replace(tzinfo=timezone.utc)
        elapsed_hours = (datetime.now(timezone.utc) - req_time).total_seconds() / 3600
        remaining = max(0.01, default_timeout_hours - elapsed_hours)
        log_info(f"대기 경과: {elapsed_hours:.1f}h, ���은 timeout: {remaining:.1f}h")
        return remaining
    except (ValueError, TypeError):
        return default_timeout_hours


def run_pipeline_resume(args):
    """
    중단된 파이프라인을 재개한다.

    WFC가 SIGTERM으로 종료된 후 TM이 --resume 플래그로 재시작할 때 호출.
    task JSON의 status와 human_interaction을 확��하여 중단 지점부터 이어서 실행한���.

    지원하는 resume 지점:
    - waiting_for_human_plan_confirm (plan_review): plan 승인 대기 → 승인 후 파이프라인 계속
    - waiting_for_human_plan_confirm (replan_review): replan 승인 대기 → 승인 후 subtask loop 계속
    """
    ctx = _load_pipeline_context(args)
    agent_hub_root = ctx["agent_hub_root"]
    project_dir = ctx["project_dir"]
    task_file = ctx["task_file"]
    task = ctx["task"]
    task_id = ctx["task_id"]
    effective = ctx["effective"]
    pipeline = ctx["pipeline"]
    git_config = ctx["git_config"]
    git_enabled = ctx["git_enabled"]
    codebase_path = ctx["codebase_path"]
    default_branch = ctx["default_branch"]
    base_branch = ctx["base_branch"]

    status = task.get("status", "")
    hi = task.get("human_interaction", {})
    review_type = hi.get("type", "plan_review") if hi else "plan_review"

    log_step(
        f"파이프라인 재개: project={args.project} task={task_id} "
        f"status={status} review_type={review_type}"
    )

    # 상태 검증
    if status not in ("waiting_for_human_plan_confirm", "needs_replan"):
        log_error(f"resume 불가: task status가 '{status}'입니다. "
                  f"waiting_for_human_plan_confirm 또는 needs_replan이어야 합니다.")
        sys.exit(1)

    # project_state.json 갱신 — resume 중임을 기록
    update_project_state(project_dir, status=status, current_task_id=task_id)

    # 디스크에서 plan과 subtask 복구
    plan_data, subtasks = _load_subtasks_from_disk(project_dir, task_id)
    if not plan_data:
        sys.exit(1)

    log_info(f"디스크에서 복구: plan + subtask {len(subtasks)}개")

    # 설정 로드
    human_review = effective.get("human_review_policy", {})
    notification_config = effective.get("notification", {})
    re_noti_hours = notification_config.get("re_notification_interval_hours", 0)
    timeout_hours = human_review.get("auto_approve_timeout_hours", 24)

    # 사용자 응답 ��인 (WFC 중단 중에 도착했을 수 있음)
    response = hi.get("response") if hi else None

    if not response:
        # 아직 응답 없음 → 대기 루프 재진입 (남은 시간 계산)
        remaining_timeout = _calculate_remaining_timeout(hi, timeout_hours)
        log_info("사용자 응답 없음 — 대기 루프 재진입")
        result = wait_for_human_response(
            task_file, project_dir, task_id, remaining_timeout,
            re_notification_interval_hours=re_noti_hours,
        )
    else:
        # 응답이 이미 도착 — 바로 처리
        result = response.get("action", "approve")
        log_info(f"기존 응답 발견: action={result}")

    # review_type에 따라 후속 처리 분기
    if review_type == "plan_review":
        _continue_after_plan_review(
            result, args, ctx, plan_data, subtasks,
            human_review, re_noti_hours,
        )
    elif review_type == "replan_review":
        _continue_after_replan_review(
            result, args, ctx, plan_data, subtasks,
        )
    else:
        log_error(f"알 수 없는 review_type: {review_type}")
        sys.exit(1)


def _continue_after_plan_review(result, args, ctx, plan_data, subtasks,
                                human_review, re_noti_hours):
    """
    plan_review 응답 처리 후 파이프라인을 계속 실행한다.

    run_pipeline()의 plan_review 이후 로직과 동일.
    cancel/modify/approve 분��� 처리 → git branch → subtask loop → finalize.
    """
    agent_hub_root = ctx["agent_hub_root"]
    project_dir = ctx["project_dir"]
    task_file = ctx["task_file"]
    task_id = ctx["task_id"]
    effective = ctx["effective"]
    pipeline = ctx["pipeline"]
    git_config = ctx["git_config"]
    git_enabled = ctx["git_enabled"]
    codebase_path = ctx["codebase_path"]
    default_branch = ctx["default_branch"]
    base_branch = ctx["base_branch"]

    if result == "cancel":
        log_warn("사용자가 plan을 취소했습니다.")
        update_task_field(task_file, "status", "cancelled")
        update_project_state(project_dir, status="idle")
        sys.exit(0)
    elif result == "modify":
        log_warn("사용자가 plan 수정을 요청했습니다. replan 실행.")
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
        # resume 경로에서도 memory_refresh task는 빈 subtask를 허용한다.
        resume_task_data = load_json(task_file)
        resume_task_type = resume_task_data.get("task_type", "feature")
        if not subtasks and resume_task_type != "memory_refresh":
            log_error("Re-plan 후 subtask가 없습니다.")
            update_task_field(task_file, "status", "failed")
            update_project_state(project_dir, status="idle", last_error=task_id)
            sys.exit(1)
        if subtasks:
            log_info(f"re-plan 완료: subtask {len(subtasks)}개 재생성")
        else:
            log_info("re-plan 완료: memory_refresh task — subtask 없음 (finalize로 진입)")

    # 승인 후 running 상태�� 복귀
    update_project_state(project_dir, status="running", current_task_id=task_id)
    update_task_field(task_file, "status", "in_progress")

    # Git: task 브랜치 생성 (이미 존재하면 checkout)
    task = load_json(task_file)
    task_branch = task.get("branch")

    if git_enabled and not task_branch:
        log_step("Git: task 브랜치 생성")
        update_pipeline_stage(task_file, "git_branch")
        branch_name = (
            task.get("branch_name")
            or plan_data.get("branch_name")
            or f"feature/{task_id}"
        )
        prefix = f"feature/{task_id}-"
        if not branch_name.startswith(prefix):
            if branch_name.startswith("feature/"):
                suffix = branch_name[len("feature/"):]
                if not suffix.startswith(task_id):
                    branch_name = f"feature/{task_id}-{suffix}"
            else:
                branch_name = f"feature/{task_id}-{branch_name}"
        task_branch = git_create_task_branch(codebase_path, branch_name, base_branch)
        update_task_field(task_file, "branch", task_branch)
    elif git_enabled and task_branch:
        log_info(f"[git] 기존 브랜치 사용: {task_branch}")

    # Usage threshold 설정 로드
    claude_config = effective.get("claude", {})
    usage_thresholds = claude_config.get("usage_thresholds", {})
    usage_check_interval = claude_config.get("usage_check_interval_seconds", 60)

    # Subtask loop
    completed_subtasks = task.get("completed_subtasks", [])

    for i, subtask in enumerate(subtasks):
        subtask_id = subtask.get("subtask_id", f"{task_id}-{i+1}")

        # 이미 완료된 subtask ��너뜀
        if subtask_id in completed_subtasks:
            log_info(f"subtask {subtask_id} 이미 완료됨 — 건너뜀")
            continue

        log_step(f"Subtask {i+1}/{len(subtasks)}: {subtask_id}")

        if not args.dummy:
            new_subtask_threshold = usage_thresholds.get("new_subtask", 0.80)
            wait_until_below_threshold(
                new_subtask_threshold,
                check_interval_seconds=usage_check_interval,
                level_name="new_subtask",
                log_fn=log_info,
            )

        update_task_field(task_file, "current_subtask", subtask_id)
        update_task_counter(task_file, "current_subtask_retry", value=0)

        subtask_success = run_subtask_pipeline(
            agent_hub_root, args.project, task_id, subtask_id,
            task_file, pipeline, args.dummy,
            usage_thresholds=usage_thresholds,
            usage_check_interval=usage_check_interval,
            codebase_path=codebase_path, git_enabled=git_enabled,
            git_config=git_config, requested_by=requested_by,
        )

        if not subtask_success:
            # replan 필요 여부 확인
            task_current = load_json(task_file)
            if task_current.get("_needs_replan", False):
                replan_count = task_current["counters"].get("replan_count", 0)
                log_warn(f"replan 요청 (현재 {replan_count}회)")
                update_task_counter(task_file, "replan_count", increment=True)
                update_task_field(task_file, "_needs_replan", False)

                log_step("Re-plan: Planner 재실행")
                success, new_plan = run_agent(
                    agent_hub_root, "planner", args.project, task_id,
                    dummy=args.dummy,
                )
                if not success or not new_plan:
                    log_error("Re-plan 실패. 파이프라인 중단.")
                    update_task_field(task_file, "status", "failed")
                    update_project_state(project_dir, status="idle", last_error=task_id)
                    sys.exit(1)

                save_plan_file(project_dir, task_id, new_plan)
                new_subtasks = create_subtask_files(project_dir, task_id, new_plan)

                # replan_review 대기
                if human_review.get("review_replan", False) and not args.dummy:
                    plan_path = os.path.join("tasks", task_id, "plan.json")
                    request_human_review(
                        task_file, task_id, "replan_review",
                        plan_path, len(new_subtasks),
                        project_dir=project_dir,
                    )
                    replan_timeout = human_review.get("auto_approve_timeout_hours", 24)
                    replan_result = wait_for_human_response(
                        task_file, project_dir, task_id, replan_timeout,
                        re_notification_interval_hours=re_noti_hours,
                    )
                    if replan_result == "cancel":
                        log_warn("사용자가 replan을 취소했습니다.")
                        update_task_field(task_file, "status", "cancelled")
                        update_project_state(project_dir, status="idle")
                        sys.exit(0)
                    elif replan_result == "modify":
                        log_error("replan 후 재수정 요청. 에스컬레이션이 필요합니다.")
                        update_task_field(task_file, "status", "escalated")
                        update_project_state(project_dir, status="idle",
                                             last_error=task_id)
                        emit_notification(
                            project_dir=project_dir, event_type="escalation",
                            task_id=task_id,
                            message="replan 후 재수정 요청으로 에스컬레이션 발생",
                        )
                        sys.exit(1)
                    update_project_state(project_dir, status="running",
                                         current_task_id=task_id)
                    update_task_field(task_file, "status", "in_progress")

                # replan: task 브랜치 폐기 후 base_branch에서 재생성 (이전 subtask 성공분 포함 폐기)
                prior_subtask_count = len(completed_subtasks)
                if git_enabled and task_branch and not args.dummy:
                    try:
                        git_wipe_and_recreate_task_branch(codebase_path, task_branch, base_branch)
                    except RuntimeError as e:
                        log_error(f"[git] replan 시 브랜치 재생성 실패: {e}")
                        update_task_field(task_file, "status", "failed")
                        update_project_state(project_dir, status="idle", last_error=task_id)
                        sys.exit(1)

                completed_subtasks = []
                update_task_field(task_file, "completed_subtasks", completed_subtasks)
                emit_notification(
                    project_dir=project_dir, event_type="replan_started", task_id=task_id,
                    message=f"replan 시작: 새 plan의 subtask {len(new_subtasks)}개 (이전 subtask {prior_subtask_count}개 폐기)",
                    details={"prior_completed_count": prior_subtask_count,
                             "new_subtask_count": len(new_subtasks)},
                )

                log_info(f"새 plan으로 subtask {len(new_subtasks)}개 재구성 "
                         f"(이전 subtask {prior_subtask_count}개 폐기)")

                run_pipeline_from_subtasks(
                    agent_hub_root, args.project, task_id, task_file,
                    new_subtasks, pipeline, args.dummy, completed_subtasks,
                    git_enabled=git_enabled, git_config=git_config,
                    codebase_path=codebase_path, task_branch=task_branch,
                    default_branch=default_branch,
                    usage_thresholds=usage_thresholds,
                    usage_check_interval=usage_check_interval,
                )
                return
            else:
                log_error(f"subtask {subtask_id} 실패. 파이프라인 중단.")
                update_task_field(task_file, "status", "failed")
                update_project_state(project_dir, status="idle", last_error=task_id)
                emit_notification(
                    project_dir=project_dir, event_type="task_failed",
                    task_id=task_id,
                    message=f"subtask {subtask_id} 실패로 파이프라인 중단",
                    details={"failed_subtask": subtask_id},
                )
                sys.exit(1)

        # Note: subtask commit은 run_subtask_pipeline 내부에서 Reviewer approved 시 수행됨 (push 보류).

        completed_subtasks.append(subtask_id)
        update_task_field(task_file, "completed_subtasks", completed_subtasks)
        log_info(f"subtask {subtask_id} 완료 ({i+1}/{len(subtasks)})")

    # Summarizer + PR
    update_pipeline_stage(task_file, "finalizing")
    finalize_task(
        agent_hub_root, args.project, task_id, task_file,
        completed_subtasks, git_enabled, git_config,
        codebase_path, task_branch, default_branch, args.dummy,
        requested_by=requested_by,
    )


def _continue_after_replan_review(result, args, ctx, plan_data, subtasks):
    """
    replan_review 응답 처리 후 파이프라인을 계속 실행한다.

    cancel/modify → 종료, approve → run_pipeline_from_subtasks()로 진행.
    """
    agent_hub_root = ctx["agent_hub_root"]
    project_dir = ctx["project_dir"]
    task_file = ctx["task_file"]
    task = ctx["task"]
    task_id = ctx["task_id"]
    effective = ctx["effective"]
    pipeline = ctx["pipeline"]
    git_config = ctx["git_config"]
    git_enabled = ctx["git_enabled"]
    codebase_path = ctx["codebase_path"]
    default_branch = ctx["default_branch"]

    if result == "cancel":
        log_warn("사용자가 replan을 취소했습니다.")
        update_task_field(task_file, "status", "cancelled")
        update_project_state(project_dir, status="idle")
        sys.exit(0)
    elif result == "modify":
        log_error("replan 후 재수정 요청. 에스컬레이션이 필요합니다.")
        update_task_field(task_file, "status", "escalated")
        update_project_state(project_dir, status="idle", last_error=task_id)
        emit_notification(
            project_dir=project_dir, event_type="escalation", task_id=task_id,
            message="replan 후 재수정 요청으로 에스컬레이션 발생",
        )
        sys.exit(1)

    # approve/timeout → subtask loop 계속
    update_project_state(project_dir, status="running", current_task_id=task_id)
    update_task_field(task_file, "status", "in_progress")

    completed_subtasks = task.get("completed_subtasks", [])
    task_branch = task.get("branch")

    claude_config = effective.get("claude", {})
    usage_thresholds = claude_config.get("usage_thresholds", {})
    usage_check_interval = claude_config.get("usage_check_interval_seconds", 60)

    log_info(f"replan 승인 — subtask {len(subtasks)}개로 재구성, "
             f"완료된 subtask: {len(completed_subtasks)}개")

    run_pipeline_from_subtasks(
        agent_hub_root, args.project, task_id, task_file,
        subtasks, pipeline, args.dummy, completed_subtasks,
        git_enabled=git_enabled, git_config=git_config,
        codebase_path=codebase_path, task_branch=task_branch,
        default_branch=default_branch,
        usage_thresholds=usage_thresholds,
        usage_check_interval=usage_check_interval,
    )


def run_pipeline_from_subtasks(agent_hub_root, project_name, task_id, task_file,
                                subtasks, pipeline, dummy, already_completed,
                                git_enabled=False, git_config=None,
                                codebase_path=None, task_branch=None,
                                default_branch="main",
                                usage_thresholds=None, usage_check_interval=60):
    """re-plan 후 남은 subtask들을 실행한다."""
    completed_subtasks = list(already_completed)
    git_config = git_config or {}

    # 토큰/requested_by는 task_file과 config에서 직접 resolve (caller 시그니처 간결 유지).
    _task_for_meta = load_json(task_file) if os.path.exists(task_file) else {}
    requested_by = _task_for_meta.get("requested_by")
    auth_token = git_config.get("auth_token", "")
    if not auth_token:
        try:
            cfg_path = os.path.join(agent_hub_root, "config.yaml")
            with open(cfg_path) as _f:
                _sys_config = yaml.safe_load(_f) or {}
            auth_token = (_sys_config.get("machines", {})
                          .get("executor", {})
                          .get("github_token", "")) or ""
        except Exception:
            auth_token = ""

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
            usage_thresholds=usage_thresholds,
            usage_check_interval=usage_check_interval,
            codebase_path=codebase_path, git_enabled=git_enabled,
            git_config=git_config, requested_by=requested_by,
        )

        if not subtask_success:
            task = load_json(task_file)
            if task.get("_needs_replan", False):
                log_error("re-plan 후에도 실패. 에스컬레이션이 필요합니다.")
            update_task_field(task_file, "status", "failed")
            project_dir = os.path.join(agent_hub_root, "projects", project_name)
            update_project_state(project_dir, status="idle", last_error=task_id)
            sys.exit(1)

        # Note: subtask commit은 run_subtask_pipeline 내부에서 Reviewer approved 시 수행됨.

        completed_subtasks.append(subtask_id)
        update_task_field(task_file, "completed_subtasks", completed_subtasks)

    # ─── Phase 3: Summarizer + PR (re-plan 경로) ───
    finalize_task(
        agent_hub_root, project_name, task_id, task_file,
        completed_subtasks, git_enabled, git_config,
        codebase_path, task_branch, default_branch, dummy,
        requested_by=requested_by,
    )


def finalize_task(agent_hub_root, project_name, task_id, task_file,
                  completed_subtasks, git_enabled, git_config,
                  codebase_path, task_branch, default_branch, dummy,
                  requested_by=None):
    """
    Subtask loop 완료 후 MemoryUpdater → Summarizer 실행 + PR 생성/머지 + task 상태 업데이트.
    run_pipeline()과 run_pipeline_from_subtasks() 양쪽에서 호출된다.
    """
    # ─── Memory Updater + Summarizer 공통 전처리 ───
    # 모든 subtask가 끝난 시점이므로 current_subtask를 먼저 비운다.
    # 남겨두면 safety_limits가 completed + current를 이중 집계해
    # max_subtask_count를 초과한 것으로 오판할 수 있다.
    update_task_field(task_file, "current_subtask", None)

    # ─── Memory Updater 실행 (Summarizer 직전) ───
    # 역할: codebase 루트의 PROJECT_NOTES.md(장기 메모리)를 이번 task 변경에 맞춰 증분 갱신.
    # 정책:
    #   - agent가 직접 PROJECT_NOTES.md만 수정한다 (다른 파일 수정 금지).
    #   - 변경이 생기면 WFC가 "[{task_id}] memory: PROJECT_NOTES.md 갱신" 커밋으로 묶어 PR에 포함.
    #   - 실패해도 PR 생성은 차단하지 않는다 (경고만). Summarizer 이후 push는 계속 진행.
    log_step("Memory Updater 실행")
    update_pipeline_stage(task_file, "memory_updater")
    memory_success, memory_data = run_agent(
        agent_hub_root, "memory_updater", project_name, task_id,
        dummy=dummy,
    )
    if not memory_success:
        log_warn("Memory Updater 실패 — PROJECT_NOTES.md 갱신 없이 계속 진행합니다.")
    elif memory_data and memory_data.get("updated"):
        log_info(
            "[memory] PROJECT_NOTES.md 갱신됨: "
            f"sections={memory_data.get('sections_changed') or []}"
        )
        # agent가 만든 PROJECT_NOTES.md 변경을 커밋 (push는 PR 생성 시 1회).
        # 변경이 실제로 있을 때만 커밋되고, 없으면 조용히 스킵된다.
        if git_enabled and codebase_path:
            memory_author_name = git_config.get("author_name", "agent-bot")
            memory_author_email = git_config.get("author_email", "agent@example.com")
            try:
                git_commit_worktree_no_push(
                    codebase_path,
                    f"[{task_id}] memory: PROJECT_NOTES.md 갱신",
                    memory_author_name, memory_author_email,
                )
            except RuntimeError as memory_commit_err:
                log_warn(f"[memory] 커밋 실패 — 변경은 worktree에 남습니다: {memory_commit_err}")
    else:
        log_info("[memory] 이번 task에서는 장기 메모리에 반영할 변경 없음")

    # ─── Summarizer 실행 ───
    log_step("Summarizer 실행")
    update_pipeline_stage(task_file, "summarizer")
    success, summary_data = run_agent(
        agent_hub_root, "summarizer", project_name, task_id,
        dummy=dummy,
    )

    tag = _format_task_tag(task_id, requested_by)
    pr_title = f"{tag} Task completed"
    pr_body = f"Automated PR for task {task_id}"
    task_summary = ""

    if success and summary_data:
        raw_title = summary_data.get("pr_title", pr_title)
        # Summarizer가 이미 [task_id] 접두사를 넣었을 수 있으므로 벗겨낸 뒤 tag를 붙인다.
        import re as _re
        stripped = _re.sub(rf"^\[{task_id}\]\s*", "", raw_title)
        stripped = _re.sub(r"^\[[^\]]+\]\s*", "", stripped) if requested_by else stripped
        pr_title = f"{tag} {stripped}" if stripped else f"{tag} Task completed"
        pr_body = summary_data.get("pr_body", pr_body)
        task_summary = summary_data.get("task_summary", "")
        update_task_field(task_file, "summary", task_summary)
    else:
        log_warn("Summarizer 실패 — 기본 PR 메시지를 사용합니다.")

    # ─── PR 생성 + 머지 ───
    if git_enabled and task_branch:
        pr_target = git_config.get("pr_target_branch", default_branch)
        # merge_strategy: require_human | pr_and_continue | auto_merge
        # 하위 호환: auto_merge(bool) 키가 있으면 변환
        if "merge_strategy" in git_config:
            merge_strategy = git_config["merge_strategy"]
        elif "auto_merge" in git_config:
            merge_strategy = "auto_merge" if git_config["auto_merge"] else "require_human"
        else:
            merge_strategy = "require_human"

        # PR 작업 전 gh 인증 재확인
        git_provider = git_config.get("provider", "github")
        finalize_auth_token = git_config.get("auth_token", "")
        if not finalize_auth_token:
            finalize_auth_token = config.get("machines", {}).get("executor", {}).get("github_token", "")
        if git_provider == "github":
            ensure_gh_auth(finalize_auth_token, codebase_path=codebase_path)

        # task 커밋들은 로컬에만 쌓여 있음 — PR 생성 전에 한 번만 push.
        # branch를 명시적으로 지정해서 다른 로컬 브랜치가 딸려가지 않도록 한다.
        log_step("Git: task 브랜치 push (PR 생성 전 1회)")
        update_pipeline_stage(task_file, "git_push")
        git_remote = git_config.get("remote", "origin")
        try:
            git_push(codebase_path, git_remote, task_branch, token=finalize_auth_token)
        except RuntimeError as push_err:
            log_error(f"[git] PR 생성 전 push 실패: {push_err}")
            record_failure_reason(task_file, f"PR 생성 전 push 실패: {push_err}")
            update_task_field(task_file, "status", "failed")
            finalize_project_dir = os.path.join(agent_hub_root, "projects", project_name)
            update_project_state(finalize_project_dir, status="idle", last_error=task_id)
            emit_notification(
                project_dir=finalize_project_dir, event_type="task_failed", task_id=task_id,
                message=f"task 브랜치 push 실패: {push_err}",
            )
            sys.exit(1)

        log_step("Git: PR 생성")
        update_pipeline_stage(task_file, "pr_create")
        try:
            pr_url = git_create_pr(codebase_path, task_branch, pr_target, pr_title, pr_body)
            update_task_field(task_file, "pr_url", pr_url)

            # PR 생성 알림
            finalize_project_dir = os.path.join(agent_hub_root, "projects", project_name)
            emit_notification(
                project_dir=finalize_project_dir,
                event_type="pr_created",
                task_id=task_id,
                message=f"PR 생성됨: {pr_title}",
                details={"pr_url": pr_url},
            )

            if merge_strategy == "auto_merge":
                log_step("Git: PR 자동 머지")
                try:
                    git_merge_pr(codebase_path, pr_url)
                except RuntimeError as merge_err:
                    # 머지 실패 (예: conflict) → task를 사용자 대기 상태로 유지하고 알림 발송.
                    # 사용자는 Web UI의 PR 버튼(Merge PR Now / Mark as Merged 등)으로 처리할 수 있다.
                    log_error(f"[git] auto_merge 실패 → 사용자 개입 대기로 전환: {merge_err}")
                    update_task_field(task_file, "status", "waiting_for_human_pr_approve")
                    update_task_field(task_file, "pr_merge_error", str(merge_err))
                    update_task_field(task_file, "pr_merge_error_at", datetime.now(timezone.utc).isoformat())
                    emit_notification(
                        project_dir=finalize_project_dir,
                        event_type="pr_merge_failed",
                        task_id=task_id,
                        message=f"PR 자동 머지 실패 (사용자 개입 필요): {merge_err}",
                        details={"pr_url": pr_url, "error": str(merge_err)},
                    )
                    # project_state를 idle로 전환해 다른 task 진행 가능하도록 (require_human과 동일 취급)
                    update_project_state(finalize_project_dir, status="idle")
                    update_task_field(task_file, "current_subtask", None)
                    update_pipeline_stage(task_file, "done")
                    return
                update_task_field(task_file, "status", "completed")
                emit_notification(
                    project_dir=finalize_project_dir,
                    event_type="pr_merged",
                    task_id=task_id,
                    message=f"PR 자동 머지 완료: {pr_title}",
                    details={"pr_url": pr_url},
                )
            elif merge_strategy == "pr_and_continue":
                log_info(f"[git] merge_strategy=pr_and_continue — PR 생성 완료, task 즉시 완료: {pr_url}")
                update_task_field(task_file, "status", "completed")
            else:  # require_human (기본값)
                log_info(f"[git] merge_strategy=require_human — PR 생성 완료. 수동 머지 대기: {pr_url}")
                update_task_field(task_file, "status", "waiting_for_human_pr_approve")
        except RuntimeError as e:
            log_error(f"PR 처리 실패: {e}")
            record_failure_reason(task_file, f"PR 처리 실패: {e}")
            update_task_field(task_file, "status", "failed")
            finalize_project_dir = os.path.join(agent_hub_root, "projects", project_name)
            update_project_state(finalize_project_dir, status="idle", last_error=task_id)
            emit_notification(
                project_dir=finalize_project_dir, event_type="task_failed", task_id=task_id,
                message=f"PR 처리 실패: {e}",
            )
            sys.exit(1)
    else:
        update_task_field(task_file, "status", "completed")

    update_task_field(task_file, "current_subtask", None)
    update_pipeline_stage(task_file, "done")

    # project_state.json 갱신 (TM 연동용)
    finalize_project_dir = os.path.join(agent_hub_root, "projects", project_name)
    update_project_state(finalize_project_dir, status="idle")

    # task 완료 알림
    task_final = load_json(task_file)
    final_status = task_final.get("status", "completed")
    pr_url = task_final.get("pr_url")
    details = {}
    if pr_url:
        details["pr_url"] = pr_url
    details["subtask_count"] = len(completed_subtasks)

    emit_notification(
        project_dir=finalize_project_dir,
        event_type="task_completed",
        task_id=task_id,
        message=f"task 완료 (subtask {len(completed_subtasks)}개){f' — PR: {pr_url}' if pr_url else ''}",
        details=details,
    )

    log_step("파이프라인 완료")
    log_info(f"task {task_id} 완료. subtask {len(completed_subtasks)}개 처리됨.")


def _subtask_file_path(project_name, task_id, subtask_id, agent_hub_root):
    """tasks/{task_id}/subtask-{seq}.json 경로를 반환한다."""
    seq = subtask_id.split("-")[-1].zfill(2)
    return os.path.join(
        agent_hub_root, "projects", project_name,
        "tasks", task_id, f"subtask-{seq}.json",
    )


def _inject_subtask_runtime_fields(subtask_file, **fields):
    """
    subtask JSON에 runtime 필드를 병합 저장한다.
    Coder/Reviewer 실행 직전에 retry_mode, attempt_history, subtask_start_sha,
    latest_instructions 등을 주입하는 용도.
    subtask 파일이 없으면 (dummy 테스트 등) 조용히 건너뛴다.
    """
    if not os.path.exists(subtask_file):
        return
    data = load_json(subtask_file)
    for key, value in fields.items():
        data[key] = value
    save_json(subtask_file, data)


def run_subtask_pipeline(agent_hub_root, project_name, task_id, subtask_id,
                          task_file, pipeline, dummy,
                          usage_thresholds=None, usage_check_interval=60,
                          codebase_path=None, git_enabled=False, git_config=None,
                          requested_by=None):
    """
    단일 subtask에 대해 pipeline을 실행한다. 성공 시 True, 실패 시 False.

    flow (Coder/Reviewer 쌍 기준):
      1. subtask 시작 시점의 HEAD SHA를 캡처 (in-memory)
      2. loop (attempt):
         a. Coder 실행 전 subtask JSON에 retry_mode/attempt_history/latest_instructions 주입
         b. Coder 실행
         c. Coder가 몰래 commit을 만들었으면 soft reset으로 되돌림
         d. Reviewer 실행 전 subtask JSON에 start_sha/attempt_history 주입
         e. Reviewer 실행 + output schema 검증 (실패 시 1회 재호출)
         f. approved: WFC가 즉시 commit (push 안 함) → subtask 성공 종료
         g. rejected: retry_mode에 따라 worktree 정리 (reset이면 hard reset, continue면 유지)
            → attempt_history에 원문 append 후 다시 loop
      3. Reporter/setup/tester 등 나머지 pipeline 단계는 이후 순차 실행
    """
    git_config = git_config or {}
    project_dir = os.path.join(agent_hub_root, "projects", project_name)

    # ─── subtask 시작 SHA 캡처 (git enabled일 때만 의미 있음) ───
    subtask_start_sha = None
    if git_enabled and codebase_path and not dummy:
        try:
            subtask_start_sha = git_head_sha(codebase_path)
            log_info(f"[{subtask_id}] subtask_start_sha={subtask_start_sha[:8]}")
        except RuntimeError as e:
            log_warn(f"[{subtask_id}] start_sha 캡처 실패 (리셋 모드 비활성): {e}")

    subtask_file = _subtask_file_path(project_name, task_id, subtask_id, agent_hub_root)

    # pipeline을 Coder/Reviewer 쌍 구간과 그 이후 구간으로 분리
    #   - Coder/Reviewer 쌍: retry loop 안에서 실행
    #   - 나머지 (setup/tester/reporter 등): retry loop 성공 후 순차 실행
    reviewer_idx = pipeline.index("reviewer") if "reviewer" in pipeline else -1
    coder_reviewer_phase = pipeline[: reviewer_idx + 1] if reviewer_idx >= 0 else pipeline
    post_review_phase = pipeline[reviewer_idx + 1:] if reviewer_idx >= 0 else []

    attempt_history = []
    latest_instructions = ""
    retry_mode = None  # 첫 attempt는 None → Coder는 일반 진행

    # ─── Coder ↔ Reviewer retry 루프 ───
    if "coder" in coder_reviewer_phase:
        while True:
            task_data = load_json(task_file)
            retry_count = task_data.get("counters", {}).get("current_subtask_retry", 0)
            attempt_num = retry_count + 1

            for agent_type in coder_reviewer_phase:
                # usage check
                if not dummy and usage_thresholds:
                    agent_stage_threshold = usage_thresholds.get("new_agent_stage", 0.90)
                    wait_until_below_threshold(
                        agent_stage_threshold,
                        check_interval_seconds=usage_check_interval,
                        level_name=f"new_agent_stage/{agent_type}",
                        log_fn=log_info,
                    )

                # ─── Coder 실행 ───
                if agent_type == "coder":
                    _inject_subtask_runtime_fields(
                        subtask_file,
                        attempt=attempt_num,
                        retry_mode=retry_mode,
                        latest_instructions=latest_instructions,
                        attempt_history=attempt_history,
                        subtask_start_sha=subtask_start_sha or "",
                    )
                    before_sha = git_head_sha(codebase_path) if (git_enabled and codebase_path and not dummy) else None
                    log_info(f"[{subtask_id}] coder 실행 (attempt {attempt_num}, mode={retry_mode})")
                    update_pipeline_stage(task_file, "coder", f"subtask {subtask_id}")
                    success, result = run_agent(
                        agent_hub_root, "coder", project_name, task_id,
                        subtask_id=subtask_id, dummy=dummy,
                    )
                    if not success:
                        log_error(f"[{subtask_id}] coder 실행 실패")
                        record_failure_reason(task_file, f"coder 실행 실패 (subtask {subtask_id})")
                        return False
                    # 몰래 commit 방어
                    if before_sha:
                        git_soft_reset_if_moved(codebase_path, before_sha)
                    coder_intent_report = result.get("intent_report") or {}
                    continue

                # ─── Reviewer 실행 (스키마 검증 + 1회 재호출) ───
                if agent_type == "reviewer":
                    _inject_subtask_runtime_fields(
                        subtask_file,
                        attempt=attempt_num,
                        attempt_history=attempt_history,
                        subtask_start_sha=subtask_start_sha or "",
                    )
                    log_info(f"[{subtask_id}] reviewer 실행 (attempt {attempt_num})")
                    update_pipeline_stage(task_file, "reviewer", f"subtask {subtask_id}")
                    success, result = run_agent(
                        agent_hub_root, "reviewer", project_name, task_id,
                        subtask_id=subtask_id, dummy=dummy,
                    )
                    if not success:
                        log_error(f"[{subtask_id}] reviewer 실행 실패")
                        record_failure_reason(task_file, f"reviewer 실행 실패 (subtask {subtask_id})")
                        return False

                    valid, reason = validate_reviewer_output(result)
                    if not valid and not dummy:
                        log_warn(f"[{subtask_id}] reviewer 출력 스키마 실패: {reason}. 1회 재호출.")
                        success, result = run_agent(
                            agent_hub_root, "reviewer", project_name, task_id,
                            subtask_id=subtask_id, dummy=dummy,
                        )
                        if success:
                            valid, reason = validate_reviewer_output(result)
                        if not valid:
                            log_error(f"[{subtask_id}] reviewer 재호출 후에도 스키마 실패: {reason}")
                            record_failure_reason(task_file, f"reviewer 스키마 실패: {reason}")
                            return False

                    action = result.get("action", "")

                    # ─── Approved: WFC가 즉시 commit (no push) ───
                    if action == "approved":
                        if git_enabled and codebase_path and not dummy:
                            subtask_title = load_json(subtask_file).get("title", subtask_id)
                            tag = _format_task_tag(task_id, requested_by)
                            commit_msg = f"{tag} {subtask_id}: {subtask_title}"
                            try:
                                git_commit_worktree_no_push(
                                    codebase_path, commit_msg,
                                    git_config.get("author_name", "Agent Hub"),
                                    git_config.get("author_email", "agent@hub"),
                                )
                            except RuntimeError as e:
                                log_error(f"[git] 커밋 실패: {e}")
                                record_failure_reason(task_file, f"commit 실패: {e}")
                                return False
                        log_info(f"[{subtask_id}] reviewer 승인 (attempt {attempt_num})")
                        break  # coder_reviewer_phase loop 탈출

                    # ─── Rejected: history 누적 후 worktree 처리 ───
                    log_warn(f"[{subtask_id}] reviewer 거절 (mode={result.get('retry_mode')})")
                    attempt_history.append({
                        "attempt": attempt_num,
                        "coder_intent_report": coder_intent_report,
                        "reviewer_feedback": {
                            "retry_mode": result.get("retry_mode"),
                            "current_state_summary": result.get("current_state_summary", ""),
                            "what_is_wrong": result.get("what_is_wrong", ""),
                            "what_should_be": result.get("what_should_be", ""),
                            "actionable_instructions": result.get("actionable_instructions", []),
                            "feedback": result.get("feedback", ""),
                        },
                    })
                    retry_mode = result.get("retry_mode")
                    latest_instructions = "\n".join(
                        f"- {x}" for x in result.get("actionable_instructions", [])
                    )

                    # reset 모드면 worktree를 시작 지점으로 되돌림
                    if retry_mode == "reset" and subtask_start_sha and not dummy:
                        try:
                            git_reset_hard_to(codebase_path, subtask_start_sha)
                        except RuntimeError as e:
                            log_error(f"[git] reset 실패: {e}")
                            record_failure_reason(task_file, f"reset 실패: {e}")
                            return False

                    # retry 카운터 증가 후 while 재진입
                    update_task_counter(task_file, "current_subtask_retry", increment=True)
                    break  # for agent_type 루프 탈출 → 다시 while

            else:
                # for가 break 없이 정상 종료 = approved로 break 되지 않은 경우
                # (coder만 있고 reviewer가 없는 구성인데, 위에서 이미 걸러짐)
                break
            # for 루프가 break로 종료된 경우 action 확인
            if action == "approved":
                break
            # rejected: while 재진입하여 다시 coder

    # ─── 나머지 pipeline 단계 (setup/tester/reporter 등) ───
    for agent_type in post_review_phase:
        if not dummy and usage_thresholds:
            agent_stage_threshold = usage_thresholds.get("new_agent_stage", 0.90)
            wait_until_below_threshold(
                agent_stage_threshold,
                check_interval_seconds=usage_check_interval,
                level_name=f"new_agent_stage/{agent_type}",
                log_fn=log_info,
            )

        log_info(f"[{subtask_id}] {agent_type} 실행")
        update_pipeline_stage(task_file, agent_type, f"subtask {subtask_id}")
        success, result = run_agent(
            agent_hub_root, agent_type, project_name, task_id,
            subtask_id=subtask_id, dummy=dummy,
        )
        if not success:
            log_error(f"[{subtask_id}] {agent_type} 실행 실패")
            record_failure_reason(task_file, f"{agent_type} 실행 실패 (subtask {subtask_id})")
            return False

        # Reporter: replan 요청
        if agent_type == "reporter" and result.get("needs_replan", False):
            log_warn(f"[{subtask_id}] reporter가 re-plan 요청")
            update_task_field(task_file, "_needs_replan", True)
            return False

        # Reporter: 실패 판정 → 전체 subtask 재시도
        if agent_type == "reporter" and result.get("verdict") == "fail":
            log_warn(f"[{subtask_id}] reporter 실패 판정 — subtask 재시도")
            update_task_counter(task_file, "current_subtask_retry", increment=True)
            return run_subtask_pipeline(
                agent_hub_root, project_name, task_id, subtask_id,
                task_file, pipeline, dummy,
                usage_thresholds=usage_thresholds,
                usage_check_interval=usage_check_interval,
                codebase_path=codebase_path, git_enabled=git_enabled,
                git_config=git_config, requested_by=requested_by,
            )

    log_info(f"[{subtask_id}] pipeline 완료")
    return True


def main():
    parser = argparse.ArgumentParser(description="Workflow Controller — 파이프라인 자동 실행")
    parser.add_argument("--project", required=True, help="프로젝트명")
    parser.add_argument("--task", required=True, help="task ID (5자리 숫자)")
    parser.add_argument("--dummy", action="store_true", help="모든 agent를 dummy 모드로 실행")
    parser.add_argument("--dry-run", action="store_true", help="pipeline 구성만 확인 (실행 안 함)")
    parser.add_argument("--resume", action="store_true",
                        help="중단된 대기 상태에서 파이프라인 재개")
    args = parser.parse_args()

    # SIGTERM/SIGINT 핸들러 등록 — 대기 루프에서 깨끗한 종료를 위해
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)

    if args.resume:
        run_pipeline_resume(args)
    else:
        run_pipeline(args)


if __name__ == "__main__":
    main()
