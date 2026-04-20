## Telegram Plan Review 상세 표시 + 수정 피드백 입력 핸드오프

> 작성: 2026-04-20
> 선행: `docs_for_claude/024-handoff-unify-user-request-intent.md`
> 브랜치: `feature/show-plan-in-telegram`
> 커밋: `16bf88c`

---

## 0. 한눈에 보기

Telegram Plan Review UX 2종 개선. **(1)** plan_review_requested / replan_review_requested 알림 본문에 strategy_note + subtask 목록(제목·책임)을 동봉해 Web 없이 Telegram만으로 plan 내용 확인이 가능하다. **(2)** "📝 수정" 버튼을 눌러도 더 이상 고정 문구로 즉시 reject되지 않고, force_reply 프롬프트를 띄워 다음 자연어 메시지를 피드백으로 받아 `hub_api.reject(message=...)`에 실어 replan Planner에 원문 그대로 전달한다.

사용자 확인:
- **plan review 표시**: 실제 task #00005에서 live 동작 확인 완료
- **수정 피드백 입력**: 코드 및 유닛 테스트만 작성, 엔드-투-엔드 live 검증 미실시

---

## 1. 구현 요약

### 1.1 Plan Summary 본문 포함 (①-a)

**데이터 생성** — `workflow_controller._build_plan_summary(plan_path)`:

```python
_PLAN_SUMMARY_MAX_TOTAL_CHARS = 3500          # Telegram 4096 한도 + 헤더 여유
_PLAN_SUMMARY_STRATEGY_NOTE_LIMIT = 400
_PLAN_SUMMARY_SUBTASK_TITLE_LIMIT = 120
_PLAN_SUMMARY_SUBTASK_RESPONSIBILITY_LIMIT = 200
```

- strategy_note는 400자로 말줄임(`…`)
- subtask 개별 항목은 title 120자, primary_responsibility 200자로 말줄임
- 누적 예산(3500자) 초과 시 꼬리 subtask들은 `truncated=True` 마커 1줄로 대체 (`"… 외 N개 더 (Web에서 전체 확인)"`)
- `total_subtasks`는 항상 원본 갯수 보존

반환 dict는 `request_human_review()`가 `details.plan_summary`로 notifications.json에 주입.

**렌더링** — `telegram/formatter._render_plan_summary_lines()`:

```
*전략 노트*
>(blockquote로 strategy_note)

*Subtasks \(N개\)*
*1\.* 제목 `subtask_id`
  ↳ primary_responsibility 본문
*2\.* ...
_… 외 N개 더 \(Web에서 전체 확인\)_    ← truncated marker는 italic
```

- MarkdownV2 특수문자 전부 escape
- 이벤트 타입 `plan_review_requested` / `replan_review_requested`에만 렌더 (다른 이벤트는 details.plan_summary가 있어도 무시)

### 1.2 "수정" 피드백 force_reply UX (②-a)

기존: `📝 수정` 버튼 콜백 → `hub_api.reject(message="Telegram 버튼으로 수정 요청")` 즉시 디스패치 (피드백 텍스트 없음)

신규: 콜백 수신 → force_reply 프롬프트 전송 → 다음 자연어 메시지 소비 → `hub_api.reject(message=<사용자가 입력한 피드백>)` 디스패치

**pending state** (파일 기반, 여러 topic 동시 지원):

```
data/telegram_pending_modify.json
{
  "pending": {
    "<chat_id>_<thread_id>": {
      "project": "...",
      "task_id": "...",
      "prompt_message_id": <int>,
      "requested_at": "<ISO8601 UTC>"
    }
  }
}
```

- TTL 10분 (`_PENDING_MODIFY_TTL_SECONDS = 600`). pop 시점에 만료된 항목은 자동 제거.
- 같은 topic에서 "수정"을 재차 누르면 덮어쓰기(사용자 의도가 바뀐 것).
- `threading.Lock`으로 read-modify-write 보호.

**라우팅 변경** — `telegram_bridge._handle_update()`:
- `natural_message`: 먼저 `_try_consume_modify_feedback()` → pending이 있으면 reject dispatch 후 조용히 종료. 없으면 기존 chatbot 경로로 폴백.
- `slash_command`: 먼저 pending을 pop해 취소 안내만 보낸 뒤 원래 슬래시 처리로 진행 (사용자가 명령어를 타이핑하면 "수정" 의사를 접은 것으로 본다).

