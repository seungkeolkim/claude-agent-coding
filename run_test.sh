#!/usr/bin/env bash
# run_test.sh — Agent Hub 테스트 실행기
#
# 사용법:
#   ./run_test.sh all          # 전체 테스트 실행
#   ./run_test.sh unit         # Unit 테스트만
#   ./run_test.sh integration  # Integration 테스트만
#   ./run_test.sh e2e          # E2E 테스트만
#   ./run_test.sh help         # 도움말

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ─── 색상 출력 ───
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

# ─── 테스트 파일 분류 ───
UNIT_TESTS=(
    "tests/test_notification.py"
    "tests/test_safety_limits.py"
    "tests/test_task_utils.py"
    "tests/test_usage_checker.py"
    "tests/test_chatbot.py"
    "tests/test_telegram_client.py"
    "tests/test_telegram_formatter.py"
    "tests/test_telegram_router.py"
    "tests/test_session_reuse.py"
)

INTEGRATION_TESTS=(
    "tests/test_wfc_pipeline.py"
    "tests/test_hub_api.py"
    "tests/test_memory_updater_integration.py"
)

E2E_TESTS=(
    "tests/test_e2e_agent_shell.py"
    "tests/test_e2e_tm_lifecycle.py"
)

# ═══════════════════════════════════════════════════════════
# 함수
# ═══════════════════════════════════════════════════════════

show_help() {
    echo -e "${CYAN}${BOLD}Agent Hub — 테스트 실행기${NC}"
    echo ""
    echo "사용법:"
    echo "  ./run_test.sh [command] [pytest 옵션...]"
    echo ""
    echo "명령:"
    echo "  all             전체 테스트 실행 (기본값)"
    echo "  unit            Unit 테스트만 실행"
    echo "  integration     Integration 테스트만 실행"
    echo "  e2e             E2E 테스트만 실행"
    echo "  list            테스트 목록만 출력 (실행 안 함)"
    echo "  help            이 도움말 표시"
    echo ""
    echo "추가 pytest 옵션 예시:"
    echo "  ./run_test.sh unit -s              # stdout 캡처 비활성화"
    echo "  ./run_test.sh e2e -k 'lifecycle'   # 이름 필터링"
    echo "  ./run_test.sh all --tb=long        # 상세 traceback"
    echo ""
    echo "테스트 분류:"
    echo -e "  ${GREEN}Unit${NC}         notification, safety_limits, task_utils, usage_checker"
    echo -e "  ${YELLOW}Integration${NC}  wfc_pipeline (mock), hub_api (파일시스템)"
    echo -e "  ${RED}E2E${NC}          agent shell subprocess, TM full lifecycle"
}

list_tests() {
    echo -e "${BOLD}Unit 테스트:${NC}"
    for f in "${UNIT_TESTS[@]}"; do
        echo "  $f"
    done
    echo ""
    echo -e "${BOLD}Integration 테스트:${NC}"
    for f in "${INTEGRATION_TESTS[@]}"; do
        echo "  $f"
    done
    echo ""
    echo -e "${BOLD}E2E 테스트:${NC}"
    for f in "${E2E_TESTS[@]}"; do
        echo "  $f"
    done
}

run_tests() {
    local label="$1"
    shift
    local files=("$@")

    # 추가 pytest 옵션 분리 (-- 이후 또는 남은 인자)
    echo ""
    echo -e "${BOLD}═══ ${label} ═══${NC}"
    echo ""

    python3 -m pytest "${files[@]}" ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}
}

# ═══════════════════════════════════════════════════════════
# 메인
# ═══════════════════════════════════════════════════════════

if [[ $# -eq 0 ]]; then
    show_help
    exit 0
fi

COMMAND="$1"
shift

# 남은 인자는 pytest에 전달
EXTRA_ARGS=("$@")

case "$COMMAND" in
    help|--help|-h)
        show_help
        ;;
    list)
        list_tests
        ;;
    unit)
        run_tests "Unit 테스트" "${UNIT_TESTS[@]}"
        ;;
    integration)
        run_tests "Integration 테스트" "${INTEGRATION_TESTS[@]}"
        ;;
    e2e)
        run_tests "E2E 테스트" "${E2E_TESTS[@]}"
        ;;
    all)
        run_tests "전체 테스트" "${UNIT_TESTS[@]}" "${INTEGRATION_TESTS[@]}" "${E2E_TESTS[@]}"
        ;;
    *)
        echo -e "${RED}알 수 없는 명령: ${COMMAND}${NC}" >&2
        echo ""
        show_help
        exit 1
        ;;
esac
