# E2E 테스트 아키텍처 의사결정 문서

> 작성: 2026-04-16
> 상태: **의사결정 완료, 구현 미착수**
> 브랜치: `feature/playwright-e2e-test`
> 관련: `docs/agent-system-spec-v07.md` §4 E2E Test Agent, §6.3, §15.3

---

## 1. 배경 및 문제 정의

### 1.1 기존 설계의 한계

현재 E2E 테스트는 다음 구조로 설계되어 있으나 미구현(TODO) 상태:

- 원격 Windows 테스트장비(장비1, 장비3)에서 실행
- 실행장비 ↔ 테스트장비 간 SSH sentinel 파일 기반 통신
- `scripts/e2e_watcher.sh`가 SSH로 handoff 파일 감시

**문제점:**
1. SSH 연결 관리, 장비 가용성 의존(업무 시간/수시 가동)
2. 크로스 머신 handoff 복잡성
3. Windows WSL 의존 → Linux 표준 환경 벗어남
4. 멀티프로젝트 동시 실행 어려움(장비당 1 세션)
5. 실행장비 → 테스트장비 SSH 불가 (역방향 push 불가)

### 1.2 해결 전제

**핵심 관찰:** 최신 브라우저 자동화 도구의 headless 모드는 GUI 없는 Linux 서버에서 완전히 동작한다. X server/Wayland/GPU 모두 불필요하며, 렌더링은 메모리 버퍼에서 수행된다.

이 관찰로부터, 원격 테스트장비가 아닌 **로컬 실행장비에서 Docker 격리로 E2E를 완결**할 수 있다는 결론 도출.

---

## 2. 요구사항

| # | 요구사항 | 근거 |
|---|---------|------|
| R1 | GUI 없는 CLI 전용 Linux 서버에서 동작 | 실행장비(장비2)가 Ubuntu 서버 |
| R2 | 멀티프로젝트 동시 실행 시 격리 | 여러 프로젝트가 병렬로 진행될 수 있음 |
| R3 | 동적(Claude 생성) + 정적(기존 테스트) 둘 다 지원 | 프로젝트별 요구사항 다양 |
| R4 | 결과물(스크린샷/비디오/trace) 영속 저장 | Coder 루프백 시 실패 원인 분석 |
| R5 | 빌드형 데스크탑 앱 테스트 확장 slot 확보 | 향후 웹 외 애플리케이션 대응 |
| R6 | WFC 기존 agent 파이프라인 패턴과 일관 | `run_agent()` 호출 → JSON 반환 |
| R7 | JSON 결과로 pass/fail 판정 및 Reporter 연동 | 기존 Reporter agent 연계 |
| R8 | 동적 테스트 작성 시 실제 UI 탐색 가능 | selector 정확도, 실패 디버깅 품질 향상 |
| R9 | 호스트 환경 의존 최소화 | 여러 실행장비에서 재현성 확보, Docker만 있으면 동작 |

---

## 3. 대안 검토

### 3.1 종합 비교표

| 항목 | **Playwright** | **Cypress** | **Selenium** | **Puppeteer** | **TestCafe** |
|------|:-:|:-:|:-:|:-:|:-:|
| 개발 주체 | Microsoft | Cypress.io | Selenium HQ | Google | DevExpress |
| 아키텍처 | WebSocket 직접 통신 | 브라우저 내부 주입 | WebDriver 중계 | DevTools Protocol | Node.js 프록시 |
| Chromium | O | O | O | O | O |
| Firefox | O | O | O | O (제한적) | O |
| WebKit/Safari | O | 실험적 | O | X | X |
| 언어 지원 | JS/TS/Python/Java/C# | JS/TS | 거의 모든 언어 | JS/TS | JS/TS |
| headless Docker | **공식 이미지** | 공식 이미지 | Grid 별도 구성 | 공식 없음 | 공식 이미지 |
| Docker 이미지 크기 | ~1.9GB (최적화 가능) | ~2.0GB | ~1.5GB (Grid node) | 직접 빌드 | ~1.5GB |
| 실행 속도 | **가장 빠름** | 빠름 | 느림 (WebDriver 오버헤드) | 빠름 (Chrome만) | 보통 |
| auto-wait | 내장 | 내장 | 없음 (수동 wait) | 없음 | 내장 |
| trace/디버깅 | **trace viewer (타임라인)** | time-travel 디버깅 | 스크린샷만 | 스크린샷만 | 스크린샷 |
| 병렬 실행 | 내장 (자동) | 유료 (Dashboard) | Grid 수동 | 직접 구현 | 내장 |
| npm 주간 다운로드 (2026) | **~33M (1위)** | ~6.5M | ~4M | ~25M | ~0.3M |
| retention rate | **94% (최고)** | 하락 | 안정 | - | 낮음 |

