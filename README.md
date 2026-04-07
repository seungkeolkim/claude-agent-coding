# Agent Hub

Claude Code CLI 기반 자동 개발 시스템.

사용자가 작업을 요청하면, 복수의 agent가 순차적으로 **설계 → 코드 작성 → 리뷰 → 테스트 → 요약 → PR 생성**을 수행하고 결과를 알린다.
복잡한 작업은 Planner가 subtask로 분할하여 순차 실행하며, 각 subtask는 독립적으로 커밋 가능한 단위이다.

## 파이프라인

```
Planner → [Plan 승인 대기] → 브랜치 생성 → Subtask Loop → Summarizer → PR 생성 → [auto_merge]
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
├── templates/
│   ├── config.yaml.template       # 시스템 설정 템플릿
│   └── project.yaml.template      # 프로젝트 설정 템플릿
├── create_config.sh               # 초기 설정 스크립트
├── run_agent.sh                   # CLI 진입점 (task 관리 + agent 실행)
├── run_system.sh                  # 시스템 관리 (start/stop/status)
├── activate_venv.sh               # Python venv 활성화
├── CLAUDE.md                      # Claude Code 정적 지식
│
├── scripts/
│   ├── task_manager.py            # Task Manager 상주 프로세스
│   ├── workflow_controller.py     # Workflow Controller (파이프라인 실행 엔진)
│   ├── cli.py                     # CLI 프론트엔드 (argparse → hub_api)
│   ├── run_claude_agent.sh        # Claude Code 세션 기동 래퍼
│   ├── check_safety_limits.py     # Safety limits 체크
│   ├── chatbot.py                 # Chatbot 대화형 인터페이스
│   ├── notification.py             # 알림 시스템 (emit/get/format)
│   ├── usage_checker.py           # Usage 사용량 조회 (PTY 기반)
│   ├── init_project.py            # 대화형 프로젝트 초기화
│   ├── e2e_watcher.sh             # 테스트장비용 감시 스크립트
│   │
│   ├── hub_api/                   # 공통 인터페이스 라이브러리
│   │   ├── __init__.py
│   │   ├── core.py                # HubAPI 클래스 (submit, approve, reject 등)
│   │   ├── models.py              # 데이터 모델
│   │   └── protocol.py            # Protocol layer (Request/Response, dispatch)
│   │
│   └── web/                       # Web Monitoring Console (Phase 2.0)
│       ├── server.py              # FastAPI 웹 서버
│       ├── db.py                  # SQLite DB 레이어
│       ├── syncer.py              # 파일→DB sync 엔진
│       ├── static/                # JS/CSS
│       └── templates/             # Jinja2 템플릿
│
├── config/
│   └── agent_prompts/             # 8개 agent 역할 프롬프트
│
├── projects/                      # 프로젝트별 디렉토리 (전체 gitignored)
│   └── {name}/
│       ├── project.yaml           # 프로젝트 정적 설정
│       ├── project_state.json     # 동적 상태
│       ├── tasks/                 # task/subtask/plan JSON + .ready sentinel
│       ├── commands/              # TM → WFC 명령 전달
│       ├── handoffs/              # E2E 테스트 요청/결과
│       ├── attachments/           # 첨부파일
│       ├── logs/                  # 세션 로그
│       └── archive/               # 완료된 task 아카이브
│
├── session_history/               # Chatbot 세션 이력 (gitignored)
│   └── chatbot/
│
├── docs/                          # 사용자용 문서
│   └── configuration-reference.md
│
├── tests/                         # Unit/Integration/E2E 테스트 스위트
│   ├── conftest.py                # 공통 fixture (임시 프로젝트 자동 생성/정리)
│   ├── test_notification.py       # Unit: 알림 시스템
│   ├── test_safety_limits.py      # Unit: Safety limits
│   ├── test_task_utils.py         # Unit: Task 유틸리티
│   ├── test_usage_checker.py      # Unit: Usage check
│   ├── test_wfc_pipeline.py       # Integration: WFC pipeline 제어 흐름
│   ├── test_hub_api.py            # Integration: HubAPI
│   ├── test_e2e_agent_shell.py    # E2E: run_agent.sh subprocess
│   └── test_e2e_tm_lifecycle.py   # E2E: TM full lifecycle
│
├── docs_for_claude/               # Claude 세션용 내부 문서
│   ├── 004-agent-system-spec-v5.md    # 전체 아키텍처 명세 (현행)
│   ├── 010-handoff-phase-2.0-web-console.md
│   └── 005-design-history-archive.md
│
└── docs_history/                  # 이전 버전 아카이브
    ├── 003-agent-system-spec-v2.md
    └── 003-agent-system-spec-v3.md
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

### 3. Task Manager 시작

```bash
# 백그라운드로 TM + Web Console 시작
./run_system.sh start

# 더미 모드 (Claude 호출 없이 파이프라인 흐름만 검증)
./run_system.sh start --dummy

# Web Console: http://localhost:9880
```

### 4. Task 제출 및 관리

```bash
# task 제출 → TM이 자동으로 WFC spawn → 파이프라인 실행
./run_agent.sh submit --project my-app --title "로그인 기능 구현" --description "OAuth 포함"

