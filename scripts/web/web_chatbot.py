"""
Web Chat 엔진 — Transport-agnostic ChatProcessor.

Web Console, 향후 Slack/Telegram에서 공용으로 사용하는 채팅 처리 엔진.
claude -p를 Popen으로 호출하여 cancel+merge 패턴을 지원한다.

핵심 흐름:
  사용자 메시지 → submit_message() → 백그라운드 스레드에서 claude -p 실행
  → 응답을 on_message 콜백으로 전달 (SSE, webhook 등)
  → 처리 중 새 메시지 도착 시 기존 프로세스 kill → 합쳐서 재실행
"""

import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
from datetime import datetime, timezone
from typing import Callable, Optional

# scripts/ 디렉토리를 path에 추가 (chatbot.py import 위해)
_scripts_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

from hub_api.core import HubAPI
from hub_api.protocol import (
    Request,
    dispatch,
)
from chatbot import (
    build_system_prompt,
    parse_claude_response,
    needs_confirmation,
    generate_session_id,
    save_session,
    load_session,
    list_sessions,
    load_chatbot_config,
    HIGH_RISK_ACTIONS,
    READ_ONLY_ACTIONS,
    LOW_RISK_ACTIONS,
)

logger = logging.getLogger(__name__)

# 확인 응답 판별용
_AFFIRMATIVE = frozenset({"yes", "y", "확인", "네", "ㅇ", "승인", "실행"})
_NEGATIVE = frozenset({"no", "n", "취소", "아니", "ㄴ", "거부", "중단"})

# ANSI 코드 제거용
_ANSI_RE = re.compile(r'\033\[[0-9;]*m')


def _strip_ansi(text: str) -> str:
    """ANSI escape 코드를 제거한다."""
    return _ANSI_RE.sub('', text)


def _format_confirmation_plain(parsed: dict, agent_hub_root: Optional[str] = None) -> str:
    """
    확인 메시지를 ANSI 없이 plain text로 생성한다.

    submit action이고 agent_hub_root가 주어지면, task.config_override 대신
    4계층 merge된 effective config 전체 트리를 (수정됨)/(기본값) 태그와 함께 표시한다.
    """
    action = parsed.get("action", "?")
    project = parsed.get("project")
    params = parsed.get("params", {})
    explanation = parsed.get("explanation", "")

    # submit + agent_hub_root가 있으면 config 트리 사전 렌더
    config_tree_text = None
    if action == "submit" and agent_hub_root and project:
        from hub_api.config_preview import format_config_override_for_confirmation
        config_tree_text = format_config_override_for_confirmation(
            agent_hub_root, project, params.get("config_override", {})
        )

    lines = []
    lines.append(f"실행할 작업: {action}")
    if project:
        lines.append(f"프로젝트: {project}")
    for k, v in params.items():
        if config_tree_text is not None and k == "config_override":
            lines.append(config_tree_text)
            continue
        if isinstance(v, (dict, list)):
            pretty = json.dumps(v, indent=2, ensure_ascii=False)
            indented = pretty.replace("\n", "\n    ")
            lines.append(f"{k}:\n    {indented}")
        else:
            display_v = str(v)
            if len(display_v) > 80:
                display_v = display_v[:77] + "..."
            lines.append(f"{k}: {display_v}")

    # submit인데 config_override가 params에 없던 경우에도 전체 트리 표시
    if config_tree_text is not None and "config_override" not in params:
        lines.append(config_tree_text)

    if explanation:
        lines.append(f"\n{explanation}")

    return "\n".join(lines)


