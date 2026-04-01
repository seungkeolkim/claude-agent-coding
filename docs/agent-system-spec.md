# Agent System Architecture Specification

> Claude Code CLI 기반 24시간 자동 개발 시스템
> 최종 정리: 2026-04-01

---

## 1. System Overview

사용자가 CLI(또는 추후 메신저)로 작업을 요청하면, 복수의 agent가 순차적으로 코드 작성, 리뷰, 테스트를 수행하고 결과를 알림.
복잡한 작업은 Planner가 subtask로 분할하여 순차 실행하며, 각 subtask는 독립적으로 커밋 가능한 단위.

### 핵심 설계 원칙

- **단일 config:** `config.yaml` 하나로 전체 시스템 세팅. task별 override 가능.
- **역할 기반 기동:** 같은 코드베이스를 실행장비/테스트장비에서 `git pull`로 동기화하되, 기동 시 role에 따라 다른 프로세스를 실행.
- **책임 범위 기반 subtask:** 파일 격리가 아닌 primary_responsibility로 분할. scope 겹침 허용. prior_changes로 맥락 전달.
- **CLAUDE.md는 정적 지식만:** 동적 상태는 전부 JSON 파일로 관리.
- **테스트 선택적:** unit test, e2e test, integration test 각각 활성화/비활성화 가능.

---

## 2. Machine Roles

### 장비 구성

| 구분 | 장비 | OS | 스펙 | 역할 | 가동 |
|------|------|-----|------|------|------|
| 실행장비(메인) | 장비2 — 랙서버 | Ubuntu 20.04 | i7, 32GB, A5000 | Agent 허브, 코드베이스 기동 | 24h |
| 실행장비(서브) | 장비4 — 구형 PC | Ubuntu 24.04 | i7-6700, 32GB, 3060 | 보조 코드베이스 기동, 보조 agent | 24h |
| 테스트장비 | 장비1 — 회사 노트북 | Windows (WSL) | i9, 64GB, 3080ti | GUI 브라우저 E2E 테스트 | 업무 시간 |
| 테스트장비 | 장비3 — 집 PC | Windows (WSL) | R7 9800X3D, 96GB, 5080 | GUI 브라우저 E2E 테스트 | 수시 |
| 테스트장비 | 장비5 — 그램 노트북 | Windows (WSL) | i7-11th, 16GB | 외부 작업용 | 이동 시 |

### 접속 패턴

- 테스트장비 → 실행장비: SSH 가능
- 실행장비 → 테스트장비: SSH 불가 (push 불가)
- 모든 장비에서 장비2에 SSH 접근 가능
- Claude Code CLI는 실행장비(장비2/4)에서 직접 실행
- 테스트장비에서는 SSH로 접속 후 실행하거나, 로컬 Windows host에서 E2E agent 실행

### .claude 세션 관리

- `.claude` 폴더는 실행장비(장비2)에 위치
- VS Code extension 미사용 (호스트 측에 .claude가 생기는 문제 회피)
- 각 agent는 subtask 단위로 새 세션 생성 (세션 공유 불필요)
- CLAUDE.md는 세션 시작 시 1회 로드 (resume 시 재로드하지 않음)

---

## 3. Agent Catalog

### 3.1 Orchestration Layer (장비2 상주)

#### Task Manager

- **역할:** 유일한 외부 인터페이스
- **입력:** CLI submit 명령 (Phase 1.0~1.3) / 메신저 메시지 (Phase 1.4)
- **출력:** 완료/실패/에스컬레이션 알림
- **상세:**
  - 작업 큐 관리 및 우선순위 결정
  - `tasks/TASK-{id}.json` 생성
  - 첨부 이미지 다운로드 → `attachments/TASK-{id}/`에 저장
  - human interaction 요청 시 CLI pending/approve/reject 대응 (Phase 1.0~1.3)
  - 24시간 상주 프로세스

#### Workflow Controller

- **역할:** 내부 파이프라인 제어
- **입력:** task JSON (status 변화 감지)
- **출력:** agent 기동, git branch/commit/PR (git 활성화 시)
- **상세:**
  - task 1개에 대해 파이프라인 전체를 책임
  - Planner → subtask loop → integration test → commit/PR 순서 제어
  - 매 agent 호출 전 counters vs limits 비교
  - testing 설정에 따라 불필요한 agent skip
  - git.enabled=false면 branch 생성, commit, PR 등 모든 git 작업을 건너뜀
  - 한도 초과 시 즉시 중단 및 에스컬레이션

