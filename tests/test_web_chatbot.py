"""
Web ChatProcessor 테스트.

ChatProcessor의 상태 전이, cancel+merge, confirmation flow를 검증한다.
claude -p 호출은 subprocess를 mock하여 테스트한다.
"""

import json
import os
import sys
import tempfile
import threading
import time
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from scripts.web.web_chatbot import (
    ChatProcessor,
    get_or_create_session,
    remove_session,
    broadcast_system_event,
    _format_confirmation_plain,
    _format_system_event,
    _strip_ansi,
    _active_sessions,
)


# ═══════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════

@pytest.fixture
def tmp_root():
    """테스트용 임시 agent-hub 루트."""
    with tempfile.TemporaryDirectory() as d:
        # 최소한의 디렉토리 구조 생성
        os.makedirs(os.path.join(d, "session_history", "web"), exist_ok=True)
        os.makedirs(os.path.join(d, "projects"), exist_ok=True)
        yield d


@pytest.fixture
def messages():
    """on_message 콜백이 수집한 메시지 목록."""
    collected = []

    def callback(event):
        collected.append(event)

    return collected, callback


@pytest.fixture(autouse=True)
def cleanup_sessions():
    """각 테스트 후 활성 세션 정리."""
    yield
    _active_sessions.clear()


# ═══════════════════════════════════════════════════════════
# 유틸리티 함수 테스트
# ═══════════════════════════════════════════════════════════

class TestUtils:
    """유틸리티 함수 테스트."""

    def test_strip_ansi(self):
        """ANSI 코드 제거를 확인한다."""
        text = "\033[0;32m[완료]\033[0m 작업 성공"
        assert _strip_ansi(text) == "[완료] 작업 성공"

    def test_strip_ansi_no_codes(self):
        """ANSI 코드가 없는 문자열은 그대로 반환한다."""
        text = "일반 텍스트"
        assert _strip_ansi(text) == "일반 텍스트"

    def test_format_confirmation_plain(self):
        """확인 메시지 포맷을 검증한다."""
        parsed = {
            "action": "submit",
            "project": "test-project",
            "params": {"title": "테스트 task"},
            "explanation": "테스트용 task를 생성합니다.",
        }
        result = _format_confirmation_plain(parsed)
        assert "submit" in result
        assert "test-project" in result
        assert "테스트 task" in result
        assert "테스트용 task를 생성합니다." in result

    def test_format_system_event(self):
        """시스템 이벤트 포맷을 검증한다."""
        event = {
            "event_type": "task_completed",
            "project": "my-project",
            "task_id": "00001",
            "message": "task 완료",
        }
        result = _format_system_event(event)
        assert "Task 완료" in result
        assert "my-project" in result
        assert "00001" in result

    def test_format_system_event_unknown_type(self):
        """알 수 없는 이벤트 타입도 처리한다."""
        event = {"event_type": "custom_event", "project": "p"}
        result = _format_system_event(event)
        assert "custom_event" in result


# ═══════════════════════════════════════════════════════════
# ChatProcessor 상태 전이 테스트
# ═══════════════════════════════════════════════════════════

