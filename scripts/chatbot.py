#!/usr/bin/env python3
"""
Agent Hub Chatbot — 자연어 대화형 인터페이스 (Phase 1.5)

사용자가 자연어로 시스템을 제어한다.
claude -p를 통해 사용자 의도를 파악하고,
protocol.dispatch()를 통해 실행한다.

사용법:
    python3 scripts/chatbot.py
    ./run_agent.sh chat
    ./run_agent.sh chat --confirmation-mode always_confirm
"""

import argparse
import json
import os
import random
import re
import string
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# hub_api 패키지를 import할 수 있도록 scripts/ 디렉토리를 path에 추가
SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from hub_api.core import HubAPI
from hub_api.protocol import (
    ACTION_REGISTRY,
    Request,
    Response,
    dispatch,
    get_action_descriptions,
)


# ═══════════════════════════════════════════════════════════
# 색상 상수
# ═══════════════════════════════════════════════════════════

GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
RED = "\033[0;31m"
CYAN = "\033[0;36m"
BOLD = "\033[1m"
DIM = "\033[2m"
NC = "\033[0m"


# ═══════════════════════════════════════════════════════════
# Action 분류
# ═══════════════════════════════════════════════════════════

# 조회성 — 항상 즉시 실행
READ_ONLY_ACTIONS = frozenset({
    "list", "get_task", "get_plan", "pending", "status", "notifications",
})

# 고위험 실행성 — smart 모드에서 확인 필요
HIGH_RISK_ACTIONS = frozenset({
    "submit", "approve", "reject", "cancel", "config",
    "create_project", "close_project", "reopen_project", "resubmit",
    "complete_pr_review",
})

# 저위험 실행성 — smart 모드에서 즉시 실행
LOW_RISK_ACTIONS = frozenset({
    "feedback", "mark_notification_read", "pause", "resume",
})


# ═══════════════════════════════════════════════════════════
# 설정 로드
# ═══════════════════════════════════════════════════════════

def load_chatbot_config(agent_hub_root: str) -> dict:
    """config.yaml에서 chatbot 섹션을 읽는다. 없으면 기본값 반환."""
    config_path = os.path.join(agent_hub_root, "config.yaml")
    default = {"confirmation_mode": "smart"}

    if not os.path.isfile(config_path):
        return default

    try:
        import yaml
        with open(config_path) as f:
            config = yaml.safe_load(f) or {}
        return config.get("chatbot", default)
    except ImportError:
        # yaml 없으면 기본값
        return default
    except Exception:
        return default


# ═══════════════════════════════════════════════════════════
# 세션 관리
# ═══════════════════════════════════════════════════════════

