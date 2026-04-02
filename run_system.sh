#!/usr/bin/env bash
# run_system.sh — Agent Hub 시스템 관리 CLI
#
# 사용법:
#   ./run_system.sh start          # Task Manager 백그라운드 실행
#   ./run_system.sh stop           # Task Manager 종료 (실행 중 WFC는 완료 대기)
#   ./run_system.sh stop --force   # Task Manager + 모든 WFC 즉시 강제종료
#   ./run_system.sh status         # 시스템 상태 출력
#   ./run_system.sh help

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ─── 색상 출력 ───
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1" >&2; }

# ─── 공통 경로 ───
CONFIG_FILE="${SCRIPT_DIR}/config.yaml"
PID_DIR="${SCRIPT_DIR}/.pids"
# TM PID 파일: task_manager.{PID}.pid 패턴
LOG_DIR="${SCRIPT_DIR}/logs"
TM_LOG="${LOG_DIR}/task_manager.log"

# ═══════════════════════════════════════════════════════════
# help 명령
# ═══════════════════════════════════════════════════════════
show_help() {
    echo -e "${CYAN}Agent Hub — 시스템 관리 CLI${NC}"
    echo ""
    echo "사용법:"
    echo "  ./run_system.sh <command> [options]"
    echo ""
    echo "명령:"
    echo "  start              Task Manager를 백그라운드로 실행"
    echo "  start --dummy      Task Manager를 dummy 모드로 실행 (claude 호출 없이)"
    echo "  stop               Task Manager 종료 (실행 중 WFC는 완료 대기)"
    echo "  stop --force       Task Manager + 모든 WFC 즉시 강제종료"
    echo "  status             시스템 상태 출력"
    echo "  help               이 도움말 표시"
    echo ""
    echo "agent/pipeline 직접 실행은 ./run_agent.sh 를 사용하세요."
    echo ""
    echo "로그 확인:"
    echo "  tail -f logs/task_manager.log"
}

# ═══════════════════════════════════════════════════════════
# TM PID 읽기 헬퍼
# ═══════════════════════════════════════════════════════════
find_tm_pid_file() {
    # task_manager.*.pid 패턴으로 TM PID 파일을 찾아 경로를 출력한다.
    # 없으면 빈 문자열을 출력한다.
    local found
    found=$(ls "${PID_DIR}"/task_manager.*.pid 2>/dev/null | head -1)
    echo "${found:-}"
}

read_tm_pid() {
    # TM PID를 찾아 stdout에 출력한다.
    # 1차: PID 파일에서 추출 (task_manager.{PID}.pid)
    # 2차: PID 파일이 없으면 pgrep으로 실제 프로세스 탐색
    local pid_file
    pid_file=$(find_tm_pid_file)
    if [[ -n "$pid_file" ]]; then
        local basename
        basename=$(basename "$pid_file")
        local pid
        pid=$(echo "$basename" | sed 's/^task_manager\.\(.*\)\.pid$/\1/')
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            echo "$pid"
            return
        fi
    fi
    # fallback: pgrep으로 task_manager.py 프로세스 탐색
    local pgrep_pid
    pgrep_pid=$(pgrep -f "scripts/task_manager.py" 2>/dev/null | head -1)
    echo "${pgrep_pid:-}"
}

is_tm_running() {
    # TM 프로세스가 실행 중이면 0(true), 아니면 1(false)를 반환한다.
    local pid
    pid=$(read_tm_pid)
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
        return 0
    fi
    return 1
}

has_tm_pid_file() {
    # PID 파일이 존재하는지 확인한다 (stale 포함).
    local pid_file
    pid_file=$(find_tm_pid_file)
    [[ -n "$pid_file" ]]
}

