# Coder/Reviewer Context Management 핸드오프

> 작성: 2026-04-15
> 기준 문서: `docs/agent-system-spec-v07.md` §15.1 "Coder/Reviewer Context Management" 섹션
> 브랜치: `feature/coder-reviewer-context-management` (main 대비 2 commits, 미머지)
> 선행: `018-handoff-telegram-phase-2.3-completion.md` (Telegram, 본 세션과 함께 아카이브)
> 이전 메모 무효화: `memory/project_reviewer_retry_mode_todo.md` — 본 세션에서 해소됨

---

## 0. 한눈에 보기

이번 세션은 **재시도 루프에서 Coder/Reviewer가 직전 attempt를 인지하지 못해 수정 지시가 중복 추가로 누적되는 문제**를 해결한다. test-project에서 "동물 이름 버튼 추가" 같은 단순 task가 retry할 때마다 버튼이 1→2→3개로 늘어나던 사례가 출발점.

해결 방향은 4축으로 정리한다:

1. **start_sha 캡처 + attempt_history**: 매 subtask 시작 직후 worktree HEAD를 in-memory로 보관. 모든 attempt의 Coder intent_report와 Reviewer feedback을 누적.
2. **Reviewer 출력 스키마 개정**: `retry_mode ∈ {continue, reset}` 명시 + `current_state_summary`/`what_is_wrong`/`what_should_be`/`actionable_instructions` 강제. git diff `subtask_start_sha`를 판정 근거로 강제.
3. **Coder 동작 분기**: `retry_mode=reset`이면 worktree를 start_sha로 git reset --hard 후 새로 시작. `continue`면 기존 변경 위에 actionable_instructions만 덧붙임. 매 attempt에 `intent_report` 출력 강제.
4. **커밋/푸시 타이밍 재정의**: Reviewer가 `approved` 한 시점에만 WFC가 worktree를 commit. 푸시는 PR 생성 직전 1회만. Coder가 몰래 commit하면 WFC가 soft reset으로 되돌림.

부수적으로 Replan 시 task 브랜치 폐기→재생성 동작과 마지막 subtask 후 Summarizer가 safety 한도 오판으로 차단되던 버그도 같이 처리.

테스트 260개 통과. test-project task 00147(subtask 2개)로 실동작 검증 완료. PR #42 생성까지 정상 동작.

---

## 1. 동기 — 무엇이 잘못되어 있었는가

### 1.1 증상

test-project task 00141~00146은 모두 "header에 동물 이름 버튼 추가"였다. attempt 1은 정상 동작했지만 Reviewer가 사소한 nit으로 reject할 경우, attempt 2/3에서 같은 버튼이 또 추가되어 화면에 동일한 동물 이름 버튼 2~3개가 떠 있게 되는 일이 반복됐다.

### 1.2 근본 원인

| 단계 | 문제 |
|------|------|
| Coder retry | 직전 attempt의 변경이 worktree에 그대로 남아 있는데, prompt에는 "subtask는 X를 추가하는 일"이라는 원본 지시만 들어가서 Coder가 "아직 안 된 줄 알고" 또 추가 |
| Reviewer | 거절 사유는 줬지만, 그 거절이 "엉망이니 새로 짜라"인지 "조금만 고치면 된다"인지 구조적으로 구분되지 않음 |
| WFC | attempt 별로 의미 있는 시작점(SHA)을 잡지 않음. retry 모드 개념 없음 |
| commit | 매 attempt에서 push가 되어 잘못된 중간 상태가 remote에 노출됨 |

### 1.3 설계 결정

- **start_sha**: 디스크에 영속할 만큼의 가치는 없다. WFC 프로세스 메모리에만 보관 (재기동 시 attempt history는 사라져도 attempt 1로 다시 시작하면 됨).
- **두 모드만**: continue / reset의 2-way로 충분. 더 잘게 쪼개면 Reviewer가 헷갈린다.
- **commit 주체는 WFC만**: Coder가 commit하면 안 한 만큼의 변경이 worktree에 남지 않아 reset이 깨진다. 따라서 Coder가 commit해도 WFC가 soft reset으로 되돌리는 방어 코드를 넣음.
- **푸시는 PR 생성 시 1회**: 중간 push가 사라져 잘못된 상태의 외부 노출이 없음. 동시에 task별로 명시적인 브랜치 이름으로만 push해 cross-project 오염도 차단.

