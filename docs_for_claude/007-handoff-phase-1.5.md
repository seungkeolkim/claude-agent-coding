# Phase 1.5 핸드오프 — Chatbot + 메신저 연동

> 작성: 2026-04-03
> 기준 문서: `docs_for_claude/003-agent-system-spec-v4.md`

---

## 현재 상태 요약

- **Phase 1.0 완료:** 수동 pipeline + git 자동화
- **TM Phase 완료:** Task Manager, CLI (11개 서브커맨드), hub_api, human review, task 큐 블로킹, 4계층 config merge
- **Phase 1.4 완료:** 알림 시스템, Usage check (3계층 threshold), 재알림, 테스트 스위트 (85개)
- **브랜치:** `feature/phase-1.4-notification` (PR 후 main 머지 예정)

---

## Phase 1.5 목표

현재 시스템은 CLI 명령어(`./run_agent.sh submit/approve/list` 등)로만 조작 가능하다.
Phase 1.5에서는 **대화형 인터페이스(Chatbot)**를 만들어, 자연어로 시스템을 조작할 수 있게 한다.

핵심 흐름:
```
사용자 (자연어) → Chatbot → HubAPI (구조화 명령) → TM/WFC
```

---

## Phase 1.5 구현 항목

### 1. Chatbot 레이어

**현재:** CLI 명령어를 직접 입력해야 함. 명령어 구조를 알아야 사용 가능.

**목표:** 자연어로 대화하면 Chatbot이 의도를 파악하여 HubAPI를 호출.

**예시:**
```
사용자: "my-app 프로젝트에 로그인 기능 만들어줘"
→ Chatbot: hub_api.submit(project="my-app", title="로그인 기능 구현", description="...")

사용자: "지금 돌아가고 있는 task 뭐 있어?"
→ Chatbot: hub_api.list_tasks(status="in_progress")

사용자: "00042번 승인해"
→ Chatbot: hub_api.approve(project=..., task_id="00042")
```

**구현 범위:**
- `scripts/chatbot.py` — Chatbot 메인 모듈
  - Claude Code CLI(`claude -p`)를 사용하여 자연어 해석
  - 시스템 프롬프트에 HubAPI 사용법, 가용 명령 목록 포함
  - 결과를 사람이 읽기 좋은 형태로 포맷하여 응답
- Chatbot이 호출할 수 있는 HubAPI 메서드 목록 정의
- 모호한 요청 시 확인 질문 (예: 프로젝트 미지정 시 "어떤 프로젝트요?")

**핵심 설계 결정:**
- Chatbot 자체도 `claude -p`로 구동 (별도 API key 불필요)
- HubAPI를 직접 import하여 Python 함수 호출 (subprocess 아님)
- Chatbot은 코드를 작성하지 않음 — 시스템 조작만 담당

### 2. Chatbot 세션 관리

**현재:** 해당 없음 (새 기능)

**목표:** 대화 이력을 유지하여 맥락 있는 대화 가능.

**구현 범위:**
- ~20턴 이후 이전 대화를 compress (요약)
- 프로젝트 전환 시 해당 프로젝트의 context를 reload
- 세션 이력 저장: `projects/{name}/chat_history.json` 또는 별도 경로
- 세션 시작/종료 관리

### 3. 메신저 연동

**현재:** CLI로만 조작 가능.

**목표:** Slack/Telegram에서 메시지를 보내면 Chatbot을 경유하여 task 생성/조회/승인.

**구현 범위:**
- 메신저 어댑터 인터페이스 정의 (공통 추상 클래스)
- Slack 어댑터 (우선) 또는 Telegram 어댑터
- 메시지 수신 → Chatbot 호출 → 응답 전송
- 알림 시스템(notification.py)과 연동: 알림 발생 시 메신저로 전송

**참고:** 메신저 연동은 Chatbot 레이어가 먼저 완성된 후 진행. 독립적으로 단계 분리 가능.

### 4. 프로토콜 body 정의

**현재:** HubAPI 메서드 파라미터가 곧 인터페이스. 명시적 프로토콜 없음.

**목표:** Request/Response 형식을 명확히 정의하여 Chatbot/메신저/웹 어디서든 동일한 형식으로 통신.

**구현 범위:**
- Request envelope: `{action, project, params, attachments}`
- Response envelope: `{success, data, error, message}`
- 에러 형식 표준화
- 첨부파일 base64 규격 (CLAUDE.md의 기존 결정 반영)

### 5. user_preferences slot

**현재:** 사용자 선호는 별도로 저장되지 않음.

**목표:** 사용자의 대화 선호 (기본 프로젝트, 응답 언어, 알림 수준 등)를 project_state.json에 저장.

**구현 범위:**
- `project_state.json`의 `user_preferences` 필드
- Chatbot이 "기본 프로젝트를 my-app으로 설정해줘" 같은 명령 처리
- 기존 4계층 설정 체계 내에서 처리 (추가 계층 없음)

---

## 권장 작업 순서

1. **프로토콜 body 정의** — 나머지의 기반
2. **Chatbot 레이어** — 핵심 기능, HubAPI 연동
3. **Chatbot 세션 관리** — 대화 품질 향상
4. **user_preferences** — Chatbot 편의 기능
5. **메신저 연동** — Chatbot 위에 어댑터 추가

---

## 참고: 기존 코드 진입점

| 파일 | 관련 함수/위치 | 용도 |
|------|---------------|------|
| `scripts/hub_api/core.py` | HubAPI 클래스 | Chatbot이 호출할 백엔드 |
| `scripts/hub_api/models.py` | SubmitResult, TaskSummary 등 | 응답 모델 |
| `scripts/cli.py` | `build_parser()`, `cmd_*()` | CLI 구현 참고 (같은 HubAPI 사용) |
| `scripts/notification.py` | `emit_notification()`, `get_notifications()` | 메신저 알림 연동 시 |
| `config.yaml.template` | notification 섹션 | 메신저 채널 설정 확장 지점 |

---

## 테스트 방침

- 기존 테스트 스위트(`./run_test.sh all`)가 regression 검증
- Chatbot 테스트: mock으로 claude -p 응답을 제어하여 의도 파악 정확도 검증
- 메신저 테스트: 어댑터별 단위 테스트 + 통합 테스트
- 새 테스트는 `tests/` 디렉토리에 추가, `run_test.sh`의 분류에 반영