### 3.2 프로젝트 요구사항별 적합성

| 요구사항 | Playwright | Cypress | Selenium | Puppeteer | TestCafe |
|----------|:-:|:-:|:-:|:-:|:-:|
| R1: GUI 없는 Linux | **최적** | 가능 | 가능 | 가능 | 가능 |
| R2: Docker 격리 | **최적** | 가능 | Grid 필요 | 직접 구성 | 가능 |
| R3: Claude 동적 생성 | **최적** | 가능 | 복잡 | 제한적 | 제한적 |
| R4: 결과물 수집 | **전부 내장** | 전부 내장 | 스크린샷만 | 스크린샷만 | 스크린샷+비디오 |
| R6: WFC 연동 | 자연스러움 | 자연스러움 | 중계 서버 추가 | 자연스러움 | 자연스러움 |
| R7: JSON reporter | **내장** | 내장 | 플러그인 | 직접 구현 | 내장 |

### 3.3 도구별 장단점

**Playwright**
- 장점: 속도 최고, 멀티브라우저 완전 지원, Python/JS/TS 모두 지원, 공식 Docker, trace viewer, auto-wait, 2026년 npm 1위
- 단점: 이미지 크기 ~1.9GB (최적화 시 감소), 2020년 출시로 레거시 자료 상대적으로 적음
- 특이: Puppeteer 원 개발자가 MS로 이직 후 만든 상위 버전

**Cypress**
- 장점: DX(개발 경험) 최고, time-travel 디버깅, 브라우저 내부 실행
- 단점: **WebKit 실험적**, JS/TS만, 병렬 실행 유료, `cy.origin()` 없이 멀티 도메인 불가
- 특이: headless Docker에서는 DX 강점(in-browser 디버깅)이 반감됨

**Selenium**
- 장점: 가장 오래됨, 모든 언어, WebDriver 표준(W3C), 방대한 자료
- 단점: **느림** (WebDriver 중계), auto-wait 없어 flaky, Grid 구성 복잡
- 특이: 대규모 레거시에선 여전히 표준이나 신규 프로젝트엔 비추천 추세

**Puppeteer**
- 장점: Chrome 저수준 제어, 빠름, 가벼움
- 단점: **Chrome만**, 테스트 프레임워크가 아니라 자동화 라이브러리 (assertion 별도), 공식 Docker 없음
- 특이: Playwright가 상위호환이라 2026년에 선택할 이유 거의 없음

**TestCafe**
- 장점: WebDriver 불필요, 설치 간단
- 단점: 커뮤니티 매우 작음(0.3M), WebKit 미지원, Claude 훈련 데이터 부족 예상
- 특이: 점유율 하락 추세

### 3.4 번외: Claude Computer Use

스크린샷 기반으로 Claude가 마우스/키보드를 조작하는 방식.

- 장점: UI 변경에 유연, 코드 작성 불필요
- 단점: **매 액션마다 API 호출 + 스크린샷 분석** → 매우 느리고 비쌈, **deterministic 불가** → 재현성 없음, CI 부적합
- 결론: E2E 테스트 용도로는 부적합. 탐색적 QA 보조 용도로만 의미

---

## 4. 최종 결정

