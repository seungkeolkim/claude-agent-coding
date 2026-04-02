# Agent System Architecture Specification v2

> Claude Code CLI 기반 24시간 자동 개발 시스템  
> 최종 정리: 2026-04-02  
> 이전 버전: `docs_for_claude/000-agent-system-spec.md` (v1, 폐기)  
> 설계 히스토리: `docs_for_claude/005-design-history-archive.md`

---

## 문서 간 관계

| 문서 | 역할 | 참고 수준 |
|------|------|-----------|
| **`003-agent-system-spec-v2.md` (이 문서)** | **현행 설계 명세. 모든 구현은 이 문서를 기준으로 합니다.** | **기준 문서** |
| `005-design-history-archive.md` | v1→v2 변경 배경 + Phase 1.0 핸드오프 병합 아카이브 | 설계 배경이 궁금할 때 |
| `000-agent-system-spec.md` | v1 설계. 폐기. | 참고 불필요 |
| `001-handoff-session-01.md` | Session 01→02 핸드오프. 폐기. | 참고 불필요 |

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
| 상주 (long-running) | Workflow Controller | 프로젝트 활성~비활성 | 프로젝트당 1개 |
| 일회성 (per-invocation) | Agent (Planner, Coder 등) | `claude -p` 실행~종료 | subtask당 1개씩 |

### 3.2 프로세스 계층

```
[상주] Task Manager (1개)
  │
  ├── [상주] Workflow Controller — project A
  │     ├── [일회성] claude -p planner ...     (끝나면 종료)
  │     ├── [일회성] claude -p coder ...       (끝나면 종료)
  │     ├── [일회성] claude -p reviewer ...    (끝나면 종료)
  │     └── ...
  │
  ├── [상주] Workflow Controller — project B
  │     ├── [일회성] claude -p planner ...
  │     └── ...
  │
  └── (프로젝트가 없으면 WFC도 없음)
```

### 3.3 Task Manager의 역할

Task Manager는 시스템의 유일한 외부 인터페이스이자, 모든 WFC 프로세스의 라이프사이클 관리자:

- **task 접수 및 라우팅:** CLI submit(또는 메신저) → 프로젝트 식별 → 해당 프로젝트의 task 큐에 적재
- **WFC 라이프사이클 관리:** 프로젝트 추가 시 WFC spawn, 시스템 기동 시 기존 프로젝트 복구
- **human interaction 통합:** 어떤 프로젝트든 CLI/메신저로 통합 응답
- **system-wide 현황 조회:** `projects/*/project_state.json`을 glob 읽기로 전체 현황 조합
- **알림 발송:** 프로젝트별 설정에 따른 완료/실패/에스컬레이션 알림

### 3.4 Workflow Controller의 역할

각 WFC는 하나의 프로젝트를 전담:

- **task 감시:** 자기 프로젝트의 `tasks/` 디렉토리에서 `.ready` sentinel 감지 (inotifywait)
- **파이프라인 실행:** Planner → subtask loop → integration test → commit/PR
- **설정 해소:** 매 subtask 전에 4계층 config merge → pipeline 구성 결정
- **명령 수신:** `commands/` 디렉토리 감시 (TM이 파일로 전달하는 pause, resume, cancel 등)
- **상태 보고:** `project_state.json` 업데이트 (TM이 읽기)
- **한도 체크:** 매 agent 호출 전 counters vs limits 비교
- **git 작업:** branch 생성 (Planner 후), subtask별 commit+push, Summarizer 후 PR 생성, auto_merge 시 PR 머지
  - `ensure_gh_auth()`: 매 git 작업 전 gh CLI 인증 + repo 권한 확인
  - branch 네이밍: `feature/{task_id}-{영문설명}` (Planner 제안, WFC 접두사 보장)
  - PR title: `[{task_id}] 한국어 제목` (Summarizer 생성, WFC가 접두사 추가)

### 3.5 TM ↔ WFC 통신

port나 소켓 없이, 파일 + sentinel 기반으로 통신:

```
TM → WFC 명령 전달:
  projects/{name}/commands/{명령}.command 파일 생성
  예) pause.command, resume.command, cancel-00042.command

WFC → TM 보고:
  projects/{name}/project_state.json 업데이트 (TM이 읽기)

WFC ← task 수신:
  projects/{name}/tasks/{id}.ready sentinel 감지
```

### 3.6 프로젝트 내 task 실행 규칙

**하나의 프로젝트 내에서 task는 직렬 실행된다.** 같은 repo에서 병렬 feature 작업이 필요하면, 별도 clone + 별도 project로 구성한다. (git worktree 자동 관리는 추후 검토)

---

## 4. Agent Catalog

### 4.1 Orchestration Layer (장비2 상주)

#### Task Manager

- **역할:** 유일한 외부 인터페이스 + WFC 라이프사이클 관리
- **입력:** CLI submit 명령 (Phase 1.0~1.3) / 메신저 메시지 (Phase 1.4)
- **출력:** 완료/실패/에스컬레이션 알림, WFC spawn/kill
- **상세:**
  - 작업 큐 관리 및 우선순위 결정
  - `projects/{name}/tasks/{id}.json` 생성
  - 첨부 이미지 다운로드 → `projects/{name}/attachments/{id}/`에 저장
  - `projects/` 디렉토리 감시 → 새 project.yaml 감지 시 WFC 자동 spawn
  - human interaction 요청 시 CLI pending/approve/reject 대응 (Phase 1.0~1.3)
  - usage threshold 확인 후 task 시작 허가/대기 결정
  - 24시간 상주 프로세스

#### Workflow Controller (프로젝트당 1개)

- **역할:** 내부 파이프라인 제어
- **입력:** task JSON (status 변화 감지), commands/ 디렉토리의 명령 파일
- **출력:** agent 기동, git branch/commit/PR (git 활성화 시), project_state.json 업데이트
- **상세:**
  - task 1개에 대해 파이프라인 전체를 책임
  - 매 subtask 전에 4계층 설정 merge → pipeline 구성 결정
  - Planner → subtask loop → integration test → commit/PR 순서 제어
  - 매 agent 호출 전 counters vs limits 비교
  - 매 agent 호출 전 usage threshold 확인
  - testing 설정에 따라 불필요한 agent를 pipeline에서 bypass (호출 자체 안 함)
  - git.enabled=false면 branch 생성, commit, PR 등 모든 git 작업을 건너뜀
  - 한도 초과 시 즉시 중단 및 에스컬레이션
  - 자기 프로젝트의 tasks/, handoffs/, commands/ 디렉토리를 inotifywait로 감시
  - 장애 시 project_state.json에서 상태 복구 가능 (stateful)

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
  - testing 설정 참고하여 require_e2e 결정
  - re-plan 요청 시 완료된 subtask의 changes_made를 참고하여 남은 계획 재구성

### 4.3 Worker Layer (subtask당 순차 실행)

#### Coder Agent

- **위치:** 실행장비
- **역할:** 코드 작성
- **입력:** subtask 정의 + prior_changes + guidance + 첨부 이미지 (해당 시)
- **출력:** 코드 변경, changes_made 기록
- **상세:**
  - primary_responsibility에 집중
  - E2E 검증에 필요한 범위는 최소한으로 함께 구현
  - 이전 subtask의 변경 맥락(prior_changes)을 인지한 상태에서 작업
  - mid_task_feedback 있으면 반영

#### Review Agent

