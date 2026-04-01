# Agent Hub

Claude Code 기반 자동 개발 시스템.

## 프로젝트 구조
- `config.yaml` — 시스템 전체 설정 (단일 진입점)
- `scripts/` — Task Manager, Workflow Controller, agent 기동 래퍼
- `config/agent_prompts/` — agent별 역할 프롬프트
- `docs/agent-system-spec.md` — 전체 아키텍처 명세

## 코딩 컨벤션
- 변수/함수/파일명: 축약 금지, 이름만 보고 알 수 있게
- 함수별 docstring 필수
- 주석은 한국어로 충분히
- 가독성 최우선 (예쁨보다 명확함)

## 경로 규칙
- config.yaml의 모든 경로는 **절대경로**로 작성 (agent가 cd로 codebase로 이동하기 때문)
- 대상 프로젝트(codebase) 내부의 상대경로는 건드리지 않음 (cwd가 codebase이므로 정상 동작)

## 핵심 규칙
- CLAUDE.md는 정적 지식만. 동적 상태는 tasks/*.json으로 관리
- agent 간 통신은 .ready sentinel 파일 기반
- 각 agent는 subtask 단위로 새 세션 생성

## 상세 명세가 필요하면
`docs/agent-system-spec.md`를 읽으세요.