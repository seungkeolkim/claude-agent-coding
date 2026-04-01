#!/usr/bin/env bash
# e2e_watcher.sh — 테스트장비용 E2E 요청 감시 스크립트
#
# 테스트장비(Windows WSL 또는 PowerShell)에서 실행.
# 실행장비의 handoffs/ 디렉토리를 SSH로 감시하여
# E2E 테스트 요청(.ready)이 생기면 테스트를 수행한다.
#
# 사용법:
#   ./scripts/e2e_watcher.sh
#
# 필요 환경:
#   - 실행장비로의 SSH 접근 가능
#   - config.yaml의 tester 섹션 설정 완료

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# TODO: config.yaml에서 tester 설정 읽기
EXECUTOR_HOST="${EXECUTOR_HOST:-192.168.1.100}"
EXECUTOR_USER="${EXECUTOR_USER:-user}"
EXECUTOR_SSH_KEY="${EXECUTOR_SSH_KEY:-~/.ssh/id_rsa_server}"
REMOTE_WORKSPACE="${REMOTE_WORKSPACE:-/home/user/agent-hub/workspaces/my-web-app}"
LOCAL_WORK_DIR="${LOCAL_WORK_DIR:-./work}"
RECONNECT_INTERVAL="${RECONNECT_INTERVAL:-5}"

SSH_CMD="ssh -i ${EXECUTOR_SSH_KEY} ${EXECUTOR_USER}@${EXECUTOR_HOST}"

mkdir -p "$LOCAL_WORK_DIR"

echo "[E2E Watcher] 시작됨"
echo "[E2E Watcher] 실행장비: ${EXECUTOR_USER}@${EXECUTOR_HOST}"
echo "[E2E Watcher] 원격 workspace: ${REMOTE_WORKSPACE}"

while true; do
    echo "[E2E Watcher] 실행장비 handoffs/ 감시 중..."

    # 실행장비의 handoffs/ 디렉토리에서 -e2e.ready 파일 감시
    READY_FILE=$($SSH_CMD "inotifywait -e create ${REMOTE_WORKSPACE}/handoffs/ --include '-e2e\.ready$' -q" 2>/dev/null) || {
        echo "[E2E Watcher] SSH 연결 끊김. ${RECONNECT_INTERVAL}초 후 재연결..."
        sleep "$RECONNECT_INTERVAL"
        continue
    }

    # .ready 파일명에서 JSON 파일명 추출
    READY_FILENAME=$(echo "$READY_FILE" | awk '{print $NF}')
    JSON_FILENAME="${READY_FILENAME%.ready}.json"

    echo "[E2E Watcher] E2E 요청 감지: ${JSON_FILENAME}"

    # handoff JSON 다운로드
    scp -i "$EXECUTOR_SSH_KEY" \
        "${EXECUTOR_USER}@${EXECUTOR_HOST}:${REMOTE_WORKSPACE}/handoffs/${JSON_FILENAME}" \
        "${LOCAL_WORK_DIR}/current_handoff.json"

    # TODO: E2E Test Agent 실행
    # - handoff JSON에서 test_target_url, test_scenarios 읽기
    # - Playwright/Puppeteer로 브라우저 테스트 수행
    # - 결과 JSON + 스크린샷 생성
    echo "[E2E Watcher] TODO: E2E Test Agent 실행 구현 필요"

    # TODO: 결과를 실행장비로 업로드
    # scp ./e2e-result.json ${EXECUTOR_USER}@${EXECUTOR_HOST}:${REMOTE_WORKSPACE}/handoffs/
    # scp -r ./screenshots/ ${EXECUTOR_USER}@${EXECUTOR_HOST}:${REMOTE_WORKSPACE}/logs/
    # ssh ... "touch ${REMOTE_WORKSPACE}/handoffs/...-e2e-result.ready"
done
