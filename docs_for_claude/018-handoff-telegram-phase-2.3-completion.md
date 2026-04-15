# Telegram 연동 (Phase 2.3) 완료 핸드오프

> 작성: 2026-04-15
> 기준 문서: `docs/agent-system-spec-v07.md` §15.4 Phase 2.3
> 브랜치: `feature/connect-telegram-messanger` (main 대비 6 commits, 미머지)
> 선행: `017-handoff-telegram-integration.md` (설계 핸드오프, 본 세션과 함께 아카이브)

---

## 요약

`017-handoff-telegram-integration.md`의 §12.1~9 기본 구현을 끝낸 뒤, 사용자 테스트 중에 발견된 운영상의 빈틈을 채워 Phase 2.3을 종료한다. 이번 세션에서 추가된 항목은 다음 5가지이다.

1. **수동 register CLI + bind 시 config.yaml 주석 보존** — `/bind_hub` 외에 운영자가 직접 chat/topic을 등록할 수 있는 `python -m telegram.cli register` 추가. bind 결과를 `config.yaml`에 다시 쓸 때 기존 주석을 잃지 않도록 라인 단위 치환으로 변경.
2. **git push 환경 분리 (Option B)** — telegram_bridge가 spawn하는 subprocess가 VSCode askpass 소켓을 상속받아 git push가 정지하는 문제를 `GIT_ASKPASS`/`SSH_ASKPASS` 등 askpass 관련 env를 strip하고 `http.extraheader`로 PAT를 inline 주입하는 방식으로 해소.
3. **`requested_by` 신원 태그 채널 전파** — Telegram에서 트리거된 task가 commit/PR 메시지에 `[00141][tg:username]`로 출처를 남기도록 router→bridge→HubAPI→Request→task JSON→WFC 경로로 식별자를 흘렸다. CLI(`source="cli"`, `requested_by=$USER`)와 Web(`source="web"`, `requested_by="web"`)도 같은 구조로 정렬.
4. **프로젝트별 Telegram opt-out** — `project.yaml`에 `telegram.enabled` 필드 도입. `init-project` 인터랙티브 단계에서 묻고, `_enqueue_telegram_command()`가 호출될 때마다 read-through 한다. 테스트 스위트는 `conftest.py` autouse 픽스처로 `_enqueue_telegram_command`를 통째로 차단.
5. **채널 간 human-review sync** — Web에서 plan/PR을 승인해도 Telegram에는 무소식이던 문제 해결. `_respond_to_interaction`/`merge_pr`/`close_pr`이 `plan_review_responded`/`pr_review_responded` 알림을 emit하고, Telegram inline 버튼은 dispatch 직전에 task 상태를 재조회해 이미 처리된 경우 "이미 ✅ 승인됨 by X (via web) · timestamp" 안내를 보낸다.

테스트: 260개 통과 (기존 212 + Telegram 48: client/formatter/router unit). E2E `gh pr` 경로는 sandbox 한계로 통합 검증 제외.

---

## 이번 세션 커밋

```
accc37e Telegram: 채널 간 human-review 상태 동기화
aa13bee docs: Telegram 연동 셋업 가이드 추가
4b2a05e Telegram: git push 환경 분리 + 사용자 식별 태그 + 프로젝트별 opt-out
db7669a Telegram: 수동 register 명령 + bind 시 config.yaml 주석 보존
4d062bb Telegram 연동 (Phase 2.3) §12.1~9 구현
516d03b Telegram 연동 (Phase 2.3) 설계 핸드오프 문서 작성
```

main 대비 6 commits. 미푸시. 사용자 테스트 통과 후 머지 예정.

---

## 1. 수동 register + 주석 보존 (`db7669a`)

### 1.1 `python -m scripts.telegram.cli register`

운영자가 슈퍼그룹/토픽을 직접 등록할 수 있는 경로. `/bind_hub` 메시지가 닿지 못하는 환경(테스트 그룹, 봇 권한 미정착 등)에서 fallback.

