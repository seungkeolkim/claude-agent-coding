# Agent System Architecture Specification v7

> Claude Code CLI 기반 24시간 자동 개발 시스템
> 최종 정리: 2026-04-15
> 이전 버전: `docs_history/agent-system-spec-v06.md` (v6)

---

## 문서 간 관계

| 문서 | 역할 | 참고 수준 |
|------|------|-----------|
| **`docs/agent-system-spec-v07.md` (이 문서)** | **현행 설계 명세. 모든 구현은 이 문서를 기준으로 합니다.** | **기준 문서** |
| `docs/task-lifecycle-fsm.md` | Task 상태 FSM 다이어그램 + pipeline_stage 정의 | 참고 |
| `docs/configuration-reference.md` | 4계층 설정 키 레퍼런스 | 참고 |
| `docs/telegram-setup.md` | Telegram 연동 운영 가이드 | 참고 |
| `docs_for_claude/019-handoff-*.md` | 직전 세션 핸드오프 (다음 세션 시작 시 읽기) | 참고 |
| `docs_history/handoffs/008~018-*.md` | Phase 1.5~2.3 완료 핸드오프 (아카이브, v07 §15.1에 통합됨) | 참고용 |
| `docs_history/agent-system-spec-v02~v06.md` | 이전 설계 명세 (아카이브) | 참고 불필요 |

---

## 1. System Overview

사용자가 CLI(또는 추후 메신저)로 작업을 요청하면, 복수의 agent가 순차적으로 코드 작성, 리뷰, 테스트를 수행하고 결과를 알림.
복잡한 작업은 Planner가 subtask로 분할하여 순차 실행하며, 각 subtask는 독립적으로 커밋 가능한 단위.

### 핵심 설계 원칙

1. **멀티 프로젝트 지원:** 하나의 agent-hub 인스턴스가 여러 프로젝트를 동시에 관리. 프로젝트별로 독립된 Workflow Controller 프로세스가 격리 실행.
2. **4계층 설정:** `config.yaml` (시스템) → `project.yaml` (프로젝트 정적) → `project_state.json` (프로젝트 동적) → `task.config_override` (task 일시). 뒤의 것이 앞의 것을 덮어씀.
3. **파일 기반 통신:** 모든 프로세스 간 통신은 JSON 파일 기반. task 큐는 priority별 `task_queue_{critical,urgent,default}.json` 3개 파일로 관리 (fcntl.flock). 내부 port나 소켓 없음.
4. **역할 기반 기동:** 같은 코드베이스를 실행장비/테스트장비에서 `git pull`로 동기화하되, 기동 시 role에 따라 다른 프로세스를 실행.
5. **책임 범위 기반 subtask:** 파일 격리가 아닌 `primary_responsibility`로 분할. scope 겹침 허용. `prior_changes`로 맥락 전달.
6. **CLAUDE.md는 정적 지식만:** 동적 상태는 전부 JSON 파일로 관리.
7. **테스트 선택적:** unit test, e2e test, integration test 각각 활성화/비활성화 가능. 비활성화된 agent는 pipeline에서 bypass (호출 자체 안 함).
8. **CLI 구독 기반:** Claude Code CLI(`claude -p`)를 직접 사용. API key 불필요. Pro/Max/Team/Enterprise 구독으로 동작.
9. **Usage 기반 제어:** claude 세션 사용량 threshold를 기반으로 새 task/subtask/agent 실행을 조절. 과사용 방지.
10. **SQLite 하이브리드:** Task JSON이 source of truth. SQLite(WAL)는 조회/집계용 캐시. 파일→DB 단방향 sync.
11. **Web Console 통합:** TM과 함께 기동/종료. FastAPI + Vanilla JS SPA. SSE로 실시간 업데이트.

---

## 2. Machine Roles

### 2.1 장비 구성

2026-04-16부로 **E2E는 실행장비 로컬 Docker 컨테이너에서 실행**(Playwright + MCP).
원격 Windows 테스트장비 의존이 제거되었다. 상세 설계 `docs/e2e-test-design-decision.md`.

| 구분 | 장비 | OS | 스펙 | 역할 | 가동 |
|------|------|-----|------|------|------|
| 실행장비(메인) | 장비2 — 랙서버 | Ubuntu 20.04 | i7, 32GB, A5000 | Agent Hub, 코드베이스 기동, **E2E Docker 컨테이너** | 24h |
| 실행장비(서브) | 장비4 — 구형 PC | Ubuntu 24.04 | i7-6700, 32GB, 3060 | 보조 코드베이스 기동, 보조 agent (당분간 미사용) | 24h |
| 이동용 | 장비5 — 그램 노트북 | Windows (WSL) | i7-11th, 16GB | 외부 작업용 | 이동 시 |
| ~~테스트장비~~ | ~~장비1/장비3~~ | | | ~~원격 E2E~~ (Docker 전환으로 제거) | — |

### 2.2 접속 패턴

- Claude Code CLI는 실행장비(장비2)에서 직접 실행
- E2E는 실행장비 내 Docker 컨테이너 (`agent-hub-e2e-playwright`)로 subtask 단위 spawn/destroy
- 장비2 ↔ 장비5는 SSH 가능 (이동용 장비에서 원격 확인)

### 2.3 .claude 세션 관리

- `.claude` 폴더는 실행장비(장비2)에 위치
- VS Code extension 미사용 (호스트 측에 .claude가 생기는 문제 회피)
- 각 agent는 subtask 단위로 새 세션 생성 (세션 공유 불필요)
- CLAUDE.md는 정적 프로젝트 지식만 담고, 동적 상태는 JSON 파일로 관리

---

## 3. Process Model

### 3.1 프로세스 분류

시스템에는 세 종류의 프로세스가 존재한다:

| 분류 | 프로세스 | 수명 | 개수 |
|------|----------|------|------|
| 상주 (long-running) | Task Manager | 시스템 기동~종료 | 1개 |
| 상주 (long-running) | Web Console | 시스템 기동~종료 | 1개 |
| 상주 (long-running) | Workflow Controller | task 실행 중 | 프로젝트당 0~1개 |
| 일회성 (per-invocation) | Agent (Planner, Coder 등) | `claude -p` 실행~종료 | subtask당 1개씩 |

### 3.2 프로세스 계층

```
[상주] Task Manager (1개)          [상주] Web Console (1개, FastAPI)
  │                                   │
  │                                   ├── SQLite DB (조회 캐시)
  │                                   ├── FileSyncer (mtime 폴링 2초)
  │                                   └── SSE event stream
  │
  ├── [일시] Workflow Controller — project A, task 00042
  │     ├── [일회성] claude -p planner ...     (끝나면 종료)
  │     ├── [일회성] claude -p coder ...       (끝나면 종료)
  │     ├── [일회성] claude -p reviewer ...    (끝나면 종료)
  │     └── ...
  │
  ├── [일시] Workflow Controller — project B, task 00010
  │     ├── [일회성] claude -p planner ...
  │     └── ...
  │
  └── (task가 없으면 WFC도 없음)
```

### 3.3 Task Manager의 역할

Task Manager는 시스템의 상주 프로세스로, 프로젝트 감시 및 WFC 라이프사이클 관리:

- **프로젝트 감시:** `projects/` 디렉토리를 폴링(기본 2초)하여 새 프로젝트 자동 감지
- **task 큐 관리:** priority queue(`task_queue_{critical,urgent,default}.json`) peek/pop → WFC spawn (critical > urgent > default, 각 priority 내 id순)
- **task 큐 블로킹:** `wait_for_prev_task_done=true`일 때 이전 task 미완료 시 다음 task spawn 차단. `waiting_for_human_pr_approve` 포함 (`require_human` 전략 시 다음 task 차단)
- **WFC 라이프사이클 관리:** spawn, 완료 감지, 정리
- **PR Watcher 스레드:** 60초 주기로 `waiting_for_human_pr_approve` task의 PR 상태를 `gh pr view`로 폴링. MERGED → `completed`, CLOSED → `failed` 자동 전이
- **system-wide 현황 조회:** `projects/*/project_state.json`을 읽어 전체 현황 조합
- **시그널 처리:** SIGTERM(graceful), SIGUSR1(force) 종료 지원. graceful shutdown 시 실행 중인 WFC에 SIGTERM 전파 후 30초 대기, 미응답 시 SIGKILL
- **WFC resume:** 기동 시 `resume_waiting_tasks()`로 중단 지점(human review/subtask loop 등)에 남은 task를 재개. 기존 task를 이어받아 WFC를 재spawn

### 3.4 Workflow Controller의 역할

각 WFC는 하나의 task를 전담하는 일시 프로세스:

- **파이프라인 실행:** Planner → human review 대기 → subtask loop → Summarizer → PR
- **설정 해소:** 4계층 config merge → effective config 계산 (resolve_effective_config)
- **human review:** review_plan/review_replan 설정에 따라 사용자 승인 대기 (폴링 기반)
- **명령 수신:** `commands/` 디렉토리의 cancel 명령 파일 감시
- **상태 보고:** `project_state.json` 업데이트 (TM이 읽기)
- **한도 체크:** 매 agent 호출 전 counters vs limits 비교
- **git 작업:** branch 생성(`base_branch` 기준), subtask별 commit+push, Summarizer 후 PR 생성(`pr_target_branch` 대상), `merge_strategy`에 따라 처리:
    - `auto_merge`: `gh pr merge`로 자동 머지 → `completed`
    - `pr_and_continue`: PR 생성만, task 즉시 `completed` (다음 task 진행 가능)
    - `require_human` (기본값): PR 생성 → `waiting_for_human_pr_approve` (다음 task 차단)

### 3.5 TM ↔ WFC 통신

port나 소켓 없이, 파일 기반으로 통신:

```
TM → WFC 명령 전달:
  projects/{name}/commands/{명령}.command 파일 생성
  예) pause.command, resume.command, cancel-00042.command

WFC → TM 보고:
  projects/{name}/project_state.json 업데이트 (TM이 읽기)

TM ← task 수신:
  projects/{name}/task_queue_{critical,urgent,default}.json peek/pop (TM 폴링)
  hub_api.submit() 시 priority queue 파일에 id append (fcntl.flock)
```

### 3.6 hub_api — 공통 인터페이스 계층

CLI, 메신저, 웹 등 모든 프론트엔드가 공유하는 Python 라이브러리 (`scripts/hub_api/`):

- **task 관리:** submit, list, cancel, resubmit, get_task, get_plan
- **human interaction:** pending, approve, reject, feedback, complete_pr_review, merge_pr, close_pr
- **설정 변경:** config (project_state.json의 overrides 동적 변경)
- **프로세스 제어:** pause, resume
- **프로젝트 관리:** create_project, close_project, reopen_project
- **시스템 상태:** status (TM + 프로젝트별 상태 조합)