- **위치:** 실행장비
- **역할:** 코드 리뷰
- **입력:** Coder의 변경 파일 목록 + diff
- **출력:** 승인 or 거절 (거절 시 피드백과 함께 Coder 루프백 지시)
- **상세:**
  - 아키텍처 일관성, 보안, 코딩 컨벤션 검사
  - subtask의 guidance를 벗어난 과도한 변경 탐지

#### Setup Agent

- **위치:** 실행장비
- **역할:** 환경 구성 및 프로그램 기동
- **입력:** 현재 코드베이스 상태
- **출력:** 기동 성공/실패 상태
- **상세:**
  - dependency 설치, 빌드, 서버 기동
  - testing이 전부 disabled면 pipeline에서 bypass
  - 기동 실패 시 에러 로그와 함께 Coder로 루프백

#### Unit Test Agent

- **위치:** 실행장비
- **역할:** 코드 레벨 테스트
- **입력:** 기동된 환경 + testing.unit_test 설정
- **출력:** 테스트 결과 (pass/fail, 실패 상세)
- **상세:**
  - testing.unit_test.enabled가 false면 pipeline에서 bypass (호출 자체 안 함)
  - 지정된 suite만 실행 (task override 또는 config default)
  - 실패 시 Coder로 루프백

#### E2E Test Agent

- **위치:** 테스트장비 (Windows host)
- **역할:** 브라우저 기반 통합 테스트
- **입력:** handoff JSON (테스트 대상 URL, 시나리오, 참조 이미지)
- **출력:** 테스트 결과 + 스크린샷
- **상세:**
  - testing.e2e_test.enabled가 false면 pipeline에서 bypass (호출 자체 안 함)
  - Playwright/Puppeteer로 Windows host의 Chrome 제어
  - 첨부된 UI 목업과 구현 결과 비교 가능
  - 결과 JSON + 스크린샷을 SCP로 장비2에 업로드

#### Reporter Agent

- **위치:** 실행장비
- **역할:** 결과 종합 및 판정
- **입력:** 모든 테스트 결과 (또는 테스트 없이 Review 결과만)
- **출력:** pass/fail 판정, 버그 리포트, changes_made 기록
- **상세:**
  - 통과: subtask status를 completed로 변경, changes_made 기록
  - 실패: 상세 피드백과 함께 Coder 루프백 지시
  - retry 한도 초과: re-plan 요청
  - re-plan 한도 초과: 에스컬레이션
  - testing이 전부 disabled면 pipeline에서 bypass (Reviewer 승인 → 바로 커밋)

#### Summarizer Agent

- **위치:** 실행장비
- **역할:** 완료된 task의 작업 요약 및 PR 메시지 생성
- **입력:** plan.json, git diff (default_branch..HEAD), 완료된 subtask 목록
- **출력:** PR 제목/본문 + task 요약
- **상세:**
  - 전체 task에서 수행된 코드 변경사항을 `git diff`/`git log`으로 분석
  - PR title: 한국어, WFC가 `[{task_id}]` 접두사 자동 추가
  - PR body: 한국어 markdown (Summary, Changes, Test Plan)
  - task_summary: 한국어, 비개발자도 이해할 수 있는 수준
  - 코드를 수정하지 않음 (읽기 전용)
  - step_number: 08

---

## 5. Workflow

### 5.1 전체 사이클

```
 1. 사용자: CLI submit (또는 메신저)
    → Task Manager: 프로젝트 식별 → projects/{name}/tasks/{id}.json 생성
    → usage threshold (70%) 확인 → 미달이면 대기

 2. Workflow Controller (해당 프로젝트)
    → 새 task 감지
    → 4계층 설정 merge → effective config 생성
    → Planner Agent 기동

 3. Planner Agent
    → 코드베이스 + 첨부 이미지 분석
    → plan 생성 (subtask 배열 + branch_name 제안)
    → [review_plan=true면] Plan 승인 대기

 3a. Workflow Controller
    → Planner 결과에서 branch_name 추출
    → git branch 생성: feature/{task_id}-{영문설명} (Planner 제안 우선, fallback: feature/{task_id})
    → feature/{task_id}- 접두사 보장

 4. Workflow Controller
    → plan의 subtask를 순차 실행

    ┌── Subtask Loop ──────────────────────────────────────────┐
    │                                                           │
    │  시작 전: 4계층 설정 merge → pipeline 구성 결정             │
    │  시작 전: usage threshold (80%) 확인 → 미달이면 대기        │
    │                                                           │
    │  4a. Coder Agent: 코드 작성                                │
    │      → usage threshold (90%) 확인 후 호출                  │
    │      → changes_made 기록                                  │
    │                                                           │
    │  4b. Review Agent: 코드 리뷰                               │
    │      → usage threshold (90%) 확인 후 호출                  │
    │      → 거절 시 Coder로 루프백                               │
    │                                                           │
    │  [testing이 하나라도 enabled면]                             │
    │  4c. Setup Agent: 환경 구성 및 기동                         │
    │      → usage threshold (90%) 확인 후 호출                  │
    │      → 기동 실패 시 Coder로 루프백                          │
    │                                                           │
    │  [unit_test.enabled=true면]                                │
    │  4d. Unit Test Agent                                      │
    │      → usage threshold (90%) 확인 후 호출                  │
    │      → 지정 suite만 실행                                   │
    │      → 실패 시 Coder로 루프백                               │
    │                                                           │
    │  [e2e_test.enabled=true면]                                 │
    │  4e. E2E Test Agent (테스트장비)                            │
    │      → 실패 시 Coder로 루프백                               │
    │                                                           │
    │  [testing이 하나라도 enabled면]                             │
    │  4f. Reporter Agent: 결과 종합                             │
    │      → usage threshold (90%) 확인 후 호출                  │
    │      → 통과: subtask 커밋 → 다음 subtask                   │
    │      → 실패 (retry 이내): Coder로 루프백                    │
    │      → 실패 (retry 초과): re-plan 요청                     │
    │      → 실패 (re-plan 초과): 에스컬레이션                    │
    │                                                           │
    │  [testing이 전부 disabled면]                               │
    │  4g. Review 승인 → 바로 커밋 → 다음 subtask                │
    │                                                           │
    └───────────────────────────────────────────────────────────┘

 5. Integration Test (모든 subtask 완료 후)
    → [integration_test.enabled=false면 bypass]
    → 지정 suite + E2E (include_e2e=true면) 실행
    → 실패 시 에스컬레이션

 6. Summarizer Agent
    → git diff/log로 전체 변경사항 분석
    → PR title/body + task_summary 생성

 7. Workflow Controller
    → Summarizer 결과로 PR 생성 (gh pr create)
    → PR title에 [{task_id}] 접두사 자동 추가
    → [auto_merge=true면] gh pr merge --merge --delete-branch
    → [auto_merge=false면] task status를 pending_review로 설정

 8. Task Manager
    → 완료 노티 (CLI stdout 또는 메신저)
    → [auto_merge=false면] PR 생성됨 알림
```

### 5.2 Pipeline 구성 결정 로직

매 subtask 시작 전에 WFC가 effective config의 testing 설정을 읽고, 해당 subtask에서 실행할 agent 목록을 생성한다. 비활성화된 agent는 pipeline에 포함하지 않는다 (bypass, skip이 아님).

