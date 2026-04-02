# Agent Hub

Claude Code CLI 기반 자동 개발 시스템.

사용자가 작업을 요청하면, 복수의 agent가 순차적으로 **설계 → 코드 작성 → 리뷰 → 테스트**를 수행하고 결과를 알린다.
복잡한 작업은 Planner가 subtask로 분할하여 순차 실행하며, 각 subtask는 독립적으로 커밋 가능한 단위이다.

## 아키텍처 개요

```
사용자 (CLI / 추후 메신저)
  │
  ▼
Task Manager (1개, 상주) ─── 작업 접수, WFC 라이프사이클 관리, 알림
  │
  ├── Workflow Controller — project A (상주)
  │     │
  │     ▼
  │   Planner Agent ─── 코드베이스 분석, subtask 분할
  │     │
  │     ▼
  │   ┌── Subtask Loop (순차 실행) ──────────────────┐
  │   │                                               │
  │   │  Coder Agent      코드 작성                    │
  │   │       ↓                                       │
  │   │  Review Agent     코드 리뷰 (거절 시 루프백)    │
  │   │       ↓                                       │
  │   │  Setup Agent      환경 구성 및 기동             │
  │   │       ↓                                       │
  │   │  Unit Test Agent  코드 레벨 테스트              │
  │   │       ↓                                       │
  │   │  E2E Test Agent   브라우저 통합 테스트          │
  │   │       ↓                                       │
  │   │  Reporter Agent   결과 종합 및 판정             │
  │   │                                               │
  │   └───────────────────────────────────────────────┘
  │     │
  │     ▼
  │   Integration Test → PR 생성 → 완료 알림
  │
  └── Workflow Controller — project B (상주)
        └── ...
```

## 디렉토리 구조

```
claude-agent-coding/
├── config.yaml.template           # 시스템 설정 템플릿
├── project.yaml.template          # 프로젝트 설정 템플릿
├── create_config.sh               # 초기 설정 스크립트
├── run_agent.sh                   # CLI 진입점
├── activate_venv.sh               # Python venv 활성화
├── CLAUDE.md                      # Claude Code 정적 지식
│
├── scripts/
│   ├── init_project.py            # 대화형 프로젝트 초기화
│   ├── run_claude_agent.sh        # Claude Code 세션 기동 래퍼
│   └── e2e_watcher.sh             # 테스트장비용 감시 스크립트
│
├── config/
│   └── agent_prompts/
│       ├── planner.md             # Planner Agent 역할 프롬프트
│       ├── coder.md               # Coder Agent 역할 프롬프트
│       ├── reviewer.md            # Review Agent 역할 프롬프트
│       ├── setup.md               # Setup Agent 역할 프롬프트
│       ├── unit_tester.md         # Unit Test Agent 역할 프롬프트
│       ├── e2e_tester.md          # E2E Test Agent 역할 프롬프트
│       └── reporter.md            # Reporter Agent 역할 프롬프트
│
├── projects/                      # 프로젝트별 디렉토리
│   └── {name}/
│       ├── project.yaml           # 프로젝트 정적 설정 (git 관리)
│       ├── project_state.json     # 동적 상태 (gitignored)
│       ├── tasks/                 # task/subtask/plan JSON + .ready
│       ├── handoffs/              # E2E 테스트 요청/결과
│       ├── commands/              # TM → WFC 명령 전달
│       ├── attachments/           # 첨부 파일 (이미지 등)
│       ├── logs/                  # 세션 로그, 스크린샷
│       └── archive/               # 완료된 task 아카이브
│
└── docs/
    ├── 002-design-evolution-for-web-discussion.md  # v1→v2 변경 배경
    └── 003-agent-system-spec-v2.md                 # 전체 아키텍처 명세 ★
```

## 사전 요구사항

- Python 3.11+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (구독 기반 — Pro/Max/Team/Enterprise)
- `inotify-tools` (실행장비, `apt install inotify-tools`) — Phase 1.1+
- PyYAML (`pip install pyyaml`)

## 빠른 시작 (Phase 1.0)

현재 Phase 1.0에서는 agent를 하나씩 수동으로 실행하여 프롬프트와 JSON 입출력을 검증합니다.

