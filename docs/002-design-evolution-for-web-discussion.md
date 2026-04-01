# Agent Hub 설계 변경 논의 문서

> 작성: 2026-04-01
> 목적: Claude Web에서 아키텍처 재설계를 논의하기 위한 전체 컨텍스트 전달
> 기준 문서: `docs/000-agent-system-spec.md` (Session 01에서 작성)

이 문서는 Claude Code CLI에서 진행한 두 개 세션의 논의 내용을 빠짐없이 담고 있습니다.
`000-agent-system-spec.md`를 이미 알고 있다는 전제 하에, 그 이후 변경된 것과 새로 제기된 설계 이슈를 모두 기술합니다.

---

## Part 1: 완료된 변경 (이미 코드에 반영됨)

### 1.1 프로젝트 설정: language/framework → 자연어 description

**변경 이유:** 이 시스템의 agent는 LLM 기반이므로, `language: "typescript"`, `framework: "next.js"` 같은 discrete 필드로 제한할 필요가 없다. 또한 사용자의 실제 사용 목적이 웹 서비스뿐 아니라 데이터 스트림 파이프라인 sandbox framework 등 다양한 프로젝트에 걸쳐 있어서, 구조화된 선택지로는 표현이 불편하다.

**Before (spec 원본):**
```yaml
project:
  name: "my-web-app"
  default_branch: "main"
  language: "typescript"
  framework: "next.js"
```

**After (현재 template):**
```yaml
project:
  name: "my-project"
  default_branch: "main"
  description: |
    이 프로젝트가 무엇인지, 어떤 기술 스택을 사용하는지 자유롭게 기술하세요.
    예) Python 기반 데이터 파이프라인. Kafka, Flink 사용. 필요 시 Go도 가능하지만 강제는 아님.
    예) TypeScript + Next.js 웹 서비스. Tailwind CSS 사용. PostgreSQL + Prisma ORM.
```

**영향:** spec의 Section 7.1 config.yaml 예시에서 `language`/`framework` 필드가 사라지고 `description`으로 대체됨. agent 프롬프트에서 프로젝트 기술 스택을 참조할 때 이 description을 읽게 됨.

### 1.2 workspace_dir 선택적 설정

**변경 이유:** workspace 경로를 매번 절대경로로 수동 지정하는 것이 번거롭다. 규칙 기반 기본값이면 충분하다.

**Before:** `workspace_dir`이 필수 설정
**After:** 미설정 시 기본값 `{agent-hub 루트}/workspaces/{codebase_path의 디렉토리명}`으로 자동 결정

**영향:** spec의 Section 7.1, Section 9.2에서 workspace 경로 설명 변경 필요.

### 1.3 sub_executor 주석 처리

**변경 이유:** 보조 실행장비(장비4) 연동은 당분간 사용하지 않을 예정.

**변경:** config.yaml.template에서 sub_executor 섹션을 주석 처리. spec에서도 선택적 기능으로 격하.

### 1.4 integration_test.include_e2e 관계 명확화

**변경:** `include_e2e` 필드에 주석 추가 — "integration test 실행 시 e2e도 포함할지 여부. e2e_test.enabled=false면 이 값도 무시됨"

**배경:** integration_test 범위 안에 e2e가 포함된다는 관계가 직관적이지 않아서 혼동 발생. e2e_test.enabled가 false인데 include_e2e가 true면 어떻게 되는지 불명확했음.

### 1.5 auto_merge 기본값 변경

**변경:** `auto_merge: true` → `auto_merge: false` (config.yaml). 초기 테스트 단계에서 자동 머지는 위험하므로 기본 off.

---

## Part 2: Session 01에서 확립된 설계 결정 (spec에 이미 반영)

이 섹션은 spec 작성 과정에서 내린 설계 결정의 배경을 기록합니다. spec 문서에는 "what"만 있고 "why"가 부족한 부분들입니다.

### 2.1 CLI 구독 기반, API key 불필요

Claude Code CLI(`claude -p`)를 직접 사용하므로 API key가 필요 없다. Pro/Max/Team/Enterprise 구독으로 동작. 이는 비용 관리를 구독 단위로 단순화하고, API key 유출 위험을 제거한다.

