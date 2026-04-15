"""Telegram Bot API HTTP 래퍼.

설계 결정:
- 외부 의존성(requests, httpx) 추가를 피하기 위해 stdlib urllib만 사용.
- bot_token은 절대 로그에 출력하지 않는다. __repr__와 로그 포맷에서 마스킹한다.
- outbound rate limit: Bot API 초당 30msg/그룹 제한 대응. threading.Lock 기반 단순
  간격 삽입 (send_interval_seconds). 정교한 token bucket은 필요해지면 도입한다.
- 429 응답 수신 시 retry_after 만큼 sleep 후 1회 재시도. 그 이상은 상위에서 다음 폴링
  사이클로 자연스럽게 재시도하도록 위임.

API 참고: https://core.telegram.org/bots/api
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

logger = logging.getLogger(__name__)


class TelegramAPIError(RuntimeError):
    """Telegram API가 ok=false를 반환했거나 HTTP 에러가 발생했을 때 raise."""

    def __init__(self, method: str, description: str, error_code: Optional[int] = None,
                 retry_after: Optional[int] = None):
        self.method = method
        self.description = description
        self.error_code = error_code
        self.retry_after = retry_after
        super().__init__(f"Telegram {method} 실패 (code={error_code}): {description}")


class TelegramClient:
    """Telegram Bot API 호출을 담당하는 최소 래퍼.

    thread-safe. bridge의 메인 long-polling 스레드와 notification poller 스레드가
    동시에 sendMessage를 호출해도 간격이 유지된다.
    """

    BASE_URL = "https://api.telegram.org"

    def __init__(self, bot_token: str, default_timeout_seconds: float = 10.0,
                 send_interval_seconds: float = 0.05):
        """
        Args:
            bot_token: BotFather가 발급한 토큰. 로그에 출력 금지.
            default_timeout_seconds: 일반 HTTP 타임아웃. long polling은 호출부에서 별도 지정.
            send_interval_seconds: 전 outbound 호출 사이의 최소 간격 (rate limit 대응).
        """
        if not bot_token:
            raise ValueError("bot_token이 비어 있습니다.")
        self._token = bot_token
        self._default_timeout = default_timeout_seconds
        self._send_interval = send_interval_seconds
        self._rate_lock = threading.Lock()
        self._last_send_at = 0.0

    def __repr__(self) -> str:
        # token 마스킹. 마지막 4자리만 노출.
        tail = self._token[-4:] if len(self._token) >= 4 else "****"
        return f"TelegramClient(token=***{tail})"

    # ─── 메시지 ───

    def send_message(self, chat_id: int, text: str,
                     message_thread_id: Optional[int] = None,
                     parse_mode: Optional[str] = None,
                     reply_markup: Optional[dict] = None,
                     disable_web_page_preview: bool = True) -> dict:
        """텍스트 메시지 전송. topic으로 보낼 때는 message_thread_id 필수."""
        params: dict[str, Any] = {"chat_id": chat_id, "text": text,
                                  "disable_web_page_preview": disable_web_page_preview}
        if message_thread_id is not None:
            params["message_thread_id"] = message_thread_id
        if parse_mode:
            params["parse_mode"] = parse_mode
        if reply_markup is not None:
            params["reply_markup"] = reply_markup
        return self._api("sendMessage", params)

    def edit_message_text(self, chat_id: int, message_id: int, text: str,
                          parse_mode: Optional[str] = None,
                          reply_markup: Optional[dict] = None) -> dict:
        """기존 메시지의 텍스트를 수정한다 (inline keyboard 업데이트 등)."""
        params: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if parse_mode:
            params["parse_mode"] = parse_mode
        if reply_markup is not None:
            params["reply_markup"] = reply_markup
        return self._api("editMessageText", params)

    def answer_callback_query(self, callback_query_id: str, text: Optional[str] = None,
                              show_alert: bool = False) -> dict:
        """inline keyboard 버튼 클릭에 대한 ack. 15초 이내에 호출 안 하면 클라이언트에
        빨간 에러가 표시되므로, dispatch 전에 먼저 호출해야 한다."""
        params: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text is not None:
            params["text"] = text
        if show_alert:
            params["show_alert"] = True
        return self._api("answerCallbackQuery", params)

    # ─── Forum Topic 관리 ───

    def create_forum_topic(self, chat_id: int, name: str,
                           icon_color: Optional[int] = None) -> dict:
        """Topic 생성. 응답의 result.message_thread_id를 project_state에 저장한다."""
        params: dict[str, Any] = {"chat_id": chat_id, "name": name}
        if icon_color is not None:
            params["icon_color"] = icon_color
        return self._api("createForumTopic", params)

    def edit_forum_topic(self, chat_id: int, message_thread_id: int,
                         name: Optional[str] = None,
                         icon_custom_emoji_id: Optional[str] = None) -> dict:
        """Topic 이름/아이콘 변경. orphan 표시용 '⚠️ [orphan] ...' rename에 사용."""
        params: dict[str, Any] = {"chat_id": chat_id, "message_thread_id": message_thread_id}
        if name is not None:
            params["name"] = name
        if icon_custom_emoji_id is not None:
            params["icon_custom_emoji_id"] = icon_custom_emoji_id
        return self._api("editForumTopic", params)

    def close_forum_topic(self, chat_id: int, message_thread_id: int) -> dict:
        """Topic을 닫는다 (메시지 보존, 🔒 아이콘)."""
        return self._api("closeForumTopic",
                         {"chat_id": chat_id, "message_thread_id": message_thread_id})

    def reopen_forum_topic(self, chat_id: int, message_thread_id: int) -> dict:
        """닫힌 topic을 다시 연다."""
        return self._api("reopenForumTopic",
                         {"chat_id": chat_id, "message_thread_id": message_thread_id})

    def delete_forum_topic(self, chat_id: int, message_thread_id: int) -> dict:
        """Topic을 영구 삭제한다. orphan 수동 삭제 명령에서만 호출된다."""
        return self._api("deleteForumTopic",
                         {"chat_id": chat_id, "message_thread_id": message_thread_id})

    def get_forum_topic_icon_stickers(self) -> dict:
        """기본 아이콘 색상 목록 조회 (참고용)."""
        return self._api("getForumTopicIconStickers", {})

    # ─── Update 수신 (long polling) ───

    def get_updates(self, offset: Optional[int] = None, timeout: int = 30,
                    allowed_updates: Optional[list[str]] = None) -> list[dict]:
        """Long polling으로 업데이트 수신. timeout은 Telegram 측 long-poll 대기 시간."""
        params: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        if allowed_updates is not None:
            params["allowed_updates"] = allowed_updates
        # HTTP 타임아웃은 long-poll 타임아웃보다 넉넉히 잡는다.
        response = self._api("getUpdates", params,
                             timeout_override=timeout + 10, skip_rate_limit=True)
        result = response.get("result")
        return result if isinstance(result, list) else []

    # ─── 파일 ───

    def get_file(self, file_id: str) -> dict:
        """파일 메타데이터 조회 (Phase C에서 첨부 다운로드에 사용)."""
        return self._api("getFile", {"file_id": file_id})

    # ─── 내부 ───

    def _api(self, method: str, params: dict,
             timeout_override: Optional[float] = None,
             skip_rate_limit: bool = False,
             _already_retried: bool = False) -> dict:
        """공통 호출 엔트리포인트. JSON POST → 응답 파싱 → ok 체크."""
        if not skip_rate_limit:
            self._enforce_rate_limit()

        url = f"{self.BASE_URL}/bot{self._token}/{method}"
        body = json.dumps(params, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        timeout = timeout_override if timeout_override is not None else self._default_timeout

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            # Telegram은 429 / 4xx에서도 JSON 바디를 내려 보낸다. 파싱을 시도한다.
            try:
                raw = e.read().decode("utf-8")
                payload = json.loads(raw)
            except (ValueError, OSError):
                payload = {}
            description = payload.get("description", str(e))
            error_code = payload.get("error_code", e.code)
            retry_after = (payload.get("parameters") or {}).get("retry_after")

            # 429: 한 번만 retry_after 대기 후 재시도.
            if e.code == 429 and retry_after is not None and not _already_retried:
                logger.warning("Telegram %s rate limit, %ds 대기 후 재시도", method, retry_after)
                time.sleep(float(retry_after))
                return self._api(method, params, timeout_override=timeout_override,
                                 skip_rate_limit=skip_rate_limit, _already_retried=True)

            raise TelegramAPIError(method, description, error_code, retry_after) from None
        except urllib.error.URLError as e:
            raise TelegramAPIError(method, f"네트워크 오류: {e.reason}") from None

        try:
            payload = json.loads(raw)
        except ValueError:
            raise TelegramAPIError(method, f"응답 JSON 파싱 실패: {raw[:200]}") from None

        if not payload.get("ok"):
            description = payload.get("description", "unknown")
            error_code = payload.get("error_code")
            raise TelegramAPIError(method, description, error_code)
        return payload

    def _enforce_rate_limit(self) -> None:
        """마지막 전송 이후 send_interval_seconds 미만이면 그 차이만큼 sleep."""
        with self._rate_lock:
            now = time.monotonic()
            delta = now - self._last_send_at
            if delta < self._send_interval:
                time.sleep(self._send_interval - delta)
            self._last_send_at = time.monotonic()
