## Playwright + MCP-in-Docker 통합 E2E 구현 핸드오프

> 작성: 2026-04-16
> 기준 문서: `docs/e2e-test-design-decision.md` (§6 구현 완료, §7 검증 진행 중), `docs/agent-system-spec-v07.md` §2.1/§2.2/§4.3/§6.3/§6.4/§15
> 선행: `docs_for_claude/020-handoff-replan-approval-and-summarizer-cleanup.md`
> 브랜치: `feature/playwright-e2e-test` (main 기준 3 commits, 미푸시)
> 커밋:
>   - `1a38e40 docs: E2E 테스트 재설계 의사결정 문서 추가`
>   - `a33632c docs: E2E 재설계 Phase 2 세부 결정 7종 확정 반영`
>   - `579af43 feat: E2E Playwright + MCP-in-Docker 통합 컨테이너 구현`

---

## 0. 한눈에 보기

기존의 원격 Windows 테스트장비 + SSH sentinel 방식(미구현 TODO)을 **폐기**하고, Playwright test CLI와 `@playwright/mcp`를 하나의 Docker 이미지로 묶은 **MCP-in-Docker 통합 컨테이너** 방식으로 전면 재설계했다. subtask 단위로 `--network=host` 컨테이너를 spawn/destroy하며, Claude Code agent는 4-Phase 흐름(탐색 → 작성 → 검증 → 재탐색)으로 동작한다.

이번 세션 범위:
1. 설계 문서 7종 의사결정 (이전 세션에서 확정, 커밋 `a33632c`)
2. **구현 완료** — Dockerfile, runner 스크립트, config/project/prompt 재작성, run_claude_agent.sh 분기 추가, 스펙 문서 갱신 (커밋 `579af43`)
3. 런타임 검증에서 **2개 버그 재현 → 수정**을 동일 커밋에 포함
4. 단위 검증 ✅ (컨테이너 단독, runner 스크립트, setup_environment, deprecated watcher)
5. 통합 검증 ⏳ (WFC + Claude MCP 실행 경로 — 다음 세션)

**다음 세션 첫 할 일은 §4.1의 "Option A 실제 모드 스모크 검증 절차"를 그대로 실행하는 것.**

---

## 1. 설계 결정 요약 (이미 확정, 참고용)

| # | 결정 | 근거 |
|---|------|------|
| 1 | Playwright 테스트 언어 = TypeScript | MCP 공식 + npm 생태계 |
| 2 | `base_url`은 `codebase.service_port`에서 자동 추론 | 간단함 우선. DB 충돌 우려는 §10.1로 이월 |
| 3 | Playwright 자체 retry 기본 0 | 첫 실패 후 무조건 성공하는 오류는 해결 불가. `testing.e2e_test.retry_count`로 override 가능 |
| 4 | Docker 환경 사전검증은 `setup_environment.sh`에 | 진입점 일원화 |
| 5 | `test_source=both` 판정 = AND | 엄격함 우선 |
| 6 | MCP 로그 보존 기본 on-failure + `always`/`never` 토글 | 실패 디버깅과 용량 최적화 균형. 성공 시에도 보존 가능하게 열어둠 |
| 7 | 이미지 빌드 트리거 = auto_build=true 기본 + 수동 `build_e2e_image.sh` 제공 | 캐시 적중 시 즉시 완료 |

세부 근거는 `docs/e2e-test-design-decision.md` §4.6.

---

## 2. 구현된 내용 (커밋 `579af43`)

### 2.1 신규 파일