### 2.2 템플릿 패턴

`config.yaml.template` → `create_config_and_env.sh` → `config.yaml` (gitignored). 민감 정보(경로, credential)가 git에 올라가는 것을 방지. `.env.template` → `.env`도 동일 패턴.

### 2.3 절대경로 필수 규칙

config.yaml의 모든 경로는 절대경로. 이유: agent가 `cd`로 codebase 디렉토리로 이동한 후 작업하므로, agent-hub 기준 상대경로가 깨진다. 대상 프로젝트(codebase) 내부의 상대경로는 cwd가 codebase이므로 정상 동작.

### 2.4 3개 디렉토리 분리

| 디렉토리 | 역할 | git 관리 |
|----------|------|----------|
| Agent Hub (`claude-agent-coding/`) | 시스템 코드, 스크립트, 프롬프트 | O |
| Codebase (`test-web-service/`) | agent가 작업하는 대상 프로젝트 | O (별도 repo) |
| Workspace (`workspaces/test-web-service/`) | runtime 데이터 (tasks, logs, handoffs) | X |

### 2.5 책임 범위 기반 subtask (파일 격리 아님)

초기에는 subtask별로 파일을 격리하는 방안을 고려했으나, 실제 개발에서 기능 단위 작업이 여러 파일에 걸치는 것이 자연스럽다고 판단. `primary_responsibility`로 분할하되 scope 겹침을 허용하고, `prior_changes`로 이전 subtask의 변경 맥락을 전달하는 방식으로 결정.

### 2.6 .claude 세션 관리

- VS Code extension은 미사용 (호스트 측에 .claude가 생기는 문제 회피)
- 각 agent는 subtask 단위로 새 세션 생성 (세션 공유 불필요)
- CLAUDE.md는 정적 지식만 담고, 동적 상태는 JSON 파일로 관리

### 2.7 git 선택적 지원

`git.enabled: false`로 설정하면 branch 생성, commit, PR 등 모든 git 작업을 건너뜀. 로컬 전용 프로젝트나 초기 테스트 시 유용.

### 2.8 테스트 비활성화 시 축소 경로

spec Section 4.5에 명시되어 있으나 중요하므로 재기술:
```
전부 disabled:  Coder → Reviewer → 커밋
unit만 enabled: Coder → Reviewer → Setup → Unit Test → Reporter → 커밋
e2e만 enabled:  Coder → Reviewer → Setup → E2E Test → Reporter → 커밋
전부 enabled:   Coder → Reviewer → Setup → Unit Test → E2E Test → Reporter → 커밋
```

### 2.9 Phase Plan 요약

| Phase | 내용 | 핵심 검증 포인트 |
|-------|------|-----------------|
| 1.0 | 수동 단일 agent 실행 | 각 agent의 프롬프트와 JSON 입출력 |
| 1.1 | 실행장비 내 파이프라인 자동화 | sentinel 기반 체인이 끊기지 않고 흐르는지 |
| 1.2 | Planner + Subtask Loop | subtask 분할 품질, 커밋 단위, 루프백/re-plan |
| 1.3 | 테스트장비 연동 (E2E) | 크로스 머신 handoff, SSH 끊김 복구 |
| 1.4 | 메신저 연동 | 메신저만으로 end-to-end 완결 |
| 2 | 웹 모니터링 대시보드 | JSON + logs read-only 렌더링 |

---

## Part 3: 미결 설계 이슈 (논의 필요)

### 이슈 1: 멀티 프로젝트 지원

**현재 문제:**
- config.yaml이 1개 → 시스템 전체가 단일 프로젝트에 종속
- task가 flat한 `tasks/` 디렉토리에 프로젝트 구분 없이 저장
- 여러 프로젝트가 동시에 agent를 돌릴 수 없음

**사용자 요구:**
- 여러 프로젝트(예: test-web-service, data-pipeline)가 동시에 agent 파이프라인을 돌릴 수 있어야 함
- 프로젝트별 task 큐, 상태, 설정이 분리되어야 함

**제안 방향:**
config를 시스템 레벨과 프로젝트 레벨로 분리.