CLI(`scripts/cli.py`), Chatbot(`scripts/chatbot.py`), Web Console(`scripts/web/server.py`)이 모두 같은 hub_api를 경유.
Web Console은 FastAPI가 hub_api를 import하여 `dispatch()`로 mutation을 처리하고, SQLite DB에서 조회를 수행.

### 3.7 프로젝트 내 task 실행 규칙

**하나의 프로젝트 내에서 task는 직렬 실행된다.** 같은 repo에서 병렬 feature 작업이 필요하면, 별도 clone + 별도 project로 구성한다.

**task 큐 블로킹:** `wait_for_prev_task_done=true` (기본값)이면, 이전 task가 미완료 상태(`in_progress`, `planned`, `running`, `waiting_for_human_plan_confirm`, `needs_replan`, `waiting_for_human_pr_approve`)일 때 다음 task를 spawn하지 않는다. `require_human` 전략에서는 `waiting_for_human_pr_approve` 상태가 다음 task를 차단한다. `pr_and_continue`는 즉시 `completed`가 되므로 차단하지 않는다. 프로젝트별로 override 가능.

---

## 4. Agent Catalog

### 4.1 Orchestration Layer (장비2 상주)

#### Task Manager

- **역할:** 프로젝트 감시 + WFC 라이프사이클 관리 + PR 상태 자동 감지
- **입력:** priority queue(`task_queue_*.json`) peek
- **출력:** WFC spawn/kill, project_state.json 갱신
- **상세:**
  - `projects/` 디렉토리 폴링 (기본 2초)
  - 프로젝트당 WFC 1개만 유지 (이미 실행 중이면 완료 대기)
  - priority queue 순회: critical → urgent → default. cancelled/failed id는 자동 skip + 정리
  - 기동 시 레거시 `.ready` 파일을 default queue로 이주 (backward compat)
  - task 큐 블로킹 (wait_for_prev_task_done). `waiting_for_human_pr_approve` 포함
  - PID 파일 기반 프로세스 추적 (.pids/)
  - 시그널 핸들링: SIGTERM (graceful), SIGUSR1 (force kill)
  - dummy 모드 지원 (WFC를 --dummy로 실행)
  - **PR Watcher 스레드:** 60초 주기 백그라운드 스레드. `gh pr view --json state`로 MERGED/CLOSED 감지 → task 자동 전이 + 알림 발행

#### Workflow Controller (task당 1개)

- **역할:** 내부 파이프라인 제어
- **입력:** task JSON, effective config
- **출력:** agent 기동, git branch/commit/PR, project_state.json 업데이트
- **상세:**
  - resolve_effective_config()로 4계층 설정 merge
  - determine_pipeline()으로 testing 설정 기반 agent 목록 결정
  - human_review_policy에 따른 Planner/Replan 후 승인 대기
  - auto_approve_timeout_hours 초과 시 자동 승인
  - cancel 명령 파일 감시 (commands/ 디렉토리)
  - git: branch 생성, subtask 커밋, push, PR 생성/머지
  - ensure_gh_auth(): 매 git 작업 전 gh CLI 인증 + repo 권한 확인

### 4.2 Planning Layer

#### Planner Agent

- **역할:** 코드베이스 분석 및 subtask 분할
- **입력:** task 요구사항 + 첨부 이미지 + 코드베이스 접근
- **출력:** plan JSON (subtask 배열, 의존관계, guidance)
- **상세:**
  - Claude Code 세션으로 코드베이스를 탐색하며 아키텍처 파악
  - 첨부된 UI 목업, 아키텍처 다이어그램, 데이터 구조 이미지를 분석
  - 기능 단위로 subtask 분할 (책임 범위 기반, 파일 격리 아님)
  - 각 subtask에 primary_responsibility와 guidance 부여
  - E2E가 필요한 subtask 식별 → 최소 UI 포함 지시
  - re-plan 요청 시 완료된 subtask의 changes_made를 참고하여 남은 계획 재구성

### 4.3 Worker Layer (subtask당 순차 실행)

#### Coder Agent
- **역할:** 코드 작성
- **입력:** subtask 정의 + prior_changes + guidance + 첨부 이미지 + (재시도 시) `retry_mode` / `attempt_history` / `subtask_start_sha` / `latest_instructions`
- **출력:** 코드 변경, `changes_made`, `intent_report` (필수 — `what_changed` / `why` / `review_focus` / `known_concerns`)
- **재시도 모드:**
  - `reset`: WFC가 worktree를 `subtask_start_sha`로 git reset --hard한 상태. 처음부터 다시 구현
  - `continue`: 이전 attempt의 변경이 worktree에 그대로 남아 있음. `git diff {subtask_start_sha}`로 상태 확인 후 `latest_instructions`만 덧붙임 (중복 추가 금지)
- **금지:** git commit/push/branch 전환 (WFC가 commit 전담, Coder가 commit해도 WFC가 soft reset으로 되돌림), 서버 기동, 패키지 설치

#### Review Agent
- **역할:** 코드 리뷰 + retry 모드 판정
- **입력:** Coder의 `intent_report` + `git diff {subtask_start_sha}` (Reviewer가 직접 실행해 1차 근거로 사용)
- **출력 스키마 (필수):**
  - 승인: `{action: "approved", current_state_summary}`
  - 거절: `{action: "rejected", retry_mode: "continue"|"reset", current_state_summary, what_is_wrong, what_should_be, actionable_instructions: [...], feedback}`
- **판정 가이드:**
  - `reset`: 방향이 잘못됐거나, 큰 폭 재작업이 필요하거나, 중복 추가/엉뚱한 파일 수정 등 "엎고 다시" 케이스
  - `continue`: 핵심 동작은 맞고 정리/주석 제거/누락된 한 줄 정도면 충분한 케이스
- **WFC 동작:** 스키마 위반 시 1회 재요청, 그래도 실패하면 `reset`으로 강등하여 진행

#### Setup Agent
- **역할:** 환경 구성 및 프로그램 기동
- **입력:** 현재 코드베이스 상태
- **출력:** 기동 성공/실패 상태

#### Unit Test Agent
- **역할:** 코드 레벨 테스트
- **입력:** 기동된 환경 + testing.unit_test 설정
- **출력:** 테스트 결과 (pass/fail)

#### E2E Test Agent
- **위치:** 실행장비 내 Docker 컨테이너 (Playwright + MCP 통합 이미지 `agent-hub-e2e-playwright`)
- **역할:** 브라우저 기반 통합 테스트. subtask당 컨테이너 spawn/destroy
- **입력:** `## E2E 실행 설정` 섹션으로 주입되는 mode/test_source/base_url/tests_dir/artifacts_dir/container 등 + test_accounts + 첨부 목업
- **출력:** `{action, overall_result, source_results, test_results[], summary}` (스키마는 `config/agent_prompts/e2e_tester.md` 참조)
- **4-Phase 흐름:**
  1. **Phase 1 (탐색, MCP):** Playwright MCP 도구로 실제 앱을 조작해 DOM/selector/시나리오 확보 (test_source=dynamic/both)
  2. **Phase 2 (작성):** `{tests_dir}/*.spec.ts`를 TypeScript로 작성. 볼륨 마운트로 컨테이너의 `/e2e/tests`와 즉시 동기화
  3. **Phase 3 (검증, docker exec):** Bash tool로 `scripts/e2e_container_runner.sh exec-test` 호출 → 결정적 실행. `{artifacts_dir}/report.json`에 JSON 리포터 결과 기록. 최종 pass/fail은 오직 이 결과로 판정
  4. **Phase 4 (재탐색, 실패 시):** MCP로 실패 시점 재현, Coder용 `error_detail`/`coder_guidance` 생성
- **판정 규칙:** test_source=both는 AND 판정 (dynamic/static 둘 다 pass여야 overall pass). 상세 `docs/e2e-test-design-decision.md` §4.6-5
- **MCP 로그 보존:** `machines.tester.mcp.log_retention = on-failure`(기본) | always | never. run_claude_agent.sh trap이 `docker logs`를 `{artifacts_dir}/mcp-session.log`로 저장
- **레거시:** `scripts/e2e_watcher.sh`는 DEPRECATED (원격 SSH sentinel 구조). 참고용으로만 유지

#### Reporter Agent
- **역할:** 결과 종합 및 판정
- **입력:** 모든 테스트 결과
- **출력:** pass/fail 판정, 버그 리포트, changes_made 기록

#### Memory Updater Agent
- **역할:** 완료된 task의 변경을 바탕으로 codebase 루트의 `PROJECT_NOTES.md`(장기 메모리)를 증분 갱신
- **입력:** plan.json, git diff, 현재 `PROJECT_NOTES.md`
- **출력:** `{action, updated, sections_changed, rationale}` + (updated=true면) 파일 직접 수정
- **위치:** Summarizer 직전에 실행. 변경된 `PROJECT_NOTES.md`는 WFC가 `[{task_id}] memory: PROJECT_NOTES.md 갱신` 커밋으로 묶어 같은 PR에 포함시킨다
- **실패 정책:** 실패해도 PR 생성은 차단하지 않음 (경고만)

#### Summarizer Agent
- **역할:** 완료된 task의 작업 요약 및 PR 메시지 생성
- **입력:** plan.json, git diff (MemoryUpdater의 `PROJECT_NOTES.md` 변경 포함), 완료된 subtask 목록
- **출력:** PR 제목/본문 + task 요약

---

## 5. Workflow

### 5.1 전체 사이클

```
 1. 사용자: CLI submit (또는 메신저, priority 지정 가능)
    → hub_api: task JSON 생성 + task_queue_{priority}.json에 id append (flock)
    → TM: priority queue peek → 큐 블로킹 확인 → WFC spawn

 2. WFC: 4계층 설정 merge → effective config 생성
    → Planner Agent 기동

 3. Planner Agent → plan 생성 (subtask 배열 + branch_name 제안)

 3a. [review_plan=true면]
    → WFC: task status → waiting_for_human_plan_confirm
    → 사용자 승인 대기 (폴링 10초 간격)
    → approve → 계속 / modify → replan / cancel → 종료
    → auto_approve_timeout 초과 시 자동 승인

 3b. WFC: git branch 생성 → feature/{task_id}-{영문설명}

 4. Subtask Loop
    ┌──────────────────────────────────────────────────────────┐
    │  시작 전: determine_pipeline() → agent 목록 결정          │
    │                                                          │
    │  4a. Coder Agent: 코드 작성                               │
    │  4b. Review Agent: 코드 리뷰 (거절 시 Coder 루프백)        │
    │  [testing 활성화 시]                                      │
    │  4c. Setup Agent: 환경 구성                               │
    │  4d. Unit Test Agent (enabled일 때)                       │
    │  4e. E2E Test Agent (enabled일 때)                        │
    │  4f. Reporter Agent: 결과 종합                            │
    │                                                          │
    │  → 통과: subtask 커밋 → 다음 subtask                      │
    │  → 실패 (retry 이내): Coder 루프백                        │
    │  → 실패 (retry 초과): re-plan 요청                        │
    │    → [review_replan=true면] 사용자 승인 대기               │
    │  → 실패 (re-plan 초과): 에스컬레이션                       │
    │                                                          │
    │  [testing 전부 disabled면]                                │
    │  Review 승인 → 바로 커밋 → 다음 subtask                   │
    └──────────────────────────────────────────────────────────┘

 5. Integration Test (모든 subtask 완료 후, enabled일 때)

 6. Memory Updater Agent → codebase `PROJECT_NOTES.md` 증분 갱신
    → 변경 있을 시 WFC가 `[{task_id}] memory: ...` 커밋 생성 (push X)

 7. Summarizer Agent → PR title/body + task_summary 생성
    (이 시점의 diff에는 6에서 만든 PROJECT_NOTES.md 변경이 포함됨)

 8. WFC → PR 생성
    → [merge_strategy=auto_merge] gh pr merge → completed
    → [merge_strategy=pr_and_continue] task 즉시 completed (PR 운명 독립)
    → [merge_strategy=require_human] task status → waiting_for_human_pr_approve
```