### 4.1 결정: **Playwright + MCP-in-Docker 통합 컨테이너**

E2E 실행 엔진으로 **Playwright**를 선택하고, Claude agent가 테스트를 더 정확히 작성하도록 **Playwright MCP를 함께 컨테이너 내부에 배치**한다. MCP 탐색과 Playwright test 실행을 **같은 컨테이너에서 공존**시켜 생명주기를 단순화한다.

**Playwright 선택 근거:**
1. R1 (GUI 없는 Linux) — 공식 Docker 이미지, headless 최적화
2. R2 (멀티프로젝트 격리) — 컨테이너별 독립 세션
3. R3 (Claude 동적 생성) — TS/Python 지원, 훈련 데이터 풍부, auto-wait로 flaky 감소
4. R4 (결과물 수집) — screenshot/video/trace 내장, JSON reporter 내장
5. R7 (JSON 결과) — `@playwright/test` JSON reporter로 바로 파싱 가능
6. 시장 추세 (npm 1위, retention 94%) — 장기 지원 안정

**MCP 도입 근거:**
- R8 (테스트 품질) — Claude가 실제 DOM/selector를 탐색 후 테스트 작성 → "눈감고 쓰기" 대비 첫 실행 성공률 급상승, 실패 시 재현 관찰로 구체적 피드백 가능
- Playwright MCP 공식 지원(MS), `mcr.microsoft.com/playwright/mcp` 공식 Docker 이미지 존재

**리스크 및 완화:**
- Docker 이미지 크기 ~1.9GB → `auto_build: true`로 최초 1회만 빌드, 이후 재사용
- MCP+test CLI 공존 시 Chromium 2개 순간 기동 → Phase 2.5에서 MCP `browser_close` 호출로 정리
- HTTP/SSE transport 세션 안정성 → `--isolated` + subtask당 신규 컨테이너로 리스크 최소화

### 4.2 MCP 배치 방식 검토 및 결정

MCP를 어디에 두느냐를 놓고 3가지 구성을 비교했다:

| 구성 | 설명 | 장점 | 단점 | 결정 |
|------|------|------|------|:---:|
| **A. 호스트 stdio** | Claude CLI가 MCP 서버를 자식 프로세스로 spawn. 검증만 Docker. | 설정 단순, stdio 안정적, 자동 정리 | 호스트에 Node/Playwright/Chromium 모두 설치 필요, 호스트 오염 | X |
| **B. Docker 내부 MCP (동일 컨테이너)** | MCP 서버와 Playwright test CLI를 같은 컨테이너에 통합. HTTP/SSE로 접근. | 호스트 무오염, 완전 재현성, 리소스 제한 가능, 설계 일관성 | 포트 동적 할당 필요, `.mcp.json` 생성 로직 필요 | **채택** |
| **C. Docker 내부 MCP (별도 컨테이너)** | MCP와 test CLI를 분리된 컨테이너로. | 역할 경계 명확 | 관리 복잡(2 컨테이너), 파일시스템 공유 부담 | X |

**B 선택 근거:**
- R9(호스트 환경 의존 최소화) 충족
- MCP 서버와 `playwright test`가 **각자 독립된 Chromium 프로세스**를 띄우므로 같은 이미지에서 공존 가능 (브라우저 충돌 없음)
- subtask 단위 컨테이너 1개로 생명주기 관리 원자화
- volume mount 한 번으로 Claude가 쓴 `.spec.ts`가 test CLI에도 즉시 보임

### 4.3 컨테이너 생명주기: subtask 단위

| 선택지 | 장점 | 단점 | 결정 |
|--------|------|------|:---:|
| 전역 상주 컨테이너 1개 | 리소스 절약 | 동시성 충돌, 상태 오염 | X |
| task 단위 | subtask 간 세션 재활용 | subtask 간 상태 오염 가능 | X |
| **subtask 단위 spawn/destroy** | 완전 격리, Setup agent 패턴과 일관, MCP `--isolated`와 매칭 | 컨테이너 시작 ~3-5초 오버헤드 | **채택** |

