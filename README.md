# Agent Hub

Claude Code CLI 기반 자동 개발 시스템.

사용자가 작업을 요청하면, 복수의 agent가 순차적으로 **설계 → 코드 작성 → 리뷰 → 테스트**를 수행하고 결과를 알린다.
복잡한 작업은 Planner가 subtask로 분할하여 순차 실행하며, 각 subtask는 독립적으로 커밋 가능한 단위이다.

## 아키텍처 개요

```
사용자 (CLI)
  │
  ▼
Task Manager ─── 작업 큐 관리, 알림
  │
  ▼
Workflow Controller ─── 파이프라인 제어, git branch/commit/PR
  │
  ▼
Planner Agent ─── 코드베이스 분석, subtask 분할
  │
  ▼
┌── Subtask Loop (순차 실행) ──────────────────┐
│                                               │
│  Coder Agent      코드 작성                    │
│       ↓                                       │
│  Review Agent     코드 리뷰 (거절 시 루프백)    │
│       ↓                                       │
│  Setup Agent      환경 구성 및 기동             │
│       ↓                                       │
│  Unit Test Agent  코드 레벨 테스트              │
│       ↓                                       │
│  E2E Test Agent   브라우저 통합 테스트          │
│       ↓                                       │
│  Reporter Agent   결과 종합 및 판정             │
│                                               │
└───────────────────────────────────────────────┘
  │
  ▼
Integration Test → PR 생성 → 완료 알림
```

## 디렉토리 구조

```
agent-hub/
├── config.yaml.template           # 설정 템플릿 (git 관리)
├── .env.template                  # 환경 변수 템플릿 (git 관리)
├── create_config_and_env.sh       # 초기 설정 스크립트
├── run_agent.sh                   # 기동/종료/상태 스크립트
├── CLAUDE.md                      # Claude Code 정적 지식
│
├── scripts/
│   ├── task_manager.py            # Task Manager 상주 프로세스
│   ├── workflow_controller.py     # Workflow Controller
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
└── docs/
    └── agent-system-spec.md       # 전체 아키텍처 명세
```

### Runtime 데이터 (git 미관리)

```
workspaces/{project}/
├── tasks/          # task/subtask/plan JSON + .ready sentinel
├── handoffs/       # E2E 테스트 요청/결과
├── attachments/    # 첨부 파일 (이미지 등)
└── logs/           # 세션 로그, 스크린샷
```

## 시작하기

### 사전 요구사항

- Python 3.11+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (구독 기반 — Pro/Max/Team/Enterprise)
- `inotify-tools` (실행장비, `apt install inotify-tools`)

### 설치

```bash
git clone <repository-url>
cd agent-hub

# 의존성 설치
pip install -r requirements.txt

# 초기 설정 (config.yaml + .env 생성, 에디터로 편집)
./create_config_and_env.sh
```

### 실행

```bash
# config.yaml / .env가 없으면 먼저 초기 설정 필요
./create_config_and_env.sh

# 실행장비에서 시작
./run_agent.sh start executor

# 테스트장비에서 시작 (E2E 감시)
./run_agent.sh start tester

# 상태 확인
./run_agent.sh status

# 종료
./run_agent.sh stop
```

## 장비 구성

| 구분 | 역할 | 가동 |
|------|------|------|
| 실행장비 (메인) | Agent Hub, 코드베이스 기동 | 24h |
| 실행장비 (서브) | 보조 코드베이스, 보조 agent | 24h |
| 테스트장비 | GUI 브라우저 E2E 테스트 | 업무 시간 / 수시 |

## 핵심 설계 원칙

- **단일 config:** `config.yaml` 하나로 전체 시스템 세팅. task별 override 가능.
- **책임 범위 기반 subtask:** 파일 격리가 아닌 primary_responsibility로 분할. scope 겹침 허용.
- **CLAUDE.md는 정적 지식만:** 동적 상태는 전부 `tasks/*.json`으로 관리.
- **안전 제한:** subtask 수, retry 횟수, re-plan 횟수, 총 agent 호출 수 등 모든 자원에 한도 설정.
- **sentinel 기반 통신:** `.ready` 파일 + `inotifywait`로 agent 간 비동기 통신.

## 상세 명세

전체 아키텍처 명세는 [`docs/agent-system-spec.md`](docs/agent-system-spec.md)를 참고하세요.