```
config.yaml (시스템) — HW 정보, credential, Claude 모델 기본값, safety limits 기본값
  ↓
projects/{name}/project.yaml (프로젝트) — 프로젝트 description, git, testing, review policy
```

디렉토리 구조:
```
claude-agent-coding/
├── config.yaml                    # 시스템 설정만
├── scripts/
├── config/agent_prompts/
│
└── projects/                      # 프로젝트별 분리
    ├── test-web-service/
    │   ├── project.yaml           # 프로젝트 설정
    │   ├── project_state.json     # 동적 상태
    │   ├── tasks/
    │   ├── logs/
    │   └── handoffs/
    │
    └── data-pipeline/
        ├── project.yaml
        ├── project_state.json
        ├── tasks/
        ├── logs/
        └── handoffs/
```

**논의 포인트:**
- 기존 spec의 `workspaces/{project}/` 구조와 겹침. `workspaces/`를 `projects/`로 바꿀지, 아니면 config와 runtime 데이터를 분리할지
- Task Manager / Workflow Controller가 멀티 프로젝트를 어떻게 다중화할지 (프로젝트당 별도 프로세스? 단일 프로세스에서 멀티플렉싱?)

### 이슈 2: 테스트 설정의 동적 변경

**현재 문제:**
- testing 설정이 config.yaml에 정적으로 박혀 있음
- 프로젝트 진행 중 "이제 unit test 켜줘"라는 자연어 명령을 처리할 경로가 없음
- task별 `config_override`는 있지만, 프로젝트 레벨에서 영속적으로 바꾸는 메커니즘이 없음

**사용자 요구 시나리오:**
1. "test-web-service에서 이제부터 unit test 포함해서 돌려줘" → 프로젝트 레벨 영속 변경
2. "이번 task만 e2e 빼줘" → task 레벨 일시 변경
3. "unit test를 한 3개 task 동안만 끄고 다시 켜줘" → 시한부 변경

**제안: 3계층 설정 우선순위**
```
config.yaml (시스템 기본값)
  → projects/{name}/project_state.json (프로젝트 동적 상태, 자연어로 변경 가능)
    → tasks/TASK-{id}.json의 config_override (task 단위 일시 변경)
```

**project_state.json 예시:**
```json
{
  "project_name": "test-web-service",
  "testing": {
    "unit_test": { "enabled": false },
    "e2e_test": { "enabled": false },
    "integration_test": { "enabled": false }
  },
  "updated_at": "2026-04-01T10:00:00Z",
  "update_history": [
    {
      "field": "testing.unit_test.enabled",
      "from": false,
      "to": true,
      "reason": "사용자 요청: 이제 unit test 포함해서 돌려줘",
      "at": "2026-04-01T12:00:00Z"
    }
  ]
}
```

**동적 변경 흐름:**
```
사용자: "test-web-service에서 이제 unit test 켜줘"
  → Task Manager: project_state.json의 testing.unit_test.enabled = true
  → Workflow Controller: 다음 subtask부터 Unit Test Agent 포함

사용자: "이번 task만 e2e 빼줘"
  → Task Manager: task JSON의 config_override에 e2e_test.enabled=false
  → 해당 task만 적용, project_state 변경 없음
```

**논의 포인트:**
- 시한부 변경("3개 task 동안만")을 project_state에서 관리할지, 아니면 그냥 사용자가 다시 끄라고 할 때까지 유지할지
- config.yaml의 testing 섹션을 시스템 기본값으로 남길지, 아니면 프로젝트 설정으로 완전히 이관할지

### 이슈 3: 프로젝트 셋업 자동화

**현재 문제:**
- config.yaml을 사용자가 직접 편집해야 함
- codebase 경로, git 설정 등을 수동으로 채워야 함
- 새 프로젝트 추가가 번거로움

**사용자 요구:**
- 프로젝트 specific 설정은 작업 시작 시점에 agent와 대화를 통해 생성
- target directory가 없으면 직접 생성
- 이미 있는 코드베이스면 사용자가 지정한 위치로 세팅

