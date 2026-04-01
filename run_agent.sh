#!/usr/bin/env bash
# run_agent.sh — Agent Hub 기동/종료/상태 확인 스크립트
#
# 사용법:
#   ./run_agent.sh start [executor|tester]   # 서비스 시작
#   ./run_agent.sh stop                      # 서비스 종료
#   ./run_agent.sh status                    # 서비스 상태 확인
#   ./run_agent.sh submit <title> <description>  # task 제출

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIDS_DIR="${SCRIPT_DIR}/.pids"
mkdir -p "$PIDS_DIR"

# 설정 파일 존재 확인
check_config_exists() {
    local missing=false
    if [[ ! -f "${SCRIPT_DIR}/config.yaml" ]]; then
        missing=true
    fi
    if [[ ! -f "${SCRIPT_DIR}/.env" ]]; then
        missing=true
    fi
    if [[ "$missing" == true ]]; then
        echo ""
        echo "[ERROR] 설정 파일이 없습니다. 먼저 초기 설정을 실행하세요:"
        echo ""
        echo "  ./create_config_and_env.sh"
        echo ""
        exit 1
    fi
}

# 색상 정의
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

start_executor() {
    """실행장비 프로세스를 시작한다."""
    log_info "실행장비(executor) 모드로 시작합니다..."

    # Task Manager 시작
    if [[ -f "${PIDS_DIR}/task_manager.pid" ]] && kill -0 "$(cat "${PIDS_DIR}/task_manager.pid")" 2>/dev/null; then
        log_warn "Task Manager가 이미 실행 중입니다."
    else
        python3 "${SCRIPT_DIR}/scripts/task_manager.py" &
        echo $! > "${PIDS_DIR}/task_manager.pid"
        log_info "Task Manager 시작됨 (PID: $!)"
    fi

    # Workflow Controller 시작
    if [[ -f "${PIDS_DIR}/workflow_controller.pid" ]] && kill -0 "$(cat "${PIDS_DIR}/workflow_controller.pid")" 2>/dev/null; then
        log_warn "Workflow Controller가 이미 실행 중입니다."
    else
        python3 "${SCRIPT_DIR}/scripts/workflow_controller.py" &
        echo $! > "${PIDS_DIR}/workflow_controller.pid"
        log_info "Workflow Controller 시작됨 (PID: $!)"
    fi
}

start_tester() {
    """테스트장비 프로세스를 시작한다."""
    log_info "테스트장비(tester) 모드로 시작합니다..."

    if [[ -f "${PIDS_DIR}/e2e_watcher.pid" ]] && kill -0 "$(cat "${PIDS_DIR}/e2e_watcher.pid")" 2>/dev/null; then
        log_warn "E2E Watcher가 이미 실행 중입니다."
    else
        bash "${SCRIPT_DIR}/scripts/e2e_watcher.sh" &
        echo $! > "${PIDS_DIR}/e2e_watcher.pid"
        log_info "E2E Watcher 시작됨 (PID: $!)"
    fi
}

stop_all() {
    """모든 프로세스를 종료한다."""
    log_info "모든 프로세스를 종료합니다..."

    for pid_file in "${PIDS_DIR}"/*.pid; do
        if [[ -f "$pid_file" ]]; then
            pid=$(cat "$pid_file")
            process_name=$(basename "$pid_file" .pid)
            if kill -0 "$pid" 2>/dev/null; then
                kill "$pid"
                log_info "${process_name} 종료됨 (PID: ${pid})"
            else
                log_warn "${process_name}는 이미 종료된 상태입니다."
            fi
            rm -f "$pid_file"
        fi
    done
}

show_status() {
    """실행 중인 프로세스 상태를 표시한다."""
    echo "=== Agent Hub 상태 ==="
    echo ""

    local any_running=false
    for pid_file in "${PIDS_DIR}"/*.pid; do
        if [[ -f "$pid_file" ]]; then
            pid=$(cat "$pid_file")
            process_name=$(basename "$pid_file" .pid)
            if kill -0 "$pid" 2>/dev/null; then
                echo -e "  ${GREEN}●${NC} ${process_name} (PID: ${pid})"
                any_running=true
            else
                echo -e "  ${RED}●${NC} ${process_name} (종료됨)"
                rm -f "$pid_file"
            fi
        fi
    done

    if [[ "$any_running" == "false" ]]; then
        echo "  실행 중인 프로세스가 없습니다."
    fi
    echo ""
}

# 메인 분기
case "${1:-help}" in
    start)
        check_config_exists
        role="${2:-executor}"
        case "$role" in
            executor) start_executor ;;
            tester)   start_tester ;;
            *)        log_error "알 수 없는 role: ${role} (executor|tester)"; exit 1 ;;
        esac
        ;;
    stop)
        stop_all
        ;;
    status)
        show_status
        ;;
    submit)
        check_config_exists
        # TODO: task 제출 CLI 구현
        log_info "TODO: task 제출 기능 구현 필요"
        ;;
    help|*)
        echo "사용법: $0 {start|stop|status|submit}"
        echo ""
        echo "  start [executor|tester]    서비스 시작 (기본: executor)"
        echo "  stop                       모든 서비스 종료"
        echo "  status                     서비스 상태 확인"
        echo "  submit <title> <desc>      새 task 제출"
        ;;
esac
