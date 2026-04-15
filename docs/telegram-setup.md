# Telegram 연동 설정 가이드

Agent Hub의 Telegram bridge를 처음 설정할 때 따라야 하는 절차입니다.
한 번만 수행하면 이후엔 프로젝트가 추가될 때마다 topic이 자동으로 생성됩니다.

전체 그림: **1 supergroup + 프로젝트당 1 forum topic** 구조.

```
[Group: AgentHub]
├── General                  ← 시스템 전역 공지, /bind_hub
├── 🟢 my-app                ← 프로젝트 A의 알림/대화
└── 🟢 web-service           ← 프로젝트 B의 알림/대화
```

---

## 1. Telegram 봇 만들기

1. Telegram에서 **@BotFather** 검색 → `/newbot`
2. 봇 표시 이름 → 봇 username 입력 (`xxx_bot`으로 끝나야 함)
3. BotFather가 알려주는 **bot token**을 메모해 둡니다 (형식: `123456:AAEx...`).
4. 같은 BotFather 대화에서 `/setprivacy` → 해당 봇 선택 → **Disable** 선택
   (그룹의 모든 메시지를 봇이 볼 수 있도록 — 그래야 자연어 메시지가 ChatProcessor로 라우팅됨)

---

## 2. 그룹 만들고 Forum 모드 켜기

1. Telegram에서 새 그룹 생성 (이름 예: `AgentHub`)
2. 그룹에 봇을 멤버로 추가
3. 그룹을 **supergroup으로 변환**해야 forum topic을 쓸 수 있습니다.
   - 보통 그룹 멤버가 일정 수 이상이거나 공개 그룹으로 설정하면 자동 변환되지만,
     가장 확실한 방법은 그룹 설정에서 **Topics** 토글을 켜는 것입니다.
4. 그룹 이름 탭 → **Edit** → **Topics** 토글 ON
   - "Topics will be enabled for this group" 안내가 뜨면 확인.
   - Topics 켜기가 보이지 않으면 supergroup 변환이 안 된 것 — 그룹 멤버 1명 더
     추가하거나 그룹 link를 만들면 supergroup으로 승격됩니다.

### 봇에게 권한 부여

그룹 → **Administrators** → 봇을 관리자로 추가하고 다음 권한을 켭니다:

- ✅ **Manage Topics** (필수 — topic 생성/닫기/재오픈/삭제)
- ✅ **Send Messages** (기본)
- (선택) Pin Messages — 환영 메시지 고정 등에 활용 가능

> ⚠️ Manage Topics 권한이 없으면 `not enough rights to create a topic` 에러가 납니다.

---

## 3. config.yaml 설정

`config.yaml`의 `telegram` 섹션을 다음과 같이 채웁니다:

```yaml
telegram:
  enabled: true
  bot_token: "123456:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"   # BotFather 토큰
  hub_chat_id: 0                              # /bind_hub 후 자동 기록 — 직접 편집 금지
  allowed_user_ids:                           # 봇과 통신을 허용할 user_id 목록
    - 123456789                               # 본인 user_id (아래 방법으로 확인)
  bind_secret: "임의의-랜덤-문자열"            # /bind_hub <secret>로 1회 사용
  long_polling_timeout_seconds: 30
  reconciliation_interval_hours: 24
  send_interval_seconds: 0.05
```

### 본인 user_id 확인 방법

Telegram에서 **@userinfobot** 검색 → 시작 → 본인 ID가 표시됩니다.
또는 `@RawDataBot`을 그룹에 잠시 추가하면 모든 멤버 ID를 보여줍니다.

### bind_secret이 필요한 이유

`/bind_hub`는 **그룹 chat_id를 봇이 자동으로 기억**하게 만드는 명령입니다.
누구나 무단으로 다른 chat에 bind 하지 못하도록 1회용 시크릿으로 검증합니다.
bind 성공 시 `bind_secret`은 빈 문자열로 자동 소비됩니다.

---

## 4. 시스템 기동 + bind

```bash
./run_system.sh start
./run_system.sh status     # Telegram Bridge: 실행 중 확인
```

bridge가 정상 기동되면 그룹의 **General topic**(좌측 상단 기본 topic)에서
다음 메시지를 보냅니다:

```
/bind_hub 임의의-랜덤-문자열
```

봇이 `✅ Agent Hub 연결됨. 프로젝트 생성 시 자동으로 topic이 추가됩니다.`로
회신하면 성공입니다. 동시에 `config.yaml`이 갱신됩니다:

```yaml
telegram:
  hub_chat_id: -1001234567890   # 자동 기록된 그룹 chat_id (음수)
  bind_secret: ""               # 소비됨
```

