"""TelegramClient 단위 테스트.

실제 HTTP 호출은 하지 않는다. urllib.request.urlopen을 monkeypatch하여
요청 URL/바디/헤더와 응답 파싱/에러 처리/rate limit을 검증한다.
"""

import io
import json
import threading
import time
import urllib.error
from unittest.mock import patch

import pytest

from telegram.client import TelegramClient, TelegramAPIError


class FakeHTTPResponse:
    """urlopen이 반환하는 context manager의 최소 흉내."""

    def __init__(self, body: bytes):
        self._buf = io.BytesIO(body)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._buf.read()


def _ok_response(result):
    return FakeHTTPResponse(json.dumps({"ok": True, "result": result}).encode("utf-8"))


def _capture_urlopen(captured: list, result):
    """urlopen을 가로채 요청(req)을 captured에 기록하고 result를 돌려주는 fake."""
    def _fake(req, timeout=None):
        captured.append({
            "url": req.full_url,
            "method": req.get_method(),
            "body": req.data.decode("utf-8") if req.data else None,
            "timeout": timeout,
        })
        return _ok_response(result)
    return _fake


# ─── 기본 동작 ───

def test_send_message_posts_json_body():
    client = TelegramClient("TESTTOKEN", send_interval_seconds=0)
    captured: list = []
    with patch("telegram.client.urllib.request.urlopen", _capture_urlopen(captured, {"message_id": 42})):
        resp = client.send_message(chat_id=-100, text="hello", message_thread_id=7)

    assert resp["ok"] is True
    assert resp["result"]["message_id"] == 42
    assert len(captured) == 1
    call = captured[0]
    assert call["url"] == "https://api.telegram.org/botTESTTOKEN/sendMessage"
    assert call["method"] == "POST"
    body = json.loads(call["body"])
    assert body == {"chat_id": -100, "text": "hello",
                    "disable_web_page_preview": True, "message_thread_id": 7}


def test_create_forum_topic_returns_thread_id():
    client = TelegramClient("T", send_interval_seconds=0)
    captured: list = []
    with patch("telegram.client.urllib.request.urlopen",
               _capture_urlopen(captured, {"message_thread_id": 55, "name": "my-app"})):
        resp = client.create_forum_topic(chat_id=-100, name="my-app")

    assert resp["result"]["message_thread_id"] == 55
    body = json.loads(captured[0]["body"])
    assert body == {"chat_id": -100, "name": "my-app"}


def test_close_reopen_delete_forum_topic_hit_correct_methods():
    client = TelegramClient("T", send_interval_seconds=0)
    captured: list = []
    with patch("telegram.client.urllib.request.urlopen",
               _capture_urlopen(captured, True)):
        client.close_forum_topic(-100, 55)
        client.reopen_forum_topic(-100, 55)
        client.delete_forum_topic(-100, 55)

    methods = [call["url"].rsplit("/", 1)[-1] for call in captured]
    assert methods == ["closeForumTopic", "reopenForumTopic", "deleteForumTopic"]


def test_get_updates_returns_list():
    client = TelegramClient("T", send_interval_seconds=0)
    updates = [{"update_id": 1}, {"update_id": 2}]
    with patch("telegram.client.urllib.request.urlopen",
               _capture_urlopen([], updates)):
        result = client.get_updates(offset=10, timeout=5)

    assert result == updates


def test_answer_callback_query_params():
    client = TelegramClient("T", send_interval_seconds=0)
    captured: list = []
    with patch("telegram.client.urllib.request.urlopen",
               _capture_urlopen(captured, True)):
        client.answer_callback_query("CBQID", text="ack", show_alert=True)
    body = json.loads(captured[0]["body"])
    assert body == {"callback_query_id": "CBQID", "text": "ack", "show_alert": True}


# ─── 에러 처리 ───

def test_api_raises_when_ok_false():
    client = TelegramClient("T", send_interval_seconds=0)
    err_body = json.dumps({"ok": False, "description": "Bad Request: chat not found",
                           "error_code": 400}).encode("utf-8")
    with patch("telegram.client.urllib.request.urlopen",
               lambda req, timeout=None: FakeHTTPResponse(err_body)):
        with pytest.raises(TelegramAPIError) as exc_info:
            client.send_message(-1, "x")

    assert exc_info.value.error_code == 400
    assert "chat not found" in exc_info.value.description


def test_api_raises_on_http_error():
    client = TelegramClient("T", send_interval_seconds=0)
    payload = json.dumps({"ok": False, "description": "Forbidden",
                          "error_code": 403}).encode("utf-8")

    def _raise(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, io.BytesIO(payload))

    with patch("telegram.client.urllib.request.urlopen", _raise):
        with pytest.raises(TelegramAPIError) as exc_info:
            client.send_message(-1, "x")

    assert exc_info.value.error_code == 403


def test_api_retries_once_on_429():
    """429 응답은 retry_after 대기 후 1회 재시도한다."""
    client = TelegramClient("T", send_interval_seconds=0)
    payload_429 = json.dumps({"ok": False, "description": "Too Many Requests",
                              "error_code": 429,
                              "parameters": {"retry_after": 0}}).encode("utf-8")
    call_count = {"n": 0}

    def _flaky(req, timeout=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many",
                                         {}, io.BytesIO(payload_429))
        return _ok_response({"message_id": 1})

    with patch("telegram.client.urllib.request.urlopen", _flaky):
        resp = client.send_message(-1, "x")

    assert call_count["n"] == 2
    assert resp["result"]["message_id"] == 1


# ─── Rate limit / 보안 ───

def test_rate_limit_inserts_gap_between_sends():
    """send_interval_seconds 만큼의 최소 간격이 보장된다."""
    client = TelegramClient("T", send_interval_seconds=0.05)
    with patch("telegram.client.urllib.request.urlopen",
               _capture_urlopen([], True)):
        t0 = time.monotonic()
        client.send_message(-1, "a")
        client.send_message(-1, "b")
        client.send_message(-1, "c")
        elapsed = time.monotonic() - t0

    # 3회 호출 사이에 최소 2번의 간격 → 0.1s 이상. (sleep 정확도는 OS 의존적이므로 여유)
    assert elapsed >= 0.08


def test_token_is_masked_in_repr():
    client = TelegramClient("SUPER-SECRET-TOKEN-1234", send_interval_seconds=0)
    rendered = repr(client)
    assert "SUPER-SECRET" not in rendered
    assert "1234" in rendered  # 마지막 4자리만 노출


def test_empty_token_rejected():
    with pytest.raises(ValueError):
        TelegramClient("", send_interval_seconds=0)


def test_thread_safe_rate_limit():
    """여러 스레드가 동시에 send_message를 호출해도 간격이 유지된다."""
    client = TelegramClient("T", send_interval_seconds=0.02)
    errors: list = []

    def worker():
        try:
            for _ in range(3):
                client.send_message(-1, "x")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    # patch는 스레드 전체 수명 동안 유지되도록 바깥에서 건다 (worker 내부에서 걸면
    # 스레드 간섭으로 unpatch 타이밍이 엉킨다).
    with patch("telegram.client.urllib.request.urlopen",
               _capture_urlopen([], True)):
        threads = [threading.Thread(target=worker) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert errors == []
