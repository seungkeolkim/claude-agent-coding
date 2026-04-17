# Task Pipeline Stage Diagram

> 작성: 2026-04-17
> 근거: `scripts/workflow_controller.py` — `determine_pipeline()`, `run_subtask_pipeline()`, `finalize_task()`

---

## 1. 전체 파이프라인 흐름

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         Task 실행 전 준비                               │
│                                                                         │
│  ┌──────────┐                                                           │
│  │ git_reset │  codebase를 base_branch HEAD로 리셋 (git enabled 시)     │
│  └────┬─────┘                                                           │
│       ▼                                                                 │
│  ┌──────────┐                                                           │
│  │ planner  │  task 분석 → subtask 분해 (plan.json 생성)                │
│  └────┬─────┘                                                           │
│       ▼                                                                 │
│  ┌─────────────┐  review_plan=true?                                     │
│  │ plan_review  │─── Yes ──▶ 사용자 승인 대기                           │
│  │  (조건부)    │           (approve / modify / cancel)                  │
│  └────┬─────────┘           modify → planner 재실행 → review_replan     │
│       │                     cancel → task 취소                          │
│       │ approve 또는 review_plan=false                                  │
│       ▼                                                                 │
│  ┌────────────┐                                                         │
│  │ git_branch │  task 전용 브랜치 생성 (git enabled 시)                 │
│  └────┬───────┘                                                         │
│       ▼                                                                 │
└───────┼─────────────────────────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                    Subtask Loop (subtask마다 반복)                       │
│                                                                         │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │              Coder ↔ Reviewer Retry Loop                        │    │
│  │                                                                 │    │
│  │  ┌────────┐                                                     │    │
│  │  │ coder  │  코드 작성/수정                                     │    │
│  │  └───┬────┘                                                     │    │
│  │      ▼                                                          │    │
│  │  ┌──────────┐                                                   │    │
│  │  │ reviewer │  코드 리뷰                                        │    │
│  │  └───┬──────┘                                                   │    │
│  │      │                                                          │    │
│  │      ├── approved ──▶ git commit (no push) → 루프 탈출          │    │
│  │      │                                                          │    │
│  │      └── rejected ──▶ retry_mode(patch/reset) + instructions    │    │
│  │                       → coder 재실행 (while 재진입)              │    │
│  │                                                                 │    │
│  └──────────────────────────────────┬──────────────────────────────┘    │
│                                     │ approved                          │
│                                     ▼                                   │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │         Post-Review Phase (testing 설정에 따라 구성)             │    │
│  │                                                                 │    │
│  │  ┌────────┐                                                     │    │
│  │  │ setup  │  서비스 기동/빌드 (testing 하나라도 enabled 시)      │    │
│  │  └───┬────┘                                                     │    │
│  │      ▼                                                          │    │
│  │  ┌─────────────┐                                                │    │
│  │  │ unit_tester │  단위 테스트 실행 (unit_test.enabled=true 시)   │    │
│  │  └───┬─────────┘                                                │    │
│  │      ▼                                                          │    │
│  │  ┌─────────────┐  Playwright + MCP-in-Docker                    │    │
│  │  │ e2e_tester  │  E2E 브라우저 테스트 (e2e_test.enabled=true 시)│    │
│  │  │             │  Phase 1: MCP 탐색 → Phase 2: spec 작성        │    │
│  │  │             │  Phase 3: docker exec 검증 → Phase 4: 재탐색   │    │
│  │  └───┬─────────┘                                                │    │
│  │      ▼                                                          │    │
│  │  ┌──────────┐                                                   │    │
│  │  │ reporter │  테스트 결과 종합 판정                             │    │
│  │  └───┬──────┘                                                   │    │
│  │      │                                                          │    │
│  │      ├── verdict=pass ──▶ 다음 단계로                           │    │
│  │      ├── verdict=fail ──▶ subtask 재시도 (전체 pipeline 재실행) │    │
│  │      └── needs_replan ──▶ planner 재실행 (plan 재구성)          │    │
│  │                                                                 │    │
│  └──────────────────────────────────┬──────────────────────────────┘    │
│                                     │ pass                              │
│                                     ▼                                   │
│                          다음 subtask로 이동                            │
│                                                                         │
└──────────────────────────────────────┼──────────────────────────────────┘
                                       │ 모든 subtask 완료
                                       ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                         Task 마무리 (Finalize)                          │