def generate_session_id() -> str:
    """세션 ID를 생성한다. 형식: YYYYMMDD_HHMMSS_xxxx (타임스탬프 + 랜덤 4자)"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"{timestamp}_{suffix}"


def get_session_dir(agent_hub_root: str, frontend: str = "chatbot") -> str:
    """세션 저장 디렉토리 경로를 반환한다. 없으면 생성한다."""
    session_dir = os.path.join(agent_hub_root, "session_history", frontend)
    os.makedirs(session_dir, exist_ok=True)
    return session_dir


def get_session_path(agent_hub_root: str, session_id: str,
                     frontend: str = "chatbot") -> str:
    """세션 파일 경로를 반환한다."""
    session_dir = get_session_dir(agent_hub_root, frontend)
    return os.path.join(session_dir, f"{session_id}.json")


def save_session(agent_hub_root: str, session_id: str,
                 conversation_history: list, frontend: str = "chatbot"):
    """세션 이력을 파일에 저장한다."""
    session_path = get_session_path(agent_hub_root, session_id, frontend)
    data = {
        "session_id": session_id,
        "frontend": frontend,
        "created_at": None,
        "updated_at": datetime.now().isoformat(),
        "turn_count": sum(1 for e in conversation_history if e["role"] == "user"),
        "history": conversation_history,
    }
    # created_at은 기존 파일에서 유지, 없으면 현재 시각
    if os.path.isfile(session_path):
        try:
            with open(session_path) as f:
                existing = json.load(f)
            data["created_at"] = existing.get("created_at")
        except (json.JSONDecodeError, IOError):
            pass
    if data["created_at"] is None:
        data["created_at"] = data["updated_at"]

    with open(session_path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_session(agent_hub_root: str, session_id: str,
                 frontend: str = "chatbot") -> Optional[list]:
    """세션 이력을 파일에서 로드한다. 없으면 None 반환."""
    session_path = get_session_path(agent_hub_root, session_id, frontend)
    if not os.path.isfile(session_path):
        return None
    try:
        with open(session_path) as f:
            data = json.load(f)
        return data.get("history", [])
    except (json.JSONDecodeError, IOError):
        return None


def list_sessions(agent_hub_root: str, frontend: str = "chatbot") -> list:
    """세션 목록을 반환한다. 최신순 정렬."""
    session_dir = get_session_dir(agent_hub_root, frontend)
    sessions = []
    for filename in os.listdir(session_dir):
        if not filename.endswith(".json"):
            continue
        session_id = filename[:-5]  # .json 제거
        filepath = os.path.join(session_dir, filename)
        try:
            with open(filepath) as f:
                data = json.load(f)
            sessions.append({
                "session_id": session_id,
                "created_at": data.get("created_at", ""),
                "updated_at": data.get("updated_at", ""),
                "turn_count": data.get("turn_count", 0),
            })
        except (json.JSONDecodeError, IOError):
            sessions.append({
                "session_id": session_id,
                "created_at": "",
                "updated_at": "",
                "turn_count": 0,
            })
    # 최신순 정렬 (session_id가 타임스탬프 기반이라 역순 정렬로 충분)
    sessions.sort(key=lambda s: s["session_id"], reverse=True)
    return sessions


# ═══════════════════════════════════════════════════════════
# 시스템 프롬프트
# ═══════════════════════════════════════════════════════════

SYSTEM_PROMPT_TEMPLATE = """\
당신은 Agent Hub 시스템의 챗봇 인터페이스입니다.
사용자가 자연어로 요청하면, 적절한 시스템 action으로 변환합니다.

## 사용 가능한 Action 목록
{action_descriptions}

## 현재 등록된 프로젝트
{project_list}

## 현재 시스템 상태
{system_status}

## 응답 형식 (반드시 준수)

