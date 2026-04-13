# Phase 2.2 UX 개선 핸드오프

> 작성: 2026-04-13
> 기준 문서: `docs/agent-system-spec-v07.md` §15.1 "Phase 2.2 UX" 행
> 브랜치: `feature/priority_queue` (6 commits ahead of main, 푸시 완료)
> 선행: `014-handoff-priority-queue.md` (Priority Queue 본체 구현)

---

## 요약

Priority Queue(Phase 2.2) 구현 직후 이어서 진행한 CLI/Web/Chat 사용성 개선 묶음. 큰 기능 추가 없이 표시·알림·입력 중복 제거 위주. 전 구간 작동 확인 후 커밋/푸시 완료. **main 머지는 다음 세션 재개 시점에 판단.**

---

## 이번 세션 커밋 (`feature/priority_queue`)

```
f6f76e8 auto_merge 시 pr_merged 알림 이벤트 추가
2bc5448 config_override 트리에서 (기본값) 태그 제거
c830395 web task 목록에 running 치환 + 색상 구분 + 확인 카드 중복 방지
5c743f3 status 출력에 running task의 pipeline stage 표시
9f376ec list 표시 상태에 running 치환 추가
1ed37b4 submit 확인 카드에 effective config 트리 미리보기 추가
5827ba0 Priority Queue (Phase 2.2) 구현    ← 이전 세션
```

---

## 작업별 상세

### 1. Submit 확인 카드 — effective config 트리 미리보기 (`1ed37b4`)

- **문제**: 사용자가 `config_override`를 지정해도 확인 카드에는 그 JSON만 보여서, 최종 적용값(4단 merge 결과)을 눈으로 검증할 수 없었음.
- **설계**:
  - 신규 파일 `scripts/hub_api/config_preview.py`.
  - `compute_effective_config()`는 WFC의 `resolve_effective_config()`를 **그대로 재사용** (4단 merge 로직 중복 금지).
  - 필터: **블랙리스트 방식** (`HIDDEN_SECTIONS`, `HIDDEN_PATHS`). 새로운 config section이 추가돼도 코드 수정 없이 자동 노출. credential 관련 경로(`git.auth_token` 등)는 명시 제외.
  - `(수정됨)` 태그는 **override에 실제 들어있는 경로에만** 붙음. 기본값은 값만 표시(후속 커밋 `2bc5448`에서 `(기본값)` 태그 제거하여 잡음 축소).
- **연결점**:
  - `scripts/chatbot.py`: `format_confirmation_prompt(parsed, agent_hub_root)` — agent_hub_root 파라미터 신규. 호출부 `self.hub_api.root` 전달.
  - `scripts/web/web_chatbot.py`: `_format_confirmation_plain(parsed, agent_hub_root)` — 동일 로직 미러.
- **LLM 중복 생성 방지** (`c830395`의 일부):
  - SYSTEM_PROMPT에 "explanation 작성 규칙" 섹션 추가. `explanation` 필드에 마크다운 카드/파라미터 나열/"확인 또는 취소" 문구 금지. GOOD/BAD 예시 포함.
  - 이유: 파서(`parse_claude_response`)가 첫 번째 JSON 블록만 캡처하므로 LLM이 응답 앞쪽에 확인 카드를 흉내낸 JSON을 넣으면 이중 확인이 발생했음.

### 2. `list` 출력에 running 치환 (`9f376ec`)

- `HubAPI.list_tasks()`에서 프로젝트별로 `project_state.json`을 1회 읽어, `current_task_id`와 일치하면서 status ∈ {`submitted`, `planned`}이면 **표시만** `running`으로 치환.
- FSM/파일은 건드리지 않음. 표시 계층 override만.

### 3. `./run_system.sh status` — pipeline stage 표시 (`5c743f3`)

- running task의 `tasks/{task_id}-*.json`을 glob으로 찾아 `pipeline_stage`, `pipeline_stage_detail`, `current_subtask` 추출.
- 포맷: `[stage / detail]` 또는 `[stage / subtask N]`.
- 실행 라인 끝에 붙여 어느 단계에서 동작 중인지 한눈에 보이게.

