## Replan 승인 누락 버그 수정 + Summarizer TODO 정리 핸드오프

> 작성: 2026-04-16
> 기준 문서: `docs/agent-system-spec-v07.md` §15 (TODO 리스트 정리), `docs/task-lifecycle-fsm.md` (변경 없음 — FSM은 이미 정확)
> 선행: `docs_history/handoffs/019-handoff-coder-reviewer-context-management.md`
> 커밋: `3fa1946 fix: plan_review modify 후 replan 승인 단계 누락 버그 수정` (main 기준 1 commits, 미푸시)

---

## 0. 한눈에 보기

이번 세션은 §15.3에 남아 있던 두 개의 TODO를 정리한다:

1. **replan 승인 단계 미집행 버그** — plan_review에서 사용자가 `modify`를 요청하면 Planner가 재실행되지만, 새 plan에 대한 human review 없이 바로 subtask loop가 시작되던 버그. 실제 코드 수정 완료.
2. **Summarizer 산발적 실패** — task 00147에서 관찰된 SAFETY 차단은 이미 핸드오프 019의 커밋 `b3f0be0`로 해소된 상태였고, 이후 task 00148~00152 5건 연속 정상 완료 확인. 스펙의 stale TODO만 제거.

FSM 다이어그램(`docs/task-lifecycle-fsm.md`)은 이미 `waiting_for_human_plan_confirm → needs_replan → waiting_for_human_plan_confirm (review_replan=true)` 경로를 정확히 명시하고 있어 수정 없음. 이번 코드 수정은 FSM대로 동작하도록 구현을 맞춘 것.

---

## 1. Replan 승인 누락 버그

### 1.1 증상

test-project task 00151 로그에서 재현:

- plan v1: subtask 1개 생성 → `human_interaction.type="plan_review"` 기록
- 사용자 응답: `action="modify"`, message="subtask를 2개로 나눠줘"
- `replan_count`가 1로 증가하면서 Planner 재실행 → 새 plan(subtask 2개) 생성
- **이 시점에 `waiting_for_human_plan_confirm` 재진입이 없어야 할 `human_review_policy.review_replan=true`였음에도 곧바로 git branch → subtask loop → PR #46 생성까지 진행**
- task JSON의 `human_interaction` 필드에는 최초 plan_review만 남고, 재생성 plan에 대한 review 기록 전무

### 1.2 원인

`scripts/workflow_controller.py`의 두 곳에서 동일한 누락이 있었다:

1. **`run_pipeline()` line 1252-1279** (최초 plan_review modify 분기)
2. **`_continue_after_plan_review()` line 1668-1697** (WFC graceful resume 경로)

두 곳 모두:

```python
elif result == "modify":
    # ... Planner 재실행, save_plan_file, create_subtask_files ...
    # ← 여기서 request_human_review / wait_for_human_response 누락
# 승인 후 running 상태로 복귀
update_project_state(..., status="running", ...)
```

대조적으로 **subtask-failure로 인한 replan 분기**(line 1372, 1787)는 `review_replan` 플래그를 확인하고 `request_human_review("replan_review", ...)` → `wait_for_human_response()`를 정상 호출. 즉 "replan은 항상 검토 요청"이라는 설계 의도가 `plan_review.modify` 경로에서만 빠진 형태였다.

### 1.3 수정

두 분기에 subtask-failure 경로와 동일한 패턴 삽입:

```python
if human_review.get("review_replan", False) and not args.dummy:
    update_pipeline_stage(task_file, "plan_review", f"subtask {len(subtasks)}개 (replan)")
    plan_path = os.path.join("tasks", task_id, "plan.json")
    request_human_review(
        task_file, task_id, "replan_review",
        plan_path, len(subtasks), project_dir=project_dir,
    )
    replan_result = wait_for_human_response(
        task_file, project_dir, task_id, timeout_hours,
        re_notification_interval_hours=re_noti_hours,
    )
    if replan_result == "cancel":
        # cancelled 처리 + sys.exit(0)
    elif replan_result == "modify":
        # escalated 처리 + emit_notification("escalation") + sys.exit(1)
    # approve/timeout → 계속 진행
```

`human_interaction.type="replan_review"`로 기록하므로, 재승인 대기 중 WFC가 SIGTERM으로 종료돼도 `run_pipeline_resume()`이 `_continue_after_replan_review()`로 올바르게 디스패치된다 (기존 resume 경로 그대로 재활용).

### 1.4 검증

실제 사용자 재현:

- plan review에서 "subtask 2개를 1개로 묶어달라" modify 요청
- → Planner 재실행 → 새 plan(subtask 1개) 생성
- → `waiting_for_human_plan_confirm` 재진입 확인
- → review 알림 정상 발생