# ═══════════════════════════════════════════════════════════
# start 명령
# ═══════════════════════════════════════════════════════════
cmd_start() {
    local dummy=false
    if [[ "${1:-}" == "--dummy" ]]; then
        dummy=true
    fi

    # 이미 실행 중인지 확인
    if is_tm_running; then
        local existing_pid
        existing_pid=$(read_tm_pid)
        log_warn "Task Manager가 이미 실행 중입니다 (PID: ${existing_pid})"
        log_warn "종료하려면: ./run_system.sh stop"
        exit 1
    fi

    # stale PID 파일 정리
    rm -f "${PID_DIR}"/task_manager.*.pid 2>/dev/null

    # config.yaml 존재 확인
    if [[ ! -f "$CONFIG_FILE" ]]; then
        log_error "시스템 설정 파일이 없습니다: ${CONFIG_FILE}"
        log_error "./create_config.sh 를 먼저 실행하세요."
        exit 1
    fi

    # venv 활성화 (PyYAML 필요)
    if [[ -f "${SCRIPT_DIR}/activate_venv.sh" ]]; then
        source "${SCRIPT_DIR}/activate_venv.sh"
    fi

    # .pids 디렉토리 생성
    mkdir -p "$PID_DIR"

    # Task Manager를 백그라운드로 실행
    # TM 자체가 파일 로거를 갖고 있으므로 nohup 출력은 최소한으로
    local tm_args=("${SCRIPT_DIR}/scripts/task_manager.py" --config "${CONFIG_FILE}")
    if [[ "$dummy" == "true" ]]; then
        tm_args+=(--dummy)
        log_warn "DUMMY 모드: WFC가 claude 호출 없이 더미 JSON으로 실행됩니다."
    fi
    nohup python3 "${tm_args[@]}" > /dev/null 2>&1 &
    local tm_pid=$!

    # 프로세스가 즉시 죽지 않았는지 짧게 확인
    sleep 1
    if ! kill -0 "$tm_pid" 2>/dev/null; then
        log_error "Task Manager 시작 실패. 로그를 확인하세요: ${TM_LOG}"
        exit 1
    fi

    echo ""
    log_info "Task Manager 시작됨 (PID: ${tm_pid})"
    log_info "  로그 확인: tail -f ${TM_LOG}"
    log_info "  상태 확인: ./run_system.sh status"
    log_info "  종료 방법: ./run_system.sh stop"
    echo ""
}

# ═══════════════════════════════════════════════════════════
# stop 명령
# ═══════════════════════════════════════════════════════════
cmd_stop() {
    local force=false
    if [[ "${1:-}" == "--force" ]]; then
        force=true
    fi

    if ! is_tm_running; then
        log_warn "Task Manager가 실행 중이 아닙니다."
        # stale PID 파일 정리
        rm -f "${PID_DIR}"/task_manager.*.pid 2>/dev/null
        exit 0
    fi

    local tm_pid
    tm_pid=$(read_tm_pid)

    if [[ "$force" == "true" ]]; then
        # SIGUSR1: 모든 WFC 강제종료 후 TM 즉시 종료
        echo ""
        log_warn "강제종료 요청 (PID: ${tm_pid})..."
        kill -USR1 "$tm_pid" 2>/dev/null || true

        # 최대 10초 대기
        local waited=0
        while kill -0 "$tm_pid" 2>/dev/null && [[ $waited -lt 10 ]]; do
            sleep 1
            waited=$((waited + 1))
        done

        # 그래도 안 죽었으면 SIGKILL
        if kill -0 "$tm_pid" 2>/dev/null; then
            log_warn "TM 프로세스가 응답하지 않음. SIGKILL 전송..."
            kill -9 "$tm_pid" 2>/dev/null || true
        fi

        rm -f "${PID_DIR}"/task_manager.*.pid 2>/dev/null
        log_info "Task Manager + WFC 강제종료 완료"
        echo ""
    else
        # SIGTERM: 새 task spawn 중단, 실행 중 WFC 완료 대기
        echo ""
        log_info "Task Manager 종료 요청 (PID: ${tm_pid})..."
        log_info "실행 중인 WFC가 있으면 완료를 대기합니다."
        log_info "(즉시 종료하려면: ./run_system.sh stop --force)"
        kill "$tm_pid" 2>/dev/null || true

        # 대기 (TM이 WFC 완료 후 스스로 종료)
        # 진행 상황 표시
        local waited=0
        while kill -0 "$tm_pid" 2>/dev/null; do
            if [[ $((waited % 10)) -eq 0 ]] && [[ $waited -gt 0 ]]; then
                log_info "WFC 완료 대기 중... (${waited}초 경과)"
            fi
            sleep 1
            waited=$((waited + 1))
        done

        rm -f "${PID_DIR}"/task_manager.*.pid 2>/dev/null
        log_info "Task Manager 종료 완료 (${waited}초 소요)"
        echo ""
    fi
}