**제안 플로우:**
```
사용자: "새 프로젝트 시작"

Task Manager (대화형 셋업):
  1. "프로젝트 이름은?"
  2. "어떤 프로젝트인지 설명해주세요 (기술 스택, 목적 등)"
  3. "기존 코드베이스가 있나요?"
     ├─ 있음 → "경로는?" → codebase_path 설정
     └─ 없음 → 경로 물어보고 → mkdir + git init
  4. "git remote 연동할까요?"
     ├─ 있음 → remote URL 물어보기
     └─ 없음 → git.enabled: false
  5. "테스트는 나중에 활성화할 수 있습니다. 기본 off로 시작합니다."

  → projects/{name}/ 디렉토리 생성
  → project.yaml 자동 작성
  → project_state.json 초기화 (testing 전부 off)
```

**논의 포인트:**
- 셋업을 Task Manager가 담당할지, 별도 setup 명령(대화형)으로 분리할지
- project.yaml을 agent가 코드베이스를 분석해서 일부 자동 채울 수 있을지 (예: package.json을 보고 "Node.js 프로젝트네요" 추론)

### 이슈 4: Testing Agent의 상주 vs 호출 방식

**현재 spec:** testing agent는 Workflow Controller가 매번 `claude -p`로 기동. testing disabled면 해당 agent를 아예 호출하지 않고 skip.

**사용자 요구 방향:**
- testing agent를 포함한 각 agent가 "최대한 이미 실행 상태로 있고"
- handoff 대상에서 테스트 수행 여부에 따라 bypass
- testing agent는 호출 자체가 안 됨 (skip이 아니라 bypass)

**현재 이해:**
"이미 실행 상태"라는 것은 agent 프로세스가 상주한다는 뜻이 아니라, Workflow Controller가 파이프라인 분기 시점에 project_state.json을 읽어서 testing이 꺼져 있으면 해당 agent를 호출하지 않고 다음 agent로 넘어간다는 의미로 이해.

즉 spec의 기존 설계(testing disabled면 skip)와 본질적으로 같지만, 강조점이 다름:
- **spec 원본:** "enabled=false면 skip" (skip이라는 표현이 호출 후 판단하는 것처럼 들림)
- **사용자 의도:** 호출 자체를 하지 않음. Workflow Controller 레벨에서 분기 결정.

**논의 포인트:**
- 이 이해가 맞는지, 아니면 정말로 testing agent를 daemon처럼 상주시키고 싶은 것인지
- Workflow Controller의 파이프라인 결정 로직을 어디서 관리할지 (하드코딩 vs 설정 기반)

### 이슈 5: config.yaml의 역할 축소

**현재 spec:** config.yaml이 모든 설정의 단일 진입점 (프로젝트 정보, git, testing, 장비 정보, credential 등)

**사용자 요구:** config.yaml은 시스템 운영을 위한 HW 정보, credential 정보 정도만 담는다.

**제안하는 config.yaml (시스템 설정만):**
```yaml
# ─── 장비 / 인프라 ───
machines:
  executor:
    ssh_key: "~/.ssh/id_rsa"
    git_credential_helper: "store"
    service_bind_address: "0.0.0.0"
  tester:
    host: "192.168.1.100"
    user: "user"
    ssh_key: "~/.ssh/id_rsa_server"
    browser: "chromium"
    viewport:
      width: 1280
      height: 720
    ssh_reconnect_interval_seconds: 5

# ─── Claude Code ───
claude:
  planner_model: "opus"
  coder_model: "sonnet"
  reviewer_model: "opus"
  e2e_tester_model: "sonnet"
  max_turns_per_session: 50

# ─── 안전 제한 기본값 (프로젝트별 override 가능) ───
default_limits:
  max_subtask_count: 5
  max_retry_per_subtask: 3
  max_replan_count: 2
  max_total_agent_invocations: 30
  max_task_duration_hours: 4

# ─── 로깅 ───
logging:
  level: "info"
  archive_completed_tasks: true
  keep_session_logs: true

# ─── 알림 ───
notification:
  channel: "cli"
```

**프로젝트 설정으로 이관되는 항목:**
- `project.*` (name, description, default_branch)
- `git.*` (enabled, remote, author, auto_merge, pr_target_branch)
- `testing.*` (unit_test, e2e_test, integration_test)
- `human_review_policy.*`
- `executor.codebase_path`, `executor.workspace_dir`, `executor.service_port`
- `limits` override

