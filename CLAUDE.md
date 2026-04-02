# Agent Hub

Claude Code 기반 자동 개발 시스템.

## 프로젝트 구조
- `config.yaml` — 시스템 전체 설정 (단일 진입점, gitignored)
- `config.yaml.template` — 시스템 설정 템플릿
- `project.yaml.template` — 프로젝트 설정 템플릿
- `projects/{name}/project.yaml` — 프로젝트별 정적 설정 (git 관리)
- `projects/{name}/project_state.json` — 프로젝트별 동적 상태 (gitignored)
- `scripts/` — init_project, agent 기동 래퍼
- `config/agent_prompts/` — agent별 역할 프롬프트
- `docs/` — 사용자용 문서 (설정 레퍼런스 등)
- `docs_for_claude/003-agent-system-spec-v3.md` — 전체 아키텍처 명세

## 코딩 컨벤션
- 변수/함수/파일명: 축약 금지, 이름만 보고 알 수 있게
- 함수별 docstring 필수
- 주석은 한국어로 충분히
- 가독성 최우선 (예쁨보다 명확함)

## 경로 규칙
- config.yaml의 모든 경로는 **절대경로**로 작성 (agent가 cd로 codebase로 이동하기 때문)
- project.yaml의 codebase.path도 **절대경로** 필수
- 대상 프로젝트(codebase) 내부의 상대경로는 건드리지 않음 (cwd가 codebase이므로 정상 동작)

## 핵심 규칙
- CLAUDE.md는 정적 지식만. 동적 상태는 project_state.json, tasks/*.json으로 관리
- agent 간 통신은 .ready sentinel 파일 기반
- 각 agent는 subtask 단위로 새 세션 생성

## 사용법
```bash
# 시스템 설정 생성
./create_config.sh

# 프로젝트 초기화
./run_agent.sh init-project

# Task Manager 시작 (백그라운드)
./run_system.sh start [--dummy]

# task 제출 → TM이 자동으로 WFC spawn
./run_agent.sh submit --project my-app --title "기능 구현"

# task 조회 / 승인 / 거부
./run_agent.sh list [--project my-app]
./run_agent.sh pending
./run_agent.sh approve 00001 --project my-app
./run_agent.sh reject 00001 --project my-app --message "사유"

# 시스템 상태 / 종료
./run_system.sh status
./run_system.sh stop

# agent 수동 실행 (디버깅용)
./run_agent.sh run coder --project my-app --task 00001 [--dry-run] [--dummy]
```

## 상세 명세가 필요하면
- v1, v2 스펙은 `docs_history/`에 아카이브되었습니다. 읽을 필요가 없습니다.
- 상세 명세를 위해 `docs_for_claude/003-agent-system-spec-v3.md`를 읽으세요.
