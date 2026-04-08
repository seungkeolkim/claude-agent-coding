# Phase 2.1+ 핸드오프 — PR 직접 머지/닫기 (merge_pr / close_pr)

> 작성: 2026-04-08
> 기준 문서: `docs/agent-system-spec-v07.md`
> 브랜치: `main`

---

## 현재 상태 요약

- **Phase 2.1까지 완료:** merge_strategy 3종, complete_pr_review, PR Watcher, branch config, agent 프롬프트 제한
- **Phase 2.1+ 완료:** PR 직접 머지/닫기 액션 (`merge_pr`, `close_pr`) + Web 버튼 4종 체계

---

## Phase 2.1+ 배경

### 해결한 문제

`waiting_for_human_pr_approve` 상태에서 기존에는 두 가지 탈출 경로만 존재했다:

1. **수동 상태 보고** (`complete_pr_review`): 사용자가 GitHub에서 직접 머지/닫기 후 시스템에 알려주는 방식
2. **자동 감지** (PR Watcher): TM이 60초 폴링으로 PR 상태를 감지하는 방식

문제: 사용자가 Web Console이나 Chatbot에서 **PR을 직접 머지/닫기**할 수 없었다. GitHub UI에 가서 별도로 조작해야 했음.

### 해결 방법

시스템이 직접 `gh pr merge` / `gh pr close`를 실행하는 **두 가지 실행형 액션**을 추가했다.

---

## 구현 완료 항목

### 1. merge_pr 액션

```python
# HubAPI 메서드
def merge_pr(project, task_id, message=None):
    # 1. task 상태 검증 (waiting_for_human_pr_approve만 가능)
    # 2. task JSON에서 pr_url 읽기
    # 3. project.yaml에서 codebase.path 읽기
    # 4. gh pr merge {pr_url} --merge --delete-branch 실행
    # 5. 성공 시: task → completed, pr_review_result=merged 기록
    # 실패 시: RuntimeError (task 상태 변경 없음)
```

### 2. close_pr 액션

```python
# HubAPI 메서드
def close_pr(project, task_id, message=None):
    # 1. task 상태 검증 (waiting_for_human_pr_approve만 가능)
    # 2. task JSON에서 pr_url 읽기
    # 3. project.yaml에서 codebase.path 읽기
    # 4. gh pr close {pr_url} 실행
    # 5. 성공 시: task → failed, pr_review_result=rejected, failure_reason 기록
    # 실패 시: RuntimeError (task 상태 변경 없음)
```

### 3. 공통 헬퍼

- `_load_pr_task(project, task_id)`: `waiting_for_human_pr_approve` 상태 task 로드 + 검증
- `_get_codebase_path(project)`: `project.yaml`에서 `codebase.path` 읽기

### 4. Web Console 버튼 4종 체계

| 버튼 | 스타일 | action | 동작 |
|------|--------|--------|------|
| **Merge PR Now** | `btn-success` (초록 배경) | `merge_pr` | `gh pr merge` 실행 → completed |
| **Close PR Now** | `btn-danger` (빨강 배경) | `close_pr` | `gh pr close` 실행 → failed |
| **Mark as Merged** | `btn-outline-success` (초록 테두리) | `complete_pr_review` (merged) | 상태만 수동 반영 |
| **Mark as Rejected** | `btn-outline-danger` (빨강 테두리) | `complete_pr_review` (rejected) | 상태만 수동 반영 |

채워진 버튼 = 실행형, outline 버튼 = 상태 보고형으로 시각 구분.

### 5. CSS 추가

```css
.btn-outline-success { background: transparent; color: var(--success); border: 1px solid var(--success); }
.btn-outline-success:hover { background: var(--success); color: white; }
.btn-outline-danger { background: transparent; color: var(--danger); border: 1px solid var(--danger); }
.btn-outline-danger:hover { background: var(--danger); color: white; }
```

---

## 변경 파일 전체 목록

| 파일 | 변경 유형 | 설명 |
|------|-----------|------|
| `scripts/hub_api/core.py` | 수정 | `merge_pr()`, `close_pr()`, `_load_pr_task()`, `_get_codebase_path()` 추가, subprocess import |
| `scripts/hub_api/protocol.py` | 수정 | `_handle_merge_pr`, `_handle_close_pr` 핸들러 + ACTION_REGISTRY 등록 (20→22개) |
| `scripts/cli.py` | 수정 | `merge-pr`, `close-pr` 서브커맨드 + `cmd_merge_pr`, `cmd_close_pr` 함수 |
| `scripts/chatbot.py` | 수정 | HIGH_RISK_ACTIONS에 `merge_pr`, `close_pr` 추가 |
| `scripts/web/static/app.js` | 수정 | 버튼 4종 체계 (2곳: pending 영역, task detail), 모달 함수 추가 |
| `scripts/web/static/style.css` | 수정 | `btn-outline-success`, `btn-outline-danger` 스타일 추가 |
| `tests/test_hub_api.py` | 수정 | `TestMergePr` (5개) + `TestClosePr` (4개) 테스트 추가, `unittest.mock` import |

---

## 테스트

- **전체 206개 통과** (`./run_test.sh all`)
- 기존 197개 + 신규 9개 (TestMergePr 5개, TestClosePr 4개)
- subprocess.run을 mock하여 gh CLI 호출 검증

---

## waiting_for_human_pr_approve 탈출 경로 전체 (4가지)

| 경로 | 트리거 | gh CLI 실행 | task 결과 |
|------|--------|:-----------:|-----------|
| **Merge PR Now** | 사용자가 Web/Chat/CLI로 `merge_pr` 호출 | O (`gh pr merge`) | completed |
| **Close PR Now** | 사용자가 Web/Chat/CLI로 `close_pr` 호출 | O (`gh pr close`) | failed |
| **Mark as Merged** | 사용자가 `complete_pr_review` (merged) 호출 | X (상태만 반영) | completed |
| **Mark as Rejected** | 사용자가 `complete_pr_review` (rejected) 호출 | X (상태만 반영) | failed |
| **PR Watcher (자동)** | TM이 60초 폴링으로 MERGED/CLOSED 감지 | X (읽기만) | completed / failed |

---

## 코드 진입점

| 파일 | 용도 |
|------|------|
| `scripts/hub_api/core.py` `merge_pr()` | PR 직접 머지 |
| `scripts/hub_api/core.py` `close_pr()` | PR 직접 닫기 |
| `scripts/hub_api/core.py` `_load_pr_task()` | PR 대기 task 로드 공통 헬퍼 |
| `scripts/hub_api/core.py` `_get_codebase_path()` | project.yaml에서 codebase path 읽기 |
| `scripts/hub_api/protocol.py` ACTION_REGISTRY | 22개 action 등록 |
| `scripts/web/static/app.js` `handleMergePr()` / `handleClosePr()` | Web 모달 + dispatch |

---

## 다음 Phase 후보

| Phase | 내용 | 상태 |
|-------|------|------|
| 2.0 (잔여) | Web 오류/사용성 개선, 웹 채팅 (async claude -p) | **다음** |
| 2.2 | 고급 기능: Pipeline resume, user_preferences, Merge conflict 처리 등 | 미착수 |
| 2.3 | Messenger (Slack/Telegram) | 미착수 |
| 2.4 | E2E 테스트장비 연동, 로컬 E2E | 미착수 |
