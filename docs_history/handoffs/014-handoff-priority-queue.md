# Priority Queue 구현 핸드오프

> 작성: 2026-04-13 (최신화: 2026-04-13, v07 조각모음 반영)
> 기준 문서: `docs/agent-system-spec-v07.md`
> 브랜치: `main` (최신) — 새 feature 브랜치 생성 필요
> 이전 커밋: `36ce58a` (Priority queue 구현 핸드오프 문서 작성) — 이 문서가 추가된 커밋

---

## 배경

현재 task 큐는 `tasks/{id}.ready` sentinel 파일 기반 단일 FIFO. task id 순서대로 처리되며 우선순위 개념 없음. 긴급 task를 앞에 끼워 넣을 방법이 없다.

**사용자 요구사항:**
- task id는 계속 순차 증가하는 integer 유지 (priority와 무관)
- priority 3단계: `critical` > `urgent` > `default`
- queue는 priority별로 별도 파일 관리 (`task_queue_critical.json` 등)
- 파일 내용은 순수 id 배열 (상태 정보 없음)
- 실행 순서: `critical` (id순) → `urgent` (id순) → `default` (id순)
- 동시성 제어: `fcntl.flock`

---

## 설계 결정 사항 (사용자와 합의됨)

### 1. Queue 파일 형식

```json
// projects/{name}/task_queue_critical.json
[7, 12, 15]
```

- **순수 id 배열**. `{"queued": [], "completed": []}` 같은 상태 정보 포함하지 않음.
- 이유: task.json이 status의 source of truth. queue 파일에 중복 저장 시 sync 불일치 위험.
- `completed` 이력은 task.json의 `status` 필드로 조회.

### 2. Queue 파일 위치 / 파일명

- 경로: `projects/{name}/task_queue_{priority}.json`
- priority 값: `critical`, `urgent`, `default`
- 3개 파일 항상 존재 (빈 배열로 초기화)

### 3. Source of Truth 역할 분리

| 정보 | Source of Truth |
|------|-----------------|
| task 상태 (submitted/in_progress/completed/cancelled 등) | `task JSON` |
| task 제출 순서/우선순위 | `task_queue_*.json` |

→ TM은 queue 파일로 "어떤 task를 다음에 실행할지" 결정, task JSON으로 "그 task가 여전히 실행 가능한 상태인지" 확인.

### 4. `.ready` sentinel 제거

현재 사용처 (Task 큐용):
- `hub_api/core.py:370` — submit() 시 생성
- `task_manager.py:203` — find_ready_tasks() 감지
- `task_manager.py:1059` — consume_ready_sentinel() 삭제
- `hub_api/core.py:455~458` — cancel() 시 정리

모두 queue 파일 조작으로 대체.

**제거 대상이 아닌 `.ready`:**
- E2E 테스트 handoff (`handoffs/*-e2e.ready`, `handoffs/*-e2e-result.ready`) — 크로스 머신 통신용, 별개 시스템. 유지.
- WFC 내부 subtask 간 `.ready` — 사용 없음 (확인 완료).

### 5. 동시성 제어

`fcntl.flock`으로 read-modify-write 보호. 단일 머신, 짧은 구간이므로 성능 영향 미미.

lock 필요 시점:
- submit: queue 파일에 id append
- TM pop: queue 파일에서 id 제거
- cancel (waiting_for_human_plan_confirm 상태): queue 파일에서 id 제거 (submit 직후 취소 케이스)

### 6. TM pop 로직

```
1. critical → urgent → default 순으로 queue 파일 peek
2. 첫 번째 id 찾으면 task.json status 확인
3. status가 실행 가능(submitted/queued)이면 → 해당 id를 queue에서 제거 → spawn
4. status가 아니면(cancelled/failed 등) → queue에서만 제거하고 다음 id 확인
```

**장점**: cancel/fail 시 queue 파일을 별도로 건드릴 필요 없음 (TM이 알아서 skip + cleanup).

---

## 구현 범위

### 변경 파일 (예상)

