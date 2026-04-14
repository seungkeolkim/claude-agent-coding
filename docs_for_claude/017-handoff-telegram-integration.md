# Telegram 연동 (Phase 2.3) 핸드오프

> 작성: 2026-04-14
> 기준 문서: `docs/agent-system-spec-v07.md` §15.3 "메신저 연동" 행
> 브랜치: `feature/telegram-integration` (예정)
> 선행: `016-handoff-merge-conflict-error.md` (main 머지 완료, 아카이브됨)

---

## 요약

Agent Hub에 Telegram 연동을 추가한다. 사용자가 텔레그램 슈퍼그룹 내에서 프로젝트별 topic을 통해 알림을 수신하고, 자연어/슬래시 명령으로 task 제출·승인·조회 등 전 기능을 수행할 수 있게 한다. Web Chat과 동일한 chatbot 파이프라인을 재사용한다.

**Phase 범위 (합의됨)**
- **Phase A**: Supergroup + Topic 자동 생성, 프로젝트 바인딩, outbound 알림
- **Phase B**: Inbound (자연어 chatbot 경유 + 슬래시 명령), inline keyboard 승인 UX
- **Phase C (후속)**: 첨부파일 (photo/document) 다운로드 → `attachments/`

A+B를 이번 세션의 구현 범위로 한다. C는 별도 세션.

---

## 1. Topology 결정

```
[Agent Hub] 슈퍼그룹 (Forum Topics 활성화)
├── General                      ← 시스템 전역 공지, /bind_hub, orphan 알림
├── 🟢 test-web-service          ← 프로젝트 A의 모든 알림/대화
├── 🟢 my-app                    ← 프로젝트 B
└── 🔒 retired-project           ← close_project 후 닫힌 topic (메시지 보존)
```

- **1 그룹 + 프로젝트당 1 topic** 구조. 그룹은 봇이 admin 권한 보유.
- Topic 자동 생성: `createForumTopic` API (bot이 호출).
- Topic 삭제는 **절대 자동으로 하지 않음**. close만 자동, delete는 사람이 명시적으로 실행.

---

## 2. Topic Lifecycle

### 생성 (자동)
```
HubAPI.create_project(name)
  → telegram_bridge.create_topic(name)
  → createForumTopic → thread_id 반환
  → project_state.json.telegram = { chat_id, thread_id }
  → 해당 topic에 환영 메시지 + 기본 사용법 안내
```

### 닫기 (자동, 복구 가능)
세 경로 모두 `closeForumTopic` 호출 (메시지 보존, 아이콘 🔒):

1. **정상 close**: `HubAPI.close_project()` → hook으로 bridge에 `close_topic` 요청
2. **폴더 소실 감지**: `scripts/web/syncer.py`의 기존 "폴더 소실 → lifecycle closed 자동 전환" 로직에 hook 추가
3. **Reconciliation**: bridge 기동 시 1회 + 매일 1회. `projects/*/project_state.json`의 thread_id 집합 vs Telegram 그룹의 실제 topic 집합 대조. 매칭 안 되는 topic을 orphan으로 분류.

### Orphan 처리 (자동 삭제 없음)
- Orphan 감지 시: topic 이름 앞에 `⚠️ [orphan]` prefix로 rename + General topic에 알림
- 사용자가 확인 후 명시적으로 수동 삭제:
  ```bash
  ./run_system.sh telegram list-orphans        # 목록 조회
  ./run_system.sh telegram delete-topic <thread_id>  # 개별 삭제
  ./run_system.sh telegram prune-orphans       # 전체 삭제 (prompt 후)
  ```
- 이 명령만 `deleteForumTopic` API 호출. 그 외 경로에서는 삭제 불가.

### 재오픈 (reopen_project)
- `HubAPI.reopen_project()` → bridge에 `reopen_topic` → `reopenForumTopic`
- thread_id는 project_state.json에 보존되어 있으므로 기존 topic 그대로 사용

---

## 3. 시스템 구조

```
[상주] Task Manager         ┐
[상주] Web Console          ├→ notification.py emit
[상주] telegram_bridge (신규)┘        ↓
                              fan-out: cli / web / telegram

telegram_bridge.py 내부:
  ├── Long polling loop (getUpdates, offset 관리)
  ├── Inbound dispatcher
  │    ├── slash command → hub_api.dispatch() 직접
  │    ├── natural language → ChatProcessor (web_chatbot 재사용)
  │    └── callback_query (inline keyboard) → hub_api.dispatch()
  ├── Outbound sender
  │    └── notification poll → sendMessage / sendPhoto (to 해당 topic)
  └── Reconciliation thread (24h)
```