멀티프로젝트 동시 실행은 `docker run -p 0:8931` 동적 포트 + 고유 컨테이너명 `e2e-{project}-{task}-{subtask}`로 해결.

### 4.4 에이전트 4-Phase 흐름

e2e_tester는 **탐색 → 작성 → 검증 → (실패 시) 재탐색** 4단계로 동작:

```
┌─────────────────────────────────────────────────────────────┐
│ e2e_tester agent                                            │
│                                                             │
│  Phase 1: 탐색 (MCP)                                        │
│    - Claude가 MCP 도구로 앱을 직접 조작                     │
│    - DOM, selector, 페이지 전환 흐름 파악                   │
│    - 의미 있는 테스트 시나리오 발굴                         │
│                                                             │
│  Phase 2: 스크립트 작성                                     │
│    - volume mount된 /e2e/tests/ 에 .spec.ts 작성            │
│    - Phase 1에서 확인한 정확한 selector 사용                │
│    - test_source=static/both면 기존 테스트 디렉토리 사용    │
│                                                             │
│  Phase 2.5 (옵션): MCP browser_close                        │
│    - 탐색용 Chromium 정리, 메모리 반납                      │
│                                                             │
│  Phase 3: 검증 (docker exec)                                │
│    - npx playwright test /e2e/tests --reporter=json         │
│    - 결정적 실행, pass/fail 권위 있는 판정                  │
│    - 이 결과만이 최종 결과로 인정됨                         │
│                                                             │
│  Phase 4 (실패 시): 재탐색 (MCP)                            │
│    - MCP로 실패 시점 재현, DOM/콘솔 관찰                    │
│    - "버튼 클릭 후 2초 로딩이 있었다" 같은 구체적 피드백    │
│    - Coder에게 수정 지시                                    │
└─────────────────────────────────────────────────────────────┘
```

**역할 분리 원칙:**
- MCP = Claude의 "눈과 손" (비결정적, 탐색/디버깅 전용)
- Playwright test CLI = "판정자" (결정적, 재현 가능, 최종 권위)
- 판정은 항상 Phase 3만. MCP는 Phase 1/4에서 품질을 올리는 보조 도구.

### 4.5 세부 결정 사항 종합

| 항목 | 결정 | 근거 |
|------|------|------|
| E2E 도구 | Playwright | §3 대안 검토 |
| MCP 도입 | Playwright MCP | R8 테스트 품질 |
| MCP 배치 | 컨테이너 내부, test CLI와 동일 이미지 | §4.2 비교 |
| Transport | HTTP/SSE (Docker 외부 접근) | MCP 컨테이너화 필수 조건 |
| 컨테이너 수명 | subtask 단위 spawn/destroy | §4.3 비교 |
| 테스트 생성 | dynamic + static + both | 프로젝트별 유연성 |
| 네트워크 | `--network=host` | Setup agent가 호스트에 서비스 기동 |
| 포트 할당 | Docker 동적 할당 (`-p 0:8931`) | 동시 실행 충돌 방지 |
| 레거시 처리 | `e2e_watcher.sh`는 DEPRECATED 주석만 | 참고용 유지 |
| 실행 모드 slot | browser / desktop | 빌드형 앱 확장 여지 R5 |
| Docker 제어 주체 | 호스트 헬퍼 스크립트 | Claude에 docker 권한 미부여 (보안) |

---

## 5. 아키텍처 설계

### 5.1 변경 파일

**새로 생성**

| 파일 | 설명 |
|------|------|
| `docker/e2e-playwright/Dockerfile` | MCP + test CLI 통합 이미지 |
| `docker/e2e-playwright/package.json` | `@playwright/test` + `@playwright/mcp` 의존성 |
| `docker/e2e-playwright/playwright.config.ts` | 환경변수 기반 설정 |
| `scripts/e2e_container_runner.sh` | 컨테이너 생명주기(기동/포트 조회/헬스체크/cleanup) |

**수정**