### 3.2 Planning Layer

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

### 3.3 Worker Layer (subtask당 순차 실행)

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
  - testing이 전부 disabled면 skip 가능
  - 기동 실패 시 에러 로그와 함께 Coder로 루프백

#### Unit Test Agent

- **위치:** 실행장비
- **역할:** 코드 레벨 테스트
- **입력:** 기동된 환경 + testing.unit_test 설정
- **출력:** 테스트 결과 (pass/fail, 실패 상세)
- **상세:**
  - testing.unit_test.enabled가 false면 skip
  - 지정된 suite만 실행 (task override 또는 config default)
  - 실패 시 Coder로 루프백

#### E2E Test Agent

- **위치:** 테스트장비 (Windows host)
- **역할:** 브라우저 기반 통합 테스트
- **입력:** handoff JSON (테스트 대상 URL, 시나리오, 참조 이미지)
- **출력:** 테스트 결과 + 스크린샷
- **상세:**
  - testing.e2e_test.enabled가 false면 skip
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

---

## 4. Workflow

### 4.1 전체 사이클

```
 1. 사용자: CLI submit (또는 메신저)
    → Task Manager: task.json + attachments 생성

 2. Workflow Controller
    → 새 task 감지 → git branch 생성 (feature/TASK-{id})
    → Planner Agent 기동

 3. Planner Agent
    → 코드베이스 + 첨부 이미지 분석
    → plan 생성 (subtask 배열)
    → [review_plan=true면] Plan 승인 대기

 4. Workflow Controller
    → plan의 subtask를 순차 실행

    ┌── Subtask Loop ──────────────────────────────────────────┐
    │                                                           │
    │  4a. Coder Agent: 코드 작성                                │
    │      → changes_made 기록                                  │
    │                                                           │
    │  4b. Review Agent: 코드 리뷰                               │
    │      → 거절 시 Coder로 루프백                               │
    │                                                           │
    │  4c. Setup Agent: 환경 구성 및 기동                         │
    │      → [testing 전부 disabled면 skip]                      │
    │      → 기동 실패 시 Coder로 루프백                          │
    │                                                           │
    │  4d. Unit Test Agent                                      │
    │      → [unit_test.enabled=false면 skip]                   │
    │      → 지정 suite만 실행                                   │
    │      → 실패 시 Coder로 루프백                               │
    │                                                           │
    │  4e. E2E Test Agent (테스트장비)                            │
    │      → [e2e_test.enabled=false면 skip]                    │
    │      → 실패 시 Coder로 루프백                               │
    │                                                           │
    │  4f. Reporter Agent: 결과 종합                             │
    │      → 통과: subtask 커밋 → 다음 subtask                   │
    │      → 실패 (retry 이내): Coder로 루프백                    │
    │      → 실패 (retry 초과): re-plan 요청                     │
    │      → 실패 (re-plan 초과): 에스컬레이션                    │
    │                                                           │
    └───────────────────────────────────────────────────────────┘

 5. Integration Test (모든 subtask 완료 후)
    → [integration_test.enabled=false면 skip]
    → 지정 suite + E2E (include_e2e=true면) 실행
    → 실패 시 에스컬레이션

 6. Workflow Controller
    → [review_before_merge=true면] 머지 승인 대기
    → PR 생성 or 직접 머지

 7. Task Manager
    → 완료 노티 (CLI stdout 또는 메신저)
```

### 4.2 Subtask 간 컨텍스트 전달

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

### 4.3 Re-plan

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

### 4.4 루프백 규칙

| 실패 지점 | 대상 | 전달 내용 |
|-----------|------|-----------|
| Review 거절 | Coder | 리뷰 피드백 |
| Setup 실패 | Coder | 빌드/기동 에러 로그 |
| Unit Test 실패 | Coder | 실패 테스트명 + 에러 메시지 |
| E2E Test 실패 | Coder | 실패 시나리오 + 스크린샷 경로 |
| Reporter: 재시도 | Coder | 종합 피드백 |
| Reporter: re-plan | Planner | 실패 사유 + 전체 히스토리 |
| Reporter: 포기 | Task Manager | 에스컬레이션 (사람에게 알림) |