사용자의 요청을 분석하여 다음 JSON 형식으로 응답하세요.
반드시 ```json 블록 안에 작성하세요.

### action을 실행해야 할 때:
```json
{{
  "intent": "action",
  "action": "<action명>",
  "project": "<프로젝트명 또는 null>",
  "params": {{}},
  "explanation": "<사용자에게 보여줄 설명 (한국어)>"
}}
```

### 일반 대화/질문에 답할 때 (action 불필요):
```json
{{
  "intent": "conversation",
  "message": "<사용자에게 보여줄 메시지>"
}}
```

### 정보가 부족하여 추가 질문이 필요할 때:
```json
{{
  "intent": "clarification",
  "message": "<사용자에게 물어볼 질문>"
}}
```

## config_override 스키마 (submit의 선택 파라미터)

submit action에서 config_override를 사용할 때, 반드시 아래 구조를 따를 것.
임의의 키를 만들지 마세요.

```
"config_override": {{
  "human_review_policy": {{
    "review_plan": true/false,       // plan 생성 후 사용자 승인 대기 여부
    "review_replan": true/false,     // replan 시 사용자 승인 대기 여부
    "auto_approve_timeout_hours": N  // N시간 후 자동 승인
  }},
  "testing": {{
    "unit_test": {{"enabled": true/false}},
    "e2e_test": {{"enabled": true/false}},
    "integration_test": {{"enabled": true/false}}
  }},
  "limits": {{
    "max_subtask_count": N,
    "max_retry_per_subtask": N
  }}
}}
```

자주 쓰는 패턴:
- "승인 없이 바로 실행" → config_override: {{"human_review_policy": {{"review_plan": false, "review_replan": false}}}}
- "테스트 없이" → config_override: {{"testing": {{"unit_test": {{"enabled": false}}, "e2e_test": {{"enabled": false}}}}}}

## action 선택 가이드 (혼동하기 쉬운 상황)

| 사용자 표현 | 올바른 action | 잘못된 선택 |
|-------------|---------------|-------------|
| "재실행해줘", "다시 실행", "재제출" (cancelled/failed task) | **resubmit** | ~~resume~~ |
| "일시정지 해제", "다시 시작" (paused task) | **resume** | ~~resubmit~~ |
| "task 다시 돌려줘" (cancelled/failed) | **resubmit** | ~~resume~~ |
| "새 프로젝트 만들어줘" | **create_project** | ~~submit~~ |

핵심 구분:
- **resume**: 일시정지(paused)된 task를 이어서 실행. 종료된 task(cancelled/failed/completed)에는 사용 불가.
- **resubmit**: cancelled/failed task의 내용을 복사하여 새 task로 재제출. 새 task_id가 부여된다.

## 주의사항
- project가 필수인 action인데 사용자가 프로젝트를 명시하지 않았고, 프로젝트가 1개뿐이면 자동 선택
- 프로젝트가 여러 개이고 명시하지 않았으면 clarification으로 물어볼 것
- params의 키 이름은 정확히 action 정의와 동일하게 사용
- 사용자가 모호하게 말해도 최선의 action을 추론할 것
- 설명(explanation)에는 사용자의 원래 의도를 반영하여 정확하게 작성
- 이전 대화에서 task 상태를 확인한 경우, 그 상태를 바탕으로 적절한 action을 선택할 것
"""


def build_system_prompt(hub_api: HubAPI) -> str:
    """현재 시스템 상태를 반영한 system prompt를 생성한다."""
    # Action descriptions
    action_desc = get_action_descriptions()

    # 프로젝트 목록
    projects = hub_api._list_projects()
    if projects:
        project_list = "\n".join(f"- {p}" for p in projects)
    else:
        project_list = "(등록된 프로젝트 없음)"

    # 시스템 상태 (간략)
    try:
        status = hub_api.status()
        tm_str = f"Task Manager: {'실행 중 (PID ' + str(status.tm_pid) + ')' if status.tm_running else '중지됨'}"
        proj_lines = []
        for p in status.projects:
            info = f"  - {p.name}: {p.status}"
            if p.current_task_id:
                info += f" (현재 task: {p.current_task_id})"
            proj_lines.append(info)
        system_status = tm_str
        if proj_lines:
            system_status += "\n프로젝트 상태:\n" + "\n".join(proj_lines)
    except Exception:
        system_status = "(상태 조회 실패)"

    return SYSTEM_PROMPT_TEMPLATE.format(
        action_descriptions=action_desc,
        project_list=project_list,
        system_status=system_status,
    )


# ═══════════════════════════════════════════════════════════
# Claude CLI 연동
# ═══════════════════════════════════════════════════════════

def call_claude_cli(system_prompt: str, user_message: str,
                    conversation_history: list,
                    model: str = "sonnet") -> str:
    """
    claude -p를 호출하여 자연어를 해석한다.

    claude -p는 stateless이므로 대화 이력을 prompt에 직접 포함한다.
    """
    # 대화 이력을 프롬프트에 포함
    history_text = ""
    if conversation_history:
        history_lines = []
        for entry in conversation_history:
            role = entry["role"]
            content = entry["content"]
            if role == "user":
                history_lines.append(f"사용자: {content}")
            elif role == "assistant":
                history_lines.append(f"챗봇: {content}")
            elif role == "system":
                history_lines.append(f"[시스템 실행 결과: {content}]")
        history_text = "\n\n## 이전 대화\n" + "\n".join(history_lines)

    full_prompt = system_prompt + history_text + f"\n\n사용자: {user_message}"

    try:
        result = subprocess.run(
            ["claude", "-p", full_prompt, "--output-format", "text",
             "--model", model],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            return json.dumps({
                "intent": "conversation",
                "message": f"Claude 호출 오류: {result.stderr.strip()}",
            })
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return json.dumps({
            "intent": "conversation",
            "message": "Claude 응답 시간이 초과되었습니다. 다시 시도해주세요.",
        })
    except FileNotFoundError:
        return json.dumps({
            "intent": "conversation",
            "message": "claude CLI를 찾을 수 없습니다. Claude Code가 설치되어 있는지 확인하세요.",
        })


# ═══════════════════════════════════════════════════════════
# 응답 파싱
# ═══════════════════════════════════════════════════════════

def parse_claude_response(raw_output: str) -> dict:
    """
    Claude 응답에서 JSON 블록을 추출한다.

    1차: ```json ... ``` 블록 추출
    2차: 전체를 JSON으로 파싱
    3차: 파싱 실패 시 conversation으로 처리
    4차: intent 보정 — action 이름이 intent에 들어온 경우 교정
    """
    # ```json ... ``` 블록 추출
    pattern = r'```json\s*\n?(.*?)\n?\s*```'
    match = re.search(pattern, raw_output, re.DOTALL)

    parsed = None
    if match:
        try:
            parsed = json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # 전체를 JSON으로 시도
    if parsed is None:
        try:
            parsed = json.loads(raw_output)
        except json.JSONDecodeError:
            pass

    if parsed is None:
        # 파싱 실패 → 일반 대화로 처리
        return {
            "intent": "conversation",
            "message": raw_output,
        }

    # intent 보정: Claude가 intent에 action 이름을 넣는 경우 교정
    # 예: {"intent": "approve", "action": "approve", ...} → intent를 "action"으로
    intent = parsed.get("intent", "")
    if intent not in ("action", "conversation", "clarification"):
        if parsed.get("action") and parsed["action"] in ACTION_REGISTRY:
            parsed["intent"] = "action"

    return parsed


# ═══════════════════════════════════════════════════════════
# 확인(confirmation) 로직
# ═══════════════════════════════════════════════════════════

def needs_confirmation(action: str, confirmation_mode: str) -> bool:
    """
    주어진 action과 confirmation_mode에 따라 사용자 확인이 필요한지 결정한다.

    Returns:
        True면 사용자에게 확인을 요청해야 함.
    """
    # 조회성 action은 항상 즉시 실행
    if action in READ_ONLY_ACTIONS:
        return False

    if confirmation_mode == "never_confirm":
        return False

    if confirmation_mode == "always_confirm":
        return True

    # smart 모드 (기본값)
    return action in HIGH_RISK_ACTIONS


def format_confirmation_prompt(parsed: dict) -> str:
    """실행 전 확인 메시지를 생성한다."""
    action = parsed.get("action", "?")
    project = parsed.get("project")
    params = parsed.get("params", {})
    explanation = parsed.get("explanation", "")

    lines = []
    lines.append(f"\n{DIM}{'─' * 50}{NC}")
    lines.append(f"  {BOLD}실행할 작업:{NC} {action}")
    if project:
        lines.append(f"  {BOLD}프로젝트:{NC}    {project}")
    for k, v in params.items():
        display_v = str(v)
        if len(display_v) > 80:
            display_v = display_v[:77] + "..."
        lines.append(f"  {BOLD}{k}:{NC} {display_v}")
    if explanation:
        lines.append(f"\n  {explanation}")
    lines.append(f"{DIM}{'─' * 50}{NC}")

    return "\n".join(lines)


def ask_user_confirmation() -> bool:
    """사용자에게 y/n 확인을 요청한다."""
    try:
        answer = input(f"\n{YELLOW}이렇게 실행할까요?{NC} (Y/n): ").strip().lower()
        return answer in ("", "y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


# ═══════════════════════════════════════════════════════════
# 결과 포맷팅
# ═══════════════════════════════════════════════════════════

def format_response_for_display(response: Response, action: str) -> str:
    """protocol.Response를 터미널 출력용 문자열로 변환한다."""
    if not response.success:
        error = response.error or {}
        return f"{RED}[오류]{NC} {error.get('message', response.message)}"

    data = response.data
    lines = [f"{GREEN}[완료]{NC} {response.message}"]

    # action별 상세 포맷팅
    if action == "list" and isinstance(data, list):
        if data:
            lines.append(f"\n{'ID':<8} {'프로젝트':<20} {'상태':<18} {'제목'}")
            lines.append("─" * 70)
            for t in data:
                if hasattr(t, "task_id"):
                    lines.append(f"{t.task_id:<8} {t.project:<20} {t.status:<18} {t.title}")
                elif isinstance(t, dict):
                    lines.append(
                        f"{t.get('task_id', ''):<8} {t.get('project', ''):<20} "
                        f"{t.get('status', ''):<18} {t.get('title', '')}"
                    )

    elif action == "get_task" and isinstance(data, dict):
        # 기본 정보
        for key in ["task_id", "title", "status", "branch", "pr_url"]:
            if data.get(key):
                lines.append(f"  {key}: {data[key]}")

        # 진행 상황 (in_progress 등 실행 중 상태)
        status = data.get("status", "")
        counters = data.get("counters", {})
        completed = data.get("completed_subtasks", [])
        current = data.get("current_subtask")

        if current or completed:
            lines.append("")
            # 완료된 subtask 수
            completed_count = len(completed)
            retry = counters.get("current_subtask_retry", 0)

            if current:
                # "subtask 01의 2번째 시도" 형태
                # current_subtask: "00101-2" → subtask 번호 추출
                sub_num = current.split("-")[-1] if "-" in current else current
                progress = f"  {CYAN}[진행]{NC} subtask {sub_num}"
                if retry > 0:
                    progress += f"의 {retry + 1}번째 시도"
                lines.append(progress)

            if completed:
                lines.append(f"  완료된 subtask: {', '.join(completed)}")

            invocations = counters.get("total_agent_invocations", 0)
            replan = counters.get("replan_count", 0)
            if invocations:
                extra = f"  agent 호출: {invocations}회"
                if replan:
                    extra += f", replan: {replan}회"
                lines.append(extra)

        # waiting_for_human_plan_confirm이면 human_interaction 상세 표시
        hi = data.get("human_interaction")
        if status == "waiting_for_human_plan_confirm" and hi and not hi.get("response"):
            lines.append(f"\n  {YELLOW}[승인 대기]{NC}")
            lines.append(f"  유형:    {hi.get('type', '?')}")
            lines.append(f"  메시지:  {hi.get('message', '')}")
            if hi.get("options"):
                lines.append(f"  옵션:    {', '.join(hi['options'])}")
            if hi.get("payload_path"):
                lines.append(f"  상세:    {hi['payload_path']}")
            lines.append(f"\n  {DIM}응답하려면: \"00XXX 승인해\" 또는 \"00XXX 거부해 (사유)\" 라고 말씀하세요{NC}")

    elif action == "pending" and isinstance(data, list):
        for item in data:
            if hasattr(item, "task_id"):
                lines.append(
                    f"\n  {YELLOW}[대기]{NC} {item.project}/{item.task_id}"
                    f" - {item.interaction_type}"
                )
                lines.append(f"  메시지: {item.message}")
                if item.options:
                    lines.append(f"  옵션:   {', '.join(item.options)}")
                if item.payload_path:
                    lines.append(f"  상세:   {item.payload_path}")
        if data:
            lines.append(f"\n  {DIM}응답하려면: \"00XXX 승인해\" 또는 \"00XXX 거부해 (사유)\" 라고 말씀하세요{NC}")

    elif action == "status":
        if hasattr(data, "tm_running"):
            tm = f"실행 중 (PID {data.tm_pid})" if data.tm_running else "중지됨"
            lines.append(f"  Task Manager: {tm}")
            for p in data.projects:
                task_info = f" (task: {p.current_task_id})" if p.current_task_id else ""
                lines.append(f"  {p.name}: {p.status}{task_info}")

    elif action == "submit" and hasattr(data, "task_id"):
        lines.append(f"  task_id: {BOLD}{data.task_id}{NC}")
        lines.append(f"  project: {data.project}")

    elif action == "get_plan" and isinstance(data, dict):
        # plan 전체 정보 표시
        branch = data.get("branch_name", "")
        strategy = data.get("strategy_note", "")
        subtasks = data.get("subtasks", [])

        if branch:
            lines.append(f"  branch: {branch}")
        if strategy:
            lines.append(f"  전략: {strategy}")

        lines.append(f"\n  {BOLD}subtask ({len(subtasks)}개){NC}")
        lines.append("  " + "─" * 60)

        for subtask in subtasks:
            subtask_id = subtask.get("subtask_id", "?")
            title = subtask.get("title", "")
            responsibility = subtask.get("primary_responsibility", "")
            depends = subtask.get("depends_on", [])
            guidance = subtask.get("guidance", [])

            lines.append(f"\n  {CYAN}[{subtask_id}]{NC} {title}")
            if responsibility:
                lines.append(f"    담당: {responsibility}")
            if depends:
                lines.append(f"    의존: {', '.join(depends)}")
            if guidance:
                for guide_item in guidance:
                    lines.append(f"    • {guide_item}")

    elif action == "resubmit" and hasattr(data, "task_id"):
        lines.append(f"  새 task_id: {BOLD}{data.task_id}{NC}")
        lines.append(f"  project: {data.project}")

    elif action == "notifications" and isinstance(data, list):
        for n in data:
            event = n.get("event_type", "?")
            msg = n.get("message", "")
            read_mark = "" if n.get("read") else f" {YELLOW}(new){NC}"
            lines.append(f"  [{event}] {msg}{read_mark}")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
# ChatBot 클래스
# ═══════════════════════════════════════════════════════════

class ChatBot:
    """
    Agent Hub 대화형 챗봇.

    자연어 입력 -> Claude 해석 -> 확인 -> dispatch -> 결과 표시
    """

    MAX_HISTORY_TURNS = 100
    COMPRESS_INTERVAL = 100  # 100턴마다 compress (autocompact이 안전망)

    def __init__(self, agent_hub_root: str, session_id: Optional[str] = None,
                 frontend: str = "chatbot"):
        self.root = os.path.abspath(agent_hub_root)
        self.frontend = frontend
        self.hub_api = HubAPI(self.root)
        self.chatbot_config = load_chatbot_config(self.root)
        self.confirmation_mode = self.chatbot_config.get("confirmation_mode", "smart")
        self.model = self.chatbot_config.get("model", "sonnet")
        self.conversation_history: list = []
        self._system_prompt: Optional[str] = None
        self._prompt_refresh_counter = 0

        # 세션 관리
        if session_id:
            # 기존 세션 재개
            self.session_id = session_id
            loaded = load_session(self.root, session_id, frontend)
            if loaded is not None:
                self.conversation_history = loaded
        else:
            # 새 세션 생성
            self.session_id = generate_session_id()

    def _get_system_prompt(self) -> str:
        """시스템 프롬프트를 가져온다. 매 5턴마다 재생성."""
        if self._system_prompt is None or self._prompt_refresh_counter >= 5:
            self._system_prompt = build_system_prompt(self.hub_api)
            self._prompt_refresh_counter = 0
        return self._system_prompt

    def _add_history(self, role: str, content: str):
        """대화 이력에 추가하고 파일에 저장한다. 오래된 항목은 잘라낸다."""
        self.conversation_history.append({"role": role, "content": content})
        # user + assistant + system 각각이므로 턴 수 * 3
        max_entries = self.MAX_HISTORY_TURNS * 3
        if len(self.conversation_history) > max_entries:
            trim_count = len(self.conversation_history) - max_entries
            self.conversation_history = self.conversation_history[trim_count:]
        # 매 턴마다 세션 파일에 저장
        save_session(self.root, self.session_id,
                     self.conversation_history, self.frontend)

    def process_input(self, user_input: str) -> str:
        """사용자 입력을 처리하고 결과 문자열을 반환한다."""
        self._add_history("user", user_input)
        self._prompt_refresh_counter += 1

        # 1. Claude에게 해석 요청
        system_prompt = self._get_system_prompt()
        raw_response = call_claude_cli(
            system_prompt, user_input,
            self.conversation_history[:-1],  # 현재 입력 제외
            model=self.model,
        )

        # 2. 응답 파싱
        parsed = parse_claude_response(raw_response)
        intent = parsed.get("intent", "conversation")

        # 3. intent별 처리
        if intent == "conversation":
            message = parsed.get("message", raw_response)
            self._add_history("assistant", message)
            return message

        if intent == "clarification":
            message = parsed.get("message", "추가 정보가 필요합니다.")
            self._add_history("assistant", message)
            return message

        if intent == "action":
            return self._handle_action(parsed)

        # 알 수 없는 intent
        message = parsed.get("message", raw_response)
        self._add_history("assistant", message)
        return message

    def _handle_action(self, parsed: dict) -> str:
        """action intent를 처리한다. 확인 정책에 따라 확인 후 dispatch."""
        action = parsed.get("action", "")
        project = parsed.get("project")
        params = parsed.get("params", {})
        explanation = parsed.get("explanation", "")

        # action 유효성 검증
        if action not in ACTION_REGISTRY:
            msg = f"알 수 없는 action입니다: {action}"
            self._add_history("assistant", msg)
            return msg

        # 확인 필요 여부 판단
        if needs_confirmation(action, self.confirmation_mode):
            prompt_text = format_confirmation_prompt(parsed)
            print(prompt_text)

            if not ask_user_confirmation():
                msg = "취소되었습니다."
                self._add_history("assistant", msg)
                return msg

        # Request 생성 및 dispatch
        request = Request(
            action=action,
            project=project,
            params=params,
            source="chatbot",
        )

        response = dispatch(self.hub_api, request)

        # 결과 포맷팅
        result_text = format_response_for_display(response, action)

        # 이력에 추가
        if explanation:
            self._add_history("assistant", explanation)
        self._add_history("system", response.message)

        return result_text

    def run_interactive(self):
        """터미널 대화형 루프."""
        resumed = bool(self.conversation_history)
        print(f"{CYAN}{'=' * 55}{NC}")
        print(f"{CYAN}  Agent Hub Chatbot{NC}")
        print(f"{CYAN}  자연어로 시스템을 제어합니다.{NC}")
        print(f"{CYAN}  종료: Ctrl+C 또는 '종료' 입력{NC}")
        print(f"{CYAN}  확인 모드: {self.confirmation_mode}{NC}")
        print(f"{CYAN}  세션: {self.session_id}{NC}")
        if resumed:
            turn_count = sum(1 for e in self.conversation_history if e["role"] == "user")
            print(f"{CYAN}  (이전 세션 재개 — {turn_count}턴 이력 로드됨){NC}")
        print(f"{CYAN}{'=' * 55}{NC}")
        print()

        while True:
            try:
                user_input = input(f"{BOLD}> {NC}").strip()
            except (EOFError, KeyboardInterrupt):
                print(f"\n{CYAN}세션을 종료합니다.{NC}")
                break

            if not user_input:
                continue

            if user_input in ("종료", "exit", "quit", "/quit", "/exit"):
                print(f"{CYAN}세션을 종료합니다.{NC}")
                break

            result = self.process_input(user_input)
            print(f"\n{result}\n")


# ═══════════════════════════════════════════════════════════
# 진입점
# ═══════════════════════════════════════════════════════════

def main():
    """chatbot.py 직접 실행 시 진입점."""
    parser = argparse.ArgumentParser(
        description="Agent Hub Chatbot - 자연어 시스템 제어",
    )
    parser.add_argument(
        "--root",
        default=os.environ.get(
            "AGENT_HUB_ROOT",
            str(Path(__file__).resolve().parent.parent),
        ),
        help="Agent Hub 루트 디렉토리",
    )
    parser.add_argument(
        "--confirmation-mode",
        choices=["always_confirm", "never_confirm", "smart"],
        help="확인 모드 override (기본: config.yaml의 chatbot.confirmation_mode)",
    )
    parser.add_argument(
        "--session",
        help="기존 세션 ID로 재개 (예: 20260403_143052_a3f1)",
    )
    parser.add_argument(
        "--list-sessions",
        action="store_true",
        help="저장된 세션 목록 출력",
    )
    args = parser.parse_args()

    # 세션 목록 출력
    if args.list_sessions:
        sessions = list_sessions(args.root)
        if not sessions:
            print("저장된 세션이 없습니다.")
        else:
            print(f"{'세션 ID':<28} {'턴':<6} {'생성일시':<22} {'최근 업데이트'}")
            print("─" * 85)
            for s in sessions:
                print(
                    f"{s['session_id']:<28} {s['turn_count']:<6} "
                    f"{s['created_at'][:19]:<22} {s['updated_at'][:19]}"
                )
        return

    bot = ChatBot(args.root, session_id=args.session)

    # CLI override가 있으면 적용
    if args.confirmation_mode:
        bot.confirmation_mode = args.confirmation_mode

    bot.run_interactive()


if __name__ == "__main__":
    main()