---

## 2. 구현 변경

### 2.1 WFC (`scripts/workflow_controller.py`)

신규 헬퍼:
- `git_head_sha(codebase_path)` — 현재 HEAD SHA
- `git_reset_hard_to(codebase_path, sha)` — reset 모드용
- `git_soft_reset_if_moved(codebase_path, expected_sha)` — Coder가 몰래 commit한 경우 방어
- `git_commit_worktree_no_push(codebase_path, message, author)` — 승인 시 commit만, push X
- `git_wipe_and_recreate_task_branch(codebase_path, branch, base_branch)` — replan 시 브랜치 폐기 후 base_branch에서 재생성
- `validate_reviewer_output(result)` — 새 스키마 강제, 위반 시 1회 재요청
- `_subtask_file_path(...)`, `_inject_subtask_runtime_fields(...)` — subtask JSON에 retry_mode/attempt_history/start_sha 주입

핵심 로직 재구성:
```
run_subtask_pipeline(subtask):
    start_sha = git_head_sha()      # in-memory
    attempt_history = []            # in-memory (매 attempt마다 append)
    for attempt in 1..max_retry:
        inject_subtask_runtime_fields(retry_mode=mode_for_attempt, history=attempt_history,
                                      start_sha=start_sha, latest_instructions=...)
        if mode == "reset":
            git_reset_hard_to(start_sha)
        coder_result = run_coder()
        git_soft_reset_if_moved(start_sha or last_attempt_sha)   # Coder commit 방어
        reviewer_result = run_reviewer()                         # 1회 재요청까지
        attempt_history.append({coder_intent_report, reviewer_feedback})
        if reviewer.action == "approved":
            git_commit_worktree_no_push(commit_msg)              # 승인 시에만 commit
            return success
        else:
            mode_for_next = reviewer.retry_mode  # "continue" or "reset"
    # 한도 초과 → replan 경로로
```

PR 생성 직전:
```
git_push(codebase_path, remote, task_branch, token=auth_token)   # 단 1회
git_create_pr(...)
```

Replan 두 경로(`run_pipeline()`, `run_pipeline_resume()`)에서 모두:
```
git_wipe_and_recreate_task_branch(codebase_path, task_branch, base_branch)
completed_subtasks = []          # task JSON에서 비움
emit_notification(event_type="replan_started")
```

### 2.2 Reviewer 프롬프트 (`config/agent_prompts/reviewer.md`)

**필수 출력 스키마**:
```json
{
  "action": "approved" | "rejected",
  "retry_mode": "continue" | "reset",          // rejected 시 필수
  "current_state_summary": "...",              // approved/rejected 모두 필수
  "what_is_wrong": "...",                      // rejected 시 필수
  "what_should_be": "...",                     // rejected 시 필수
  "actionable_instructions": ["...", "..."],   // rejected 시 필수, list
  "feedback": "..."                            // rejected 시 톤 부드러운 자유 텍스트
}
```

**판정 강제**:
- 매 호출 첫 단계로 `git diff {subtask_start_sha} -- .` 실행
- intent_report와 diff가 다르면 diff를 신뢰
- retry_mode 결정 가이드:
  - **reset**: 방향이 잘못됐거나, 큰 폭 재작업이 필요하거나, 중복 추가/엉뚱한 파일 수정 등 "엎고 다시" 케이스
  - **continue**: 핵심 동작은 맞고 정리/주석 제거/누락된 한 줄 정도면 충분한 케이스

### 2.3 Coder 프롬프트 (`config/agent_prompts/coder.md`)

