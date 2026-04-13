# Phase 2.0 핸드오프 — Web Chat + PR 비동기 처리 + PID 네이밍 통일

> 작성: 2026-04-08
> 기준 문서: `docs/agent-system-spec-v07.md`
> 브랜치: `main`

---

## 현재 상태 요약

- **Phase 2.1+까지 완료:** merge_strategy 3종, PR 직접 머지/닫기, Web 버튼 4종 체계
- **Phase 2.0 (잔여) 완료:** Web Chat 실시간 양방향 채팅, PR 비동기 처리, PID 네이밍 통일

---

## 구현 완료 항목

### 1. Web Chat — 실시간 양방향 채팅

#### 1.1 ChatProcessor 엔진 (`scripts/web/web_chatbot.py`)

Transport-agnostic 채팅 엔진. Web/Slack/Telegram 공용 설계.

```
사용자 메시지 → submit_message() → 백그라운드 스레드
  → Popen("claude -p ...") → parse_claude_response()
  → intent 분기 (conversation/action/clarification)
  → on_message 콜백으로 SSE 이벤트 전달
```

**핵심 기능:**

| 기능 | 설명 |
|------|------|
| **cancel+merge** | 처리 중 새 메시지 도착 시 Popen.kill() → 메시지 합쳐서 재실행 |
| **확인 흐름** | 고위험 action(submit, approve 등)은 확인 카드 → "확인"/"취소" 텍스트로 응답 |
| **시스템 이벤트 주입** | notification 이벤트를 chat 메시지로 자동 push (plan 승인 요청, task 완료 등) |
| **세션 관리** | `session_history/web/{session_id}.json`에 저장, 페이지 새로고침 시 복원 |

**상태 머신:**

```
idle ──(메시지)──→ processing ──(응답)──→ idle
                      │                     ↑
                      │ (확인 필요)          │ (확인/취소)
                      └──→ awaiting_confirmation ──┘
                      
processing + 새 메시지 → kill + merge → processing (재시작)
```

**chatbot.py에서 import하여 재사용:**
- `build_system_prompt`, `parse_claude_response`, `needs_confirmation`
- `generate_session_id`, `save_session`, `load_session`, `list_sessions`
- `HIGH_RISK_ACTIONS`, `READ_ONLY_ACTIONS`, `LOW_RISK_ACTIONS`

**별도 구현 (Popen 기반):**
- `_call_claude_popen()` — subprocess.Popen + communicate(timeout=600)
- `_format_confirmation_plain()` — ANSI 코드 없는 확인 메시지
- `_format_response_plain()` — ANSI 코드 없는 결과 포맷팅
- `_format_system_event()` — notification 이벤트를 chat 텍스트로 변환

#### 1.2 세션 레지스트리

```python
_active_sessions: dict[str, ChatProcessor] = {}

get_or_create_session(root, session_id, on_message) → ChatProcessor
broadcast_system_event(event)  # 모든 활성 세션에 이벤트 주입
remove_session(session_id)
```

#### 1.3 API 엔드포인트 (`scripts/web/server.py`)

| Endpoint | Method | 설명 |
|----------|--------|------|
| `/api/chat/session` | POST | 세션 생성/복원. `{session_id?}` → `{session_id, history}` |
| `/api/chat/send` | POST | 메시지 전송 (fire-and-forget). 응답은 SSE로 전달 |
| `/api/chat/sessions` | GET | 웹 채팅 세션 목록 |
| `/api/chat/history/{session_id}` | GET | 세션 히스토리 조회 |

#### 1.4 SSE 이벤트 타입

| 이벤트 | payload | 설명 |
|--------|---------|------|
| `chat_message` | `{session_id, role, content, confirmation, action_details?, timestamp}` | 채팅 메시지 |
| `chat_typing` | `{session_id, active}` | typing indicator |

#### 1.5 프론트엔드 (`scripts/web/static/app.js`)

- **세션 관리:** `localStorage`에 `chat_session_id` 저장, 페이지 로드 시 복원
- **fire-and-forget 전송:** POST 후 즉시 반환, 응답은 SSE `chat_message`로 수신
- **확인 카드:** action/project/params를 보여주는 카드 + Confirm/Cancel 버튼 (텍스트 "확인"/"취소" 전송)
- **typing indicator:** 점 3개 펄스 애니메이션
- **system 메시지:** italic, centered, 알림 스타일
- **New Session 버튼:** Chat 영역 상단