### 5.2 Pipeline 구성 결정 로직

매 subtask 시작 전에 WFC가 effective config의 testing 설정을 읽고, 해당 subtask에서 실행할 agent 목록을 생성한다. 비활성화된 agent는 pipeline에 포함하지 않는다 (bypass).

```
전부 disabled:  [coder, reviewer] → 커밋
unit만 enabled: [coder, reviewer, setup, unit_tester, reporter] → 커밋
e2e만 enabled:  [coder, reviewer, setup, e2e_tester, reporter] → 커밋
전부 enabled:   [coder, reviewer, setup, unit_tester, e2e_tester, reporter] → 커밋
```

### 5.3 Subtask 간 컨텍스트 전달

각 subtask는 이전 subtask의 결과 위에서 작업. 파일 scope 겹침 허용.

```
Subtask 1 완료 → 커밋
  │  changes_made: ["api/auth/login.py 생성", "src/pages/login/index.tsx 최소 구현"]
  ▼
Subtask 2 시작
  Coder 컨텍스트: subtask 2의 primary_responsibility + guidance + prior_changes
```

### 5.4 Re-plan

Reporter가 subtask retry 한도를 초과했다고 판단하면 re-plan 요청. Replan 시 task 브랜치를 폐기하고 base_branch에서 재생성하여 이전 plan의 산출물을 깨끗이 제거한다 (local only — 어차피 push는 PR 생성 시점까지 유예되므로 remote 영향 없음).

```
Reporter → task status: "needs_replan"
  → WFC → git_wipe_and_recreate_task_branch(branch, base_branch)
  → completed_subtasks = []
  → emit_notification(event_type="replan_started")
  → Planner Agent 재기동
  → [review_replan=true면] 사용자 승인 대기
  → 새 plan으로 subtask loop 재개
```

### 5.5 루프백 규칙

| 실패 지점 | 대상 | 전달 내용 |
|-----------|------|-----------|
| Review 거절 | Coder | `retry_mode` (continue/reset) + `actionable_instructions` + `attempt_history` |
| Setup 실패 | Coder | 빌드/기동 에러 로그 |
| Unit Test 실패 | Coder | 실패 테스트명 + 에러 메시지 |
| E2E Test 실패 | Coder | 실패 시나리오 + 스크린샷 경로 |
| Reporter: 재시도 | Coder | 종합 피드백 |
| Reporter: re-plan | Planner | 실패 사유 + 전체 히스토리 |
| Reporter: 포기 | Task Manager | 에스컬레이션 |

---

## 6. Communication Structure

### 6.1 실행장비 내부 (같은 머신)

로컬 파일 기반. Atomic write: tmp 파일 → os.replace(). Priority queue 조작은 fcntl.flock으로 동시성 제어.

### 6.2 TM ↔ WFC 통신

| 경로 | 방식 | 트리거 |
|------|------|--------|
| TM → WFC | commands/ 디렉토리에 .command 파일 생성 | WFC 폴링 |
| WFC → TM | project_state.json 업데이트 | TM 폴링 |
| TM ← task | task_queue_{critical,urgent,default}.json | TM 폴링 (peek/pop, flock) |

### 6.3 실행장비 내 E2E Docker 컨테이너 통신 (2026-04-16 전환)

구(舊) 원격 테스트장비 + SSH sentinel 구조를 대체. 모든 E2E는 실행장비 로컬 Docker.

| 경로 | 방식 | 트리거 |
|------|------|--------|
| run_claude_agent.sh → 컨테이너 기동 | `docker run -d -p 0:8931 --network=host ...` via `scripts/e2e_container_runner.sh start` | e2e_tester 분기 진입 시 |
| Claude agent → MCP 서버 | HTTP/SSE (`.mcp.json`에 주입된 동적 호스트 포트) | Phase 1/4 탐색 시 |
| Claude agent → Playwright test | Bash tool로 `e2e_container_runner.sh exec-test` 호출 → `docker exec ... npx playwright test` | Phase 3 검증 시 |
| 컨테이너 → 호스트 artifacts | volume mount (`{artifacts_dir}:/e2e/test-results`) | report.json / screenshots / traces / video 저장 |
| 호스트 → 컨테이너 tests | volume mount (`{tests_dir}:/e2e/tests`) | Phase 2에서 작성한 `.spec.ts` 즉시 반영 |
| run_claude_agent.sh trap → 컨테이너 정리 | `e2e_container_runner.sh stop` (docker stop/rm + 임시 .mcp.json 삭제 + MCP 로그 보존 정책 적용) | Claude 프로세스 EXIT |

컨테이너 이름: `e2e-{project}-{task}-{subtask_seq}`. 포트는 Docker 동적 할당(`-p 0:8931`). 상세 `docs/e2e-test-design-decision.md` §5.3.

### 6.4 레거시 원격 E2E 통신 (DEPRECATED)

~~실행장비 ↔ 테스트장비 sentinel 기반 구조~~. `scripts/e2e_watcher.sh`는 레거시 참조용으로만 유지.

---

## 7. Configuration

### 7.1 4계층 설정 우선순위

```
config.yaml (시스템 기본값)
  → projects/{name}/project.yaml (프로젝트 정적 설정)
    → projects/{name}/project_state.json의 overrides (프로젝트 동적 설정)
      → tasks/{id}.json의 config_override (task 단위 일시 변경)
```

뒤의 것이 앞의 것을 재귀적으로 deep merge하여 덮어씀.

### 7.2 config.yaml (시스템 설정)

```yaml
machines: { ... }

claude:
  planner_model: "opus"
  coder_model: "sonnet"
  reviewer_model: "opus"
  # ... 기타 agent 모델
  max_turns_per_session: 50
  usage_thresholds:
    new_task: 0.70
    new_subtask: 0.80
    new_agent_stage: 0.90
  usage_check_interval_seconds: 60

default_limits:
  max_subtask_count: 5
  max_retry_per_subtask: 3
  max_replan_count: 2
  max_total_agent_invocations: 30
  max_task_duration_hours: 4

default_human_review_policy:
  review_plan: true
  review_replan: true
  review_before_merge: false
  auto_approve_timeout_hours: 24

default_task_queue:
  wait_for_prev_task_done: true

logging: { level: "info", archive_completed_tasks: true, keep_session_logs: true }

notification:
  channel: "cli"  # Phase 1.4+: "slack" | "telegram"
```

### 7.3 project.yaml (프로젝트 정적 설정)

프로젝트별 고유 설정. `project`, `codebase`, `git`, `testing` 필수. 나머지는 시스템 기본값 override용으로 선택적 작성.

override 가능 섹션: `limits`, `claude`, `human_review_policy`, `task_queue`, `notification`

### 7.4 project_state.json (프로젝트 동적 설정)

사용자가 CLI `config` 명령으로 동적 변경하는 runtime 상태.

```json
{
  "project_name": "test-web-service",
  "lifecycle": "active",
  "status": "idle | running | waiting_for_human_plan_confirm",
  "current_task_id": null,
  "last_updated": "2026-04-03T00:00:00Z",
  "overrides": { ... },
  "update_history": [ ... ]
}
```

**lifecycle 값:**

| lifecycle | 의미 | task 제출 | 기본 목록 |
|-----------|------|:---------:|:---------:|
| `active` | 정상 운영 (기본값) | O | 표시 |
| `closed` | 종료 (모든 task 종료 상태일 때만 전환 가능) | X | `--all` / `include_closed`로만 표시 |

- `close_project`: 모든 task가 종료 상태(completed/cancelled/failed/escalated)일 때만 가능
- `reopen_project`: closed → active 재전환
- syncer가 폴더 소실 감지 시 DB에서 자동 closed 처리

### 7.5 task.config_override

task JSON 내부의 `config_override` 필드. 해당 task에만 적용.

### 7.6 설정 해소 (resolve_effective_config)

WFC가 파이프라인 시작 전에 수행하는 4계층 deep merge:

```python
def resolve_effective_config(config_yaml, project_yaml, project_state, task_json):
    # 1. config.yaml에서 default_* 접두사 키를 매핑
    # 2. project.yaml로 deep merge
    # 3. project_state.json의 overrides로 deep merge
    # 4. task.config_override로 deep merge
    # → effective config 반환
```

---

## 8. Data Structures

### 8.1 Task (projects/{name}/tasks/{id}-{제목}.json)

```json
{
  "task_id": "00042",
  "project_name": "test-web-service",
  "title": "로그인 기능 구현",
  "description": "...",
  "submitted_via": "cli",
  "submitted_at": "2026-04-01T09:00:00Z",
  "status": "in_progress",
  "branch": "feature/00042-login-implementation",
  "attachments": [ ... ],
  "plan_version": 1,
  "current_subtask": "00042-2",
  "completed_subtasks": ["00042-1"],
  "counters": {
    "total_agent_invocations": 12,
    "replan_count": 0,
    "current_subtask_retry": 1
  },
  "config_override": {},
  "human_interaction": null,
  "mid_task_feedback": [],
  "summary": "...",
  "pr_url": "..."
}
```

**status 값:** `submitted` → `planned` → `waiting_for_human_plan_confirm` → `in_progress` → `waiting_for_human_pr_approve` → `completed` / `needs_replan` / `escalated` / `failed` / `cancelled`

> Task 상태 전이의 전체 FSM 다이어그램: `docs/images/task-lifecycle-fsm.md` 참고

**pipeline_stage (in_progress 세부 단계):** `planner` → `plan_review` → `git_branch` → `coder` → `reviewer` → `memory_updater` → `summarizer` → `git_push` → `pr_create` → `finalizing` → `done`

**failure_reason:** 실패 시 원인 기록 (Planner 실패, git push 실패, PR 생성 실패 등)

