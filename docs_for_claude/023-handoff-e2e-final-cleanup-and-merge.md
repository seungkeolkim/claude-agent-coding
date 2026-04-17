## E2E 브랜치 최종 정리 및 머지 핸드오프

> 작성: 2026-04-17
> 선행: `docs_for_claude/022-handoff-e2e-integration-test-and-bugfix.md`
> 브랜치: `feature/playwright-e2e-test`

---

## 0. 한눈에 보기

handoff 022 이후 추가 버그 1종 수정, 문서 2종 추가/갱신, project.yaml 원복을 완료하고 머지 준비를 마쳤다.

**이 세션에서 수행한 것:**
1. 버그 #7 수정 — artifacts 설정(video/screenshots/trace) 미전달 문제
2. Task pipeline stage 다이어그램 작성 (`docs/task_status/pipeline-stage-diagram.md`)
3. 설계문서 `docs/e2e-test-design-decision.md` §9.1에 버그 #7 추가
4. project.yaml 운영 상태 원복 (사용자 직접 수행)
5. 다음 세션 과제 정리 (메모리 저장)

---

## 1. 버그 #7 — artifacts 설정 미전달

- **파일**: `scripts/run_claude_agent.sh`
- **증상**: `config.yaml`에 `artifacts.video: "on"` 설정했으나 video 파일 미생성
- **원인**: `run_claude_agent.sh`가 `machines.tester.artifacts.*` 값을 읽지 않음. `exec-test` 호출 시 `--video`/`--screenshots`/`--trace` 옵션 미전달 → `e2e_container_runner.sh` 기본값(`video=off`) 적용
- **수정**: artifacts 3개 값 읽기 추가 + Phase 3 검증 명령 프롬프트에 옵션 3개 추가

---

## 2. 수정/추가 파일

| 파일 | 변경 내용 |
|------|-----------|
| `scripts/run_claude_agent.sh` | artifacts 설정 3개(video/screenshots/trace) 읽기 + exec-test 명령에 전달 |
| `docs/task_status/pipeline-stage-diagram.md` | 신규 — 전체 파이프라인 stage 다이어그램 |
| `docs/e2e-test-design-decision.md` | §9.1에 버그 #7 추가 |
| `docs_for_claude/023-handoff-e2e-final-cleanup-and-merge.md` | 본 문서 |

---

## 3. 브랜치 커밋 이력 (전체)

```
b1838d3 docs: Task pipeline stage 다이어그램 추가
044b1df docs: E2E 통합 검증 완료 + 버그 4종 수정 반영 + handoff 022 작성
156bdda fix: both 모드 AND 판정 + WFC SIGTERM orphan 컨테이너 정리
cde55c2 fix: static/both 모드에서 기존 테스트 파일을 tests_dir에 복사
bf92762 fix: MCP config에 type: sse 필드 추가 (Claude CLI 스키마 호환)
e48b0b5 docs: E2E 구현 완료 + 런타임 버그 2종 fix 반영 + handoff 021 작성
579af43 feat: E2E Playwright + MCP-in-Docker 통합 컨테이너 구현
a33632c docs: E2E 재설계 Phase 2 세부 결정 7종 확정 반영
1a38e40 docs: E2E 테스트 재설계 의사결정 문서 추가
+ 미커밋: run_claude_agent.sh artifacts 설정 전달 수정
```

변경 규모: 17파일, +2,482줄 / -46줄

---

## 4. project.yaml 원복 완료

사용자가 직접 원복:
- `human_review_policy.review_plan`: `false` → `true`
- `human_review_policy.review_replan`: `false` → `true`
- `merge_strategy`: `auto_merge` → `require_human`
- E2E 관련 새 필드(`e2e_test` 섹션)는 그대로 유지

---

## 5. 다음 세션 과제

### E2E 관련
1. **명시적 테스트 가이드 전달** — "어떤 테스트를 해 달라"를 task/config 수준에서 전달하는 구조
2. **서비스 포트/기동 설정 전달** — 실행 중 포트 전달, 미기동 시 자동 기동, 동적 포트

### 그 외
3. **설정 복잡도 완화** — 4계층 설정 merge 구조의 파악 어려움 개선
4. **Web Console UX 개선** — 필터, 실시간 반영, F5 상태 유지
5. **merge 관련 설정 정리** — `merge_strategy`와 `review_before_merge`의 의미 겹침/충돌 가능성 해소
