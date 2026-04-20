"""TelegramBridge의 '수정 피드백 대기' pending state 헬퍼 테스트.

TelegramBridge는 enabled=false 설정에서는 TelegramClient 없이도 인스턴스화되므로
이 테스트는 실제 네트워크/봇 토큰 없이 파일 I/O만 검증한다.
"""

import json
import os

import pytest
import yaml

from telegram_bridge import (
    TelegramBridge,
    _pending_modify_path,
    _PENDING_MODIFY_TTL_SECONDS,
)


@pytest.fixture
def bridge(tmp_path):
    """enabled=false로 TelegramBridge를 인스턴스화. client=None 상태라 네트워크 호출 불가."""
    root = str(tmp_path)
    os.makedirs(os.path.join(root, "data"), exist_ok=True)
    os.makedirs(os.path.join(root, "projects"), exist_ok=True)
    config_path = os.path.join(root, "config.yaml")
    with open(config_path, "w") as f:
        yaml.safe_dump({"telegram": {"enabled": False}}, f)
    return TelegramBridge(root, config_path)


def test_set_and_pop_roundtrip(bridge):
    """한 topic에 pending을 저장하고 pop하면 같은 데이터가 돌아오고, 두 번째 pop은 None."""
    bridge._set_pending_modify(chat_id=-100, thread_id=7,
                               project="my-app", task_id="00042",
                               prompt_message_id=99)

    popped = bridge._pop_pending_modify(chat_id=-100, thread_id=7)
    assert popped is not None
    assert popped["project"] == "my-app"
    assert popped["task_id"] == "00042"
    assert popped["prompt_message_id"] == 99

    assert bridge._pop_pending_modify(chat_id=-100, thread_id=7) is None


def test_pop_returns_none_when_missing(bridge):
    """저장된 적 없는 키는 None."""
    assert bridge._pop_pending_modify(chat_id=1, thread_id=2) is None


def test_pop_ignores_expired_entry(bridge, tmp_path):
    """TTL을 넘긴 항목은 pop해도 None (파일에서는 제거됨)."""
    path = _pending_modify_path(str(tmp_path))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({"pending": {"-100_7": {
            "project": "p",
            "task_id": "t",
            "requested_at": "2020-01-01T00:00:00+00:00",
            "prompt_message_id": None,
        }}}, f)

    assert bridge._pop_pending_modify(chat_id=-100, thread_id=7) is None
    # 두 번째 pop에서도 None (이미 제거됨)
    assert bridge._pop_pending_modify(chat_id=-100, thread_id=7) is None


def test_set_overwrites_existing_for_same_topic(bridge):
    """같은 topic에서 pending이 이미 있어도 덮어써야 한다 (사용자가 버튼을 다시 눌렀다는 뜻)."""
    bridge._set_pending_modify(-100, 7, "p1", "t1", 1)
    bridge._set_pending_modify(-100, 7, "p2", "t2", 2)
    popped = bridge._pop_pending_modify(-100, 7)
    assert popped["project"] == "p2"
    assert popped["task_id"] == "t2"


def test_key_normalizes_none_thread(bridge):
    """thread_id가 None이면 0으로 정규화되어 일관된 키를 만든다."""
    bridge._set_pending_modify(-100, None, "p", "t", None)
    assert bridge._pop_pending_modify(-100, None) is not None


def test_separate_topics_are_independent(bridge):
    """서로 다른 (chat_id, thread_id)는 독립적으로 저장/소비된다."""
    bridge._set_pending_modify(-100, 1, "pA", "tA", None)
    bridge._set_pending_modify(-100, 2, "pB", "tB", None)

    a = bridge._pop_pending_modify(-100, 1)
    assert a["task_id"] == "tA"
    # 다른 topic은 영향 없음
    b = bridge._pop_pending_modify(-100, 2)
    assert b["task_id"] == "tB"


def test_ttl_constant_is_positive():
    """설계 가정: TTL이 양수여야 동작 의미가 있다."""
    assert _PENDING_MODIFY_TTL_SECONDS > 0