### 8.2 Human Interaction (task JSON 내부)

```json
{
  "human_interaction": {
    "type": "plan_review | replan_review | merge_review | escalation",
    "message": "Plan을 확인해주세요. subtask 3개 생성됨.",
    "payload_path": "tasks/00042/plan.json",
    "options": ["approve", "modify", "cancel"],
    "requested_at": "...",
    "response": {
      "action": "approve | modify | cancel",
      "message": "...",
      "attachments": [],
      "responded_at": "..."
    }
  }
}
```

WFC가 `response` 필드를 폴링(10초 간격)하여 사용자 응답 감지.
hub_api의 `approve`/`reject` 명령이 이 필드를 채움.

### 8.3 Plan, Subtask State, E2E Handoff/Result

v2와 동일. 상세는 `docs_history/003-agent-system-spec-v2.md` 섹션 8.2~8.5 참고.

### 8.4 Subtask 런타임 컨텍스트 (재시도 루프)

Coder/Reviewer 호출 직전에 WFC가 subtask JSON에 주입하는 휘발성 필드:

```json
{
  "subtask_id": "00042-1",
  "subtask_start_sha": "bcc16257...",
  "retry_mode": "continue" | "reset" | null,
  "latest_instructions": "...",
  "attempt_history": [
    {
      "attempt": 1,
      "coder_intent_report": { "what_changed": "...", "why": "...", "review_focus": [...], "known_concerns": [...] },
      "reviewer_feedback":   { "retry_mode": "continue", "current_state_summary": "...", "what_is_wrong": "...", "what_should_be": "...", "actionable_instructions": [...], "feedback": "..." }
    }
  ]
}
```

- `subtask_start_sha`는 WFC 메모리에서 캡처한 값을 그때그때 주입 (영속할 가치 없음 — WFC 재기동 시 attempt 1로 다시 시작하면 됨).
- `attempt_history`는 attempt마다 누적. 다음 Coder/Reviewer가 직전 attempt의 의도와 거절 사유를 그대로 본다.
- Reviewer는 `git diff {subtask_start_sha} -- .`를 직접 실행해 1차 근거로 사용한다 — `coder_intent_report`와 diff가 다르면 diff를 신뢰.

### 8.5 Commit / Push 정책

- **Commit**: Reviewer가 `approved`한 시점에만 WFC가 worktree를 commit. Coder가 몰래 commit해도 WFC가 `git_soft_reset_if_moved()`로 되돌림.
- **Push**: subtask 단위로 push하지 않음. PR 생성 직전에 task 브랜치 이름을 명시해 단 1회 push (`git push --set-upstream origin {task_branch}`). 한 task = 한 push 정책으로 cross-project 오염 차단 + 잘못된 중간 상태 외부 노출 차단.
- **Replan 시**: task 브랜치를 `git_wipe_and_recreate_task_branch()`로 폐기 후 base_branch에서 재생성 (local only).

---

## 9. Safety Limits & Usage Control

### 9.1 Safety Limits

| 제한 | 기본값 | 초과 시 |
|------|--------|---------|
| max_subtask_count | 5 | Planner 에스컬레이션 |
| max_retry_per_subtask | 3 | re-plan 요청 |
| max_replan_count | 2 | 에스컬레이션 |
| max_total_agent_invocations | 30 | 강제 중단 |
| max_task_duration_hours | 4 | 강제 중단 |

모든 제한값은 4계층 설정으로 override 가능.

### 9.2 Usage Threshold 기반 실행 제어

| 실행 레벨 | threshold 기본값 |
|-----------|----------------|
| 새 task 시작 | 70% |
| 새 subtask 시작 | 80% |
| 다음 agent stage 호출 | 90% |

threshold 초과 시 `usage_check_interval_seconds`마다 재확인하며 대기.

---

## 10. Directory Structure

### 10.1 agent-hub 레포

```
claude-agent-coding/
├── config.yaml                         # 시스템 설정 (gitignored)
├── templates/
│   ├── config.yaml.template            # 시스템 설정 템플릿
│   └── project.yaml.template           # 프로젝트 설정 템플릿
├── create_config.sh                    # 템플릿 → config.yaml 생성
├── run_agent.sh                        # CLI 진입점: run, pipeline, submit, list, approve 등
├── run_system.sh                       # 시스템 관리: start, stop, status
├── activate_venv.sh                    # venv 활성화 스크립트
├── requirements.txt
├── CLAUDE.md
├── README.md
│
├── scripts/
│   ├── task_manager.py                 # Task Manager 상주 프로세스
│   ├── workflow_controller.py          # Workflow Controller
│   ├── chatbot.py                      # Chatbot 대화형 인터페이스 (Phase 1.5)
│   ├── cli.py                          # CLI 프론트엔드 (argparse → hub_api)
│   ├── init_project.py                 # 대화형 프로젝트 초기화
│   ├── run_claude_agent.sh             # Claude Code 세션 기동 래퍼
│   ├── check_safety_limits.py          # Safety limits 체크
│   ├── e2e_watcher.sh                  # 테스트장비용 E2E 감시 (Phase 1.3)
│   │
│   ├── hub_api/                        # 공통 인터페이스 라이브러리
│   │   ├── __init__.py                 # 패키지 exports
│   │   ├── core.py                     # HubAPI 클래스 (submit, approve, reject 등)
│   │   ├── models.py                   # 데이터 모델 (SubmitResult, TaskSummary 등)
│   │   └── protocol.py                 # Protocol layer (Request/Response, dispatch)
│   │
│   └── web/                            # Web Monitoring Console (Phase 2.0)
│       ├── __init__.py
│       ├── server.py                   # FastAPI 앱, 라우트, SSE, Chat API, PR 비동기
│       ├── web_chatbot.py              # ChatProcessor: Popen 기반 채팅 엔진 (cancel+merge, confirmation)
│       ├── db.py                       # SQLite 스키마, CRUD
│       ├── syncer.py                   # 파일→DB sync 엔진
│       ├── static/
│       │   ├── app.js                  # SPA 프론트엔드
│       │   └── style.css               # 다크 테마 스타일
│       └── templates/
│           └── index.html              # SPA 셸 (Jinja2)
│
├── config/
│   └── agent_prompts/                  # 8개 agent 프롬프트
│       ├── planner.md
│       ├── coder.md
│       ├── reviewer.md
│       ├── setup.md
│       ├── unit_tester.md
│       ├── e2e_tester.md
│       ├── reporter.md
│       └── summarizer.md
│
├── docs/
│   └── configuration-reference.md
│
├── session_history/                    # Chatbot 세션 이력 (gitignored)
│   └── chatbot/
│       └── {session_id}.json
│
├── docs_for_claude/                    # 활성 핸드오프 (미구현 작업, 비어있을 수 있음)
│
├── data/                               # 런타임 데이터 (gitignored)
│   └── ai_agent_coding.db             # SQLite DB (조회 캐시)
│
├── docs_history/                       # 이전 버전/완료 핸드오프 아카이브
│   ├── agent-system-spec-v02.md ~ v06.md  # 이전 설계 명세
│   └── handoffs/                       # 완료 핸드오프 (v07 §15.1에 통합됨)
│       ├── 008-handoff-phase-1.5-complete.md
│       ├── 009-handoff-phase-1.6-chatbot-usability.md
│       ├── 010-handoff-phase-2.0-web-console.md
│       ├── 011-handoff-phase-2.1.md
│       ├── 012-handoff-phase-2.1-pr-actions.md
│       └── 013-handoff-phase-2.0-web-chat.md
│
└── projects/                           # 프로젝트별 디렉토리 (runtime, gitignored)
    └── {name}/
        ├── project.yaml                # git 관리
        ├── project_state.json          # 동적 상태
        ├── task_queue_critical.json    # priority queue (id 배열, flock)
        ├── task_queue_urgent.json
        ├── task_queue_default.json
        ├── tasks/                      # task/subtask JSON
        ├── handoffs/                   # E2E 요청/결과
        ├── commands/                   # TM → WFC 명령 전달
        ├── attachments/                # 첨부파일
        ├── logs/                       # agent 실행 로그
        └── archive/                    # 완료된 task 아카이브
```

---

## 11. Web Layer (Phase 2.0)

### 11.1 아키텍처

```
Browser (SPA)
  ↕ HTTP / SSE
FastAPI (scripts/web/server.py)
  ├── POST /api/dispatch → hub_api dispatch() → 파일 mutation → syncer → DB
  ├── GET /api/* → SQLite DB 직접 조회 (빠른 읽기)
  └── GET /api/events → SSE 스트림 (syncer on_change → event queue)

FileSyncer (백그라운드 스레드, 2초 폴링)
  └── 파일 mtime 비교 → DB upsert → SSE 이벤트 발행
```

### 11.2 SQLite 하이브리드

- **Source of truth:** Task JSON 파일 (`projects/{name}/tasks/*.json`)
- **캐시:** SQLite (WAL 모드) — 조회/집계/필터 전용
- **Sync 방향:** 파일 → DB (단방향). 웹의 mutation은 dispatch() → 파일 수정 → sync → DB
- **DB 위치:** `data/ai_agent_coding.db` (config.yaml `web.db_path`)

### 11.3 테이블 구조

| 테이블 | 역할 | PK |
|--------|------|-----|
| projects | 프로젝트 상태 캐시 | name |
| tasks | task 메타데이터 캐시 (pipeline_stage, failure_reason 포함) | (project, task_id) |
| notifications | 알림 캐시 | id (auto) |
| chatbot_sessions | 세션 메타데이터 | session_id |
| schema_version | 스키마 버전 | version |

### 11.4 실시간 업데이트

- SSE (Server-Sent Events): `/api/events`
- 이벤트 타입: `task_updated`, `project_updated`, `notification`
- FileSyncer가 변경 감지 시 event queue에 push → SSE로 브라우저에 전달
- 5초 heartbeat로 연결 유지

### 11.5 설정 (config.yaml)

```yaml
web:
  port: 9880                          # 웹 콘솔 포트
  db_path: "data/ai_agent_coding.db"  # SQLite DB 경로
```

### 11.6 시스템 통합

- `run_system.sh start` → TM + Web Console 동시 백그라운드 기동
- `run_system.sh stop` → Web Console 종료 → TM 종료
- `run_system.sh status` → TM + Web Console 상태 표시
- 로그: `logs/web_console_chat.log`
- PID: `.pids/web_console_chat.{PID}.pid`

---

## 12. CLI Interface

### 12.1 시스템 관리 (run_system.sh)

```bash
./run_system.sh start [--dummy]    # Task Manager + Web Console 백그라운드 실행
./run_system.sh stop [--force]     # Task Manager + Web Console 종료
./run_system.sh status             # 시스템 상태 출력 (TM + Web Console)
```

### 12.2 Task 관리 (run_agent.sh)