사용자 confirm.

### 1.5 회귀 위험

- `review_replan=false` 프로젝트는 기존처럼 바로 진행 (해당 `if` 블록 자체를 건너뜀)
- `args.dummy` 모드에서도 스킵 (기존 replan 경로와 동일 조건)
- 기존 subtask-failure replan 경로는 무관

---

## 2. Summarizer 산발적 실패 — 정리

### 2.1 재조사 결과

전체 task logs 스캔 (`projects/test-project/logs/00*/`):

| task | summarizer 결과 | 비고 |
|------|-----------------|------|
| 00147 | **FAIL** (SAFETY `subtask 개수 초과: 3/2`) | 2026-04-15 14:03, fix(`b3f0be0`) **11분 전** |
| 00148 ~ 00152 | 모두 OK | fix 이후 5건 연속 정상 |

summarizer 로그가 없는 task들(00120~00145 일부)은 전부 `failed`/`cancelled` 상태로 summarizer 진입 전 중단된 건들. Silent 실패 아님.

### 2.2 결론

핸드오프 019의 커밋 `b3f0be0`에 포함된 **2중 방어**가 정상 동작:

1. `scripts/workflow_controller.py:2086` — `finalize_task()` 진입 시 `current_subtask=None` 클리어
2. `scripts/check_safety_limits.py:100-107` — `current_subtask`가 이미 `completed_subtasks`에 있으면 이중 집계 안 함

스펙 §15.3의 "Summarizer 산발적 실패" TODO는 **이미 해소된 stale 항목**으로, 이번 세션에서 제거.

---

## 3. FSM 문서 점검

`docs/task-lifecycle-fsm.md`의 replan 관련 전이는 이미 정확:

- `waiting_for_human_plan_confirm → needs_replan` (reject/modify)
- `needs_replan → waiting_for_human_plan_confirm` (Replanner 성공 + review_replan=true)
- `needs_replan → planned` (Replanner 성공 + review_replan=false)

이번 코드 수정은 FSM대로 동작하도록 구현을 맞춘 것. **FSM 문서는 수정 불필요.**

참고: 코드 흐름상 hub_api.reject()가 task status를 일시적으로 `needs_replan`으로 설정하고, WFC가 이를 감지해 Planner 재실행 → 성공 시 `waiting_for_human_plan_confirm` 재진입. 이 경로는 FSM과 정확히 일치.

---

## 4. 문서 변경

### 4.1 `docs/agent-system-spec-v07.md`

- §15.2 검증 완료 항목에 2건 추가:
  - Summarizer 차단 버그 fix 설명에 "후속 task 00148~00152까지 5건 연속 정상 완료" 보강
  - "plan_review modify 후 replan 승인" 검증 한 줄 신규
- §15.3 미구현 목록에서 2건 정리:
  - "replan 승인 단계 미집행 버그" → 완료로 전환 (취소선 + §15.4 포인터)
  - "Summarizer 산발적 실패" → 행 제거 (stale)
- §15.4 Phase 로드맵에 `plan_review modify 후 replan 승인` Phase 엔트리 추가

### 4.2 `docs_for_claude/`

- `019-handoff-coder-reviewer-context-management.md` → `docs_history/handoffs/`로 이동
- 본 핸드오프 `020-handoff-replan-approval-and-summarizer-cleanup.md` 신규

### 4.3 `docs/task-lifecycle-fsm.md`

수정 없음. 기존 FSM이 이미 정확.

---

## 5. 이번 세션 커밋

```
3fa1946 fix: plan_review modify 후 replan 승인 단계 누락 버그 수정
```

main 기준 1 commits, 미푸시. 문서 업데이트는 별도 커밋 예정.

---

## 6. 다음 세션 — 후속 과제

§15.3의 남은 미구현 항목:

- **user_preferences slot** (Phase 2.2+): project_state.json에 사용자 선호 저장
- **GH_TOKEN 환경변수 전환** (Phase 2.2+): 멀티유저 시 gh 토큰 격리
- **강제 실행 옵션** (Phase 2.2+): `wait_for_prev_task_done` 무시 force 요청
- **Slack 연동** (Phase 2.3+): Telegram과 동일 패턴
- **Telegram 첨부파일** (Phase 2.3+): photo/document 다운로드
- **E2E 테스트장비 연동 / 로컬 E2E** (Phase 2.4)

우선순위는 사용자 요청에 따라 결정. 2.2+ 묶음이 가장 큰 덩어리이고, 그중 `user_preferences`가 가장 독립적이라 진입 난이도 낮음.

메모리 상태:
- `project_chatbot_execution_todo.md` — chatbot 상주 프로세스 검토 TODO, 잔존
- 이번 세션에서 생성/삭제된 메모리 없음