│                                                                         │
│  ┌────────────────┐                                                     │
│  │ memory_updater │  프로젝트 메모리 갱신                               │
│  └───┬────────────┘                                                     │
│      ▼                                                                  │
│  ┌────────────┐                                                         │
│  │ summarizer │  전체 task 요약 생성                                    │
│  └───┬────────┘                                                         │
│      ▼                                                                  │
│  ┌──────────┐                                                           │
│  │ git_push │  task 브랜치를 remote에 push                              │
│  └───┬──────┘                                                           │
│      ▼                                                                  │
│  ┌───────────┐                                                          │
│  │ pr_create │  PR 생성                                                 │
│  └───┬───────┘                                                          │
│      ▼                                                                  │
│      │  merge_strategy?                                                 │
│      │                                                                  │
│      ├── auto_merge ───────▶ PR 자동 머지 → done                       │
│      │                       (실패 시 → waiting_for_human_pr_approve)   │
│      │                                                                  │
│      ├── pr_and_continue ──▶ PR만 생성, 머지 안 함 → done              │
│      │                                                                  │
│      └── require_human ────▶ 사용자에게 머지 위임 → done               │
│                                                                         │
│  ┌──────┐                                                               │
│  │ done │  task 완료                                                    │
│  └──────┘                                                               │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Pipeline 구성 변형 (determine_pipeline)

testing 설정 조합에 따라 pipeline 구성이 달라진다:

```
testing 전부 disabled:
  coder → reviewer
  (post_review_phase 없음, setup/tester/reporter 생략)

unit_test만 enabled:
  coder → reviewer → setup → unit_tester → reporter

e2e_test만 enabled:
  coder → reviewer → setup → e2e_tester → reporter

unit + e2e 둘 다 enabled:
  coder → reviewer → setup → unit_tester → e2e_tester → reporter

integration_test enabled:
  (위와 동일 패턴, integration_tester 추가)
```

---

## 3. Pipeline Stage 값 목록

task JSON의 `pipeline_stage` 필드에 기록되는 값:

| Stage | 설명 | 발생 시점 |
|-------|------|-----------|
| `git_reset` | codebase를 base branch HEAD로 리셋 | task 시작 직후 |
| `planner` | Planner agent 실행 | plan 생성 |
| `plan_review` | 사용자 plan 승인 대기 | review_plan/review_replan=true |
| `git_branch` | task 전용 브랜치 생성 | plan 확정 후 |
| `coder` | Coder agent 실행 | subtask별 코드 작성 |
| `reviewer` | Reviewer agent 실행 | subtask별 코드 리뷰 |
| `setup` | Setup agent 실행 | 서비스 기동/빌드 |
| `unit_tester` | Unit Tester 실행 | unit_test enabled |
| `e2e_tester` | E2E Tester 실행 (Playwright + MCP) | e2e_test enabled |
| `reporter` | Reporter agent 실행 | 테스트 결과 종합 판정 |
| `finalizing` | 마무리 단계 진입 | 모든 subtask 완료 |
| `memory_updater` | Memory Updater 실행 | 프로젝트 메모리 갱신 |
| `summarizer` | Summarizer agent 실행 | task 요약 생성 |
| `git_push` | 브랜치 push | PR 생성 전 |
| `pr_create` | PR 생성 | push 완료 후 |
| `done` | task 완료 | 모든 처리 종료 |

---

## 4. E2E Tester 내부 Phase (상세)

e2e_tester stage 내부에서 4-Phase로 동작:

```
┌─ e2e_tester ─────────────────────────────────────────────────┐
│                                                               │
│  [Docker 컨테이너 기동]                                       │
│  e2e_container_runner.sh start                                │
│       │                                                       │
│       ▼                                                       │
│  Phase 1: MCP 탐색                    ← dynamic/both 시      │
│  (Claude가 MCP tool로 앱을 실제 브라우저에서 조작)            │
│       │                                                       │
│       ▼                                                       │
│  Phase 2: .spec.ts 작성               ← dynamic/both 시      │
│  (탐색한 selector 기반으로 테스트 스크립트 생성)              │
│       │                                                       │
│       ▼                                                       │
│  Phase 2.5: MCP browser_close (옵션)                          │
│  (탐색용 Chromium 정리, 메모리 반납)                          │
│       │                                                       │
│       ▼                                                       │
│  Phase 3: 검증 (권위 있는 판정)       ← 모든 mode            │
│  docker exec npx playwright test → report.json                │
│       │                                                       │
│       ├── PASS ──▶ JSON 결과 반환                             │
│       │                                                       │
│       └── FAIL ──▶ Phase 4: MCP 재탐색                        │
│                    (실패 시나리오 재현, 원인 분석)             │
│                    → coder_guidance 포함 JSON 반환             │
│                                                               │
│  [Docker 컨테이너 정리]                                       │
│  e2e_container_runner.sh stop (trap 보장)                     │
│                                                               │
└───────────────────────────────────────────────────────────────┘
```

test_source별 Phase 실행 범위:

| test_source | Phase 1 | Phase 2 | Phase 3 | Phase 4 |
|-------------|:-------:|:-------:|:-------:|:-------:|
| dynamic | O | O | O | (실패 시) |
| static | - | - | O | (실패 시) |
| both | O | O | O (AND 판정) | (실패 시) |