# 첨부파일과 함께 제출
./run_agent.sh submit --project my-app --title "UI 수정" --attach mockup.png

# task 목록 확인
./run_agent.sh list [--project my-app] [--status in_progress]

# 대기 중인 승인 요청 확인 (plan 검토 등)
./run_agent.sh pending

# plan 승인 / 거부
./run_agent.sh approve 00001 --project my-app
./run_agent.sh reject 00001 --project my-app --message "subtask 2번 범위 축소 필요"

# 실행 중 task에 피드백
./run_agent.sh feedback 00001 --project my-app --message "remember me 체크박스도 추가해줘"

# 프로젝트 설정 동적 변경
./run_agent.sh config --project my-app --set "testing.unit_test.enabled=true"

# task 제어
./run_agent.sh pause --project my-app [00001]
./run_agent.sh resume --project my-app [00001]
./run_agent.sh cancel 00001 --project my-app
```

### 5. Chatbot (자연어 대화형 인터페이스)

```bash
# 대화형 모드 시작
./run_agent.sh chat

# 확인 모드 지정
./run_agent.sh chat --confirmation-mode always_confirm

# 이전 세션 재개
./run_agent.sh chat --session 20260403_143052_a3f1

# 저장된 세션 목록
./run_agent.sh chat --list-sessions
```

자연어로 시스템을 제어합니다:
- "my-app에 로그인 기능 만들어줘" → task 제출
- "현재 상태 알려줘" → 시스템 상태 조회
- "00001번 승인해" → plan 승인

### 6. 시스템 상태 확인 및 종료

```bash
# 시스템 상태 (TM + 프로젝트별 상태)
./run_system.sh status

# TM 종료 (실행 중 WFC는 완료 대기)
./run_system.sh stop

# 즉시 강제 종료
./run_system.sh stop --force
```

### 7. 디버깅용 수동 실행

```bash
# 개별 agent 실행
./run_agent.sh run coder --project my-app --task 00001 --subtask 00001-1

# 전체 파이프라인 수동 실행
./run_agent.sh pipeline --project my-app --task 00001 [--dummy]

# dry-run: Claude 호출 없이 조합된 프롬프트만 확인
./run_agent.sh run planner --project my-app --task 00001 --dry-run
```

### 8. 결과 확인

- 로그: `projects/{name}/logs/{task_id}/` 아래에 agent별 로그 파일 생성
- PR: `auto_merge=true`면 자동 머지, `false`면 PR만 생성 (status=`waiting_for_human_pr_approve`)
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
| 시스템 | `config.yaml` | 장비 정보, Claude 모델, 안전 제한, task 큐 기본값 |
| 프로젝트 정적 | `project.yaml` | 코드베이스 경로, git, 테스트, human review 설정 |
| 프로젝트 동적 | `project_state.json` | 런타임 override (`config` 명령으로 변경) |
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

## 핵심 설계 원칙

- **멀티 프로젝트:** 하나의 agent-hub 인스턴스가 여러 프로젝트를 동시 관리
- **파일 기반 통신:** JSON 파일 + `.ready` sentinel, 내부 port/소켓 없음
- **human review:** Planner/Replan 결과 승인 대기, auto_approve timeout 지원
- **task 큐 블로킹:** 이전 task 미완료 시 다음 task 자동 차단 (설정으로 비활성화 가능)
- **책임 범위 기반 subtask:** 파일 격리가 아닌 primary_responsibility로 분할
- **테스트 선택적 bypass:** 비활성화된 agent는 pipeline에 아예 포함하지 않음
- **Usage 기반 제어:** 5시간 세션 사용량 threshold로 과사용 방지

## 테스트

```bash
./run_test.sh all          # 전체 (213개)
./run_test.sh unit         # Unit 테스트만
./run_test.sh integration  # Integration 테스트만
./run_test.sh e2e          # E2E 테스트만
./run_test.sh help         # 도움말
```

## Phase 로드맵

| Phase | 목표 | 상태 |
|-------|------|------|
| **1.0** | 수동 pipeline 실행 + git 자동화 | **완료** |
| **TM** | Task Manager + CLI + hub_api + human review + 큐 블로킹 | **완료** |
| **1.4** | 운영 안정화: 알림, Usage check, 재알림, 테스트 스위트 | **완료** |
| **1.5** | Chatbot 대화형 인터페이스 + Protocol + 세션 관리 | **완료** |
| **1.6** | Chatbot 사용성: create_project, resubmit, get_plan (177개 테스트) | **완료** |
| **2.0** | Web Monitoring Console + SQLite 하이브리드 (213개 테스트) | **진행 중** |
| 2.1 | 고급 기능: Pipeline resume, user_preferences 등 | 예정 |
| 2.2 | Messenger (Slack/Telegram) | 예정 |
| 2.3 | E2E 테스트장비 연동 | 예정 |

## 상세 명세

- 전체 아키텍처: [`docs_for_claude/004-agent-system-spec-v5.md`](docs_for_claude/004-agent-system-spec-v5.md)
- 설정 레퍼런스: [`docs/configuration-reference.md`](docs/configuration-reference.md)
- 설계 히스토리: [`docs_for_claude/005-design-history-archive.md`](docs_for_claude/005-design-history-archive.md)
