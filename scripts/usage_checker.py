"""
Usage Checker — Claude Code 세션 사용량 조회 모듈.

인터랙티브 claude 세션을 PTY로 열고 /usage 명령으로 rate limit 정보를 캡처한다.

사용법 (모듈):
    from usage_checker import get_usage
    result = get_usage()
    # {"session_percent": 10, "week_percent": 15, "session_resets": "1:59pm", ...}

사용법 (CLI):
    python3 scripts/usage_checker.py
"""

import os
import pty
import re
import select
import signal
import subprocess
import sys
import time


def get_usage(timeout_seconds=10):
    """
    Claude Code의 현재 사용량을 조회한다.

    인터랙티브 세션을 PTY로 열고, /usage 명령의 출력을 파싱하여
    세션/주간 사용률(%)과 리셋 시각을 반환한다.

    Args:
        timeout_seconds: 전체 프로세스 타임아웃 (기본 10초)

    Returns:
        dict: {
            "session_percent": int|None,
            "session_resets": str|None,
            "week_percent": int|None,
            "week_resets": str|None,
            "raw_output": str,
        }
        실패 시 percent 값이 None.
    """
    master_fd = None
    proc = None
    result = {
        "session_percent": None,
        "session_resets": None,
        "week_percent": None,
        "week_resets": None,
        "raw_output": "",
    }

    try:
        # PTY로 인터랙티브 claude 프로세스 열기
        master_fd, slave_fd = pty.openpty()
        proc = subprocess.Popen(
            ["claude", "--no-chrome"],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            preexec_fn=os.setsid,
        )
        os.close(slave_fd)
        slave_fd = None

        # 초기 로드 대기 + 버퍼 비우기
        _read_all(master_fd, timeout_sec=3)

        # /usage 전송
        os.write(master_fd, b"/usage\r")

        # 출력 대기 (사용자 피드백: 1초 이내에 뜨지만 여유 3초)
        time.sleep(3)
        raw_bytes = _read_all(master_fd, timeout_sec=1)
        raw_text = raw_bytes.decode("utf-8", errors="replace")
        clean_text = _clean_ansi(raw_text)
        result["raw_output"] = clean_text

        # 파싱: "10% used" 패턴 추출
        # Current session ... 11%usedReses2pm (Asia/Seoul)
        # Current week  ... 15%usedResets Apr 8, 11am (Asia/Seoul)
        # TUI 렌더링으로 "Resets"가 "Reses" 등으로 깨질 수 있음
        percent_matches = re.findall(r"(\d+)%\s*used", clean_text)
        # "Reses2pm" 같이 공백 없이 붙는 경우 처리
        resets_matches = re.findall(
            r"Rese(?:ts|s)(\S*)\s*(.*?)(?=Current|Extra|Esc|$)", clean_text,
        )
        # 각 매치: (붙은문자열, 나머지) → 합쳐서 resets 정보
        resets_values = [
            (f"{g1} {g2}").strip() for g1, g2 in resets_matches
        ]

        if len(percent_matches) >= 1:
            result["session_percent"] = int(percent_matches[0])
        if len(percent_matches) >= 2:
            result["week_percent"] = int(percent_matches[1])

        if len(resets_values) >= 1:
            result["session_resets"] = resets_values[0]
        if len(resets_values) >= 2:
            result["week_resets"] = resets_values[1]

        # /exit 전송
        os.write(master_fd, b"\x1b")  # Esc로 Usage 탭 닫기
        time.sleep(0.3)
        os.write(master_fd, b"/exit\r")
        time.sleep(1)

    except Exception as e:
        result["error"] = str(e)

    finally:
        # 리소스 정리: fd 닫기 + 프로세스 종료 + 좀비 방지
        _cleanup(master_fd, proc, timeout_seconds)

    return result


def check_threshold(threshold_percent, timeout_seconds=10):
    """
    현재 세션 사용률이 threshold 이하인지 확인한다.

    Args:
        threshold_percent: 허용 기준 (0.0~1.0). 예: 0.80 = 80%
        timeout_seconds: 조회 타임아웃

    Returns:
        tuple: (allowed: bool, usage: dict)
            allowed — 사용률이 threshold 이하이면 True
            usage — get_usage() 결과
    """
    usage = get_usage(timeout_seconds=timeout_seconds)
    session_pct = usage.get("session_percent")

    if session_pct is None:
        # 조회 실패 시 보수적으로 허용 (조회 실패로 작업 차단 방지)
        return True, usage

    threshold_as_int = int(threshold_percent * 100)
    allowed = session_pct < threshold_as_int
    return allowed, usage