| 파일 | 변경 내용 |
|------|----------|
| `templates/config.yaml.template` | `machines.tester`에 Docker + MCP 섹션 추가 |
| `templates/project.yaml.template` | `e2e_test`에 mode, base_url, test_source 추가 |
| `config/agent_prompts/e2e_tester.md` | 4-Phase(탐색/작성/검증/재탐색) 흐름으로 전면 재작성 |
| `scripts/run_claude_agent.sh` | e2e_tester 시 컨테이너 기동 + 동적 `.mcp.json` + `--mcp-config` 전달 |
| `scripts/e2e_watcher.sh` | DEPRECATED 주석 추가 |
| `docs/agent-system-spec-v07.md` | §2.1, §4, §6.3, §15 업데이트 |

**변경 불필요**

| 파일 | 이유 |
|------|------|
| `scripts/workflow_controller.py` | `determine_pipeline()`, `run_agent()` 이미 e2e_tester 통합 완료 |

### 5.2 통합 컨테이너 Dockerfile

```dockerfile
FROM mcr.microsoft.com/playwright:v1.52.0-noble

WORKDIR /e2e

COPY package.json playwright.config.ts ./
RUN npm ci

# 기본 진입점: MCP 서버 기동 (test CLI는 docker exec로 호출)
CMD ["npx", "@playwright/mcp@latest", \
     "--isolated", "--headless", \
     "--port", "8931", "--host", "0.0.0.0"]
```

**package.json:**
```json
{
  "dependencies": {
    "@playwright/test": "1.52.0",
    "@playwright/mcp": "latest"
  }
}
```

**`playwright.config.ts` 환경변수:**
- `BROWSER` (chromium/firefox/webkit)
- `BASE_URL` (테스트 대상 URL)
- `VIEWPORT_W`, `VIEWPORT_H`
- `SCREENSHOTS` (on/off/only-on-failure)
- `VIDEO` (on/off/retain-on-failure)
- `TRACE` (on/off/retain-on-failure)

**MCP 서버와 Playwright test CLI 공존:**
두 도구는 **각자 독립된 Chromium 프로세스**를 생성 → 브라우저 충돌 없음. 같은 이미지에 두 패키지를 설치하면 하나의 컨테이너에서 모두 사용 가능.

### 5.3 컨테이너 실행 흐름 (subtask 단위)

```
[WFC가 e2e_tester 호출]
    │
    ▼
[run_claude_agent.sh]
    │
    ├─ e2e_container_runner.sh start
    │    ├─ docker run -d -p 0:8931 \
    │    │     --network=host \
    │    │     -v {codebase}/e2e-tests/{subtask}:/e2e/tests \
    │    │     -v {artifacts-dir}:/e2e/test-results \
    │    │     --name e2e-{project}-{task}-{subtask} \
    │    │     agent-hub-e2e-playwright
    │    │       → MCP 서버 기동 (Chromium 대기)
    │    │
    │    ├─ 헬스체크: MCP SSE endpoint 응답 대기 (최대 30초)
    │    └─ HOST_PORT=$(docker port ... 8931 | cut -d: -f2) 출력
    │
    ├─ 임시 .mcp.json 생성:
    │    /tmp/mcp-{task}-{subtask}.json
    │    { mcpServers: { playwright: { url: "http://localhost:${HOST_PORT}/sse" } } }
    │
    ├─ claude -p --mcp-config /tmp/mcp-{...}.json ...
    │    │
    │    ├─ Phase 1: MCP tool 호출 (탐색)
    │    │      → 컨테이너의 MCP 서버 → Chromium #1
    │    │
    │    ├─ Phase 2: /e2e/tests/*.spec.ts 작성
    │    │      (volume mount로 호스트 fs에도 즉시 반영)
    │    │
    │    ├─ Phase 2.5: MCP browser_close
    │    │      → Chromium #1 정리, 메모리 반납
    │    │
    │    └─ Phase 3: docker exec 검증
    │           docker exec e2e-{...} \
    │              npx playwright test /e2e/tests --reporter=json
    │           → Chromium #2 기동 → 결과 JSON
    │
    └─ trap cleanup (성공/실패/인터럽트 공통):
         docker stop + rm
         /tmp/mcp-*.json 삭제
```

