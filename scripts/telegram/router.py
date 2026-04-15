"""Inbound Telegram update 라우팅.

Bot API에서 받은 update dict를 파싱해 **무엇을 해야 하는지**(RoutingDecision)를
정한다. 프로젝트 해석(thread_id → project_name)이나 실제 dispatch는 bridge가
수행한다. 이 모듈은 pure 함수 위주로 유지하여 테스트가 쉽게 만든다.

지원하는 update:
- message.text: 슬래시 명령 또는 자연어
- message.photo / message.document: 현 Phase에서는 미지원 → 안내 후 drop
- callback_query: inline keyboard 클릭

Whitelist 규칙:
- allowed_user_ids에 포함되지 않은 user는 모두 drop (조용히 무시하되 1회성 안내 선택).
- chat_id는 hub_chat_id와 일치해야 한다. 단, bind 이전 (`hub_chat_id == 0`)일 때는
  `/bind_hub <secret>` 명령에 한해 chat_id 검증을 skip (user_id만 검증).
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import Optional

# 지원하는 슬래시 명령 집합. 이외의 `/xxx`는 "알 수 없는 명령" 안내.
SUPPORTED_SLASH_COMMANDS = frozenset({
    "status", "list", "pending", "cancel", "help",
    "new_session", "bind_hub",
})


@dataclass
class RoutingDecision:
    """라우팅 결과. bridge가 이 값을 보고 다음 행동을 결정한다.

    kind:
      - "ignore"                 : 완전히 drop (whitelist 실패 등). reply 없음
      - "reply"                  : 단일 텍스트로 즉시 회신 (권한 없음, 미지원 등)
      - "bind_hub"               : /bind_hub 처리 요청 (chat_id 저장 + secret 소비)
      - "slash_command"          : 지원하는 슬래시 명령
      - "natural_message"        : 자연어 → ChatProcessor로 전달
      - "callback_query"         : inline keyboard 클릭 결과 dispatch
    """
    kind: str
    chat_id: Optional[int] = None
    thread_id: Optional[int] = None
    user_id: Optional[int] = None
    user_display: Optional[str] = None
    message_id: Optional[int] = None

    # slash_command 전용
    command: Optional[str] = None
    args: list[str] = field(default_factory=list)

    # natural_message / reply 전용
    text: Optional[str] = None

    # callback_query 전용
    callback_query_id: Optional[str] = None
    callback_action: Optional[str] = None       # approve / reject_modify / reject_cancel / view ...
    callback_project: Optional[str] = None
    callback_task_id: Optional[str] = None

    # bind_hub 전용
    bind_secret: Optional[str] = None


def route(update: dict, config: dict) -> RoutingDecision:
    """하나의 Telegram update를 RoutingDecision으로 변환한다.

    Args:
        update: Telegram getUpdates가 돌려준 단일 update dict.
        config: 시스템 config의 telegram 섹션. allowed_user_ids / hub_chat_id가 필요.
    """
    allowed_user_ids = set(config.get("allowed_user_ids") or [])
    hub_chat_id = config.get("hub_chat_id") or 0

    # ─── callback_query ───
    if "callback_query" in update:
        cbq = update["callback_query"]
        from_user = cbq.get("from") or {}
        user_id = from_user.get("id")
        user_display = _display_name(from_user)
        msg = cbq.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        thread_id = msg.get("message_thread_id")
        data = cbq.get("data") or ""

        if not allowed_user_ids or user_id not in allowed_user_ids:
            return RoutingDecision(kind="ignore", user_id=user_id, chat_id=chat_id,
                                   callback_query_id=cbq.get("id"))
        if hub_chat_id and chat_id != hub_chat_id:
            return RoutingDecision(kind="ignore", user_id=user_id, chat_id=chat_id,
                                   callback_query_id=cbq.get("id"))

        action, project, task_id = _parse_callback_data(data)
        if not action:
            return RoutingDecision(
                kind="reply", chat_id=chat_id, thread_id=thread_id,
                callback_query_id=cbq.get("id"),
                text="⚠️ 알 수 없는 버튼입니다.",
            )
        return RoutingDecision(
            kind="callback_query", chat_id=chat_id, thread_id=thread_id,
            user_id=user_id, user_display=user_display,
            callback_query_id=cbq.get("id"),
            callback_action=action, callback_project=project, callback_task_id=task_id,
        )

    # ─── message ───
    if "message" in update:
        msg = update["message"]
        from_user = msg.get("from") or {}
        user_id = from_user.get("id")
        user_display = _display_name(from_user)
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        thread_id = msg.get("message_thread_id")
        message_id = msg.get("message_id")
        text = (msg.get("text") or "").strip()

        # 첨부 (현 Phase 미지원) — whitelist를 통과한 사용자에게만 안내.
        has_attachment = bool(msg.get("photo") or msg.get("document")
                              or msg.get("video") or msg.get("audio") or msg.get("voice"))

        # User whitelist 먼저 검사. 통과 못하면 조용히 drop.
        if not allowed_user_ids or user_id not in allowed_user_ids:
            return RoutingDecision(kind="ignore", user_id=user_id, chat_id=chat_id,
                                   thread_id=thread_id, message_id=message_id)

        # /bind_hub는 hub_chat_id가 아직 0일 때를 위해 chat_id 검사 전에 처리.
        if text.startswith("/bind_hub"):
            parts = _split_args(text)
            secret = parts[1] if len(parts) > 1 else ""
            return RoutingDecision(
                kind="bind_hub", chat_id=chat_id, thread_id=thread_id,
                user_id=user_id, user_display=user_display,
                message_id=message_id, bind_secret=secret,
            )

        # 이후 모든 경로는 chat_id whitelist 필요.
        if hub_chat_id and chat_id != hub_chat_id:
            return RoutingDecision(kind="ignore", user_id=user_id, chat_id=chat_id,
                                   thread_id=thread_id, message_id=message_id)

        if has_attachment:
            return RoutingDecision(
                kind="reply", chat_id=chat_id, thread_id=thread_id, user_id=user_id,
                message_id=message_id,
                text="아직 이미지/문서는 지원하지 않습니다. 텍스트로 요청해 주세요.",
            )

        if not text:
            return RoutingDecision(kind="ignore", user_id=user_id, chat_id=chat_id,
                                   thread_id=thread_id, message_id=message_id)

        if text.startswith("/"):
            cmd, args = _parse_slash(text)
            if cmd not in SUPPORTED_SLASH_COMMANDS:
                return RoutingDecision(
                    kind="reply", chat_id=chat_id, thread_id=thread_id,
                    user_id=user_id, message_id=message_id,
                    text=f"알 수 없는 명령입니다: /{cmd}\n/help 로 사용 가능한 명령을 확인하세요.",
                )
            return RoutingDecision(
                kind="slash_command", chat_id=chat_id, thread_id=thread_id,
                user_id=user_id, user_display=user_display,
                message_id=message_id, command=cmd, args=args,
            )

        # 자연어 → ChatProcessor
        return RoutingDecision(
            kind="natural_message", chat_id=chat_id, thread_id=thread_id,
            user_id=user_id, user_display=user_display,
            message_id=message_id, text=text,
        )

    # 기타 update 타입 (edited_message, channel_post 등)은 모두 drop.
    return RoutingDecision(kind="ignore")


# ─── 내부 헬퍼 ───

def _display_name(from_user: dict) -> Optional[str]:
    """Telegram from 필드에서 식별자 문자열을 뽑는다.

    우선순위: username → first_name → id. 태그(`tg:...`)의 suffix로 쓰인다.
    """
    if not from_user:
        return None
    username = from_user.get("username")
    if username:
        return str(username)
    first_name = from_user.get("first_name")
    if first_name:
        return str(first_name)
    user_id = from_user.get("id")
    if user_id is not None:
        return str(user_id)
    return None


def _parse_slash(text: str) -> tuple[str, list[str]]:
    """'/cmd arg1 arg2' → (cmd, [arg1, arg2]). '@botname' suffix 제거."""
    parts = _split_args(text)
    head = parts[0].lstrip("/")
    # /cmd@botname 형태 → /cmd
    if "@" in head:
        head = head.split("@", 1)[0]
    return head, parts[1:]


def _split_args(text: str) -> list[str]:
    """shlex 기반 인자 분리. 실패 시 공백 분리로 fallback."""
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def _parse_callback_data(data: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """'action:project:task_id' → (action, project, task_id). 잘못된 포맷이면 (None, None, None)."""
    if not data:
        return None, None, None
    parts = data.split(":", 2)
    if len(parts) != 3:
        return None, None, None
    action, project, task_id = parts
    if not action or not project or not task_id:
        return None, None, None
    return action, project, task_id