| 파일 | 변경 내용 |
|------|-----------|
| `scripts/hub_api/core.py` | `submit()` — priority 파라미터, queue 파일 append (flock) |
| `scripts/hub_api/core.py` | `cancel()` — .ready 제거 → queue 파일에서 id 제거 (flock) |
| `scripts/hub_api/protocol.py` | `_handle_submit()` — priority 파라미터 파싱 |
| `scripts/task_manager.py` | `find_ready_tasks()` → `find_next_task()` (priority 순회) |
| `scripts/task_manager.py` | `consume_ready_sentinel()` → queue pop 로직 |
| `scripts/init_project.py` | 프로젝트 초기화 시 3개 queue 파일 생성 (빈 배열) |
| `scripts/chatbot.py` | chatbot에서 priority 지정 가능하도록 (선택) |
| `scripts/web/server.py` / `app.js` | Web UI에서 priority 선택 가능 (선택) |
| `docs/agent-system-spec-v07.md` | 구조 변경 반영 |
| 테스트 | queue 파일 기반 테스트로 수정 + 신규 priority 테스트 |

### 신규 헬퍼 함수

`scripts/hub_api/queue_helpers.py` (신규 파일 권장):
```python
def append_to_queue(project_dir, priority, task_id) -> None
def remove_from_queue(project_dir, priority, task_id) -> bool
def peek_next_task(project_dir) -> tuple[priority, task_id] | None
def pop_task(project_dir, priority, task_id) -> bool
```

모두 `fcntl.flock` 래핑.

---

## 현재 `.ready` 사용 현황 조사 결과

**Task 큐 용도 (제거 대상):** `submit 1곳 생성 → TM 1곳 소비 → cancel 1곳 정리` 세 지점만 존재. 교체 범위 작음.

**WFC 내부:** subtask 간 `.ready` 사용 **없음**. subtask 순차 실행은 task.json의 `completed_subtasks` 필드로 관리.

**E2E handoff (유지):** `scripts/e2e_watcher.sh`가 inotifywait로 감시. 크로스 머신 SSH 기반. queue 작업과 분리.

**테스트 코드:** `consume_ready_sentinel`/`find_ready_tasks`/`submit` 경로 테스트는 queue 기반으로 수정 필요.

---

## 주의사항

1. **테스트 코드 마이그레이션**: `tests/` 내에 `.ready` 파일 생성/검증하는 테스트 다수. 꼼꼼히 찾아서 queue 파일 기반으로 교체.

2. **Queue 파일 초기화 시점**: 기존 프로젝트에도 queue 파일이 없을 수 있음. TM/hub_api 진입 시 없으면 자동 생성 (빈 배열) 로직 필요.

3. **Race 시나리오 검토**:
   - submit A + submit B 동시 → flock으로 보호
   - submit + cancel 동시 → flock으로 보호
   - TM pop + cancel 동시 → TM이 pop 직후 task.json status 확인 단계에서 cancelled 감지하면 무시하고 다음. 여기엔 flock 불필요(각 연산이 독립적)

4. **backward compatibility**: 기존에 `.ready`로 대기 중이던 task가 있을 수 있음. 마이그레이션 로직: TM 시작 시 남은 `.ready` 파일 스캔하여 queue 파일로 이주 후 `.ready` 삭제.

5. **Priority 기본값**: `default`. `submit()` 호출 시 priority 미지정하면 default로.

6. **Spec 문서**: Phase 2.2+로 표시된 "task 순서 변경 / 큐 내 task 우선순위 변경" 항목이 이 작업에 해당. 완료 시 상태 업데이트.

---

## 테스트 시나리오

1. submit 3개 (default) → id 순서대로 실행 확인
2. submit 2개 (default) + submit 1개 (critical) → critical 먼저 실행
3. submit critical + submit urgent + submit default → critical → urgent → default 순
4. waiting_for_human_plan_confirm 상태에서 cancel → queue 파일에서 제거됨 확인
5. submit 직후 cancel → queue 파일에 추가 후 바로 제거됨 확인
6. 동시 submit 2개 (race) → 둘 다 queue에 append됨 확인 (flock 동작)
7. TM이 cancelled task를 queue에서 만나면 skip 확인
8. 기존 `.ready` 파일이 남은 상태로 TM 시작 → 자동 이주 확인

