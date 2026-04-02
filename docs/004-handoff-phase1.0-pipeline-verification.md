# Session Handoff: Phase 1.0 파이프라인 검증 및 Phase 1.0~1.4 진행

> 작성: 2026-04-02  
> 이전 세션: `c7dfe2e` — v2 설계 기준으로 Phase 1.0 재구축  
> 기준 문서: `docs/003-agent-system-spec-v2.md`

---

## 1. 현재 상태

### 완료된 것
- v2 명세(`docs/003-agent-system-spec-v2.md`) 작성 완료
- v1 골격 코드 삭제, v2 기준 Phase 1.0 최소 시스템 구성 완료
- 구현된 파일:
  - `config.yaml.template` — v2 시스템 설정 (시스템 전역만)
  - `project.yaml.template` — 프로젝트 정적 설정 템플릿
  - `scripts/init_project.py` — 대화형 프로젝트 초기화
  - `scripts/run_claude_agent.sh` — Claude Code 세션 기동 래퍼 (project.yaml 기반, --dry-run 지원)
  - `run_agent.sh` — CLI 진입점 (run, init-project, help)
  - `config/agent_prompts/*.md` — 7개 agent 프롬프트 (v1에서 유지, v2 호환)
- dry-run 모드로 프롬프트 조합 검증 완료 (coder=sonnet, planner=opus 모델 결정 정상)
- 에러 케이스 (잘못된 agent, 존재하지 않는 프로젝트 등) 검증 완료

### 아직 안 된 것
- **실제 `claude -p` 호출로 agent를 실행해본 적 없음** (dry-run만 테스트)
- Task Manager, Workflow Controller 미구현 (Phase 1.1 범위)
- 파이프라인 자동화 미구현 (sentinel 감시, agent 체이닝)
- git branch/commit/PR 자동화 미구현

---

## 2. 이번 세션 목표

### 목표 A: 더미 파이프라인 검증 (로그 추가)

**실제 claude -p를 호출하지 않고**, 파이프라인 흐름이 정상적으로 이어지는지 확인한다.

구현 방향:
1. `scripts/run_claude_agent.sh`에 `--dummy` 모드 추가
   - claude 호출 대신 더미 JSON 결과를 출력
   - 각 agent가 기대하는 출력 형식을 시뮬레이션
   - 예: coder → `{"action": "code_complete", "changes_made": [...]}` 형태
2. 수동으로 pipeline 순서대로 실행하며 로그 확인:
   ```bash
   ./run_agent.sh run planner --project demo --task TASK-001 --dummy
   # plan JSON 생성 확인
   ./run_agent.sh run coder --project demo --task TASK-001 --subtask TASK-001-1 --dummy
   # changes_made 기록 확인
   ./run_agent.sh run reviewer --project demo --task TASK-001 --subtask TASK-001-1 --dummy
   # approved 확인
   ```
3. 각 단계 사이에 JSON 파일이 올바르게 생성/갱신되는지 확인
4. 로그 파일이 `projects/{name}/logs/{task_id}/`에 정상 기록되는지 확인

### 목표 B: 테스트 없이 간단한 task를 실제 실행하고 커밋 & 푸시 확인

testing 전부 disabled 상태에서 최소 파이프라인:
```
Coder → Reviewer → (testing 전부 disabled이므로 Setup/UnitTest/E2E/Reporter bypass) → 커밋
```

1. 실제 코드베이스에 대해 간단한 task 실행 (예: README.md 작성)
2. Coder agent가 실제로 코드를 생성하는지 확인
3. Reviewer agent가 리뷰 결과를 JSON으로 출력하는지 확인
4. 수동으로 git commit & push 수행
5. **이 과정에서 발견되는 프롬프트/JSON 문제를 수정**

### 목표 C: Phase 1.0~1.4 순차 진행 계획 수립

목표 A, B 검증 후 발견된 문제를 반영하여:
- Phase 1.0 완료 기준 확정
- Phase 1.1 (파이프라인 자동화) 구현 시작

---

## 3. Phase 로드맵 상세

### Phase 1.0 — 수동 단일 agent 실행 (현재)
- [x] 각 agent의 프롬프트와 JSON 입출력 검증 (dry-run)
- [ ] 더미 모드로 pipeline 흐름 검증
- [ ] 실제 claude -p로 최소 1개 task 완주 (Coder + Reviewer)
- [ ] testing disabled 상태에서 수동 커밋 확인

