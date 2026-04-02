#!/usr/bin/env bash
# run_agent.sh — Agent Hub CLI (v2, Phase 1.0)
#
# 사용법:
#   ./run_agent.sh run <agent_type> --project <name> --task <id> [--subtask <id>] [--dry-run]
#   ./run_agent.sh init-project
#   ./run_agent.sh help

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ─── 색상 출력 ───
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

log_info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1" >&2; }

# ─── 유효한 agent 목록 ───
VALID_AGENTS="planner coder reviewer setup unit_tester e2e_tester reporter"

# ─── 시스템 설정 파일 ───
CONFIG_FILE="${SCRIPT_DIR}/config.yaml"

# ═══════════════════════════════════════════════════════════
# help 명령
# ═══════════════════════════════════════════════════════════
show_help() {
    echo -e "${CYAN}Agent Hub — CLI (Phase 1.0)${NC}"
    echo ""
    echo "사용법:"
    echo "  ./run_agent.sh <command> [options]"
    echo ""
    echo "명령:"
    echo "  run <agent_type> --project <name> --task <id> [--subtask <id>] [--dry-run] [--dummy]"
    echo "                       수동으로 agent 하나 실행"
    echo "  init-project         대화형 프로젝트 초기화"
    echo "  kill-all             모든 agent 프로세스 종료 (claude -p 포함)"
    echo "  help                 이 도움말 표시"
    echo ""
    echo "agent_type:"
    echo "  planner, coder, reviewer, setup, unit_tester, e2e_tester, reporter"
    echo ""
    echo "예시:"
    echo "  ./run_agent.sh init-project"
    echo "  ./run_agent.sh run coder --project my-app --task 00001"
    echo "  ./run_agent.sh run coder --project my-app --task 00001 --subtask 00001-1"
    echo "  ./run_agent.sh run coder --project my-app --task 00001 --dry-run"
    echo ""
    echo "task 파일명 규칙: 00001-간단한-설명.json (--task에는 00001만 지정)"
    echo ""
    echo "Phase 1.1+ 명령 (미구현):"
    echo "  start, stop, status, submit, pending, approve, reject, list"
}