### 1.3 버그 픽스 (relative plan_path anchoring)

구현 초기에 `request_human_review()`가 `plan_path = os.path.join("tasks", task_id, "plan.json")`로 상대경로를 넘긴 상태에서 `_build_plan_summary()`가 WFC cwd 기준으로 `os.path.exists()`를 호출해 False → `plan_summary=None` 반환 → 알림에 요약이 전혀 실리지 않는 실버그가 있었다.

WFC cwd는 codebase 경로로 이미 이동된 상태이므로, plan_path가 상대경로이면 `project_dir` 기준으로 절대화한 뒤 helper에 전달한다:

```python
absolute_plan_path = plan_path
if absolute_plan_path and not os.path.isabs(absolute_plan_path):
    absolute_plan_path = os.path.join(resolved_project_dir, absolute_plan_path)
plan_summary = _build_plan_summary(absolute_plan_path)
```

재알림(already-processed human_interaction 재발송) 분기에도 동일 패치. 유닛 테스트는 tmp_path의 절대경로를 썼기 때문에 이 버그를 잡지 못했다 — live smoke로만 드러났다.

---

## 2. 수정 파일

| 파일 | 변경 내용 |
|------|-----------|
| `scripts/workflow_controller.py` | `_build_plan_summary()` + 상수 4종 추가. `request_human_review()`와 재알림 분기에서 `plan_path` 절대화 후 `details.plan_summary` 주입 |
| `scripts/telegram/formatter.py` | plan_summary 블록 렌더 로직(`_render_plan_summary_lines`) + plan_review 이벤트에서만 호출. MarkdownV2 escape 준수 |
| `scripts/telegram_bridge.py` | `data/telegram_pending_modify.json` pending state(TTL 10m) + `_set_pending_modify`/`_pop_pending_modify`/`_pending_modify_key` 헬퍼 + `_prompt_modify_feedback` + `_try_consume_modify_feedback` + `_handle_update` 라우팅 변경 + `_handle_callback_query` reject_modify 분기 교체 |
| `tests/test_telegram_formatter.py` | plan_summary 렌더 4종 추가(strategy+subtasks, truncated marker, non-review 무시, 하위호환 fallback) |
| `tests/test_wfc_plan_summary.py` | 신규 7종(`_build_plan_summary` 경계 케이스) |
| `tests/test_telegram_bridge_pending_modify.py` | 신규 7종(enabled=false 브릿지로 파일 I/O만 검증: roundtrip, missing key, TTL 만료, overwrite, None thread 정규화, topic 독립성, TTL 상수 positivity) |

변경 규모: 6파일, +640 / -6 (도 감사 수치는 `git show --stat 16bf88c` 참조)

---

## 3. 동작 흐름

### 3.1 Plan Review 수신

```
WFC run_pipeline()
  → Planner 실행 후 plan.json 저장
  → request_human_review("plan_review", ...)
       plan_path = relative "tasks/00042/plan.json"
       → project_dir 기준으로 absolute 화
       → _build_plan_summary(absolute) → dict
       → details = {plan_path, plan_summary: {...}}
       → notifications.json append

telegram_bridge (poller)
  → notification 읽음
  → format_notification():
       헤더 🟡 + "Plan을 확인해주세요. subtask N개 생성됨."
       + 전략 노트 blockquote
       + Subtasks (N개) 리스트 (제목 + ↳책임)
  → reply_markup_for_notification():
       [✅ 승인] [📝 수정] [❌ 취소]
  → send_message(chat_id, thread_id, text, reply_markup)
```

### 3.2 수정 피드백 입력

```
사용자: "📝 수정" 버튼 탭
  → callback_query: reject_modify:my-app:00042
  → _handle_callback_query → _prompt_modify_feedback()
     send_message(force_reply=True, "task 00042 수정 요청 내용을 알려주세요…")
     prompt_message_id 획득
     _set_pending_modify(chat_id, thread_id, project, task_id, prompt_message_id)

사용자: (force_reply에 답장으로) "subtask 2개를 1개로 합쳐줘"
  → natural_message
  → _try_consume_modify_feedback():
       pending 있음 → pop
       hub_api.reject(project, task_id, message="subtask 2개를 1개로 합쳐줘")
       send_message("📝 수정 요청을 전달했습니다. 재계획이 진행됩니다.")
       return True (chatbot 경로로 폴백하지 않음)

WFC
  → plan_review modify 분기 → Planner 재실행
  → 새 plan → replan_review_requested 알림 → (3.1 사이클 재개)
```

