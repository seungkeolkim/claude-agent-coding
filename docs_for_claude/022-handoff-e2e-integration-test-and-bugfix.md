## E2E 통합 검증 완료 + 버그 4종 수정 핸드오프

> 작성: 2026-04-17
> 기준 문서: `docs/e2e-test-design-decision.md` (§7~§9 갱신), handoff 021
> 선행: `docs_for_claude/021-handoff-playwright-mcp-docker-e2e.md`
> 브랜치: `feature/playwright-e2e-test`
> 커밋:
>   - `bf92762 fix: MCP config에 type: sse 필드 추가`
>   - `cde55c2 fix: static/both 모드에서 기존 테스트 파일을 tests_dir에 복사`
>   - `156bdda fix: both 모드 AND 판정 + WFC SIGTERM orphan 컨테이너 정리`

---

## 0. 한눈에 보기

handoff 021에서 "다음 세션 첫 할 일"로 지정된 **통합 검증(§7.3~§7.5)**을 모두 수행 완료. 5개 카테고리 12개 테스트를 실행하여 **버그 4종을 발견하고 수정**했다. 설계문서 `docs/e2e-test-design-decision.md`의 §7을 전면 갱신하고 §9(통합 검증 결과)를 신설했다.

**이 세션에서 수행한 것:**
1. Smoke test — full pipeline 정상 동작 확인 (task 00154~00155)
2. Mode별 실 테스트 — dynamic/static/both 3종 검증
3. 동시성/격리 — SIGKILL/SIGTERM 인터럽트 + WFC retry orphan
4. Runner edge cases — healthcheck 실패, 이미지 없음, build 실패
5. Config regression — 14개 설정값 + base_url 자동 추론

---

## 1. 수정된 버그 4종

### 버그 #3 — MCP config `type: sse` 누락 (커밋 `bf92762`)

- **파일**: `scripts/run_claude_agent.sh` L784
- **증상**: Claude CLI가 `.mcp.json` 스키마 거부
- **수정**: JSON 템플릿에 `"type": "sse"` 추가

### 버그 #4 — static 테스트 파일 미탑재 (커밋 `cde55c2`)

- **파일**: `scripts/run_claude_agent.sh` L746~762
- **증상**: `test_source=static/both`에서 static 파일이 컨테이너에 없음
- **수정**: `E2E_TESTS_DIR`로 `*.spec.ts`/`*.spec.js` 복사 로직 추가

### 버그 #5 — both 모드 AND 판정 불가 (커밋 `156bdda`)

- **파일**: `scripts/run_claude_agent.sh` + `config/agent_prompts/e2e_tester.md`
- **증상**: agent가 static/dynamic 결과를 구분 못 함
- **수정**: static 파일 목록(`E2E_STATIC_FILE_LIST`)을 프롬프트에 `static_files` 항목으로 주입. e2e_tester 프롬프트에 report.json basename 기반 분류 방법 명시.

### 버그 #6 — WFC SIGTERM orphan 컨테이너 (커밋 `156bdda`)

- **파일**: `scripts/workflow_controller.py`
- **증상**: WFC SIGTERM → 재시도 시작 → task=failed 종료 → 재시도 컨테이너 orphan
- **수정 (3중 방어)**:
  1. `_handle_sigterm`: flag + 자식에 SIGTERM 전파 (비블로킹 `_forward_sigterm_to_children`)
  2. `run_subtask_pipeline`: post_review 루프 / reporter retry 전 `_shutdown_requested` 체크
  3. `_cleanup_child_processes` (atexit): SIGTERM→15초→SIGKILL + Docker `docker rm -f` 안전망 + MCP `/tmp/mcp-*.json` 정리

---

## 2. 수정된 파일 목록

| 파일 | 변경 내용 |
|------|-----------|
| `scripts/run_claude_agent.sh` | MCP type:sse, static 복사, static_files 프롬프트 주입 |
| `scripts/workflow_controller.py` | SIGTERM 핸들러 강화, atexit cleanup, pipeline shutdown 체크 |
| `config/agent_prompts/e2e_tester.md` | both 모드 AND 판정 가이드 (static_files 기반 분류) |
| `docs/e2e-test-design-decision.md` | §7 전면 갱신, §9 신설, §11.4/11.5 추가 |

