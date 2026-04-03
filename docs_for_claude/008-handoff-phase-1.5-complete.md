# Phase 1.5 완료 핸드오프 — Chatbot 대화형 인터페이스

> 작성: 2026-04-03
> 기준 문서: `docs_for_claude/003-agent-system-spec-v4.md`

---

## 현재 상태 요약

- **Phase 1.0 완료:** 수동 pipeline + git 자동화
- **TM Phase 완료:** Task Manager, CLI (11개 서브커맨드), hub_api, human review, task 큐 블로킹, 4계층 config merge
- **Phase 1.4 완료:** 알림 시스템, Usage check (3계층 threshold), 재알림, 테스트 스위트 (85개)
- **Phase 1.5 완료:** Chatbot 레이어, Protocol, 세션 관리 (140개 테스트)
- **브랜치:** `feature/phase_1_5_user_interface_01`

---

## Phase 1.5 구현 완료 항목

### 1. Protocol Layer (Request/Response Envelope)

모든 프론트엔드(CLI, Chatbot, 향후 Web/Messenger)가 공통으로 사용하는 통신 규격.

**파일:** `scripts/hub_api/protocol.py` (신규)

- `Request` / `Response` dataclass envelope
- `ErrorCode` 상수: INVALID_ACTION, MISSING_PARAM, INVALID_PARAM, PROJECT_NOT_FOUND, TASK_NOT_FOUND, INVALID_STATE, INTERNAL_ERROR
- `ACTION_REGISTRY`: 14개 action 정의 (handler, description, required/optional params, requires_project)
- `dispatch(api, request) -> Response`: 단일 진입점, 에러 자동 변환
- `get_action_descriptions()`: Chatbot 시스템 프롬프트용 action 설명 텍스트 생성
- `_resolve_attachments()`: base64 → 임시 파일 변환

**HubAPI 보강:**
- `get_task()`: 단건 task dict 조회
- `mark_notification_read()`: 알림 읽음 처리
- `submit()`: `source` 파라미터 추가 (제출 경로 기록)

### 2. Chatbot Layer + Confirmation 메커니즘

자연어 입력 → Claude 해석 → 확인 → dispatch → 결과 표시.

**파일:** `scripts/chatbot.py` (신규)

**Action 분류:**
- `READ_ONLY_ACTIONS`: list, get_task, pending, status, notifications — 항상 즉시 실행
- `HIGH_RISK_ACTIONS`: submit, approve, reject, cancel, config — smart 모드에서 확인 필요
- `LOW_RISK_ACTIONS`: feedback, mark_notification_read, pause, resume — smart 모드에서 즉시 실행

**Confirmation 3-mode:**
- `always_confirm`: 모든 실행성 action에서 확인 요청
- `never_confirm`: 확인 없이 즉시 실행
- `smart` (기본): HIGH_RISK만 확인, LOW_RISK는 즉시

**Claude CLI 연동:**
- `claude -p --model sonnet`으로 자연어 해석 (매번 새 프로세스, stateless)
- 시스템 프롬프트: action 목록 + 프로젝트 + 상태 + config_override 스키마
- 대화 이력은 system prompt에 텍스트로 포함
- 5턴마다 system prompt 재생성 (최신 상태 반영)
- intent 보정: Claude가 `"intent": "approve"` 같이 action명을 intent에 넣는 경우 자동 교정

**결과 포맷팅:**
- action별 상세 표시 (list: 테이블, get_task: 진행상황+human_interaction, pending: 응답 힌트 등)

### 3. Session Management

세션 이력 저장/로드/재개.

**저장 경로:** `session_history/{frontend}/{session_id}.json`
- 예: `session_history/chatbot/20260403_143052_a3f1.json`

**session_id 형식:** `YYYYMMDD_HHMMSS_xxxx` (타임스탬프 + 랜덤 4자)

**세션 파일 구조:**
```json
{
  "session_id": "20260403_143052_a3f1",
  "frontend": "chatbot",
  "created_at": "2026-04-03T14:30:52",
  "updated_at": "2026-04-03T15:20:10",
  "turn_count": 15,
  "history": [...]
}
```

**기능:**
- 매 턴마다 자동 저장
- `--session <id>`로 기존 세션 재개
- `--list-sessions`로 세션 목록 조회
- `frontend` 파라미터로 chatbot/slack/web 분리
- MAX_HISTORY_TURNS: 100 (autocompact이 안전망)