### 4. Web Task 카드 running 치환 + 색상 분리 (`c830395`)

- `scripts/web/server.py`: `_PRE_RUNNING_STATUSES`, `_active_task_ids()`, `_apply_running_override()` 추가. `/api/tasks`, `/api/tasks/{project}/{task_id}` 응답에 적용.
- `scripts/web/static/app.js`: Cancel/Feedback 버튼 조건에 `running` 포함.
- `scripts/web/static/style.css`: `.status-running { background: var(--warning); color: black; }` — 기존에 completed와 같은 녹색이던 것을 **orange로 분리**.

### 5. Web Chat 확인 카드 들여쓰기 보존 + XSS 방지 (`c830395`)

- `app.js appendChatMsg`: `el.innerHTML = escapeHtml(text).replace(/\n/g, '<br>')` → `el.textContent = text`.
- `style.css .chat-msg`: `white-space: pre-wrap`.
- 공백 연속이 HTML에서 collapse되어 트리 들여쓰기가 사라지던 버그 해결. 덤으로 XSS 방지.

### 6. `pr_merged` 알림 이벤트 추가 (`f6f76e8`)

- 기존: `auto_merge` 경로는 `pr_created` 이후 바로 `task_completed`로 점프해서 "머지 완료" 피드백이 없었음. 사용자는 manual merge 때만 알림을 받음.
- 변경:
  - `scripts/workflow_controller.py`: `auto_merge` 분기에서 `git_merge_pr()` 성공 후 `emit_notification(event_type="pr_merged", ...)`.
  - `scripts/hub_api/core.py: merge_pr()`: 수동 머지 성공 후에도 동일 이벤트 발생 (try/except로 안전 래핑).
  - `scripts/notification.py`: `EVENT_STYLES["pr_merged"]` 등록, docstring 목록 갱신.
  - `scripts/web/web_chatbot.py`, `scripts/web/static/app.js`: type_label `"pr_merged": "🟢 PR 머지 완료"`.

---

## 검증 상태

- 전 항목 사용자와 함께 수동 동작 확인 완료 ("잘 작동한다" 피드백 다수).
- pytest 돌리지 **않았음** — 이번 세션 변경은 표시/알림 중심이라 기존 테스트 영향은 제한적이지만, 머지 전 `./run_test.sh all` 1회 권장.

---

## 다음 세션 후보

### A. 브랜치 머지 (우선)

- `feature/priority_queue`는 Priority Queue 본체 + 이번 UX 묶음 총 7커밋.
- 사용자가 직접 테스트 완료했다고 확인함. main 머지 여부만 결정 필요.

### B. `parse_claude_response` 다중 JSON 블록 방어 (선택)

- 현재 SYSTEM_PROMPT 규칙으로 LLM이 확인 카드를 중복 생성하지 못하게 막아놓음 (커밋 `c830395`).
- 방어적 2중 안전망으로 파서가 **마지막** 유효 JSON 블록을 선택하도록 수정하면 프롬프트 drift에도 견고. 필수는 아님.
- 위치: `scripts/chatbot.py`의 `parse_claude_response` (또는 공통 유틸).

### C. Spec 잔여 TODO (§15.3)

- user_preferences slot, GH_TOKEN 환경변수, Merge conflict 처리, 강제 실행 옵션. 모두 Phase 2.2+.

---

## 재개 가이드

```bash
# 현재 상태 확인
git checkout feature/priority_queue
git log --oneline main..HEAD   # 7개 커밋 보이면 정상

# 옵션 A: 테스트 후 main 머지
./run_test.sh all
git checkout main && git merge --no-ff feature/priority_queue
git push origin main

# 옵션 B: 추가 보강(파서 방어 등) 후 머지
```

---

## 참고: 이번 세션에서 만지지 않은 영역

- FSM 자체 (status enum, 전이 규칙) — 건드리지 않음.
- Queue 파일 포맷 / flock 로직 — Priority Queue 본체 그대로.
- 에이전트 프롬프트 (coder/reviewer/reporter/planner) — 수정 없음.
- DB 스키마 — 변경 없음.
