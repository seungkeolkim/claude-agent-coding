"""
Agent Hub 웹 모니터링 콘솔 — FastAPI 서버.

기존 protocol.py의 dispatch()를 100% 재활용하며,
SQLite DB에서 빠른 조회를 제공한다.
"""

import asyncio
import json
import logging
import os
import queue
import sys
import threading
from contextlib import asynccontextmanager
from typing import Optional

import yaml
from fastapi import FastAPI, Query, Request as FastAPIRequest
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# 프로젝트 루트 + scripts/ 를 sys.path에 추가
# hub_api의 내부 import가 `from hub_api.core import ...` 형태이므로 scripts/ 필요
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_scripts_dir = os.path.join(_project_root, "scripts")
for p in [_project_root, _scripts_dir]:
    if p not in sys.path:
        sys.path.insert(0, p)

from hub_api import HubAPI, dispatch as hub_dispatch
from hub_api.protocol import Request as HubRequest
from scripts.web.db import Database
from scripts.web.syncer import FileSyncer

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
# 설정 로드
# ═══════════════════════════════════════════════════════════

def load_web_config(agent_hub_root: str) -> dict:
    """config.yaml에서 web 섹션을 읽는다. 없으면 기본값."""
    config_path = os.path.join(agent_hub_root, "config.yaml")
    defaults = {"port": 9880, "db_path": os.path.join(agent_hub_root, "data", "ai_agent_coding.db")}
    if not os.path.isfile(config_path):
        return defaults
    with open(config_path) as f:
        config = yaml.safe_load(f) or {}
    web_config = config.get("web", {})
    return {**defaults, **web_config}


# ═══════════════════════════════════════════════════════════
# 글로벌 상태 (lifespan에서 초기화)
# ═══════════════════════════════════════════════════════════

hub_api: Optional[HubAPI] = None
db: Optional[Database] = None
syncer: Optional[FileSyncer] = None
# thread-safe queue — ChatProcessor 등 백그라운드 스레드에서도 안전하게 push 가능
event_queue: queue.Queue = queue.Queue(maxsize=500)
agent_hub_root: str = ""


def _on_change(event: dict):
    """syncer 변경 콜백 → SSE event queue에 push + chat 세션에 notification 주입."""
    try:
        event_queue.put_nowait(event)
    except queue.Full:
        pass  # 큐가 가득 차면 무시

    # notification 이벤트는 Notification 탭에서 표시.
    # chat 세션에 주입하지 않음 (무관한 프로젝트의 이벤트가 섞이는 문제 방지).


# ═══════════════════════════════════════════════════════════
# Lifespan — 서버 시작/종료 시 초기화/정리
# ═══════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    """서버 시작 시 DB/Syncer 초기화, 종료 시 정리."""
    global hub_api, db, syncer, agent_hub_root

    agent_hub_root = os.environ.get("AGENT_HUB_ROOT", _project_root)
    web_config = load_web_config(agent_hub_root)

    # HubAPI 초기화
    hub_api = HubAPI(agent_hub_root)

    # DB 초기화
    db = Database(web_config["db_path"])

    # Syncer 초기화 + 최초 full sync + 백그라운드 시작
    syncer = FileSyncer(
        db=db,
        projects_dir=os.path.join(agent_hub_root, "projects"),
        session_history_dir=os.path.join(agent_hub_root, "session_history"),
        on_change=_on_change,
    )
    syncer.sync_all()
    syncer.start_background_sync(interval_seconds=2.0)

    logger.info("웹 서버 초기화 완료 (DB: %s)", web_config["db_path"])
    yield

    # 종료
    syncer.stop_background_sync()
    logger.info("웹 서버 종료")


# ═══════════════════════════════════════════════════════════
# FastAPI 앱
# ═══════════════════════════════════════════════════════════

app = FastAPI(title="Agent Hub Console", lifespan=lifespan)