```bash
# task 제출
./run_agent.sh submit --project <name> --title "제목" [--description "설명"] [--attach 파일]

# task 조회
./run_agent.sh list [--project <name>] [--status <status>]

# human interaction
./run_agent.sh pending [--project <name>]
./run_agent.sh approve <task_id> --project <name> [--message "코멘트"]
./run_agent.sh reject <task_id> --project <name> --message "사유"
./run_agent.sh feedback <task_id> --project <name> --message "피드백"

# PR 리뷰 완료 (상태 수동 반영)
./run_agent.sh complete-pr-review <task_id> --project <name> --result merged|rejected [--message "코멘트"]

# PR 직접 머지/닫기 (gh CLI 실행)
./run_agent.sh merge-pr <task_id> --project <name> [--message "코멘트"]
./run_agent.sh close-pr <task_id> --project <name> [--message "코멘트"]

# 설정 동적 변경
./run_agent.sh config --project <name> --set "key=value"

# 제어
./run_agent.sh pause --project <name> [<task_id>]
./run_agent.sh resume --project <name> [<task_id>]
./run_agent.sh cancel <task_id> --project <name>

# agent 수동 실행 (디버깅용)
./run_agent.sh run <agent_type> --project <name> --task <id> [--subtask <id>] [--dry-run] [--dummy]
./run_agent.sh pipeline --project <name> --task <id> [--dummy]

# 기타
./run_agent.sh init-project
./run_agent.sh kill-all [--force]
```

---

## 13. Claude Code Session Management

### 13.1 세션 생성 규칙

- **세션 재사용**: `scripts/run_claude_agent.sh`가 `(task_id, agent_type)` 단위로 UUID를 발급(`scripts/allocate_session_id.py` 참고)하여 task JSON의 `agent_sessions.{agent_type}`에 기록한다. 같은 task 안에서 같은 agent_type의 모든 subtask/attempt는 하나의 claude 세션을 공유한다: 첫 호출은 `--session-id <uuid>`, 이후는 `--resume <uuid>`. config.yaml의 `claude.session_reuse: false`로 기능 전체 비활성화 가능(기본 true).
- **재개 시 판단 고착 방지**: resume 세션에는 프롬프트 맨 앞에 "이전 세션 맥락은 배경 자료이며, 이번 턴의 Task/Subtask/Plan Context가 최우선" 가드가 자동 삽입된다. subtask2 coder가 subtask1 때 자기가 내린 결정을 기억하되, 이번 subtask 요구사항과 충돌하면 이번 요구사항을 따르도록 강제한다.
- **컨텍스트 전달**: 세션 재사용으로 이전 맥락이 살아 있어도, 공식 컨텍스트는 계속 prompt + JSON 파일(`subtask-NN.json`의 attempt_history 등)로 주입한다. 세션 재사용은 의도(intent)의 연속성을 위한 부가 채널이며 단일 source of truth가 되지는 않는다.
- **codebase CLAUDE.md**: claude는 `cd "$CODEBASE_PATH"` 후 실행되므로 codebase 안에 CLAUDE.md가 있으면 자동 인식. init-project 시 codebase 루트에 포인터 한 줄짜리 CLAUDE.md가 생성되며 실제 장기 메모리는 `PROJECT_NOTES.md`에 담긴다(§15.6).

### 13.2 Agent 기동 래퍼

`scripts/run_claude_agent.sh`: 9개 agent 지원, dummy/dry-run/force-result 모드, step numbering, stdout/stderr .log 캡처.

**Step numbering:**

| Agent | Step |
|-------|------|
| planner | 01 |
| coder | 02 |
| reviewer | 03 |
| setup | 04 |
| unit_tester | 05 |
| e2e_tester | 06 |
| reporter | 07 |
| memory_updater | 08 |
| summarizer | 09 |

---

## 14. 설계 결정 배경 요약

### 14.1 왜 프로젝트별 WFC 프로세스인가?
inotifywait 기반 blocking loop와 `claude -p` blocking 호출 특성상 프로세스 레벨 격리가 자연스럽다.

### 14.2 왜 파일 기반 통신인가?
port/소켓 없이 기존 agent 간 통신과 일관. debug 시 파일을 직접 읽을 수 있다.

### 14.3 왜 프로젝트 내 task 직렬인가?
같은 repo에서 병렬 작업 시 git 충돌. 병렬 필요하면 별도 clone + 별도 project.

### 14.4 왜 bypass인가? (skip이 아닌)
`claude -p` 한 번 호출에도 세션 비용. pipeline 구성 시점에 아예 빼는 게 효율적.

### 14.5 왜 4계층 설정인가?
시스템/프로젝트(정적)/프로젝트(동적)/task 4단으로 각 레벨의 변경을 독립적으로 처리.

### 14.6 절대경로 필수 규칙
agent가 `cd`로 codebase로 이동하므로 상대경로가 깨짐.

### 14.7 hub_api를 별도 프로세스가 아닌 라이브러리로
CLI/메신저/웹이 같은 Python 라이브러리를 import. 서버 없이 파일 직접 조작. Phase 2에서 웹 API 서버가 필요해지면 hub_api 위에 FastAPI를 올리는 구조. **Phase 2.0에서 이 구조가 실현됨:** `scripts/web/server.py`가 hub_api를 import하여 FastAPI 위에 웹 API 제공.

### 14.8 왜 SQLite 하이브리드인가?
파일이 source of truth이므로 사용자가 직접 JSON을 읽어 디버깅 가능. SQLite는 web 조회/집계/필터 성능만 담당.
파일 수가 적어(프로젝트당 수십 개) mtime 폴링으로 충분. DB 장애 시에도 파일 기반 시스템은 정상 동작.

---

## 15. 현재 구현 상태 및 TODO

> 최종 업데이트: 2026-04-15

### 15.1 구현 완료

