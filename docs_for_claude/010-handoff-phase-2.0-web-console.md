# Phase 2.0 핸드오프 — Web Monitoring Console + SQLite 하이브리드

> 작성: 2026-04-06
> 기준 문서: `docs_for_claude/004-agent-system-spec-v5.md`
> 브랜치: `feature/monitoring-web-console`

---

## 현재 상태 요약

- **Phase 1.0~1.6 완료:** 수동 pipeline → TM → CLI → 알림 → Usage check → Chatbot → 사용성 개선 (177개 테스트)
- **Phase 2.0 진행 중:** Web Monitoring Console 기본 구조 완성, 통합 검증 일부 완료

---

## Phase 2.0 설계 결정

### 하이브리드 아키텍처

| 항목 | 선택 | 이유 |
|------|------|------|
| DB | SQLite (WAL 모드) | 무설정, 단일 파일, 단일 사용자 충분 |
| 웹 프레임워크 | FastAPI + uvicorn | async 지원 (SSE), 자동 API 문서, 경량 |
| 프론트엔드 | Vanilla JS + Jinja2 | 빌드 도구 불필요, Node.js 불필요 |
| 실시간 | SSE (Server-Sent Events) | 단방향(서버→클라) 적합, 브라우저 네이티브 |
| Sync | mtime 기반 폴링 (2초) | 단순, 파일 수 적어 부담 없음 |

### 핵심 원칙

- **Task JSON이 source of truth** — DB는 조회용 캐시
- **파일→DB 단방향 sync** — 웹은 mutation 시 `dispatch()` 호출(파일 수정) → sync → DB 반영
- **protocol.py dispatch() 100% 재활용** — 웹은 Request 변환 후 dispatch 호출만
- **run_system.sh start 시 TM + Web Console 동시 기동**

---

## 구현 완료 항목

### 1. SQLite DB 레이어 (`scripts/web/db.py`)

- **Database 클래스:** WAL 모드, 자동 스키마 생성, 마이그레이션 지원
- **테이블:** projects, tasks, notifications, chatbot_sessions, schema_version
- **tasks 확장 컬럼:** pipeline_stage, pipeline_stage_detail, pipeline_stage_updated_at, failure_reason
- **자동 마이그레이션:** `_migrate()` — `PRAGMA table_info` + `ALTER TABLE ADD COLUMN` 으로 기존 DB에 새 컬럼 안전 추가
- **CRUD:** upsert_project, upsert_task, insert_notification, upsert_session 등
- **집계:** get_task_count_by_status, get_unread_count, get_max_notification_created_at

### 2. 파일→DB Sync 엔진 (`scripts/web/syncer.py`)

- **FileSyncer 클래스:** mtime 기반 변경 감지
- **sync_all():** 전체 프로젝트 + 세션 full sync
- **sync_project(name):** 단일 프로젝트 delta sync (project_state → tasks → notifications)
- **알림 incremental sync:** `max_created_at` 기준으로 새 알림만 INSERT
- **백그라운드 스레드:** 2초 간격 폴링, on_change 콜백으로 SSE event 전달
- **corrupt JSON 내성:** 파싱 실패 시 로그 후 skip

### 3. FastAPI 웹 서버 (`scripts/web/server.py`)

- **Lifespan:** HubAPI, Database, FileSyncer 초기화/정리
- **config.yaml 연동:** `web:` 섹션에서 port, db_path 읽기
- **라우트:**
  - `POST /api/dispatch` — protocol dispatch() 재활용 (모든 mutation)
  - `GET /api/status` — TM 실행 여부 + 시스템 상태
  - `GET /api/projects` — 프로젝트 목록 (DB) + task 개수 + 미읽은 알림
  - `GET /api/projects/{name}` — 프로젝트 상세
  - `GET /api/tasks` — task 목록 (project/status 필터)
  - `GET /api/tasks/{project}/{task_id}` — task 상세
  - `GET /api/tasks/{project}/{task_id}/plan` — plan.json (파일 직접)
  - `GET /api/notifications` — 알림 (limit, unread_only)
  - `GET /api/pending` — 승인 대기 (dispatch 경유)
  - `GET /api/events` — SSE 스트림 (task_updated, project_updated, notification)

### 4. SPA 프론트엔드

- **`templates/index.html`:** SPA 셸 (Jinja2), 캐시 무효화 쿼리스트링
- **`static/app.js`:** 4개 탭 (Dashboard, Tasks, Notifications, Chat)
  - Dashboard: 시스템 상태, 프로젝트 카드, Pending Approvals, 알림 배지
  - Tasks: 필터(project/status), 인라인 detail expand (클릭 시 row 아래에 펼침)
  - Notifications: 읽음/안읽음, 시간순
  - Chat: 기본 구조 (Step 5 예정)
  - SSE EventSource 연결, 모달 시스템
- **`static/style.css`:** 다크 테마 (남색 배경, 시안 accent)
  - 상태별 배지 색상 (submitted, queued, in_progress, waiting_for_human, pending_review, completed, cancelled, failed, planned, needs_replan)
  - pipeline_stage 배지, failure_reason 표시, inline detail 스타일 + slide-down 애니메이션

### 5. Pipeline 가시성 (`scripts/workflow_controller.py` 수정)

