# PR 머지 실패 알림 + Web UI 에러 표시 핸드오프

> 작성: 2026-04-14
> 기준 문서: `docs/agent-system-spec-v07.md` §15.3 "Merge conflict 처리" 행
> 브랜치: `feature/merge-conflict-error-notify` (main 대비 1 commit, 미푸시)
> 선행: `015-handoff-ux-improvements.md` (main에 머지 완료)

---

## 요약

`git_merge_pr()`가 merge conflict 등으로 실패할 때, 기존에는 task를 `failed`로 고정하고 `task_failed` 알림만 발송하여 사용자가 재시도할 길이 막혀 있었다. 이번 작업으로:

1. **auto_merge 실패 시 task를 `waiting_for_human_pr_approve`로 유지** — 기존 Phase 2.1+ 의 PR 버튼 4종(Merge PR Now / Close PR Now / Mark as Merged / Mark as Rejected)이 그대로 활성화되어 사용자가 PR을 수정 후 재시도하거나 수동 머지 후 표시할 수 있다.
2. **`pr_merge_failed` 알림 이벤트 신설** — `pr_merged`와 동일 메커니즘으로 CLI/Web Chat에 에러 메시지 전달.
3. **에러 메시지 영속화** — `pr_merge_error`/`pr_merge_error_at` 필드를 task JSON에 저장. Web UI가 SSE로 재렌더링되어도 빨간 `.pr-error` 박스가 그대로 남는다 (기존 `showPrError`는 DOM-only라 재렌더링 시 사라지는 문제 해결).

---

## 이번 세션 커밋

```
78f093b PR 머지 실패 시 사용자 알림 + Web UI 에러 표시
```

main 대비 1 commit. 미푸시 상태.

---

## 변경 파일

### 1. 알림 이벤트 (`scripts/notification.py`)

- `EVENT_STYLES["pr_merge_failed"]` 등록: RED, label "PR 머지 실패".
- docstring 이벤트 목록에 추가.

### 2. WFC auto_merge 분기 (`scripts/workflow_controller.py:1817-1840`)

- `git_merge_pr()` 호출을 내부 try/except로 감싸 실패 분기 추가.
- 실패 시:
  - `status` → `waiting_for_human_pr_approve` (기존 `failed` 아님).
  - `pr_merge_error`, `pr_merge_error_at` 필드 기록.
  - `emit_notification(event_type="pr_merge_failed", ...)`.
  - `current_subtask` clear, `pipeline_stage` done, `project_state` idle.
  - `return` — task_completed 알림은 발송하지 않음.
- 외부 try/except(PR 생성/기타 실패)는 그대로 유지 → 그 경로는 여전히 `failed`.

### 3. HubAPI `merge_pr()` (`scripts/hub_api/core.py:828~`)

- `gh pr merge` 실패 시:
  - task JSON에 `pr_merge_error`, `pr_merge_error_at` 영속 저장 (Web UI 재렌더링에도 남도록).
  - `pr_merge_failed` 알림 발송 (try/except 래핑).
  - 이후 `RuntimeError` raise는 그대로 (Web 비동기 핸들러가 `pr_action_result` SSE로 인지).
- 재시도 성공 시 `pr_merge_error`/`pr_merge_error_at` 필드 pop — stale 에러가 completed task에 남지 않도록.

### 4. HubAPI `pending()` (`scripts/hub_api/core.py:626~`)

- `waiting_for_human_pr_approve` task에서 `pr_merge_error`가 있으면 **별도 필드**로 전달.
- `message` 필드는 clean 유지 (`"PR 리뷰/머지 대기: {title}"` 그대로). 사용자 피드백: 메시지 안에 에러를 통으로 박으면 가독성 해침.

### 5. 모델 (`scripts/hub_api/models.py`)

- `HumanInteractionInfo.pr_merge_error: Optional[str]` 필드 추가.

### 6. Web DB 스키마 (`scripts/web/db.py`)

- `tasks` 테이블에 `pr_merge_error TEXT`, `pr_merge_error_at TEXT` 컬럼 추가.
- `_migrate()`에 두 컬럼 ALTER TABLE 추가.
- `upsert_task()` INSERT/UPDATE에 두 필드 반영.

### 7. Web UI (`scripts/web/static/app.js`)

- **태스크 목록 row**: `failure_reason`의 `!` 뱃지 옆에 `pr_merge_error`용 `⚠` 뱃지(tooltip에 에러).
- **승인 대기 카드 (`pending-list`)**: PR 버튼 4종 아래에 조건부 `<div class="pr-error">⚠️ PR 머지 실패: ...</div>`.
- **태스크 상세 인라인 뷰**: `waiting_for_human_pr_approve` 상태에서 버튼 영역에 동일한 `.pr-error` 빨간 div.
  - 초기 시도(failure-box 스타일)에서 사용자 피드백으로 `.pr-error` 스타일로 통일 — 기존 `showPrError`의 빨간 메시지와 시각적 일관성.
- `_formatNotificationForChat` type_label에 `"pr_merge_failed": "⚠️ PR 머지 실패 (사용자 개입 필요)"` 추가.

### 8. Web Chatbot (`scripts/web/web_chatbot.py`)

- `_format_notification_for_chat` type_labels에 동일 엔트리 추가. Chat 시스템 메시지로 "⚠️ PR 머지 실패 ..." 표시.