#### 1.6 notification → chat 연동

```python
# server.py _on_change()
if event.get("type") == "notification":
    broadcast_system_event(event)  # 모든 활성 chat 세션에 주입
```

### 2. PR 비동기 처리 (merge_pr / close_pr)

기존 동기 처리를 비동기로 전환:

```
[기존] 버튼 클릭 → 서버(gh pr merge, 수 초) → 응답 → UI 갱신
[변경] 버튼 클릭 → 서버 즉시 accepted → UI "Processing PR..." → 백그라운드 gh 실행 → SSE → UI 갱신
```

- 서버: `_run_pr_action_background()` 백그라운드 스레드, 중복 방지(`_pr_processing` dict)
- 프론트: `setPrProcessing()` → "Processing PR..." 펄스 애니메이션, 실패 시 `showPrError()` 버튼 복원 + 에러 메시지

### 3. PID 네이밍 통일

| 기존 | 변경 |
|------|------|
| `.pids/web_console.pid` (고정 파일명, PID를 파일 내용에 저장) | `.pids/web_console_chat.{PID}.pid` (TM과 동일 패턴) |
| `logs/web_console.log` | `logs/web_console_chat.log` |

TM과 동일한 헬퍼 함수 패턴: `find_web_pid_file()`, `read_web_pid()`, `is_web_running()`

---

## 변경 파일 전체 목록

| 파일 | 변경 유형 | 설명 |
|------|-----------|------|
| `scripts/web/web_chatbot.py` | **신규** | ChatProcessor 엔진 (652줄) |
| `scripts/web/server.py` | 수정 | Chat API 4개 + PR 비동기 처리 + notification→chat 연동 |
| `scripts/web/static/app.js` | 수정 | Chat UI 전면 재작성 + PR 비동기 UI |
| `scripts/web/static/style.css` | 수정 | chat system/typing/confirmation + PR processing/error 스타일 |
| `scripts/web/templates/index.html` | 수정 | Chat 영역에 New Session 버튼 추가 |
| `run_system.sh` | 수정 | PID 패턴 변경 (`web_console_chat.{PID}.pid`), 헬퍼 함수 추가 |
| `docs/agent-system-spec-v07.md` | 수정 | PID/로그 경로 업데이트, Web Chat 구현 상태 반영 |
| `tests/test_web_chatbot.py` | **신규** | 18개 테스트 (유틸, 상태 전이, cancel+merge, confirmation, 세션 레지스트리, 이벤트 주입, typing) |

---

## 테스트

- **전체 260개 통과** (`./run_test.sh all`)
- 신규 18개: TestUtils(5), TestChatProcessorStates(4), TestConfirmationFlow(3), TestSessionRegistry(4), TestInjectSystemEvent(1), TestTypingIndicator(1)
- subprocess.Popen을 mock하여 claude -p 호출 검증

---

## 코드 진입점

| 파일 | 용도 |
|------|------|
| `scripts/web/web_chatbot.py` `ChatProcessor` | 핵심 채팅 엔진 |
| `scripts/web/web_chatbot.py` `get_or_create_session()` | 세션 생성/복원 |
| `scripts/web/web_chatbot.py` `broadcast_system_event()` | notification → chat 브로드캐스트 |
| `scripts/web/server.py` `api_chat_send()` | 메시지 전송 엔드포인트 |
| `scripts/web/server.py` `_run_pr_action_background()` | PR 비동기 처리 |
| `scripts/web/static/app.js` `sendChat()` | 프론트 메시지 전송 |
| `scripts/web/static/app.js` `connectSSE()` → `chat_message`/`chat_typing` | SSE 수신 |

---

## 다음 Phase 후보

| Phase | 내용 | 상태 |
|-------|------|------|
| 2.0 (잔여) | Web 오류/사용성 개선 (필터, 표시 등) | **다음** |
| 2.2 | 고급 기능: Pipeline resume, user_preferences, Merge conflict 등 | 미착수 |
| 2.3 | Messenger (Slack/Telegram) — ChatProcessor 재사용 | 미착수 |
| 2.4 | E2E 테스트장비 연동, 로컬 E2E | 미착수 |
