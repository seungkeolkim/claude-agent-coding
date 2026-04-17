#!/usr/bin/env bash
# e2e_container_runner.sh — E2E 컨테이너 생명주기 관리
#
# subtask 단위로 Playwright + MCP 통합 컨테이너를 기동/실행/정리한다.
# run_claude_agent.sh가 e2e_tester 호출 전에 start,
# Phase 3 검증에서 exec-test, 종료 시점에 stop 을 호출한다.
#
# 서브커맨드:
#   start   <container_name> <tests_dir> <artifacts_dir>
#           이미지 확인(없으면 auto_build면 빌드) → docker run -d -p 0:8931 →
#           MCP SSE 헬스체크 → 호스트 포트를 stdout에 출력 (그 외 로그는 stderr)
#
#   exec-test <container_name> [--browser chromium] [--base-url http://...] \
#             [--retries 0] [--viewport-w 1280] [--viewport-h 720] \
#             [--screenshots only-on-failure] [--video off] [--trace retain-on-failure]
#           docker exec로 Playwright test 실행. 결과는 playwright.config.ts의
#           reporter 설정에 따라 /e2e/test-results/report.json에 기록됨
#           (volume mount 된 경로). 콘솔에는 list reporter 출력이 나타난다.
#
#   stop    <container_name> [--mcp-config <path>]
#           컨테이너 정지/제거 + 임시 .mcp.json 파일 삭제. trap에서 호출되어
#           성공/실패/인터럽트 공통 정리 보장.
#
# 환경변수로 호스트 config.yaml 값을 주입한다 (run_claude_agent.sh가 설정):
#   E2E_IMAGE          Docker 이미지 이름 (기본 agent-hub-e2e-playwright)
#   E2E_AUTO_BUILD     true|false (§4.6-7 안전망. 기본 true)
#   E2E_NETWORK        Docker network 옵션 (기본 host)
#   E2E_HEALTHCHECK_TIMEOUT   MCP SSE 헬스체크 대기 초수 (기본 30)
#   E2E_MCP_ISOLATED   true|false (기본 true)
#
# 사용 예:
#   HOST_PORT=$(scripts/e2e_container_runner.sh start \
#       e2e-myproj-00001-01 \
#       /abs/path/codebase/e2e-tests/00001-1 \
#       /abs/path/logs/00001/e2e-artifacts/00001-1)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_HUB_ROOT="$(dirname "$SCRIPT_DIR")"

# 기본값
E2E_IMAGE="${E2E_IMAGE:-agent-hub-e2e-playwright}"
E2E_AUTO_BUILD="${E2E_AUTO_BUILD:-true}"
E2E_NETWORK="${E2E_NETWORK:-host}"
E2E_HEALTHCHECK_TIMEOUT="${E2E_HEALTHCHECK_TIMEOUT:-30}"
E2E_MCP_ISOLATED="${E2E_MCP_ISOLATED:-true}"
E2E_MCP_INTERNAL_PORT="${E2E_MCP_INTERNAL_PORT:-8931}"

# 모든 진단 로그는 stderr로 (stdout은 호스트 포트 등 순수 출력 전용)
log() { echo "[e2e_container_runner] $*" >&2; }

# ─── 이미지 존재 확인 + 필요 시 빌드 ───
ensure_image() {
    if docker image inspect "$E2E_IMAGE" >/dev/null 2>&1; then
        log "이미지 확인됨: $E2E_IMAGE"
        return 0
    fi

    if [[ "$E2E_AUTO_BUILD" != "true" ]]; then
        log "이미지 없음 ($E2E_IMAGE) 그리고 auto_build=false → 즉시 실패"
        log "빌드 방법: docker build -t $E2E_IMAGE $AGENT_HUB_ROOT/docker/e2e-playwright/"
        return 1
    fi

    local builder="$SCRIPT_DIR/build_e2e_image.sh"
    if [[ ! -x "$builder" ]]; then
        log "빌드 스크립트를 찾을 수 없음: $builder"
        return 1
    fi

    log "이미지 없음 → auto_build 실행: $builder"
    if E2E_IMAGE="$E2E_IMAGE" "$builder" >&2; then
        log "이미지 빌드 완료"
        return 0
    else
        log "이미지 빌드 실패"
        return 1
    fi
}

