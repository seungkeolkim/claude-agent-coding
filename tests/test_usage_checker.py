"""
usage_checker.py 단위 테스트.

check_threshold 로직, 파싱 로직 검증.
PTY 기반 get_usage()는 실제 claude CLI가 필요하므로 mock 처리.
"""

from unittest.mock import patch

from usage_checker import check_threshold, _clean_ansi


class TestCleanAnsi:
    """ANSI 이스케이프 시퀀스 제거 함수 테스트."""

    def test_removes_color_codes(self):
        """색상 코드가 제거된다."""
        text = "\033[0;32mGreen\033[0m Normal"
        result = _clean_ansi(text)
        assert result == "Green Normal"

    def test_removes_cursor_movement(self):
        """커서 이동 코드가 제거된다."""
        text = "\033[2J\033[HHello"
        result = _clean_ansi(text)
        assert result == "Hello"

    def test_plain_text_unchanged(self):
        """일반 텍스트는 변경되지 않는다."""
        text = "Hello World 123"
        result = _clean_ansi(text)
        assert result == "Hello World 123"

    def test_control_chars_removed(self):
        """제어 문자가 제거된다."""
        text = "Hello\x00\x01\x02World"
        result = _clean_ansi(text)
        assert result == "HelloWorld"


class TestCheckThreshold:
    """check_threshold 로직 테스트 (get_usage를 mock)."""

    @patch("usage_checker.get_usage")
    def test_below_threshold_allowed(self, mock_get_usage):
        """사용률이 threshold 미만이면 allowed=True."""
        mock_get_usage.return_value = {
            "session_percent": 50,
            "week_percent": 30,
            "session_resets": "2pm",
            "week_resets": "Apr 8",
            "raw_output": "",
        }
        allowed, usage = check_threshold(0.80)
        assert allowed is True
        assert usage["session_percent"] == 50

    @patch("usage_checker.get_usage")
    def test_above_threshold_blocked(self, mock_get_usage):
        """사용률이 threshold 이상이면 allowed=False."""
        mock_get_usage.return_value = {
            "session_percent": 85,
            "week_percent": 60,
            "session_resets": "3pm",
            "week_resets": "Apr 9",
            "raw_output": "",
        }
        allowed, usage = check_threshold(0.80)
        assert allowed is False
        assert usage["session_percent"] == 85

    @patch("usage_checker.get_usage")
    def test_exact_threshold_blocked(self, mock_get_usage):
        """사용률이 threshold와 정확히 같으면 차단 (< 비교)."""
        mock_get_usage.return_value = {
            "session_percent": 80,
            "raw_output": "",
        }
        allowed, _ = check_threshold(0.80)
        assert allowed is False

    @patch("usage_checker.get_usage")
    def test_query_failure_allows(self, mock_get_usage):
        """조회 실패 시 보수적으로 허용 (작업 차단 방지)."""
        mock_get_usage.return_value = {
            "session_percent": None,
            "week_percent": None,
            "raw_output": "",
            "error": "timeout",
        }
        allowed, usage = check_threshold(0.80)
        assert allowed is True

    @patch("usage_checker.get_usage")
    def test_three_tier_thresholds(self, mock_get_usage):
        """3계층 threshold가 각각 올바르게 동작한다."""
        mock_get_usage.return_value = {
            "session_percent": 75,
            "raw_output": "",
        }

        # new_task (0.70) → 75% >= 70% → 차단
        allowed, _ = check_threshold(0.70)
        assert allowed is False

        # new_subtask (0.80) → 75% < 80% → 통과
        allowed, _ = check_threshold(0.80)
        assert allowed is True

        # new_agent_stage (0.90) → 75% < 90% → 통과
        allowed, _ = check_threshold(0.90)
        assert allowed is True
