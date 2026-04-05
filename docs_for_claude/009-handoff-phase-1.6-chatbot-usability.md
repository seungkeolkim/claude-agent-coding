# Phase 1.6 핸드오프 — Chatbot 사용성 개선

> 작성: 2026-04-06
> 기준 문서: `docs_for_claude/003-agent-system-spec-v4.md`
> 브랜치: `feature/phase-1.6-chatbot-usability`

---

## 현재 상태 요약

- **Phase 1.0 완료:** 수동 pipeline + git 자동화
- **TM Phase 완료:** Task Manager, CLI (11개 서브커맨드), hub_api, human review, task 큐 블로킹, 4계층 config merge
- **Phase 1.4 완료:** 알림 시스템, Usage check (3계층 threshold), 재알림, 테스트 스위트 (85개)
- **Phase 1.5 완료:** Chatbot 레이어, Protocol, 세션 관리 (140개 테스트)
- **Phase 1.6 진행중:** Chatbot 사용성 개선 (177개 테스트)

---

## Phase 1.6 구현 완료 항목

### 1. create_project API (Chatbot/Protocol 경유 프로젝트 생성)

기존에는 `./run_agent.sh init-project` (대화형 stdin 위저드)으로만 프로젝트를 생성할 수 있었음.
이제 hub_api, protocol dispatch, chatbot에서 프로그래밍 방식으로 프로젝트 생성 가능.

**변경 파일:**
- `scripts/hub_api/models.py`: `CreateProjectResult` dataclass 추가
- `scripts/hub_api/core.py`: `HubAPI.create_project()` — 이름 검증, 중복 검사, 디렉토리 생성, YAML/state 초기화
- `scripts/hub_api/protocol.py`: `create_project` action 등록, `ErrorCode.PROJECT_ALREADY_EXISTS` 추가
- `scripts/hub_api/__init__.py`: `CreateProjectResult` export
- `scripts/chatbot.py`: `HIGH_RISK_ACTIONS`에 `create_project` 추가
- `scripts/init_project.py`: `UNCONFIGURED_PLACEHOLDER` 상수 추가, `main()`을 `HubAPI.create_project()` 사용으로 리팩터

**`__UNCONFIGURED__` 플레이스홀더:**
- `git_settings` 미지정 시 `author_name`, `author_email`에 `__UNCONFIGURED__` 값이 채워짐
- project.yaml 헤더에 안내 주석 포함
- 프로젝트 실행 전 반드시 사용자가 실제 값으로 교체해야 함

### 2. resubmit action (cancelled/failed task 재제출)

cancelled/failed task에 resume을 요청해도 성공으로 응답하던 버그 수정.
cancelled/failed task를 새 task로 재제출하는 `resubmit` action 추가.

**변경 파일:**
- `scripts/hub_api/core.py`: `resubmit()` — 원본 task의 title/description/config_override를 복사하여 새 task 생성
- `scripts/hub_api/core.py`: `_validate_task_is_active()` — 종료 상태 task에 resume/pause 차단 + 안내 메시지
- `scripts/hub_api/protocol.py`: `resubmit` action 등록
- `scripts/chatbot.py`: `HIGH_RISK_ACTIONS`에 `resubmit` 추가

### 3. get_plan action (plan 내용 조회)

plan_review 대기 시 plan.json의 내용을 볼 수 없었던 문제 해결.
chatbot에서 "플랜 보여줘"로 subtask 목록, 전략, guidance를 상세 확인 가능.

**변경 파일:**
- `scripts/hub_api/core.py`: `get_plan()` — tasks/{id}/plan.json 읽기
- `scripts/hub_api/protocol.py`: `get_plan` action 등록
- `scripts/chatbot.py`: `READ_ONLY_ACTIONS`에 `get_plan` 추가, 결과 포맷터 추가 (branch, 전략, subtask 상세)

### 4. Chatbot 시스템 프롬프트 개선

Claude가 "재실행/다시 실행" 요청을 resume으로 잘못 매핑하던 문제 해결.

**변경:**
- 시스템 프롬프트에 action 선택 가이드 테이블 추가 (resume vs resubmit 구분)
- resubmit action 설명에 "재실행, 다시 돌려줘" 등의 한국어 표현 명시

### 5. WFC gh 인증 fallback

project.yaml의 `git.auth_token`이 비어있을 때 config.yaml의 `machines.executor.github_token`을 자동 사용.

**변경 파일:**
- `scripts/workflow_controller.py`: 파이프라인 시작 + PR 생성 두 지점에 fallback 적용

### 6. setup_environment.sh (시스템 환경 초기화 스크립트)

기존 `activate_venv.sh`는 Python venv만 처리. 실제 시스템 기동에 필요한 전체 환경을 검증/설치하는 통합 스크립트 추가.

**파일:** `setup_environment.sh` (신규)

