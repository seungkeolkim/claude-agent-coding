# Agent Hub 설정 레퍼런스

Agent Hub의 모든 설정 항목을 정리한 문서입니다.

---

## 설정 계층 구조

설정은 4계층으로 나뉘며, **아래 계층이 위 계층의 값을 덮어씁니다.**

| 계층 | 파일 | 범위 | 설명 |
|------|------|------|------|
| 1 (시스템) | `config.yaml` | 전체 시스템 | 모든 프로젝트에 적용되는 기본값 |
| 2 (프로젝트 정적) | `projects/{name}/project.yaml` | 프로젝트 | 프로젝트 생성 시 작성. 거의 변경 없음 |
| 3 (프로젝트 동적) | `projects/{name}/project_state.json` | 프로젝트 | 런타임에 사용자 명령으로 변경 가능 |
| 4 (Task 일시) | task JSON의 `config_override` | 해당 task | 해당 task에만 적용. 완료 후 의미 없음 |

**예시:** config.yaml에서 `max_retry_per_subtask: 3` → project.yaml에서 미설정 → task의 `config_override`에서 `5`로 변경 → **최종 적용값: 5**

---

## 1. config.yaml (시스템 설정)

시스템 전체에 적용되는 설정. `create_config.sh`로 생성합니다.

### 장비 / 인프라 (`machines`)

| 키 | 기본값 | 설명 |
|----|--------|------|
| `executor.ssh_key` | `~/.ssh/id_rsa` | SSH 키 경로 |
| `executor.github_token` | `""` | GitHub API 토큰 (시스템 레벨) |
| `executor.git_credential_helper` | `"store"` | git credential helper 방식 |
| `executor.service_bind_address` | `"0.0.0.0"` | 서비스 바인드 주소 |
| `tester.browser` | `"chromium"` | E2E 테스트용 브라우저 |
| `tester.viewport.width` | `1280` | 브라우저 뷰포트 너비 |
| `tester.viewport.height` | `720` | 브라우저 뷰포트 높이 |
| `tester.ssh_reconnect_interval_seconds` | `5` | SSH 재연결 간격 |

### Claude 모델 (`claude`)

| 키 | 기본값 | 설명 |
|----|--------|------|
| `planner_model` | `"opus"` | Planner agent 모델 |
| `coder_model` | `"sonnet"` | Coder agent 모델 |
| `reviewer_model` | `"opus"` | Reviewer agent 모델 |
| `setup_model` | `"sonnet"` | Setup agent 모델 |
| `unit_tester_model` | `"sonnet"` | Unit Tester agent 모델 |
| `e2e_tester_model` | `"sonnet"` | E2E Tester agent 모델 |
| `reporter_model` | `"sonnet"` | Reporter agent 모델 |
| `max_turns_per_session` | `50` | agent 세션당 최대 turn 수 |

### Usage 기반 실행 제어 (`claude.usage_thresholds`)

5시간 세션 사용량이 threshold 이상이면 해당 레벨의 실행을 대기합니다.

| 키 | 기본값 | 설명 |
|----|--------|------|
| `new_task` | `0.70` | 새 task 시작 허용 기준 |
| `new_subtask` | `0.80` | 새 subtask 시작 허용 기준 |
| `new_agent_stage` | `0.90` | pipeline 내 다음 agent 호출 허용 기준 |
| `usage_check_interval_seconds` | `60` | threshold 초과 시 재확인 주기 (초) |

### 안전 제한 기본값 (`default_limits`)

| 키 | 기본값 | override 가능 계층 | 초과 시 동작 |
|----|--------|-------------------|-------------|
| `max_subtask_count` | `5` | project, task | Planner가 생성할 수 있는 subtask 수 제한 |
| `max_retry_per_subtask` | `3` | project, task | Coder 재시도 차단 → Reporter 판단 |
| `max_replan_count` | `2` | project, task | Planner 재실행 차단 → escalation |
| `max_total_agent_invocations` | `30` | project, task | 모든 agent 호출 차단 → escalation |
| `max_task_duration_hours` | `4` | project, task | 시간 초과 → escalation |

### 사람 개입 정책 기본값 (`default_human_review_policy`)

| 키 | 기본값 | override 가능 계층 | 설명 |
|----|--------|-------------------|------|
| `review_plan` | `true` | project, task | Planner 결과를 사람이 확인 후 진행 |
| `review_replan` | `true` | project, task | replan 발생 시 사람이 확인 후 진행 |
| `review_before_merge` | `false` | project, task | 머지 전 사람 확인 |
| `auto_approve_timeout_hours` | `24` | project, task | 사람 응답 없으면 자동 승인까지 대기 시간 |

### 로깅 (`logging`)