def _format_response_plain(response, action: str) -> str:
    """protocol.Response를 plain text로 변환한다 (ANSI 없음)."""
    if not response.success:
        error = response.error or {}
        return f"[오류] {error.get('message', response.message)}"

    data = response.data
    lines = [f"[완료] {response.message}"]

    # action별 상세 포맷팅 (간략 버전)
    if action == "list" and isinstance(data, list):
        if data:
            lines.append(f"\n{'ID':<8} {'프로젝트':<20} {'상태':<18} {'제목'}")
            lines.append("─" * 70)
            for t in data:
                if isinstance(t, dict):
                    lines.append(
                        f"{t.get('task_id', ''):<8} {t.get('project', ''):<20} "
                        f"{t.get('status', ''):<18} {t.get('title', '')}"
                    )
                elif hasattr(t, "task_id"):
                    lines.append(f"{t.task_id:<8} {t.project:<20} {t.status:<18} {t.title}")

    elif action == "get_task" and isinstance(data, dict):
        for key in ["task_id", "title", "status", "branch", "pr_url"]:
            if data.get(key):
                lines.append(f"  {key}: {data[key]}")

    elif action == "pending" and isinstance(data, list):
        for item in data:
            if hasattr(item, "task_id"):
                lines.append(f"\n  [대기] {item.project}/{item.task_id} - {item.interaction_type}")
                lines.append(f"  메시지: {item.message}")
            elif isinstance(item, dict):
                lines.append(f"\n  [대기] {item.get('project')}/{item.get('task_id')} - {item.get('interaction_type')}")
                lines.append(f"  메시지: {item.get('message')}")

    elif action == "status":
        if hasattr(data, "tm_running"):
            tm = f"실행 중 (PID {data.tm_pid})" if data.tm_running else "중지됨"
            lines.append(f"  Task Manager: {tm}")
            for p in data.projects:
                task_info = f" (task: {p.current_task_id})" if p.current_task_id else ""
                lines.append(f"  {p.name}: {p.status}{task_info}")

    elif action == "submit" and hasattr(data, "task_id"):
        lines.append(f"  task_id: {data.task_id}")
        lines.append(f"  project: {data.project}")

    elif action == "get_plan" and isinstance(data, dict):
        subtasks = data.get("subtasks", [])
        branch = data.get("branch_name", "")
        if branch:
            lines.append(f"  branch: {branch}")
        lines.append(f"\n  subtask ({len(subtasks)}개)")
        for st in subtasks:
            lines.append(f"  [{st.get('subtask_id', '?')}] {st.get('title', '')}")
            if st.get("primary_responsibility"):
                lines.append(f"    담당: {st['primary_responsibility']}")

    elif action == "resubmit" and hasattr(data, "task_id"):
        lines.append(f"  새 task_id: {data.task_id}")
        lines.append(f"  project: {data.project}")

    elif action == "notifications" and isinstance(data, list):
        for n in data:
            event = n.get("event_type", "?")
            msg = n.get("message", "")
            read_mark = "" if n.get("read") else " (new)"
            lines.append(f"  [{event}] {msg}{read_mark}")

    return "\n".join(lines)


def _format_system_event(event: dict) -> str:
    """notification SSE 이벤트를 chat용 텍스트로 변환한다."""
    event_type = event.get("event_type", "")
    project = event.get("project", "")
    task_id = event.get("task_id", "")
    message = event.get("message", "")

    # 이벤트 타입별 이모지 + 제목
    type_labels = {
        "task_completed": "✅ Task 완료",
        "task_failed": "❌ Task 실패",
        "pr_created": "🔗 PR 생성됨",
        "pr_merged": "🟢 PR 머지 완료",
        "plan_review_requested": "📋 Plan 승인 요청",
        "replan_review_requested": "📋 Re-plan 승인 요청",
        "escalation": "🚨 에스컬레이션",
    }
    label = type_labels.get(event_type, f"📢 {event_type}")

    parts = [f"{label}"]
    if project and task_id:
        parts.append(f"{project} #{task_id}")
    elif project:
        parts.append(project)

    if message:
        parts.append(message)

    return " — ".join(parts)


# ═══════════════════════════════════════════════════════════
# ChatProcessor — 핵심 채팅 엔진
# ═══════════════════════════════════════════════════════════

