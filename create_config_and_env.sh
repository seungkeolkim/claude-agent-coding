#!/usr/bin/env bash
# create_config_and_env.sh — config.yaml과 .env를 템플릿에서 생성
#
# 사용법:
#   ./create_config_and_env.sh          # 대화형 (에디터 열림)
#   ./create_config_and_env.sh --quiet  # 복사만 (에디터 안 열림)
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

# 파일 생성 함수
create_from_template() {
    local template="$1"
    local target="$2"
    local description="$3"

    if [[ ! -f "$template" ]]; then
        echo "[ERROR] 템플릿 파일 없음: ${template}" >&2
        exit 1
    fi

    if [[ -f "$target" ]]; then
        log_warn "${target} 이미 존재 — 건너뜀 (덮어쓰려면 삭제 후 재실행)"
        return 1
    fi

    cp "$template" "$target"
    log_info "${target} 생성됨 (from ${template})"
    return 0
}

echo ""
echo "=== Agent Hub 초기 설정 ==="
echo ""

# config.yaml 생성
config_created=false
if create_from_template "${SCRIPT_DIR}/config.yaml.template" "${SCRIPT_DIR}/config.yaml" "시스템 설정"; then
    config_created=true
fi

# .env 생성
env_created=false
if create_from_template "${SCRIPT_DIR}/.env.template" "${SCRIPT_DIR}/.env" "환경 변수"; then
    env_created=true
fi

# 대화형 모드: 에디터로 편집 유도
if [[ "$QUIET" != "--quiet" ]]; then
    EDITOR_CMD=$(get_editor)

    if [[ -z "$EDITOR_CMD" ]]; then
        echo ""
        log_warn "에디터를 찾을 수 없습니다. 직접 편집하세요:"
        [[ "$config_created" == true ]] && echo "  - ${SCRIPT_DIR}/config.yaml"
        [[ "$env_created" == true ]]    && echo "  - ${SCRIPT_DIR}/.env"
        echo ""
    else
        # config.yaml 편집
        if [[ "$config_created" == true ]]; then
            echo ""
            echo "config.yaml을 편집합니다. 최소한 아래 항목을 확인하세요:"
            echo "  - project.name          : 대상 프로젝트 이름"
            echo "  - executor.codebase_path: 대상 프로젝트 절대경로"
            echo "  - executor.workspace_dir: runtime 데이터 절대경로"
            echo "  - git.enabled           : git 사용 여부"
            echo ""
            read -rp "에디터로 열까요? (Y/n) " answer
            if [[ "${answer:-Y}" =~ ^[Yy]?$ ]]; then
                $EDITOR_CMD "${SCRIPT_DIR}/config.yaml"
            fi
        fi

        # .env 편집
        if [[ "$env_created" == true ]]; then
            echo ""
            echo ".env를 편집합니다."
            echo ""
            read -rp "에디터로 열까요? (Y/n) " answer
            if [[ "${answer:-Y}" =~ ^[Yy]?$ ]]; then
                $EDITOR_CMD "${SCRIPT_DIR}/.env"
            fi
        fi
    fi
fi

echo ""
echo "=== 설정 완료 ==="
echo ""
echo "  config.yaml : $(realpath "${SCRIPT_DIR}/config.yaml" 2>/dev/null || echo '미생성')"
echo "  .env        : $(realpath "${SCRIPT_DIR}/.env" 2>/dev/null || echo '미생성')"
echo ""
echo "시작하려면: ./run_agent.sh start"
echo ""