**retry_mode 인지**:
- subtask context에 `retry_mode`가 있으면 재시도 상황
- `reset`: worktree는 이미 start_sha로 되돌려진 상태. attempt_history에서 "왜 reset됐는지" 읽고 같은 실수 피하면서 처음부터 다시 구현
- `continue`: 이전 attempt의 변경이 worktree에 그대로 남아 있음. `git diff {subtask_start_sha}`로 현 상태 확인 후 actionable_instructions만 덧붙임. **중복 추가 금지**

**필수 출력**:
```json
{
  "action": "code_complete",
  "changes_made": [{"file": "...", "change_type": "...", "summary": "..."}],
  "intent_report": {
    "what_changed": "...",
    "why": "...",
    "review_focus": ["..."],
    "known_concerns": ["..."]
  }
}
```

`intent_report`는 다음 attempt의 Coder 및 Reviewer 양쪽이 그대로 받는다. diff와 일치하지 않으면 Reviewer는 diff를 기준으로 판정한다.

**금지**:
- git commit/push/branch 전환 등 모든 git 쓰기 (WFC가 commit 전담)
- 서버 기동/패키지 설치 (Setup Agent 책임)
- subtask scope 밖 변경

### 2.4 알림 (`scripts/notification.py`, `scripts/telegram/formatter.py`)

신규 이벤트 등록:
- `replan_started` (color: YELLOW, label: "재계획 시작", icon: 🔄)

### 2.5 force_result fixture (`scripts/run_claude_agent.sh`)

`reviewer:approve`/`reviewer:reject` 강제 시나리오 JSON을 새 스키마에 맞춤 (E2E 테스트 호환 유지).

### 2.6 Summarizer 차단 버그 수정

증상: 마지막 subtask 완료 직후 Summarizer가 `subtask 개수 초과: 3/2`로 차단됨.

원인: `current_subtask`가 마지막 subtask ID로 남아 있는데 `completed_subtasks`에도 들어가서 `check_safety_limits.py:99`가 `completed + current = 2+1=3`으로 이중 집계.

조치 (양쪽 동시):
- `workflow_controller.py:finalize_task()` — Summarizer 호출 직전 `current_subtask=None` 설정
- `check_safety_limits.py` — `current_subtask`가 이미 `completed_subtasks`에 있으면 중복 집계 안 함 (방어적 fallback)

---

## 3. 검증 결과 — test-project task 00147

조건: subtask 2개 ("랜덤 배경 변경 버튼" + "흰색 클리어 버튼"), `max_subtask_count: 2` override.

타임라인 요약:
1. Planner → plan v1 (2 subtasks). Web에서 plan 승인.
2. **subtask 00147-1**:
   - attempt 1: Coder가 Dolphin 버튼 + 이벤트 핸들러 추가 (주석 2개 포함)
   - Reviewer rejected, `retry_mode=continue`, instruction = "주석 2개만 제거"
   - attempt 2: Coder가 continue 모드로 주석 2개만 제거 (기존 변경 보존)
   - Reviewer approved → WFC가 commit `[00147][tg:SeungKeol] 00147-1: ...` 생성, push 보류
3. **subtask 00147-2**:
   - attempt 1: Coder가 Polar Bear 버튼 + clearBackgroundImage() 추가
   - Reviewer approved (1-shot) → WFC commit 생성, push 보류
4. Summarizer 직전 `current_subtask=None`으로 클리어 → safety 통과
5. Summarizer 호출. 본 task에서는 실패해 fallback PR 메시지 사용 (별도 사후 점검 필요, 이번 핸드오프 범위 외)
6. **단 1회 push**: `git push --set-upstream origin feature/00147-animal-bg-buttons`
7. PR #42 생성 → `waiting_for_human_pr_approve` → 사용자 머지 후 `completed`

검증된 핵심 동작:
- `subtask_start_sha=bcc16257` / `a993d280` in-memory 추적 ✓
- `coder 실행 (attempt 2, mode=continue)` 로그 ✓
- `[git] 커밋 완료 (push 보류)` 메시지 ✓
- 한 task = 한 push 정책 ✓
- Reviewer 새 스키마 모든 필드 출력 ✓
- attempt_history에 coder_intent_report + reviewer_feedback 모두 누적 ✓