```
전부 disabled:  [coder, reviewer] → 커밋
unit만 enabled: [coder, reviewer, setup, unit_tester, reporter] → 커밋
e2e만 enabled:  [coder, reviewer, setup, e2e_tester, reporter] → 커밋
전부 enabled:   [coder, reviewer, setup, unit_tester, e2e_tester, reporter] → 커밋
```

이 결정이 매 subtask마다 이루어지므로, 사용자가 subtask 사이에 project_state.json을 변경하면 다음 subtask부터 즉시 반영된다.

### 5.3 Subtask 간 컨텍스트 전달

각 subtask는 이전 subtask의 결과 위에서 작업. 파일 scope 겹침 허용.

```
Subtask 1 완료 → 커밋
  │  changes_made: ["api/auth/login.py 생성", "src/pages/login/index.tsx 최소 구현"]
  ▼
Subtask 2 시작
  Coder 컨텍스트:
  - subtask 2의 primary_responsibility + guidance
  - prior_changes: subtask 1의 changes_made 전체
  - "login/index.tsx는 이전 subtask에서 최소 구현됨. 이 파일을 확장"
```

### 5.4 Re-plan

Reporter가 subtask retry 한도를 초과했다고 판단하면 re-plan 요청.

```
Reporter → task status: "needs_replan"
  ↓
Workflow Controller → Planner Agent 재기동
  ↓
Planner: 완료된 subtask의 changes_made 참고, 남은 subtask 재구성
  ↓
[review_replan=true면] re-plan 승인 대기
  ↓
Workflow Controller: 새 plan으로 subtask loop 재개
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
| Reporter: 포기 | Task Manager | 에스컬레이션 (사람에게 알림) |

---

## 6. Communication Structure

### 6.1 실행장비 내부 (같은 머신)

로컬 파일 + `.ready` sentinel 방식.

쓰기 패턴 (atomic write):
```bash
# 1. tmp 파일에 쓰기
echo '{"task_id": "00042", ...}' > projects/my-app/tasks/00042.json.tmp
# 2. rename (atomic)
mv projects/my-app/tasks/00042.json.tmp projects/my-app/tasks/00042.json
# 3. sentinel 생성 (읽기 트리거)
touch projects/my-app/tasks/00042.ready
```

Workflow Controller가 `.ready` 파일 생성을 `inotifywait`로 감지:
```bash
inotifywait -m -e create projects/my-app/tasks/ --include '\.ready$' |
while read dir event file; do
  task_id="${file%.ready}"
  # 다음 agent 기동 로직
done
```

### 6.2 TM → WFC 명령 전달

Task Manager가 특정 프로젝트의 WFC에게 명령을 전달할 때도 파일 기반:

```bash
# Task Manager가 명령 파일 생성
touch projects/my-app/commands/pause.command
touch projects/my-app/commands/cancel-00042.command
```

WFC가 `commands/` 디렉토리를 inotifywait로 감시:
```bash
inotifywait -m -e create projects/my-app/commands/ --include '\.command$' |
while read dir event file; do
  # 명령 처리
done
```

### 6.3 실행장비 → 테스트장비 (E2E 테스트 요청)

실행장비에서 테스트장비로 직접 push 불가.
테스트장비가 실행장비를 SSH로 감시:

```powershell
# 테스트장비 (Windows host) PowerShell
while ($true) {
    ssh server2 "inotifywait -e create agent-hub/projects/my-app/handoffs/ --include '-e2e\.ready$' -q" |
    ForEach-Object {
        $file = $_.Trim().Split(" ")[-1]
        $jsonFile = $file -replace '\.ready$', '.json'

        # handoff 파일 가져오기
        scp server2:agent-hub/projects/my-app/handoffs/$jsonFile ./current_handoff.json

        # E2E Test Agent 실행 (Windows host에서 브라우저 제어)
        claude -p "$(Get-Content e2e-agent-prompt.md -Raw) $(Get-Content current_handoff.json -Raw)"
    }
    # SSH 끊김 시 재연결
    Start-Sleep 5
}
```

### 6.4 테스트장비 → 실행장비 (E2E 결과 전송)

SCP로 결과 업로드:
```powershell
scp ./e2e-result.json server2:agent-hub/projects/my-app/handoffs/00042-2-e2e-result.json
scp -r ./screenshots/ server2:agent-hub/projects/my-app/logs/00042/screenshots/
ssh server2 "touch agent-hub/projects/my-app/handoffs/00042-2-e2e-result.ready"
```

### 6.5 통신 요약

| 경로 | 방식 | 트리거 |
|------|------|--------|
| 실행장비 내 agent 간 | 로컬 파일 + .ready | inotifywait (로컬) |
| TM → WFC | 로컬 commands/ + .command | inotifywait (로컬) |
| WFC → TM | project_state.json 업데이트 | TM이 glob 읽기 |
| 실행장비 → 테스트장비 | .ready를 테스트장비가 감시 | inotifywait (SSH 원격) |
| 테스트장비 → 실행장비 | SCP + SSH touch .ready | 직접 실행 |

---

## 7. Configuration

### 7.1 4계층 설정 우선순위

```
config.yaml (시스템 기본값)
  → projects/{name}/project.yaml (프로젝트 정적 설정)
    → projects/{name}/project_state.json (프로젝트 동적 설정, 자연어로 변경 가능)
      → tasks/{id}.json의 config_override (task 단위 일시 변경)
```

뒤의 것이 앞의 것을 덮어씀. 없는 필드는 상위 계층의 값 사용.

### 7.2 config.yaml (시스템 설정)

시스템 전체에 적용되는 장비 정보, credential, Claude 모델 기본값, safety limits 기본값.
프로젝트와 무관한 인프라/환경 설정만 담는다.

```yaml
# ─── 장비 / 인프라 ───
machines:
  executor:
    ssh_key: "~/.ssh/id_rsa"
    git_credential_helper: "store"
    service_bind_address: "0.0.0.0"
  tester:
    browser: "chromium"
    viewport:
      width: 1280
      height: 720
    ssh_reconnect_interval_seconds: 5

# ─── Claude Code ───
claude:
  planner_model: "opus"
  coder_model: "sonnet"
  reviewer_model: "opus"
  e2e_tester_model: "sonnet"
  max_turns_per_session: 50

  # Usage 기반 실행 제어
  # 5시간 세션 사용량이 이 threshold 이상이면 해당 레벨의 실행을 대기
  usage_thresholds:
    new_task: 0.70            # 새 task 시작 허용 기준
    new_subtask: 0.80         # 새 subtask 시작 허용 기준
    new_agent_stage: 0.90     # pipeline 내 다음 agent 호출 허용 기준
  usage_check_interval_seconds: 60   # threshold 초과 시 재확인 주기

# ─── 안전 제한 기본값 (프로젝트별 override 가능) ───
default_limits:
  max_subtask_count: 5
  max_retry_per_subtask: 3
  max_replan_count: 2
  max_total_agent_invocations: 30
  max_task_duration_hours: 4

# ─── 사람 개입 정책 기본값 (프로젝트별 override 가능) ───
default_human_review_policy:
  review_plan: true
  review_replan: true
  review_before_merge: false
  auto_approve_timeout_hours: 24

# ─── 로깅 ───
logging:
  level: "info"
  archive_completed_tasks: true
  keep_session_logs: true

# ─── 알림 기본값 (프로젝트별 override 가능) ───
notification:
  channel: "cli"      # Phase 1.0~1.3: "cli", Phase 1.4: "slack" | "telegram" 등