### 4.5 테스트 비활성화 시 축소 경로

```
전부 disabled:  Coder → Reviewer → 커밋
unit만 enabled: Coder → Reviewer → Setup → Unit Test → Reporter → 커밋
e2e만 enabled:  Coder → Reviewer → Setup → E2E Test → Reporter → 커밋
전부 enabled:   Coder → Reviewer → Setup → Unit Test → E2E Test → Reporter → 커밋
```

---

## 5. Communication Structure

### 5.1 실행장비 내부 (같은 머신)

로컬 파일 + `.ready` sentinel 방식.

쓰기 패턴 (atomic write):
```bash
# 1. tmp 파일에 쓰기
echo '{"task_id": "TASK-042", ...}' > tasks/TASK-042.json.tmp
# 2. rename (atomic)
mv tasks/TASK-042.json.tmp tasks/TASK-042.json
# 3. sentinel 생성 (읽기 트리거)
touch tasks/TASK-042.ready
```

Workflow Controller가 `.ready` 파일 생성을 `inotifywait`로 감지하고 다음 agent 기동:
```bash
inotifywait -m -e create tasks/ --include '\.ready$' |
while read dir event file; do
  task_id="${file%.ready}"
  # 다음 agent 기동 로직
done
```

### 5.2 실행장비 → 테스트장비 (E2E 테스트 요청)

실행장비에서 테스트장비로 직접 push 불가.
테스트장비가 실행장비를 SSH로 감시:

```powershell
# 테스트장비 (Windows host) PowerShell
while ($true) {
    ssh server2 "inotifywait -e create agent-hub/handoffs/ --include '-e2e\.ready$' -q" |
    ForEach-Object {
        $file = $_.Trim().Split(" ")[-1]
        $jsonFile = $file -replace '\.ready$', '.json'

        # handoff 파일 가져오기
        scp server2:agent-hub/handoffs/$jsonFile ./current_handoff.json

        # E2E Test Agent 실행 (Windows host에서 브라우저 제어)
        claude -p "$(Get-Content e2e-agent-prompt.md -Raw) $(Get-Content current_handoff.json -Raw)"
    }
    # SSH 끊김 시 재연결
    Start-Sleep 5
}
```

### 5.3 테스트장비 → 실행장비 (E2E 결과 전송)

SCP로 결과 업로드:
```powershell
scp ./e2e-result.json server2:agent-hub/handoffs/TASK-042-e2e-result.json
scp -r ./screenshots/ server2:agent-hub/logs/TASK-042/screenshots/
ssh server2 "touch agent-hub/handoffs/TASK-042-e2e-result.ready"
```

### 5.4 통신 요약

| 경로 | 방식 | 트리거 |
|------|------|--------|
| 실행장비 내 agent 간 | 로컬 파일 + .ready | inotifywait (로컬) |
| 실행장비 → 테스트장비 | .ready를 테스트장비가 감시 | inotifywait (SSH 원격) |
| 테스트장비 → 실행장비 | SCP + SSH touch .ready | 직접 실행 |

---

## 6. Data Structures

### 6.1 Task (tasks/TASK-042.json)

```json
{
  "task_id": "TASK-042",
  "title": "로그인 기능 구현",
  "description": "첨부된 UI 목업대로 로그인 페이지 구현. OAuth 포함",
  "submitted_via": "cli",
  "submitted_at": "2026-04-01T09:00:00Z",
  "status": "in_progress",
  "branch": "feature/TASK-042",

  "attachments": [
    {
      "filename": "ui_mockup.png",
      "path": "attachments/TASK-042/ui_mockup.png",
      "type": "ui_design",
      "description": "로그인 페이지 UI 목업"
    },
    {
      "filename": "architecture_diagram.jpg",
      "path": "attachments/TASK-042/architecture_diagram.jpg",
      "type": "architecture",
      "description": "전체 인증 아키텍처"
    }
  ],

  "testing": {
    "unit_test": {
      "enabled": true,
      "suites": ["model", "api", "service"]
    },
    "e2e_test": { "enabled": true },
    "integration_test": { "enabled": true }
  },

  "human_review_policy": {
    "review_plan": true,
    "review_replan": true,
    "review_before_merge": false,
    "auto_approve_timeout_hours": 24
  },

  "plan_version": 1,
  "current_subtask": "TASK-042-2",
  "completed_subtasks": ["TASK-042-1"],

  "counters": {
    "total_agent_invocations": 12,
    "replan_count": 0,
    "current_subtask_retry": 1
  },

  "limits": {
    "max_subtask_count": 5,
    "max_retry_per_subtask": 3,
    "max_replan_count": 2,
    "max_total_agent_invocations": 30,
    "max_task_duration_hours": 4
  },

  "config_override": {},

  "human_interaction": null,

  "mid_task_feedback": [],

  "escalation_reason": null
}
```

