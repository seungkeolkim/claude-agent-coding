"""Telegram topic ↔ Chat 세션 매핑.

Web Chat의 `ChatProcessor` 인프라를 그대로 재사용한다. Telegram은 topic 단위로
영구 session을 유지하며, session_id는 `tg_{chat_id}_{thread_id}` 형태다.
세션 파일은 `session_history/chatbot/{session_id}.json`에 저장되며, Web Chat과
동일한 compression/history 로직이 그대로 동작한다.
"""

from __future__ import annotations

import os
import sys
from typing import Callable, Optional

# web/web_chatbot.py의 ChatProcessor 및 레지스트리를 그대로 재사용한다.
_scripts_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

from web.web_chatbot import ChatProcessor, get_or_create_session, remove_session  # noqa: E402

FRONTEND = "telegram"


def session_id_for(chat_id: int, thread_id: int) -> str:
    """topic 단위 영구 세션 ID를 생성한다."""
    return f"tg_{chat_id}_{thread_id}"


def get_session(agent_hub_root: str, chat_id: int, thread_id: int,
                on_message: Callable[[dict], None],
                requested_by: Optional[str] = None) -> ChatProcessor:
    """해당 topic의 ChatProcessor를 가져오거나 생성한다 (session_id 영구).

    requested_by는 메시지를 보낸 사용자 식별자 (예: 'tg:username'). 이후 submit 시
    task JSON의 requested_by 필드 및 커밋/PR 제목 태그에 쓰인다. 같은 topic에
    여러 사용자가 게시할 수 있으므로 매 호출마다 갱신된다.
    """
    sid = session_id_for(chat_id, thread_id)
    return get_or_create_session(agent_hub_root, sid, on_message,
                                 frontend=FRONTEND, requested_by=requested_by)


def drop_session(chat_id: int, thread_id: int) -> None:
    """topic 세션을 레지스트리에서 제거한다 (/new_session 처리 등)."""
    remove_session(session_id_for(chat_id, thread_id))
