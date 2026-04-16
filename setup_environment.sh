#!/usr/bin/env bash
# setup_environment.sh — Agent Hub 실행 환경 전체 초기화
#
# Python venv + pip 의존성 + 시스템 도구(git, gh, claude) 검증을 한 번에 수행한다.
# 최초 세팅이나 새 장비에서 시스템을 기동하기 전에 반드시 실행한다.
#
# 사용법:
#   ./setup_environment.sh          # 전체 초기화 (검증 + venv + config)
#   ./setup_environment.sh --check  # 검증만 (설치/생성 없이 상태 확인)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
CHECK_ONLY="${1:-}"

# ─── 색상 정의 ───
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

# ─── 출력 헬퍼 ───
log_ok()   { echo -e "  ${GREEN}✓${NC} $*"; }
log_fail() { echo -e "  ${RED}✗${NC} $*"; }
log_warn() { echo -e "  ${YELLOW}!${NC} $*"; }
log_info() { echo -e "  ${CYAN}→${NC} $*"; }

# 전체 검증 결과를 추적하는 카운터
TOTAL_CHECKS=0
PASSED_CHECKS=0
FAILED_CHECKS=0
WARNING_CHECKS=0

record_pass() { TOTAL_CHECKS=$((TOTAL_CHECKS + 1)); PASSED_CHECKS=$((PASSED_CHECKS + 1)); }
record_fail() { TOTAL_CHECKS=$((TOTAL_CHECKS + 1)); FAILED_CHECKS=$((FAILED_CHECKS + 1)); }
record_warn() { TOTAL_CHECKS=$((TOTAL_CHECKS + 1)); WARNING_CHECKS=$((WARNING_CHECKS + 1)); }


# ═══════════════════════════════════════════════════════════
# 1. 시스템 필수 도구 검증
# ═══════════════════════════════════════════════════════════

check_system_requirements() {
    echo ""
    echo -e "${BOLD}[1/5] 시스템 필수 도구 검증${NC}"
    echo ""

    # --- Python 3 ---
    if command -v python3 &>/dev/null; then
        local python_version
        python_version=$(python3 --version 2>&1)
        log_ok "python3: ${python_version}"
        record_pass
    else
        log_fail "python3: 설치되지 않음"
        log_info "설치: sudo apt install python3 python3-venv"
        record_fail
    fi

    # --- python3-venv (venv 모듈) ---
    if python3 -c "import venv" 2>/dev/null; then
        log_ok "python3-venv: 사용 가능"
        record_pass
    else
        log_fail "python3-venv: 모듈 없음"
        log_info "설치: sudo apt install python3-venv"
        record_fail
    fi

    # --- git ---
    if command -v git &>/dev/null; then
        local git_version
        git_version=$(git --version 2>&1)
        log_ok "git: ${git_version}"
        record_pass
    else
        log_fail "git: 설치되지 않음"
        log_info "설치: sudo apt install git"
        record_fail
    fi

    # --- gh (GitHub CLI) ---
    if command -v gh &>/dev/null; then
        local gh_version
        gh_version=$(gh --version 2>&1 | head -1)
        log_ok "gh: ${gh_version}"
        record_pass

        # gh 인증 상태 확인
        if gh auth status &>/dev/null; then
            log_ok "gh auth: 인증됨"
            record_pass
        else
            log_warn "gh auth: 인증되지 않음 (git 연동 기능 사용 시 필요)"
            log_info "인증: gh auth login"
            record_warn
        fi
    else
        log_fail "gh (GitHub CLI): 설치되지 않음"
        log_info "설치: https://cli.github.com/ 또는"
        log_info "  sudo apt install gh  (Ubuntu 22.04+)"
        log_info "  또는: sudo mkdir -p -m 755 /etc/apt/keyrings && wget -qO- https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null && sudo chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg && echo 'deb [arch=\$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main' | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null && sudo apt update && sudo apt install gh -y"
        record_fail
    fi

    # --- claude (Claude Code CLI) ---
    if command -v claude &>/dev/null; then
        local claude_version
        claude_version=$(claude --version 2>&1 || echo "버전 확인 불가")
        log_ok "claude: ${claude_version}"
        record_pass
    else
        log_warn "claude (Claude Code CLI): 설치되지 않음"
        log_info "Chatbot, agent 실행에 필요. --dummy 모드는 claude 없이 동작"
        log_info "설치: npm install -g @anthropic-ai/claude-code"
        record_warn
    fi
}


# ═══════════════════════════════════════════════════════════
# 2. Python 가상환경 + pip 의존성
# ═══════════════════════════════════════════════════════════

