# Phase 1.4 핸드오프 — 운영 안정화

> 작성: 2026-04-03
> 기준 문서: `docs_for_claude/003-agent-system-spec-v3.md`

## Phase 순서 결정

Phase 1.3(E2E 테스트장비 연동)과 1.4(운영 안정화)는 상호 의존성이 없다.
1.4를 먼저 진행하기로 결정. 이유: E2E 없이도 알림, resume, usage check는 실 사용에서 바로 체감되는 기능.
1.3은 1.4 완료 후 또는 필요 시점에 착수.

---

## 현재 상태 요약

- **Phase 1.0 완료:** 수동 pipeline + git 자동화 (실제 task 6개 검증 완료)
- **TM Phase 완료:** Task Manager, CLI (10개 서브커맨드), hub_api, human review (plan/replan 승인 대기), task 큐 블로킹, 4계층 config merge
- **브랜치:** `feature/task-manager-run-entire-system` (마지막 커밋: `22a86c6`)
- **test-project:** 단위변환기 웹서비스, task 00001~00100 + 테스트용 00201~00206 (정리 완료)

---

## Phase 1.4 구현 항목

### 1. 알림 시스템 (CLI 기반)

**현재:** WFC가 로그만 남기고, 사용자가 `./run_agent.sh pending`이나 `./run_system.sh status`를 직접 실행해야 상태를 알 수 있음.

**목표:** task 완료/실패/PR생성/human review 요청 시 사용자에게 능동적으로 알림.

**구현 범위:**
- CLI 알림: TM이 project_state.json 변화를 감지하여 stdout/파일에 알림 출력
- 알림 이벤트 종류:
  - `task_completed` — task 완료 (PR URL 포함)
  - `task_failed` — task 실패 (에러 요약)
  - `pr_created` — PR 생성됨 (auto_merge=false일 때)
  - `plan_review_requested` — plan 승인 대기
  - `replan_review_requested` — replan 승인 대기
  - `escalation` — 에스컬레이션 발생
- config.yaml의 `notification.channel: "cli"` 기반 (향후 slack/telegram 확장점)
- 알림 히스토리 저장 (projects/{name}/notifications.json 또는 logs/ 하위)

**변경 파일:**
- `scripts/task_manager.py` — 알림 감지 + 발송 로직
- `scripts/workflow_controller.py` — 이벤트 발생 시점에 알림 트리거 (project_state.json에 알림 정보 기록)
- `scripts/hub_api/core.py` — 알림 조회 API (선택적)
- `config.yaml.template` — notification 섹션 확장

### 2. Pipeline Resume (실패 지점 자동 재개)

**현재:** `run_pipeline_from_subtasks()` 함수가 있지만 TM에서 자동 트리거 안 됨. 실패 시 수동 재실행 필요.

**목표:** 실패한 task를 사용자 명령 한 줄로 이어서 실행.

**구현 범위:**
- CLI: `./run_agent.sh resume-task <task_id> --project <name>` 또는 기존 `resume` 명령 확장
- WFC가 task JSON의 `completed_subtasks`를 읽고, 마지막 실패 subtask부터 재개
- task status `failed` → `in_progress`로 변경 + .ready 재생성
- TM이 감지하여 WFC spawn

**변경 파일:**
- `scripts/hub_api/core.py` — resume_task() 메서드 추가
- `scripts/cli.py` — resume-task 서브커맨드 (또는 기존 resume 확장)
- `scripts/workflow_controller.py` — resume 모드 진입 시 completed subtask 건너뛰기
- `run_agent.sh` — 라우팅 추가

### 3. Usage Check (세션 사용량 기반 실행 제어)

**현재:** config.yaml에 `usage_thresholds`와 `usage_check_interval_seconds` 설정은 있으나, 실제 사용량을 체크하는 로직이 없음.

**목표:** Claude Code CLI의 사용량을 확인하여 threshold 초과 시 대기.

**선행 조사 필요:**
- `claude` CLI에서 사용량을 프로그래밍으로 가져올 수 있는지 확인 (`/usage` 출력 파싱 등)
- 불가능하면 세션 시작 시각 기반 추정 등 우회 방법 검토
- 확인 결과에 따라 구현 범위가 달라짐

**구현 범위 (가져올 수 있는 경우):**
- `scripts/usage_checker.py` — 사용량 조회 + threshold 비교
- WFC: 매 agent 호출 전 usage check 호출 → 초과 시 대기 루프
- TM: 새 task spawn 전 usage check

**변경 파일:**
- `scripts/usage_checker.py` (신규)
- `scripts/workflow_controller.py` — agent 호출 전 체크 삽입
- `scripts/task_manager.py` — spawn 전 체크 삽입

### 4. 재알림 (Re-notification)

**현재:** waiting_for_human 상태에서 auto_approve_timeout까지 한 번만 요청하고 끝.

**목표:** 특정 시간 후 응답 없으면 재알림 전송.

**구현 범위:**
- config에 `re_notification_interval_hours` 추가 (기본: 4시간)
- `wait_for_human_response()` 폴링 루프 내에서 interval마다 재알림 이벤트 발생
- 알림 시스템(#1)과 연동

**변경 파일:**
- `scripts/workflow_controller.py` — wait_for_human_response() 내 재알림 로직
- `config.yaml.template` — re_notification_interval_hours 추가

---

## 작업 진행 상황

1. **알림 시스템** — **완료** (notification.py, WFC/TM/CLI 연동)
2. **Pipeline resume** — **보류** (정책 미결정: resume vs 취소+재등록. 정책 확정 후 구현)
3. **Usage check** — 선행 조사 필요, 결과에 따라 스코프 조정
4. **재알림** — **완료** (wait_for_human_response에 re_notification_interval_hours 추가)

---

## 참고: 기존 코드 진입점

| 파일 | 관련 함수/위치 | 용도 |
|------|---------------|------|
| `scripts/task_manager.py` | `run()` 메인 루프 (line ~550) | TM 폴링, WFC spawn |
| `scripts/task_manager.py` | `check_workflow_controller()` (line ~277) | WFC 완료 감지 → 여기서 알림 트리거 |
| `scripts/workflow_controller.py` | `run_pipeline()` (line ~660) | 파이프라인 메인 |
| `scripts/workflow_controller.py` | `wait_for_human_response()` (line ~304) | 승인 대기 폴링 → 재알림 삽입 지점 |
| `scripts/workflow_controller.py` | `run_pipeline_from_subtasks()` | resume용 기존 함수 (TM 연동 필요) |
| `scripts/hub_api/core.py` | HubAPI 클래스 | 모든 CLI 액션의 백엔드 |
| `config.yaml.template` | notification 섹션 | 알림 설정 |