**status 값:** `queued` → `planned` → `waiting_for_human` → `in_progress` → `completed` / `needs_replan` / `escalated` / `failed` / `cancelled`

**attachment type 값:** `ui_design` | `architecture` | `data_structure` | `reference`

### 6.2 Plan (tasks/TASK-042-plan.json)

```json
{
  "task_id": "TASK-042",
  "plan_version": 1,
  "created_at": "2026-04-01T09:05:00Z",
  "strategy_note": "백엔드 우선 구현, 각 API마다 E2E 검증용 최소 UI 포함. 이후 프론트엔드 subtask에서 동일 파일을 확장",

  "subtasks": [
    {
      "subtask_id": "TASK-042-1",
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
      "subtask_id": "TASK-042-2",
      "title": "로그인 API + E2E 검증용 최소 UI",
      "primary_responsibility": "인증 API",
      "description": "POST /auth/login 엔드포인트 구현. E2E 검증을 위해 최소 로그인 폼 포함",
      "guidance": [
        "API 구현이 핵심",
        "E2E 통과를 위해 로그인 폼 최소 구현 필요",
        "프론트엔드 스타일링은 하지 않음"
      ],
      "depends_on": ["TASK-042-1"],
      "require_e2e": true,
      "acceptance_criteria": "POST /auth/login 정상 응답, 잘못된 인증 시 401, 브라우저에서 로그인 가능",
      "reference_attachments": ["ui_mockup.png", "architecture_diagram.jpg"]
    },
    {
      "subtask_id": "TASK-042-3",
      "title": "로그인 프론트엔드 완성",
      "primary_responsibility": "프론트엔드 UX",
      "description": "이전 subtask에서 최소 구현된 login/index.tsx를 확장",
      "guidance": [
        "TASK-042-2에서 만든 login/index.tsx를 확장",
        "API 연동 구조는 유지, UI/UX 보강",
        "유효성 검사, 에러 표시, 로딩 상태 추가"
      ],
      "depends_on": ["TASK-042-2"],
      "require_e2e": true,
      "acceptance_criteria": "전체 로그인 UX 완성, 에러 핸들링, E2E 전체 시나리오 통과",
      "reference_attachments": ["ui_mockup.png"]
    }
  ]
}
```

### 6.3 Subtask State (tasks/TASK-042-1.json)

```json
{
  "subtask_id": "TASK-042-1",
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
      "session_log": "logs/TASK-042/coder_subtask-1_attempt-1.log"
    },
    {
      "agent": "reviewer",
      "action": "approved",
      "timestamp": "2026-04-01T09:12:00Z",
      "summary": "컨벤션 준수, 구조 적절",
      "session_log": "logs/TASK-042/reviewer_subtask-1.log"
    },
    {
      "agent": "unit_test",
      "action": "passed",
      "timestamp": "2026-04-01T09:15:00Z",
      "summary": "3/3 테스트 통과 (suite: model)",
      "session_log": "logs/TASK-042/unit_test_subtask-1.log"
    },
    {
      "agent": "reporter",
      "action": "subtask_complete",
      "timestamp": "2026-04-01T09:16:00Z",
      "summary": "subtask 완료, 커밋 대상",
      "session_log": "logs/TASK-042/reporter_subtask-1.log"
    }
  ]
}
```

### 6.4 E2E Handoff (handoffs/TASK-042-2-e2e.json)