### 3.3 슬래시 명령으로 취소 경로

```
사용자: "📝 수정" 탭 → force_reply 프롬프트 뜸
사용자: (답장 대신) "/list" 입력
  → slash_command
  → _handle_update: pop_pending → 기존 대기분 취소
     send_message("ℹ️ 대기 중이던 수정 요청이 취소되었습니다 (슬래시 명령 수신)…")
  → 이어서 /list 정상 처리
```

---

## 4. 테스트 / 검증

### 4.1 실행

```
./run_test.sh unit          # 160개 (formatter +4, wfc +7, pending +7, 기존 142)
./run_test.sh integration   # 107개 (무수정, regression 없음)
```

### 4.2 Live 검증 결과

| 경로 | 결과 |
|------|------|
| plan_review 상세 표시 | ✅ 실제 task #00005, test-web-service로 확인 ("plan review는 만족스럽게 작동") |
| relative plan_path 버그 | ✅ fix 후 재기동하여 plan_summary 정상 주입 확인 |
| "📝 수정" 버튼 → force_reply → 피드백 dispatch | ⚠ **미검증**. 유닛 테스트만 통과. replan Planner까지의 실제 라운드트립은 다음 세션에서 확인 필요 |

### 4.3 다음 세션 live 검증 체크리스트 (수정 피드백)

1. `./run_system.sh start` 후 Telegram bind된 프로젝트에 task 제출
2. plan_review 카드 수신 → "📝 수정" 탭
3. force_reply 프롬프트 UI 정상 표시 여부 (`input_field_placeholder` 포함, selective 적용)
4. 프롬프트에 답장(reply)으로 "subtask 2개를 1개로 묶어줘" 등 피드백 입력
5. ✅ confirmation 메시지 수신 ("📝 수정 요청을 전달했습니다…")
6. WFC 로그에서 `hub_api.reject(message="...")` → `plan_review modify 분기 → Planner 재실행` 확인
7. replan_review_requested 카드에 새 plan_summary가 정상 렌더되는지 확인
8. TTL 경계: 11분 후 피드백 입력 시 pending이 소멸되어 chatbot 경로로 폴백되는지
9. 슬래시 취소: 프롬프트 뜬 상태에서 `/pending` 입력 시 취소 안내 + /pending 정상 실행

---

## 5. 알려진 제한 / 후속 과제

1. **수정 피드백 live 검증 미완**. 위 4.3 체크리스트 수행 필요.
2. **force_reply는 클라이언트/그룹에 따라 동작이 다름.** topic이 있는 supergroup에서 `selective=true`와 `message_thread_id` 동시 사용 시 일부 버전 Telegram 앱이 답장 지정을 놓칠 수 있다 — 문제 발생 시 `reply_to_message_id`를 pending과 묶어 추가하는 방향 고려.
3. **pending state 파일 잠금은 프로세스 내 `threading.Lock`만 사용.** 단일 telegram_bridge 프로세스 가정이라 괜찮지만, 향후 멀티 인스턴스가 되면 `fcntl.flock`으로 전환 필요.
4. **plan_summary 예산은 고정**. 매우 긴 strategy_note + subtask 50개 이상 케이스에서 정보 손실이 크다. 테스트는 커버하지만 실제 UX 만족도는 4096 한도 안에서 타협한 값이라, Web 상세 링크를 함께 싣는 개선 여지가 있다 (현재는 "Web에서 전체 확인" 문구만).
5. **re-broadcast 경로 점검 미완**. 재알림(poller가 이미 처리된 human_interaction을 다시 브로드캐스트) 분기에서 절대경로 fix는 적용했지만 live에서 이 경로를 직접 밟아보진 않았다.

---

## 6. 커밋 상태

커밋 완료 — `16bf88c feat: Telegram plan review에 상세 plan 표시 + 수정 피드백 입력` (브랜치 `feature/show-plan-in-telegram`). 아직 main 머지 전.

PR 생성 여부는 사용자 지시 대기.