- **`update_pipeline_stage(task_file, stage, detail)`:** task JSON에 pipeline_stage 기록
- **`record_failure_reason(task_file, reason)`:** 실패 원인 기록
- **Pipeline stages:** planner → plan_review → git_branch → coder → reviewer → git_push → summarizer → pr_create → finalizing → done
- **실패 기록 지점:** Planner 실패, subtask 0개, Agent 실행 실패, git push 실패, PR 생성 실패
- **git push 에러 핸들링:** try/except 추가 (이전엔 RuntimeError 미처리로 프로세스 크래시)

### 6. 시스템 통합 (`run_system.sh` 수정)

- **start:** TM + Web Console 동시 백그라운드 기동, PID 관리
- **stop:** Web Console 먼저 종료 → TM 종료
- **status:** Web Console 실행 상태 + URL 표시
- **포트 설정:** config.yaml에서 web.port 읽기 (하드코딩 제거)
- **로그:** `logs/web_console.log`

### 7. pending() 확장 (`scripts/hub_api/core.py` 수정)

- `pending_review` 상태 task도 pending 결과에 포함 (기존: waiting_for_human만)
- interaction_type="pending_review", message="PR 리뷰/머지 대기: {title}"

### 8. DB 테스트 (`tests/test_web_db.py` — 36개)

- TestDatabase: 스키마, 프로젝트 CRUD, task CRUD + 필터, 알림 CRUD, 세션 CRUD
- TestFileSyncer: sync 동작, delta skip, on_change 콜백, 백그라운드 start/stop, corrupt JSON

---

## 변경 파일 목록

| 파일 | 변경 유형 | 설명 |
|------|-----------|------|
| `scripts/web/__init__.py` | 신규 | 패키지 초기화 |
| `scripts/web/db.py` | 신규 | SQLite 스키마/CRUD |
| `scripts/web/syncer.py` | 신규 | 파일→DB sync 엔진 |
| `scripts/web/server.py` | 신규 | FastAPI 웹 서버 |
| `scripts/web/static/app.js` | 신규 | SPA 프론트엔드 |
| `scripts/web/static/style.css` | 신규 | 다크 테마 스타일 |
| `scripts/web/templates/index.html` | 신규 | SPA 셸 |
| `tests/test_web_db.py` | 신규 | DB/Syncer 테스트 (36개) |
| `scripts/workflow_controller.py` | 수정 | pipeline_stage 추적, failure_reason 기록, git push 에러 핸들링 |
| `scripts/hub_api/core.py` | 수정 | pending()에 pending_review 포함 |
| `run_system.sh` | 수정 | Web Console 동시 기동/종료/상태, config.yaml 포트 연동 |
| `run_agent.sh` | 수정 | `web` 서브커맨드 추가 |
| `requirements.txt` | 수정 | fastapi, uvicorn, jinja2, aiofiles 추가 |
| `templates/config.yaml.template` | 수정 | `web:` 섹션 추가 (port, db_path) |
| `config.yaml` | 수정 | `web:` 섹션 추가 |
| `.gitignore` | 수정 | `data/`, `test_sample.db` 추가 |

---

## 설정 (config.yaml)

```yaml
web:
  port: 9880                          # 웹 콘솔 포트
  db_path: "data/ai_agent_coding.db"  # SQLite DB 경로
```

---

## 미완료 항목 (TODO)

### 현재 Phase 2.0 범위 잔여

| 항목 | 설명 | 우선순위 |
|------|------|----------|
| **Task lifecycle 정립** | status 전이 규칙 정비, 비정상 전이 방지, pending_review 처리 흐름 | **높음** |
| **Web 오류 수정 + 사용성 개선** | 필터/표시 개선, 액션 버튼 동작 검증, UX 다듬기 | **높음** |
| **웹 채팅** | `scripts/web/web_chatbot.py` — async claude -p 호출, Chat 탭 완성 | 중간 |
| **핸드오프 문서 최종화** | 이 문서를 lifecycle + UX 완료 후 최종 업데이트 | 낮음 |

### 다음 Phase 후보

| Phase | 내용 | 상태 |
|-------|------|------|
| 2.1 | 고급 기능: Pipeline resume, user_preferences, GH_TOKEN, Merge conflict, task 순서 변경 | 미착수 |
| 2.2 | Messenger (Slack/Telegram) | 미착수 |
| 2.3 | E2E 테스트장비 연동, 로컬 E2E | 미착수 |

---

## 코드 진입점

| 파일 | 용도 |
|------|------|
| `scripts/web/server.py` | FastAPI 앱, 라우트, SSE, lifespan |
| `scripts/web/db.py` | SQLite 스키마, CRUD 헬퍼 |
| `scripts/web/syncer.py` | 파일→DB sync 엔진 (mtime 기반) |
| `scripts/web/static/app.js` | SPA 프론트엔드 로직 |
| `scripts/workflow_controller.py` | pipeline_stage/failure_reason 추적 (update_pipeline_stage, record_failure_reason) |
| `scripts/hub_api/core.py` | pending() 확장 (pending_review 포함) |
| `run_system.sh` | TM + Web Console 동시 기동 |
| `docs/images/task-lifecycle-fsm.md` | Task 상태 FSM 다이어그램 (Mermaid) |

---

## 테스트

- **기존 177개 + 신규 36개 = 213개** (web DB/syncer 테스트)
- `./run_test.sh all` 전체 통과 확인