# ─── MCP SSE endpoint 헬스체크 ───
# 컨테이너가 기동되어 MCP 서버가 SSE 요청을 받을 수 있을 때까지 대기.
wait_for_mcp_ready() {
    local host_port="$1"
    local deadline=$(( $(date +%s) + E2E_HEALTHCHECK_TIMEOUT ))

    while (( $(date +%s) < deadline )); do
        # SSE 엔드포인트는 헤더에 200을 준 뒤 스트림을 계속 열어두므로
        # --max-time으로 끊으면 curl이 exit 28로 빠진다. 이때 stdout에는
        # `%{http_code}`가 이미 "200"으로 찍혀 있으므로, `|| fallback`으로
        # 덮어쓰지 말고 exit code는 무시한 채 stdout만 신뢰한다.
        local status
        status=$(curl -s -o /dev/null -w "%{http_code}" --max-time 2 \
                 "http://localhost:${host_port}/sse" 2>/dev/null) || true
        status="${status:-000}"
        if [[ "$status" =~ ^(200|204|405|406)$ ]]; then
            log "MCP SSE ready (port=$host_port, http=$status)"
            return 0
        fi
        sleep 0.5
    done

    log "MCP SSE 헬스체크 실패 (port=$host_port, timeout=${E2E_HEALTHCHECK_TIMEOUT}s)"
    return 1
}

# ─── start <container_name> <tests_dir> <artifacts_dir> ───
cmd_start() {
    local container_name="$1"
    local tests_dir="$2"
    local artifacts_dir="$3"

    if [[ -z "$container_name" || -z "$tests_dir" || -z "$artifacts_dir" ]]; then
        log "start 사용법: start <container_name> <tests_dir> <artifacts_dir>"
        return 1
    fi

    # 기존 동명 컨테이너가 남아있으면 먼저 정리
    if docker ps -a --format '{{.Names}}' | grep -qx "$container_name"; then
        log "기존 동명 컨테이너 발견 — 제거: $container_name"
        docker rm -f "$container_name" >/dev/null 2>&1 || true
    fi

    ensure_image

    mkdir -p "$tests_dir" "$artifacts_dir"

    # MCP 옵션 조립
    local mcp_flags=("--headless" "--port" "$E2E_MCP_INTERNAL_PORT" "--host" "0.0.0.0")
    if [[ "$E2E_MCP_ISOLATED" == "true" ]]; then
        mcp_flags=("--isolated" "${mcp_flags[@]}")
    fi

    # 네트워크 옵션
    local network_args=()
    if [[ "$E2E_NETWORK" == "host" ]]; then
        network_args=(--network host)
    elif [[ -n "$E2E_NETWORK" ]]; then
        network_args=(--network "$E2E_NETWORK")
    fi

    # `--network=host` 사용 시에도 -p 0:8931 플래그로 포트 정보가 docker port에 기록되도록 한다.
    # 단, host 네트워크에서 -p는 docker가 경고만 내고 무시한다. 이 경우 내부 포트를 그대로 쓴다.
    local port_arg=("-p" "0:${E2E_MCP_INTERNAL_PORT}")

    log "컨테이너 기동: $container_name (image=$E2E_IMAGE, network=$E2E_NETWORK)"

    local run_output
    if ! run_output=$(docker run -d \
        "${network_args[@]}" \
        "${port_arg[@]}" \
        -v "${tests_dir}:/e2e/tests" \
        -v "${artifacts_dir}:/e2e/test-results" \
        --name "$container_name" \
        "$E2E_IMAGE" \
        npx @playwright/mcp@latest "${mcp_flags[@]}" 2>&1); then
        log "docker run 실패: $run_output"
        return 1
    fi

    # 호스트 포트 조회.
    # host 네트워크 모드에서는 docker port가 비어있을 수 있으므로 내부 포트를 그대로 사용.
    local host_port=""
    if [[ "$E2E_NETWORK" == "host" ]]; then
        host_port="$E2E_MCP_INTERNAL_PORT"
        log "host network — 내부 포트 그대로 사용: $host_port"
    else
        # 최대 5초 동안 포트 정보가 잡힐 때까지 재시도
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            host_port=$(docker port "$container_name" "${E2E_MCP_INTERNAL_PORT}/tcp" 2>/dev/null \
                        | awk -F: '{print $NF}' | head -1 || true)
            if [[ -n "$host_port" ]]; then break; fi
            sleep 0.5
        done
        if [[ -z "$host_port" ]]; then
            log "호스트 포트 조회 실패"
            docker logs "$container_name" >&2 || true
            docker rm -f "$container_name" >/dev/null 2>&1 || true
            return 1
        fi
    fi

    if ! wait_for_mcp_ready "$host_port"; then
        log "MCP ready 실패 — 컨테이너 로그:"
        docker logs "$container_name" >&2 || true
        docker rm -f "$container_name" >/dev/null 2>&1 || true
        return 1
    fi

    # 호스트 포트만 stdout으로 (호출자가 capture)
    echo "$host_port"
}