```json
{
  "task_id": "TASK-042",
  "subtask_id": "TASK-042-2",
  "test_target_url": "http://192.168.1.100:3000",
  "reference_images": [
    {
      "path": "attachments/TASK-042/ui_mockup.png",
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

### 6.5 E2E Result (handoffs/TASK-042-2-e2e-result.json)

```json
{
  "task_id": "TASK-042",
  "subtask_id": "TASK-042-2",
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
      "screenshot": "screenshots/TASK-042-2_wrong_password_fail.png"
    }
  ]
}
```

### 6.6 Human Interaction (task JSON 내부)

```json
{
  "human_interaction": {
    "type": "plan_review",
    "message": "plan을 생성했습니다. 검토 후 승인/수정해주세요.",
    "payload_path": "tasks/TASK-042-plan.json",
    "options": ["approve", "modify", "cancel"],
    "requested_at": "2026-04-01T09:05:00Z",
    "timeout_hours": 24,
    "response": {
      "action": "modify",
      "message": "소셜 로그인도 포함해줘. 구글, 카카오",
      "attachments": ["attachments/TASK-042/social_login_flow.png"],
      "responded_at": "2026-04-01T09:30:00Z"
    }
  }
}
```

**type 값:** `plan_review` | `replan_review` | `merge_review` | `escalation`

---

## 7. Configuration

### 7.1 config.yaml (단일 설정 파일)

```yaml
# ─── 프로젝트 (공통) ───
project:
  name: "my-web-app"
  default_branch: "main"
  language: "typescript"
  framework: "next.js"

# ─── Git (공통) ───
# enabled: false면 branch/commit/PR 등 모든 git 작업을 건너뜀
# 로컬 전용 프로젝트나 테스트 시 유용
git:
  enabled: true
  remote: "origin"
  author_name: "agent-bot"
  author_email: "agent@example.com"
  auto_merge: false
  pr_target_branch: "develop"

# ─── 알림 (공통 — Phase 1.4에서 구체화) ───
notification:
  channel: "cli"      # Phase 1.0~1.3: "cli", Phase 1.4: "slack" | "telegram" 등

# ─── 사람 개입 정책 (공통, task별 override 가능) ───
human_review_policy:
  review_plan: true
  review_replan: true
  review_before_merge: false
  auto_approve_timeout_hours: 24

# ─── 안전 제한 (공통, task별 override 가능) ───
limits:
  max_subtask_count: 5
  max_retry_per_subtask: 3
  max_replan_count: 2
  max_total_agent_invocations: 30
  max_task_duration_hours: 4

# ─── 테스트 (공통, task별 override 가능) ───
testing:
  unit_test:
    enabled: true
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
      - name: "integration"
        command: "pytest tests/integration/"
        description: "서비스 간 연동 테스트"
    default_suites: ["model", "api", "service"]

  e2e_test:
    enabled: true
    tool: "playwright"

  integration_test:
    enabled: true
    suites: ["integration", "api"]
    include_e2e: true

# ─── Claude Code (공통) ───
claude:
  planner_model: "opus"
  coder_model: "sonnet"
  reviewer_model: "opus"
  e2e_tester_model: "sonnet"
  max_turns_per_session: 50

# ─── 로깅 (공통) ───
logging:
  level: "info"
  archive_completed_tasks: true
  keep_session_logs: true

# ═══════════════════════════════════════════════════════════
# 역할별 설정 — 기동 시 role argument에 따라 해당 섹션만 활성화
# ═══════════════════════════════════════════════════════════

executor:
  codebase_path: "/home/user/projects/my-web-app"
  workspace_dir: "/home/user/agent-hub/workspaces/my-web-app"
  git_credential_helper: "store"
  ssh_key: "~/.ssh/id_rsa"
  service_bind_address: "0.0.0.0"
  service_port: 3000
  sub_executor:
    enabled: false
    host: "192.168.1.200"
    user: "user"
    ssh_key: "~/.ssh/id_rsa_machine4"
    codebase_path: "/home/user/projects/my-web-app"

tester:
  executor_host: "192.168.1.100"
  executor_user: "user"
  executor_ssh_key: "~/.ssh/id_rsa_server"
  remote_workspace_dir: "/home/user/agent-hub/workspaces/my-web-app"
  test_base_url: "http://192.168.1.100:3000"
  local_work_dir: "C:\\Users\\user\\agent-hub\\work"
  browser: "chromium"
  viewport:
    width: 1280
    height: 720
  test_accounts:
    - email: "test@example.com"
      password: "password123"
      role: "user"
    - email: "admin@example.com"
      password: "admin123"
      role: "admin"
  ssh_reconnect_interval_seconds: 5
  watch_path: "handoffs/"
  watch_pattern: "-e2e\\.ready$"
