# Long-term Memory + Agent Session Reuse 핸드오프

> 작성: 2026-04-16
> 기준 문서: `docs/agent-system-spec-v07.md` §13.1 / §15.4 (Project Long-term Memory, Phase 2.5)
> 브랜치: `feature/project-long-term-memory` (PR #5, **main 머지 완료**) + `feature/agent-session-reuse` (PR 전)

---

## 요약

핸드오프 시점의 3대 관심사(세션 재사용, CLAUDE.md, PROJECT_NOTES) 중 뒤쪽 두 개를 한 브랜치로 처리하고, 이어 세션 재사용을 별도 브랜치로 분리해 실환경 검증까지 마쳤다. 이번 세션에서 매듭지은 것은 다음 5가지다.

1. **Project Long-term Memory (MemoryUpdater stage 도입)** — codebase 루트에 LLM-agnostic한 `PROJECT_NOTES.md`(장기 메모리 본문) + `CLAUDE.md`(포인터 한 줄)를 init-project 시 템플릿 생성. 새 agent `memory_updater`가 Summarizer 직전에 실행되어 이번 task의 변경을 바탕으로 `PROJECT_NOTES.md`를 증분 갱신하고, WFC가 `[{task_id}] memory: ...` 커밋으로 묶어 같은 PR에 포함. Step numbering을 summarizer 08→09, memory_updater=08로 재조정. 실패해도 PR 생성은 차단하지 않음(경고만).
2. **memory_refresh task_type (full-scan 모드)** — 초기 init 이후 누락되었거나 사용자가 직접 개발한 변경 때문에 `PROJECT_NOTES.md`가 out-of-date일 때를 위한 경로. 기존 파이프라인을 그대로 재사용한다: Planner는 `subtasks: []`로 통과하고, finalize 단계의 MemoryUpdater가 full-scan 모드로 codebase 전체를 스캔해 문서를 재구성한다. 특수 파이프라인을 새로 만들지 않고 task_type 분기로만 처리해 관리 포인트를 늘리지 않았다.
3. **진입점 확장 (chat / telegram / web)** — `submit`에 `task_type` 파라미터 추가, CLI `refresh-memory` 서브커맨드, chatbot system prompt에 memory_refresh 가이드(텔레그램 자연어 요청 자동 라우팅), Web Console New Task 모달에 Type 드롭다운(`feature`/`memory_refresh`). 4채널 공통 경로.
4. **chatbot pending 주입** — chatbot은 single-shot이라 tool loop가 없는데 "pr 진행해줘" 같은 요청에서 LLM이 대상 task_id를 **추측(할루시네이션)** 하는 버그가 테스트 중 관찰됨. 근본 원인: notification 이벤트가 텔레그램 채팅창에는 표시되지만 `session_history/telegram/*.json`의 conversation_history에는 기록되지 않아 LLM이 PR 대기 task 존재를 모름. `build_system_prompt()`에서 `hub_api.pending()`을 조회해 "PR 머지 대기 / plan 승인 대기" 항목을 system_status에 덧붙이는 것으로 해결.
5. **Agent Session Reuse (Phase 2.5)** — 그동안 매 agent 호출이 cold start여서 subtask2 coder는 subtask1 coder의 **의도**("왜 이렇게 바꿨는가")를 diff와 문서 재분석으로만 추정해야 했다. `(task_id, agent_type)` 단위로 UUID를 발급해 같은 task 안의 모든 subtask/attempt가 하나의 claude 세션을 공유하게 하고, resume 세션에는 "이전 맥락은 참고용, 이번 턴 지시가 최우선" 가드를 프롬프트 앞에 자동 삽입해 판단 고착을 완화. `claude.session_reuse` 토글로 on/off.

테스트: Unit/Integration/E2E 모두 통과 (263 → 281개로 확장, memory_updater 통합 테스트 + session_reuse 테스트 추가, 누락돼 있던 `test_memory_updater_integration.py`를 `run_test.sh`에 등록).

---

## 이번 세션 커밋

**브랜치: `feature/project-long-term-memory` (PR #5 → main 머지)**

```
454cd0e chatbot: pending 항목을 system prompt에 주입
746e9ad memory_refresh 진입점 확장: chat/telegram/web 지원
062a262 memory_refresh task_type: PROJECT_NOTES.md 전체 재생성 경로 추가
5d8c1f0 프로젝트 장기 메모리: MemoryUpdater stage 도입
```

**브랜치: `feature/agent-session-reuse` (PR 전)**

```
9c55471 test 러너: 누락 테스트 등록 + project_name 언더스코어 수정
de8729d agent 세션 재사용: (task, agent_type) 단위로 claude 세션 공유
```

---

## 구현 핵심 포인트

### Long-term Memory 설계 결정

- **codebase 루트에 둔다 (docs/ 아님)** — `docs/`는 레포마다 위치가 다르고 agent가 cd `$CODEBASE_PATH` 후 실행되므로 루트가 가장 안전.
- **CLAUDE.md는 포인터 한 줄** — 본문을 여기 두면 Claude 전용이 됨. PROJECT_NOTES.md는 어떤 LLM이 읽어도 동일 의미를 가지는 플레인 문서로 유지.
- **직접 파일 수정 허용** — patch JSON으로 받지 않고 MemoryUpdater agent가 Edit/Write로 직접 수정. 모든 변경은 WFC 커밋으로 git 이력에 남으므로 감사 가능.
- **memory 커밋은 Summarizer 직전** — PR diff에 PROJECT_NOTES.md 변경이 포함되어 리뷰어가 의도를 함께 확인 가능.
- **memory_refresh 때 Planner의 역할은 "코드 변경 없음 확정"** — `subtasks: []` 반환 + branch_name만 제안. WFC의 3곳 빈-subtask 가드가 task_type==memory_refresh일 때만 통과를 허용.

### Session Reuse 설계 결정

- **키: `(task_id, agent_type)`** — subtask_id까지 세분하면 subtask 간 의도 전달이 끊기므로 task 전체를 같은 세션. 단 agent_type은 분리 (Coder/Reviewer가 같은 세션을 쓰면 역할 혼선).
- **Reviewer도 포함** — reject 이유가 누적되어 노이즈가 될 수 있지만, 다른 agent들과의 일관성 + subtask1 관찰을 subtask2 리뷰에 자연스럽게 이어 쓰는 이득이 큼.
- **Lazy UUID 발급** — WFC에서 pre-allocation 대신 `run_claude_agent.sh`가 호출 시점에 `allocate_session_id.py`로 task JSON을 lock 없이 업데이트(WFC가 같은 agent를 동시에 실행하지 않으므로 race 없음). 최소 cross-module 변경.
- **resume 프롬프트 가드** — 고착 방지 경고문을 agent prompt 파일마다 넣지 않고 `run_claude_agent.sh`가 resume일 때만 prepend. 모든 agent에 일괄 적용.

### 실환경 검증 — test-project task 00151

subtask 2개짜리 task에서:

| Agent | 호출 | 모드 | session_id |
|-------|------|------|------------|
| coder | subtask 01 attempt-1 | new | `c6832199…` |
| coder | subtask 02 attempt-1 | **resume** | `c6832199…` (동일) |
| reviewer | subtask 01 attempt-1 | new | `0618cc65…` |
| reviewer | subtask 02 attempt-1 | **resume** | `0618cc65…` (동일) |

subtask 02 coder 응답에서 발췌: `"JS는 00151-1에서 만든 playBeep()을 그대로 재사용했고, 새 함수는 작성하지 않았다"` — 이전 subtask에서 자기가 만든 함수를 세션 맥락으로 기억하고 있음(diff에서 추측한 게 아님). 부가 효과로 subtask 02 coder 호출이 `cache_read_input_tokens: 172,711`를 활용(prompt cache 대량 재사용).

---

## 다음 TODO

- **replan 승인 단계 미집행 버그** — Planner가 replan 결과를 내놓은 뒤 `human_review_policy.review_replan=true`임에도 사용자 검토 없이 바로 다음 단계로 넘어가는 것으로 관찰됨. WFC의 replan 분기(`waiting_for_human_plan_confirm` 진입 경로)와 `review_replan` 플래그 검사 누락/오용 여부 점검 필요. 재현 케이스 확보 후 수정.
- **`feature/agent-session-reuse` PR → main 머지** — 이번 세션 끝에 아직 미머지 상태.
- **장기 메모리 실데이터 누적 관찰** — `PROJECT_NOTES.md`가 여러 task를 거치며 10개짜리 "최근 변경 이력" 롤링이 의도대로 동작하는지, 아키텍처 섹션이 과도하게 커지지 않는지 중장기 관찰 필요.
- **세션 크기 모니터링** — 긴 task(많은 subtask + 여러 retry)에서 `--resume` 세션이 얼마나 커지는지, 200k 토큰 한계 근접 시 auto-compact가 의도대로 걸리는지 관찰.

---

## 관련 파일

- `scripts/init_project.py` — `generate_codebase_memory_files()` (PROJECT_NOTES.md + CLAUDE.md 템플릿)
- `scripts/workflow_controller.py` — finalize_task 내 MemoryUpdater stage, 3곳 빈-subtask 게이트
- `scripts/allocate_session_id.py` — (task, agent_type) UUID lazy 발급
- `scripts/run_claude_agent.sh` — `--session-id`/`--resume` 분기, resume 가드 prepend, memory_updater/summarizer step 재조정
- `scripts/chatbot.py` — memory_refresh 가이드, pending 주입
- `scripts/cli.py` — `submit --type`, `refresh-memory` 서브커맨드
- `scripts/hub_api/core.py`, `scripts/hub_api/protocol.py` — `submit(task_type=...)`
- `scripts/web/static/app.js` — New Task 모달 Type 드롭다운
- `config/agent_prompts/memory_updater.md` — 신규 agent prompt (incremental + full-scan 모드)
- `config/agent_prompts/planner.md` — `task_type == "memory_refresh"` 섹션
- `templates/config.yaml.template` — `claude.session_reuse: true`
- `tests/test_memory_updater_integration.py` — MemoryUpdater/memory_refresh 통합 테스트
- `tests/test_session_reuse.py` — 세션 재사용 unit 테스트