---

## 이전 작업 상태

**최근 커밋 (main HEAD = 36ce58a 기준):**
- `36ce58a` Priority queue 구현 핸드오프 문서 작성 (이 문서)
- `40d4dac` Merge feature/web_chat_error_correction into main
- `1c2d41b` Cancel 시 project_state.json idle로 갱신 → v07 반영됨
- `26baeb5` Safety limiter: task duration 계산에서 사람 대기 시간 제외 → v07 반영됨
- `c401083` Web Chat 사용성 개선: 사이드바, SSE 실시간 전달, 알림 표시 → v07 반영됨
- `ed19b73` WFC graceful shutdown + resume (SIGTERM 기반 안전 종료 및 TM 자동 재시작) → v07 반영됨

**테스트 현황**: 266개 전체 통과 (`./run_test.sh all`, pytest --collect-only 기준)

**Priority Queue 구현과의 관계**:
- graceful shutdown/resume과 queue 전환은 독립적 작업. TM이 재기동되더라도 queue 파일 기반 pop 로직만 구현되면 resume_waiting_tasks()는 그대로 동작해야 함.
- `resume_waiting_tasks()`는 task.json 상태 기반으로 재개 대상을 찾으므로 queue 파일과 무관하지만, queue에 남아 있는 cancelled/failed id를 TM이 skip하는 로직이 함께 동작해야 함 (본 문서 "6. TM pop 로직" 참고).

---

## 작업 시작 가이드

```bash
# 1. 최신 main 확인
git checkout main && git pull

# 2. feature 브랜치 생성
git checkout -b feature/priority_queue

# 3. 기존 .ready 사용처 재확인
grep -rn "\.ready" scripts/ tests/ | grep -v e2e_watcher

# 4. 테스트 먼저 돌려 baseline 확보
./run_test.sh all

# 5. 구현 순서 (권장):
#    a. queue_helpers.py 신규 + unit test
#    b. init_project.py에 queue 파일 3개 초기화
#    c. hub_api.submit() priority 파라미터 + queue append
#    d. hub_api.cancel() queue 제거
#    e. TM find_next_task() + pop 로직
#    f. .ready 마이그레이션 로직
#    g. 기존 테스트 마이그레이션
#    h. 신규 priority 테스트
#    i. chatbot/web priority UI (선택)
#    j. spec 문서 업데이트
```

---

## 참고: spec 문서에서 업데이트할 항목

`docs/agent-system-spec-v07.md` (section 기반, line은 편집에 따라 drift):

- **§1 핵심 설계 원칙 #3**: "파일 기반 통신: JSON 파일 + `.ready` sentinel" → queue 파일(`task_queue_*.json`) 기반 우선순위 큐로 교체.
- **§3.3 Task Manager의 역할**: `.ready` sentinel 감지 → queue 파일 peek/pop. PR Watcher/resume_waiting_tasks와 병행 기술.
- **§3.5 TM ↔ WFC 통신**: `tasks/{id}.ready` sentinel 감지 → `task_queue_*.json` 최상위 id peek.
- **§4.1 Task Manager 상세**: 폴링 대상 변경 (`.ready` → queue 파일 mtime/내용).
- **§5.1 전체 사이클 1번**: `hub_api: task JSON 생성 + .ready sentinel` → `task JSON 생성 + queue 파일에 id append (priority별)`.
- **§6.1~6.2 통신 구조 테이블**: `tasks/{id}.ready` 행 → `task_queue_*.json` 행으로 교체.
- **§10.1 디렉토리 구조**: `projects/{name}/` 아래 `task_queue_{critical,urgent,default}.json` 3개 파일 추가.
- **§15.3 TODO의 "Priority Queue" 행**: 구현 완료 후 §15.1로 이동.
- **§15.4 Phase 로드맵의 "2.2 Priority Queue (다음)"**: 완료로 상태 변경.