# 정적 파일 & 템플릿
_web_dir = os.path.dirname(__file__)
app.mount("/static", StaticFiles(directory=os.path.join(_web_dir, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(_web_dir, "templates"))


# ─── 메인 페이지 ───

@app.get("/", response_class=HTMLResponse)
async def index(request: FastAPIRequest):
    """SPA 메인 페이지."""
    return templates.TemplateResponse(request, "index.html")


# ─── PR 비동기 액션 처리 ───

# PR 작업 중인 task 추적 (key: "project/task_id")
_pr_processing: dict[str, bool] = {}

PR_ASYNC_ACTIONS = {"merge_pr", "close_pr"}


def _run_pr_action_background(action: str, project: str, task_id: str, message: Optional[str]):
    """백그라운드 스레드에서 gh PR 액션을 실행하고 결과를 SSE로 전달한다."""
    key = f"{project}/{task_id}"
    try:
        params = {"task_id": task_id}
        if message:
            params["message"] = message
        request = HubRequest(action=action, project=project, params=params,
                             source="web", requested_by="web")
        response = hub_dispatch(hub_api, request)

        if response.success:
            # 성공 — syncer로 DB 갱신 후 SSE 이벤트 발행
            if syncer:
                syncer.sync_project(project)
            _on_change({
                "type": "pr_action_result",
                "project": project,
                "task_id": task_id,
                "action": action,
                "success": True,
            })
        else:
            # hub_api 레벨 에러 (상태 불일치 등)
            error_msg = response.error.get("message", "알 수 없는 오류") if response.error else "알 수 없는 오류"
            _on_change({
                "type": "pr_action_result",
                "project": project,
                "task_id": task_id,
                "action": action,
                "success": False,
                "error": error_msg,
            })
    except Exception as exc:
        # subprocess 실패, 네트워크 오류 등
        _on_change({
            "type": "pr_action_result",
            "project": project,
            "task_id": task_id,
            "action": action,
            "success": False,
            "error": str(exc),
        })
    finally:
        _pr_processing.pop(key, None)


# ─── Protocol dispatch (mutation) ───

@app.post("/api/dispatch")
async def api_dispatch(body: dict):
    """
    기존 protocol.py dispatch()를 그대로 호출한다.
    모든 mutation(submit, approve, reject 등)은 이 엔드포인트를 통한다.

    merge_pr, close_pr 액션은 비동기로 처리한다:
    즉시 accepted 응답을 반환하고, 결과는 SSE pr_action_result 이벤트로 전달.
    """
    request = HubRequest.from_dict(body)
    request.source = "web"
    # 현재 web은 단일 사용자 가정. 다중 사용자 지원 시 auth에서 식별자 주입.
    if not request.requested_by:
        request.requested_by = "web"

    # merge_pr / close_pr → 비동기 처리
    if request.action in PR_ASYNC_ACTIONS:
        task_id = (request.params or {}).get("task_id", "")
        key = f"{request.project}/{task_id}"

        if key in _pr_processing:
            return {"success": False, "error": {"code": "ALREADY_PROCESSING", "message": "이미 처리 중입니다."}}

        _pr_processing[key] = True
        message = (request.params or {}).get("message")
        thread = threading.Thread(
            target=_run_pr_action_background,
            args=(request.action, request.project, task_id, message),
            daemon=True,
        )
        thread.start()
        return {"success": True, "message": "요청 접수됨. 처리 결과는 알림으로 전달됩니다.", "accepted": True}

    response = hub_dispatch(hub_api, request)

    # mutation 후 즉시 sync
    if request.project and syncer:
        syncer.sync_project(request.project)

    return response.to_dict()


# ─── 조회 편의 엔드포인트 (DB 직접 쿼리) ───

@app.get("/api/status")
async def api_status():
    """시스템 상태 조회 (TM 실행 여부 포함)."""
    # TM 실행 여부는 파일 기반이므로 dispatch로 조회
    request = HubRequest(action="status", source="web")
    response = hub_dispatch(hub_api, request)
    return response.to_dict()


@app.get("/api/projects")
async def api_projects(include_closed: bool = False):
    """프로젝트 목록 조회 (DB)."""
    all_projects = db.get_projects()
    projects = all_projects if include_closed else [
        p for p in all_projects if p.get("lifecycle", "active") != "closed"
    ]
    # 각 프로젝트의 task 개수 추가
    for p in projects:
        counts = db.get_task_count_by_status(p["name"])
        p["task_counts"] = counts
        p["unread_notifications"] = db.get_unread_count(p["name"])
    return {"success": True, "data": projects}


@app.get("/api/projects/{name}")
async def api_project_detail(name: str):
    """프로젝트 상세 조회 (DB)."""
    project = db.get_project(name)
    if not project:
        return JSONResponse(
            status_code=404,
            content={"success": False, "error": {"code": "PROJECT_NOT_FOUND", "message": f"프로젝트 '{name}'을 찾을 수 없습니다."}},
        )
    project["task_counts"] = db.get_task_count_by_status(name)
    project["unread_notifications"] = db.get_unread_count(name)
    return {"success": True, "data": project}


_PRE_RUNNING_STATUSES = {"submitted", "planned"}


def _active_task_ids(project_names):
    """
    각 프로젝트의 project_state.json을 읽어 현재 실행 중인 task_id를 맵으로 반환.
    status=='running'이 아닌 프로젝트는 포함되지 않는다.
    """
    active = {}
    for name in project_names:
        if not name:
            continue
        state_path = os.path.join(agent_hub_root, "projects", name, "project_state.json")
        if not os.path.isfile(state_path):
            continue
        try:
            with open(state_path) as f:
                s = json.load(f)
        except Exception:
            continue
        if s.get("status") == "running" and s.get("current_task_id"):
            active[name] = s["current_task_id"]
    return active


def _apply_running_override(tasks, project_filter=None):
    """
    DB의 raw status를 표시용으로 보정: project_state가 해당 task를 실행 중으로
    지목하고 task.status가 submitted/planned라면 'running'으로 치환한다.
    Planner 진행 중 task가 큐 대기와 구분되지 않는 문제를 해결.
    """
    if project_filter:
        project_names = {project_filter}
    else:
        project_names = {t.get("project") for t in tasks}
    active = _active_task_ids(project_names)
    if not active:
        return tasks
    for t in tasks:
        proj = t.get("project")
        if (proj in active and t.get("task_id") == active[proj]
                and t.get("status") in _PRE_RUNNING_STATUSES):
            t["status"] = "running"
    return tasks


@app.get("/api/tasks")
async def api_tasks(project: Optional[str] = None, status: Optional[str] = None):
    """task 목록 조회 (DB)."""
    tasks = db.get_tasks(project=project, status=status)
    tasks = _apply_running_override(tasks, project_filter=project)
    return {"success": True, "data": tasks}


@app.get("/api/tasks/{project}/{task_id}")
async def api_task_detail(project: str, task_id: str):
    """task 상세 조회 (DB)."""
    task = db.get_task(project, task_id)
    if not task:
        return JSONResponse(
            status_code=404,
            content={"success": False, "error": {"code": "TASK_NOT_FOUND", "message": f"task '{task_id}'를 찾을 수 없습니다."}},
        )
    _apply_running_override([task], project_filter=project)
    return {"success": True, "data": task}


@app.get("/api/tasks/{project}/{task_id}/plan")
async def api_task_plan(project: str, task_id: str):
    """task의 plan.json을 파일에서 직접 읽는다."""
    request = HubRequest(action="get_plan", project=project, params={"task_id": task_id}, source="web")
    response = hub_dispatch(hub_api, request)
    return response.to_dict()


@app.get("/api/notifications")
async def api_notifications(
    project: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=200),
    unread_only: bool = False,
):
    """알림 목록 조회 (DB)."""
    notifications = db.get_notifications(project=project, limit=limit, unread_only=unread_only)
    unread_count = db.get_unread_count(project=project)
    return {"success": True, "data": notifications, "unread_count": unread_count}


@app.get("/api/pending")
async def api_pending():
    """승인 대기 항목 조회 (dispatch 경유 — human_interaction 상세 정보 필요)."""
    request = HubRequest(action="pending", source="web")
    response = hub_dispatch(hub_api, request)
    return response.to_dict()


# ─── Chat API ───


def _chat_on_message(event: dict):
    """ChatProcessor의 on_message 콜백 → SSE event queue에 push."""
    try:
        event_queue.put_nowait(event)
    except queue.Full:
        pass


@app.post("/api/chat/session")
async def api_chat_session(body: dict = {}):
    """
    Chat 세션을 생성하거나 복원한다.

    Request body: {"session_id": "optional_existing_id"}
    Response: {"success": true, "session_id": "...", "history": [...]}
    """
    from scripts.web.web_chatbot import get_or_create_session

    session_id = body.get("session_id")
    processor = get_or_create_session(
        agent_hub_root=agent_hub_root,
        session_id=session_id,
        on_message=_chat_on_message,
    )
    return {
        "success": True,
        "session_id": processor.session_id,
        "history": processor.conversation_history,
    }


@app.post("/api/chat/send")
async def api_chat_send(body: dict):
    """
    Chat 메시지를 전송한다 (fire-and-forget).

    Request body: {"session_id": "...", "message": "..."}
    Response: {"success": true} (실제 응답은 SSE chat_message로 전달)
    """
    from scripts.web.web_chatbot import get_or_create_session

    session_id = body.get("session_id")
    message = body.get("message", "").strip()

    if not session_id:
        return {"success": False, "error": {"code": "MISSING_SESSION", "message": "session_id가 필요합니다."}}
    if not message:
        return {"success": False, "error": {"code": "EMPTY_MESSAGE", "message": "메시지가 비어있습니다."}}

    processor = get_or_create_session(
        agent_hub_root=agent_hub_root,
        session_id=session_id,
        on_message=_chat_on_message,
    )
    processor.submit_message(message)

    return {"success": True}


@app.get("/api/chat/sessions")
async def api_chat_sessions():
    """Chat 세션 목록을 반환한다."""
    from chatbot import list_sessions as _list_sessions
    sessions = _list_sessions(agent_hub_root, frontend="web")
    return {"success": True, "data": sessions}


@app.get("/api/chat/history/{session_id}")
async def api_chat_history(session_id: str):
    """Chat 세션 히스토리를 반환한다."""
    from chatbot import load_session as _load_session
    history = _load_session(agent_hub_root, session_id, frontend="web")
    if history is None:
        return {"success": False, "error": {"code": "SESSION_NOT_FOUND", "message": f"세션 '{session_id}'을 찾을 수 없습니다."}}
    return {"success": True, "data": history}


@app.patch("/api/chat/sessions/{session_id}")
async def api_chat_session_rename(session_id: str, body: dict):
    """Chat 세션 제목을 변경한다."""
    from chatbot import rename_session as _rename_session

    title = body.get("title")
    if not title or not title.strip():
        return {"success": False, "error": {"code": "INVALID_TITLE", "message": "제목이 비어있습니다."}}

    ok = _rename_session(agent_hub_root, session_id, title.strip(), frontend="web")
    if not ok:
        return {"success": False, "error": {"code": "SESSION_NOT_FOUND", "message": f"세션 '{session_id}'을 찾을 수 없습니다."}}
    return {"success": True}


@app.delete("/api/chat/sessions/{session_id}")
async def api_chat_session_delete(session_id: str):
    """Chat 세션을 삭제한다."""
    from chatbot import delete_session as _delete_session
    from scripts.web.web_chatbot import remove_session

    remove_session(session_id)
    ok = _delete_session(agent_hub_root, session_id, frontend="web")
    if not ok:
        return {"success": False, "error": {"code": "SESSION_NOT_FOUND", "message": f"세션 '{session_id}'을 찾을 수 없습니다."}}
    return {"success": True}


# ─── SSE 실시간 이벤트 스트림 ───

@app.get("/api/events")
async def api_events():
    """Server-Sent Events 스트림. syncer의 변경 이벤트를 실시간 전달."""

    async def event_generator():
        heartbeat_interval = 5.0  # 초
        elapsed = 0.0
        poll_interval = 0.1  # 100ms 간격 폴링
        while True:
            try:
                event = event_queue.get_nowait()
                yield f"event: {event.get('type', 'update')}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                elapsed = 0.0
            except queue.Empty:
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval
                if elapsed >= heartbeat_interval:
                    yield ": heartbeat\n\n"
                    elapsed = 0.0

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ═══════════════════════════════════════════════════════════
# 서버 실행 진입점
# ═══════════════════════════════════════════════════════════

def main():
    """웹 서버를 시작한다."""
    import uvicorn

    root = os.environ.get("AGENT_HUB_ROOT", _project_root)
    web_config = load_web_config(root)
    port = web_config.get("port", 9880)

    print(f"\n  Agent Hub Console")
    print(f"  http://localhost:{port}")
    print(f"  Press Ctrl+C to stop\n")

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
