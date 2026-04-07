# Phase 2.1 핸드오프 — merge_strategy + PR Watcher + Branch Config

> 작성: 2026-04-07
> 기준 문서: `docs_for_claude/004-agent-system-spec-v6.md`
> 브랜치: `feature/project-state-management`

---

## 현재 상태 요약

- **Phase 1.0~2.0 완료:** 수동 pipeline → TM → CLI → 알림 → Usage check → Chatbot → 사용성 개선 → Web Console → 프로젝트 lifecycle
- **Phase 2.1 완료:** merge_strategy 3종 + complete_pr_review + PR Watcher + branch config + agent 프롬프트 제한 규칙

---

## Phase 2.1 배경

### 해결한 문제

1. **`auto_merge: bool`의 한계:** PR 처리 방식이 2가지(자동 머지 vs 수동 대기)로만 구분 → 3가지 전략 필요
2. **`waiting_for_human_pr_approve` 탈출 불가:** PR이 머지/거부되어도 task가 영원히 대기 상태 → 수동(complete_pr_review) + 자동(PR Watcher) 탈출 경로 구현
3. **브랜치 설정 미분리:** feature branch 시작점과 PR 대상이 같은 설정 → `base_branch`와 `pr_target_branch` 분리
4. **`require_human`이 다음 task를 차단하지 않음:** `waiting_for_human_pr_approve`가 `incomplete_statuses`에 없었음 → 추가
5. **Coder agent가 git 명령/PR 생성:** 프롬프트에 제한 규칙 없어 의도치 않은 동작 발생 → 4개 agent 프롬프트에 제한 섹션 추가

---

## 구현 완료 항목

### 1. merge_strategy 3종 enum

`auto_merge: bool` → `merge_strategy: str` 전환.

| 전략 | 동작 | task 최종 상태 | 다음 task 차단 |
|------|------|:-------------:|:-------------:|
| `require_human` (기본값) | PR 생성 → 사람이 머지할 때까지 대기 | `waiting_for_human_pr_approve` | O |
| `pr_and_continue` | PR 생성 → task 즉시 완료, 다음 task 진행 | `completed` | X |
| `auto_merge` | PR 생성 → `gh pr merge` 자동 머지 | `completed` | X |

**하위 호환:** `auto_merge: true/false` 키가 남아있으면 WFC가 자동 변환 (`true` → `"auto_merge"`, `false` → `"require_human"`).

**변경 파일:**
- `scripts/workflow_controller.py` `finalize_task()` — merge_strategy 분기 로직
- `templates/project.yaml.template` — `auto_merge: false` 제거, `merge_strategy: "require_human"` 추가
- `scripts/init_project.py` — `merge_strategy: "require_human"` 기본값
- `docs/configuration-reference.md` — merge_strategy 설명 추가

### 2. base_branch + pr_target_branch 분리

| 설정 | 용도 | 예시 |
|------|------|------|
| `base_branch` | feature branch 생성 기준 (`git checkout -b feature/... base_branch`) | `develop` |
| `pr_target_branch` | PR 머지 대상 브랜치 | `main` |

**변경 파일:**
- `scripts/workflow_controller.py` — `base_branch = git_config.get("base_branch", default_branch)` 사용
- `templates/project.yaml.template` — `base_branch` 필드 추가
- `scripts/init_project.py` — `base_branch` 대화형 입력

### 3. complete_pr_review 액션

`waiting_for_human_pr_approve` → `completed`(merged) 또는 `failed`(rejected) 수동 전이.

```python
# HubAPI 메서드
def complete_pr_review(project, task_id, result, message=None):
    # result: "merged" → completed, "rejected" → failed
    # 상태 검증: waiting_for_human_pr_approve일 때만 동작
    # task JSON에 pr_review_result, pr_reviewed_at 기록
```

**변경 파일:**
- `scripts/hub_api/core.py` — `complete_pr_review()` 메서드
- `scripts/hub_api/protocol.py` — `_handle_complete_pr_review` 핸들러 + ACTION_REGISTRY 등록
- `scripts/cli.py` — `complete-pr-review` 서브커맨드
- `scripts/chatbot.py` — HIGH_RISK_ACTIONS에 추가
- `scripts/web/static/app.js` — PR Merged/PR Rejected 버튼 분리

**테스트:** `tests/test_hub_api.py` — 7개 TestCompletePrReview 테스트 (정상 merged/rejected, 잘못된 상태, 잘못된 result 값, task 미존재 등)

### 4. PR Watcher 스레드

TM 내 백그라운드 스레드로 60초 주기 PR 상태 폴링.

```
60초마다:
  모든 프로젝트 → waiting_for_human_pr_approve task 스캔
  → pr_url로 `gh pr view --json state` 실행
  → MERGED → task completed + 알림
  → CLOSED → task failed + 알림
  → OPEN → 변경 없음
```

**변경 파일:**
- `scripts/task_manager.py` — `start_pr_watcher()`, `_pr_watcher_loop()`, `_check_pending_prs()`, `_check_pr_state()`, `_save_task_json()`, `_emit_pr_notification()`
- dummy 모드에서는 비활성

### 5. TM 폴링 주기 5초 → 2초

- `scripts/task_manager.py` — `polling_interval` 기본값 변경

### 6. incomplete_statuses 수정

`waiting_for_human_pr_approve`를 `incomplete_statuses`에 추가하여 `require_human` 전략 시 다음 task가 차단되도록 수정.

```python
incomplete_statuses = {
    "in_progress", "planned", "running",
    "waiting_for_human_plan_confirm", "needs_replan",
    "waiting_for_human_pr_approve",  # ← 추가
}
```