```

### 7.3 project.yaml (프로젝트 정적 설정)

프로젝트별 고유 설정. 프로젝트 생성 시 한 번 작성하고, 이후 큰 변경이 없는 항목들.
시스템 기본값을 override하고 싶은 항목만 작성하면 된다.

```yaml
# projects/test-web-service/project.yaml

project:
  name: "test-web-service"
  description: |
    TypeScript + Next.js 웹 서비스. Tailwind CSS 사용. PostgreSQL + Prisma ORM.
    프론트엔드와 백엔드가 같은 repo에 있는 풀스택 프로젝트.
  default_branch: "main"

# ─── 코드베이스 ───
codebase:
  path: "/home/user/projects/test-web-service"    # 절대경로 필수
  service_bind_address: "0.0.0.0"
  service_port: 3000

# ─── Git ───
# enabled: false면 branch/commit/PR 등 모든 git 작업을 건너뜀
git:
  enabled: true
  provider: "github"              # github | bitbucket | gitlab (현재 github만 구현)
  remote: "origin"
  author_name: "agent-bot"
  author_email: "agent@example.com"
  auto_merge: false               # true: PR 생성 후 자동 머지 / false: PR만 생성 (pending_review)
  pr_target_branch: "develop"
  auth_token: ""                  # provider 인증 토큰 (GitHub PAT 등)

# ─── 테스트 ───
# 각 항목의 enabled 값은 project_state.json에서 동적으로 변경 가능
testing:
  unit_test:
    enabled: false
    available_suites:
      - name: "model"
        command: "pytest tests/models/"
        description: "ORM 모델 CRUD 테스트"
      - name: "api"
        command: "pytest tests/api/"
        description: "API 엔드포인트 테스트"
      - name: "service"
        command: "pytest tests/services/"
        description: "비즈니스 로직 테스트"
    default_suites: ["model", "api", "service"]

  e2e_test:
    enabled: false
    tool: "playwright"
    test_accounts:
      - email: "test@example.com"
        password: "password123"
        role: "user"
      - email: "admin@example.com"
        password: "admin123"
        role: "admin"

  integration_test:
    enabled: false
    suites: ["integration", "api"]
    include_e2e: false    # e2e_test.enabled=false면 이 값도 무시됨

