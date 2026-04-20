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


# ─── plan_summary 본문 렌더링 ───

def _plan_review_notification(plan_summary):
    return {
        "event_type": "plan_review_requested",
        "task_id": "00042",
        "message": "Plan을 확인해주세요. subtask 2개 생성됨.",
        "details": {"project": "my-app", "plan_summary": plan_summary},
    }


def test_plan_review_renders_strategy_and_subtasks():
    """plan_review_requested 알림에 plan_summary가 붙으면 전략/subtask 블록이 렌더된다."""
    plan_summary = {
        "strategy_note": "원문 의도를 살려 3개 subtask로 분할한다.",
        "subtasks": [
            {"index": 1, "subtask_id": "00042-1",
             "title": "데이터 모델 범용화",
             "responsibility": "DB 모델에서 IRIS 명명을 제거한다."},
            {"index": 2, "subtask_id": "00042-2",
             "title": "스크래퍼 플러그인화",
             "responsibility": "adapter 레지스트리를 도입한다."},
        ],
        "total_subtasks": 2,
    }
    rendered = format_notification(_plan_review_notification(plan_summary))

    assert "*전략 노트*" in rendered
    # blockquote 프리픽스와 escape 적용 확인
    assert ">원문 의도를 살려 3개 subtask로 분할한다\\." in rendered
    assert "*Subtasks \\(2개\\)*" in rendered
    assert "*1\\.* 데이터 모델 범용화 `00042\\-1`" in rendered
    assert "↳ DB 모델에서 IRIS 명명을 제거한다\\." in rendered
    assert "*2\\.* 스크래퍼 플러그인화 `00042\\-2`" in rendered


def test_plan_review_renders_truncated_marker():
    """truncated=True 항목은 subtask_id/responsibility 없이 이탤릭 힌트로만 표기된다."""
    plan_summary = {
        "strategy_note": "",
        "subtasks": [
            {"index": 1, "subtask_id": "00042-1",
             "title": "첫 subtask", "responsibility": "내용"},
            {"index": 2, "subtask_id": "",
             "title": "… 외 3개 더 (Web에서 전체 확인)",
             "responsibility": "", "truncated": True},
        ],
        "total_subtasks": 4,
    }
    rendered = format_notification(_plan_review_notification(plan_summary))

    assert "*Subtasks \\(4개\\)*" in rendered
    # truncated entry는 `_이탤릭_` 표기만, 앞에 번호를 붙이지 않는다.
    assert "_… 외 3개 더\\(Web에서 전체 확인\\)_" in rendered or \
           "_… 외 3개 더 \\(Web에서 전체 확인\\)_" in rendered


def test_plan_summary_ignored_for_non_review_events():
    """plan_summary가 실려 있어도 plan_review/replan_review 이외에는 렌더하지 않는다."""
    noti = {
        "event_type": "task_completed",
        "task_id": "1",
        "message": "완료",
        "details": {"plan_summary": {
            "strategy_note": "무시되어야 함",
            "subtasks": [{"index": 1, "title": "무시", "responsibility": "무시"}],
            "total_subtasks": 1,
        }},
    }
    rendered = format_notification(noti)
    assert "전략 노트" not in rendered
    assert "Subtasks" not in rendered


def test_plan_review_without_summary_still_renders_header():
    """plan_summary가 없어도 기존 헤더+메시지 렌더링은 깨지지 않는다 (하위 호환)."""
    noti = {
        "event_type": "plan_review_requested",
        "task_id": "00042",
        "message": "Plan을 확인해주세요. subtask 2개 생성됨.",
        "details": {"project": "my-app"},
    }
    rendered = format_notification(noti)
    assert rendered.startswith("🟡")
    assert "Plan을 확인해주세요" in rendered
    assert "전략 노트" not in rendered
