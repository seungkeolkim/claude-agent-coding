# Session 01 → Session 02 작업 전달

> 작성: 2026-04-01
> 세션: [ac][260401] agent coding repo initial setting

---

## 완료된 작업

Agent Hub 레포(`claude-agent-coding`)의 초기 스켈레톤 구축 완료.

### 생성된 파일 목록

```
claude-agent-coding/
├── config.yaml.template           # 시스템 설정 템플릿
├── .env.template                  # 환경 변수 템플릿
├── create_config_and_env.sh       # 템플릿 → 사용자 설정 파일 생성
├── run_agent.sh                   # 기동/종료/상태 CLI
├── requirements.txt               # Python 의존성 (PyYAML)
├── CLAUDE.md                      # Claude Code 정적 지식
├── README.md                      # 프로젝트 소개
│
├── scripts/
│   ├── task_manager.py            # Task Manager (골격 + task 생성 로직)
│   ├── workflow_controller.py     # Workflow Controller (골격 + 한도체크/파이프라인 결정)
│   ├── run_claude_agent.sh        # Claude Code 세션 기동 래퍼 (config 읽기 + cd codebase + claude -p)
│   └── e2e_watcher.sh             # 테스트장비용 E2E 감시 (골격)
│
├── config/agent_prompts/
│   ├── planner.md, coder.md, reviewer.md, setup.md
│   ├── unit_tester.md, e2e_tester.md, reporter.md
│
└── docs/
    └── agent-system-spec.md       # 전체 아키텍처 명세 (설계 원본)
```

### 주요 설계 결정 사항

1. **CLI 구독 기반**: API key 불필요. Claude Code CLI(`claude -p`)로 agent 실행
2. **템플릿 패턴**: `config.yaml.template` → `create_config_and_env.sh` → `config.yaml` (gitignored)
3. **절대경로 필수**: config.yaml의 모든 경로는 절대경로 (agent가 cd로 codebase 이동하므로)
4. **git 선택적**: `git.enabled: false`로 git 없는 로컬 프로젝트도 지원
5. **3개 디렉토리 분리**:
   - Agent Hub (`claude-agent-coding/`) — 시스템 코드
   - Codebase (`test-web-service/`) — agent가 작업하는 대상
   - Workspace (`claude-agent-coding/workspaces/test-web-service/`) — runtime 데이터

---

## 다음 세션 목표

**test-web-service 레포를 대상으로 agent 파이프라인을 실제 구동시키기.**

### 테스트 대상 프로젝트

```
경로: /home/azzibobjo/workspace/test-web-service
상태: 빈 깡통 git repo (remote: github.com/seungkeolkim/test-web-service.git)
```

### 추천 진행 순서

#### Step 1 — 환경 연결

```bash
cd /home/azzibobjo/workspace/claude-agent-coding
pip install -r requirements.txt
./create_config_and_env.sh
```

config.yaml에서 설정할 값:
```yaml
project:
  name: "test-web-service"

executor:
  codebase_path: "/home/azzibobjo/workspace/test-web-service"
  workspace_dir: "/home/azzibobjo/workspace/claude-agent-coding/workspaces/test-web-service"

git:
  enabled: false    # 초기 테스트에서는 git off 추천

testing:
  unit_test:
    enabled: false
  e2e_test:
    enabled: false
  integration_test:
    enabled: false
```

#### Step 2 — Phase 0: 파일 흐름 검증 (agent 없이)

agent 호출을 mock으로 대체한 상태에서 JSON 상태 전이를 확인:
1. task submit → `tasks/TASK-001.json` 생성 확인
2. `.ready` sentinel → Workflow Controller 감지 확인
3. subtask status 전이: `queued → planned → in_progress → completed`

#### Step 3 — Phase 1: 최소 파이프라인 (testing 전부 off)

실제 `claude -p`를 호출하는 최단 경로:
```
Planner → Coder → Reviewer → 완료
```

간단한 task 예시: "Hello World를 출력하는 Python 스크립트 생성"

#### Step 4 — 점진적 확장

- Unit Test 활성화
- 루프백 (Review 거절 → Coder 재작성) 검증
- retry 초과 → re-plan 검증

### 현재 미구현 (TODO) 목록

| 파일 | 미구현 내용 |
|------|------------|
| `task_manager.py` | CLI 인터페이스 (submit/status/approve/reject), 작업 큐 관리, human interaction 처리 |
| `workflow_controller.py` | inotifywait 감시 루프, 실제 agent 호출 (run_claude_agent.sh 연동), 루프백 분기 로직 |
| `run_agent.sh` | submit 명령 구현 |

---

## 참고

- 전체 아키텍처 명세: `docs/agent-system-spec.md`
- 테스트 단계별 검증 포인트는 이전 세션 대화에서 정리됨 (Phase 0~3)