| 키 | 기본값 | 설명 |
|----|--------|------|
| `level` | `"info"` | 로그 레벨 |
| `archive_completed_tasks` | `true` | 완료된 task를 아카이브 |
| `keep_session_logs` | `true` | agent 세션 로그 보존 |

### 알림 (`notification`)

| 키 | 기본값 | override 가능 계층 | 설명 |
|----|--------|-------------------|------|
| `channel` | `"cli"` | project | `cli` / `slack` / `telegram` (Phase 1.4+) |

---

## 2. project.yaml (프로젝트 정적 설정)

프로젝트별 `projects/{name}/project.yaml`에 위치합니다. 템플릿: `templates/project.yaml.template`

### 프로젝트 기본 정보 (`project`)

| 키 | 필수 | 설명 |
|----|------|------|
| `name` | O | 프로젝트명 (디렉토리명과 일치) |
| `description` | O | 프로젝트 설명 (기술 스택, 목적 등) |
| `default_branch` | O | 기본 브랜치 (`main`, `develop` 등) |

### 코드베이스 (`codebase`)

| 키 | 필수 | 설명 |
|----|------|------|
| `path` | O | 코드베이스 절대경로 |
| `service_bind_address` | - | 서비스 바인드 주소 (기본: `0.0.0.0`) |
| `service_port` | - | 서비스 포트 (기본: `3000`) |

### Git (`git`)

| 키 | 기본값 | 설명 |
|----|--------|------|
| `enabled` | `true` | `false`면 모든 git 작업 건너뜀 |
| `provider` | `"github"` | `github` / `bitbucket` / `gitlab` (현재 github만 구현) |
| `remote` | `"origin"` | git remote 이름 |
| `author_name` | — | 커밋 작성자 이름 |
| `author_email` | — | 커밋 작성자 이메일 |
| `auto_merge` | `false` | `true`: PR 생성 후 자동 머지 / `false`: PR만 생성 |
| `pr_target_branch` | `"main"` | PR의 base branch |
| `auth_token` | `""` | provider 인증 토큰 (GitHub PAT 등) |

### 테스트 (`testing`)

#### 단위 테스트 (`testing.unit_test`)

| 키 | 기본값 | 설명 |
|----|--------|------|
| `enabled` | `false` | 단위 테스트 실행 여부 |
| `available_suites` | `[]` | 사용 가능한 테스트 스위트 목록 |
| `default_suites` | `[]` | 기본 실행 스위트 |

`available_suites` 항목 구조:

| 키 | 설명 |
|----|------|
| `name` | 스위트 이름 (예: `"model"`) |
| `command` | 실행 명령어 (예: `"pytest tests/models/"`) |
| `description` | 스위트 설명 |

#### E2E 테스트 (`testing.e2e_test`)

| 키 | 기본값 | 설명 |
|----|--------|------|
| `enabled` | `false` | E2E 테스트 실행 여부 |
| `tool` | `"playwright"` | E2E 테스트 도구 |
| `test_accounts` | `[]` | 테스트용 계정 목록 |

`test_accounts` 항목 구조:

| 키 | 설명 |
|----|------|
| `email` | 테스트 계정 이메일 |
| `password` | 테스트 계정 비밀번호 |
| `role` | 계정 역할 (`user`, `admin` 등) |

#### 통합 테스트 (`testing.integration_test`)

| 키 | 기본값 | 설명 |
|----|--------|------|
| `enabled` | `false` | 통합 테스트 실행 여부 |
| `suites` | `[]` | 실행할 스위트 목록 |
| `include_e2e` | `false` | E2E 테스트 포함 여부 (`e2e_test.enabled=false`면 무시) |

### 시스템 기본값 override (선택적)

project.yaml에서 아래 섹션을 추가하면 config.yaml의 기본값을 프로젝트 레벨에서 덮어씁니다.
**필요한 항목만 작성하면 됩니다.**

| 섹션 | config.yaml 대응 | 설명 |
|------|------------------|------|
| `limits` | `default_limits` | 안전 제한 (subtask 수, retry 횟수, replan 횟수 등) |
| `claude` | `claude` | 모델 선택, 세션 turn 수 |
| `human_review_policy` | `default_human_review_policy` | 사람 개입 정책 |
| `notification` | `notification` | 알림 채널 |

---

## 3. project_state.json (프로젝트 동적 설정)

런타임에 Task Manager가 관리하는 동적 상태. `projects/{name}/project_state.json`에 위치합니다.
사용자의 자연어 명령으로 변경됩니다 (예: "이제 unit test 포함해서 돌려줘").

| 키 | 설명 |
|----|------|
| `project_name` | 프로젝트명 |
| `status` | 프로젝트 상태 (`idle`, `running`, `paused`) |
| `current_task_id` | 현재 실행 중인 task ID |
| `last_activity_at` | 마지막 활동 시각 (ISO 8601) |
| `overrides` | project.yaml 값을 런타임에서 덮어쓸 설정 |
| `update_history` | override 변경 이력 |