setup_python_environment() {
    echo ""
    echo -e "${BOLD}[2/5] Python 가상환경 + pip 의존성${NC}"
    echo ""

    if [[ "$CHECK_ONLY" == "--check" ]]; then
        # 검증만 수행
        if [ -d "$VENV_DIR" ]; then
            log_ok ".venv: 존재함 (${VENV_DIR})"
            record_pass

            # pip 패키지 검증
            if "$VENV_DIR/bin/python" -c "import yaml" 2>/dev/null; then
                log_ok "PyYAML: 설치됨"
                record_pass
            else
                log_fail "PyYAML: 설치되지 않음"
                record_fail
            fi

            if "$VENV_DIR/bin/python" -c "import pytest" 2>/dev/null; then
                log_ok "pytest: 설치됨"
                record_pass
            else
                log_warn "pytest: 설치되지 않음 (테스트 실행 시 필요)"
                record_warn
            fi
        else
            log_fail ".venv: 존재하지 않음"
            log_info "생성: ./setup_environment.sh (--check 없이 실행)"
            record_fail
        fi
        return
    fi

    # 실제 설치 수행
    if [ ! -d "$VENV_DIR" ]; then
        log_info ".venv 생성 중..."
        if python3 -m venv "$VENV_DIR"; then
            log_ok ".venv 생성 완료"
            record_pass
        else
            log_fail ".venv 생성 실패"
            record_fail
            return
        fi
    else
        log_ok ".venv: 이미 존재함"
        record_pass
    fi

    # pip 의존성 설치
    log_info "pip 의존성 설치 중..."
    if "$VENV_DIR/bin/pip" install -r "$SCRIPT_DIR/requirements.txt" -q 2>&1; then
        log_ok "requirements.txt 설치 완료"
        record_pass
    else
        log_fail "requirements.txt 설치 실패"
        record_fail
    fi

    # pytest (테스트 실행에 필요)
    if ! "$VENV_DIR/bin/python" -c "import pytest" 2>/dev/null; then
        log_info "pytest 설치 중..."
        if "$VENV_DIR/bin/pip" install pytest -q 2>&1; then
            log_ok "pytest 설치 완료"
            record_pass
        else
            log_warn "pytest 설치 실패 (테스트 실행 불가)"
            record_warn
        fi
    else
        log_ok "pytest: 이미 설치됨"
        record_pass
    fi
}


# ═══════════════════════════════════════════════════════════
# 3. 설정 파일 확인
# ═══════════════════════════════════════════════════════════

check_configuration_files() {
    echo ""
    echo -e "${BOLD}[3/5] 설정 파일 확인${NC}"
    echo ""

    # config.yaml
    if [ -f "$SCRIPT_DIR/config.yaml" ]; then
        log_ok "config.yaml: 존재함"
        record_pass
    else
        log_warn "config.yaml: 없음"
        log_info "생성: ./create_config.sh"
        record_warn
    fi

    # templates 확인
    if [ -f "$SCRIPT_DIR/templates/config.yaml.template" ]; then
        log_ok "templates/config.yaml.template: 존재함"
        record_pass
    else
        log_fail "templates/config.yaml.template: 없음 (필수 파일)"
        record_fail
    fi

    if [ -f "$SCRIPT_DIR/templates/project.yaml.template" ]; then
        log_ok "templates/project.yaml.template: 존재함"
        record_pass
    else
        log_fail "templates/project.yaml.template: 없음 (필수 파일)"
        record_fail
    fi

    # agent prompts
    local prompt_count
    prompt_count=$(find "$SCRIPT_DIR/config/agent_prompts" -name "*.md" 2>/dev/null | wc -l)
    if [ "$prompt_count" -ge 8 ]; then
        log_ok "agent_prompts: ${prompt_count}개 (8개 필요)"
        record_pass
    else
        log_fail "agent_prompts: ${prompt_count}개 (8개 필요)"
        record_fail
    fi
}


# ═══════════════════════════════════════════════════════════
# 4. 디렉토리 구조 확인 + 생성
# ═══════════════════════════════════════════════════════════

check_directory_structure() {
    echo ""
    echo -e "${BOLD}[4/5] 디렉토리 구조${NC}"
    echo ""

    # runtime 디렉토리 (gitignored)
    local runtime_directories=("projects" "session_history" "logs" ".pids")

    for directory_name in "${runtime_directories[@]}"; do
        local directory_path="$SCRIPT_DIR/$directory_name"
        if [ -d "$directory_path" ]; then
            log_ok "${directory_name}/: 존재함"
            record_pass
        else
            if [[ "$CHECK_ONLY" == "--check" ]]; then
                log_warn "${directory_name}/: 없음 (실행 시 자동 생성됨)"
                record_warn
            else
                mkdir -p "$directory_path"
                log_ok "${directory_name}/: 생성됨"
                record_pass
            fi
        fi
    done
}


# ═══════════════════════════════════════════════════════════
# 5. E2E 테스트 환경 (Docker + Playwright 이미지)
# ═══════════════════════════════════════════════════════════
# docs/e2e-test-design-decision.md §4.6-4, §4.6-7 결정에 따라
# setup 시점에 Docker 환경 검증 + Playwright 이미지 최초 빌드 수행.
# 이미 빌드되어 있으면 layer cache로 거의 no-op.