**핵심**: Web Chat의 `ChatProcessor`를 재사용. session_id를 `tg_{chat_id}_{thread_id}`로 생성해 topic 단위 영구 세션.

---

## 4. 신규/수정 파일

| 파일 | 상태 | 역할 |
|------|------|------|
| `scripts/telegram/__init__.py` | 신규 | 패키지 |
| `scripts/telegram/client.py` | 신규 | Bot API HTTP 래퍼 (sendMessage, createForumTopic, closeForumTopic, deleteForumTopic, editMessageText, answerCallbackQuery, getUpdates, getFile) |
| `scripts/telegram/router.py` | 신규 | 수신 update → (project, action) 매핑. whitelist 검증 |
| `scripts/telegram/formatter.py` | 신규 | notification → Telegram 메시지 포맷 (MarkdownV2 escape, inline keyboard 생성) |
| `scripts/telegram/session.py` | 신규 | `(chat_id, thread_id) → session_id` 매핑, ChatProcessor 인스턴스 관리 |
| `scripts/telegram/reconciler.py` | 신규 | orphan topic 탐지 + rename |
| `scripts/telegram_bridge.py` | 신규 | 상주 프로세스 진입점. long polling + outbound poll + reconciler |
| `scripts/hub_api/core.py` | 수정 | `create_project()`, `close_project()`, `reopen_project()`에 telegram hook |
| `scripts/hub_api/models.py` | 수정 | `ProjectState.telegram: Optional[TelegramBinding]` |
| `scripts/web/syncer.py` | 수정 | 폴더 소실 hook에 bridge 통지 추가 |
| `scripts/notification.py` | 수정 | channel fan-out (cli / web / telegram) |
| `templates/config.yaml.template` | 수정 | `telegram` 섹션 추가 |
| `run_system.sh` | 수정 | bridge 프로세스 start/stop/status 추가, `telegram` 서브커맨드 (list-orphans, delete-topic, prune-orphans, bind) |
| `scripts/web/db.py` | 수정 | projects 테이블에 `telegram_chat_id`, `telegram_thread_id` 컬럼 (Web UI 표시용) |
| `docs/agent-system-spec-v07.md` | 수정 | §16 Telegram Integration 신설, §15에 완료 기록 |
| `tests/test_telegram_*.py` | 신규 | bot API mock 기반 단위 테스트 + bridge 통합 테스트 |

---

## 5. config.yaml.template 추가 섹션

```yaml
# ─── Telegram 연동 (Phase 2.3) ───
telegram:
  enabled: false                    # true로 하면 run_system.sh start 시 bridge 기동
  bot_token: ""                     # BotFather에서 발급 (빈 값이면 자동 disabled)
  hub_chat_id: 0                    # /bind_hub 명령으로 자동 기록 (편집 금지 권장)
  allowed_user_ids: []              # 이 목록 외 user의 메시지는 무시 (빈 배열은 전체 거부)
  bind_secret: ""                   # /bind_hub <secret> 검증용. bind 성공 후 소비 (비워짐)
  long_polling_timeout_seconds: 30
  reconciliation_interval_hours: 24
```

---

## 6. Bootstrap UX

### 최초 시스템 설정 (1회)
```
1. BotFather에서 봇 생성 → token 획득
2. config.yaml 편집:
   telegram.enabled: true
   telegram.bot_token: "<token>"
   telegram.allowed_user_ids: [<내_user_id>]
   telegram.bind_secret: "<랜덤 문자열, 예: uuid4>"
3. Telegram에서 슈퍼그룹 생성 → Settings → Topics 활성화
4. 봇을 그룹에 초대 + admin 권한 부여 (topic 생성/관리/삭제, 메시지 삭제)
5. ./run_system.sh start
6. 그룹의 General topic에서: /bind_hub <secret>
   → bot이 chat_id를 config.yaml에 기록 + bind_secret 소비
   → "✅ Agent Hub 연결됨" 응답
```