### 9. Config template (`templates/config.yaml.template`)

- `notification.events`에 `pr_merged: true`, `pr_merge_failed: true` 추가 (기존 템플릿에 `pr_merged`도 누락되어 있어 함께 보강).

---

## 동작 흐름

### 시나리오 A: auto_merge 실패

```
WFC: git_create_pr() → 성공 → pr_created 알림
WFC: git_merge_pr() → RuntimeError (예: "Pull request #39 is not mergeable...")
  ↓
task.status = waiting_for_human_pr_approve
task.pr_merge_error = "..."
project_state.status = idle
pr_merge_failed 알림 발송
  ↓
Web UI (SSE 재렌더링 후에도 유지):
  - Dashboard 승인 대기 카드: 버튼 4종 + 빨간 에러 박스
  - Tasks 상세 뷰: 동일
  - Chat: "⚠️ PR 머지 실패 (사용자 개입 필요) — ..."
  ↓
사용자 선택지:
  (1) GitHub에서 PR 수정 → Merge PR Now 재시도
  (2) 직접 merge 후 Mark as Merged
  (3) Close PR Now / Mark as Rejected
```

### 시나리오 B: 수동 Merge PR Now 실패

```
Web UI: Merge PR Now 클릭 → /api/pr/merge 비동기 처리
HubAPI.merge_pr() → gh pr merge 실패
  ↓
task.pr_merge_error 기록 (status 변동 없음, 이미 waiting_for_human_pr_approve)
pr_merge_failed 알림 발송
RuntimeError raise → 백그라운드 스레드가 pr_action_result SSE 발행 (data.error 포함)
  ↓
app.js pr_action_result 리스너: showPrError() 호출 → DOM-only 에러 div 표시
(추가로 영속된 pr_merge_error는 다음 SSE 재렌더링에도 유지)
```

### 재시도 성공

- `merge_pr()` 성공 분기에서 `pr_merge_error`/`pr_merge_error_at` pop → completed task에 stale 에러 노출 방지.

---

## 설계 판단 기록

1. **실패 시 task를 failed 아니라 waiting으로 유지** — failed로 두면 user가 cancel/resubmit밖에 못하는데, merge conflict는 사용자가 GitHub에서 rebase/resolve 하면 해결되는 경우가 많으므로 재시도 경로 확보가 우선.
2. **pr_merge_error를 pending message에 넣지 않음** — 1차 구현에서 message 앞에 박아넣었으나 사용자 피드백으로 별도 필드로 분리. 이유: 메시지 가독성 + 프론트가 스타일(빨간 박스) 자율 결정.
3. **Web UI pr-error 스타일 재사용** — Phase 2.0 Chat의 `showPrError`가 쓰던 `.pr-error` 클래스를 그대로 써서 시각적 일관성 확보. `failure-box`와 구분하여 "merge 실패 (사용자 개입으로 해소 가능)" 의미 전달.
4. **project_state idle 전환** — auto_merge 실패 시 WFC 종료. TM의 queue 블로킹은 task.status가 `waiting_for_human_pr_approve`이므로 `incomplete_statuses`로 자연스럽게 다음 task 차단.

---

## 검증 상태

- 사용자 수동 동작 확인 완료:
  - auto_merge 실패 시 `pr_merge_failed` 알림 Chat에 도착 ✅
  - 최초 구현 시 메시지 통합 형태는 reject, 별도 필드로 분리 후 빨간 박스 OK ✅
- pytest 미실행. 기존 `test_hub_api_merge_pr.py` 등이 실패 시 동작을 검증하고 있으니 머지 전 `./run_test.sh all` 권장.

---

## 다음 세션 후보

### A. 테스트 보강 (권장)

- `tests/test_hub_api_merge_pr.py`에 실패 시 `pr_merge_error` 저장 + `pr_merge_failed` 알림 발송 검증 추가.
- WFC auto_merge 실패 분기 integration test: dummy로 `git_merge_pr` 실패 mock → task status `waiting_for_human_pr_approve`, 알림 발생 검증.

### B. main 머지

- 테스트 추가 후 `./run_test.sh all` → main 머지.

### C. spec §15.3 잔여 TODO (Phase 2.2+)

- user_preferences slot
- GH_TOKEN 환경변수 전환
- 강제 실행 옵션 (wait_for_prev_task_done 무시)

---

## 재개 가이드

```bash
git checkout feature/merge-conflict-error-notify
git log --oneline main..HEAD   # 78f093b 1개

# 옵션 A: 테스트 보강 후 머지
# (tests/test_hub_api_merge_pr.py 편집)
./run_test.sh all
git checkout main && git merge --no-ff feature/merge-conflict-error-notify

# 옵션 B: 현재 구현으로 바로 머지
./run_test.sh all   # 기존 테스트 회귀 확인
git checkout main && git merge --no-ff feature/merge-conflict-error-notify
```

---

## 참고: 이번 세션에서 만지지 않은 영역

- FSM 자체 (status enum, 전이 규칙) — 변경 없음. 기존 `waiting_for_human_pr_approve`를 재사용.
- Priority Queue / `.ready` 관련 — 건드리지 않음.
- `close_pr()` — 기존 동작 유지 (현재 요구에 해당 없음).
- 에이전트 프롬프트 — 수정 없음.