# ═══════════════════════════════════════════════════════════
# kill-all 명령
# ═══════════════════════════════════════════════════════════
cmd_kill_all() {
    local force=false
    if [[ "${1:-}" == "--force" ]]; then
        force=true
    fi
    local pid_dir="${SCRIPT_DIR}/.pids"
    local killed=0
    local stale=0

    echo ""
    log_info "=== Agent Hub 프로세스 종료 ==="

    # 1단계: PID 파일 기반 종료 (우리가 추적하는 프로세스)
    if [[ -d "$pid_dir" ]] && ls "$pid_dir"/*.pid &>/dev/null; then
        for pid_file in "$pid_dir"/*.pid; do
            local pid
            pid=$(python3 -c "import json; print(json.load(open('${pid_file}'))['pid'])" 2>/dev/null || echo "")
            local agent_type
            agent_type=$(python3 -c "import json; print(json.load(open('${pid_file}'))['agent_type'])" 2>/dev/null || echo "unknown")
            local task_id
            task_id=$(python3 -c "import json; print(json.load(open('${pid_file}'))['task_id'])" 2>/dev/null || echo "unknown")

            if [[ -z "$pid" ]]; then
                rm -f "$pid_file"
                continue
            fi

            # 프로세스가 실제로 살아있는지 확인
            if kill -0 "$pid" 2>/dev/null; then
                # 프로세스 그룹 전체를 종료 (자식 프로세스 포함)
                kill -- -"$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true
                log_info "종료: PID=${pid} agent=${agent_type} task=${task_id}"
                killed=$((killed + 1))
            else
                stale=$((stale + 1))
            fi
            rm -f "$pid_file"
        done
    fi

    # 2단계: 잔여 claude -p 프로세스 정리 (PID 파일 없이 남은 것들)
    local orphan_pids
    orphan_pids=$(pgrep -f "claude.*--dangerously-skip-permissions" 2>/dev/null || true)
    if [[ -n "$orphan_pids" ]]; then
        echo ""
        log_warn "PID 파일 없는 잔여 claude 프로세스 발견:"
        echo "$orphan_pids" | while read -r opid; do
            local cmd
            cmd=$(ps -p "$opid" -o args= 2>/dev/null || echo "unknown")
            log_warn "  PID=${opid}: ${cmd:0:80}"
        done

        echo ""
        local answer="Y"
        if [[ "$force" != "true" ]]; then
            read -rp "잔여 프로세스도 종료할까요? (Y/n) " answer
        fi
        if [[ "${answer:-Y}" =~ ^[Yy]?$ ]]; then
            echo "$orphan_pids" | while read -r opid; do
                kill "$opid" 2>/dev/null || true
                killed=$((killed + 1))
            done
            log_info "잔여 프로세스 종료 완료"
        fi
    fi

    # 3단계: PID 디렉토리 정리
    if [[ -d "$pid_dir" ]]; then
        rm -f "$pid_dir"/*.pid 2>/dev/null
    fi

    echo ""
    log_info "결과: ${killed}개 종료, ${stale}개 이미 종료된 PID 정리"
    echo ""
}

# ═══════════════════════════════════════════════════════════
# init-project 명령
# ═══════════════════════════════════════════════════════════
cmd_init_project() {
    # venv 활성화 (PyYAML 필요)
    if [[ -f "${SCRIPT_DIR}/activate_venv.sh" ]]; then
        source "${SCRIPT_DIR}/activate_venv.sh"
    fi

    python3 "${SCRIPT_DIR}/scripts/init_project.py"
}

# ═══════════════════════════════════════════════════════════
# run 명령
# ═══════════════════════════════════════════════════════════
cmd_run() {
    # agent_type은 첫 번째 인자
    local agent_type="${1:?agent_type을 지정하세요. ./run_agent.sh help 참고}"
    shift

    # agent_type 유효성 검증
    if ! echo "$VALID_AGENTS" | grep -qw "$agent_type"; then
        log_error "유효하지 않은 agent_type: ${agent_type}"
        log_error "가능한 값: ${VALID_AGENTS}"
        exit 1
    fi

    # 옵션 파싱
    local project_name=""
    local task_id=""
    local subtask_id=""
    local dry_run=false
    local dummy=false

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --project)
                project_name="$2"
                shift 2
                ;;
            --task)
                task_id="$2"
                shift 2
                ;;
            --subtask)
                subtask_id="$2"
                shift 2
                ;;
            --dry-run)
                dry_run=true
                shift
                ;;
            --dummy)
                dummy=true
                shift
                ;;
            *)
                log_error "알 수 없는 옵션: $1"
                exit 1
                ;;
        esac
    done

    # 필수 옵션 검증
    if [[ -z "$project_name" ]]; then
        log_error "--project 옵션이 필요합니다."
        exit 1
    fi
    if [[ -z "$task_id" ]]; then
        log_error "--task 옵션이 필요합니다."
        exit 1
    fi

    # config.yaml 존재 확인
    if [[ ! -f "$CONFIG_FILE" ]]; then
        log_error "시스템 설정 파일이 없습니다: ${CONFIG_FILE}"
        log_error "./create_config.sh 를 먼저 실행하세요."
        exit 1
    fi

    # 프로젝트 디렉토리 및 project.yaml 확인
    local project_dir="${SCRIPT_DIR}/projects/${project_name}"
    local project_yaml="${project_dir}/project.yaml"

    if [[ ! -d "$project_dir" ]]; then
        log_error "프로젝트 디렉토리가 없습니다: ${project_dir}"
        log_error "./run_agent.sh init-project 으로 프로젝트를 먼저 생성하세요."
        exit 1
    fi
    if [[ ! -f "$project_yaml" ]]; then
        log_error "프로젝트 설정 파일이 없습니다: ${project_yaml}"
        exit 1
    fi

    # task JSON 확인 (00001-*.json 패턴 지원)
    local task_file
    task_file=$(find "${project_dir}/tasks" -maxdepth 1 -name "${task_id}-*.json" -o -name "${task_id}.json" 2>/dev/null | head -1)
    if [[ -z "$task_file" || ! -f "$task_file" ]]; then
        log_error "task 파일이 없습니다: ${project_dir}/tasks/${task_id}[-*].json"
        log_error "task JSON을 수동으로 작성해서 넣어주세요."
        exit 1
    fi

    # subtask JSON 확인 (지정된 경우, 00001-1-*.json 패턴 지원)
    local subtask_file=""
    if [[ -n "$subtask_id" ]]; then
        subtask_file=$(find "${project_dir}/tasks" -maxdepth 1 -name "${subtask_id}-*.json" -o -name "${subtask_id}.json" 2>/dev/null | head -1)
        if [[ -z "$subtask_file" || ! -f "$subtask_file" ]]; then
            log_error "subtask 파일이 없습니다: ${project_dir}/tasks/${subtask_id}[-*].json"
            exit 1
        fi
    fi

    # 실행 정보 출력
    log_info "Agent 실행 준비"
    log_info "  프로젝트: ${project_name}"
    log_info "  agent: ${agent_type}"
    log_info "  task: ${task_id}"
    if [[ -n "$subtask_id" ]]; then
        log_info "  subtask: ${subtask_id}"
    fi
    if [[ "$dry_run" == "true" ]]; then
        log_warn "  DRY-RUN 모드 (claude 호출 없이 프롬프트만 출력)"
    fi
    if [[ "$dummy" == "true" ]]; then
        log_warn "  DUMMY 모드 (claude 호출 없이 더미 JSON 출력)"
    fi
    echo ""

    # venv 활성화 (PyYAML 필요)
    if [[ -f "${SCRIPT_DIR}/activate_venv.sh" ]]; then
        source "${SCRIPT_DIR}/activate_venv.sh"
    fi

    # run_claude_agent.sh 호출
    local run_args=(
        "${SCRIPT_DIR}/scripts/run_claude_agent.sh"
        "$agent_type"
        --config "$CONFIG_FILE"
        --project-yaml "$project_yaml"
        --task-file "$task_file"
    )

    if [[ -n "$subtask_file" ]]; then
        run_args+=(--subtask-file "$subtask_file")
    fi

    if [[ "$dry_run" == "true" ]]; then
        run_args+=(--dry-run)
    fi

    if [[ "$dummy" == "true" ]]; then
        run_args+=(--dummy)
    fi

    exec "${run_args[@]}"
}

# ═══════════════════════════════════════════════════════════
# 메인 분기
# ═══════════════════════════════════════════════════════════
COMMAND="${1:-help}"

case "$COMMAND" in
    run)
        shift
        cmd_run "$@"
        ;;
    init-project)
        cmd_init_project
        ;;
    help|--help|-h)
        show_help
        ;;
    kill-all)
        shift
        cmd_kill_all "$@"
        ;;
    start|stop|status|submit|pending|approve|reject|list)
        log_warn "'${COMMAND}' 명령은 Phase 1.1+에서 구현 예정입니다."
        log_warn "현재 Phase 1.0에서는 run, init-project, kill-all만 사용 가능합니다."
        exit 1
        ;;
    *)
        log_error "알 수 없는 명령: ${COMMAND}"
        echo ""
        show_help
        exit 1
        ;;
esac