### 프로젝트 생성 (이후 매번 자동)
```
사용자: ./run_agent.sh init-project  (또는 chatbot "프로젝트 만들어줘")
  ↓
HubAPI.create_project()
  ↓
bridge.create_topic(project_name)
  ↓
새 topic에 환영 메시지:
  "🆕 프로젝트 'xxx' 연결됨
   사용 가능 명령: /status /list /pending /help
   또는 자연어로 직접 요청하세요."
```

### 팀원 추가 (선택)
- config.yaml `allowed_user_ids`에 user_id 추가 → bridge 재기동 또는 SIGHUP
- Phase 2.3에서는 정적 관리. 동적 `/invite` 명령은 후속.

---

## 7. Interaction UX

### Outbound 알림 (topic으로 전송)

**Plan Review 요청**
```
🟡 Plan Review 요청 · task #00042
"로그인 기능 구현"

Planner가 subtask 3개 생성:
  1. 백엔드 API
  2. 프론트 UI
  3. E2E 테스트

[✅ 승인] [📝 수정] [❌ 취소]
```
버튼 → callback_query → `approve / reject(modify) / reject(cancel)` dispatch.

**기타 이벤트 포맷**
| 이벤트 | 표시 |
|------|------|
| pr_created | 🔵 PR #N 생성됨 (URL) |
| pr_merged | 🟢 PR 머지 완료 |
| pr_merge_failed | ⚠️ PR 머지 실패: {error} |
| task_completed | ✅ task #N 완료 |
| task_failed | 🔴 task #N 실패: {reason} |
| escalation | 🚨 에스컬레이션 필요 |

### Inbound (사용자 → bot)

**슬래시 명령 (fast path)**
- `/status` — 프로젝트 현재 상태 + 실행 중 task의 pipeline_stage
- `/list [--status <s>]` — task 목록
- `/pending` — 승인 대기 항목 + inline keyboard
- `/cancel <id>` — task 취소 (확인 버튼)
- `/help` — 명령 안내

**자연어 (natural path)**
```
user: 로그인 기능 구현해줘 급함
bot (typing...): ...
bot: ⚠️ 확인이 필요합니다
     action: submit
     title: 로그인 기능 구현
     priority: urgent
     [✅ 확인] [❌ 취소]
user: (✅ 클릭)
bot: ✅ task #00043 제출됨 (priority=urgent)
```
→ `ChatProcessor` 경유. 확인 카드는 inline keyboard로 변환.

---

## 8. Session 매핑

```python
session_id = f"tg_{chat_id}_{thread_id}"
# 영구 유지. Web Chat과 동일한 session_history/chatbot/<session_id>.json
```

- Topic 단위로 1개 영구 세션 → conversation 컨텍스트가 topic 내에서 지속
- `/new_session` 슬래시 명령으로 세션 리셋
- 20턴 초과 시 기존 Chatbot compression 로직 재사용

---

## 9. 보안

| 항목 | 정책 |
|------|------|
| User whitelist | `allowed_user_ids` 정적 배열. 목록 외 user는 "⚠️ 권한 없음" 응답 후 무시 |
| Chat whitelist | `hub_chat_id` 1개만 허용. 다른 chat의 업데이트는 drop |
| Bot token | config.yaml에만 (gitignored). 로그에 출력 금지 |
| Bind secret | 1회용. bind 성공 후 config.yaml에서 소비 (빈 문자열로 rewrite) |
| 고위험 action 확인 | chatbot HIGH_RISK_ACTIONS 그대로 상속. inline keyboard로 "확인" 필수 |

---

## 10. 시그널 / 수명

- `SIGTERM`: graceful shutdown. getUpdates 루프 중단 + outbound 큐 drain
- `SIGHUP`: config.yaml 재로드 (allowed_user_ids 동적 반영)
- `run_system.sh stop`: Web → bridge → TM 순 종료
- `run_system.sh status`: 3 프로세스 상태 각각 표시

---

## 11. 테스트 전략

- **Unit**: Bot API HTTP 래퍼는 `requests` mock. formatter는 pure function. router는 whitelist/매핑 로직 단위 검증.
- **Integration**: bridge 프로세스를 spawn하고 가짜 getUpdates 응답(fixture JSON)을 주입 → hub_api.dispatch 경로까지 검증.
- **E2E 제외**: 실제 Telegram API 호출은 CI에서 안 돌림. 수동 검증 체크리스트 별도.

