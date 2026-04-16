#!/usr/bin/env bash
# build_e2e_image.sh — E2E Playwright + MCP 통합 컨테이너 이미지 빌드
#
# `e2e_container_runner.sh`의 ensure_image()도 auto_build=true면 동일한 빌드를
# 자동 실행하지만, 아래와 같이 "미리 빌드해 두고 싶을 때" 쓰는 수동 진입점이다.
#
# 사용:
#   ./scripts/build_e2e_image.sh                    # 기본 이미지명으로 빌드
#   ./scripts/build_e2e_image.sh --no-cache         # 캐시 무시
#   E2E_IMAGE=my-e2e ./scripts/build_e2e_image.sh   # 이미지명 override
#
# 환경변수:
#   E2E_IMAGE   빌드할 이미지 태그 (기본 agent-hub-e2e-playwright)
#
# 나머지 인자는 모두 `docker build`에 그대로 전달된다 (예: --no-cache, --pull).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_HUB_ROOT="$(dirname "$SCRIPT_DIR")"

E2E_IMAGE="${E2E_IMAGE:-agent-hub-e2e-playwright}"
BUILD_CONTEXT="$AGENT_HUB_ROOT/docker/e2e-playwright"

if [[ ! -f "$BUILD_CONTEXT/Dockerfile" ]]; then
    echo "[build_e2e_image] Dockerfile을 찾을 수 없습니다: $BUILD_CONTEXT/Dockerfile" >&2
    exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
    echo "[build_e2e_image] docker CLI가 설치되어 있지 않습니다." >&2
    exit 1
fi

if ! docker info >/dev/null 2>&1; then
    echo "[build_e2e_image] docker daemon에 접근할 수 없습니다 (sudo 권한 / docker 그룹 확인 필요)." >&2
    exit 1
fi

echo "[build_e2e_image] 이미지 태그   : $E2E_IMAGE"
echo "[build_e2e_image] 빌드 컨텍스트 : $BUILD_CONTEXT"
echo "[build_e2e_image] 추가 인자     : $*"

docker build -t "$E2E_IMAGE" "$@" "$BUILD_CONTEXT"

echo "[build_e2e_image] 완료. 현재 이미지 목록:"
docker images "$E2E_IMAGE"