class ChatProcessor:
    """
    Transport-agnostic 채팅 엔진.

    Web, Slack, Telegram 등 어떤 프론트엔드에서든 동일하게 사용.
    on_message 콜백으로 응답을 전달한다.
    """

    MAX_HISTORY_TURNS = 100
    PROMPT_REFRESH_INTERVAL = 5  # 5턴마다 시스템 프롬프트 갱신

    def __init__(self, agent_hub_root: str, session_id: str,
                 on_message: Callable[[dict], None],
                 frontend: str = "web"):
        """
        ChatProcessor를 초기화한다.

        Args:
            agent_hub_root: agent-hub 루트 디렉토리
            session_id: 세션 ID
            on_message: 메시지 전달 콜백 (dict → None)
            frontend: 프론트엔드 식별자 (세션 저장 경로 결정)
        """
        self._root = agent_hub_root
        self._session_id = session_id
        self._frontend = frontend
        self._on_message = on_message
        self._hub_api = HubAPI(agent_hub_root)

        # claude -p 서브프로세스 관리
        self._process: Optional[subprocess.Popen] = None
        self._pending_messages: list[str] = []
        self._state: str = "idle"  # idle | processing | awaiting_confirmation
        self._pending_action: Optional[dict] = None
        self._lock = threading.Lock()

        # 대화 이력
        self._conversation_history: list = []

        # 설정
        chatbot_config = load_chatbot_config(agent_hub_root)
        self._confirmation_mode = chatbot_config.get("confirmation_mode", "smart")
        self._model = chatbot_config.get("model", "sonnet")

        # 시스템 프롬프트 캐시
        self._system_prompt: Optional[str] = None
        self._prompt_refresh_counter = 0

        # 세션 로드
        history = load_session(agent_hub_root, session_id, frontend)
        if history:
            self._conversation_history = history

    @property
    def session_id(self) -> str:
        """세션 ID를 반환한다."""
        return self._session_id

    @property
    def conversation_history(self) -> list:
        """대화 이력을 반환한다."""
        return list(self._conversation_history)

    def submit_message(self, user_message: str) -> None:
        """
        사용자 메시지를 제출한다.

        상태에 따라 분기:
        - idle: 백그라운드 처리 시작
        - processing: 기존 프로세스 kill, 메시지를 pending에 추가
        - awaiting_confirmation: 확인 응답으로 처리
        """
        with self._lock:
            if self._state == "processing":
                # 처리 중 — 메시지를 pending에 추가하고 기존 프로세스 kill
                self._pending_messages.append(user_message)
                if self._process and self._process.poll() is None:
                    try:
                        self._process.kill()
                    except OSError:
                        pass
                return

            if self._state == "awaiting_confirmation":
                # 확인 응답 처리
                self._state = "processing"
                thread = threading.Thread(
                    target=self._handle_confirmation_response,
                    args=(user_message,),
                    daemon=True,
                )
                thread.start()
                return

            # idle — 처리 시작
            self._state = "processing"

        thread = threading.Thread(
            target=self._process_pipeline,
            args=([user_message],),
            daemon=True,
        )
        thread.start()

    def inject_system_event(self, event: dict) -> None:
        """
        시스템 이벤트(notification)를 chat 메시지로 주입한다.

        활성 세션에 자동으로 시스템 메시지를 추가한다.
        """
        content = _format_system_event(event)
        self._add_history("system", content)
        self._emit_message("system", content)

    # ─── 내부 메서드 ───

    def _process_pipeline(self, messages: list[str]) -> None:
        """
        백그라운드 스레드에서 메시지를 처리한다.

        cancel+merge 패턴: 처리 중 kill된 경우 pending 메시지를 합쳐서 재실행.
        """
        while True:
            # 메시지 합치기
            if len(messages) > 1:
                merged = "\n".join(messages)
            else:
                merged = messages[0]

            # 이력에 사용자 메시지 추가
            self._add_history("user", merged)

            # typing 표시
            self._emit_typing(True)

            # claude -p 호출
            stdout, was_killed = self._call_claude_popen(merged)

            if was_killed:
                # kill됨 — pending 메시지 확인
                with self._lock:
                    if self._pending_messages:
                        # 기존 메시지 + pending을 합쳐서 재실행
                        # 이력에서 방금 추가한 user 메시지 제거 (재합성할 것이므로)
                        if self._conversation_history and self._conversation_history[-1]["role"] == "user":
                            self._conversation_history.pop()
                        new_messages = [merged] + self._pending_messages
                        self._pending_messages.clear()
                    else:
                        # pending 없이 kill됨 (비정상) — idle로 복귀
                        self._state = "idle"
                        self._emit_typing(False)
                        return

                messages = new_messages
                continue  # loop 재시작

            # 정상 완료 — 응답 처리
            self._emit_typing(False)

            if not stdout:
                self._add_history("assistant", "응답을 받지 못했습니다. 다시 시도해주세요.")
                self._emit_message("assistant", "응답을 받지 못했습니다. 다시 시도해주세요.")
                with self._lock:
                    self._state = "idle"
                return

            parsed = parse_claude_response(stdout)
            intent = parsed.get("intent", "conversation")

            if intent in ("conversation", "clarification"):
                msg = parsed.get("message", stdout)
                self._add_history("assistant", msg)
                self._emit_message("assistant", msg)
                with self._lock:
                    self._state = "idle"

            elif intent == "action":
                action = parsed.get("action", "")
                if needs_confirmation(action, self._confirmation_mode):
                    # 확인 필요 — plain text 한 건으로 확인 요청
                    explanation = parsed.get("explanation", "")
                    confirmation_text = _format_confirmation_plain(parsed, self._root)
                    parts = []
                    if explanation:
                        parts.append(explanation)
                    parts.append(confirmation_text)
                    parts.append("\"확인\" 또는 \"취소\"로 답해주세요.")
                    prompt_text = "\n\n".join(parts)
                    self._add_history("assistant", prompt_text)
                    self._emit_message("assistant", prompt_text)

                    with self._lock:
                        self._state = "awaiting_confirmation"
                        self._pending_action = parsed
                else:
                    # 확인 불필요 — 즉시 실행
                    explanation = parsed.get("explanation", "")
                    if explanation:
                        self._add_history("assistant", explanation)
                        self._emit_message("assistant", explanation)

                    self._execute_action(parsed)
                    with self._lock:
                        self._state = "idle"
            else:
                msg = parsed.get("message", stdout)
                self._add_history("assistant", msg)
                self._emit_message("assistant", msg)
                with self._lock:
                    self._state = "idle"

            return

    def _call_claude_popen(self, user_message: str) -> tuple[str, bool]:
        """
        claude -p를 Popen으로 호출한다.

        Returns:
            (stdout 텍스트, kill로 종료되었는지 여부) 튜플
        """
        system_prompt = self._get_system_prompt()

        # 대화 이력을 프롬프트에 포함 (현재 메시지 제외 — 이미 prompt 끝에 붙음)
        history_text = ""
        # 현재 메시지를 제외한 이력 (마지막이 현재 user 메시지)
        history_for_prompt = self._conversation_history[:-1] if self._conversation_history else []
        if history_for_prompt:
            history_lines = []
            for entry in history_for_prompt:
                role = entry["role"]
                content = entry["content"]
                if role == "user":
                    history_lines.append(f"사용자: {content}")
                elif role == "assistant":
                    history_lines.append(f"챗봇: {content}")
                elif role == "system":
                    history_lines.append(f"[시스템: {content}]")
            history_text = "\n\n## 이전 대화\n" + "\n".join(history_lines)

        full_prompt = system_prompt + history_text + f"\n\n사용자: {user_message}"

        try:
            self._process = subprocess.Popen(
                ["claude", "-p", full_prompt, "--output-format", "text",
                 "--model", self._model],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            stdout, stderr = self._process.communicate(timeout=600)
            returncode = self._process.returncode
            self._process = None

            if returncode < 0:
                # 시그널로 종료됨 (kill)
                return ("", True)

            if returncode != 0:
                logger.warning("claude -p 비정상 종료 (code=%d): %s", returncode, stderr.strip())
                return (json.dumps({
                    "intent": "conversation",
                    "message": f"Claude 호출 오류: {stderr.strip()}"
                }), False)

            return (stdout.strip(), False)

        except subprocess.TimeoutExpired:
            if self._process:
                self._process.kill()
                self._process = None
            return (json.dumps({
                "intent": "conversation",
                "message": "Claude 응답 시간이 초과되었습니다 (10분). 다시 시도해주세요."
            }), False)
        except FileNotFoundError:
            self._process = None
            return (json.dumps({
                "intent": "conversation",
                "message": "claude CLI를 찾을 수 없습니다."
            }), False)
        except Exception as exc:
            self._process = None
            logger.error("claude -p 호출 실패: %s", exc)
            return (json.dumps({
                "intent": "conversation",
                "message": f"오류 발생: {exc}"
            }), False)

    def _handle_confirmation_response(self, user_input: str) -> None:
        """확인 응답을 처리한다."""
        self._add_history("user", user_input)
        normalized = user_input.strip().lower()

        if normalized in _AFFIRMATIVE:
            # 승인 — action 실행
            self._emit_message("assistant", "실행합니다.")
            self._add_history("assistant", "실행합니다.")
            self._execute_action(self._pending_action)
            with self._lock:
                self._pending_action = None
                self._state = "idle"

        elif normalized in _NEGATIVE:
            # 거부 — 취소
            self._add_history("assistant", "취소되었습니다.")
            self._emit_message("assistant", "취소되었습니다.")
            with self._lock:
                self._pending_action = None
                self._state = "idle"

        else:
            # 불분명 — 재질문
            msg = "\"확인\" 또는 \"취소\"로 답해주세요."
            self._add_history("assistant", msg)
            self._emit_message("assistant", msg)
            with self._lock:
                self._state = "awaiting_confirmation"

    def _execute_action(self, parsed: dict) -> None:
        """action을 실행하고 결과를 전달한다."""
        action = parsed.get("action", "")
        project = parsed.get("project")
        params = parsed.get("params", {})

        try:
            request = Request(
                action=action,
                project=project,
                params=params,
                source="web_chat",
            )
            response = dispatch(self._hub_api, request)
            result_text = _format_response_plain(response, action)
        except Exception as exc:
            result_text = f"[오류] 실행 실패: {exc}"

        self._add_history("system", result_text)
        self._emit_message("system", result_text)

    def _get_system_prompt(self) -> str:
        """시스템 프롬프트를 반환한다. 일정 주기로 갱신."""
        if self._system_prompt is None or self._prompt_refresh_counter >= self.PROMPT_REFRESH_INTERVAL:
            self._system_prompt = build_system_prompt(self._hub_api)
            self._prompt_refresh_counter = 0
        return self._system_prompt

    def _add_history(self, role: str, content: str) -> None:
        """대화 이력에 추가하고 세션을 저장한다."""
        self._conversation_history.append({"role": role, "content": content})

        # 이력 크기 제한
        max_entries = self.MAX_HISTORY_TURNS * 3
        if len(self._conversation_history) > max_entries:
            self._conversation_history = self._conversation_history[-max_entries:]

        # user 메시지일 때 prompt refresh 카운터 증가
        if role == "user":
            self._prompt_refresh_counter += 1

        # 세션 저장
        save_session(self._root, self._session_id,
                     self._conversation_history, self._frontend)

    def _emit_message(self, role: str, content: str) -> None:
        """on_message 콜백으로 chat 메시지를 전달한다."""
        self._on_message({
            "type": "chat_message",
            "session_id": self._session_id,
            "role": role,
            "content": content,
            "confirmation": False,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    def _emit_confirmation(self, parsed: dict, confirmation_text: str) -> None:
        """on_message 콜백으로 확인 카드를 전달한다."""
        self._on_message({
            "type": "chat_message",
            "session_id": self._session_id,
            "role": "assistant",
            "content": confirmation_text,
            "confirmation": True,
            "action_details": {
                "action": parsed.get("action"),
                "project": parsed.get("project"),
                "params": parsed.get("params", {}),
                "explanation": parsed.get("explanation", ""),
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    def _emit_typing(self, active: bool) -> None:
        """on_message 콜백으로 typing 상태를 전달한다."""
        self._on_message({
            "type": "chat_typing",
            "session_id": self._session_id,
            "active": active,
        })


# ═══════════════════════════════════════════════════════════
# 세션 레지스트리 — 활성 ChatProcessor 관리
# ═══════════════════════════════════════════════════════════

_active_sessions: dict[str, ChatProcessor] = {}
_sessions_lock = threading.Lock()


def get_or_create_session(agent_hub_root: str,
                          session_id: Optional[str],
                          on_message: Callable[[dict], None],
                          frontend: str = "web") -> ChatProcessor:
    """
    세션을 가져오거나 새로 생성한다.

    Args:
        agent_hub_root: agent-hub 루트
        session_id: 기존 세션 ID (None이면 새로 생성)
        on_message: 메시지 전달 콜백
        frontend: 프론트엔드 식별자

    Returns:
        ChatProcessor 인스턴스
    """
    with _sessions_lock:
        if session_id and session_id in _active_sessions:
            return _active_sessions[session_id]

        if not session_id:
            session_id = generate_session_id()

        processor = ChatProcessor(
            agent_hub_root=agent_hub_root,
            session_id=session_id,
            on_message=on_message,
            frontend=frontend,
        )
        _active_sessions[session_id] = processor
        return processor


def remove_session(session_id: str) -> None:
    """활성 세션을 제거한다."""
    with _sessions_lock:
        _active_sessions.pop(session_id, None)


def broadcast_system_event(event: dict) -> None:
    """모든 활성 세션에 시스템 이벤트를 주입한다."""
    with _sessions_lock:
        sessions = list(_active_sessions.values())

    for processor in sessions:
        try:
            processor.inject_system_event(event)
        except Exception as exc:
            logger.warning("시스템 이벤트 주입 실패 (session=%s): %s",
                           processor.session_id, exc)
