## 사용자 요청 의도 단계 간 통합 핸드오프

> 작성: 2026-04-20
> 선행: `docs_for_claude/023-handoff-e2e-final-cleanup-and-merge.md`
> 브랜치: `feature/unify-user-request-intent-between-stak-process`

---

## 0. 한눈에 보기

사용자 지시 prompt가 파이프라인 각 단계에서 재해석되며 의도가 유실되는 구조적 문제 3종을 해소했다. task 스키마에 `user_request_raw` 필드를 추가해 원문을 보존하고, subtask 실행 시점에 `plan_position`/`prior_changes` 두 필드를 항상 주입해 Coder/Reviewer가 "전체 중 내 위치"와 "앞 단계에서 이미 이뤄진 일"을 인지한 상태로 움직이도록 만들었다.

**수정한 문제 3종:**
1. chatbot/Web이 자연어 요청을 title/description으로 재해석하며 원문 손실
2. Planner가 재해석된 title/description을 다시 subtask로 재해석
3. 각 subtask가 전체 plan 내 자신의 위치와 앞 단계 산출물을 모르는 채 독립 실행

---

## 1. 구현 요약

### 1.1 `user_request_raw` 필드 — 원문 보존 경로

| 레이어 | 파일 | 역할 |
|--------|------|------|
| 클라이언트 | `scripts/chatbot.py` | submit/resubmit intent 직전 사용자 raw input을 params에 주입 |
| 클라이언트 | `scripts/web/web_chatbot.py` | Web chat 경로에서 merged 문자열을 params에 주입 (confirmation/즉시실행 공통) |
| Protocol | `scripts/hub_api/protocol.py` | `_handle_submit`이 `user_request_raw`를 core.submit에 전달 |
| Core | `scripts/hub_api/core.py` | submit() 시그니처에 인자 추가, task_data에 필드 저장. resubmit()도 원본에서 복사 |
| Agent 기동 | `scripts/run_claude_agent.sh` | task JSON에서 읽어 role 프롬프트 바로 뒤에 "사용자 원문 요청 (최우선 근거)" 섹션 삽입 |

CLI `submit` 경로는 title/description을 사용자가 직접 입력하므로 `user_request_raw=None`. 이 경우 프롬프트 섹션 자체가 출력되지 않고 title/description이 원문 역할을 대신한다.

### 1.2 `plan_position` — 전체 중 내 위치 맥락

WFC가 subtask loop 시작 시점에 주입하는 dict:

```json
{
  "index": 1,
  "total": 3,
  "strategy_note": "plan.json에서 추출",
  "siblings": [
    { "subtask_id": "...", "title": "...", "primary_responsibility": "...",
      "status": "completed|current|upcoming" }
  ]
}
```

`status`는 실제 `completed_subtasks` 집합 기준이라 re-plan으로 목록이 초기화돼도 정확히 반영된다.

### 1.3 `prior_changes` — 앞 단계 산출물 round-trip

- **저장:** Reviewer approved 브랜치에서 `_inject_subtask_runtime_fields(subtask_file, final_changes_made=..., final_intent_report=...)` 호출 → subtask JSON의 `final_*` 필드로 승격
- **수집:** 다음 subtask 시작 시 `_collect_prior_changes()`가 `completed_subtask_ids`를 순회하며 각 subtask 파일의 `final_*`을 읽어 배열 생성
- **주입:** `_inject_subtask_overall_context()`가 `plan_position`과 함께 subtask JSON에 기록

approved 없이 종료된 subtask는 completed 목록에 없으므로 수집 대상이 아니며, 재시도 중인 임시값이 섞이지 않는다.

### 1.4 주입 헬퍼 (workflow_controller.py)

```
_inject_subtask_overall_context()
  ├─ _load_plan_strategy_note(project_dir, task_id)
  ├─ _collect_prior_changes(project_dir, task_id, completed_subtask_ids)
  ├─ _build_plan_position(current_index, all_subtasks, strategy_note, completed_subtask_ids)
  └─ _inject_subtask_runtime_fields(subtask_file, prior_changes=..., plan_position=...)
```

3곳에서 호출: `run_pipeline`, `run_pipeline_resume`, `run_pipeline_from_subtasks` (신규/재개/re-plan 모두 커버).

### 1.5 Agent 프롬프트 업데이트

| 프롬프트 | 추가 내용 |
|----------|-----------|
| `planner.md` | "요구사항 해석 우선순위" 섹션 — 원문이 최상위, title/description은 2차. strategy_note에 원문 핵심 의도 1~2줄 요약 지시 |
| `coder.md` | "전체 맥락 필드" 섹션 — plan_position/prior_changes 읽기 의무 + scope 침범 금지. "요구사항 해석 우선순위"로 원문 최우선 |
| `reviewer.md` | 입력 맥락에 plan_position/prior_changes 추가. "이 지적이 내 scope인가 upcoming의 일인가" 판정 근거. 원문 의도 vs subtask guidance 충돌 시 원문 우선 |

---

## 2. 수정 파일