# ─── 시스템 기본값 override (선택적, 필요한 항목만 작성) ───
# limits:
#   max_subtask_count: 10
#   max_retry_per_subtask: 3
#   max_replan_count: 2
#   max_total_agent_invocations: 30
#   max_task_duration_hours: 4
# claude:
#   coder_model: "opus"
#   max_turns_per_session: 50
# human_review_policy:
#   review_plan: false
#   review_replan: true
#   review_before_merge: false
#   auto_approve_timeout_hours: 24
# notification:
#   channel: "telegram"
#   telegram_chat_id: "-100123456789"
```

### 7.4 project_state.json (프로젝트 동적 설정)

사용자가 자연어 명령으로 변경하는 동적 상태.
project.yaml의 값을 runtime에서 덮어쓴다.
TM이 읽어서 system-wide 현황을 조합하는 데에도 사용.

```json
{
  "project_name": "test-web-service",
  "status": "idle",
  "current_task_id": null,
  "last_activity_at": "2026-04-01T10:00:00Z",

  "overrides": {
    "testing": {
      "unit_test": { "enabled": true },
      "e2e_test": { "enabled": false }
    }
  },

  "update_history": [
    {
      "field": "testing.unit_test.enabled",
      "from": false,
      "to": true,
      "reason": "사용자 요청: 이제 unit test 포함해서 돌려줘",
      "at": "2026-04-01T12:00:00Z"
    }
  ]
}
```

### 7.5 task.config_override (task 단위 일시 변경)

task JSON 내부의 `config_override` 필드. 해당 task에만 적용되고 끝나면 사라짐.

```json
{
  "config_override": {
    "testing": {
      "e2e_test": { "enabled": false }
    },
    "limits": {
      "max_retry_per_subtask": 5
    }
  }
}
```

### 7.6 설정 해소 (Effective Config 계산)

WFC가 subtask 실행 전에 수행하는 merge 로직:

```
1. config.yaml에서 default_limits, default_human_review_policy, claude 등을 읽음
2. project.yaml에서 해당 프로젝트의 testing, limits, human_review_policy 등을 읽어 덮어씀
3. project_state.json에서 overrides를 읽어 덮어씀
4. task.config_override에서 읽어 덮어씀
→ 최종 effective config
```

예시:
- config.yaml: `default_limits.max_retry_per_subtask: 3`
- project.yaml: (limits 미설정 → 시스템 기본값 사용)
- project_state.json: (overrides에 limits 없음)
- task.config_override: `limits.max_retry_per_subtask: 5`
- **effective:** `max_retry_per_subtask: 5`

---

## 8. Data Structures

### 8.1 Task (projects/{name}/tasks/00042.json)

```json
{
  "task_id": "00042",
  "project_name": "test-web-service",
  "title": "로그인 기능 구현",
  "description": "첨부된 UI 목업대로 로그인 페이지 구현. OAuth 포함",
  "submitted_via": "cli",
  "submitted_at": "2026-04-01T09:00:00Z",
  "status": "in_progress",
  "branch": "feature/00042-login-implementation",

  "attachments": [
    {
      "filename": "ui_mockup.png",
      "path": "attachments/00042/ui_mockup.png",
      "type": "ui_design",
      "description": "로그인 페이지 UI 목업"
    },
    {
      "filename": "architecture_diagram.jpg",
      "path": "attachments/00042/architecture_diagram.jpg",
      "type": "architecture",
      "description": "전체 인증 아키텍처"
    }
  ],

  "human_review_policy": {
    "review_plan": true,
    "review_replan": true,
    "review_before_merge": false,
    "auto_approve_timeout_hours": 24
  },

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

  "escalation_reason": null,

  "summary": "한국어 작업 요약 (Summarizer 생성, 완료 후 기록)",
  "pr_url": "https://github.com/owner/repo/pull/42"
}
```

**status 값:** `submitted` → `queued` → `planned` → `waiting_for_human` → `in_progress` → `pending_review` → `completed` / `needs_replan` / `escalated` / `failed` / `cancelled`

- `pending_review`: auto_merge=false일 때, PR이 생성되었지만 사람의 리뷰/머지를 기다리는 상태

**attachment type 값:** `ui_design` | `architecture` | `data_structure` | `reference`

주의: task JSON에는 testing이나 limits 필드가 없다. 이들은 4계층 merge로 결정된다. task 레벨에서 변경하려면 `config_override`를 사용.

### 8.2 Plan (projects/{name}/tasks/00042-plan.json)

```json
{
  "task_id": "00042",
  "plan_version": 1,
  "created_at": "2026-04-01T09:05:00Z",
  "strategy_note": "백엔드 우선 구현, 각 API마다 E2E 검증용 최소 UI 포함. 이후 프론트엔드 subtask에서 동일 파일을 확장",

  "subtasks": [
    {
      "subtask_id": "00042-1",
      "title": "User 모델 및 DB 마이그레이션",
      "primary_responsibility": "데이터 모델",
      "description": "User 테이블 생성, ORM 모델 정의, 마이그레이션 스크립트",
      "guidance": [
        "ORM과 마이그레이션만 작성",
        "UI 변경 없음"
      ],
      "depends_on": [],
      "require_e2e": false,
      "acceptance_criteria": "모델 생성 완료, 마이그레이션 성공, 기본 CRUD 테스트 통과",
      "reference_attachments": ["architecture_diagram.jpg"]
    },
    {
      "subtask_id": "00042-2",
      "title": "로그인 API + E2E 검증용 최소 UI",
      "primary_responsibility": "인증 API",
      "description": "POST /auth/login 엔드포인트 구현. E2E 검증을 위해 최소 로그인 폼 포함",
      "guidance": [
        "API 구현이 핵심",
        "E2E 통과를 위해 로그인 폼 최소 구현 필요",
        "프론트엔드 스타일링은 하지 않음"
      ],
      "depends_on": ["00042-1"],
      "require_e2e": true,
      "acceptance_criteria": "POST /auth/login 정상 응답, 잘못된 인증 시 401, 브라우저에서 로그인 가능",
      "reference_attachments": ["ui_mockup.png", "architecture_diagram.jpg"]
    },
    {
      "subtask_id": "00042-3",
      "title": "로그인 프론트엔드 완성",
      "primary_responsibility": "프론트엔드 UX",
      "description": "이전 subtask에서 최소 구현된 login/index.tsx를 확장",
      "guidance": [
        "00042-2에서 만든 login/index.tsx를 확장",
        "API 연동 구조는 유지, UI/UX 보강",
        "유효성 검사, 에러 표시, 로딩 상태 추가"
      ],
      "depends_on": ["00042-2"],
      "require_e2e": true,
      "acceptance_criteria": "전체 로그인 UX 완성, 에러 핸들링, E2E 전체 시나리오 통과",
      "reference_attachments": ["ui_mockup.png"]
    }
  ]
}
```

### 8.3 Subtask State (projects/{name}/tasks/00042-1.json)

```json
{
  "subtask_id": "00042-1",
  "status": "completed",
  "retry_count": 0,

  "prior_changes": [],

  "changes_made": [
    {
      "file": "models/user.py",
      "type": "created",
      "summary": "User ORM 모델, 이메일/비밀번호 필드, created_at"
    },
    {
      "file": "migrations/001_create_user.py",
      "type": "created",
      "summary": "users 테이블 생성 마이그레이션"
    },
    {
      "file": "tests/test_user_model.py",
      "type": "created",
      "summary": "User CRUD 기본 테스트"
    }
  ],

  "mid_task_feedback": [],

  "history": [
    {
      "agent": "coder",
      "action": "code_complete",
      "timestamp": "2026-04-01T09:10:00Z",
      "summary": "User 모델 및 마이그레이션 작성 완료",
      "session_log": "logs/00042/coder_subtask-1_attempt-1.log"
    },
    {
      "agent": "reviewer",
      "action": "approved",
      "timestamp": "2026-04-01T09:12:00Z",
      "summary": "컨벤션 준수, 구조 적절",
      "session_log": "logs/00042/reviewer_subtask-1.log"
    },
    {
      "agent": "unit_test",
      "action": "passed",
      "timestamp": "2026-04-01T09:15:00Z",
      "summary": "3/3 테스트 통과 (suite: model)",
      "session_log": "logs/00042/unit_test_subtask-1.log"
    },
    {
      "agent": "reporter",
      "action": "subtask_complete",
      "timestamp": "2026-04-01T09:16:00Z",
      "summary": "subtask 완료, 커밋 대상",
      "session_log": "logs/00042/reporter_subtask-1.log"
    }
  ]
}
```

### 8.4 E2E Handoff (projects/{name}/handoffs/00042-2-e2e.json)

```json
{
  "task_id": "00042",
  "subtask_id": "00042-2",
  "project_name": "test-web-service",
  "test_target_url": "http://192.168.1.100:3000",
  "reference_images": [
    {
      "path": "attachments/00042/ui_mockup.png",
      "description": "이 목업과 구현 결과를 비교"
    }
  ],
  "test_scenarios": [
    {
      "name": "정상 로그인",
      "steps": [
        "http://192.168.1.100:3000/login 접속",
        "이메일 필드에 test@example.com 입력",
        "비밀번호 필드에 password123 입력",
        "로그인 버튼 클릭",
        "대시보드 페이지로 리다이렉트 확인"
      ]
    },
    {
      "name": "잘못된 비밀번호",
      "steps": [
        "로그인 페이지에서 틀린 비밀번호로 로그인 시도",
        "에러 메시지 표시 확인"
      ]
    }
  ]
}
```

### 8.5 E2E Result (projects/{name}/handoffs/00042-2-e2e-result.json)

```json
{
  "task_id": "00042",
  "subtask_id": "00042-2",
  "overall_result": "fail",
  "executed_at": "2026-04-01T09:25:00Z",
  "test_results": [
    {
      "name": "정상 로그인",
      "result": "pass",
      "duration_seconds": 4.2
    },
    {
      "name": "잘못된 비밀번호",
      "result": "fail",
      "error_detail": "에러 메시지가 표시되지 않음. 로그인 버튼 클릭 후 페이지 변화 없음",
      "screenshot": "screenshots/00042-2_wrong_password_fail.png"
    }
  ]
}
```

### 8.6 Human Interaction (task JSON 내부)

```json
{
  "human_interaction": {
    "type": "plan_review",
    "message": "plan을 생성했습니다. 검토 후 승인/수정해주세요.",
    "payload_path": "tasks/00042-plan.json",
    "options": ["approve", "modify", "cancel"],
    "requested_at": "2026-04-01T09:05:00Z",
    "timeout_hours": 24,
    "response": {
      "action": "modify",
      "message": "소셜 로그인도 포함해줘. 구글, 카카오",
      "attachments": ["attachments/00042/social_login_flow.png"],
      "responded_at": "2026-04-01T09:30:00Z"
    }
  }
}
```

**type 값:** `plan_review` | `replan_review` | `merge_review` | `escalation`

---

## 9. Safety Limits & Usage Control

### 9.1 Safety Limits

| 제한 | 기본값 | 대상 | 초과 시 |
|------|--------|------|---------|
| max_subtask_count | 5 | Plan 레벨 | Planner가 에스컬레이션 |
| max_retry_per_subtask | 3 | Subtask 레벨 | re-plan 요청 |
| max_replan_count | 2 | Task 레벨 | 에스컬레이션 |
| max_total_agent_invocations | 30 | Task 레벨 | 강제 중단 |
| max_task_duration_hours | 4 | Task 레벨 | 강제 중단 |

모든 제한값은 config.yaml에 시스템 기본값, project.yaml에서 프로젝트별 override, task에서 config_override 가능.

### 9.2 Usage Threshold 기반 실행 제어

Claude Code CLI 구독(MAX plan)의 5시간 세션 사용량을 기준으로, 과사용을 방지하기 위한 단계별 threshold:

| 실행 레벨 | threshold 기본값 | 의미 |
|-----------|----------------|------|
| 새 task 시작 | 70% | 5시간 세션 사용량이 70% 미만일 때만 새 task를 시작 |
| 새 subtask 시작 | 80% | 80% 미만일 때만 새 subtask를 시작 |
| 다음 agent stage 호출 | 90% | 90% 미만일 때만 pipeline 내 다음 agent를 호출 |

threshold를 초과하면 `usage_check_interval_seconds` (기본 60초)마다 재확인하며 대기.
5시간 세션이 리셋되면 자연스럽게 풀림.

**참고:** `/usage` 값을 프로그래밍으로 가져올 수 있는지는 구현 단계에서 검증 필요. 불가능하면 세션 시작 시각 기반 추정 등 우회 방법 검토.

### 9.3 Concurrency 제한

| 항목 | 기본값 | 비고 |
|------|--------|------|
| max_concurrent_projects | unlimited | 동시에 pipeline이 돌 수 있는 프로젝트 수. 리소스 부족 시 설정. |

프로젝트 내에서 task는 항상 직렬 실행. 프로젝트 간 병렬 실행은 usage threshold로 자연 조절.

---

## 10. Directory Structure

### 10.1 agent-hub 레포 (git 관리)

```
claude-agent-coding/
├── config.yaml                         # 시스템 설정 (gitignored)
├── config.yaml.template                # 시스템 설정 템플릿
├── create_config.sh                    # 템플릿 → config.yaml 생성
├── run_agent.sh                        # 시스템 기동/종료/상태/submit CLI
├── activate_venv.sh                    # venv 활성화 스크립트
├── requirements.txt                    # Python 의존성
├── CLAUDE.md                           # 정적 프로젝트 지식 (짧게 유지)
├── README.md
│
├── scripts/
│   ├── task_manager.py                 # Task Manager 상주 프로세스
│   ├── workflow_controller.py          # Workflow Controller (프로젝트별 인스턴스)
│   ├── init_project.py                 # 대화형 프로젝트 초기화 스크립트
│   ├── run_claude_agent.sh             # Claude Code 세션 기동 래퍼
│   └── e2e_watcher.sh                  # 테스트장비용 E2E 감시 스크립트
│
├── config/
│   └── agent_prompts/
│       ├── planner.md                  # Planner Agent 역할 프롬프트
│       ├── coder.md                    # Coder Agent 역할 프롬프트
│       ├── reviewer.md                 # Review Agent 역할 프롬프트
│       ├── setup.md                    # Setup Agent 역할 프롬프트
│       ├── unit_tester.md              # Unit Test Agent 역할 프롬프트
│       ├── e2e_tester.md               # E2E Test Agent 역할 프롬프트
│       ├── reporter.md                 # Reporter Agent 역할 프롬프트
│       └── summarizer.md              # Summarizer Agent 역할 프롬프트
│
├── docs/                               # 사용자용 문서 (설정 레퍼런스 등)
│   └── configuration-reference.md
│
├── docs_for_claude/                    # Claude 세션용 내부 문서
│   ├── 000-agent-system-spec.md        # v1 설계 (폐기, 참고하지 마세요)
│   ├── 001-handoff-session-01.md       # Session 01 핸드오프 (폐기)
│   ├── 003-agent-system-spec-v2.md     # v2 설계 (현행 기준 문서) ★
│   └── 005-design-history-archive.md  # 설계 히스토리 아카이브
│
└── projects/                           # 프로젝트별 디렉토리
    └── (아래 10.2 참조)