```

### 7.2 Config 적용 우선순위

```
config.yaml 기본값 → task.json의 config_override → 실행 시점의 값
```

task에 `testing`, `limits`, `human_review_policy` 필드가 있으면 해당 값이 config 기본값을 덮어씀.
없는 필드는 config 기본값 사용.

---

## 8. Safety Limits

| 제한 | 기본값 | 대상 | 초과 시 |
|------|--------|------|---------|
| max_subtask_count | 5 | Plan 레벨 | Planner가 에스컬레이션 |
| max_retry_per_subtask | 3 | Subtask 레벨 | re-plan 요청 |
| max_replan_count | 2 | Task 레벨 | 에스컬레이션 |
| max_total_agent_invocations | 30 | Task 레벨 | 강제 중단 |
| max_task_duration_hours | 4 | Task 레벨 | 강제 중단 |

모든 제한값은 config.yaml에 기본값, task 요청 시 override 가능.

---

## 9. Directory Structure

### 9.1 agent-hub 레포 (git 관리)

```
agent-hub/
├── config.yaml                    # 단일 설정 파일
├── run_agent.sh                   # 기동/종료/상태 스크립트
├── .gitignore                     # workspaces/, .pids/, *.log, .env
├── .env.example                   # credential 템플릿
│
├── scripts/
│   ├── task_manager.py            # Task Manager 상주 프로세스
│   ├── workflow_controller.py     # Workflow Controller
│   ├── run_claude_agent.sh        # Claude Code 세션 기동 래퍼
│   └── e2e_watcher.sh             # 테스트장비용 감시 스크립트
│
└── config/
    └── agent_prompts/
        ├── planner.md             # Planner Agent 역할 프롬프트
        ├── coder.md               # Coder Agent 역할 프롬프트
        ├── reviewer.md            # Review Agent 역할 프롬프트
        ├── setup.md               # Setup Agent 역할 프롬프트
        ├── unit_tester.md         # Unit Test Agent 역할 프롬프트
        ├── e2e_tester.md          # E2E Test Agent 역할 프롬프트
        └── reporter.md            # Reporter Agent 역할 프롬프트
```

### 9.2 Runtime 데이터 (git 미관리)

```
workspaces/my-web-app/
├── tasks/
│   ├── TASK-042.json              # task 상태
│   ├── TASK-042.ready             # sentinel
│   ├── TASK-042-plan.json         # plan
│   ├── TASK-042-1.json            # subtask 상태
│   ├── TASK-042-2.json
│   └── ...
│
├── handoffs/
│   ├── TASK-042-2-e2e.json        # E2E 요청
│   ├── TASK-042-2-e2e.ready       # 테스트장비 트리거
│   ├── TASK-042-2-e2e-result.json # E2E 결과
│   └── TASK-042-2-e2e-result.ready
│
├── attachments/
│   └── TASK-042/
│       ├── ui_mockup.png
│       └── architecture_diagram.jpg
│
├── logs/
│   └── TASK-042/
│       ├── coder_subtask-1_attempt-1.log
│       ├── reviewer_subtask-1.log
│       ├── unit_test_subtask-1.log
│       └── screenshots/
│           └── TASK-042-2_wrong_password_fail.png
│
└── archive/                       # 완료된 task 아카이브
    └── TASK-041/
        └── ...
```

---

## 10. Startup & CLI

### 10.1 기동 명령

```bash
# 실행장비 (장비2)
./run_agent.sh start executor --config config.yaml

# 테스트장비 (장비1/3/5)
./run_agent.sh start tester --config config.yaml

# 상태 확인 (어느 장비든)
./run_agent.sh status

# 종료 (어느 장비든)
./run_agent.sh stop
```

### 10.2 Task 제출 (Phase 1.0~1.3)

```bash
# 대화형
./run_agent.sh submit --config config.yaml

# JSON 파일로
./run_agent.sh submit --config config.yaml --file my_task.json

# 인라인
./run_agent.sh submit --config config.yaml \
  --title "로그인 기능 구현" \
  --description "OAuth 포함" \
  --attach ui_mockup.png \
  --attach architecture.png

# 테스트 설정 override
./run_agent.sh submit --config config.yaml \
  --title "README 오타 수정" \
  --test none

./run_agent.sh submit --config config.yaml \
  --title "User 모델 수정" \
  --test-unit model,service \
  --test-skip e2e