| 구분 | 파일 | 설명 |
|------|------|------|
| **Phase 1.0** | `scripts/workflow_controller.py` | WFC 핵심: run_pipeline(), finalize_task(), git 자동화, Summarizer, replan, safety limits, 로그 rotation |
| | `scripts/run_claude_agent.sh` | Claude Code 세션 래퍼: 8 agent, dummy/dry-run/force-result, step numbering |
| | `scripts/check_safety_limits.py` | Safety limits 체크 |
| | `scripts/init_project.py` | 대화형 프로젝트 초기화 |
| | `config/agent_prompts/*.md` | 8개 agent 프롬프트 |
| | `templates/config.yaml.template` | 시스템 설정 템플릿 |
| | `templates/project.yaml.template` | 프로젝트 설정 템플릿 |
| | `create_config.sh` | 템플릿 → config.yaml 생성 |
| **TM Phase** | `scripts/task_manager.py` | TM 상주 프로세스: 폴링, WFC spawn/kill, 큐 블로킹, 시그널 핸들링 |
| | `run_system.sh` | 시스템 관리 CLI: start/stop/status |
| | `scripts/hub_api/` | 공통 인터페이스 라이브러리 (submit, list, approve, reject, feedback, config, pause, resume, cancel, status) |
| | `scripts/cli.py` | CLI 프론트엔드 (11개 서브커맨드, notifications 포함) |
| | `run_agent.sh` | CLI 진입점 확장 (task 관리 명령 11개) |
| | WFC: `resolve_effective_config()` | 4계층 설정 deep merge |
| | WFC: `request_human_review()` | Planner/Replan 후 human interaction 기록 |
| | WFC: `wait_for_human_response()` | 폴링 기반 승인 대기 (approve/modify/cancel/timeout) + 재알림 |
| | TM: `has_incomplete_tasks()` | 미완료 task 스캔 |
| | TM: `should_block_next_task()` | wait_for_prev_task_done 기반 큐 블로킹 |
| **Phase 1.4** | `scripts/notification.py` | 알림 시스템: emit/get/mark_read/format, 6가지 이벤트 타입 |
| | `scripts/usage_checker.py` | PTY 기반 `/usage` 파싱, 3계층 threshold check, 좀비 방지 |
| | WFC: usage check 삽입 | new_subtask(0.80), new_agent_stage(0.90) threshold |
| | TM: usage check 삽입 | new_task(0.70) threshold |
| | TM: `poll_notifications()` | 프로젝트별 알림 폴링 + stdout 출력 |
| | WFC: `emit_notification()` 연동 | 완료/실패/PR/승인요청/에스컬레이션 시점 알림 |
| | WFC: 재알림 | `re_notification_interval_hours` 기반 재알림 |
| **Phase 1.5** | `scripts/chatbot.py` | Chatbot: 자연어→action 변환, 3-mode confirmation, 세션 관리 |
| | `scripts/hub_api/protocol.py` | Protocol layer: Request/Response envelope, dispatch, ACTION_REGISTRY (22 actions) |
| | HubAPI: `get_task()`, `mark_notification_read()` | 단건 조회, 알림 읽음 처리, submit source 파라미터 |
| | `templates/config.yaml.template` chatbot 섹션 | model (sonnet), confirmation_mode (smart) |
| | `run_agent.sh` chat 서브커맨드 | `./run_agent.sh chat [--session <id>] [--list-sessions]` |
| | `session_history/chatbot/` | 세션 이력 저장 (YYYYMMDD_HHMMSS_xxxx.json) |
| **Phase 1.6** | HubAPI: `create_project()` | Chatbot/Protocol 경유 프로젝트 생성, `__UNCONFIGURED__` 플레이스홀더 |
| | HubAPI: `resubmit()` | cancelled/failed task를 새 task로 재제출 |
| | HubAPI: `get_plan()` | task의 plan.json(subtask 목록, 전략) 조회 |
| | HubAPI: `_validate_task_is_active()` | 종료 상태 task에 resume/pause 차단 |
| | `scripts/hub_api/protocol.py` | ACTION_REGISTRY 확장 (14 → 22개: create_project, resubmit, get_plan, close_project, reopen_project, complete_pr_review, merge_pr, close_pr) |
| | `scripts/hub_api/protocol.py` | `ErrorCode.PROJECT_ALREADY_EXISTS` 추가, ValueError/FileExistsError 예외 처리 |
| | `scripts/chatbot.py` | action 선택 가이드(resume vs resubmit), get_plan/resubmit 결과 포맷터 |
| | `scripts/init_project.py` | `UNCONFIGURED_PLACEHOLDER` 상수, `main()` → HubAPI.create_project() 리팩터 |
| | WFC: `ensure_gh_auth()` | project.yaml auth_token 없으면 config.yaml github_token fallback |
| | `setup_environment.sh` | 시스템 환경 초기화: 도구 검증(python3, git, gh, claude) + venv + 설정 + 디렉토리 |
| | `tests/conftest.py` | session-scope cleanup fixture (테스트 프로젝트 잔재 자동 삭제) |
| **Phase 2.0** | `scripts/web/db.py` | SQLite 스키마/CRUD/마이그레이션 (WAL 모드) |
| | `scripts/web/syncer.py` | 파일→DB sync 엔진 (mtime 기반 2초 폴링) |
| | `scripts/web/server.py` | FastAPI 웹 서버: dispatch 재활용, GET 편의 라우트, SSE |
| | `scripts/web/static/app.js` | SPA 프론트엔드: Dashboard, Tasks (인라인 detail), Notifications, Chat 탭 |
| | `scripts/web/static/style.css` | 다크 테마, 상태 배지, pipeline_stage 표시 |
| | `scripts/web/templates/index.html` | SPA 셸 (Jinja2) |
| | WFC: `update_pipeline_stage()` | task 진행 단계 실시간 기록 (planner→coder→reviewer→...) |
| | WFC: `record_failure_reason()` | 실패 원인 기록 (git push, PR 생성 등) |
| | HubAPI: `pending()` 확장 | waiting_for_human_pr_approve 상태도 pending 결과에 포함 |
| | `run_system.sh` | TM + Web Console 동시 기동/종료/상태, config.yaml 포트 연동 |
| | `run_agent.sh` web 서브커맨드 | `./run_agent.sh web` (수동 기동용) |
| | HubAPI: `close_project()`, `reopen_project()` | 프로젝트 lifecycle 관리 (active/closed) |
| | HubAPI: `_require_active_project()` | closed 프로젝트에 submit 차단 |
| | `scripts/hub_api/protocol.py` | close_project, reopen_project, complete_pr_review action 등록 (20개) |
| | `scripts/web/db.py` | projects 테이블 lifecycle 컬럼 + 마이그레이션 |
| | `scripts/web/syncer.py` | 폴더 소실 감지 → DB에서 자동 closed 처리 |
| | `scripts/init_project.py` | project_state.json 초기값에 lifecycle: "active" 추가 |
| **Phase 2.1** | HubAPI: `complete_pr_review()` | `waiting_for_human_pr_approve` → `completed`(merged) / `failed`(rejected) 수동 전이 |
| | `scripts/hub_api/protocol.py` | `complete_pr_review` action 등록 |
| | `scripts/cli.py` | `complete-pr-review` 서브커맨드 추가 |
| | `scripts/chatbot.py` | HIGH_RISK_ACTIONS에 `complete_pr_review` 추가, `merge_strategy` config_override 가이드 |
| | `scripts/web/static/app.js` | `waiting_for_human_pr_approve` task에 PR Merged/PR Rejected 버튼 분리 |
| | WFC: `merge_strategy` enum | `require_human` / `pr_and_continue` / `auto_merge` (`auto_merge: bool` 대체, 하위 호환 유지) |
| | WFC: `base_branch` 설정 | feature branch 생성 기준 브랜치 (`pr_target_branch`와 분리) |
| | TM: PR Watcher 스레드 | 60초 주기 `gh pr view`로 PR MERGED/CLOSED 자동 감지 → task 상태 전이 + 알림 |
| | TM: 폴링 주기 변경 | 기본값 5초 → 2초 |
| | TM: `incomplete_statuses` 수정 | `waiting_for_human_pr_approve` 추가 → `require_human` 시 다음 task 차단 |
| | `templates/project.yaml.template` | `auto_merge` 제거, `base_branch` + `merge_strategy` 추가 |
| | `scripts/init_project.py` | `base_branch` 대화형 입력, `merge_strategy: "require_human"` 기본값 |
| | Agent 프롬프트 제한 규칙 | Coder(git/서버/패키지 금지), Reviewer(코드수정/파일생성삭제 금지), Reporter(코드수정/task JSON 직접수정 금지), Planner(코드수정 금지, git 읽기전용, 프로젝트 설정 참고) |
| **Phase 2.1+** | HubAPI: `merge_pr()` | `gh pr merge` 실행 → task completed. `_load_pr_task()`, `_get_codebase_path()` 공통 헬퍼 |
| | HubAPI: `close_pr()` | `gh pr close` 실행 → task failed |
| | `scripts/hub_api/protocol.py` | `merge_pr`, `close_pr` action 등록 (ACTION_REGISTRY 20→22개) |
| | `scripts/cli.py` | `merge-pr`, `close-pr` 서브커맨드 추가 |
| | `scripts/chatbot.py` | HIGH_RISK_ACTIONS에 `merge_pr`, `close_pr` 추가 |
| | `scripts/web/static/app.js` | 버튼 4종 체계: Merge PR Now, Close PR Now, Mark as Merged, Mark as Rejected |
| | `scripts/web/static/style.css` | `btn-outline-success`, `btn-outline-danger` outline 버튼 스타일 |
| **Phase 2.0 Chat** | `scripts/web/web_chatbot.py` | ChatProcessor: Popen 기반 claude -p, cancel+merge, confirmation flow, 세션 관리 |
| | `scripts/web/web_chatbot.py` 세션 레지스트리 | `get_or_create_session()`, `broadcast_system_event()`, `_active_sessions` |
| | `scripts/web/server.py` Chat API | `/api/chat/session`, `/api/chat/send`, `/api/chat/sessions`, `/api/chat/history/{id}` |
| | `scripts/web/server.py` PR 비동기 | `merge_pr`/`close_pr` 백그라운드 스레드 실행, SSE `pr_action_result` 이벤트 |
| | `scripts/web/server.py` notification→chat | `_on_change()`에서 `broadcast_system_event()` 호출 |
| | `scripts/web/static/app.js` Chat UI | 세션(localStorage), fire-and-forget 전송, SSE `chat_message`/`chat_typing` 수신, 확인 카드 |
| | `scripts/web/static/app.js` PR 비동기 UI | `setPrProcessing()`, `showPrError()`, SSE `pr_action_result` 리스너 |
| | `scripts/web/static/style.css` | chat system/typing/confirmation, PR processing/error 스타일 |
| | `scripts/web/templates/index.html` | Chat 영역에 New Session 버튼 추가 |
| | `run_system.sh` | PID: `web_console_chat.{PID}.pid` 패턴, 헬퍼 함수 (`find_web_pid_file`, `read_web_pid`, `is_web_running`) |
| **Phase 2.2** | `scripts/hub_api/queue_helpers.py` | Priority Queue 헬퍼: append/remove/peek/pop (fcntl.flock), ensure_queue_files, migrate_ready_sentinels |
| | HubAPI: `submit(priority=...)`, `cancel()` | priority 파라미터(critical/urgent/default, 기본 default), cancel 시 queue에서 제거 |
| | `scripts/cli.py` submit | `--priority` 옵션 추가 |
| | `scripts/hub_api/protocol.py` submit | `priority` optional param 추가 |
| | TM: `find_next_task()` + `consume_queue_entry()` | critical→urgent→default 순회, 비실행 상태 id 자동 skip + 정리 |
| | TM: `migrate_legacy_ready_sentinels()` | 기동 시 레거시 `.ready` 파일을 default queue로 이주 |
| | `scripts/init_project.py` (HubAPI.create_project) | 프로젝트 생성 시 3개 queue 파일 빈 배열로 초기화 |
| | `scripts/chatbot.py` | SYSTEM_PROMPT에 priority 파라미터 설명 + 자연어 매핑 가이드 (긴급→critical, 급함→urgent), submit 결과 포매터에 priority 표시 |
| | `scripts/web/static/app.js` | New Task 모달에 Priority select (default/urgent/critical) |
| | `scripts/hub_api/models.py` | `SubmitResult.priority` 필드 추가 |
| | `tests/test_queue_helpers.py` + `tests/test_priority_queue_integration.py` | 25개 신규 테스트 (단위 17 + 통합 8): priority 순서, cancel, flock race, cancelled skip, .ready 이주 |
| **Phase 2.2 UX** | `scripts/hub_api/config_preview.py` | submit 확인 카드용 effective config 트리 미리보기. `compute_effective_config()`는 WFC `resolve_effective_config()` 재사용. `HIDDEN_SECTIONS`/`HIDDEN_PATHS` 블랙리스트(신규 section 자동 노출). override 경로에만 `(수정됨)` 태그 |
| | `scripts/chatbot.py` confirmation | `format_confirmation_prompt(parsed, agent_hub_root)`에서 config_override를 트리로 렌더링. SYSTEM_PROMPT에 "explanation 작성 규칙" 추가 (LLM이 확인 카드/파라미터 나열/“확인 또는 취소”를 explanation에 중복 생성 금지) |
| | `scripts/web/web_chatbot.py` | `_format_confirmation_plain()` 동일 로직, `pr_merged` type_label 추가 |
| | HubAPI: `list_tasks()` running 치환 | 프로젝트별 `project_state.json` 1회 읽어 `current_task_id`와 일치 + status ∈ {submitted, planned}이면 `running`으로 표시 (FSM 변경 없이 표시만 override) |
| | `scripts/web/server.py` | `_apply_running_override()` — `/api/tasks`, `/api/tasks/{project}/{task_id}` 응답에 동일 running 치환 |
| | `scripts/web/static/app.js` | 확인 카드 렌더 시 `textContent` 사용 (XSS 방지 + 공백 보존), Cancel/Feedback 버튼 조건에 `running` 포함, `pr_merged` 라벨 |
| | `scripts/web/static/style.css` | `.chat-msg { white-space: pre-wrap; }` (들여쓰기 보존), `.status-running` 색상 orange(`var(--warning)`)로 분리 (completed 녹색과 구분) |
| | `run_system.sh status` | 실행 중 task의 `tasks/{id}-*.json`에서 `pipeline_stage`/`pipeline_stage_detail`/`current_subtask` 읽어 `[stage / detail]` 또는 `[stage / subtask N]` 표시 |
| | WFC `auto_merge` 분기 | `git_merge_pr()` 성공 후 `emit_notification(event_type="pr_merged")` 호출 (기존 `pr_created`→`task_completed` 사이 침묵 제거) |
| | HubAPI `merge_pr()` | 수동 머지 성공 후에도 동일 `pr_merged` 이벤트 발생 (auto/manual 일관성) |
| | `scripts/notification.py` | `EVENT_STYLES["pr_merged"]`(GREEN, "PR 머지") 등록, docstring 이벤트 목록 갱신 |
| **Merge 실패 알림** | `scripts/notification.py` | `EVENT_STYLES["pr_merge_failed"]`(RED, "PR 머지 실패") 등록 |
| | WFC `auto_merge` 실패 분기 | `git_merge_pr()` RuntimeError 시 task status를 `failed`가 아닌 `waiting_for_human_pr_approve`로 유지하고 `pr_merge_error`/`pr_merge_error_at` 기록, `pr_merge_failed` 알림 발송. `project_state` idle 전환 후 `return` (task_completed 알림 억제) |
| | HubAPI `merge_pr()` 실패 분기 | `gh pr merge` 실패 시 task JSON에 `pr_merge_error`/`pr_merge_error_at` 영속 저장 + `pr_merge_failed` 알림 발송, 재시도 성공 시 해당 필드 정리 (stale 방지) |
| | HubAPI `pending()` | `waiting_for_human_pr_approve` task에 `pr_merge_error`가 있으면 `HumanInteractionInfo.pr_merge_error`로 별도 전달 (message는 clean 유지) |
| | `scripts/hub_api/models.py` | `HumanInteractionInfo.pr_merge_error: Optional[str]` 필드 추가 |
| | `scripts/web/db.py` | `tasks` 테이블 `pr_merge_error`/`pr_merge_error_at` 컬럼 + 마이그레이션 + upsert 반영 |
| | `scripts/web/static/app.js` | 승인 대기 카드 / 태스크 상세 버튼 영역에 `.pr-error` 빨간 박스 조건부 렌더 (SSE 재렌더링에도 영속). row에 `⚠` 뱃지(tooltip 에러). `pr_merge_failed` type_label 추가 |
| | `scripts/web/web_chatbot.py` | `pr_merge_failed` type_label 추가 (Chat 시스템 이벤트 메시지) |
| | `templates/config.yaml.template` | `notification.events`에 `pr_merged`, `pr_merge_failed` 추가 |
| **Phase 2.0+** | WFC: graceful shutdown | SIGTERM 핸들러, `_load_pipeline_context()`, `run_pipeline_resume()`, `_continue_after_plan_review()`, `_continue_after_replan_review()` — 중단 지점에서 재개 |
| | TM: WFC shutdown 전파 | `shutdown()`이 실행 중 WFC에 SIGTERM 전파 후 30초 대기 → 미응답 시 SIGKILL |
| | TM: WFC resume on boot | `resume_waiting_tasks()`, `_check_waiting_task_response()` — 기동 시 중단된 task 자동 재개 및 human response 폴링 재개 |
| | DB: `projects.wfc_pid` 컬럼 | WFC PID 추적 (마이그레이션 포함) |
| | Safety limiter 정확도 | WFC: `_accumulate_human_wait_seconds()` — human_interaction의 requested_at~responded_at 차이를 `counters.human_wait_seconds`에 누적. `check_safety_limits.py`: task duration에서 대기 시간 차감 |
| | `hub_api.cancel()` | 취소 시 `project_state.json`의 current_task_id 일치하면 status → `idle`로 갱신 (Web UI 정합성 수정) |
| | Web Chat 사용성 개선 | 세션 사이드바(목록/rename/delete), SSE 실시간 전달(queue.Queue 스레드 안전), 확인 카드 → plain text, Chat 시스템 알림, Shift+Enter 줄바꿈 |
| **Coder/Reviewer Context Mgmt** | `scripts/workflow_controller.py` | subtask 진입 시 `subtask_start_sha` 캡처(in-memory) + `attempt_history` 누적. Reviewer 출력 스키마 validation(`validate_reviewer_output()`) + 1회 재요청, 실패 시 reset으로 강등. retry_mode-aware 분기: `reset`이면 `git_reset_hard_to(start_sha)`, `continue`면 worktree 보존. Coder 몰래 commit 방어(`git_soft_reset_if_moved()`). Reviewer `approved` 시점에만 `git_commit_worktree_no_push()`로 local commit, 중간 push 전면 제거 → PR 생성 직전 task 브랜치 명시 1회 push. Replan 시 `git_wipe_and_recreate_task_branch()`로 task 브랜치 폐기 후 base_branch에서 재생성, `completed_subtasks=[]` 비움, `replan_started` 알림 |
| | `config/agent_prompts/reviewer.md` | 출력 스키마 개정 (`retry_mode`/`current_state_summary`/`what_is_wrong`/`what_should_be`/`actionable_instructions`/`feedback`). 매 호출 첫 단계로 `git diff {subtask_start_sha} -- .` 실행 강제. coder intent_report와 diff 불일치 시 diff 신뢰 |
| | `config/agent_prompts/coder.md` | retry_mode 인지 동작(reset: 처음부터 / continue: 기존 변경 보존 후 actionable_instructions만 덧붙임), `intent_report` 필수 출력, git 쓰기 명령 금지 |
| | `scripts/notification.py` + `scripts/telegram/formatter.py` | `replan_started` 이벤트 추가 (color YELLOW / icon 🔄) |
| | `scripts/run_claude_agent.sh` | `reviewer:approve`/`reviewer:reject` force_result fixture를 새 스키마에 맞춤 |
| | `scripts/workflow_controller.py:finalize_task()` | Summarizer 호출 직전 `current_subtask=None` 클리어 (마지막 subtask 후 safety 차단 버그 수정) |
| | `scripts/check_safety_limits.py` | `current_subtask`가 이미 `completed_subtasks`에 있으면 중복 집계 안 함 (방어적 fallback) |
| **테스트** | `tests/` (260개) | Unit/Integration/E2E + Web DB/Syncer + Lifecycle + PR Review + merge_pr/close_pr + Web Chat + graceful shutdown/resume + safety limiter + priority queue + Telegram(client/formatter/router) 테스트 |
| | `run_test.sh` | 테스트 실행 스크립트 (unit/integration/e2e/all) |
| | `pytest.ini` | pytest 설정 |