```

### 10.2 프로젝트 디렉토리 구조

`projects/` 안에 프로젝트당 하나의 디렉토리. `project.yaml`만 git 관리, 나머지는 runtime 데이터.

```
projects/
├── test-web-service/
│   ├── project.yaml                    # git 관리 — 프로젝트 정적 설정
│   │
│   │  (이하 전부 gitignored — runtime 데이터)
│   ├── project_state.json              # 동적 상태
│   │
│   ├── tasks/
│   │   ├── 00042.json               # task 상태
│   │   ├── 00042.ready              # sentinel
│   │   ├── 00042-plan.json          # plan
│   │   ├── 00042-1.json             # subtask 상태
│   │   ├── 00042-2.json
│   │   └── ...
│   │
│   ├── handoffs/
│   │   ├── 00042-2-e2e.json         # E2E 요청
│   │   ├── 00042-2-e2e.ready        # 테스트장비 트리거
│   │   ├── 00042-2-e2e-result.json  # E2E 결과
│   │   └── 00042-2-e2e-result.ready
│   │
│   ├── commands/                       # TM → WFC 명령 전달
│   │   └── (pause.command, cancel-00042.command 등)
│   │
│   ├── attachments/
│   │   └── 00042/
│   │       ├── ui_mockup.png
│   │       └── architecture_diagram.jpg
│   │
│   ├── logs/
│   │   └── 00042/
│   │       ├── coder_subtask-1_attempt-1.log
│   │       ├── reviewer_subtask-1.log
│   │       ├── unit_test_subtask-1.log
│   │       └── screenshots/
│   │           └── 00042-2_wrong_password_fail.png
│   │
│   └── archive/                        # 완료된 task 아카이브
│       └── 00041/
│           └── ...
│
└── data-pipeline/
    ├── project.yaml
    ├── project_state.json
    ├── tasks/
    └── ...
```

### 10.3 .gitignore 패턴

```gitignore
# 시스템 설정 (credential 포함)
config.yaml