> 주의: `/bind_hub`는 반드시 **실제 그룹의 General topic**에서 보내야 합니다.
> 봇과의 1:1 DM에서 보내면 user_id가 hub_chat_id로 잘못 기록돼 이후 topic 생성이 실패합니다.
> 잘못 bind 했다면 `bind_secret`을 다시 채우고 bridge를 재기동(`./run_system.sh stop && start`)한 뒤
> 그룹에서 다시 시도하세요.

---

## 5. 프로젝트와 topic 연동

### 새 프로젝트 — 자동 생성

```bash
./run_agent.sh init-project
```

`HubAPI.create_project()`가 자동으로 bridge에 `create_topic` 명령을 enqueue 하므로
1~2초 안에 그룹에 새 topic이 생기고 환영 메시지가 표시됩니다.

### 기존 프로젝트 — 수동 등록

bridge 미기동 상태에서 만든 프로젝트, 또는 권한 부족으로 자동 생성이 실패한
프로젝트는 다음 명령으로 사후 등록할 수 있습니다:

```bash
./run_system.sh telegram register <project-name>
# 이미 등록된 프로젝트의 topic을 새로 만들고 싶다면:
./run_system.sh telegram register <project-name> --force
```

내부 동작은 `init-project`와 동일합니다 — `data/telegram_commands/`에 명령 파일을
쌓고 bridge가 폴링해서 처리합니다. bridge가 꺼져 있어도 큐에는 남아 있다가
다음 기동 시 소비됩니다.

---

## 6. 사용 흐름

연동이 끝나면 그룹에서 다음과 같이 사용할 수 있습니다.

| 위치 | 입력 | 동작 |
|---|---|---|
| 프로젝트 topic | 자연어 ("로그인 기능 추가해줘") | ChatProcessor로 라우팅 → task 제안 |
| 프로젝트 topic | `/status` | 해당 프로젝트 현황 |
| 프로젝트 topic | `/list --status submitted` | task 필터 조회 |
| 프로젝트 topic | `/pending` | 사용자 승인 대기 task |
| 프로젝트 topic | `/cancel <task_id>` | task 취소 |
| 프로젝트 topic | `/new_session` | 해당 topic의 ChatProcessor 세션 초기화 |
| 어디든 | `/help` | 사용 가능한 명령 |

알림(승인 요청, 머지 실패 등)은 자동으로 해당 프로젝트 topic에 인라인 키보드와
함께 게시됩니다.

---

## 7. 운영 명령 (`./run_system.sh telegram`)

| 명령 | 용도 |
|---|---|
| `register <project> [--force]` | 기존 프로젝트의 topic을 수동 등록 |
| `list-orphans` | 우리 ledger(`project_state.json`)에 기록된 topic binding 목록 출력 |
| `delete-topic <thread_id>` | 특정 topic 직접 삭제 (Bot API `deleteForumTopic` 호출) |
| `prune-orphans [-y]` | `lifecycle=closed`인 모든 binding의 topic을 일괄 삭제 |

> 참고: Bot API에는 그룹의 모든 forum topic을 일괄 조회하는 endpoint가 없습니다.
> "그룹에는 있지만 우리 ledger에는 없는" 진짜 orphan 탐지는 차기 Phase의 reconciler가
> 담당합니다.

---

## 8. 종료 / 일시 비활성

- 프로세스 종료: `./run_system.sh stop` (Web → Bridge → TM 순)
- 영구 비활성: `config.yaml`의 `telegram.enabled: false`로 변경 → 다음 `start` 시 bridge 미기동.
  (이때 HubAPI hook은 명령 파일을 큐에 계속 쌓지만 소비되지 않을 뿐 시스템엔 영향 없음.)

---

## 9. 트러블슈팅

| 증상 | 원인 / 해결 |
|---|---|
| `the chat is not a forum` | 그룹의 Topics 모드가 꺼져 있음. 그룹 Edit → Topics ON. |
| `not enough rights to create a topic` | 봇이 관리자가 아니거나 Manage Topics 권한 없음. |
| `/bind_hub` 후 `hub_chat_id`가 양수 (예: 본인 user_id와 같은 값) | DM에서 bind 한 것. `bind_secret` 다시 채우고 그룹에서 재시도. |
| 봇이 그룹 메시지에 반응 없음 | BotFather에서 `/setprivacy` → Disable. `allowed_user_ids`에 본인 user_id 등록 확인. |
| topic은 생겼는데 알림이 안 옴 | `logs/telegram_bridge.log` 확인. `notifications.json` mtime이 갱신되는지, bridge 프로세스가 살아있는지(`./run_system.sh status`) 확인. |

자세한 아키텍처는 `docs_for_claude/017-handoff-telegram-integration.md`를 참고하세요.
