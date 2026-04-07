"""
Agent Hub 웹 모니터링 콘솔 — FastAPI 서버.

기존 protocol.py의 dispatch()를 100% 재활용하며,
SQLite DB에서 빠른 조회를 제공한다.
"""

import asyncio
import json
import logging
import os
import sys
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
event_queue: asyncio.Queue = asyncio.Queue()
agent_hub_root: str = ""


def _on_change(event: dict):
    """syncer 변경 콜백 → SSE event queue에 push."""
    try:
        event_queue.put_nowait(event)
    except asyncio.QueueFull:
        pass  # 큐가 가득 차면 무시


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


# ─── Protocol dispatch (mutation) ───

@app.post("/api/dispatch")
async def api_dispatch(body: dict):
    """
    기존 protocol.py dispatch()를 그대로 호출한다.
    모든 mutation(submit, approve, reject 등)은 이 엔드포인트를 통한다.
    """
    request = HubRequest.from_dict(body)
    request.source = "web"
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


@app.get("/api/tasks")
async def api_tasks(project: Optional[str] = None, status: Optional[str] = None):
    """task 목록 조회 (DB)."""
    tasks = db.get_tasks(project=project, status=status)
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


# ─── SSE 실시간 이벤트 스트림 ───

@app.get("/api/events")
async def api_events():
    """Server-Sent Events 스트림. syncer의 변경 이벤트를 실시간 전달."""

    async def event_generator():
        while True:
            try:
                # 5초 타임아웃으로 heartbeat 겸용
                event = await asyncio.wait_for(event_queue.get(), timeout=5.0)
                yield f"event: {event.get('type', 'update')}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            except asyncio.TimeoutError:
                # heartbeat (연결 유지용)
                yield ": heartbeat\n\n"

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
