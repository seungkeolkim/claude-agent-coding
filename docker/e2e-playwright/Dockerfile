# ═══════════════════════════════════════════════════════════
# Agent Hub — E2E Playwright + MCP 통합 이미지
#
# 역할:
#   - Playwright 공식 베이스 이미지(Chromium/Firefox/WebKit 내장)
#   - @playwright/mcp: Claude agent가 탐색/디버깅용으로 쓰는 MCP 서버
#   - @playwright/test: 최종 검증(Phase 3)용 test runner CLI
#
# 기본 진입점은 MCP 서버(HTTP/SSE) 상주.
# 검증은 e2e_container_runner.sh가 `docker exec`로 별도 호출한다.
#
# 빌드:
#   docker build -t agent-hub-e2e-playwright docker/e2e-playwright/
# ═══════════════════════════════════════════════════════════

FROM mcr.microsoft.com/playwright:v1.52.0-noble

WORKDIR /e2e

# package.json을 먼저 복사하여 layer cache 재사용
COPY package.json package-lock.json* ./
RUN npm ci --no-audit --no-fund 2>/dev/null \
    || npm install --no-audit --no-fund

# playwright.config.ts는 test 실행 시 환경변수로 동작을 조절한다
COPY playwright.config.ts ./

# 테스트 디렉토리는 실행 시 volume mount (기본 위치만 생성)
RUN mkdir -p /e2e/tests /e2e/test-results

# MCP SSE 포트 (고정). 호스트 포트는 docker run 시 동적 할당.
EXPOSE 8931

# 기본 진입점: Playwright MCP 서버
#   --isolated: 매 세션 새 프로필 (상태 격리)
#   --headless: GUI 없는 서버 대응
#   --port/--host: Docker 외부에서 접근 가능한 HTTP/SSE transport
CMD ["npx", "@playwright/mcp@latest", \
     "--isolated", \
     "--headless", \
     "--port", "8931", \
     "--host", "0.0.0.0"]