**컨테이너 이름**: `e2e-{project}-{task}-{subtask}` — 멀티프로젝트 동시 실행 시 충돌 방지.
**포트 할당**: `-p 0:8931` — Docker가 사용 가능한 호스트 포트 자동 할당. 동시성 보장.

### 5.4 e2e_container_runner.sh 책임

| 서브커맨드 | 역할 |
|------------|------|
| `start` | 이미지 존재 확인 (없으면 auto_build) → `docker run -d` → 헬스체크 → 호스트 포트 stdout 출력 |
| `exec-test` | `docker exec <container> npx playwright test ...` → JSON 결과 파싱 |
| `stop` | 컨테이너 정지 + 제거 + 임시 .mcp.json 삭제 |

`trap` 기반으로 중간 실패/인터럽트 시에도 `stop`이 반드시 실행되도록 보장.

### 5.5 설정 스키마

**config.yaml — `machines.tester`:**
```yaml
tester:
  mode: browser                      # browser | desktop
  browser: chromium                  # MCP Docker는 chromium 전용
  viewport:
    width: 1280
    height: 720
  docker:
    image: agent-hub-e2e-playwright
    auto_build: true
    network: host
    timeout_seconds: 300
    healthcheck_timeout_seconds: 30
  mcp:
    enabled: true
    internal_port: 8931              # 컨테이너 내부 고정 포트
    isolated: true                   # --isolated 플래그
    browser_close_before_test: true  # Phase 2.5 자동 호출
  artifacts:
    screenshots: only-on-failure
    video: off
    trace: retain-on-failure
```

**project.yaml — `testing.e2e_test`:**
```yaml
e2e_test:
  enabled: false
  mode: browser
  tool: playwright
  test_source: dynamic               # dynamic | static | both
  base_url: ""                       # 비우면 codebase.service_port에서 자동 추론
  static_test_dir: ""                # test_source=static/both일 때
  test_accounts: []
```

**test_source 옵션:**
- `dynamic`: Claude가 Phase 1에서 MCP로 탐색 후 Phase 2에서 `.spec.ts` 동적 생성
- `static`: `static_test_dir`의 기존 테스트를 Phase 3에서 실행만 함 (Phase 1/2 skip)
- `both`: dynamic 먼저 실행 후, static도 실행

### 5.6 artifacts 저장 구조

```
projects/{project}/logs/{task}/e2e-artifacts/{subtask_id}/
  ├── report.json          # Playwright JSON reporter (Phase 3 결과)
  ├── screenshots/
  ├── videos/              # 활성화 시
  ├── traces/              # retain-on-failure 시
  └── mcp-session.log      # (옵션) MCP 탐색 로그
```

### 5.7 e2e_tester 프롬프트 방향

- "Windows host" → "Docker 내부 MCP + test CLI 통합 환경"
- 4-Phase 흐름 명시 (탐색/작성/검증/재탐색)
- MCP tool 사용 지침 (Phase 1/4)
- `docker exec`는 runner 스크립트가 처리, agent는 호출만
- 출력 JSON 포맷 유지: `{action, overall_result, test_results, summary}`

### 5.8 run_claude_agent.sh 변경점

e2e_tester 분기에서 기존 패턴에 추가:
1. 호출 전: `e2e_container_runner.sh start` → 컨테이너 기동 + HOST_PORT 획득
2. 임시 `.mcp.json` 생성 (SSE URL 포함)
3. `claude -p --mcp-config <임시파일> ...` 로 MCP 주입
4. 프롬프트 동적 주입:
   ```
   ## E2E 실행 설정
   - mode: {tester.mode}
   - test_source: {e2e_test.test_source}
   - browser: chromium
   - viewport: {viewport.width}x{viewport.height}
   - base_url: {e2e_test.base_url or auto-inferred}
   - static_test_dir: {e2e_test.static_test_dir}
   - artifacts_dir: projects/{project}/logs/{task}/e2e-artifacts/{subtask}/
   - container: e2e-{project}-{task}-{subtask}
   - mcp_sse_url: http://localhost:{HOST_PORT}/sse
   - test_accounts: [...]
   ```