목표 테스트 수: +30~40개 (unit 25 + integration 10). 기존 212 → 240~250.

---

## 12. 구현 순서 (권장)

```
1. config.yaml.template + project_state.json schema + models.py 업데이트
2. scripts/telegram/client.py (HTTP 래퍼 + 단위 테스트)
3. scripts/telegram/formatter.py (notification → 메시지 + 단위 테스트)
4. scripts/telegram/session.py + router.py (매핑 + whitelist + 단위 테스트)
5. scripts/telegram_bridge.py (진입점, long polling, outbound poll)
6. HubAPI hook (create/close/reopen_project)
7. notification.py fan-out
8. syncer.py 폴더 소실 hook
9. run_system.sh 통합 (start/stop/status + telegram 서브커맨드)
10. reconciler.py + 수동 삭제 명령
11. Web DB 컬럼 + UI에 topic 링크 표시 (선택)
12. spec §16 작성 + §15 완료 기록
13. 수동 검증 체크리스트 실행
```

---

## 13. 수동 검증 체크리스트 (머지 전)

- [ ] BotFather 봇 생성 → token 설정
- [ ] 슈퍼그룹 + Topics 활성화 + 봇 admin
- [ ] `/bind_hub <secret>` 성공 + config.yaml 반영
- [ ] 새 프로젝트 생성 → topic 자동 생성 + 환영 메시지
- [ ] `/status` /list /pending 응답
- [ ] 자연어 submit → 확인 버튼 → 제출 성공
- [ ] Plan review 요청 outbound → 버튼 승인 → 진행
- [ ] PR merged 알림 수신
- [ ] close_project → topic 🔒 확인
- [ ] 프로젝트 폴더 수동 삭제 → syncer가 topic close
- [ ] reopen_project → topic 복귀
- [ ] `./run_system.sh telegram list-orphans` 동작
- [ ] whitelist 외 user 메시지 무시
- [ ] SIGTERM graceful shutdown

---

## 14. 주의사항

1. **MarkdownV2 escape**: `_ * [ ] ( ) ~ ` > # + - = | { } . !` 모두 escape 필요. 전용 util 필수.
2. **Rate limit**: Bot API 초당 30msg/그룹. outbound 큐에 간격 삽입.
3. **message_thread_id 누락**: topic을 활성화한 그룹에서 `sendMessage`에 `message_thread_id` 안 넣으면 General로 감. 전 outbound 경로에서 필수 파라미터.
4. **Long polling offset**: 재기동 시 offset을 파일에 저장/복원 안 하면 직전 메시지 중복 처리. `data/telegram_offset.json` 등에 저장.
5. **callback_query timeout**: `answerCallbackQuery`를 15초 이내 호출 안 하면 클라이언트에 빨간 에러 표시. dispatch 전에 먼저 ack.
6. **첨부파일은 이번 Phase 제외**. Photo/document 수신 시 "첨부는 아직 지원 안 함" 안내.

---

## 다음 세션 후보

### A. 이 핸드오프 기반 구현 시작 (권장)
- `feature/telegram-integration` 브랜치
- 12 구현 순서대로 진행
- 중간 커밋 단위: 각 모듈(client/formatter/router/bridge) + bootstrap + hook + run_system 통합

### B. Phase C (첨부파일)
- Phase A+B 안정화 후

### C. 동적 whitelist (`/invite @X`)
- 팀 공유 필요해지면

---

## 재개 가이드

```bash
git checkout main && git pull
git checkout -b feature/telegram-integration

# 구현 진행 (12의 1~12 순서)
# 각 단계마다 테스트 돌리기 권장
./run_test.sh unit

# 수동 검증 (13 체크리스트)
./run_system.sh start
# ... Telegram에서 테스트 ...
./run_system.sh stop

# 머지
git checkout main && git merge --no-ff feature/telegram-integration
git push origin main
```

---

## 참고: 이번 핸드오프에서 만지지 않을 영역

- E2E 테스트장비 연동 (Phase 2.4) — 별개
- GH_TOKEN 환경변수 전환 (Phase 2.2+) — 별개
- user_preferences slot (Phase 2.2+) — 별개
- 동적 whitelist / 권한 레벨 — 후속 Phase
- Slack 연동 — Phase 2.3 이후 별도