4단계 검증:
1. 시스템 필수 도구: python3, python3-venv, git, gh (GitHub CLI), claude (Claude Code CLI)
2. Python 환경: .venv 생성, PyYAML, pytest 설치
3. 설정 파일: config.yaml, templates, agent_prompts (8개)
4. 디렉토리 구조: projects/, session_history/, logs/, .pids/

`--check` 모드로 설치 없이 상태만 확인 가능.

### 7. 테스트 정리

- 테스트 프로젝트 이름 형식: `test-{label}-YYMMDD-HHmmss` (테스트임을 명시 + 타임스탬프)
- session-scope cleanup fixture: 테스트 종료 후 잔여 test 프로젝트 자동 삭제
- 정규식으로 테스트 프로젝트만 매칭 (사용자 프로젝트 보존)

---

## 테스트

- **총 177개** (기존 140 → 160 → 172 → 177)
- 신규 37개: create_project (20), resubmit (9), resume 검증 (4), get_plan (5) 등 (일부 기존 테스트 파일에도 import 변경)
- `./run_test.sh all`

---

## ACTION_REGISTRY 현황 (17개)

| action | 설명 | requires_project |
|--------|------|:---:|
| submit | 새 task 제출 | O |
| get_task | 단건 task 조회 | O |
| get_plan | task plan(subtask 목록) 조회 | O |
| list | task 목록 조회 | X |
| pending | 승인 대기 항목 조회 | X |
| approve | plan/replan 승인 | O |
| reject | plan/replan 거부 | O |
| feedback | 실행 중 task에 피드백 | O |
| resubmit | cancelled/failed task 재제출 | O |
| config | 프로젝트 설정 동적 변경 | O |
| pause | 프로젝트/task 일시정지 | O |
| resume | 프로젝트/task 재개 | O |
| cancel | task 취소 | O |
| status | 시스템 전체 상태 조회 | X |
| notifications | 알림 조회 | X |
| mark_notification_read | 알림 읽음 처리 | O |
| create_project | 새 프로젝트 생성 | X |

---

## 해결한 버그

| 버그 | 원인 | 수정 |
|------|------|------|
| cancelled task에 resume 성공 응답 | `_send_command`가 상태 검증 없이 .command 파일 생성 | `_validate_task_is_active()` 추가, 종료 상태면 ValueError |
| Chatbot이 "재실행"을 resume으로 매핑 | 시스템 프롬프트에 resume/resubmit 구분 없음 | action 선택 가이드 테이블 추가 |
| plan 내용을 볼 수 없음 | get_plan action 없음 | get_plan 추가 + 포맷터 |
| project.yaml에 auth_token 없으면 gh 인증 실패 | config.yaml fallback 없음 | ensure_gh_auth에서 github_token fallback |
| E2E 테스트 후 프로젝트 잔재 | TM/WFC가 추가 파일 생성 → teardown race condition | session-scope cleanup fixture |

---

## 아직 수행하지 않은 작업 (TODO)

### 이번 Phase 1.6 범위 내 잔여

| 항목 | 설명 | 우선순위 |
|------|------|----------|
| Chatbot 질문 통합 | create_project 시 필수 파라미터를 한 번에 물어보기 (현재 하나씩 나뉨) | 낮음 (Claude 모델 스타일 의존) |
| create_project CLI 서브커맨드 | `./run_agent.sh create-project --name foo --codebase /path` 비대화형 CLI | 낮음 |
| resubmit 포맷팅 검증 | Chatbot에서 resubmit 결과 표시 실제 확인 | 낮음 |

### 다음 Phase 후보

| Phase | 내용 | 상태 |
|-------|------|------|
| 1 잔여 | E2E 테스트장비 연동 (e2e_watcher.sh, SSH) | 미착수 |
| 2.0 | scope 파라미터 + chatbot 실행 모델 개선 | 미착수 |
| 2.0 | GH_TOKEN 환경변수 방식 전환 (멀티유저 격리) | 미착수 |
| 2.1 | Web monitor & chat | 미착수 |
| 2.2 | Messenger (Slack/Telegram) | 미착수 |

---

## 코드 진입점

| 파일 | 용도 |
|------|------|
| `scripts/hub_api/core.py` | create_project, get_plan, resubmit, _validate_task_is_active 추가 |
| `scripts/hub_api/protocol.py` | 17개 action (create_project, get_plan, resubmit 추가) |
| `scripts/hub_api/models.py` | CreateProjectResult 추가 |
| `scripts/chatbot.py` | action 선택 가이드, get_plan 포맷터, resubmit 포맷터 |
| `scripts/init_project.py` | UNCONFIGURED_PLACEHOLDER, HubAPI.create_project() 경유 리팩터 |
| `scripts/workflow_controller.py` | gh auth_token fallback |
| `setup_environment.sh` | 시스템 환경 초기화 (신규) |
| `tests/conftest.py` | session-scope cleanup fixture |
| `tests/test_hub_api.py` | create_project/resubmit/get_plan/resume 검증 테스트 |