# 프로젝트 runtime 데이터
projects/*/project_state.json
projects/*/tasks/
projects/*/handoffs/
projects/*/commands/
projects/*/attachments/
projects/*/logs/
projects/*/archive/
```

---

## 11. Startup & CLI

### 11.1 시스템 기동

```bash
# 전체 시스템 기동 (사용자가 직접 실행)
# → TM 프로세스 시작 → projects/ 스캔 → 프로젝트마다 WFC spawn
./run_agent.sh start

# 상태 확인
./run_agent.sh status

# 전체 종료
./run_agent.sh stop
```

시스템 기동 흐름:
```
./run_agent.sh start
  1. config.yaml 로드
  2. Task Manager 프로세스 기동
  3. TM이 projects/ 디렉토리 스캔
  4. project.yaml이 있는 프로젝트마다:
     - project_state.json 있으면 로드 (이전 상태 복구)
     - project_state.json 없으면 초기화
     - WFC 프로세스 spawn
  5. 기존에 in_progress였던 task가 있으면 이어서 실행
  6. TM이 projects/ 디렉토리 watch 시작 (새 프로젝트 감지)
```

### 11.2 프로젝트 추가

```bash
# 대화형 프로젝트 초기화
./run_agent.sh init-project
```

init-project 흐름:
```
1. "프로젝트 이름은?"
2. "어떤 프로젝트인지 설명해주세요 (기술 스택, 목적 등)"
3. "기존 코드베이스가 있나요?"
   ├─ 있음 → "경로는?" → codebase_path 설정
   └─ 없음 → 경로 물어보고 → mkdir + git init
4. "git remote 연동할까요?"
   ├─ 있음 → remote URL 물어보기
   └─ 없음 → git.enabled: false
5. "테스트는 나중에 활성화할 수 있습니다. 기본 off로 시작합니다."

→ projects/{name}/ 디렉토리 생성
→ project.yaml 자동 작성
→ project_state.json 초기화 (testing 전부 off)
→ 하위 runtime 디렉토리 생성 (tasks/, handoffs/, commands/, logs/, archive/)
→ TM이 실행 중이면, 새 프로젝트 감지 → WFC 자동 spawn
```

### 11.3 Task 제출 (Phase 1.0~1.3)

```bash
# 대화형
./run_agent.sh submit --project test-web-service

# JSON 파일로
./run_agent.sh submit --project test-web-service --file my_task.json

# 인라인
./run_agent.sh submit --project test-web-service \
  --title "로그인 기능 구현" \
  --description "OAuth 포함" \
  --attach ui_mockup.png \
  --attach architecture.png

# 테스트 설정 override (이번 task만)
./run_agent.sh submit --project test-web-service \
  --title "README 오타 수정" \
  --test none

./run_agent.sh submit --project test-web-service \
  --title "User 모델 수정" \
  --test-unit model,service \
  --test-skip e2e
```

### 11.4 Human Interaction (Phase 1.0~1.3)

```bash
# 대기 중인 interaction 확인 (전체)
./run_agent.sh pending

# 특정 프로젝트만
./run_agent.sh pending --project test-web-service

# Plan 승인
./run_agent.sh approve 00042 --project test-web-service

# Plan 수정 요청
./run_agent.sh reject 00042 --project test-web-service \
  --message "소셜 로그인도 추가해줘" \
  --attach social_login_flow.png

# 진행 중 task에 피드백
./run_agent.sh feedback 00042 --project test-web-service \
  --message "remember me 체크박스 추가해줘"

# 프로젝트 설정 동적 변경
./run_agent.sh config --project test-web-service \
  --set "testing.unit_test.enabled=true"

# Task 일시정지 / 재개 / 취소
./run_agent.sh pause 00042 --project test-web-service
./run_agent.sh resume 00042 --project test-web-service
./run_agent.sh cancel 00042 --project test-web-service

# Task 목록 (전체)
./run_agent.sh list

# 특정 프로젝트만
./run_agent.sh list --project test-web-service
```

### 11.5 테스트장비 기동

```bash
# 테스트장비 (장비1/3/5) — E2E watcher 기동
./run_agent.sh start-tester --project test-web-service
```

---

## 12. Claude Code Session Management

### 12.1 세션 생성 규칙

- 각 agent는 subtask 단위로 새 세션 생성
- 세션 간 컨텍스트 공유는 JSON 파일로만 수행
- CLAUDE.md는 정적 프로젝트 지식만 (컨벤션, 아키텍처 설명 등)

### 12.2 Agent 기동 래퍼

`scripts/run_claude_agent.sh`가 모든 agent 실행을 담당. WFC가 호출한다.

```bash
# 사용법 (WFC에서 호출)
scripts/run_claude_agent.sh \
  --agent-type coder \
  --config /path/to/config.yaml \
  --project-yaml /path/to/project.yaml \
  --task-file /path/to/00042.json \
  --subtask-seq 1 \
  --agent-hub-root /path/to/claude-agent-coding

# 지원 옵션
#   --dummy      : Claude 호출 대신 더미 JSON 출력
#   --dry-run    : 프롬프트만 출력하고 종료
#   --model MODEL: 모델 강제 지정

# 지원 agent: planner, coder, reviewer, setup, unit_tester, e2e_tester, reporter, summarizer
```

**실행 흐름:**
1. config.yaml에서 `claude.{agent_type}_model` 읽기
2. project.yaml에서 모델 override 확인
3. safety limits 체크 (check_safety_limits.py 호출)
4. agent 프롬프트 + task/subtask/plan context 조합
5. `cd $CODEBASE && claude --model $MODEL -p "$PROMPT" --output-format json`
6. stdout/stderr를 `logs/{task_id}/` 하위 `.log` 파일에 캡처
7. JSON 결과 출력 (WFC가 파싱)

**Step numbering:**

| Agent | Step | Name |
|-------|------|------|
| planner | 01 | planner |
| setup | 02 | setup |
| coder | 03 | coder |
| reviewer | 04 | reviewer |
| unit_tester | 05 | unit_tester |
| e2e_tester | 06 | e2e_tester |
| reporter | 07 | reporter |
| summarizer | 08 | summarizer |

---

## 13. Phase Plan

### Phase 1.0 — 수동 단일 agent 실행

- task JSON을 손으로 작성해서 `projects/{name}/tasks/`에 넣기
- `run_agent.sh run <agent_type> --project <name> --task 00001` 으로 agent 하나를 직접 실행
- 결과 확인 후 다음 agent를 수동 실행
- **목표:** 각 agent의 프롬프트와 JSON 입출력 검증

### Phase 1.1 — 실행장비 내 파이프라인 자동화

- Workflow Controller가 `.ready` sentinel 감지 → 다음 agent 자동 기동
- Coder → Reviewer → Setup → Unit Test → Reporter 체인 자동 실행
- 실패 시 Coder 자동 루프백
- task 제출은 CLI 수동
- **목표:** 체인이 끊기지 않고 끝까지 흐르는지 검증

### Phase 1.2 — Planner + Subtask Loop

- Planner Agent 추가
- subtask 단위 커밋, prior_changes 전달
- re-plan 로직 및 safety limits
- CLI로 plan 승인/거절
- **목표:** subtask 분할 품질, 커밋 단위, 루프백/re-plan 검증

### Phase 1.3 — 테스트장비 연동 (E2E)

- 테스트장비에서 `run_agent.sh start-tester --project <name>` 기동
- SSH + inotifywait 감시, SCP 결과 전송
- E2E Test Agent 브라우저 테스트
- **목표:** 크로스 머신 handoff 안정성, SSH 끊김 복구 검증

### Phase 1.4 — 메신저 연동

- 이 시점에서 메신저 플랫폼 결정 (Slack/Telegram/Discord 등)
- Task Manager가 메시지 수신 → task JSON 생성
- 프로젝트별 채팅방 매핑 지원
- 진행 상황 업데이트, plan 승인 버튼, 에스컬레이션 알림
- 이미지 첨부 자동 다운로드
- CLI submit은 백업으로 유지
- **목표:** end-to-end가 메신저만으로 완결되는지 검증

### Phase 2 — 웹 모니터링 대시보드

- Task 목록/상세/Timeline 뷰 (멀티 프로젝트 통합)
- 프로젝트별 필터링
- 첨부 자료 뷰 (입력 이미지 + E2E 스크린샷 비교)
- 미사용 WFC 프로세스 종료 기능
- 장비2에서 웹서버 기동 (FastAPI 등)
- Phase 1에서 쌓인 JSON + logs를 read-only로 렌더링
- agent 시스템 자체의 첫 실전 task로 개발 가능

---

## 14. 설계 결정 배경 요약

이 섹션은 주요 설계 결정의 "왜"를 기록합니다. 더 자세한 내용은 `002-design-evolution-for-web-discussion.md`를 참고하세요.

### 14.1 왜 A안 (프로젝트별 WFC 프로세스)인가?

B안(단일 WFC 멀티플렉싱)이 관리는 단순하지만, inotifywait 기반 blocking loop와 `claude -p` blocking 호출 특성상 프로세스 레벨 격리가 자연스럽다. 한 프로젝트의 장애가 다른 프로젝트에 영향을 주지 않는다.

### 14.2 왜 파일 기반 통신인가?

TM-WFC 간 port/소켓 통신은 프로젝트 수만큼 port가 점유되고, 관리 복잡성이 증가한다. 파일 + sentinel 방식은 기존 agent 간 통신과 일관되고, inotifywait로 이벤트 기반 반응이 가능하며, debug 시 파일을 직접 읽을 수 있다.

### 14.3 왜 프로젝트 내 task 직렬인가?

같은 repo에서 병렬 작업 시 branch checkout, uncommitted changes, git lock 충돌이 발생한다. 병렬 feature가 필요하면 별도 clone + 별도 project로 구성한다. worktree 자동 관리는 복잡도 대비 현 단계에서 이득이 작아 추후 검토.

### 14.4 왜 bypass인가? (skip이 아닌)

`claude -p` 한 번 호출에도 세션 비용이 든다. agent를 호출한 뒤 내부에서 "아 disabled네" 하고 리턴하는 것(skip)보다, pipeline 구성 시점에 아예 빼는 것(bypass)이 효율적이다.

### 14.5 왜 usage threshold인가? (concurrent limit이 아닌)

MAX plan의 5시간 세션 사용량이 직접적인 병목이다. concurrent session 수를 제한하는 것보다, 사용량 자체를 보고 단계별로 조절하는 게 더 정확하다. 사용량이 넉넉하면 여러 프로젝트가 동시에 돌 수 있고, 부족하면 자연스럽게 대기한다.

### 14.6 왜 4계층 설정인가?

시스템/프로젝트/동적/task 4단으로 나누면, "이 프로젝트에서 이제부터 unit test 켜줘" (project_state 변경)와 "이번 task만 e2e 빼줘" (task override)를 각각 독립적으로 처리할 수 있다. 2계층(시스템/task)으로는 프로젝트 레벨 영속 변경을 표현할 수 없고, 3계층(시스템/프로젝트/task)으로는 정적 설정과 동적 변경을 구분할 수 없다.

### 14.7 절대경로 필수 규칙

config.yaml과 project.yaml의 모든 경로는 절대경로. agent가 `cd`로 codebase 디렉토리로 이동한 후 작업하므로, agent-hub 기준 상대경로가 깨진다.

### 14.8 템플릿 패턴

`config.yaml.template` → `create_config.sh` → `config.yaml` (gitignored). 민감 정보(경로, credential)가 git에 올라가는 것을 방지.

---

## 15. 현재 구현 상태 및 TODO

> 최종 업데이트: 2026-04-02

### 15.1 구현 완료

| 파일 | 설명 |
|------|------|
| `config.yaml.template` | v2 시스템 설정 템플릿 |
| `config.yaml` | 실제 시스템 설정 (gitignored) |
| `project.yaml.template` | 프로젝트 설정 템플릿 (전체 override 항목 포함) |
| `create_config.sh` | 템플릿 → config.yaml 생성 |
| `run_agent.sh` | CLI 진입점: run, pipeline, init-project, kill-all, help |
| `scripts/init_project.py` | 대화형 프로젝트 초기화 |
| `scripts/workflow_controller.py` | WFC 핵심: run_pipeline(), run_pipeline_from_subtasks(), finalize_task(), git 자동화, Summarizer 연동, replan 로직, safety limits 연동, 로그 rotation |
| `scripts/run_claude_agent.sh` | Claude Code 세션 래퍼: 8개 agent 지원, dummy/dry-run/force-result 모드, step numbering, stdout/stderr .log 캡처 |
| `scripts/check_safety_limits.py` | Safety limits 체크: 3계층 merge (config→project→task), 5개 limit 항목 |
| `config/agent_prompts/*.md` | 8개 agent 프롬프트 (planner, coder, reviewer, setup, unit_tester, e2e_tester, reporter, summarizer) |
| `CLAUDE.md` | 정적 프로젝트 지식 |
| `docs/configuration-reference.md` | 사용자용 설정 레퍼런스 (표 기반) |

### 15.2 Phase 1.0 검증 완료 항목

| 항목 | 검증 방법 |
|------|-----------|
| 더미 파이프라인 사이클 | run_dummy_pipeline.sh |
| 실제 task 실행 (00002~00006) | test-web-service 대상 실제 claude -p 실행 |
| Git 자동화 | branch 생성, subtask 커밋, push, PR 생성/머지 |
| auto_merge=true | task 00005: PR 생성 + 자동 머지 |
| auto_merge=false | task 00006: PR 생성, status=pending_review |
| Replan 로직 | task 00099: dummy 모드, reporter force_result=replan |
| Safety limits | check_safety_limits.py 초과 시 agent 차단 |
| gh 인증 자동화 | ensure_gh_auth() + project.yaml auth_token |

### 15.3 미구현 (TODO)

| 범위 | 내용 | 예정 Phase |
|------|------|-----------|
| **Task Manager** | CLI 인터페이스, WFC spawn/kill, 프로젝트 감시, human interaction, usage check, 알림 | 다음 (TM) |
| **human_review_policy 실행** | review_plan, review_replan, waiting_for_human 상태 전환 + 대기 로직 | TM |
| **project_state.json 연동** | WFC가 project_state.json 읽기/쓰기 (3계층 동적 override) | TM |
| **4계층 설정 merge (WFC 내부)** | WFC 자체의 testing/claude 모델 등 effective config 계산. 현재 check_safety_limits.py만 3계층 merge | TM |
| **알림** | 완료/실패/PR생성 시 사용자 알림 (cli/slack/telegram) | TM → Phase 1.4 |
| **Merge conflict 처리** | git_merge_pr()에 TODO. 에러 시 사용자 noti 필요 | Phase 2 이후 |
| **Pipeline resume** | 실패 지점부터 자동 재개 (run_pipeline_from_subtasks() 있으나 TM 연동 필요) | TM |
| **Chatbot 레이어** | TM 앞단 대화형 인터페이스. 자연어→구조화 명령 변환, 프로젝트 자동 식별 | Phase 1.4 |
| **Chatbot 세션 관리** | ~20턴 도달 시 Claude compact 방식으로 대화 요약(compress) 후 요약+최근 대화+project context로 세션 유지. 프로젝트 전환 시 context reload, 멀티프로젝트 세션 | Phase 1.4 |
| **user_preferences slot** | project_state.json에 사용자 선호 저장 영역 추가 (custom_instructions 등). 별도 계층 추가 없이 기존 4계층 내에서 처리 | Phase 1.4 |
| **SQLite 전환 (선택적)** | 대화 이력, 알림 이력, task 조회 캐시 등 필요 시점에 SQLite 도입. task JSON은 source of truth 유지 | 필요 시 |
| **프로토콜 body 정의** | Request/Response envelope, 에러 형식, 알림 이벤트 형식, 첨부파일 base64 인코딩 규격 | Phase 1.4 |
| **E2E 테스트장비 연동** | e2e_watcher.sh, 크로스 머신 handoff, SSH 복구 | Phase 1.3 |
| **메신저 연동** | Slack/Telegram 등 메시지 수신 → task 생성. Chatbot 레이어 경유 | Phase 1.4 |
| **웹 대시보드** | Task 목록/상세/Timeline, 프로젝트별 필터링 | Phase 2 |

### 15.4 Phase 로드맵 (현재 시점)

| Phase | 내용 | 상태 |
|-------|------|------|
| 1.0 | 수동 pipeline 실행 + git 자동화 | **완료** |
| TM | Task Manager (CLI 인터페이스, WFC 연동, human review) | **다음** |
| 1.3 | 테스트장비 연동 (E2E) | 미착수 |
| 1.4 | 메신저 연동 | 미착수 |
| 2.0 | 웹 모니터링 대시보드 | 미착수 |

참고: 원래 Phase 1.1(파이프라인 자동화)과 1.2(Planner+Subtask Loop)는 Phase 1.0에서 WFC로 통합 구현되어, 별도 Phase가 불필요해짐.