class TestChatProcessorStates:
    """ChatProcessor 상태 전이 테스트."""

    def test_initial_state_idle(self, tmp_root, messages):
        """초기 상태가 idle인지 확인한다."""
        collected, callback = messages
        processor = ChatProcessor(tmp_root, "test-session", callback)
        assert processor._state == "idle"

    def test_submit_message_changes_state_to_processing(self, tmp_root, messages):
        """메시지 제출 시 processing 상태로 전환되는지 확인한다."""
        collected, callback = messages

        # claude -p를 mock하여 빠르게 응답
        mock_response = json.dumps({"intent": "conversation", "message": "안녕하세요!"})
        with mock.patch("scripts.web.web_chatbot.subprocess.Popen") as mock_popen:
            mock_proc = mock.MagicMock()
            mock_proc.communicate.return_value = (mock_response, "")
            mock_proc.returncode = 0
            mock_proc.poll.return_value = None
            mock_popen.return_value = mock_proc

            processor = ChatProcessor(tmp_root, "test-session", callback)
            processor.submit_message("안녕")

            # 스레드가 완료될 때까지 대기
            time.sleep(0.5)

        # idle로 복귀
        assert processor._state == "idle"

        # 응답 메시지 확인
        chat_messages = [m for m in collected if m.get("type") == "chat_message"]
        assert len(chat_messages) >= 1
        assert chat_messages[-1]["content"] == "안녕하세요!"

    def test_submit_during_processing_adds_to_pending(self, tmp_root, messages):
        """처리 중 새 메시지가 pending에 추가되는지 확인한다."""
        collected, callback = messages

        # claude -p를 느리게 만들어서 cancel+merge 시나리오
        call_count = [0]

        def slow_communicate(timeout=None):
            call_count[0] += 1
            if call_count[0] == 1:
                # 첫 번째 호출은 느리게 — kill될 것
                time.sleep(2)
                return ("", "")
            else:
                # 두 번째 호출은 빠르게
                return (json.dumps({"intent": "conversation", "message": "합쳐진 응답"}), "")

        with mock.patch("scripts.web.web_chatbot.subprocess.Popen") as mock_popen:
            mock_proc = mock.MagicMock()
            mock_proc.communicate.side_effect = slow_communicate
            mock_proc.returncode = 0
            mock_proc.poll.return_value = None

            def kill_side_effect():
                mock_proc.returncode = -9  # SIGKILL

            mock_proc.kill.side_effect = kill_side_effect
            mock_popen.return_value = mock_proc

            processor = ChatProcessor(tmp_root, "test-session", callback)
            processor.submit_message("메시지 A")
            time.sleep(0.1)  # 처리 시작 대기

            # 처리 중에 새 메시지 전송
            processor.submit_message("메시지 B")

            # kill이 호출되었는지 확인
            time.sleep(0.5)
            mock_proc.kill.assert_called()

    def test_session_history_persists(self, tmp_root, messages):
        """세션 히스토리가 파일에 저장되는지 확인한다."""
        collected, callback = messages

        mock_response = json.dumps({"intent": "conversation", "message": "응답입니다."})
        with mock.patch("scripts.web.web_chatbot.subprocess.Popen") as mock_popen:
            mock_proc = mock.MagicMock()
            mock_proc.communicate.return_value = (mock_response, "")
            mock_proc.returncode = 0
            mock_proc.poll.return_value = None
            mock_popen.return_value = mock_proc

            processor = ChatProcessor(tmp_root, "test-persist", callback)
            processor.submit_message("테스트")
            time.sleep(0.5)

        # 세션 파일 확인
        session_path = os.path.join(tmp_root, "session_history", "web", "test-persist.json")
        assert os.path.isfile(session_path)

        with open(session_path) as f:
            data = json.load(f)
        assert len(data["history"]) >= 2  # user + assistant


# ═══════════════════════════════════════════════════════════
# Confirmation Flow 테스트
# ═══════════════════════════════════════════════════════════

class TestConfirmationFlow:
    """확인 흐름 테스트."""

    def test_high_risk_action_triggers_confirmation(self, tmp_root, messages):
        """고위험 action이 확인 요청을 트리거하는지 확인한다."""
        collected, callback = messages

        mock_response = json.dumps({
            "intent": "action",
            "action": "submit",
            "project": "test-project",
            "params": {"title": "새 task"},
            "explanation": "task를 생성합니다.",
        })

        with mock.patch("scripts.web.web_chatbot.subprocess.Popen") as mock_popen:
            mock_proc = mock.MagicMock()
            mock_proc.communicate.return_value = (mock_response, "")
            mock_proc.returncode = 0
            mock_proc.poll.return_value = None
            mock_popen.return_value = mock_proc

            processor = ChatProcessor(tmp_root, "test-confirm", callback)
            processor.submit_message("task 생성해줘")
            time.sleep(0.5)

        # awaiting_confirmation 상태
        assert processor._state == "awaiting_confirmation"

        # 확인 카드가 전송되었는지 확인
        confirmations = [m for m in collected
                         if m.get("type") == "chat_message" and m.get("confirmation")]
        assert len(confirmations) == 1
        assert confirmations[0]["action_details"]["action"] == "submit"

    def test_confirmation_yes_executes_action(self, tmp_root, messages):
        """확인에 '확인'으로 답하면 action이 실행되는지 확인한다."""
        collected, callback = messages

        # feedback은 LOW_RISK이므로 always_confirm에서만 확인 필요
        mock_response = json.dumps({
            "intent": "action",
            "action": "feedback",
            "project": "test-project",
            "params": {"task_id": "00001", "message": "좋아요"},
            "explanation": "피드백 전송",
        })

        with mock.patch("scripts.web.web_chatbot.subprocess.Popen") as mock_popen:
            mock_proc = mock.MagicMock()
            mock_proc.communicate.return_value = (mock_response, "")
            mock_proc.returncode = 0
            mock_proc.poll.return_value = None
            mock_popen.return_value = mock_proc

            # confirmation_mode를 always_confirm으로 설정
            processor = ChatProcessor(tmp_root, "test-yes", callback)
            processor._confirmation_mode = "always_confirm"
            processor.submit_message("피드백 보내줘")
            time.sleep(0.5)

            assert processor._state == "awaiting_confirmation"

            # "확인" 응답
            with mock.patch.object(processor, "_execute_action") as mock_exec:
                processor.submit_message("확인")
                time.sleep(0.5)
                mock_exec.assert_called_once()

        assert processor._state == "idle"

    def test_confirmation_no_cancels(self, tmp_root, messages):
        """확인에 '취소'로 답하면 취소되는지 확인한다."""
        collected, callback = messages

        mock_response = json.dumps({
            "intent": "action",
            "action": "submit",
            "project": "test-project",
            "params": {"title": "test"},
            "explanation": "test",
        })

        with mock.patch("scripts.web.web_chatbot.subprocess.Popen") as mock_popen:
            mock_proc = mock.MagicMock()
            mock_proc.communicate.return_value = (mock_response, "")
            mock_proc.returncode = 0
            mock_proc.poll.return_value = None
            mock_popen.return_value = mock_proc

            processor = ChatProcessor(tmp_root, "test-no", callback)
            processor.submit_message("task 생성해줘")
            time.sleep(0.5)

            assert processor._state == "awaiting_confirmation"

            # "취소" 응답
            processor.submit_message("취소")
            time.sleep(0.5)

        assert processor._state == "idle"

        # "취소되었습니다" 메시지 확인
        cancel_msgs = [m for m in collected
                       if m.get("type") == "chat_message" and "취소" in m.get("content", "")]
        assert len(cancel_msgs) >= 1