**논의 포인트:**
- `executor.service_port` 같은 것은 프로젝트별인가 장비별인가? (같은 장비에서 여러 프로젝트가 다른 포트로 기동될 수 있음)
- `tester.test_accounts`는 프로젝트별인가 시스템별인가?
- `notification.channel`을 프로젝트별로 다르게 할 필요가 있는가?

---

## Part 4: 현재 구현 상태

### 생성된 파일 목록

```
claude-agent-coding/
├── config.yaml                    # 현재 test-web-service 용으로 설정됨
├── config.yaml.template           # 변경사항 반영됨 (description, workspace_dir 등)
├── .env                           # GITHUB_TOKEN 설정됨
├── .env.template
├── create_config_and_env.sh       # 템플릿 → 사용자 설정 파일 생성
├── run_agent.sh                   # 기동/종료/상태 CLI (submit 미구현)
├── activate_venv.sh               # venv 활성화 스크립트 (.venv 없으면 생성)
├── requirements.txt               # PyYAML>=6.0
├── CLAUDE.md
├── README.md
│
├── scripts/
│   ├── task_manager.py            # 골격 + task 생성 로직 (CLI, 큐, human interaction 미구현)
│   ├── workflow_controller.py     # 골격 + 한도체크/파이프라인 결정 (inotifywait, agent 호출, 루프백 미구현)
│   ├── run_claude_agent.sh        # claude -p 세션 기동 래퍼 (config 읽기 + cd codebase)
│   └── e2e_watcher.sh             # 테스트장비용 E2E 감시 (골격)
│
├── config/agent_prompts/
│   ├── planner.md, coder.md, reviewer.md, setup.md
│   ├── unit_tester.md, e2e_tester.md, reporter.md
│
└── docs/
    ├── 000-agent-system-spec.md   # 전체 아키텍처 명세 (재설계 전)
    └── 001-handoff-session-01.md  # Session 01 → 02 핸드오프
```

### 미구현 (TODO)

| 파일 | 미구현 내용 |
|------|------------|
| `task_manager.py` | CLI 인터페이스 (submit/status/approve/reject), 작업 큐 관리, human interaction 처리, 프로젝트 셋업 대화 |
| `workflow_controller.py` | inotifywait 감시 루프, 실제 agent 호출 (run_claude_agent.sh 연동), 루프백 분기 로직, project_state 읽기 |
| `run_agent.sh` | submit 명령 구현 |

### 원래 Session 02 목표 (보류됨)

핸드오프 문서에서 계획한 Session 02 목표는 "test-web-service를 대상으로 agent 파이프라인을 실제 구동시키기"였으나, 멀티 프로젝트/동적 설정 재설계 논의가 먼저 필요하여 보류 상태.

원래 계획한 단계:
1. Step 1 — 환경 연결 (config.yaml 설정) → **완료**
2. Step 2 — Phase 0: 파일 흐름 검증 (agent 없이 JSON 상태 전이) → **미착수**
3. Step 3 — Phase 1: 최소 파이프라인 (Planner → Coder → Reviewer) → **미착수**
4. Step 4 — 점진적 확장 (Unit Test, 루프백, retry) → **미착수**

---

## Part 5: 논의 요청 사항

Claude Web에서 다음 주제들을 심도 있게 논의해야 합니다:

1. **config 2계층 분리 확정** — 시스템 config vs 프로젝트 config의 경계선 (어떤 항목이 어디에 속하는지)
2. **멀티 프로젝트 아키텍처** — Task Manager / Workflow Controller의 다중화 방식
3. **동적 테스트 설정** — 3계층 우선순위, 자연어 명령 처리 경로
4. **프로젝트 셋업 자동화** — 대화형 셋업 플로우, 코드베이스 분석 자동화
5. **Testing agent bypass 방식** — Workflow Controller의 파이프라인 분기 로직
6. **spec 문서 업데이트 범위** — 000-agent-system-spec.md를 수정할지, 새 버전으로 만들지

이 논의가 끝나면 spec을 업데이트하고, Phase 0(파일 흐름 검증)부터 재개할 수 있습니다.
