# Agent System Architecture Specification v4

> Claude Code CLI 기반 24시간 자동 개발 시스템
> 최종 정리: 2026-04-03
> 이전 버전: `docs_history/003-agent-system-spec-v3.md` (v3)
> 설계 히스토리: `docs_for_claude/005-design-history-archive.md`

---

## 문서 간 관계

| 문서 | 역할 | 참고 수준 |
|------|------|-----------|
| **`003-agent-system-spec-v4.md` (이 문서)** | **현행 설계 명세. 모든 구현은 이 문서를 기준으로 합니다.** | **기준 문서** |
| `005-design-history-archive.md` | v1→v2 변경 배경 + Phase 1.0 핸드오프 병합 아카이브 | 설계 배경이 궁금할 때 |
| `docs_history/003-agent-system-spec-v3.md` | v3 설계 (아카이브) | 참고 불필요 |

---

## 1. System Overview

사용자가 CLI(또는 추후 메신저)로 작업을 요청하면, 복수의 agent가 순차적으로 코드 작성, 리뷰, 테스트를 수행하고 결과를 알림.
복잡한 작업은 Planner가 subtask로 분할하여 순차 실행하며, 각 subtask는 독립적으로 커밋 가능한 단위.

### 핵심 설계 원칙

1. **멀티 프로젝트 지원:** 하나의 agent-hub 인스턴스가 여러 프로젝트를 동시에 관리. 프로젝트별로 독립된 Workflow Controller 프로세스가 격리 실행.
2. **4계층 설정:** `config.yaml` (시스템) → `project.yaml` (프로젝트 정적) → `project_state.json` (프로젝트 동적) → `task.config_override` (task 일시). 뒤의 것이 앞의 것을 덮어씀.
3. **파일 기반 통신:** 모든 프로세스 간 통신은 JSON 파일 + `.ready` sentinel. 내부 port나 소켓 없음.
4. **역할 기반 기동:** 같은 코드베이스를 실행장비/테스트장비에서 `git pull`로 동기화하되, 기동 시 role에 따라 다른 프로세스를 실행.
5. **책임 범위 기반 subtask:** 파일 격리가 아닌 `primary_responsibility`로 분할. scope 겹침 허용. `prior_changes`로 맥락 전달.
6. **CLAUDE.md는 정적 지식만:** 동적 상태는 전부 JSON 파일로 관리.
7. **테스트 선택적:** unit test, e2e test, integration test 각각 활성화/비활성화 가능. 비활성화된 agent는 pipeline에서 bypass (호출 자체 안 함).
8. **CLI 구독 기반:** Claude Code CLI(`claude -p`)를 직접 사용. API key 불필요. Pro/Max/Team/Enterprise 구독으로 동작.
9. **Usage 기반 제어:** claude 세션 사용량 threshold를 기반으로 새 task/subtask/agent 실행을 조절. 과사용 방지.

---

## 2. Machine Roles

### 2.1 장비 구성

| 구분 | 장비 | OS | 스펙 | 역할 | 가동 |
|------|------|-----|------|------|------|
| 실행장비(메인) | 장비2 — 랙서버 | Ubuntu 20.04 | i7, 32GB, A5000 | Agent Hub, 코드베이스 기동 | 24h |
| 실행장비(서브) | 장비4 — 구형 PC | Ubuntu 24.04 | i7-6700, 32GB, 3060 | 보조 코드베이스 기동, 보조 agent (당분간 미사용) | 24h |
| 테스트장비 | 장비1 — 회사 노트북 | Windows (WSL) | i9, 64GB, 3080ti | GUI 브라우저 E2E 테스트 | 업무 시간 |
| 테스트장비 | 장비3 — 집 PC | Windows (WSL) | R7 9800X3D, 96GB, 5080 | GUI 브라우저 E2E 테스트 | 수시 |
| 이동용 | 장비5 — 그램 노트북 | Windows (WSL) | i7-11th, 16GB | 외부 작업용 | 이동 시 |