check_e2e_docker_environment() {
    echo ""
    echo -e "${BOLD}[5/5] E2E 테스트 환경 (Docker + Playwright 이미지)${NC}"
    echo ""

    local e2e_image="agent-hub-e2e-playwright"
    local dockerfile_dir="$SCRIPT_DIR/docker/e2e-playwright"

    # --- docker CLI ---
    if ! command -v docker &>/dev/null; then
        log_warn "docker: 설치되지 않음 (E2E 테스트 사용 시 필요)"
        log_info "설치: https://docs.docker.com/engine/install/"
        record_warn
        return
    fi
    local docker_version
    docker_version=$(docker --version 2>&1)
    log_ok "docker: ${docker_version}"
    record_pass

    # --- docker 데몬 접근 권한 ---
    if docker ps &>/dev/null; then
        log_ok "docker 데몬: 접근 가능"
        record_pass
    else
        log_warn "docker 데몬: 접근 불가 (권한 또는 데몬 미기동)"
        log_info "sudo 없이 실행: sudo usermod -aG docker \$USER  (로그아웃 후 재로그인)"
        log_info "데몬 시작: sudo systemctl start docker"
        record_warn
        return
    fi

    # --- Dockerfile 존재 ---
    if [ ! -f "$dockerfile_dir/Dockerfile" ]; then
        log_fail "Dockerfile 없음: $dockerfile_dir/Dockerfile"
        record_fail
        return
    fi
    log_ok "Dockerfile 존재함: docker/e2e-playwright/"
    record_pass

    # --- 이미지 존재 + 빌드 ---
    if docker image inspect "$e2e_image" &>/dev/null; then
        log_ok "Playwright 이미지 존재함: $e2e_image"
        record_pass
    else
        if [[ "$CHECK_ONLY" == "--check" ]]; then
            log_warn "Playwright 이미지 없음: $e2e_image (실행 시 auto_build)"
            log_info "수동 빌드: ./scripts/build_e2e_image.sh"
            record_warn
        else
            log_info "Playwright 이미지 빌드 중 (최초 1회 ~수분 소요)..."
            if E2E_IMAGE="$e2e_image" "$SCRIPT_DIR/scripts/build_e2e_image.sh" &>/dev/null; then
                log_ok "Playwright 이미지 빌드 완료: $e2e_image"
                record_pass
            else
                log_warn "Playwright 이미지 빌드 실패 (E2E 테스트 사용 시 필요)"
                log_info "수동 실행: ./scripts/build_e2e_image.sh"
                record_warn
            fi
        fi
    fi
}


# ═══════════════════════════════════════════════════════════
# 결과 요약
# ═══════════════════════════════════════════════════════════

print_summary() {
    echo ""
    echo -e "${BOLD}════════════════════════════════════════${NC}"

    if [ "$FAILED_CHECKS" -eq 0 ] && [ "$WARNING_CHECKS" -eq 0 ]; then
        echo -e "${GREEN}${BOLD}  환경 준비 완료 — 모든 검증 통과 (${PASSED_CHECKS}/${TOTAL_CHECKS})${NC}"
    elif [ "$FAILED_CHECKS" -eq 0 ]; then
        echo -e "${YELLOW}${BOLD}  환경 준비 완료 — 경고 ${WARNING_CHECKS}개 (${PASSED_CHECKS}/${TOTAL_CHECKS} 통과)${NC}"
    else
        echo -e "${RED}${BOLD}  환경 준비 미완료 — 실패 ${FAILED_CHECKS}개, 경고 ${WARNING_CHECKS}개${NC}"
    fi

    echo -e "${BOLD}════════════════════════════════════════${NC}"
    echo ""

    if [ "$FAILED_CHECKS" -gt 0 ]; then
        echo -e "  ${RED}✗${NC} 실패 항목을 해결한 뒤 다시 실행하세요."
        echo ""
    fi

    # 다음 단계 안내
    if [ "$FAILED_CHECKS" -eq 0 ]; then
        echo "  다음 단계:"
        if [ ! -f "$SCRIPT_DIR/config.yaml" ]; then
            echo "    1. ./create_config.sh        # 시스템 설정 생성"
            echo "    2. ./run_agent.sh init-project  # 프로젝트 초기화"
        else
            echo "    ./run_system.sh start        # Task Manager 시작"
            echo "    ./run_agent.sh chat          # Chatbot 대화형 인터페이스"
        fi
        echo ""
    fi
}


# ═══════════════════════════════════════════════════════════
# 메인
# ═══════════════════════════════════════════════════════════

echo ""
echo -e "${BOLD}═══ Agent Hub 환경 초기화 ═══${NC}"

if [[ "$CHECK_ONLY" == "--check" ]]; then
    echo -e "  (검증 모드 — 설치/생성 없이 상태만 확인)"
fi

check_system_requirements
setup_python_environment
check_configuration_files
check_directory_structure
check_e2e_docker_environment
print_summary