- 옵션: `--chat-id`, `--thread-id`, `--project`. project 지정 시 해당 project_state.json의 `telegram.{chat_id, thread_id}`에 직접 기록.
- 동일 모듈에 `list-projects`/`unbind` 등 점검 명령도 함께 둠.

### 1.2 config.yaml 주석 보존

bind 성공 시 `telegram.hub_chat_id` 갱신과 `telegram.bind_secret` 비우기를 수행하는데, 기존 `yaml.safe_dump` 재기록은 사용자 주석을 모두 날렸다. ruamel 의존성 추가를 피하고자 **라인 단위 정규식 치환**으로 변경:

- `^\s*hub_chat_id:` 라인을 in-place로 새 값으로 교체.
- `bind_secret:`도 동일.
- 둘 다 매치되지 않으면 `telegram:` 블록 끝에 append (들여쓰기 보존).

코드 위치: `scripts/telegram_bridge.py:_persist_bind_to_config()`

---

## 2. git push 환경 분리 (Option B, `4b2a05e`)

### 2.1 증상

telegram_bridge가 `python run_agent.sh run wfc ...`을 spawn하면, WFC가 git push 단계에서 무기한 정지. 원인은 부모가 VSCode 환경을 상속받아 `GIT_ASKPASS=/.../askpass.sh`가 박혀 있고, 해당 스크립트가 부모의 VSCode socket(`VSCODE_GIT_IPC_HANDLE`)을 통해 자격 증명을 받는 구조라, 부모와 끊긴 자식 프로세스에서는 응답이 영원히 오지 않기 때문.

### 2.2 해결 (Option B 채택)

자식 프로세스 환경에서 askpass/socket 관련 env를 제거하고, push 시 PAT를 `http.extraheader`로 inline 주입한다.

- env strip 대상: `GIT_ASKPASS`, `SSH_ASKPASS`, `VSCODE_GIT_ASKPASS_NODE`, `VSCODE_GIT_ASKPASS_MAIN`, `VSCODE_GIT_ASKPASS_EXTRA_ARGS`, `VSCODE_GIT_IPC_HANDLE`.
- 적용 위치: `scripts/telegram_bridge.py:_spawn_wfc()` 자식 env 가공 단계.
- 토큰 경로: `config.yaml` `github.token` (없으면 `GITHUB_TOKEN` env). subprocess `env`에 `GH_TOKEN`/`GITHUB_TOKEN` 동시 주입.
- 토큰을 git push에 전달하기 위한 `http.extraheader=AUTHORIZATION: bearer ${TOKEN}` 옵션은 WFC의 push 헬퍼에서 명시 사용.

→ Option A(매번 ssh-agent forward)나 keychain 의존을 피해 최소 침습으로 해결.

---

## 3. requested_by 신원 태그 (`4b2a05e`)

### 3.1 원칙

모든 변경 요청은 출처(`source`)와 행위자(`requested_by`)를 가져야 한다. 커밋/PR 본문은 `[task_id][requested_by]` 접두로 누가 트리거했는지 보존한다.

| 채널 | source | requested_by 예시 |
|------|--------|------------------|
| CLI | `"cli"` | `$USER` (예: `seungkeol`) |
| Web | `"web"` | `"web"` (사용자 다중화 전까지 단일 식별자) |
| Telegram | `"telegram"` | `tg:{username}` → `tg:{first_name}` → `tg:{user_id}` |

### 3.2 흐름

```
Telegram update
  ↓ scripts/telegram/router.py (_display_name)
RoutingDecision(user_display)
  ↓ scripts/telegram_bridge.py (_requested_by_for)
Request(source="telegram", requested_by="tg:...")
  ↓ scripts/hub_api/protocol.py (5 handlers forward)
HubAPI.{submit,approve,reject,merge_pr,close_pr,complete_pr_review}(..., source, requested_by)
  ↓ task JSON에 기록 / 응답 dict에 기록 / WFC 환경변수로 전달
WFC: commit/PR title 접두 = `[{task_id}][{requested_by}]`
```