### 2.2 접속 패턴

- **테스트장비 → 실행장비:** SSH 가능 (모든 장비에서 장비2에 접근 가능)
- **실행장비 → 테스트장비:** SSH 불가 (push 불가)
- Claude Code CLI는 실행장비(장비2)에서 직접 실행
- 테스트장비에서는 SSH로 실행장비에 접속하여 작업하거나, 로컬 Windows host에서 E2E agent 실행

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
| 상주 (long-running) | Workflow Controller | task 실행 중 | 프로젝트당 0~1개 |
| 일회성 (per-invocation) | Agent (Planner, Coder 등) | `claude -p` 실행~종료 | subtask당 1개씩 |

### 3.2 프로세스 계층

```
[상주] Task Manager (1개)
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

- **프로젝트 감시:** `projects/` 디렉토리를 폴링하여 새 프로젝트 자동 감지
- **task 큐 관리:** `.ready` sentinel 감지 → WFC spawn
- **task 큐 블로킹:** `wait_for_prev_task_done=true`일 때 이전 task 미완료 시 다음 task spawn 차단
- **WFC 라이프사이클 관리:** spawn, 완료 감지, 정리
- **system-wide 현황 조회:** `projects/*/project_state.json`을 읽어 전체 현황 조합
- **시그널 처리:** SIGTERM(graceful), SIGUSR1(force) 종료 지원

### 3.4 Workflow Controller의 역할

각 WFC는 하나의 task를 전담하는 일시 프로세스:

- **파이프라인 실행:** Planner → human review 대기 → subtask loop → Summarizer → PR
- **설정 해소:** 4계층 config merge → effective config 계산 (resolve_effective_config)
- **human review:** review_plan/review_replan 설정에 따라 사용자 승인 대기 (폴링 기반)
- **명령 수신:** `commands/` 디렉토리의 cancel 명령 파일 감시
- **상태 보고:** `project_state.json` 업데이트 (TM이 읽기)
- **한도 체크:** 매 agent 호출 전 counters vs limits 비교
- **git 작업:** branch 생성, subtask별 commit+push, Summarizer 후 PR 생성, auto_merge 시 PR 머지

### 3.5 TM ↔ WFC 통신

port나 소켓 없이, 파일 기반으로 통신:

```
TM → WFC 명령 전달:
  projects/{name}/commands/{명령}.command 파일 생성
  예) pause.command, resume.command, cancel-00042.command

WFC → TM 보고:
  projects/{name}/project_state.json 업데이트 (TM이 읽기)

TM ← task 수신:
  projects/{name}/tasks/{id}.ready sentinel 감지 (TM 폴링)
```

### 3.6 hub_api — 공통 인터페이스 계층

CLI, 메신저, 웹 등 모든 프론트엔드가 공유하는 Python 라이브러리 (`scripts/hub_api/`):

- **task 관리:** submit, list, cancel
- **human interaction:** pending, approve, reject, feedback
- **설정 변경:** config (project_state.json의 overrides 동적 변경)
- **프로세스 제어:** pause, resume
- **시스템 상태:** status (TM + 프로젝트별 상태 조합)

CLI(`scripts/cli.py`)가 hub_api를 직접 import하여 사용. 메신저/웹은 추후 같은 hub_api를 경유.

### 3.7 프로젝트 내 task 실행 규칙

**하나의 프로젝트 내에서 task는 직렬 실행된다.** 같은 repo에서 병렬 feature 작업이 필요하면, 별도 clone + 별도 project로 구성한다.

**task 큐 블로킹:** `wait_for_prev_task_done=true` (기본값)이면, 이전 task가 `in_progress`, `pending_review`, `waiting_for_human` 등 미완료 상태일 때 다음 task를 spawn하지 않는다. 프로젝트별로 override 가능.

---

## 4. Agent Catalog

### 4.1 Orchestration Layer (장비2 상주)

#### Task Manager

- **역할:** 프로젝트 감시 + WFC 라이프사이클 관리
- **입력:** `.ready` sentinel 파일 감지
- **출력:** WFC spawn/kill, project_state.json 갱신
- **상세:**
  - `projects/` 디렉토리 폴링 (기본 5초)
  - 프로젝트당 WFC 1개만 유지 (이미 실행 중이면 완료 대기)
  - task 큐 블로킹 (wait_for_prev_task_done)
  - PID 파일 기반 프로세스 추적 (.pids/)
  - 시그널 핸들링: SIGTERM (graceful), SIGUSR1 (force kill)
  - dummy 모드 지원 (WFC를 --dummy로 실행)

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
- **입력:** subtask 정의 + prior_changes + guidance + 첨부 이미지
- **출력:** 코드 변경, changes_made 기록

#### Review Agent
- **역할:** 코드 리뷰
- **입력:** Coder의 변경 파일 목록 + diff
- **출력:** 승인 or 거절 (거절 시 Coder 루프백)

#### Setup Agent
- **역할:** 환경 구성 및 프로그램 기동
- **입력:** 현재 코드베이스 상태
- **출력:** 기동 성공/실패 상태

#### Unit Test Agent
- **역할:** 코드 레벨 테스트
- **입력:** 기동된 환경 + testing.unit_test 설정
- **출력:** 테스트 결과 (pass/fail)

#### E2E Test Agent
- **위치:** 테스트장비 (Windows host)
- **역할:** 브라우저 기반 통합 테스트
- **입력:** handoff JSON
- **출력:** 테스트 결과 + 스크린샷

#### Reporter Agent
- **역할:** 결과 종합 및 판정
- **입력:** 모든 테스트 결과
- **출력:** pass/fail 판정, 버그 리포트, changes_made 기록

#### Summarizer Agent
- **역할:** 완료된 task의 작업 요약 및 PR 메시지 생성
- **입력:** plan.json, git diff, 완료된 subtask 목록
- **출력:** PR 제목/본문 + task 요약

---

## 5. Workflow

### 5.1 전체 사이클

```
 1. 사용자: CLI submit (또는 메신저)
    → hub_api: task JSON 생성 + .ready sentinel
    → TM: .ready 감지 → 큐 블로킹 확인 → WFC spawn

 2. WFC: 4계층 설정 merge → effective config 생성
    → Planner Agent 기동

 3. Planner Agent → plan 생성 (subtask 배열 + branch_name 제안)

 3a. [review_plan=true면]
    → WFC: task status → waiting_for_human
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

 6. Summarizer Agent → PR title/body + task_summary 생성

 7. WFC → PR 생성
    → [auto_merge=true] gh pr merge
    → [auto_merge=false] task status → pending_review
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

Reporter가 subtask retry 한도를 초과했다고 판단하면 re-plan 요청.

```
Reporter → task status: "needs_replan"
  → WFC → Planner Agent 재기동
  → [review_replan=true면] 사용자 승인 대기
  → 새 plan으로 subtask loop 재개
```

### 5.5 루프백 규칙

| 실패 지점 | 대상 | 전달 내용 |
|-----------|------|-----------|
| Review 거절 | Coder | 리뷰 피드백 |
| Setup 실패 | Coder | 빌드/기동 에러 로그 |
| Unit Test 실패 | Coder | 실패 테스트명 + 에러 메시지 |
| E2E Test 실패 | Coder | 실패 시나리오 + 스크린샷 경로 |
| Reporter: 재시도 | Coder | 종합 피드백 |
| Reporter: re-plan | Planner | 실패 사유 + 전체 히스토리 |
| Reporter: 포기 | Task Manager | 에스컬레이션 |

---

## 6. Communication Structure

### 6.1 실행장비 내부 (같은 머신)

로컬 파일 + `.ready` sentinel 방식. Atomic write: tmp 파일 → os.replace().

### 6.2 TM ↔ WFC 통신

| 경로 | 방식 | 트리거 |
|------|------|--------|
| TM → WFC | commands/ 디렉토리에 .command 파일 생성 | WFC 폴링 |
| WFC → TM | project_state.json 업데이트 | TM 폴링 |
| TM ← task | tasks/{id}.ready sentinel | TM 폴링 |

### 6.3 실행장비 ↔ 테스트장비 (E2E, Phase 1.3)

- 실행장비 → 테스트장비: `.ready`를 테스트장비가 SSH 감시
- 테스트장비 → 실행장비: SCP + SSH touch `.ready`

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
  "status": "idle | running | waiting_for_human",
  "current_task_id": null,
  "last_updated": "2026-04-03T00:00:00Z",
  "overrides": { ... },
  "update_history": [ ... ]
}
```

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

**status 값:** `submitted` → `queued` → `planned` → `waiting_for_human` → `in_progress` → `pending_review` → `completed` / `needs_replan` / `escalated` / `failed` / `cancelled`

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
│   └── hub_api/                        # 공통 인터페이스 라이브러리
│       ├── __init__.py                 # 패키지 exports
│       ├── core.py                     # HubAPI 클래스 (submit, approve, reject 등)
│       ├── models.py                   # 데이터 모델 (SubmitResult, TaskSummary 등)
│       └── protocol.py                 # Protocol layer (Request/Response, dispatch)
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
├── docs_for_claude/
│   ├── 003-agent-system-spec-v4.md     # v4 설계 (현행 기준 문서) ★
│   ├── 005-design-history-archive.md
│   └── 008-handoff-phase-1.5-complete.md
│
├── docs_history/                       # 이전 버전 아카이브
│   └── 003-agent-system-spec-v2.md
│
└── projects/                           # 프로젝트별 디렉토리 (runtime, gitignored)
    └── {name}/
        ├── project.yaml                # git 관리
        ├── project_state.json          # 동적 상태
        ├── tasks/                      # task/subtask JSON + .ready sentinel
        ├── handoffs/                   # E2E 요청/결과
        ├── commands/                   # TM → WFC 명령 전달
        ├── attachments/                # 첨부파일
        ├── logs/                       # agent 실행 로그
        └── archive/                    # 완료된 task 아카이브
```

---

## 11. CLI Interface

### 11.1 시스템 관리 (run_system.sh)

```bash
./run_system.sh start [--dummy]    # Task Manager 백그라운드 실행
./run_system.sh stop [--force]     # Task Manager 종료
./run_system.sh status             # 시스템 상태 출력
```

### 11.2 Task 관리 (run_agent.sh)

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

## 12. Claude Code Session Management

### 12.1 세션 생성 규칙

- 각 agent는 subtask 단위로 새 세션 생성
- 세션 간 컨텍스트 공유는 JSON 파일로만 수행
- CLAUDE.md는 정적 프로젝트 지식만

### 12.2 Agent 기동 래퍼

`scripts/run_claude_agent.sh`: 8개 agent 지원, dummy/dry-run/force-result 모드, step numbering, stdout/stderr .log 캡처.

**Step numbering:**

| Agent | Step |
|-------|------|
| planner | 01 |
| setup | 02 |
| coder | 03 |
| reviewer | 04 |
| unit_tester | 05 |
| e2e_tester | 06 |
| reporter | 07 |
| summarizer | 08 |

---

## 13. 설계 결정 배경 요약

### 13.1 왜 프로젝트별 WFC 프로세스인가?
inotifywait 기반 blocking loop와 `claude -p` blocking 호출 특성상 프로세스 레벨 격리가 자연스럽다.

### 13.2 왜 파일 기반 통신인가?
port/소켓 없이 기존 agent 간 통신과 일관. debug 시 파일을 직접 읽을 수 있다.

### 13.3 왜 프로젝트 내 task 직렬인가?
같은 repo에서 병렬 작업 시 git 충돌. 병렬 필요하면 별도 clone + 별도 project.

### 13.4 왜 bypass인가? (skip이 아닌)
`claude -p` 한 번 호출에도 세션 비용. pipeline 구성 시점에 아예 빼는 게 효율적.

### 13.5 왜 4계층 설정인가?
시스템/프로젝트(정적)/프로젝트(동적)/task 4단으로 각 레벨의 변경을 독립적으로 처리.

### 13.6 절대경로 필수 규칙
agent가 `cd`로 codebase로 이동하므로 상대경로가 깨짐.

### 13.7 hub_api를 별도 프로세스가 아닌 라이브러리로
CLI/메신저/웹이 같은 Python 라이브러리를 import. 서버 없이 파일 직접 조작. Phase 2에서 웹 API 서버가 필요해지면 hub_api 위에 FastAPI를 올리는 구조.

---

## 14. 현재 구현 상태 및 TODO

> 최종 업데이트: 2026-04-06

### 14.1 구현 완료

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
| | `scripts/hub_api/protocol.py` | Protocol layer: Request/Response envelope, dispatch, ACTION_REGISTRY (14 actions) |
| | HubAPI: `get_task()`, `mark_notification_read()` | 단건 조회, 알림 읽음 처리, submit source 파라미터 |
| | `templates/config.yaml.template` chatbot 섹션 | model (sonnet), confirmation_mode (smart) |
| | `run_agent.sh` chat 서브커맨드 | `./run_agent.sh chat [--session <id>] [--list-sessions]` |
| | `session_history/chatbot/` | 세션 이력 저장 (YYYYMMDD_HHMMSS_xxxx.json) |
| **Phase 1.6** | HubAPI: `create_project()` | Chatbot/Protocol 경유 프로젝트 생성, `__UNCONFIGURED__` 플레이스홀더 |
| | HubAPI: `resubmit()` | cancelled/failed task를 새 task로 재제출 |
| | HubAPI: `get_plan()` | task의 plan.json(subtask 목록, 전략) 조회 |
| | HubAPI: `_validate_task_is_active()` | 종료 상태 task에 resume/pause 차단 |
| | `scripts/hub_api/protocol.py` | ACTION_REGISTRY 확장 (14 → 17개: create_project, resubmit, get_plan) |
| | `scripts/hub_api/protocol.py` | `ErrorCode.PROJECT_ALREADY_EXISTS` 추가, ValueError/FileExistsError 예외 처리 |
| | `scripts/chatbot.py` | action 선택 가이드(resume vs resubmit), get_plan/resubmit 결과 포맷터 |
| | `scripts/init_project.py` | `UNCONFIGURED_PLACEHOLDER` 상수, `main()` → HubAPI.create_project() 리팩터 |
| | WFC: `ensure_gh_auth()` | project.yaml auth_token 없으면 config.yaml github_token fallback |
| | `setup_environment.sh` | 시스템 환경 초기화: 도구 검증(python3, git, gh, claude) + venv + 설정 + 디렉토리 |
| | `tests/conftest.py` | session-scope cleanup fixture (테스트 프로젝트 잔재 자동 삭제) |
| **테스트** | `tests/` (177개) | Unit/Integration/E2E 테스트 스위트 |
| | `run_test.sh` | 테스트 실행 스크립트 (unit/integration/e2e/all) |
| | `pytest.ini` | pytest 설정 |

### 14.2 검증 완료 항목

| 항목 | 검증 방법 |
|------|-----------|
| 더미 파이프라인 사이클 | run_dummy_pipeline.sh |
| 실제 task 실행 (00002~00006) | test-web-service 대상 실제 claude -p 실행 |
| Git 자동화 | branch 생성, subtask 커밋, push, PR 생성/머지 |
| auto_merge=true/false | task 00005 (자동 머지), task 00006 (pending_review) |
| Replan 로직 | task 00099: dummy 모드, reporter force_result=replan |
| Safety limits | check_safety_limits.py 초과 시 agent 차단 |
| gh 인증 자동화 | ensure_gh_auth() + project.yaml auth_token + config.yaml github_token fallback |
| TM → WFC spawn | .ready 감지 → WFC 자동 실행 → 완료 감지 |
| Task 큐 블로킹 | 미완료 task 존재 시 spawn 차단, 완료 후 해제 |
| wait_for_prev_task_done=false | 프로젝트 override 시 블로킹 비활성화 |
| human review 함수 | request/wait/approve/timeout/cancel 단위 테스트 |
| run_system.sh status | waiting_for_human 노란색 표시 |
| CLI 전수 테스트 | submit, list, pending, approve, reject, feedback, config, pause, resume, cancel + 에러 케이스 |

### 14.3 미구현 (TODO)

| 범위 | 내용 | 예정 Phase |
|------|------|-----------|
| **메신저 연동** | Slack/Telegram 메시지 수신 → task 생성. Chatbot 경유 | 2.2 |
| **user_preferences slot** | project_state.json에 사용자 선호 저장. 기존 4계층 내 처리 | 2.0 |
| **E2E 테스트장비 연동** | e2e_watcher.sh, 크로스 머신 handoff, SSH 복구 | 1.7 |
| **로컬 E2E 테스트** | 브라우저 없는 API E2E를 로컬 실행. mode: local/remote 옵션 | 1.7 |
| **GH_TOKEN 환경변수 전환** | 멀티유저 시 gh 토큰을 환경변수로 격리 (현재 시스템 로그인 공유) | 2.0 |
| **Pipeline resume** | 실패 지점부터 자동 재개 (TM 연동). 정책 미결정: resume vs 취소+재등록 | 2.0 |
| **SQLite 전환 (선택적)** | 대화 이력, 알림 이력, task 조회 캐시. task JSON은 source of truth 유지 | 2.0 |
| **웹 대시보드** | Task 목록/상세/Timeline, 프로젝트별 필터링 | 2.1 |
| **Merge conflict 처리** | git_merge_pr() 에러 시 사용자 noti | 2.0 |
| **task 순서 변경** | 큐 내 task 우선순위 변경 | 2.0 |
| **강제 실행 옵션** | wait_for_prev_task_done 무시 force 타입 요청 | 2.0 |

### 14.4 Phase 로드맵

| Phase | 내용 | 상태 |
|-------|------|------|
| **1.0** | 수동 pipeline 실행 + git 자동화 | **완료** |
| **TM** | Task Manager + CLI + hub_api + human review + 큐 블로킹 | **완료** |
| **1.4** | 운영 안정화: 알림, Usage check, 재알림, 테스트 스위트 | **완료** |
| **1.5** | Chatbot 레이어, 세션 관리, 프로토콜 body (14 actions) | **완료** |
| **1.6** | Chatbot 사용성: create_project, resubmit, get_plan, resume 검증, 환경 초기화 (17 actions, 177개 테스트) | **진행중** |
| **1.7** | E2E 테스트장비 연동, 로컬 E2E | 미착수 |
| **2.0** | 고급 기능: Pipeline resume, SQLite, user_preferences, GH_TOKEN 환경변수, Merge conflict, task 순서 변경 | 미착수 |
| **2.1** | 웹 대시보드 + Web monitor & chat | 미착수 |
| **2.2** | Messenger (Slack/Telegram) | 미착수 |

참고: Phase 1.1(파이프라인 자동화)과 1.2(Planner+Subtask Loop)는 Phase 1.0에서 WFC로 통합 구현됨. E2E 테스트장비 연동은 1.3→1.7로 재배치.