- **`docker/e2e-playwright/Dockerfile`** — `mcr.microsoft.com/playwright:v1.52.0-noble` 베이스. CMD로 MCP 서버 기동 (`npx @playwright/mcp@latest --isolated --headless --port 8931 --host 0.0.0.0`). 볼륨 마운트 지점 `/e2e/tests`와 `/e2e/test-results`를 mkdir로 확보.
- **`docker/e2e-playwright/package.json`** — `@playwright/test@1.52.0` + `@playwright/mcp@latest`.
- **`docker/e2e-playwright/playwright.config.ts`** — env-var 주도 설정. `reporter: [['json', {outputFile: '/e2e/test-results/report.json'}], ['list']]` 중요.
- **`scripts/build_e2e_image.sh`** — 수동 빌드 진입점. 사전 체크(docker CLI / daemon / Dockerfile), `E2E_IMAGE` override 지원, `docker build` 추가 인자 passthrough.
- **`scripts/e2e_container_runner.sh`** — 3개 서브커맨드:
  - `start <container_name> <tests_dir> <artifacts_dir>`: 이미지 확인(없고 auto_build면 `build_e2e_image.sh` 호출) → `docker run -d -p 0:8931 --network=host` → MCP SSE 헬스체크 → stdout에 호스트 포트만 출력 (로그는 모두 stderr)
  - `exec-test <container_name> [--browser ... --base-url ... --retries ... --viewport-w ... --viewport-h ... --screenshots ... --video ... --trace ...]`: `docker exec`로 `npx playwright test --config=/e2e/playwright.config.ts` 실행. **`--reporter=json` CLI 플래그는 절대 넘기지 말 것** (버그 #2 참조).
  - `stop <container_name> [--mcp-config <path>]`: 컨테이너 정지/제거 + 임시 `.mcp.json` 삭제

### 2.2 수정 파일

- **`scripts/run_claude_agent.sh`** (line 691~) — e2e_tester 전용 분기. 가드는 `AGENT_TYPE == "e2e_tester" && DRY_RUN != "true" && DUMMY != "true" && -n SUBTASK_FILE`. 역할:
  - config.yaml + project.yaml에서 tester 설정 전부 읽음 (image, auto_build, network, healthcheck_timeout, mcp.isolated, mcp.internal_port, mcp.log_retention, browser, viewport, retry_count, test_source, mode, base_url, static_test_dir, test_accounts)
  - `base_url`이 비어 있으면 `codebase.service_port`에서 `http://localhost:{port}`로 자동 추론
  - 컨테이너명 `e2e-{project}-{task}-{subtask_seq}`, 아티팩트 디렉토리 `logs/{task}/e2e-artifacts/{subtask_id}`, 테스트 디렉토리 `{codebase}/e2e-tests/{subtask_id}`
  - `e2e_container_runner.sh start`로 호스트 포트 획득 → `/tmp/mcp-{task}-{subtask}.$.json` 동적 생성 (SSE URL `http://localhost:{port}/sse` 주입)
  - 프롬프트에 `## E2E 실행 설정` 섹션 append (`AGENT_HUB_ROOT`, 설정 값들, Phase 3 Bash 명령 템플릿 전체)
  - `CLAUDE_ARGS`에 `--mcp-config "$E2E_MCP_CONFIG_FILE"` 추가
  - `cleanup_pid` trap 확장: 종료 시 `{artifacts_dir}/report.json` 존재 여부와 `stats.unexpected` 값으로 pass/fail 판정 → `log_retention` 정책(on-failure / always / never)에 따라 `docker logs > mcp-session.log` 저장 → `e2e_container_runner.sh stop` 호출
- **`scripts/e2e_watcher.sh`** — 파일 상단에 DEPRECATED 배너, `E2E_WATCHER_ACK_DEPRECATED != true`면 exit 2.
- **`setup_environment.sh`** — `check_e2e_docker_environment()` 추가 ([5/5] 단계). docker CLI / daemon / Dockerfile 존재 / 이미지 존재 확인. 이미지 없으면 `build_e2e_image.sh` 재사용해서 빌드.
- **`templates/config.yaml.template`** — `machines.tester`를 docker+mcp 스펙으로 재작성. `docker.{image, auto_build, network, timeouts}`, `mcp.{isolated, internal_port, browser_close, log_retention}`, `artifacts`.
- **`templates/project.yaml.template`** — `testing.e2e_test`에 mode/test_source/base_url/static_test_dir 추가, retry_count override 주석.
- **`config/agent_prompts/e2e_tester.md`** — 4-Phase 흐름으로 전면 재작성. **Phase 3에서 Claude는 Bash tool로 `${AGENT_HUB_ROOT}/scripts/e2e_container_runner.sh exec-test ...`를 직접 호출**하는 설계 (runner가 자동 호출 X). AGENT_HUB_ROOT는 run_claude_agent.sh가 프롬프트에 주입.
- **`docs/agent-system-spec-v07.md`** — §2.1(장비 구성 → testing devices 취소선), §2.2(SSH 제거), §4.3(4-Phase 재작성), §6.3(장비 간 통신 → 컨테이너 통신으로 교체), §6.4(레거시 DEPRECATED 별도 섹션), §15.3(TODO에서 "E2E 테스트장비 연동"은 폐기, "로컬 E2E"는 완료로 이관), §15.4(Phase 로드맵에 본 작업 entry 추가).

### 2.3 변경 불필요 (이미 e2e_tester 통합)

- `scripts/workflow_controller.py` — `determine_pipeline()`, `run_agent()`가 이미 e2e_tester를 처리. 추가 수정 없음.

---

## 3. 런타임 검증에서 수정된 버그 2종 (같은 커밋에 포함)

두 개 모두 구현 초안 작성 후 smoke test 중 재현 → 수정 → 재검증까지 완료한 상태. 설계 결함이 아니라 shell/CLI의 세부 동작을 잘못 이해한 것.

### 3.1 버그 #1 — wait_for_mcp_ready의 curl 폴백과 SSE 충돌

**증상**: 정상 기동된 MCP 서버에 대해 `wait_for_mcp_ready()`가 30초 타임아웃까지 계속 실패.

**원인**:
```bash
status=$(curl -s -o /dev/null -w "%{http_code}" --max-time 2 ".../sse" 2>/dev/null || echo "000")
```
SSE 엔드포인트는 헤더 200 전송 후 스트림을 끊지 않는다. `--max-time 2`로 curl이 exit 28로 빠질 때, stdout에는 `200`이 이미 찍혀 있다. 그 뒤에 `||` 분기로 `000`이 한 번 더 append → 최종 `$(...)` 치환값이 `200000`이 되어 정규식 `^(200|204|405|406)$`에 매칭 실패.

**수정**: exit code 무시하고 stdout만 신뢰, 비었으면 "000" 폴백.
```bash
status=$(curl -s -o /dev/null -w "%{http_code}" --max-time 2 ".../sse" 2>/dev/null) || true
status="${status:-000}"
```

### 3.2 버그 #2 — exec-test의 --reporter=json CLI가 config outputFile을 override

**증상**: 테스트는 pass, stdout에 JSON도 출력되지만 `artifacts_dir/report.json` 파일이 생성되지 않음.

**원인**: `playwright.config.ts`에 `reporter: [['json', {outputFile: '/e2e/test-results/report.json'}], ['list']]`를 정의했는데, `exec-test` 구현이 `npx playwright test --config=... --reporter=json`처럼 CLI 플래그로 reporter를 또 지정. Playwright CLI `--reporter` 플래그는 config의 reporter 설정을 통째로 대체하고, 이때 CLI로 준 `json`에는 outputFile이 없어서 stdout으로만 출력된다.

**수정**: `exec-test`에서 `--reporter=json` 플래그 제거. config의 `[json(outputFile), list]` 조합을 그대로 사용 → 콘솔엔 list, 파일엔 json이 동시 동작.

### 3.3 주의사항 (향후 수정 시)

- SSE 헬스체크 함수를 건드릴 일이 생기면 반드시 실제 컨테이너를 띄운 상태에서 `exit code != 0`이 정상이라는 점을 의식할 것.
- `exec-test`에 reporter 관련 플래그를 추가하려면 반드시 config의 reporter 배열을 건드리는 방향으로 가야 하고, CLI 플래그 추가는 기존 설정을 통째로 날린다는 점을 명심.

---

## 4. 다음 세션 첫 작업

### 4.1 Option A 실제 모드 스모크 검증 절차

test-project의 e2e_test를 임시로 켜고, 외부 URL(example.com)을 대상으로 dynamic test_source로 Claude를 실제 실행하여 **컨테이너 기동 → `.mcp.json` 생성 → Claude + MCP 통신 → 4-Phase 수행 → cleanup**까지 풀 경로를 1회 검증한다.

**Step 1. project.yaml 임시 활성화**

`projects/test-project/project.yaml` 는 gitignore되므로 Edit으로 직접 수정하고, 검증 끝난 뒤 역으로 Edit으로 원복. `testing.e2e_test` 섹션을:

```yaml
  e2e_test:
    enabled: true
    mode: browser
    tool: playwright
    test_source: dynamic
    base_url: "https://example.com"
    static_test_dir: ""
    test_accounts: []
```

로 바꾸고, 검증 끝나면 원본으로:

```yaml
  e2e_test:
    enabled: false
    tool: playwright
    test_accounts: []
```

**Step 2. 임시 task/subtask JSON 생성**

```bash
mkdir -p /tmp/e2e-verify

cat > /tmp/e2e-verify/task.json <<'EOF'
{
  "task_id": "E2EVERIFY",
  "title": "E2E 통합 검증용 smoke task",
  "task_type": "feature",
  "requested_by": "verify-script",
  "counters": {"current_subtask_retry": 0}
}
EOF

cat > /tmp/e2e-verify/subtask-01.json <<'EOF'
{
  "subtask_id": "E2EVERIFY-1",
  "title": "example.com 타이틀 확인",
  "description": "외부 페이지 https://example.com 을 대상으로 dynamic 모드 1-spec smoke.",
  "retry_count": 0
}
EOF
```

**Step 3. 실제 모드 e2e_tester 호출**

```bash
cd ~/workspace/claude-agent-coding

./scripts/run_claude_agent.sh e2e_tester \
  --config       ./config.yaml \
  --project-yaml ./projects/test-project/project.yaml \
  --task-file    /tmp/e2e-verify/task.json \
  --subtask-file /tmp/e2e-verify/subtask-01.json 2>&1 | tee /tmp/e2e-verify/run.log
```

**Step 4. 성공 지표 확인**

```bash
# Claude가 남긴 agent 결과 JSON
cat projects/test-project/logs/E2EVERIFY/E2EVERIFY_01_06-e2e-tester_attempt-1.json

# Playwright reporter 결과
ls projects/test-project/logs/E2EVERIFY/e2e-artifacts/E2EVERIFY-1/
# 기대: report.json + (조건부) screenshots/, traces/, mcp-session.log

# Claude가 작성한 spec.ts
ls /home/azzibobjo/workspace/test-web-service/e2e-tests/E2EVERIFY-1/

# cleanup 확인
docker ps --filter name=e2e-test-project
ls /tmp/mcp-E2EVERIFY-* 2>/dev/null
```

**Step 5. Cleanup (반드시 실행)**

```bash
# 1. project.yaml 원복 (git이 tracking하지 않으므로 직접 Edit)
#    검증 끝난 뒤 위 Step 1의 "원본" 블록으로 복구

# 2. 임시 산출물 제거
rm -rf /tmp/e2e-verify
rm -rf projects/test-project/logs/E2EVERIFY
rm -rf ~/workspace/test-web-service/e2e-tests/E2EVERIFY-1

# 3. 혹시 남은 컨테이너
docker ps -a --filter "name=e2e-test-project-E2EVERIFY" --format '{{.Names}}' | xargs -r docker rm -f

# 4. 혹시 남은 mcp config
rm -f /tmp/mcp-E2EVERIFY-*
```

### 4.2 그 후 이어서 할 검증 (§7 잔여)

- §7.3-10: 같은 경로에서 Phase 4 재탐색 로그 확인 (의도적으로 실패할 spec을 넣거나 위 example.com 대신 존재하지 않는 URL을 써서 fail 유발)
- §7.4-12: `test_source=static` 검증 — test-web-service에 기존 `.spec.ts` 배치 후 static 모드
- §7.4-14: `test_source=both` AND 판정 검증
- §7.5-15: 두 프로젝트 동시 실행 (컨테이너명/포트 격리)
- §7.5-18: 인터럽트 시 컨테이너 고아 없음

### 4.3 보안 이슈 (별건)

`projects/test-project/project.yaml`의 `git.auth_token` 필드에 GitHub PAT가 **평문**으로 저장되어 있음. gitignore 덕분에 커밋 이력엔 안 올라가지만 로그/백업에 유출될 가능성이 있으니 **토큰 회수 및 재발급 후 환경변수 또는 GH_TOKEN 기반 전환 검토** 필요. §15.3의 "GH_TOKEN 환경변수 전환" 항목과 연결되는 이슈.

---

## 5. 되돌아보면 좋았던 점 / 주의할 점

### 잘 작동한 것
- **MCP-in-Docker 단일 이미지 결정**: 호스트 오염 없이 Playwright MCP를 Claude agent에 노출. 컨테이너 생명주기를 subtask에 맞춘 것도 일관성 유지에 도움.
- **stdout/stderr 분리 (runner)**: `start`의 stdout이 순수 호스트 포트만 출력되도록 모든 진단 로그를 stderr로 보내 caller가 `HOST_PORT=$(runner.sh start ...)` 로 안전하게 캡처 가능.
- **build_e2e_image.sh DRY**: runner의 auto_build와 setup_environment의 빌드 분기 모두 이 래퍼 하나를 재사용하여 경로 규칙 일관성.

### 주의할 점
- **dummy 모드와 e2e_tester 분기**: 691번 라인 가드로 dummy에서는 분기 전체가 스킵됨 → dummy 파이프라인으로는 이 기능을 검증할 수 없다. 실제 모드 or 별도 smoke-only 플래그가 필요.
- **MCP endpoint**: @playwright/mcp 최신은 `/mcp`(Streamable HTTP)를 primary로, `/sse`를 legacy로 분류. 현재 `.mcp.json`의 url은 `/sse`를 사용. Claude CLI의 기본 `url` 해석이 SSE transport이므로 호환성 이슈는 없지만, 장래 @playwright/mcp가 /sse를 dropping하면 url을 `/mcp`로 바꾸고 Claude config에 `type: "http"`를 추가해야 한다.
- **권한**: 컨테이너 내부에서 만든 `report.json`이나 마운트된 spec 파일의 owner가 `root`로 잡힘. cleanup은 동작하지만 사용자 셸에서 `rm`이 필요할 때 권한 고려 필요.

---

## 6. 참고 파일

- `docs/e2e-test-design-decision.md` — §6 구현 완료, §7 검증 진행 중, §8 구현 결과 요약, §10 후속 논의 과제
- `docs/agent-system-spec-v07.md` — §4.3 E2E agent, §6.3 컨테이너 통신, §15.4 Phase 로드맵 (본 작업 entry)
- 커밋 `579af43` — 모든 구현 + 버그 fix 2종 통합