### 1. 초기 설정

```bash
git clone <repository-url>
cd claude-agent-coding

# Python 가상환경 + 의존성 설치
source activate_venv.sh

# 시스템 설정 파일 생성
./create_config.sh
```

### 2. 프로젝트 초기화

```bash
./run_agent.sh init-project
```

대화형으로 프로젝트 이름, 설명, 코드베이스 경로, git 연동 여부를 입력하면
`projects/{name}/` 디렉토리와 `project.yaml`, `project_state.json`이 자동 생성됩니다.

### 3. Task JSON 작성

`projects/{name}/tasks/00001-login-feature.json` 형식으로 수동 작성합니다:

```json
{
  "task_id": "00001",
  "project_name": "my-app",
  "title": "로그인 기능 구현",
  "description": "OAuth 포함 로그인 페이지를 구현해주세요.",
  "submitted_via": "manual",
  "submitted_at": "2026-04-02T10:00:00Z",
  "status": "in_progress",
  "branch": null,
  "attachments": [],
  "plan_version": 0,
  "current_subtask": null,
  "completed_subtasks": [],
  "counters": {
    "total_agent_invocations": 0,
    "replan_count": 0,
    "current_subtask_retry": 0
  },
  "config_override": {},
  "human_interaction": null,
  "mid_task_feedback": [],
  "escalation_reason": null
}
```

### 4. Agent 실행

```bash
# dry-run: claude 호출 없이 조합된 프롬프트만 확인 (사용량 절약)
./run_agent.sh run planner --project my-app --task 00001 --dry-run

# 실제 실행
./run_agent.sh run planner --project my-app --task 00001

# subtask 지정 실행
./run_agent.sh run coder --project my-app --task 00001 --subtask 00001-1

# 도움말
./run_agent.sh help
```

### 5. 결과 확인

- 로그: `projects/{name}/logs/{task_id}/` 아래에 agent별 로그 파일 생성
- 프롬프트 검증: `--dry-run` 모드로 모델명, 실행 디렉토리, 프롬프트 내용 확인

## 설정 체계

4계층으로 구성되며, 뒤의 것이 앞의 것을 덮어씁니다:

```
config.yaml (시스템 기본값)
  → projects/{name}/project.yaml (프로젝트 정적)
    → projects/{name}/project_state.json (프로젝트 동적)
      → task.config_override (task 일시)
```

| 계층 | 파일 | 용도 |
|------|------|------|
| 시스템 | `config.yaml` | 장비 정보, Claude 모델, 안전 제한 기본값 |
| 프로젝트 정적 | `project.yaml` | 코드베이스 경로, git, 테스트 설정 |
| 프로젝트 동적 | `project_state.json` | 런타임 override (자연어로 변경 가능) |
| Task 일시 | `task.config_override` | 해당 task에만 적용 |

## 핵심 설계 원칙

- **멀티 프로젝트:** 하나의 agent-hub 인스턴스가 여러 프로젝트를 동시 관리
- **파일 기반 통신:** JSON 파일 + `.ready` sentinel, 내부 port/소켓 없음
- **책임 범위 기반 subtask:** 파일 격리가 아닌 primary_responsibility로 분할
- **테스트 선택적 bypass:** 비활성화된 agent는 pipeline에 아예 포함하지 않음
- **Usage 기반 제어:** 5시간 세션 사용량 threshold로 과사용 방지
- **CLAUDE.md는 정적 지식만:** 동적 상태는 JSON 파일로 관리

## Phase 계획

| Phase | 목표 | 상태 |
|-------|------|------|
| **1.0** | 수동 단일 agent 실행, 프롬프트/JSON 검증 | **현재** |
| 1.1 | 실행장비 내 파이프라인 자동화 (WFC + sentinel 감시) | 예정 |
| 1.2 | Planner + Subtask Loop + re-plan | 예정 |
| 1.3 | 테스트장비 연동 (E2E) | 예정 |
| 1.4 | 메신저 연동 | 예정 |
| 2 | 웹 모니터링 대시보드 | 예정 |

## 상세 명세

전체 아키텍처 명세는 [`docs/003-agent-system-spec-v2.md`](docs/003-agent-system-spec-v2.md)를 참고하세요.