# ═══════════════════════════════════════════════════════════
# 세션 레지스트리 테스트
# ═══════════════════════════════════════════════════════════

class TestSessionRegistry:
    """세션 레지스트리 테스트."""

    def test_get_or_create_new_session(self, tmp_root, messages):
        """새 세션이 생성되는지 확인한다."""
        _, callback = messages
        processor = get_or_create_session(tmp_root, None, callback)
        assert processor.session_id is not None
        assert processor.session_id in _active_sessions

    def test_get_existing_session(self, tmp_root, messages):
        """기존 세션을 반환하는지 확인한다."""
        _, callback = messages
        p1 = get_or_create_session(tmp_root, "existing-id", callback)
        p2 = get_or_create_session(tmp_root, "existing-id", callback)
        assert p1 is p2

    def test_remove_session(self, tmp_root, messages):
        """세션 제거를 확인한다."""
        _, callback = messages
        p = get_or_create_session(tmp_root, "to-remove", callback)
        assert "to-remove" in _active_sessions
        remove_session("to-remove")
        assert "to-remove" not in _active_sessions

    def test_broadcast_system_event(self, tmp_root, messages):
        """시스템 이벤트가 모든 세션에 전달되는지 확인한다."""
        collected1 = []
        collected2 = []

        p1 = get_or_create_session(tmp_root, "session-1", lambda e: collected1.append(e))
        p2 = get_or_create_session(tmp_root, "session-2", lambda e: collected2.append(e))

        event = {
            "type": "notification",
            "event_type": "task_completed",
            "project": "test",
            "task_id": "00001",
        }
        broadcast_system_event(event)

        # 양쪽 세션 모두에 메시지 전달 확인
        assert any(m.get("type") == "chat_message" for m in collected1)
        assert any(m.get("type") == "chat_message" for m in collected2)


# ═══════════════════════════════════════════════════════════
# inject_system_event 테스트
# ═══════════════════════════════════════════════════════════

class TestInjectSystemEvent:
    """시스템 이벤트 주입 테스트."""

    def test_inject_adds_to_history(self, tmp_root, messages):
        """시스템 이벤트가 히스토리에 추가되는지 확인한다."""
        collected, callback = messages
        processor = ChatProcessor(tmp_root, "test-inject", callback)

        event = {
            "event_type": "pr_created",
            "project": "my-proj",
            "task_id": "00005",
            "message": "PR 생성됨: 기능 추가",
        }
        processor.inject_system_event(event)

        # 히스토리에 system 메시지 추가 확인
        assert len(processor.conversation_history) == 1
        assert processor.conversation_history[0]["role"] == "system"
        assert "PR 생성됨" in processor.conversation_history[0]["content"]

        # on_message 콜백 호출 확인
        assert len(collected) == 1
        assert collected[0]["type"] == "chat_message"
        assert collected[0]["role"] == "system"


# ═══════════════════════════════════════════════════════════
# Typing indicator 테스트
# ═══════════════════════════════════════════════════════════

class TestTypingIndicator:
    """Typing indicator 이벤트 테스트."""

    def test_typing_events_emitted(self, tmp_root, messages):
        """처리 중 typing 이벤트가 발행되는지 확인한다."""
        collected, callback = messages

        mock_response = json.dumps({"intent": "conversation", "message": "응답"})
        with mock.patch("scripts.web.web_chatbot.subprocess.Popen") as mock_popen:
            mock_proc = mock.MagicMock()
            mock_proc.communicate.return_value = (mock_response, "")
            mock_proc.returncode = 0
            mock_proc.poll.return_value = None
            mock_popen.return_value = mock_proc

            processor = ChatProcessor(tmp_root, "test-typing", callback)
            processor.submit_message("테스트")
            time.sleep(0.5)

        typing_events = [m for m in collected if m.get("type") == "chat_typing"]
        # typing start + typing end
        assert len(typing_events) >= 2
        assert typing_events[0]["active"] is True
        assert typing_events[-1]["active"] is False