### 3.3 변경 파일

- `scripts/telegram/router.py`: `RoutingDecision.user_display`, `_display_name(from_user)` 헬퍼.
- `scripts/telegram_bridge.py`: `_requested_by_for(d)`, 자연어/콜백/슬래시 모든 경로에서 Request에 attach.
- `scripts/hub_api/core.py`: `approve`/`reject`/`merge_pr`/`close_pr`/`complete_pr_review`에 `source`/`requested_by` kwarg 추가, response dict와 `pr_review_responded`/`plan_review_responded` 알림 details에 기록.
- `scripts/hub_api/protocol.py`: 5개 핸들러가 `request.source`/`request.requested_by`를 forward.
- `scripts/cli.py`: `cmd_approve`/`cmd_reject`가 `source="cli"`, `requested_by=os.environ.get("USER") or "cli"` 부착.
- `scripts/web/server.py`: `/api/dispatch`가 `request.requested_by = "web"` (없을 때만), `_run_pr_action_background`도 동일.

---

## 4. 프로젝트별 Telegram opt-out (`4b2a05e`)

### 4.1 동기

테스트 스위트가 `create_project`를 다수 호출하면서, 매번 `createForumTopic` 명령을 큐에 쌓아 텔레그램 봇이 폭주.

### 4.2 설계

- `project.yaml`에 `telegram.enabled` 추가 (기본 `true` — 미존재/누락 시 호환). `templates/project.yaml.template`에도 반영.
- `init_project.generate_project_yaml(..., telegram_enabled=True)` 인자 추가.
- 인터랙티브 흐름에 `ask_telegram_enabled()` 추가.
- `HubAPI.create_project(..., telegram_enabled=True)` → `Request.params["telegram_enabled"]`로 protocol 전달.
- `_enqueue_telegram_command(...)` 상단에서 `_project_telegram_enabled(agent_hub_root, project)`를 검사. False면 enqueue 자체를 skip.
- 테스트: `tests/conftest.py` autouse 픽스처가 `hub_api.core._enqueue_telegram_command`를 no-op로 monkeypatch. 통합 테스트가 진짜 텔레그램에 닿을 일을 원천 차단.

---

## 5. 채널 간 human-review sync (`accc37e`)

### 5.1 증상

Web에서 plan을 승인해도 Telegram의 plan-review 메시지(승인/수정/취소 inline keyboard)는 그대로 남아 있고, 사용자가 Telegram에서 다시 ✅을 누르면 "이미 처리됨" 같은 안내 없이 dispatch가 되어 상태 충돌이 났다.

### 5.2 해결 — emit + 재조회

**Producer side** — `_respond_to_interaction`/`complete_pr_review`/`merge_pr`/`close_pr`에 알림 emit 추가:

```python
noti.emit_notification(
    project_dir=project_dir,
    event_type="plan_review_responded",
    task_id=task_id,
    message=f"{kind_label} {action_label}{by_part}{via_part}",
    details={"project": project, "action": action, "kind": hi_type,
             "responded_by": requested_by or "", "source": source or "",
             "user_message": message or ""},
)
```

신규 이벤트 두 종을 등록:

- `plan_review_responded` (label "Plan 응답") — `scripts/notification.py`, `scripts/telegram/formatter.py`(아이콘 📬).
- `pr_review_responded` (label "PR 응답") — 동일.

**Consumer side (Telegram)** — `telegram_bridge._handle_callback_query`가 dispatch 직전에 `_already_responded_notice(project, task_id)`를 호출. task 상태가 더 이상 `waiting_for_human_*`이 아니면 다음 형식으로 응답하고 dispatch는 skip:

```
이 항목은 이미 ✅ 승인됨 by web · 2026-04-15 11:32 (via web)
```

- `notification_loop`는 기존 폴링 경로로 `*_responded` 이벤트도 자연스럽게 Telegram에 흘려 보낸다.

이로써 어느 채널에서 처리해도 다른 채널이 즉시 사실을 안다.

---

## 6. 변경 파일 요약 (이번 세션)

```
docs/telegram-setup.md                          (신규, 191 lines)
scripts/telegram/cli.py                         (신규, 240 lines)
scripts/telegram/__init__.py                    (신규)
scripts/telegram/router.py                      (수정, user_display)
scripts/telegram/formatter.py                   (수정, *_responded 이벤트)
scripts/telegram_bridge.py                      (수정, env strip + already-responded + requested_by)
scripts/hub_api/core.py                         (수정, source/requested_by 흐름 + emit + opt-out)
scripts/hub_api/protocol.py                     (수정, 5 handlers forward + telegram_enabled)
scripts/notification.py                         (수정, *_responded 이벤트)
scripts/init_project.py                         (수정, telegram_enabled prompt)
scripts/cli.py                                  (수정, source/requested_by 부착)
scripts/web/server.py                           (수정, requested_by="web")
templates/project.yaml.template                 (수정, telegram.enabled)
templates/config.yaml.template                  (수정, telegram 블록)
tests/conftest.py                               (수정, autouse 픽스처)
tests/test_telegram_client.py                   (신규, 219 lines)
tests/test_telegram_formatter.py                (신규, 117 lines)
tests/test_telegram_router.py                   (신규, 172 lines)
run_test.sh                                     (수정, telegram tests 등록)
```

---

## 7. 검증

- `./run_test.sh all` → 260 passed.
- 사용자 수동 검증 통과 항목:
  - 슈퍼그룹 `/bind_hub` 1회 등록 후 `config.yaml` 주석 유지.
  - `create_project` 시 topic 자동 생성 + 환영 메시지.
  - `/submit`(자연어 포함) → task 생성, commit/PR 제목에 `[00###][tg:username]` 접두.
  - Web에서 plan 승인 시 Telegram에 "Plan 승인됨" 알림 도착, 동일 task의 Telegram inline 버튼 클릭 시 "이미 처리됨" 응답.
  - test 프로젝트는 `telegram.enabled: false`로 생성하면 topic 미생성 확인.
- 머지 전 검토 권장: PR 직접 close/merge 경로에서 `requested_by`가 비어 있는 historical task가 있으면 응답 메시지에서 `by ` 부분이 공백이 되는지 확인 (현재는 `by_part` 가드 있음).

---

## 8. 다음 세션을 위한 참고

### 8.1 이번 세션에서 다루지 않은 후속 작업

- **Reviewer retry_mode (continue / reset)** — 메모리 `project_reviewer_retry_mode_todo.md`. test-project task 00144 분석 결과로 도출된 설계 TODO. Telegram 작업과 분리.
- **첨부파일 (Phase 2.3 C)** — photo/document 다운로드 → `attachments/`. 현재는 안내 후 drop.
- **Web DB의 `requested_by`/`source` 컬럼 노출** — 현재는 task JSON에만 기록. UI에 작은 배지로 표시하면 채널 간 상황 인지가 더 빠를 것.
- **callback_data 64 byte 한계** — 긴 project name + task_id 조합은 위험. project를 hash/code로 치환하는 매핑 테이블 필요 (router/formatter 양쪽 개정).

### 8.2 운영 메모

- bot token 회전 시 `config.yaml` 수정 후 telegram_bridge만 재기동 (`run_system.sh restart` 권장 — 기존 chatbot 세션은 영향 없음).
- 그룹/봇 admin 권한 잃으면 `createForumTopic` 403 → 큐에 영원히 retry 누적되지 않도록 주기 정리 필요 (현재는 단순 로그 후 drop).
