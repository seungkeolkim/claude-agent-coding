"""Telegram router 단위 테스트.

route(update, config)의 파싱/whitelist 로직을 검증한다. 실제 Telegram 호출이나
ChatProcessor 연동 없이 순수 함수로 테스트 가능하다.
"""

from telegram.router import route, RoutingDecision


HUB = {"allowed_user_ids": [111], "hub_chat_id": -1001}
HUB_UNBOUND = {"allowed_user_ids": [111], "hub_chat_id": 0}
EMPTY_ALLOW = {"allowed_user_ids": [], "hub_chat_id": -1001}


def _msg(text, user_id=111, chat_id=-1001, thread_id=7, message_id=1, **extra):
    msg = {
        "message_id": message_id,
        "from": {"id": user_id},
        "chat": {"id": chat_id},
        "message_thread_id": thread_id,
        "text": text,
    }
    msg.update(extra)
    return {"update_id": 42, "message": msg}


def _cbq(data, user_id=111, chat_id=-1001, thread_id=7):
    return {
        "update_id": 43,
        "callback_query": {
            "id": "CBQID",
            "from": {"id": user_id},
            "data": data,
            "message": {
                "chat": {"id": chat_id},
                "message_thread_id": thread_id,
                "message_id": 99,
            },
        },
    }


# ─── 자연어 메시지 ───

def test_natural_language_message_routes_to_chatprocessor():
    d = route(_msg("로그인 기능 구현해줘"), HUB)
    assert d.kind == "natural_message"
    assert d.text == "로그인 기능 구현해줘"
    assert d.chat_id == -1001
    assert d.thread_id == 7


def test_empty_text_ignored():
    d = route(_msg(""), HUB)
    assert d.kind == "ignore"


# ─── Whitelist ───

def test_unknown_user_is_ignored():
    d = route(_msg("hello", user_id=999), HUB)
    assert d.kind == "ignore"


def test_empty_allowlist_rejects_all():
    d = route(_msg("hello"), EMPTY_ALLOW)
    assert d.kind == "ignore"


def test_wrong_chat_id_ignored():
    d = route(_msg("hello", chat_id=-2002), HUB)
    assert d.kind == "ignore"


# ─── 슬래시 명령 ───

def test_slash_status():
    d = route(_msg("/status"), HUB)
    assert d.kind == "slash_command"
    assert d.command == "status"
    assert d.args == []


def test_slash_with_args():
    d = route(_msg("/list --status submitted"), HUB)
    assert d.kind == "slash_command"
    assert d.command == "list"
    assert d.args == ["--status", "submitted"]


def test_slash_with_botname_suffix():
    """BotFather 봇명 suffix는 제거된다 (/status@agenthubbot → status)."""
    d = route(_msg("/status@agenthubbot"), HUB)
    assert d.kind == "slash_command"
    assert d.command == "status"


def test_unknown_slash_returns_help_reply():
    d = route(_msg("/explode"), HUB)
    assert d.kind == "reply"
    assert "/help" in d.text


# ─── /bind_hub (chat_id 검증 skip) ───

def test_bind_hub_when_not_bound():
    d = route(_msg("/bind_hub mysecret", chat_id=-9999), HUB_UNBOUND)
    assert d.kind == "bind_hub"
    assert d.bind_secret == "mysecret"
    assert d.chat_id == -9999


def test_bind_hub_missing_secret():
    d = route(_msg("/bind_hub"), HUB_UNBOUND)
    assert d.kind == "bind_hub"
    assert d.bind_secret == ""


def test_bind_hub_respects_user_whitelist():
    """bind_hub이라도 whitelist 밖 user는 drop."""
    d = route(_msg("/bind_hub s", user_id=999), HUB_UNBOUND)
    assert d.kind == "ignore"


# ─── 첨부 ───

def test_photo_is_rejected_politely():
    update = _msg("", photo=[{"file_id": "xxx", "width": 100, "height": 100}])
    d = route(update, HUB)
    assert d.kind == "reply"
    assert "지원하지 않" in d.text


def test_document_is_rejected_politely():
    update = _msg("", document={"file_id": "xxx", "file_name": "x.pdf"})
    d = route(update, HUB)
    assert d.kind == "reply"


# ─── callback_query ───

def test_callback_query_parsed():
    d = route(_cbq("approve:my-app:00042"), HUB)
    assert d.kind == "callback_query"
    assert d.callback_action == "approve"
    assert d.callback_project == "my-app"
    assert d.callback_task_id == "00042"
    assert d.callback_query_id == "CBQID"


def test_callback_query_invalid_format_replied():
    d = route(_cbq("garbage"), HUB)
    assert d.kind == "reply"
    assert "알 수 없는" in d.text


def test_callback_query_user_whitelist_enforced():
    d = route(_cbq("approve:x:1", user_id=999), HUB)
    assert d.kind == "ignore"


def test_callback_query_chat_whitelist_enforced():
    d = route(_cbq("approve:x:1", chat_id=-77), HUB)
    assert d.kind == "ignore"


# ─── 기타 update 타입 ───

def test_edited_message_is_ignored():
    update = {"update_id": 50, "edited_message": {"text": "ignore me"}}
    d = route(update, HUB)
    assert d.kind == "ignore"