### 15.2 검증 완료 항목

| 항목 | 검증 방법 |
|------|-----------|
| 더미 파이프라인 사이클 | run_dummy_pipeline.sh |
| 실제 task 실행 (00002~00006) | test-web-service 대상 실제 claude -p 실행 |
| Git 자동화 | branch 생성, subtask 커밋, push, PR 생성/머지 |
| merge_strategy 분기 | auto_merge (자동 머지), require_human (waiting_for_human_pr_approve + 다음 task 차단), pr_and_continue (즉시 완료) |
| complete_pr_review | waiting_for_human_pr_approve → completed (merged) / failed (rejected) 수동 전이 |
| PR Watcher 자동 감지 | MERGED → completed, CLOSED → failed (60초 폴링) |
| incomplete_statuses 블로킹 | waiting_for_human_pr_approve 포함 → require_human 시 다음 task 차단 검증 |
| Replan 로직 | task 00099: dummy 모드, reporter force_result=replan |
| Safety limits | check_safety_limits.py 초과 시 agent 차단 |
| gh 인증 자동화 | ensure_gh_auth() + project.yaml auth_token + config.yaml github_token fallback |
| TM → WFC spawn | .ready 감지 → WFC 자동 실행 → 완료 감지 |
| Task 큐 블로킹 | 미완료 task 존재 시 spawn 차단, 완료 후 해제 |
| wait_for_prev_task_done=false | 프로젝트 override 시 블로킹 비활성화 |
| human review 함수 | request/wait/approve/timeout/cancel 단위 테스트 |
| run_system.sh status | waiting_for_human_plan_confirm 노란색 표시 |
| CLI 전수 테스트 | submit, list, pending, approve, reject, feedback, config, pause, resume, cancel + 에러 케이스 |
| merge_pr / close_pr | subprocess mock으로 gh CLI 실행 검증, 상태 전이, 에러 케이스 (9개 테스트) |
| PR 비동기 처리 | Web에서 Merge/Close PR Now 클릭 → 즉시 accepted → SSE로 결과 전달, 성공/실패 UI 검증 |
| Web Chat 양방향 | 자연어 메시지 전송 → claude -p 해석 → action dispatch → SSE 응답 수신 |
| Web Chat cancel+merge | 처리 중 새 메시지 → Popen.kill → 합쳐서 재실행 (18개 테스트) |
| Web Chat confirmation | 고위험 action → 확인 카드 → "확인"/"취소" 텍스트 응답 |
| Web Chat 시스템 이벤트 | notification → broadcast_system_event → 활성 세션에 system 메시지 주입 |
| PID 네이밍 | `web_console_chat.{PID}.pid` 패턴으로 TM과 통일, start/stop/status 검증 |
| WFC graceful shutdown | SIGTERM 수신 시 현재 pipeline_stage 보존 → TM 재기동 시 해당 지점부터 재개 (plan_review/replan_review/subtask loop) |
| Safety limiter 대기 시간 제외 | human_interaction 대기 시간 누적, 실제 agent 활동 시간만 max_task_duration_hours에 반영 |
| cancel 시 idle 전환 | 현재 실행 task를 cancel → project_state.json status idle 즉시 반영 (Web UI 정합성) |
| Coder/Reviewer 재시도 컨텍스트 | test-project task 00147 (subtask 2개): start_sha 캡처, retry_mode=continue 모드 전달, attempt_history 누적, Reviewer 새 스키마 출력, 승인 시점 commit, PR 생성 시 단 1회 push 모두 정상 동작 |
| Summarizer 차단 버그 fix | task 00147에서 마지막 subtask 후 Summarizer가 막히던 `subtask 개수 초과: N+1/N` 재현 후, finalize_task에서 current_subtask 클리어 + safety 중복 집계 방어 적용. 후속 task 00148~00152까지 5건 연속 Summarizer 정상 완료로 회귀 없음 확인 (스펙 TODO였던 "Summarizer 산발적 실패"도 같은 fix로 해소) |
| plan_review modify 후 replan 승인 | test-project task 진행 중, plan_review에서 "subtask 2개를 1개로 묶어달라" modify 요청 → Planner 재실행 → 새 plan에 대한 `waiting_for_human_plan_confirm` 진입 및 승인 대기 정상 동작 확인 (이전에는 검토 없이 바로 실행되던 버그) |

### 15.3 미구현 (TODO)