| 파일 | 변경 내용 |
|------|-----------|
| `scripts/hub_api/core.py` | `submit()`에 `user_request_raw` 인자 추가, task_data 저장, resubmit 복사 |
| `scripts/hub_api/protocol.py` | `_handle_submit`이 params에서 읽어 core로 전달 |
| `scripts/chatbot.py` | submit/resubmit action 직전 user_input → params.user_request_raw |
| `scripts/web/web_chatbot.py` | Web 경로에서 merged → params.user_request_raw (confirmation/즉시 공통) |
| `scripts/run_claude_agent.sh` | 프롬프트 상단에 "사용자 원문 요청 (최우선 근거)" 섹션 삽입 |
| `scripts/workflow_controller.py` | 4개 헬퍼(`_load_plan_strategy_note`, `_collect_prior_changes`, `_build_plan_position`, `_inject_subtask_overall_context`) + 주입 지점 3곳 + approved 시점 `final_*` 승격 |
| `config/agent_prompts/planner.md` | 요구사항 해석 우선순위 섹션 |
| `config/agent_prompts/coder.md` | 전체 맥락 필드 + 해석 우선순위 섹션 |
| `config/agent_prompts/reviewer.md` | 입력 맥락에 plan_position/prior_changes + 원문 우선 판정 규칙 |
| `docs/agent-system-spec-v07.md` | §5.3 컨텍스트 전달, §8.1 Task 스키마(user_request_raw), §8.4 런타임 컨텍스트(전체 맥락 필드 B) |

변경 규모 (문서 제외): 9파일, +233줄 / -2줄

---

## 3. 동작 흐름 (예시)

사용자가 chatbot에 "로그인 페이지 만들어줘. 이메일+비번으로 하고 실패 시 빨간 메시지"라고 입력:

```
chatbot.py (또는 web_chatbot.py)
  intent = "action", action = "submit"
  → params.user_request_raw = "로그인 페이지 만들어줘. ..."  ← 원문
  → params.title = "로그인 페이지 구현"                      ← LLM 재해석
  → params.description = "이메일/비밀번호 인증과 ..."         ← LLM 재해석

hub_api.submit(user_request_raw="로그인 페이지 만들어줘. ...", title=..., description=...)
  → task JSON에 user_request_raw 저장

Planner 실행
  run_claude_agent.sh가 프롬프트 상단에 원문 섹션 주입
  → Planner는 title 대신 원문에서 "빨간 에러 메시지 요구"를 읽어냄
  → strategy_note에 "원문의 핵심 의도: ..., 3개 subtask로 분할" 기록

Subtask 00042-1 실행
  WFC: _inject_subtask_overall_context()
    → plan_position = {index:0, total:3, strategy_note, siblings:[*status*]}
    → prior_changes = []                              (첫 subtask)
  Coder 프롬프트 = 원문 + subtask context (plan_position+prior_changes 포함)

Subtask 00042-1 Reviewer approved
  WFC: final_changes_made/final_intent_report 승격 저장

Subtask 00042-2 실행
  WFC: _inject_subtask_overall_context()
    → plan_position = {index:1, ..., siblings[0].status="completed"}
    → prior_changes = [{00042-1의 final_*}]
  Coder는 "00042-1이 이미 만든 것 위에 쌓는다"를 인지
```

---

## 4. 테스트 / 검증 가이드

아직 실행 검증은 하지 않았다. 다음 세션에서 아래 순서로 확인 권장:

1. **유닛/통합 테스트 — 스키마 호환성 확인**
   ```
   ./run_test.sh unit
   ./run_test.sh integration
   ```
   core.submit 시그니처 변경으로 기존 호출부가 깨지지 않는지 (기본값 None이라 문제 없어야 함).

2. **CLI submit (user_request_raw=None 경로)**
   ```
   ./run_agent.sh submit --project <p> --title "..." --description "..."
   → 생성된 task JSON에 "user_request_raw": null 확인
   → run_claude_agent.sh --dry-run으로 프롬프트에 "사용자 원문 요청" 섹션이 나타나지 않는지 확인
   ```

3. **chatbot submit (user_request_raw 보존 경로)**
   ```
   ./run_agent.sh chat
   > 아무 자연어 요청 입력
   → task JSON에 원문이 그대로 저장되는지
   → dry-run으로 프롬프트 최상단에 섹션이 주입되는지
   ```

4. **multi-subtask 작업으로 plan_position/prior_changes 주입 확인**
   ```
   project_state.json으로 dummy 모드 + 2~3개 subtask task 제출
   → projects/<p>/tasks/<id>/subtask-01.json 에 plan_position/prior_changes 필드가 들어갔는지
   → subtask-02.json의 prior_changes에 subtask-01의 final_*가 복사되는지
   ```

5. **E2E 시나리오**
   실제 chatbot → Planner → Coder 1회 돌려서 Coder 프롬프트에 의도가 유실되지 않고 원문이 전달되는지 육안 확인. Claude Code 사용량 소모가 있으므로 필요 시에만.

---

## 5. 알려진 제한 / 후속 과제

1. **Planner 재해석 여전함.** 원문을 최상위 근거로 "명시"만 했지, Planner가 원문을 무시하고 title만 쓰면 여전히 왜곡 가능. 프롬프트 지시에 의존하는 구조이므로 verification 단계(사용자 plan review)에서 확인 필요.
2. **prior_changes 크기.** subtask가 많거나 changes_made가 커지면 context 폭증 가능. 현재는 full dump. 추후 요약/클리핑 정책 필요할 수 있음.
3. **기존 미완 task 호환성.** `user_request_raw` 없는 기존 task들은 섹션이 안 보일 뿐 동작에는 지장 없음. 마이그레이션 불필요.
4. **resubmit 시 원문 복사.** 원본 task의 user_request_raw를 그대로 복사한다. resubmit이 "원문 수정" 시나리오라면 별도 처리 필요하지만 현재 UX상 그런 경우 없음.

---

## 6. 커밋 상태

미커밋. 사용자가 테스트 완료 후 커밋 지시 예정.

```
변경 파일 9개 (docs 포함 시 10):
  config/agent_prompts/coder.md
  config/agent_prompts/planner.md
  config/agent_prompts/reviewer.md
  docs/agent-system-spec-v07.md
  scripts/chatbot.py
  scripts/hub_api/core.py
  scripts/hub_api/protocol.py
  scripts/run_claude_agent.sh
  scripts/web/web_chatbot.py
  scripts/workflow_controller.py
```
