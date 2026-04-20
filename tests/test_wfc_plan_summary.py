"""workflow_controller._build_plan_summary 단위 테스트.

plan.json → 알림 채널(Telegram/Web)에 실을 요약 dict 변환 로직의 경계를 검증한다.
"""

import json
import os

import pytest

from workflow_controller import (
    _build_plan_summary,
    _PLAN_SUMMARY_STRATEGY_NOTE_LIMIT,
    _PLAN_SUMMARY_SUBTASK_RESPONSIBILITY_LIMIT,
    _PLAN_SUMMARY_SUBTASK_TITLE_LIMIT,
    _PLAN_SUMMARY_MAX_TOTAL_CHARS,
)


def _write_plan(tmp_path, data):
    """임시 plan.json 파일을 만들고 경로를 반환."""
    path = os.path.join(str(tmp_path), "plan.json")
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False)
    return path


def test_summary_includes_strategy_and_subtasks(tmp_path):
    """정상 plan에서 strategy_note와 subtask 목록이 모두 요약에 포함된다."""
    plan = {
        "strategy_note": "간단한 전략",
        "subtasks": [
            {"subtask_id": "x-1", "title": "A",
             "primary_responsibility": "aa"},
            {"subtask_id": "x-2", "title": "B",
             "primary_responsibility": "bb"},
        ],
    }
    summary = _build_plan_summary(_write_plan(tmp_path, plan))
    assert summary is not None
    assert summary["strategy_note"] == "간단한 전략"
    assert summary["total_subtasks"] == 2
    assert [s["subtask_id"] for s in summary["subtasks"]] == ["x-1", "x-2"]
    assert summary["subtasks"][0]["responsibility"] == "aa"
    assert summary["subtasks"][1]["title"] == "B"


def test_summary_truncates_long_strategy_note(tmp_path):
    """길이 제한을 초과한 strategy_note는 말줄임 처리된다."""
    plan = {
        "strategy_note": "가" * (_PLAN_SUMMARY_STRATEGY_NOTE_LIMIT * 3),
        "subtasks": [],
    }
    summary = _build_plan_summary(_write_plan(tmp_path, plan))
    assert summary is not None
    assert len(summary["strategy_note"]) <= _PLAN_SUMMARY_STRATEGY_NOTE_LIMIT
    assert summary["strategy_note"].endswith("…")


def test_summary_truncates_long_responsibility(tmp_path):
    """각 subtask의 responsibility도 개별 제한으로 잘린다."""
    plan = {
        "strategy_note": "",
        "subtasks": [{
            "subtask_id": "x-1",
            "title": "A",
            "primary_responsibility": "가" * 1000,
        }],
    }
    summary = _build_plan_summary(_write_plan(tmp_path, plan))
    entry = summary["subtasks"][0]
    assert len(entry["responsibility"]) <= _PLAN_SUMMARY_SUBTASK_RESPONSIBILITY_LIMIT
    assert entry["responsibility"].endswith("…")


def test_summary_truncates_trailing_subtasks_when_budget_exceeded(tmp_path):
    """subtask가 너무 많거나 내용이 길면 꼬리 항목을 truncated 마커로 축약한다."""
    plan = {
        "strategy_note": "",
        "subtasks": [
            {"subtask_id": f"id-{i}", "title": f"t{i}",
             "primary_responsibility": "r" * _PLAN_SUMMARY_SUBTASK_RESPONSIBILITY_LIMIT}
            for i in range(50)
        ],
    }
    summary = _build_plan_summary(_write_plan(tmp_path, plan))
    assert summary["total_subtasks"] == 50
    # 총량이 예산 안에 들어와야 한다
    total_chars = sum(
        len(s.get("title", "")) + len(s.get("responsibility", ""))
        for s in summary["subtasks"]
    )
    assert total_chars <= _PLAN_SUMMARY_MAX_TOTAL_CHARS + 100
    # 꼬리 항목은 truncated 마커
    assert summary["subtasks"][-1].get("truncated") is True
    assert "외" in summary["subtasks"][-1]["title"]


def test_summary_returns_none_for_missing_file(tmp_path):
    """없거나 빈 경로는 None을 돌려 호출측이 '요약 없이 알림'으로 폴백하도록 한다."""
    assert _build_plan_summary(os.path.join(str(tmp_path), "none.json")) is None
    assert _build_plan_summary(None) is None
    assert _build_plan_summary("") is None


def test_summary_returns_none_for_non_dict(tmp_path):
    """plan.json이 dict가 아니면 None."""
    path = os.path.join(str(tmp_path), "plan.json")
    with open(path, "w") as f:
        f.write("[1, 2, 3]")
    assert _build_plan_summary(path) is None


def test_summary_handles_missing_subtasks_field(tmp_path):
    """subtasks 필드가 없어도 동작한다 (subtasks=[], total_subtasks=0)."""
    plan = {"strategy_note": "전략만 있음"}
    summary = _build_plan_summary(_write_plan(tmp_path, plan))
    assert summary is not None
    assert summary["subtasks"] == []
    assert summary["total_subtasks"] == 0