```

### 10.3 Human Interaction (Phase 1.0~1.3)

```bash
# 대기 중인 interaction 확인
./run_agent.sh pending --config config.yaml

# Plan 승인
./run_agent.sh approve TASK-042 --config config.yaml

# Plan 수정 요청
./run_agent.sh reject TASK-042 \
  --message "소셜 로그인도 추가해줘" \
  --attach social_login_flow.png \
  --config config.yaml

# 진행 중 task에 피드백
./run_agent.sh feedback TASK-042 \
  --message "remember me 체크박스 추가해줘" \
  --config config.yaml

# Task 일시정지 / 재개 / 취소
./run_agent.sh pause TASK-042 --config config.yaml
./run_agent.sh resume TASK-042 --config config.yaml
./run_agent.sh cancel TASK-042 --config config.yaml

# Task 목록
./run_agent.sh list --config config.yaml
```

### 10.4 Config 배포

agent-hub 레포를 양쪽 장비에서 `git pull`로 동기화.
credential은 `.env`에 분리하여 `.gitignore` 처리.

```bash
# 테스트장비에서 최신 코드 + config 가져오기
cd ~/agent-hub && git pull
./run_agent.sh start tester --config config.yaml
```

---

## 11. Claude Code Session Management

### 세션 생성 규칙

- 각 agent는 subtask 단위로 새 세션 생성
- 세션 간 컨텍스트 공유는 JSON 파일로만 수행
- CLAUDE.md는 정적 프로젝트 지식만 (컨벤션, 아키텍처 설명 등)

### Agent 기동 래퍼

```bash
# run_claude_agent.sh
AGENT_TYPE=$1  # planner | coder | reviewer | setup | unit_tester | e2e_tester | reporter
TASK_FILE=$2
SUBTASK_FILE=$3
CONFIG=$4

MODEL=$(yq ".claude.${AGENT_TYPE}_model // .claude.coder_model" $CONFIG)
CODEBASE=$(yq '.executor.codebase_path' $CONFIG)
PROMPT_FILE="config/agent_prompts/${AGENT_TYPE}.md"

# 첨부 이미지 참조 지시 생성
ATTACHMENT_INSTRUCTIONS=""
for attachment in $(jq -r '.attachments[]?.path // empty' $TASK_FILE 2>/dev/null); do
  ATTACHMENT_INSTRUCTIONS+="\n- ${attachment} 파일을 view 명령으로 확인하세요"
done

cd $CODEBASE
claude --model $MODEL -p "$(cat $PROMPT_FILE)

## 첨부 자료
${ATTACHMENT_INSTRUCTIONS}

## Task Context
$(cat $TASK_FILE)

## Subtask
$(cat $SUBTASK_FILE)

## Plan
$(cat $(dirname $TASK_FILE)/$(jq -r '.task_id' $TASK_FILE)-plan.json 2>/dev/null || echo /dev/null)
" --output-format json
```

---

## 12. Phase Plan

### Phase 1.0 — 수동 단일 agent 실행

- task JSON을 손으로 작성해서 `tasks/`에 넣기
- `run_agent.sh run <agent_type> --task TASK-001` 으로 agent 하나를 직접 실행
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

- 테스트장비에서 `run_agent.sh start tester` 기동
- SSH + inotifywait 감시, SCP 결과 전송
- E2E Test Agent 브라우저 테스트
- **목표:** 크로스 머신 handoff 안정성, SSH 끊김 복구 검증

### Phase 1.4 — 메신저 연동

- 이 시점에서 메신저 플랫폼 결정 (Slack/Telegram/Discord 등)
- Task Manager가 메시지 수신 → task JSON 생성
- 진행 상황 업데이트, plan 승인 버튼, 에스컬레이션 알림
- 이미지 첨부 자동 다운로드
- CLI submit은 백업으로 유지
- **목표:** end-to-end가 메신저만으로 완결되는지 검증

### Phase 2 — 웹 모니터링 대시보드

- Task 목록/상세/Timeline 뷰
- 첨부 자료 뷰 (입력 이미지 + E2E 스크린샷 비교)
- 장비2에서 웹서버 기동 (FastAPI 등)
- Phase 1에서 쌓인 JSON + logs를 read-only로 렌더링
- agent 시스템 자체의 첫 실전 task로 개발 가능
