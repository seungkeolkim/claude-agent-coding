"""
check_safety_limits.py 단위 테스트.

4계층 limits 해소, 각 limit 항목별 통과/차단 검증.
"""

from datetime import datetime, timezone, timedelta

from check_safety_limits import check_limits, resolve_effective_limits


def _make_task(counters=None, submitted_at=None, completed_subtasks=None,
               current_subtask=None, config_override=None):
    """테스트용 task dict를 빌드한다."""
    if submitted_at is None:
        submitted_at = datetime.now(timezone.utc).isoformat()
    return {
        "task_id": "00001",
        "status": "in_progress",
        "submitted_at": submitted_at,
        "counters": counters or {
            "total_agent_invocations": 0,
            "replan_count": 0,
            "current_subtask_retry": 0,
        },
        "completed_subtasks": completed_subtasks or [],
        "current_subtask": current_subtask,
        "config_override": config_override or {},
    }


class TestResolveLimits:
    """4계층 config merge로 limits를 해소하는 로직."""

    def test_default_only(self):
        """config.yaml의 default_limits만 있을 때."""
        config = {"default_limits": {"max_retry_per_subtask": 5}}
        limits = resolve_effective_limits(config, {}, _make_task())
        assert limits["max_retry_per_subtask"] == 5

    def test_project_overrides_config(self):
        """project.yaml이 config.yaml을 override한다."""
        config = {"default_limits": {"max_retry_per_subtask": 5}}
        project_yaml = {"limits": {"max_retry_per_subtask": 2}}
        limits = resolve_effective_limits(config, project_yaml, _make_task())
        assert limits["max_retry_per_subtask"] == 2

    def test_task_overrides_project(self):
        """task.config_override가 project.yaml을 override한다."""
        config = {"default_limits": {"max_retry_per_subtask": 5}}
        project_yaml = {"limits": {"max_retry_per_subtask": 2}}
        task = _make_task(config_override={"limits": {"max_retry_per_subtask": 10}})
        limits = resolve_effective_limits(config, project_yaml, task)
        assert limits["max_retry_per_subtask"] == 10

    def test_partial_override(self):
        """일부 항목만 override하면 나머지는 기본값을 유지한다."""
        config = {"default_limits": {
            "max_retry_per_subtask": 3,
            "max_replan_count": 2,
            "max_total_agent_invocations": 30,
        }}
        project_yaml = {"limits": {"max_replan_count": 5}}
        limits = resolve_effective_limits(config, project_yaml, _make_task())
        assert limits["max_retry_per_subtask"] == 3   # 기본값 유지
        assert limits["max_replan_count"] == 5          # override됨
        assert limits["max_total_agent_invocations"] == 30


class TestCheckLimits:
    """각 safety limit 항목별 통과/차단 검증."""

    def test_all_within_limits(self):
        """모든 항목이 한계 내이면 에러 없음."""
        limits = {
            "max_total_agent_invocations": 30,
            "max_retry_per_subtask": 3,
            "max_replan_count": 2,
            "max_subtask_count": 5,
            "max_task_duration_hours": 4,
        }
        task = _make_task(counters={
            "total_agent_invocations": 5,
            "replan_count": 0,
            "current_subtask_retry": 1,
        })
        errors = check_limits(limits, task, "coder")
        assert errors == []

    def test_total_invocations_exceeded(self):
        """총 agent 호출 횟수 초과."""
        limits = {"max_total_agent_invocations": 10}
        task = _make_task(counters={"total_agent_invocations": 10})
        errors = check_limits(limits, task, "coder")
        assert len(errors) == 1
        assert "총 agent 호출 횟수 초과" in errors[0]

    def test_retry_exceeded(self):
        """subtask retry 횟수 초과."""
        limits = {"max_retry_per_subtask": 3}
        task = _make_task(counters={"current_subtask_retry": 3})
        errors = check_limits(limits, task, "coder")
        assert len(errors) == 1
        assert "subtask retry 횟수 초과" in errors[0]

    def test_replan_exceeded_for_planner(self):
        """replan 횟수 초과 — planner일 때만 차단."""
        limits = {"max_replan_count": 2}
        task = _make_task(counters={"replan_count": 2})

        # planner일 때 차단
        errors = check_limits(limits, task, "planner")
        assert len(errors) == 1
        assert "replan 횟수 초과" in errors[0]

        # coder일 때는 무관
        errors = check_limits(limits, task, "coder")
        assert errors == []

    def test_subtask_count_exceeded(self):
        """subtask 개수 초과."""
        limits = {"max_subtask_count": 3}
        task = _make_task(
            completed_subtasks=["s1", "s2", "s3"],
            current_subtask="s4",
        )
        errors = check_limits(limits, task, "coder")
        assert len(errors) == 1
        assert "subtask 개수 초과" in errors[0]

    def test_duration_exceeded(self):
        """task 지속 시간 초과."""
        limits = {"max_task_duration_hours": 1}
        old_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        task = _make_task(submitted_at=old_time)
        errors = check_limits(limits, task, "coder")
        assert len(errors) == 1
        assert "task 지속 시간 초과" in errors[0]

    def test_duration_within_limit(self):
        """task 지속 시간 이내."""
        limits = {"max_task_duration_hours": 4}
        task = _make_task()  # 방금 생성됨
        errors = check_limits(limits, task, "coder")
        # duration 관련 에러 없음
        duration_errors = [e for e in errors if "지속 시간" in e]
        assert duration_errors == []

    def test_multiple_limits_exceeded(self):
        """여러 한계가 동시에 초과되면 모두 보고된다."""
        limits = {
            "max_total_agent_invocations": 5,
            "max_retry_per_subtask": 2,
        }
        task = _make_task(counters={
            "total_agent_invocations": 10,
            "current_subtask_retry": 5,
        })
        errors = check_limits(limits, task, "coder")
        assert len(errors) == 2