5. trap에서 `e2e_container_runner.sh stop` 보장

---

## 6. 구현 순서 (향후)

1. **`docker/e2e-playwright/`** 디렉토리
   - `Dockerfile` (MCP + test CLI 통합 이미지, `mcr.microsoft.com/playwright:v1.52.0-noble` 베이스)
   - `package.json` (`@playwright/test` + `@playwright/mcp` 고정 버전)
   - `playwright.config.ts` (환경변수 기반 설정)
2. **`scripts/e2e_container_runner.sh`** — 컨테이너 생명주기 스크립트
   - `start`: 이미지 확인/빌드 → `docker run -d -p 0:8931` → MCP SSE healthcheck → 호스트 포트 stdout
   - `exec-test`: `docker exec ... npx playwright test ... --reporter=json`
   - `stop`: `docker stop && docker rm` + 임시 `.mcp.json` 삭제 (trap 보장)
3. **`templates/config.yaml.template`** — `machines.tester` 섹션
   - `mode`, `browser`, `viewport`
   - `docker.{image, auto_build, network, timeout_seconds, healthcheck_timeout_seconds}`
   - `mcp.{enabled, internal_port, isolated, browser_close_before_test}`
   - `artifacts.{screenshots, video, trace}`
4. **`templates/project.yaml.template`** — `testing.e2e_test` 섹션
   - `mode`, `tool`, `test_source`, `base_url`, `static_test_dir`, `test_accounts`
5. **`config/agent_prompts/e2e_tester.md`** — 4-Phase 흐름으로 전면 재작성
   - 호스트 Windows 전제 제거
   - Phase 1 (MCP 탐색) / Phase 2 (spec 작성) / Phase 2.5 (browser_close) / Phase 3 (docker exec 검증) / Phase 4 (실패 재탐색)
   - MCP tool 사용 지침 + 판정은 Phase 3 전담 원칙
   - 출력 JSON 포맷 유지 (`{action, overall_result, test_results, summary}`)
6. **`scripts/run_claude_agent.sh`** — e2e_tester 분기 추가
   - 호출 전: `e2e_container_runner.sh start` → HOST_PORT 획득
   - 임시 `/tmp/mcp-{task}-{subtask}.json` 생성 (SSE URL 주입)
   - `claude -p --mcp-config <임시파일> ...` 호출
   - 프롬프트에 컨테이너명/SSE URL/test_source/base_url 등 동적 주입
   - trap으로 `e2e_container_runner.sh stop` 보장
7. **`scripts/e2e_watcher.sh`** — DEPRECATED 주석 추가 (파일 자체는 레거시 참조용 유지)
8. **`docs/agent-system-spec-v07.md`** 업데이트
   - §2.1 장비 구성: "장비1/장비3 = 테스트장비" → "실행장비 내 Docker 컨테이너"
   - §4 E2E Test Agent: 4-Phase 흐름 반영
   - §6.3 에이전트 통신: e2e_watcher 기반 → 직접 컨테이너 호출로 교체
   - §15.3 TODO: "E2E 테스트장비 연동 / 로컬 E2E" 완료로 이관

---

## 7. 검증 방법 (향후)

### 7.1 컨테이너 단독 검증

1. **이미지 빌드**: `docker build -t agent-hub-e2e-playwright docker/e2e-playwright/`
2. **수동 기동**: `docker run -d -p 0:8931 --network=host --name e2e-manual-test agent-hub-e2e-playwright`
3. **포트 조회**: `docker port e2e-manual-test 8931` → 할당된 호스트 포트 확인
4. **MCP SSE 접근**: `curl -N http://localhost:${PORT}/sse` → event-stream 응답 수신
5. **docker exec 테스트**: 최소 `.spec.ts` 배치 후 `docker exec e2e-manual-test npx playwright test /e2e/tests --reporter=json` → JSON 결과 확인
6. **정리**: `docker stop e2e-manual-test && docker rm e2e-manual-test`

