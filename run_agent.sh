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
VALID_AGENTS="planner coder reviewer setup unit_tester e2e_tester reporter summarizer"

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
    echo "  pipeline --project <name> --task <id> [--dummy] [--dry-run]"
    echo "                       전체 파이프라인 자동 실행 (Planner → Subtask Loop)"
    echo "  init-project         대화형 프로젝트 초기화"
    echo "  kill-all [--force]   모든 agent 프로세스 종료 (claude -p 포함)"
    echo "  help                 이 도움말 표시"
    echo ""
    echo "Task 관리:"
    echo "  submit --project <name> --title \"제목\" [--description \"설명\"] [--attach 파일]"
    echo "  list [--project <name>] [--status <status>]"
    echo "  pending [--project <name>]"
    echo "  approve <task_id> --project <name> [--message \"코멘트\"]"
    echo "  reject <task_id> --project <name> --message \"사유\""
    echo "  feedback <task_id> --project <name> --message \"피드백\""
    echo "  config --project <name> --set \"key=value\""
    echo "  pause --project <name> [<task_id>]"
    echo "  resume --project <name> [<task_id>]"
    echo "  cancel <task_id> --project <name>"
    echo "  notifications [--project <name>] [--limit N] [--unread]"
    echo ""
    echo "대화형:"
    echo "  chat [--confirmation-mode always_confirm|never_confirm|smart]"
    echo "                       자연어 Chatbot 시작"
    echo ""
    echo "웹 콘솔:"
    echo "  web                  웹 모니터링 콘솔 시작 (기본 포트: 9880)"
    echo ""
    echo "agent_type:"
    echo "  planner, coder, reviewer, setup, unit_tester, e2e_tester, reporter"
    echo ""
    echo "예시:"
    echo "  ./run_agent.sh submit --project my-app --title \"로그인 기능 구현\""
    echo "  ./run_agent.sh list --project my-app --status in_progress"
    echo "  ./run_agent.sh approve 00042 --project my-app"
    echo "  ./run_agent.sh run coder --project my-app --task 00001"
    echo "  ./run_agent.sh pipeline --project my-app --task 00001 --dummy"
    echo ""
    echo "시스템 관리 (./run_system.sh):"
    echo "  start, stop, status → ./run_system.sh 참고"
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
    # 파일명 규칙: {info}.{PID}.pid — PID는 파일명 마지막에서 추출
    if [[ -d "$pid_dir" ]] && ls "$pid_dir"/*.pid &>/dev/null; then
        for pid_file in "$pid_dir"/*.pid; do
            local basename
            basename=$(basename "$pid_file")
            # 파일명에서 PID 추출: xxx.12345.pid → 12345
            local pid
            pid=$(echo "$basename" | sed 's/.*\.\([0-9]*\)\.pid$/\1/')
            # PID 부분이 없는 파일명이면 (구 형식) JSON에서 읽기 시도
            if [[ -z "$pid" ]] || ! [[ "$pid" =~ ^[0-9]+$ ]]; then
                pid=$(python3 -c "import json; print(json.load(open('${pid_file}'))['pid'])" 2>/dev/null || echo "")
            fi
            # 파일명에서 정보 표시 (PID 부분 제거)
            local display_name
            display_name=$(echo "$basename" | sed 's/\.[0-9]*\.pid$//')

            if [[ -z "$pid" ]]; then
                rm -f "$pid_file"
                continue
            fi

            # 프로세스가 실제로 살아있는지 확인
            if kill -0 "$pid" 2>/dev/null; then
                # 프로세스 그룹 전체를 종료 (자식 프로세스 포함)
                kill -- -"$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true
                log_info "종료: ${display_name} (PID ${pid})"
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
    local force_result=""

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
            --force-result)
                force_result="$2"
                shift 2
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

    # subtask JSON 확인 (지정된 경우)
    # subtask_id 형식: {task_id}-{num} (예: 00001-1)
    # 파일 위치: tasks/{task_id}/subtask-{num zero-padded}.json
    local subtask_file=""
    if [[ -n "$subtask_id" ]]; then
        local subtask_num
        subtask_num=$(echo "$subtask_id" | sed 's/.*-//')
        local subtask_num_padded
        subtask_num_padded=$(printf "%02d" "$subtask_num")
        subtask_file="${project_dir}/tasks/${task_id}/subtask-${subtask_num_padded}.json"
        if [[ ! -f "$subtask_file" ]]; then
            log_error "subtask 파일이 없습니다: ${subtask_file}"
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
    if [[ -n "$force_result" ]]; then
        log_warn "  FORCE-RESULT: ${force_result}"
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

    if [[ -n "$force_result" ]]; then
        run_args+=(--force-result "$force_result")
    fi

    exec "${run_args[@]}"
}

# ═══════════════════════════════════════════════════════════
# pipeline 명령
# ═══════════════════════════════════════════════════════════
cmd_pipeline() {
    local project_name=""
    local task_id=""
    local dummy=false
    local dry_run=false

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
            --dummy)
                dummy=true
                shift
                ;;
            --dry-run)
                dry_run=true
                shift
                ;;
            *)
                log_error "알 수 없는 옵션: $1"
                exit 1
                ;;
        esac
    done

    if [[ -z "$project_name" ]]; then
        log_error "--project 옵션이 필요합니다."
        exit 1
    fi
    if [[ -z "$task_id" ]]; then
        log_error "--task 옵션이 필요합니다."
        exit 1
    fi

    # venv 활성화 (PyYAML 필요)
    if [[ -f "${SCRIPT_DIR}/activate_venv.sh" ]]; then
        source "${SCRIPT_DIR}/activate_venv.sh"
    fi

    local pipeline_args=(
        python3 "${SCRIPT_DIR}/scripts/workflow_controller.py"
        --project "$project_name"
        --task "$task_id"
    )

    if [[ "$dummy" == "true" ]]; then
        pipeline_args+=(--dummy)
    fi

    if [[ "$dry_run" == "true" ]]; then
        pipeline_args+=(--dry-run)
    fi

    exec "${pipeline_args[@]}"
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
    pipeline)
        shift
        cmd_pipeline "$@"
        ;;
    kill-all)
        shift
        cmd_kill_all "$@"
        ;;
    start|stop|status)
        log_warn "'${COMMAND}' 명령은 ./run_system.sh 로 이동되었습니다."
        log_warn "사용법: ./run_system.sh ${COMMAND}"
        exit 1
        ;;
    chat)
        shift
        PYTHONPATH="${SCRIPT_DIR}/scripts" python3 "${SCRIPT_DIR}/scripts/chatbot.py" "$@"
        ;;
    web)
        shift
        if [[ -f "${SCRIPT_DIR}/activate_venv.sh" ]]; then
            source "${SCRIPT_DIR}/activate_venv.sh"
        fi
        AGENT_HUB_ROOT="${SCRIPT_DIR}" PYTHONPATH="${SCRIPT_DIR}:${SCRIPT_DIR}/scripts" python3 -m scripts.web.server "$@"
        ;;
    submit|list|pending|approve|reject|feedback|config|pause|resume|cancel|notifications)
        shift
        PYTHONPATH="${SCRIPT_DIR}/scripts" python3 "${SCRIPT_DIR}/scripts/cli.py" "${COMMAND}" "$@"
        ;;
    *)
        log_error "알 수 없는 명령: ${COMMAND}"
        echo ""
        show_help
        exit 1
        ;;
esac