### Phase 1.1 — 실행장비 내 파이프라인 자동화
구현 대상:
- `scripts/workflow_controller.py` — inotifywait 감시 루프, agent 체이닝
- `scripts/task_manager.py` — WFC spawn/kill, task 라우팅
- `.ready` sentinel 기반 자동 기동
- 4계층 설정 merge 로직 (config.yaml + project.yaml + project_state.json + task.config_override)
- 매 subtask 전 pipeline 구성 결정 (testing 설정에 따라 agent bypass)
- 루프백 로직 (Review 거절 → Coder, 테스트 실패 → Coder)
- safety limits 체크 (max_retry, max_replan 등)
- `run_agent.sh start/stop/status` 명령 구현
- 검증: `.ready` 파일 하나 생성하면 체인이 끝까지 흐르는지

### Phase 1.2 — Planner + Subtask Loop
구현 대상:
- Planner Agent 파이프라인 통합
- subtask 단위 커밋, prior_changes 전달
- re-plan 로직 (Reporter → needs_replan → Planner 재기동)
- CLI로 plan 승인/거절 (`run_agent.sh approve/reject`)
- 검증: 3개 이상 subtask가 순차 실행되고 각각 커밋되는지

### Phase 1.3 — 테스트장비 연동 (E2E)
구현 대상:
- `scripts/e2e_watcher.sh` 완성 (SSH + inotifywait 감시)
- E2E handoff JSON 생성/전송
- SCP 결과 업로드 + .ready sentinel
- SSH 끊김 복구
- `run_agent.sh start-tester` 명령 구현
- 검증: 크로스 머신 handoff 안정성

### Phase 1.4 — 메신저 연동
구현 대상:
- 메신저 플랫폼 결정 (Slack/Telegram/Discord)
- Task Manager에 메시지 수신 로직 추가
- 진행 상황 업데이트, plan 승인 버튼, 에스컬레이션 알림
- 이미지 첨부 자동 다운로드
- 검증: end-to-end가 메신저만으로 완결되는지

---

## 4. 현재 파일 구조

```
claude-agent-coding/
├── config.yaml.template           # v2 시스템 설정 템플릿
├── project.yaml.template          # 프로젝트 설정 템플릿
├── .env.template                  # 환경 변수 템플릿
├── create_config_and_env.sh       # 초기 설정 스크립트
├── run_agent.sh                   # CLI 진입점 (run, init-project, help)
├── activate_venv.sh               # Python venv 활성화
├── requirements.txt               # PyYAML
├── CLAUDE.md                      # 정적 프로젝트 지식
├── README.md
│
├── scripts/
│   ├── init_project.py            # 대화형 프로젝트 초기화
│   ├── run_claude_agent.sh        # Claude Code 세션 래퍼 (--dry-run 지원)
│   └── e2e_watcher.sh             # 테스트장비용 감시 (골격)
│
├── config/
│   └── agent_prompts/             # 7개 agent 프롬프트 (완성)
│       ├── planner.md
│       ├── coder.md
│       ├── reviewer.md
│       ├── setup.md
│       ├── unit_tester.md
│       ├── e2e_tester.md
│       └── reporter.md
│
├── docs/
│   ├── 002-design-evolution-for-web-discussion.md   # v1→v2 배경
│   ├── 003-agent-system-spec-v2.md                  # 현행 기준 ★
│   └── 004-handoff-phase1.0-pipeline-verification.md # 이 문서
│
└── projects/                      # (runtime, 현재 비어 있음)
```

---

## 5. 빠른 시작 (새 세션에서)

```bash
# 1. 현재 상태 확인
cat docs/004-handoff-phase1.0-pipeline-verification.md

# 2. config.yaml 생성 (없으면)
./create_config_and_env.sh

# 3. 테스트용 프로젝트 초기화
./run_agent.sh init-project
# → name: demo, codebase: /tmp/demo-codebase 등

# 4. task JSON 수동 작성
# → projects/demo/tasks/TASK-001.json (예시는 README.md 참고)

# 5. dry-run으로 프롬프트 확인
./run_agent.sh run coder --project demo --task TASK-001 --dry-run

# 6. 목표 A (더미 파이프라인) 또는 목표 B (실제 실행) 진행
```

---

## 6. 주의사항

- `config.yaml`은 gitignored. 새 환경에서는 `./create_config_and_env.sh`로 생성 필요.
- `projects/` 하위의 runtime 데이터(tasks/, logs/ 등)도 gitignored.
- agent 프롬프트 수정 시 `config/agent_prompts/*.md` 직접 편집.
- v2 명세의 상세 내용은 반드시 `docs/003-agent-system-spec-v2.md`를 참조.
- Phase 1.0에서는 Task Manager / Workflow Controller가 없으므로, 모든 실행은 수동.
