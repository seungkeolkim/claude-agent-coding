r"""Notification → Telegram 메시지 포맷 변환.

핵심 책임:
- MarkdownV2 escape util (`_ * [ ] ( ) ~ \` > # + - = | { } . !`)
- 이벤트별 텍스트 포맷 (아이콘 + 라벨 + 본문)
- human review 이벤트에는 inline keyboard(승인/수정/취소) 부착

callback_data 포맷:
    "{action}:{project}:{task_id}"
    예) "approve:my-app:00042", "reject_modify:my-app:00042", "reject_cancel:my-app:00042"
Telegram callback_data는 1~64 byte 제한. project_name이 긴 경우는 router가 dispatch 직전에
프로젝트 해시/코드로 치환하는 경로를 향후 추가할 수 있으나, 본 세션에서는 단순 포맷 유지.
"""

from __future__ import annotations

from typing import Optional

# MarkdownV2 파싱에서 특수문자로 간주되는 모든 문자. 본문에 포함시키려면 역슬래시 escape 필수.
# 참고: https://core.telegram.org/bots/api#markdownv2-style
_MARKDOWN_V2_SPECIALS = r"_*[]()~`>#+-=|{}.!"

# 이벤트별 아이콘과 라벨. notification.py의 EVENT_STYLES와 의미적으로 매칭시킨다.
_EVENT_STYLES = {
    "task_completed":          {"icon": "✅", "label": "완료"},
    "task_failed":             {"icon": "🔴", "label": "실패"},
    "pr_created":              {"icon": "🔵", "label": "PR 생성"},
    "pr_merged":               {"icon": "🟢", "label": "PR 머지"},
    "pr_merge_failed":         {"icon": "⚠️", "label": "PR 머지 실패"},
    "plan_review_requested":   {"icon": "🟡", "label": "Plan Review 요청"},
    "replan_review_requested": {"icon": "🟡", "label": "Replan Review 요청"},
    "plan_review_responded":   {"icon": "📬", "label": "Plan 응답"},
    "pr_review_responded":     {"icon": "📬", "label": "PR 응답"},
    "escalation":              {"icon": "🚨", "label": "에스컬레이션"},
}


def escape_markdown_v2(text: str) -> str:
    """MarkdownV2 파싱에서 특수 의미를 갖는 문자를 모두 escape한다.

    Telegram이 요구하는 규칙을 그대로 따른다. 본문에 링크/강조를 넣고 싶으면 호출부에서
    해당 구간만 raw MarkdownV2를 구성한 뒤 나머지 부분에 대해서만 이 함수를 적용해야 한다.
    """
    if not text:
        return ""
    result = []
    for ch in text:
        if ch in _MARKDOWN_V2_SPECIALS:
            result.append("\\")
        result.append(ch)
    return "".join(result)


def format_notification(notification: dict) -> str:
    """notification dict를 MarkdownV2 텍스트로 렌더링한다.

    본문은 항상 escape되며, 헤더의 아이콘과 label은 ASCII 이외이거나 특수문자가 적어
    escape 없이도 문제가 없는 문자만 사용한다.
    """
    event_type = notification.get("event_type", "unknown")
    style = _EVENT_STYLES.get(event_type, {"icon": "•", "label": event_type})
    task_id = str(notification.get("task_id", "?"))
    message = notification.get("message", "") or ""
    details = notification.get("details") or {}

    header = f"{style['icon']} *{escape_markdown_v2(style['label'])}* · task \\#{escape_markdown_v2(task_id)}"
    body_lines = [header]

    if message:
        body_lines.append(escape_markdown_v2(message))

    # details에서 자주 쓰이는 필드만 별도 블록으로 (escape 적용).
    pr_url = details.get("pr_url")
    if pr_url:
        # URL은 MarkdownV2의 [text](url) 문법에서 ')' 만 추가 escape 필요.
        url_safe = pr_url.replace(")", "\\)").replace("\\", "\\\\")
        body_lines.append(f"[PR 링크]({url_safe})")

    error_summary = details.get("error_summary") or details.get("error")
    if error_summary:
        body_lines.append(f"`{_escape_for_code(error_summary)}`")

    return "\n".join(body_lines)


def build_review_keyboard(project: str, task_id: str,
                          include_modify: bool = True) -> dict:
    """plan/replan review 알림에 부착할 inline keyboard를 구성한다.

    반환값은 Telegram API의 reply_markup 구조(JSON-serializable dict)이다.
    """
    row: list[dict] = [
        {"text": "✅ 승인", "callback_data": _callback("approve", project, task_id)},
    ]
    if include_modify:
        row.append({"text": "📝 수정", "callback_data": _callback("reject_modify", project, task_id)})
    row.append({"text": "❌ 취소", "callback_data": _callback("reject_cancel", project, task_id)})
    return {"inline_keyboard": [row]}


def build_pr_retry_keyboard(project: str, task_id: str) -> dict:
    """PR 머지 실패 알림에 부착할 간단한 확인 keyboard (자세히는 Web/CLI에서 처리)."""
    return {"inline_keyboard": [[
        {"text": "상세 보기", "callback_data": _callback("view", project, task_id)},
    ]]}


def reply_markup_for_notification(notification: dict) -> Optional[dict]:
    """이벤트 타입에 따른 기본 inline keyboard를 반환. 없으면 None."""
    event_type = notification.get("event_type")
    project = (notification.get("details") or {}).get("project") or notification.get("project")
    task_id = str(notification.get("task_id", ""))
    if not project or not task_id:
        return None

    if event_type in ("plan_review_requested", "replan_review_requested"):
        return build_review_keyboard(project, task_id)
    if event_type == "pr_merge_failed":
        return build_pr_retry_keyboard(project, task_id)
    return None


# ─── 내부 ───

def _callback(action: str, project: str, task_id: str) -> str:
    """callback_data 포맷. 64 byte 제한을 넘지 않도록 호출부에서 검증 권장."""
    return f"{action}:{project}:{task_id}"


def _escape_for_code(text: str) -> str:
    """MarkdownV2 `code` 블록 내부는 백틱과 역슬래시만 escape하면 된다."""
    return text.replace("\\", "\\\\").replace("`", "\\`")
