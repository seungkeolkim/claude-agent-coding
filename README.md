# Agent Hub

Claude Code CLI 기반 자동 개발 시스템.

사용자가 작업을 요청하면, 복수의 agent가 순차적으로 **설계 → 코드 작성 → 리뷰 → 테스트 → 요약 → PR 생성**을 수행하고 결과를 알린다.
복잡한 작업은 Planner가 subtask로 분할하여 순차 실행하며, 각 subtask는 독립적으로 커밋 가능한 단위이다.

## 파이프라인

```
Planner → 브랜치 생성 → Subtask Loop → Summarizer → PR 생성 → [auto_merge]
                            │
                    ┌───────┴───────┐
                    │  Coder        │
                    │  → Reviewer   │
                    │  → [루프백]    │  ← retry (max_retry_per_subtask)
                    │  → Reporter   │
                    │  → [replan]   │  ← replan (max_replan_count)
                    └───────────────┘
```

| 단계 | Agent | 역할 |
|------|-------|------|
| 계획 | Planner | 코드베이스 분석, subtask 분할, 브랜치명 제안 |
| 코딩 | Coder | subtask 코드 작성 |
| 리뷰 | Reviewer | 코드 리뷰 (거절 시 Coder 루프백) |
| 테스트 | Setup / Unit Tester / E2E Tester | 환경 구성, 테스트 실행 (비활성화 시 bypass) |
| 보고 | Reporter | 결과 종합, pass/fail/replan/escalate 판정 |
| 요약 | Summarizer | 전체 변경사항 분석, PR 제목/본문 + task 요약 생성 |

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
│   ├── workflow_controller.py     # Workflow Controller (파이프라인 실행 엔진)
│   ├── run_claude_agent.sh        # Claude Code 세션 기동 래퍼
│   ├── check_safety_limits.py     # Safety limits 체크
│   ├── init_project.py            # 대화형 프로젝트 초기화
│   └── e2e_watcher.sh             # 테스트장비용 감시 스크립트
│
├── config/
│   └── agent_prompts/             # 8개 agent 역할 프롬프트
│       ├── planner.md
│       ├── coder.md
│       ├── reviewer.md
│       ├── setup.md
│       ├── unit_tester.md
│       ├── e2e_tester.md
│       ├── reporter.md
│       └── summarizer.md
│
├── projects/                      # 프로젝트별 디렉토리 (전체 gitignored)
│   └── {name}/
│       ├── project.yaml           # 프로젝트 정적 설정
│       ├── project_state.json     # 동적 상태
│       ├── tasks/                 # task/subtask/plan JSON
│       ├── handoffs/              # E2E 테스트 요청/결과
│       ├── logs/                  # 세션 로그
│       └── archive/               # 완료된 task 아카이브
│
├── docs/                          # 사용자용 문서
│   └── configuration-reference.md # 설정 레퍼런스 (전체 항목 표)
│
└── docs_for_claude/               # Claude 세션용 내부 문서
    ├── 003-agent-system-spec-v2.md    # 전체 아키텍처 명세
    └── 005-design-history-archive.md  # 설계 히스토리 아카이브
```

## 사전 요구사항

- Python 3.11+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (구독 기반 — Pro/Max/Team/Enterprise)
- [GitHub CLI (`gh`)](https://cli.github.com/) — PR 생성/머지 자동화
- PyYAML (`pip install pyyaml`)

## 빠른 시작

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
  "status": "submitted",
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

### 4. 실행

```bash
# 전체 파이프라인 실행 (Planner → Subtask Loop → Summarizer → PR)
./run_agent.sh pipeline --project my-app --task 00001

# 더미 모드 (Claude 호출 없이 파이프라인 흐름만 검증)
./run_agent.sh pipeline --project my-app --task 00001 --dummy

# 개별 agent 실행
./run_agent.sh run coder --project my-app --task 00001 --subtask 00001-1

# dry-run: Claude 호출 없이 조합된 프롬프트만 확인
./run_agent.sh run planner --project my-app --task 00001 --dry-run

# 도움말
./run_agent.sh help
```

### 5. 결과 확인

- 로그: `projects/{name}/logs/{task_id}/` 아래에 agent별 로그 파일 생성
- PR: `auto_merge=true`면 자동 머지, `false`면 PR만 생성 (status=`pending_review`)
- task 요약: task JSON의 `summary` 필드에 Summarizer가 생성한 한국어 요약

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

전체 설정 항목 상세는 [`docs/configuration-reference.md`](docs/configuration-reference.md)를 참고하세요.

## Git 자동화

| 기능 | 설명 |
|------|------|
| 브랜치 생성 | Planner가 영문 이름 제안, `feature/{task_id}-{설명}` 형식 |
| subtask 커밋 | 각 subtask 완료 시 자동 commit + push |
| PR 생성 | Summarizer가 한국어 title/body 생성, `[{task_id}]` 접두사 자동 |
| 자동 머지 | `auto_merge: true`면 `gh pr merge`, `false`면 PR만 생성 |
| 인증 | project.yaml의 `auth_token`으로 `gh` CLI 자동 로그인 |

git provider는 `github`, `bitbucket`, `gitlab` 설정 가능 (현재 github만 구현).

## 핵심 설계 원칙

- **멀티 프로젝트:** 하나의 agent-hub 인스턴스가 여러 프로젝트를 동시 관리
- **파일 기반 통신:** JSON 파일 + `.ready` sentinel, 내부 port/소켓 없음
- **책임 범위 기반 subtask:** 파일 격리가 아닌 primary_responsibility로 분할
- **테스트 선택적 bypass:** 비활성화된 agent는 pipeline에 아예 포함하지 않음
- **Usage 기반 제어:** 5시간 세션 사용량 threshold로 과사용 방지
- **CLAUDE.md는 정적 지식만:** 동적 상태는 JSON 파일로 관리

## Phase 로드맵

| Phase | 목표 | 상태 |
|-------|------|------|
| **1.0** | 수동 pipeline 실행 + git 자동화 | **완료** |
| **TM** | Task Manager (CLI 인터페이스, WFC 연동, human review) | **다음** |
| 1.3 | 테스트장비 연동 (E2E) | 예정 |
| 1.4 | 메신저 연동 | 예정 |
| 2.0 | 웹 모니터링 대시보드 | 예정 |

## 상세 명세

- 전체 아키텍처: [`docs_for_claude/003-agent-system-spec-v2.md`](docs_for_claude/003-agent-system-spec-v2.md)
- 설정 레퍼런스: [`docs/configuration-reference.md`](docs/configuration-reference.md)
- 설계 히스토리: [`docs_for_claude/005-design-history-archive.md`](docs_for_claude/005-design-history-archive.md)
