# 설계 히스토리 아카이브

> 이 문서는 `002-design-evolution-for-web-discussion.md`와 `004-handoff-phase1.0-pipeline-verification.md`를 병합한 것입니다.
> 두 문서의 원본은 삭제합니다. 현행 기준 문서는 `003-agent-system-spec-v2.md`입니다.

---

## 1. v1 → v2 주요 설계 변경 (002 요약)

원본 작성: 2026-04-01. Claude Web에서 아키텍처 재설계를 논의하기 위한 컨텍스트 전달 문서.

### 반영 완료된 변경

| 변경 | 요약 |
|------|------|
| project 설정: language/framework → description | LLM 기반이므로 discrete 필드 불필요. 자연어 description으로 대체 |
| workspace_dir 선택적 설정 | 미설정 시 규칙 기반 기본값 사용 |
| sub_executor 주석 처리 | 보조 실행장비 당분간 미사용 |
| integration_test.include_e2e 관계 명확화 | e2e_test.enabled=false면 include_e2e도 무시됨 |
| auto_merge 기본값 false | 초기 테스트 안전을 위해 |

### 논의 후 확정된 설계

| 이슈 | 결론 |
|------|------|
| 멀티 프로젝트 지원 | `projects/{name}/` 구조 채택. 프로젝트당 독립 WFC 프로세스 |
| 테스트 동적 변경 | 4계층 설정 (config → project.yaml → project_state.json → task.config_override) |
| 프로젝트 셋업 | `init_project.py` 대화형 초기화 구현 완료 |
| Testing bypass | 호출 자체 안 함 (skip이 아님). WFC 레벨에서 pipeline 구성 시 결정 |
| config.yaml 역할 축소 | 시스템 설정만. 프로젝트 설정은 project.yaml로 이관 |

---

## 2. Phase 1.0 진행 기록 (004 요약)

원본 작성: 2026-04-02. Phase 1.0 파이프라인 검증 핸드오프 문서.

### Phase 1.0 목표 및 달성 상태

| 목표 | 상태 |
|------|------|
| 더미 파이프라인 검증 (dummy 모드) | **완료** — 전체 pipeline dummy 사이클 통과 |
| 실제 claude -p로 task 완주 | **완료** — task 00002~00006 실행, PR 생성/머지 확인 |
| Git 자동화 (branch/commit/push/PR) | **완료** — feature/{task_id}-{설명} 브랜치, subtask별 커밋, PR 기반 머지 |
| Summarizer agent | **완료** — task 요약 + PR title/body 생성 |
| auto_merge on/off | **완료** — true: PR+자동머지, false: PR생성+pending_review |
| Replan 로직 | **완료** — dummy 모드에서 reporter→replan→planner 재기동 검증 |
| Safety limits | **완료** — check_safety_limits.py 동작 확인 |

### Phase 1.0에서 발견/해결한 문제

| 문제 | 해결 |
|------|------|
| claude -p JSON wrapper 파싱 | `extract_agent_result()` 추가 — wrapper의 `result` 텍스트에서 ```json``` 코드블록 추출 |
| 한국어 브랜치명 | Planner가 영문 `branch_name` 제안, WFC가 `feature/{task_id}-` 접두사 보장 |
| gh CLI 인증 | `ensure_gh_auth()` — project.yaml의 auth_token으로 자동 로그인 + repo 권한 확인 |
| summarizer VALID_AGENTS 누락 | run_agent.sh, run_claude_agent.sh 양쪽 모두 추가 |

---

## 3. Phase 로드맵 (현재 시점 기준)

| Phase | 내용 | 상태 |
|-------|------|------|
| 1.0 | 수동 단일 agent + pipeline 실행 | **완료** |
| 1.1 | Task Manager + 자동 파이프라인 | **다음** |
| 1.2 | ~~Planner + Subtask Loop~~ | 1.0에서 이미 구현 (Phase 재조정 필요) |
| 1.3 | 테스트장비 연동 (E2E) | 미착수 |
| 1.4 | 메신저 연동 | 미착수 |
| 2.0 | 웹 모니터링 대시보드 | 미착수 |

**참고:** 원래 Phase 1.1~1.2로 분리되어 있던 "파이프라인 자동화"와 "Planner + Subtask Loop"가 Phase 1.0에서 이미 WFC로 통합 구현되었으므로, 다음 단계는 Task Manager 구현(원래 Phase 1.1의 TM 부분)으로 넘어가면 됩니다.