# ═══════════════════════════════════════════════════════════
# status 명령
# ═══════════════════════════════════════════════════════════
cmd_status() {
    echo ""
    log_info "=== Agent Hub 상태 ==="
    echo ""

    # TM 상태
    if is_tm_running; then
        local tm_pid
        tm_pid=$(read_tm_pid)
        if has_tm_pid_file; then
            log_info "Task Manager: ${GREEN}실행 중${NC} (PID ${tm_pid})"
        else
            log_warn "Task Manager: ${GREEN}실행 중${NC} (PID ${tm_pid}, PID 파일 없음 — pgrep으로 발견)"
        fi
    elif has_tm_pid_file; then
        log_warn "Task Manager: 종료됨 (stale PID 파일)"
    else
        log_warn "Task Manager: 미실행"
    fi

    # 프로젝트별 상태
    echo ""
    log_info "─── 프로젝트별 상태 ───"

    local found_any=false
    for project_dir in "${SCRIPT_DIR}"/projects/*/; do
        [[ -d "$project_dir" ]] || continue
        [[ -f "${project_dir}/project.yaml" ]] || continue

        found_any=true
        local name
        name=$(basename "$project_dir")
        local state_file="${project_dir}/project_state.json"

        if [[ -f "$state_file" ]]; then
            python3 -c "
import json, sys
CYAN = '\033[0;36m'
BLUE = '\033[1;34m'
RED = '\033[0;31m'
NC = '\033[0m'
try:
    with open('${state_file}') as f:
        s = json.load(f)
    status = s.get('status', 'unknown')
    task = s.get('current_task_id', '')
    task_str = f' — task {task}' if task else ''
    last_error = s.get('last_error_task_id', '')
    error_str = f' ({RED}마지막 오류: task {last_error}{NC})' if last_error else ''
    # running 상태는 보라색 bold로 강조 (task 정보 포함)
    PURPLE = '\033[1;35m'
    if status == 'running':
        print(f'  {PURPLE}{status}{task_str}{NC}{error_str}')
    else:
        print(f'  {status}{task_str}{error_str}')
except Exception as e:
    print(f'  (상태 파일 읽기 실패: {e})', file=sys.stderr)
    print('  unknown')
" 2>/dev/null | while read -r line; do
                echo -e "  ${CYAN}${name}${NC}:${line}"
            done
        else
            echo -e "  ${CYAN}${name}${NC}: 상태 파일 없음 (초기 상태)"
        fi
    done

    if [[ "$found_any" == "false" ]]; then
        log_warn "  등록된 프로젝트 없음"
    fi

    echo ""
}

# ═══════════════════════════════════════════════════════════
# 메인 분기
# ═══════════════════════════════════════════════════════════
COMMAND="${1:-help}"

case "$COMMAND" in
    start)
        shift
        cmd_start "$@"
        ;;
    stop)
        shift
        cmd_stop "$@"
        ;;
    status)
        shift
        cmd_status "$@"
        ;;
    help|--help|-h)
        show_help
        ;;
    *)
        log_error "알 수 없는 명령: ${COMMAND}"
        echo ""
        show_help
        exit 1
        ;;
esac