`overrides`에서 덮어쓸 수 있는 항목:

| 섹션 | 예시 |
|------|------|
| `testing.unit_test.enabled` | 단위 테스트 on/off |
| `testing.e2e_test.enabled` | E2E 테스트 on/off |
| `limits.*` | 안전 제한값 변경 |
| `human_review_policy.*` | 사람 개입 정책 변경 |

---

## 4. task JSON의 config_override (Task 일시 설정)

개별 task JSON 내부의 `config_override` 필드. 해당 task에만 적용됩니다.

```json
{
  "config_override": {
    "testing": {
      "e2e_test": { "enabled": false }
    },
    "limits": {
      "max_retry_per_subtask": 5
    }
  }
}
```

override 가능한 항목:

| 섹션 | 키 | 설명 |
|------|-----|------|
| `testing` | `unit_test.enabled` | 이 task에서만 단위 테스트 on/off |
| `testing` | `e2e_test.enabled` | 이 task에서만 E2E 테스트 on/off |
| `limits` | `max_subtask_count` | 이 task의 subtask 수 제한 |
| `limits` | `max_retry_per_subtask` | 이 task의 subtask당 retry 제한 |
| `limits` | `max_replan_count` | 이 task의 replan 제한 |
| `limits` | `max_total_agent_invocations` | 이 task의 총 agent 호출 제한 |
| `limits` | `max_task_duration_hours` | 이 task의 최대 실행 시간 |

---

## 5. Task JSON 구조

`projects/{name}/tasks/00001-설명.json`에 위치합니다.

### 필드 목록

| 키 | 타입 | 필수 | 설명 |
|----|------|------|------|
| `task_id` | string | O | 5자리 ID (`"00001"`) |
| `project_name` | string | O | 프로젝트명 |
| `title` | string | O | task 제목 |
| `description` | string | O | task 상세 설명 |
| `submitted_via` | string | O | 제출 방식 (`manual`, `cli`, `web`) |
| `submitted_at` | string | O | 제출 시각 (ISO 8601) |
| `status` | string | - | 현재 상태 (아래 표 참조) |
| `branch` | string | - | 작업 브랜치명 (`feature/{task_id}-...`) |
| `attachments` | array | - | 첨부파일 목록 |
| `plan_version` | number | - | plan 버전 (replan마다 증가) |
| `current_subtask` | string | - | 현재 진행 중인 subtask ID |
| `completed_subtasks` | array | - | 완료된 subtask ID 목록 |
| `counters` | object | - | 실행 카운터 (아래 표 참조) |
| `config_override` | object | - | task 레벨 설정 override |
| `human_interaction` | object | - | 사람 개입 대기 상태 |
| `mid_task_feedback` | array | - | 실행 중 사용자 피드백 |
| `escalation_reason` | string | - | escalation 사유 |
| `summary` | string | - | 완료 후 작업 요약 (Summarizer 생성) |
| `pr_url` | string | - | 생성된 PR URL |

### status 값

| 값 | 설명 |
|----|------|
| `submitted` | 제출됨, 실행 대기 |
| `queued` | 큐에 등록됨 |
| `planned` | Planner 완료 |
| `waiting_for_human_plan_confirm` | 사람 확인 대기 |
| `in_progress` | 실행 중 |
| `waiting_for_human_pr_approve` | PR 생성됨, 리뷰 대기 (`auto_merge=false`) |
| `completed` | 완료 |
| `needs_replan` | replan 필요 |
| `escalated` | 사람에게 에스컬레이션 |
| `failed` | 실패 |
| `cancelled` | 취소 |

### counters 필드

| 키 | 설명 |
|----|------|
| `total_agent_invocations` | 전체 agent 호출 횟수 |
| `replan_count` | replan 횟수 |
| `current_subtask_retry` | 현재 subtask의 retry 횟수 |

### attachments 항목 구조

| 키 | 설명 |
|----|------|
| `filename` | 파일명 |
| `path` | 경로 (`attachments/{task_id}/파일명`) |
| `type` | `ui_design` / `architecture` / `data_structure` / `reference` |
| `description` | 파일 설명 |

---

## 6. 파이프라인 흐름

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

| 단계 | agent | step | 설명 |
|------|-------|------|------|
| 계획 | Planner | 01 | task를 subtask로 분해, 브랜치명 제안 |
| 코딩 | Coder | 03 | subtask 구현 |
| 리뷰 | Reviewer | 04 | 코드 리뷰, pass/fail 판단 |
| 보고 | Reporter | 07 | subtask 완료/retry/replan/escalate 판단 |
| 요약 | Summarizer | 08 | 전체 변경사항 요약, PR 제목/본문 생성 |