### 7.2 runner 스크립트 검증

7. `e2e_container_runner.sh start` 단독 실행 → HOST_PORT stdout 확인
8. MCP healthcheck 실패 시나리오 (예: 포트 미바인딩) → 타임아웃 후 종료 코드 확인
9. `trap` 동작 검증: runner 중간에 SIGTERM → 컨테이너와 `.mcp.json`이 모두 정리되는지

### 7.3 Claude + MCP 연동 검증

10. `claude -p --mcp-config /tmp/mcp-test.json "브라우저로 http://example.com 열고 타이틀 읽어줘"` → MCP tool 호출 로그 확인
11. 실패 재현: Claude가 존재하지 않는 selector를 넣은 spec 작성 → Phase 3 실패 → Phase 4 MCP 재탐색 로그 수집

### 7.4 WFC 파이프라인 검증

12. dummy 모드: `./run_agent.sh run e2e_tester --project test-project --task 00001 --dummy` → 기존 더미 JSON 흐름 유지 확인
13. `test_source=static`: 기존 `.spec.ts` 배치 → Phase 1/2 skip 후 Phase 3만 실행
14. `test_source=dynamic`: Claude가 MCP로 탐색 후 spec 작성 → 실제 pass 관찰
15. `test_source=both`: dynamic 생성분 + static 모두 실행, 종합 판정 확인 (판정 규칙은 추가 논의 필요)

### 7.5 동시성/격리 검증

16. 서로 다른 두 프로젝트의 e2e_tester를 **동시 실행** → 서로 다른 호스트 포트 할당 + 컨테이너명 충돌 없음
17. 같은 task 내 subtask 순차 실행 → 이전 컨테이너 완전 정리 후 다음 시작 확인
18. 실패/인터럽트 시 컨테이너 고아(orphan) 없음 → `docker ps -a | grep e2e-` 비어있음 확인

---

## 8. 참고 자료

### 도구 비교 / 시장 추세
- [Playwright vs Cypress vs Selenium 2026 다운로드 통계](https://tech-insider.org/playwright-vs-cypress-vs-selenium-2026/)
- [Performance Benchmark 2026 (TestDino)](https://testdino.com/blog/performance-benchmarks/)
- [Better Stack: Playwright vs Puppeteer vs Cypress vs Selenium](https://betterstack.com/community/guides/scaling-nodejs/playwright-cypress-puppeteer-selenium-comparison/)
- [BrowserStack: Playwright vs Selenium 2026](https://www.browserstack.com/guide/playwright-vs-selenium)

### Playwright 공식
- [Playwright Docker 공식 문서](https://playwright.dev/docs/docker)
- [Playwright JSON Reporter](https://playwright.dev/docs/test-reporters#json-reporter)
- [Playwright Trace Viewer](https://playwright.dev/docs/trace-viewer)
- [Distroless: 최적화된 Playwright Docker 이미지](https://medium.com/@thananjayan1988/optimize-the-docker-image-for-playwright-tests-3688c7d4be5f)

### Playwright MCP
- [microsoft/playwright-mcp (GitHub)](https://github.com/microsoft/playwright-mcp) — 공식 MCP 서버 리포지토리
- [Playwright MCP Docker 이미지](https://mcr.microsoft.com/en-us/product/playwright/mcp) — `mcr.microsoft.com/playwright/mcp`
- [Playwright MCP Configuration](https://github.com/microsoft/playwright-mcp#configuration) — `--isolated`, `--headless`, `--port`, `--host` 등 플래그
- [MCP Specification — Transports](https://modelcontextprotocol.io/specification/2025-03-26/basic/transports) — stdio vs HTTP/SSE
- [Claude Code — MCP Integration](https://docs.anthropic.com/en/docs/claude-code/mcp) — `--mcp-config` 사용법