def wait_until_below_threshold(threshold_percent, check_interval_seconds=60,
                               level_name="", log_fn=None):
    """
    사용률이 threshold 미만이 될 때까지 대기한다.

    이미 threshold 미만이면 즉시 반환.
    초과 시 check_interval_seconds마다 재확인하며 블로킹.

    Args:
        threshold_percent: 허용 기준 (0.0~1.0). 예: 0.80 = 80%
        check_interval_seconds: 재확인 주기 (초)
        level_name: 로그용 레벨명 (예: "new_task", "new_subtask")
        log_fn: 로그 출력 함수 (없으면 print)

    Returns:
        dict: 마지막 get_usage() 결과
    """
    if log_fn is None:
        log_fn = lambda msg: print(f"[usage] {msg}", flush=True)

    allowed, usage = check_threshold(threshold_percent)

    if allowed:
        pct = usage.get("session_percent", "?")
        log_fn(f"usage check 통과: {pct}% < {int(threshold_percent * 100)}% ({level_name})")
        return usage

    # threshold 초과 — 대기 루프
    pct = usage.get("session_percent", "?")
    log_fn(
        f"usage {pct}% >= {int(threshold_percent * 100)}% ({level_name}) "
        f"— {check_interval_seconds}초마다 재확인하며 대기"
    )

    while True:
        time.sleep(check_interval_seconds)
        allowed, usage = check_threshold(threshold_percent)
        pct = usage.get("session_percent", "?")

        if allowed:
            log_fn(f"usage check 통과: {pct}% < {int(threshold_percent * 100)}% ({level_name})")
            return usage

        resets = usage.get("session_resets", "?")
        log_fn(f"usage 여전히 {pct}% >= {int(threshold_percent * 100)}% — 계속 대기 (resets {resets})")


# ═══════════════════════════════════════════════════════════
# 내부 헬퍼
# ═══════════════════════════════════════════════════════════


def _read_all(fd, timeout_sec=2):
    """fd에서 읽을 수 있는 모든 데이터를 읽는다."""
    output = b""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        r, _, _ = select.select([fd], [], [], 0.2)
        if r:
            try:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                output += chunk
            except OSError:
                break
    return output


def _clean_ansi(text):
    """ANSI 이스케이프 시퀀스 및 제어 문자를 제거한다."""
    text = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", text)
    text = re.sub(r"\x1b\][^\x07\x1b]*[\x07]", "", text)
    text = re.sub(r"\x1b[()][AB012]", "", text)
    text = re.sub(r"\x1b[>=<]", "", text)
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)
    return text


def _cleanup(master_fd, proc, timeout_seconds=10):
    """
    PTY fd 닫기 + 프로세스 종료 + 좀비 프로세스 회수.
    """
    # master fd 닫기
    if master_fd is not None:
        try:
            os.close(master_fd)
        except OSError:
            pass

    if proc is None:
        return

    pid = proc.pid

    # 정상 종료 대기
    try:
        proc.wait(timeout=3)
        return
    except subprocess.TimeoutExpired:
        pass

    # SIGTERM → 프로세스 그룹 전체
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass

    try:
        proc.wait(timeout=3)
        return
    except subprocess.TimeoutExpired:
        pass

    # SIGKILL 강제 종료
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass

    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass

    # 좀비 회수 최종 시도
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass


# ═══════════════════════════════════════════════════════════
# CLI 진입점
# ═══════════════════════════════════════════════════════════


def main():
    """CLI로 사용량을 조회한다."""
    usage = get_usage()

    session_pct = usage.get("session_percent")
    week_pct = usage.get("week_percent")
    session_resets = usage.get("session_resets", "?")
    week_resets = usage.get("week_resets", "?")

    if session_pct is not None:
        print(f"Session: {session_pct}% used (resets {session_resets})")
    else:
        print("Session: 조회 실패")

    if week_pct is not None:
        print(f"Week:    {week_pct}% used (resets {week_resets})")
    else:
        print("Week:    조회 실패")

    if "error" in usage:
        print(f"Error:   {usage['error']}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
