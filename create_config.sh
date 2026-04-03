#!/usr/bin/env bash
# create_config.sh — config.yaml을 템플릿에서 생성
#
# 사용법:
#   ./create_config.sh          # 대화형 (에디터 열림)
#   ./create_config.sh --quiet  # 복사만 (에디터 안 열림)
#
# 이미 존재하는 파일은 덮어쓰지 않는다.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
QUIET="${1:-}"

# 색상 정의
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }

# 에디터 결정 (환경변수 → vim → vi → nano 순)
get_editor() {
    if [[ -n "${EDITOR:-}" ]]; then
        echo "$EDITOR"
    elif command -v vim &>/dev/null; then
        echo "vim"
    elif command -v vi &>/dev/null; then
        echo "vi"
    elif command -v nano &>/dev/null; then
        echo "nano"
    else
        echo ""
    fi
}

echo ""
echo "=== Agent Hub 초기 설정 ==="
echo ""

# config.yaml 생성
TEMPLATE="${SCRIPT_DIR}/templates/config.yaml.template"
TARGET="${SCRIPT_DIR}/config.yaml"

if [[ ! -f "$TEMPLATE" ]]; then
    echo "[ERROR] 템플릿 파일 없음: ${TEMPLATE}" >&2
    exit 1
fi

if [[ -f "$TARGET" ]]; then
    log_warn "${TARGET} 이미 존재 — 건너뜀 (덮어쓰려면 삭제 후 재실행)"
else
    cp "$TEMPLATE" "$TARGET"
    log_info "config.yaml 생성됨 (from config.yaml.template)"

    # 대화형 모드: 에디터로 편집 유도
    if [[ "$QUIET" != "--quiet" ]]; then
        EDITOR_CMD=$(get_editor)

        if [[ -z "$EDITOR_CMD" ]]; then
            echo ""
            log_warn "에디터를 찾을 수 없습니다. 직접 편집하세요:"
            echo "  - ${TARGET}"
            echo ""
        else
            echo ""
            echo "config.yaml을 편집합니다. 확인할 항목:"
            echo "  - machines.executor.ssh_key       : SSH 키 경로"
            echo "  - machines.executor.github_token   : GitHub 토큰 (필요 시)"
            echo "  - machines.tester.*                : 테스트 장비 정보 (Phase 1.3)"
            echo "  - claude.*_model                   : agent별 모델 설정"
            echo ""
            read -rp "에디터로 열까요? (Y/n) " answer
            if [[ "${answer:-Y}" =~ ^[Yy]?$ ]]; then
                $EDITOR_CMD "$TARGET"
            fi
        fi
    fi
fi

echo ""
echo "=== 설정 완료 ==="
echo ""
echo "  config.yaml : $(realpath "${TARGET}" 2>/dev/null || echo '미생성')"
echo ""
echo "다음 단계: ./run_agent.sh init-project"
echo ""
