"""Telegram formatter 단위 테스트. pure function 위주라 mock 없이 검증 가능하다."""

import pytest

from telegram.formatter import (
    escape_markdown_v2,
    format_notification,
    build_review_keyboard,
    reply_markup_for_notification,
)


# ─── escape ───

@pytest.mark.parametrize("raw,expected", [
    ("hello", "hello"),
    ("a.b", "a\\.b"),
    ("1+1=2", "1\\+1\\=2"),
    ("foo_bar", "foo\\_bar"),
    ("(x)[y]{z}", "\\(x\\)\\[y\\]\\{z\\}"),
    ("", ""),
    ("task #42!", "task \\#42\\!"),
])
def test_escape_markdown_v2(raw, expected):
    assert escape_markdown_v2(raw) == expected


def test_escape_covers_all_specials():
    """MarkdownV2 문서가 명시한 특수문자 전부가 escape되는지 확인."""
    specials = "_*[]()~`>#+-=|{}.!"
    escaped = escape_markdown_v2(specials)
    # 각 특수문자 앞에 반드시 역슬래시가 붙는다.
    for ch in specials:
        assert f"\\{ch}" in escaped


# ─── format_notification ───

def test_format_notification_basic():
    noti = {
        "event_type": "task_completed",
        "task_id": "00042",
        "message": "로그인 기능 완료",
        "details": {"pr_url": "https://github.com/x/y/pull/7"},
    }
    rendered = format_notification(noti)
    assert rendered.startswith("✅")
    assert "task \\#00042" in rendered
    assert "로그인 기능 완료" in rendered
    assert "[PR 링크](https://github.com/x/y/pull/7)" in rendered


def test_format_notification_escapes_message_body():
    """본문의 특수문자는 escape되어 MarkdownV2 파싱을 깨뜨리지 않는다."""
    noti = {"event_type": "task_failed", "task_id": "1",
            "message": "오류: file.py (line 10) 실패!"}
    rendered = format_notification(noti)
    assert "file\\.py" in rendered
    assert "\\(line 10\\)" in rendered
    assert "실패\\!" in rendered


def test_format_notification_unknown_event_falls_back():
    noti = {"event_type": "mystery", "task_id": "9", "message": "뭐지"}
    rendered = format_notification(noti)
    assert "mystery" in rendered
    assert "뭐지" in rendered


def test_format_notification_error_summary_as_code():
    noti = {"event_type": "pr_merge_failed", "task_id": "5", "message": "merge 충돌",
            "details": {"error_summary": "conflict in foo.py"}}
    rendered = format_notification(noti)
    assert "`conflict in foo.py`" in rendered


# ─── inline keyboard ───

def test_build_review_keyboard_structure():
    kb = build_review_keyboard("my-app", "00042")
    assert kb == {
        "inline_keyboard": [[
            {"text": "✅ 승인", "callback_data": "approve:my-app:00042"},
            {"text": "📝 수정", "callback_data": "reject_modify:my-app:00042"},
            {"text": "❌ 취소", "callback_data": "reject_cancel:my-app:00042"},
        ]]
    }


def test_build_review_keyboard_without_modify():
    kb = build_review_keyboard("my-app", "00042", include_modify=False)
    buttons = kb["inline_keyboard"][0]
    assert len(buttons) == 2
    assert [b["callback_data"] for b in buttons] == [
        "approve:my-app:00042", "reject_cancel:my-app:00042"]


def test_reply_markup_for_plan_review():
    noti = {"event_type": "plan_review_requested", "task_id": "7",
            "details": {"project": "alpha"}}
    kb = reply_markup_for_notification(noti)
    assert kb is not None
    assert "inline_keyboard" in kb
    assert any("approve:alpha:7" in b["callback_data"]
               for b in kb["inline_keyboard"][0])


def test_reply_markup_returns_none_when_project_missing():
    noti = {"event_type": "plan_review_requested", "task_id": "7", "details": {}}
    assert reply_markup_for_notification(noti) is None


def test_reply_markup_none_for_non_interactive_events():
    """task_completed 등 사용자 입력이 필요 없는 이벤트는 keyboard 없이 단순 텍스트로."""
    noti = {"event_type": "task_completed", "task_id": "1",
            "details": {"project": "alpha"}}
    assert reply_markup_for_notification(noti) is None