| 범위 | 내용 | 예정 Phase |
|------|------|-----------|
| ~~**claude -p 세션 재사용**~~ | (구현됨, §15.4 Phase 2.5) `(task_id, agent_type)` UUID로 subtask·attempt 간 세션 공유, resume 시 "이전 맥락은 참고용" 가드 자동 삽입으로 판단 고착 완화. config `claude.session_reuse`로 on/off. | 완료 |
| ~~**replan 승인 단계 미집행 버그**~~ | (구현됨, §15.4 "plan_review modify 후 replan 승인") plan_review의 `modify` 분기에서 Planner 재실행 후 `review_replan` 기반 승인 대기가 누락되어 새 plan이 검토 없이 실행되던 버그. `run_pipeline()`과 `_continue_after_plan_review()` 양쪽에 `request_human_review("replan_review", ...)` + `wait_for_human_response()` 삽입으로 해결 (subtask-failure replan 경로와 대칭). | 완료 |
| ~~**codebase CLAUDE.md 자동 생성/유지**~~ | (구현됨, §15.6) init-project 시 codebase 루트에 CLAUDE.md 포인터 + `PROJECT_NOTES.md` 템플릿 생성. MemoryUpdater agent가 매 task 완료 시 PROJECT_NOTES.md를 증분 갱신하고 Summarizer 직전에 커밋 | 완료 |
| ~~**projects/{name}/PROJECT_NOTES.md**~~ | (구현됨, §15.6) codebase 루트에 두어 LLM 런타임 비종속. CLAUDE.md는 포인터 한 줄만. | 완료 |
| **user_preferences slot** | project_state.json에 사용자 선호 저장. 기존 4계층 내 처리 | 2.2+ |
| **GH_TOKEN 환경변수 전환** | 멀티유저 시 gh 토큰을 환경변수로 격리 (현재 시스템 로그인 공유) | 2.2+ |
| **강제 실행 옵션** | wait_for_prev_task_done 무시 force 타입 요청 | 2.2+ |
| **Slack 연동** | Slack 메시지 수신 → task 생성. Telegram(2.3 완료)과 동일 패턴 | 2.3+ |
| **Telegram 첨부파일** | photo/document 다운로드 → `attachments/` (현재는 drop) | 2.3+ |
| ~~**E2E 테스트장비 연동**~~ | (폐기, Docker로 전환됨) 크로스 머신 SSH 기반 원격 실행은 더 이상 추진하지 않음. `e2e_watcher.sh`는 deprecated 마킹만 남김 | 폐기 |
| ~~**로컬 E2E 테스트**~~ | (구현됨, §15.4 "E2E Playwright+MCP-in-Docker") 브라우저 E2E를 Docker 컨테이너에서 완결. mode slot은 browser(완료)/desktop(미착수)로 재정의 | 완료 |

### 15.4 Phase 로드맵

| Phase | 내용 | 상태 |
|-------|------|------|
| **1.0** | 수동 pipeline 실행 + git 자동화 | **완료** |
| **TM** | Task Manager + CLI + hub_api + human review + 큐 블로킹 | **완료** |
| **1.4** | 운영 안정화: 알림, Usage check, 재알림, 테스트 스위트 | **완료** |
| **1.5** | Chatbot 레이어, 세션 관리, 프로토콜 body | **완료** |
| **1.6** | Chatbot 사용성: create_project, resubmit, get_plan, resume 검증, 환경 초기화 (17 actions, 177개 테스트) | **완료** |
| **2.0** | Web Monitoring Console + SQLite 하이브리드 | **진행 중** |
|  | └ 기본 구조 완성: DB, Syncer, FastAPI, SPA, SSE, pipeline 가시성 | 완료 |
|  | └ Task lifecycle 정립 (프로젝트 lifecycle, 상태명 명확화, FSM 다이어그램) | 완료 |
|  | └ Web Chat: ChatProcessor, cancel+merge, confirmation flow, system event, PR 비동기 | 완료 |
|  | └ PID 네이밍 통일 (`web_console_chat.{PID}.pid`), 로그 경로 일원화 | 완료 |
|  | └ Web Chat 사용성 개선 (사이드바, SSE 실시간 전달, 알림 표시) | 완료 |
| **2.1** | merge_strategy 3종 + complete_pr_review + PR Watcher + branch config + agent 프롬프트 제한 | **완료** |
| **2.1+** | PR 직접 머지/닫기 (merge_pr, close_pr) + Web 버튼 4종 체계 (206개 테스트) | **완료** |
| **2.0+** | WFC graceful shutdown + resume, Safety limiter 정확도, cancel idle 전환, Web Chat 사용성 개선 | **완료** |
| **2.2** | Priority Queue (`task_queue_{critical,urgent,default}.json` 기반 우선순위 큐, `fcntl.flock` 동시성, 레거시 `.ready` 자동 이주) | **완료** |
| **2.2 UX** | submit 확인 카드 effective config 트리 미리보기 (`config_preview.py`, 블랙리스트), list/status/web에 running 상태 치환 + pipeline stage 표시, running vs completed 색상 구분, auto_merge `pr_merged` 알림, chatbot explanation 중복 생성 방지 | **완료** |
| **Merge 실패 알림** | `pr_merge_failed` 이벤트 + auto_merge/manual merge 실패 시 task를 `waiting_for_human_pr_approve`로 유지, `pr_merge_error` 영속 저장, Web UI `.pr-error` 빨간 박스 (SSE 재렌더링에도 남음), Chat 시스템 이벤트 | **완료** |
| **2.2+** | 고급 기능: user_preferences, GH_TOKEN 환경변수, Merge conflict 처리, 강제 실행 옵션 | 미착수 |
| **2.3** | Telegram 연동 | **완료** |
|  | └ Forum Topics 자동 생성/close/reopen, /bind_hub + 수동 register CLI, config.yaml 주석 보존 | 완료 |
|  | └ notification poller 패턴 (telegram_bridge 내부, notification.py 무수정), inline keyboard 승인 UX | 완료 |
|  | └ 자연어 chatbot 경유 + 슬래시 명령, ChatProcessor topic당 유지 | 완료 |
|  | └ git push 환경 분리(askpass/socket env strip + http.extraheader 토큰 inline) | 완료 |
|  | └ requested_by 신원 태그 채널 전파 (CLI/Web/Telegram → commit/PR `[task_id][requested_by]`) | 완료 |
|  | └ 프로젝트별 telegram.enabled opt-out + 테스트 autouse 픽스처 차단 | 완료 |
|  | └ 채널 간 human-review sync (`plan_review_responded`/`pr_review_responded` 이벤트, already-processed 응답) | 완료 |
| **base_branch 자동 리셋** | Planner 실행 전 `git reset --hard origin/<base_branch>`로 codebase 동기화 (`pipeline_stage=git_reset` 추가). 이전 task의 잔여 변경/잘못된 브랜치 상태로 Planner가 잘못된 코드베이스를 분석하던 사고 차단 | **완료** |
| **Project Long-term Memory** | codebase 루트에 `PROJECT_NOTES.md`(장기 메모리) + `CLAUDE.md`(포인터 한 줄)를 init-project 시 템플릿 생성. 신규 agent `memory_updater`가 Summarizer 직전에 실행되어 task 변경을 바탕으로 `PROJECT_NOTES.md`를 증분 갱신 후, WFC가 `[{task_id}] memory: ...` 커밋으로 묶어 같은 PR에 포함. 실패해도 PR 생성 차단 X(경고만). LLM 런타임에 종속되지 않도록 CLAUDE.md에는 본문을 두지 않고 PROJECT_NOTES.md pointer만 유지. Step numbering 재조정: summarizer 08→09, memory_updater=08 신설 | **완료** |
| **Coder/Reviewer Context Mgmt** | retry 루프에서 Coder/Reviewer가 직전 attempt를 인지하지 못해 수정 지시가 중복 추가로 누적되던 문제 해결. (1) `subtask_start_sha` in-memory 캡처 + `attempt_history` 누적, (2) Reviewer 출력 스키마 개정(`retry_mode ∈ continue/reset` + actionable_instructions, git diff 강제), (3) Coder retry_mode-aware 동작(reset: reset --hard 후 새로 / continue: 변경 보존 후 덧붙임), (4) 커밋 주체 WFC 단일화(승인 시점에만 commit, Coder 몰래 commit 방어), (5) 중간 push 전면 제거 → PR 생성 직전 task 브랜치 명시 1회 push, (6) Replan 시 task 브랜치 폐기/재생성 + completed_subtasks 비움. 부수: 마지막 subtask 후 Summarizer 차단 버그 fix(current_subtask 클리어 + safety 중복 집계 방어). test-project task 00147(subtask 2개)로 실동작 검증 완료. 핸드오프: `docs_for_claude/019-handoff-coder-reviewer-context-management.md` | **완료** |
| **E2E Playwright+MCP-in-Docker** | 기존 SSH + Windows 테스트장비 설계를 폐기하고, Playwright + `@playwright/mcp`를 한 이미지(`mcr.microsoft.com/playwright:v1.52.0-noble`)에 통합해 subtask 단위로 `--network=host` 컨테이너를 spawn. e2e_tester agent를 4-Phase(탐색→작성→검증→재탐색)로 재작성하고, `/tmp/mcp-{task}-{subtask}.$.json`을 동적 생성해 Claude CLI `--mcp-config`로 주입. 판정은 `docker exec ... npx playwright test` JSON reporter만 권위 있음. `scripts/e2e_container_runner.sh`가 기동/헬스체크/cleanup을 담당하고 `scripts/run_claude_agent.sh`의 e2e_tester 분기에서 컨테이너·config·프롬프트 주입을 통합. MCP 로그는 기본 on-failure 보존(always/never 토글). test_source=dynamic\|static\|both(AND 판정), `base_url` 비우면 `codebase.service_port`에서 자동 추론. 설계 문서: `docs/e2e-test-design-decision.md` | **완료** |
| **2.4** | desktop mode(빌드형 앱), E2E 다중 인스턴스 충돌 회피(DB/세션) — 본격적 웹 개발 이후 | 미착수 |
| **plan_review modify 후 replan 승인** | `run_pipeline()`과 `_continue_after_plan_review()`의 plan_review `modify` 분기에서 Planner 재실행 후 `review_replan` 기반 승인 대기가 누락되어 있던 버그 수정. `request_human_review("replan_review", ...)` + `wait_for_human_response()` 삽입으로 subtask-failure replan 경로와 동작 대칭화. 재시작 시 `human_interaction.type="replan_review"` 디스패치로 resume 경로도 정상. 핸드오프: `docs_for_claude/020-handoff-replan-approval-and-summarizer-cleanup.md` | **완료** |
| **2.5** | claude 세션 재사용: `(task_id, agent_type)` UUID 기반 subtask·attempt 간 세션 공유. `scripts/allocate_session_id.py` lazy 발급 + task JSON `agent_sessions` 기록, `run_claude_agent.sh`가 `--session-id`/`--resume` 분기, resume 프롬프트 가드, `claude.session_reuse` 토글. test-project task 00151(subtask 2개)로 실동작 검증 — subtask 02 coder가 subtask 01에서 만든 `playBeep()` 재사용 의도까지 세션 맥락으로 연속 전달 확인. 핸드오프: `docs_history/handoffs/019-handoff-long-term-memory-and-session-reuse.md` | **완료** |

참고: Phase 1.1(파이프라인 자동화)과 1.2(Planner+Subtask Loop)는 Phase 1.0에서 WFC로 통합 구현됨. E2E는 당초 1.3→1.7→2.4로 밀려있던 원격 테스트장비 연동(SSH sentinel)을 2026-04-16에 **폐기**하고, Playwright + MCP-in-Docker 로컬 컨테이너 방식으로 재설계하여 본 Phase에서 구현 완료. desktop mode 및 다중 인스턴스 충돌 회피는 2.4로 이월.

### 15.5 배포

배포 시 Python 소스 코드 비식별화를 위해 **Cython**을 사용한다. `.py` → `.c` → `.so`로 컴파일하여 바이너리만 배포하며, 프롬프트 등 텍스트 리소스는 Python 변수로 임베드 후 함께 컴파일한다.