**핵심 동작:**
- `require_human` → `waiting_for_human_pr_approve` → 다음 task 차단 (PR merged/rejected 후 해제)
- `pr_and_continue` → 즉시 `completed` → 다음 task 진행

### 7. Agent 프롬프트 제한 규칙

| Agent | 추가된 제한 |
|-------|-----------|
| Coder | git 명령 금지, 서버 기동 금지, 패키지 설치 금지, scope 밖 작업 금지 |
| Reviewer | 코드 수정 금지, 파일 생성/삭제 금지, git 읽기 전용만 |
| Reporter | 코드 수정 금지, git 읽기 전용만, task JSON 직접 수정 금지 |
| Planner | 코드 수정 금지, git 읽기 전용만, 프로젝트 설정(base_branch, pr_target_branch, merge_strategy) 참고 |

**배경:** Task 108에서 Coder agent가 `gh pr create`를 직접 실행하여 WFC의 PR 생성과 충돌한 문제 해결.

### 8. Chatbot merge_strategy 가이드

- 시스템 프롬프트에 `git.merge_strategy` config_override 스키마 추가
- 사용 예시: "PR 올리고 바로 다음 작업" → `pr_and_continue`, "PR 자동 머지해" → `auto_merge`
- 확인 프롬프트에서 dict/list 타입 config_override를 pretty-print JSON으로 출력 (80자 truncation 대신)

---

## 변경 파일 전체 목록

| 파일 | 변경 유형 | 설명 |
|------|-----------|------|
| `scripts/hub_api/core.py` | 수정 | complete_pr_review(), default git settings 업데이트 |
| `scripts/hub_api/protocol.py` | 수정 | complete_pr_review handler + ACTION_REGISTRY (20개) |
| `scripts/workflow_controller.py` | 수정 | merge_strategy 분기, base_branch 사용 |
| `scripts/task_manager.py` | 수정 | PR Watcher 스레드, 폴링 2초, incomplete_statuses |
| `scripts/cli.py` | 수정 | complete-pr-review 서브커맨드 |
| `scripts/chatbot.py` | 수정 | HIGH_RISK_ACTIONS, merge_strategy 가이드, pretty-print |
| `scripts/web/static/app.js` | 수정 | PR Merged/Rejected 버튼 분리 |
| `scripts/init_project.py` | 수정 | base_branch 입력, merge_strategy 기본값 |
| `scripts/notification.py` | 수정 | auto_merge 참조 제거 |
| `templates/project.yaml.template` | 수정 | base_branch, merge_strategy, 레거시 주석 제거 |
| `config/agent_prompts/coder.md` | 수정 | 제한 섹션 추가 |
| `config/agent_prompts/reviewer.md` | 수정 | 제한 섹션 추가 |
| `config/agent_prompts/reporter.md` | 수정 | 제한 섹션 추가 |
| `config/agent_prompts/planner.md` | 수정 | 프로젝트 설정 참고 + 제한 섹션 추가 |
| `docs/configuration-reference.md` | 수정 | merge_strategy 테이블, base_branch 추가 |
| `docs/task-lifecycle-fsm.md` | 수정 | merge_strategy 전이, PR Watcher 자동 전이, incomplete_statuses |
| `docs/images/task-lifecycle-fsm.dot` | 수정 | 다이어그램 라벨 업데이트 |
| `README.md` | 수정 | auto_merge 참조 → merge_strategy |
| `tests/conftest.py` | 수정 | fixture에서 auto_merge → merge_strategy |
| `tests/test_hub_api.py` | 수정 | 7개 TestCompletePrReview 추가, create_project 테스트 수정 |

---

## 커밋 이력 (feature/project-state-management 브랜치)

```
946efbb require_human 시 다음 task 차단: incomplete_statuses에 waiting_for_human_pr_approve 추가
92257f4 Agent 프롬프트 제한 규칙 추가 + Chatbot merge_strategy 가이드
ec8a3a5 Chatbot 확인 프롬프트에서 config_override를 pretty print JSON으로 출력
2f92075 Phase 2.1: merge_strategy + complete_pr_review + PR Watcher + branch config
488ef10 프로젝트 lifecycle 도입 (active/closed) + close/reopen API
a2241cc Task 상태명 명확화 + FSM 다이어그램 배경 흰색 전환
```

---

## 테스트

- **전체 197개 통과** (`./run_test.sh all`)
- 신규 테스트: TestCompletePrReview (7개), create_project 테스트 수정

---

## 코드 진입점

| 파일 | 용도 |
|------|------|
| `scripts/hub_api/core.py` `complete_pr_review()` | PR 리뷰 수동 완료 |
| `scripts/workflow_controller.py` `finalize_task()` | merge_strategy 3종 분기 |
| `scripts/task_manager.py` `_pr_watcher_loop()` | PR 상태 자동 감지 |
| `scripts/task_manager.py` `has_incomplete_tasks()` | 미완료 task 스캔 (waiting_for_human_pr_approve 포함) |
| `config/agent_prompts/*.md` | Agent 프롬프트 (제한 규칙 포함) |
| `docs/task-lifecycle-fsm.md` | Task 상태 FSM 다이어그램 |

---

## 다음 Phase 후보

| Phase | 내용 | 상태 |
|-------|------|------|
| 2.0 (잔여) | Web 오류/사용성 개선, 웹 채팅 (async claude -p) | **다음** |
| 2.2 | 고급 기능: Pipeline resume, user_preferences, Merge conflict 처리 등 | 미착수 |
| 2.3 | Messenger (Slack/Telegram) | 미착수 |
| 2.4 | E2E 테스트장비 연동, 로컬 E2E | 미착수 |
