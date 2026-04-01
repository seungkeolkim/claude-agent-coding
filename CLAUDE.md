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
- `docs/003-agent-system-spec-v2.md` — 전체 아키텍처 명세

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

## Phase 1.0 사용법
```bash
# 1. 시스템 설정 생성
./create_config_and_env.sh

# 2. 프로젝트 초기화
./run_agent.sh init-project

# 3. task JSON 수동 작성 → projects/{name}/tasks/TASK-001.json

# 4. agent 수동 실행
./run_agent.sh run coder --project my-app --task TASK-001
./run_agent.sh run coder --project my-app --task TASK-001 --dry-run  # 프롬프트만 확인
```

## 상세 명세가 필요하면
- `docs/000-agent-system-spec.md`는 deprecate 되었습니다. 읽을 필요가 없습니다.
- 대신 상세 명세를 위해 `docs/003-agent-system-spec-v2.md`를 읽으세요.