### 4. 설정 추가

**config.yaml.template chatbot 섹션:**
```yaml
chatbot:
  model: "sonnet"
  confirmation_mode: "smart"
```

**run_agent.sh:**
- `chat` 서브커맨드 추가
- `./run_agent.sh chat [--confirmation-mode always_confirm] [--session <id>] [--list-sessions]`

### 5. 폴더 정리

- `templates/` 디렉토리 생성, `config.yaml.template` + `project.yaml.template` 이동
- `session_history/` gitignored

---

## 해결한 버그

| 버그 | 원인 | 수정 |
|------|------|------|
| TM 큐 블로킹 | `pending_review`가 `incomplete_statuses`에 포함 → auto_merge=false 시 후속 task 차단 | `pending_review` 제거 |
| config_override 스키마 오류 | Claude가 `{"auto_approve": true}` 같은 임의 키 생성 | system prompt에 config_override 스키마 + 패턴 추가 |
| intent 파싱 오류 | Claude가 `"intent": "approve"` 반환 (정상: `"intent": "action"`) | intent 보정 로직 추가 (action명이면 "action"으로 교정) |

---

## 테스트

- **총 140개** (기존 85 → 131 → 140)
- chatbot 테스트 55개: parse, confirmation, format, config, action 분류, protocol dispatch, HubAPI 보강, Response 직렬화, action descriptions, 세션 관리
- `./run_test.sh all` 또는 `./run_test.sh unit`

---

## 아키텍처 결정 사항 (향후 참고)

### Chatbot 실행 모델
- 현재: `claude -p`를 매 메시지마다 새로 호출하고 종료 (cold start ~16.8k 토큰 오버헤드)
- 향후 옵션: `claude --resume`, API 직접 호출, 상주 프로세스
- Phase 2.0+에서 성능이 문제될 때 검토

### Scope 파라미터 (Phase 2.0)
- ChatBot init에 `scope="global"|"project"` 추가 필요
- `scope="global"`: 전체 시스템 제어 (CLI, 웹 글로벌 채널, 메신저 글로벌 채널)
- `scope="project"`: 프로젝트 내 action만 허용 (웹 프로젝트별 채널, 메신저 프로젝트별 채널)
- strict하게 차단할지, 경고만 할지는 열어둠

### Web/Messenger 채널 구조 (Phase 2.1/2.2)
- **Web**: 글로벌 챗봇 (프로젝트 생성, TM 제어) + 프로젝트별 챗봇 (task 관리, 결과 확인)
- **메신저**: 글로벌 채널 (프로젝트/채널 생성) + 프로젝트별 채널 (submit, feedback)
- 메신저가 실시간 feedback에 더 적합 (바로바로 확인 가능)
- Web은 WebSocket 없으면 feedback 요청 처리가 어색할 수 있음

---

## Phase 로드맵 (업데이트)

| Phase | 목표 | 상태 |
|-------|------|------|
| **1.0** | 수동 pipeline + git 자동화 | **완료** |
| **TM** | Task Manager + CLI + hub_api + human review + 큐 블로킹 | **완료** |
| **1.4** | 운영 안정화: 알림, Usage check, 재알림, 테스트 스위트 | **완료** |
| **1.5** | Chatbot 대화형 인터페이스 + Protocol + 세션 관리 | **완료** |
| 1 잔여 | E2E 테스트장비 연동 | 예정 |
| 2.0 | scope 파라미터 + chatbot 실행 모델 개선 (공통 기반) | 예정 |
| 2.1 | Web monitor & chat | 예정 |
| 2.2 | Messenger (Slack/Telegram) | 예정 |

---

## 코드 진입점

| 파일 | 용도 |
|------|------|
| `scripts/chatbot.py` | Chatbot 메인: ChatBot 클래스, Claude CLI 연동, 세션 관리 |
| `scripts/hub_api/protocol.py` | Protocol layer: Request/Response, dispatch, ACTION_REGISTRY |
| `scripts/hub_api/core.py` | HubAPI 백엔드 (get_task, mark_notification_read 추가) |
| `scripts/hub_api/__init__.py` | 패키지 exports (Request, Response, ErrorCode, dispatch 등) |
| `templates/config.yaml.template` | chatbot 섹션 (model, confirmation_mode) |
| `run_agent.sh` | chat 서브커맨드 |
| `tests/test_chatbot.py` | 55개 테스트 (parse, confirmation, session 등) |