# ─── exec-test <container_name> [options] ───
cmd_exec_test() {
    local container_name="$1"
    shift

    if [[ -z "$container_name" ]]; then
        log "exec-test 사용법: exec-test <container_name> [옵션]"
        return 1
    fi

    local browser="chromium"
    local base_url=""
    local retries="0"
    local viewport_w="1280"
    local viewport_h="720"
    local screenshots="only-on-failure"
    local video="off"
    local trace="retain-on-failure"

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --browser)     browser="$2";     shift 2 ;;
            --base-url)    base_url="$2";    shift 2 ;;
            --retries)     retries="$2";     shift 2 ;;
            --viewport-w)  viewport_w="$2";  shift 2 ;;
            --viewport-h)  viewport_h="$2";  shift 2 ;;
            --screenshots) screenshots="$2"; shift 2 ;;
            --video)       video="$2";       shift 2 ;;
            --trace)       trace="$2";       shift 2 ;;
            *) log "알 수 없는 옵션: $1"; return 1 ;;
        esac
    done

    # 환경변수로 Playwright config에 전달
    local env_args=(
        -e "BROWSER=${browser}"
        -e "RETRIES=${retries}"
        -e "VIEWPORT_W=${viewport_w}"
        -e "VIEWPORT_H=${viewport_h}"
        -e "SCREENSHOTS=${screenshots}"
        -e "VIDEO=${video}"
        -e "TRACE=${trace}"
    )
    if [[ -n "$base_url" ]]; then
        env_args+=(-e "BASE_URL=${base_url}")
    fi

    log "Playwright test 실행: container=$container_name browser=$browser base_url=${base_url:-unset} retries=$retries"

    # docker exec는 -e로 환경변수 주입
    # reporter는 playwright.config.ts에서 [['json', {outputFile}], ['list']]로 지정.
    # 여기서 --reporter=json 을 또 주면 config의 outputFile 설정이 override되어
    # stdout 출력만 남고 report.json 파일이 생성되지 않으니 주지 말 것.
    # exit code는 test 실패 시에도 유지되어야 하므로 그대로 통과
    docker exec "${env_args[@]}" "$container_name" \
        npx playwright test --config=/e2e/playwright.config.ts
}

# ─── stop <container_name> [--mcp-config <path>] ───
cmd_stop() {
    local container_name="$1"
    shift || true

    local mcp_config=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --mcp-config) mcp_config="$2"; shift 2 ;;
            *) shift ;;
        esac
    done

    if [[ -n "$container_name" ]]; then
        if docker ps -a --format '{{.Names}}' | grep -qx "$container_name"; then
            log "컨테이너 정지/제거: $container_name"
            docker stop "$container_name" >/dev/null 2>&1 || true
            docker rm "$container_name" >/dev/null 2>&1 || true
        else
            log "컨테이너 없음 — skip: $container_name"
        fi
    fi

    if [[ -n "$mcp_config" && -f "$mcp_config" ]]; then
        log "임시 mcp-config 삭제: $mcp_config"
        rm -f "$mcp_config"
    fi
}

# ─── 메인 디스패치 ───
SUBCOMMAND="${1:-}"
shift || true

case "$SUBCOMMAND" in
    start)     cmd_start "$@" ;;
    exec-test) cmd_exec_test "$@" ;;
    stop)      cmd_stop "$@" ;;
    "")
        echo "사용법: $0 {start|exec-test|stop} [args...]" >&2
        exit 1
        ;;
    *)
        echo "알 수 없는 서브커맨드: $SUBCOMMAND" >&2
        exit 1
        ;;
esac