---

## 4. 이번 세션 커밋

```
b3f0be0 safety: 마지막 subtask 완료 후 Summarizer 차단 버그 수정
f66727e Coder/Reviewer retry 컨텍스트 재설계
```

main 대비 2 commits, 미푸시. 사용자 추가 검토 후 merge 예정.

---

## 5. 다음 세션 — 후속 과제

이번 세션 말미에 발견된 별개 이슈. 다음 세션의 주제로 옮긴다.

### 5.1 claude -p 세션 재사용 (없음)

`scripts/run_claude_agent.sh:645`이 매번 `--resume`/`--session-id` 플래그 없이 `claude -p`를 호출해 모든 agent 호출이 cold start. 같은 task의 같은 operation(예: coder)이 attempt 간 컨텍스트를 잃는다.

후보:
- A. project+agent 단위로 세션 ID 부여 → `--resume` 사용
- 주의: 세션이 길어지면 토큰/비용 누적, 잘못된 결정 고착화 위험
- 한 task 단위로만 살리고 task 종료 시 세션 종료가 적절할 듯

### 5.2 codebase CLAUDE.md 부재

`run_claude_agent.sh:641`에서 `cd "$CODEBASE_PATH"` 후 claude를 실행하므로 codebase에 CLAUDE.md가 있으면 자동 인식되지만, 현재 어느 codebase에도 CLAUDE.md가 없음 (test-web-service에는 README.md만).

후보:
- B. init-project 시 codebase에 CLAUDE.md 템플릿 자동 생성
- 매 PR merge 후 Summarizer가 갱신
- 위험: 한 번 잘못된 컨벤션을 박으면 전염

### 5.3 프로젝트별 진행 누적 메모 부재

`project_state.json`은 동적 상태(현 task/PR), `tasks/*.json`은 단일 task 산출물. "이 프로젝트는 X 패턴으로 가고 있다"는 장기 누적 메모가 없음.

후보:
- C. `projects/{name}/PROJECT_NOTES.md` 같은 누적 메모를 매 agent prompt에 inject
- 가벼운 시작점

권장 진행: **B + C 우선**, A는 세션 누수 관리 필요해 별도.

### 5.4 Summarizer 산발적 실패

task 00147 Summarizer 호출이 실패해 기본 PR 메시지로 fallback. 이번 리팩토링과 무관할 가능성이 높지만 로그 확인 필요. `projects/test-project/logs/00147/00147_99_08-summarizer.log` 점검.

---

## 6. 변경 파일 요약 (이번 세션)

```
config/agent_prompts/coder.md       (재작성, retry_mode-aware)
config/agent_prompts/reviewer.md    (재작성, 새 스키마)
scripts/workflow_controller.py      (+500/-144 — 헬퍼 + 파이프라인 재구성)
scripts/check_safety_limits.py      (current/completed 중복 집계 방어)
scripts/notification.py             (replan_started 이벤트)
scripts/telegram/formatter.py       (replan_started 이벤트)
scripts/run_claude_agent.sh         (force_result fixture 새 스키마)
docs/agent-system-spec-v07.md       (§4.3, §5.4/5.5, §8.1, §13.1, §15.1/15.3/15.4 갱신)
docs/task-lifecycle-fsm.md          (이미 git_reset 반영됨, 추가 수정 없음)
docs_for_claude/019-handoff-coder-reviewer-context-management.md  (본 문서)
```

---

## 7. 다음 세션 시작 시 참고

- 메모리 `project_reviewer_retry_mode_todo.md`는 본 세션에서 해소됨 → 정리 권장
- 메모리 `project_chatbot_execution_todo.md`는 별개 이슈로 잔존 (chatbot 상주 프로세스 검토 TODO)
- 본 브랜치 머지 후 다음 작업은 §5의 B/C부터 시작