---

## 3. 검증 매트릭스 (전체 PASS)

| # | 테스트 | 방법 | 결과 |
|---|--------|------|------|
| 1 | Smoke (dynamic full pipeline) | task 00154, 00155, 00160 | ✅ |
| 2 | test_source=dynamic | task 00154, 00155 | ✅ |
| 3 | test_source=static | task 00156 | ✅ |
| 4 | test_source=both | task 00158 → fix → 프롬프트 검증 | ✅ |
| 5 | SIGKILL 인터럽트 | task 00159 | ✅ (expected orphan) |
| 6 | SIGTERM 인터럽트 | task 00161 | ✅ |
| 7 | WFC retry orphan fix | task 00168 | ✅ |
| 8 | Healthcheck 실패 (포트 충돌) | 직접 테스트 | ✅ |
| 9 | auto_build=false + 이미지 없음 | 직접 테스트 | ✅ |
| 10 | Build 실패 (Dockerfile 없음) | 직접 테스트 | ✅ |
| 11 | Config regression (14개 값) | 직접 테스트 | ✅ |
| 12 | Unit test suite (156개) | `./run_test.sh unit` | ✅ |

---

## 4. 현재 상태

### 4.1 project.yaml 임시 변경 (원복 필요)

현재 `projects/test-project/project.yaml`:
- `e2e_test.enabled: true` — 테스트를 위해 변경됨
- `human_review_policy: 전부 false` — 테스트를 위해 변경됨
- `merge_strategy: auto_merge` — 테스트를 위해 변경됨

원래 운영 상태:
- `e2e_test.enabled: false` (또는 필요에 따라 유지)
- `human_review_policy: review_plan/review_replan = true, review_before_merge = false`
- `merge_strategy: require_human` (또는 운영 정책에 맞게)

### 4.2 test-web-service 상태

동물 버튼이 많이 추가된 상태 (task 00154~00168까지 누적). 코드 자체에 기능 문제는 없으나, 테스트로 인한 기능 누적이 있음.

### 4.3 브랜치 상태

`feature/playwright-e2e-test`에 커밋 7개 (미푸시):
```
156bdda fix: both 모드 AND 판정 + WFC SIGTERM orphan 컨테이너 정리
cde55c2 fix: static/both 모드에서 기존 테스트 파일을 tests_dir에 복사
bf92762 fix: MCP config에 type: sse 필드 추가
e48b0b5 docs: E2E 구현 완료 + 런타임 버그 2종 fix 반영 + handoff 021 작성
579af43 feat: E2E Playwright + MCP-in-Docker 통합 컨테이너 구현
a33632c docs: E2E 재설계 Phase 2 세부 결정 7종 확정 반영
1a38e40 docs: E2E 테스트 재설계 의사결정 문서 추가
```

---

## 5. 알려진 제한사항 / 향후 과제

1. **멀티 프로젝트 동시 e2e**: host network 고정 포트 8931 → 동시 실행 불가. bridge network + 동적 포트 전환 필요 (설계문서 §11.1)
2. **both 모드 AND 판정 강화**: 프롬프트 기반 → runner 레벨 subdirectory 분리 검토 (§11.4)
3. **SIGKILL orphan 자동 정리**: cron/systemd watchdog 또는 TM 시작 시 orphan 체크 (§11.5)
4. **MCP 세션 중 Claude 크래시 복구**: 현재는 전체 재시작. Phase 3부터 재개 최적화 가능 (§11.3)

---

## 6. 다음 세션에서 할 것

1. **project.yaml 원복** — e2e_test.enabled, human_review_policy, merge_strategy를 운영 상태로 복원
2. **브랜치 push + PR** — `feature/playwright-e2e-test` → main PR 생성 및 머지
3. **운영 반영** — main에서 E2E 파이프라인이 정상 동작하는지 최종 확인
