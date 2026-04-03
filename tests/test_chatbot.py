"""
Chatbot + Protocol 레이어 테스트.

테스트 대상:
  1. parse_claude_response — Claude 응답 JSON 추출
  2. needs_confirmation — 확인 정책 로직
  3. format_confirmation_prompt — 확인 메시지 생성
  4. format_response_for_display — 결과 포맷팅
  5. load_chatbot_config — config.yaml에서 chatbot 섹션 로드
  6. Protocol dispatch — Request/Response envelope 통한 HubAPI 호출
  7. HubAPI 보강 — get_task, mark_notification_read, source 파라미터
  8. Action 분류 일관성 — 모든 action이 분류에 포함되어 있는지
  9. Session 관리 — 생성, 저장, 로드, 목록 조회
"""

import json
import os

import pytest

from chatbot import (
    HIGH_RISK_ACTIONS,
    LOW_RISK_ACTIONS,
    READ_ONLY_ACTIONS,
    format_confirmation_prompt,
    format_response_for_display,
    generate_session_id,
    list_sessions,
    load_chatbot_config,
    load_session,
    needs_confirmation,
    parse_claude_response,
    save_session,
)
from hub_api.core import HubAPI
from hub_api.protocol import (
    ACTION_REGISTRY,
    ErrorCode,
    Request,
    Response,
    dispatch,
    get_action_descriptions,
)


# ═══════════════════════════════════════════════════════════
# parse_claude_response
# ═══════════════════════════════════════════════════════════


class TestParseClaude:
    """Claude 응답 파싱 테스트."""

    def test_json_block_extraction(self):
        """```json 블록에서 JSON을 추출한다."""
        raw = """설명 텍스트
```json
{"intent": "action", "action": "submit", "project": "my-app", "params": {"title": "test"}}
```
뒤따르는 텍스트"""
        result = parse_claude_response(raw)
        assert result["intent"] == "action"
        assert result["action"] == "submit"
        assert result["params"]["title"] == "test"

    def test_raw_json(self):
        """전체가 JSON인 경우도 파싱한다."""
        raw = '{"intent": "conversation", "message": "안녕하세요"}'
        result = parse_claude_response(raw)
        assert result["intent"] == "conversation"
        assert result["message"] == "안녕하세요"

    def test_fallback_to_conversation(self):
        """JSON이 없으면 conversation으로 처리한다."""
        raw = "그냥 일반 텍스트입니다"
        result = parse_claude_response(raw)
        assert result["intent"] == "conversation"
        assert result["message"] == raw

    def test_intent_correction_action_name_as_intent(self):
        """intent에 action 이름이 들어오면 "action"으로 보정한다."""
        raw = '```json\n{"intent": "approve", "action": "approve", "project": "my-app", "params": {"task_id": "00001"}}\n```'
        result = parse_claude_response(raw)
        assert result["intent"] == "action"
        assert result["action"] == "approve"

    def test_intent_correction_unknown_stays(self):
        """action이 없는 알 수 없는 intent는 보정하지 않는다."""
        raw = '```json\n{"intent": "unknown_thing", "message": "뭔가"}\n```'
        result = parse_claude_response(raw)
        assert result["intent"] == "unknown_thing"

    def test_malformed_json_block(self):
        """```json 블록 안의 JSON이 깨져있으면 fallback."""
        raw = '```json\n{broken json\n```'
        result = parse_claude_response(raw)
        assert result["intent"] == "conversation"

    def test_clarification_intent(self):
        """clarification intent도 정상 파싱한다."""
        raw = '```json\n{"intent": "clarification", "message": "어떤 프로젝트요?"}\n```'
        result = parse_claude_response(raw)
        assert result["intent"] == "clarification"
        assert "프로젝트" in result["message"]

    def test_empty_input(self):
        """빈 입력도 처리한다."""
        result = parse_claude_response("")
        assert result["intent"] == "conversation"


# ═══════════════════════════════════════════════════════════
# needs_confirmation
# ═══════════════════════════════════════════════════════════


class TestConfirmation:
    """확인 정책 로직 테스트."""

    def test_readonly_always_skips(self):
        """조회성 action은 어떤 모드에서든 확인 불필요."""
        for action in READ_ONLY_ACTIONS:
            assert needs_confirmation(action, "always_confirm") is False
            assert needs_confirmation(action, "smart") is False
            assert needs_confirmation(action, "never_confirm") is False

    def test_never_confirm_skips_all(self):
        """never_confirm 모드는 모든 action에서 확인 불필요."""
        for action in HIGH_RISK_ACTIONS | LOW_RISK_ACTIONS:
            assert needs_confirmation(action, "never_confirm") is False

    def test_always_confirm_requires_all_mutating(self):
        """always_confirm 모드는 모든 실행성 action에서 확인 필요."""
        for action in HIGH_RISK_ACTIONS | LOW_RISK_ACTIONS:
            assert needs_confirmation(action, "always_confirm") is True

    def test_smart_high_risk_confirms(self):
        """smart 모드에서 고위험 action은 확인 필요."""
        for action in HIGH_RISK_ACTIONS:
            assert needs_confirmation(action, "smart") is True

    def test_smart_low_risk_skips(self):
        """smart 모드에서 저위험 action은 확인 불필요."""
        for action in LOW_RISK_ACTIONS:
            assert needs_confirmation(action, "smart") is False


# ═══════════════════════════════════════════════════════════
# format 함수
# ═══════════════════════════════════════════════════════════


class TestFormat:
    """포맷팅 함수 테스트."""

    def test_confirmation_prompt_contains_action(self):
        """확인 프롬프트에 action 이름이 포함된다."""
        parsed = {
            "action": "submit",
            "project": "my-app",
            "params": {"title": "로그인 구현"},
            "explanation": "task를 제출합니다.",
        }
        result = format_confirmation_prompt(parsed)
        assert "submit" in result
        assert "my-app" in result
        assert "로그인 구현" in result

    def test_confirmation_prompt_without_project(self):
        """project 없는 action도 포맷팅 가능하다."""
        parsed = {"action": "status", "params": {}}
        result = format_confirmation_prompt(parsed)
        assert "status" in result

    def test_confirmation_prompt_long_value_truncated(self):
        """80자 초과 값은 잘린다."""
        parsed = {
            "action": "submit",
            "params": {"description": "x" * 100},
        }
        result = format_confirmation_prompt(parsed)
        assert "..." in result

    def test_display_success(self):
        """성공 응답 포맷에 '완료'가 포함된다."""
        resp = Response(success=True, data=None, message="task 00001 제출 완료")
        result = format_response_for_display(resp, "submit")
        assert "완료" in result

    def test_display_error(self):
        """에러 응답 포맷에 '오류'가 포함된다."""
        resp = Response(
            success=False,
            error={"code": "TASK_NOT_FOUND", "message": "task 없음"},
            message="task 없음",
        )
        result = format_response_for_display(resp, "get_task")
        assert "오류" in result


# ═══════════════════════════════════════════════════════════
# load_chatbot_config
# ═══════════════════════════════════════════════════════════


class TestChatbotConfig:
    """chatbot 설정 로드 테스트."""

    def test_default_when_no_config(self, tmp_path):
        """config.yaml이 없으면 기본값 반환."""
        config = load_chatbot_config(str(tmp_path))
        assert config["confirmation_mode"] == "smart"

    def test_load_from_config_yaml(self, tmp_path):
        """config.yaml에서 chatbot 섹션을 읽는다."""
        import yaml
        config_data = {
            "chatbot": {"confirmation_mode": "always_confirm"},
        }
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump(config_data))

        config = load_chatbot_config(str(tmp_path))
        assert config["confirmation_mode"] == "always_confirm"

    def test_default_when_no_chatbot_section(self, tmp_path):
        """config.yaml에 chatbot 섹션이 없으면 기본값."""
        import yaml
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump({"logging": {"level": "info"}}))

        config = load_chatbot_config(str(tmp_path))
        assert config["confirmation_mode"] == "smart"


# ═══════════════════════════════════════════════════════════
# Action 분류 일관성
# ═══════════════════════════════════════════════════════════


class TestActionClassification:
    """Action 분류가 ACTION_REGISTRY와 일관되는지 확인."""

    def test_all_registry_actions_classified(self):
        """ACTION_REGISTRY의 모든 action이 분류에 포함되어 있다."""
        all_classified = READ_ONLY_ACTIONS | HIGH_RISK_ACTIONS | LOW_RISK_ACTIONS
        for action in ACTION_REGISTRY:
            assert action in all_classified, f"action '{action}'이 분류에 없음"

    def test_no_overlap(self):
        """분류 간 겹침이 없다."""
        assert not (READ_ONLY_ACTIONS & HIGH_RISK_ACTIONS)
        assert not (READ_ONLY_ACTIONS & LOW_RISK_ACTIONS)
        assert not (HIGH_RISK_ACTIONS & LOW_RISK_ACTIONS)


# ═══════════════════════════════════════════════════════════
# Protocol dispatch
# ═══════════════════════════════════════════════════════════


class TestProtocolDispatch:
    """Protocol dispatch를 통한 HubAPI 호출 테스트."""

    def test_submit_via_dispatch(self, test_project, agent_hub_root):
        """dispatch로 task를 제출할 수 있다."""
        api = HubAPI(agent_hub_root)
        req = Request(
            action="submit",
            project=test_project["name"],
            params={"title": "프로토콜 테스트", "description": "dispatch 경유"},
            source="chatbot",
        )
        resp = dispatch(api, req)
        assert resp.success is True
        assert resp.data.task_id == "00001"
        assert "제출 완료" in resp.message

    def test_submit_source_recorded(self, test_project, agent_hub_root):
        """submit 시 source가 task JSON에 기록된다."""
        api = HubAPI(agent_hub_root)
        req = Request(
            action="submit",
            project=test_project["name"],
            params={"title": "source 테스트"},
            source="chatbot",
        )
        resp = dispatch(api, req)
        task = api.get_task(test_project["name"], resp.data.task_id)
        assert task["submitted_via"] == "chatbot"

    def test_list_via_dispatch(self, test_project, agent_hub_root):
        """dispatch로 task 목록을 조회할 수 있다."""
        api = HubAPI(agent_hub_root)
        # task 하나 생성
        api.submit(test_project["name"], title="목록 테스트", description="")
        resp = dispatch(api, Request(action="list", project=test_project["name"]))
        assert resp.success is True
        assert len(resp.data) == 1

    def test_get_task_via_dispatch(self, test_project, agent_hub_root):
        """dispatch로 단건 task를 조회할 수 있다."""
        api = HubAPI(agent_hub_root)
        api.submit(test_project["name"], title="단건 조회 테스트", description="")
        resp = dispatch(api, Request(
            action="get_task",
            project=test_project["name"],
            params={"task_id": "00001"},
        ))
        assert resp.success is True
        assert resp.data["title"] == "단건 조회 테스트"

    def test_status_via_dispatch(self, agent_hub_root):
        """dispatch로 시스템 상태를 조회할 수 있다."""
        api = HubAPI(agent_hub_root)
        resp = dispatch(api, Request(action="status"))
        assert resp.success is True

    def test_invalid_action(self, agent_hub_root):
        """존재하지 않는 action은 에러를 반환한다."""
        api = HubAPI(agent_hub_root)
        resp = dispatch(api, Request(action="does_not_exist"))
        assert resp.success is False
        assert resp.error["code"] == ErrorCode.INVALID_ACTION

    def test_missing_project(self, agent_hub_root):
        """project 필수 action에서 project 누락 시 에러."""
        api = HubAPI(agent_hub_root)
        resp = dispatch(api, Request(action="submit", params={"title": "x"}))
        assert resp.success is False
        assert resp.error["code"] == ErrorCode.MISSING_PARAM

    def test_missing_required_param(self, test_project, agent_hub_root):
        """필수 파라미터 누락 시 에러."""
        api = HubAPI(agent_hub_root)
        resp = dispatch(api, Request(
            action="submit",
            project=test_project["name"],
            params={},
        ))
        assert resp.success is False
        assert resp.error["code"] == ErrorCode.MISSING_PARAM

    def test_project_not_found(self, agent_hub_root):
        """없는 프로젝트 접근 시 에러."""
        api = HubAPI(agent_hub_root)
        resp = dispatch(api, Request(
            action="submit",
            project="nonexistent-project",
            params={"title": "x"},
        ))
        assert resp.success is False
        assert resp.error["code"] == ErrorCode.PROJECT_NOT_FOUND

    def test_task_not_found(self, test_project, agent_hub_root):
        """없는 task 접근 시 에러."""
        api = HubAPI(agent_hub_root)
        resp = dispatch(api, Request(
            action="get_task",
            project=test_project["name"],
            params={"task_id": "99999"},
        ))
        assert resp.success is False
        assert resp.error["code"] == ErrorCode.TASK_NOT_FOUND

    def test_empty_action(self, agent_hub_root):
        """action이 비어있으면 에러."""
        api = HubAPI(agent_hub_root)
        resp = dispatch(api, Request(action=""))
        assert resp.success is False
        assert resp.error["code"] == ErrorCode.INVALID_ACTION


# ═══════════════════════════════════════════════════════════
# HubAPI 보강 메서드
# ═══════════════════════════════════════════════════════════


class TestHubAPIEnhancements:
    """get_task, mark_notification_read, source 파라미터 테스트."""

    def test_get_task(self, test_project, agent_hub_root):
        """get_task로 단건 task를 dict로 조회한다."""
        api = HubAPI(agent_hub_root)
        api.submit(test_project["name"], title="get_task 테스트", description="")
        task = api.get_task(test_project["name"], "00001")
        assert task["title"] == "get_task 테스트"
        assert task["status"] == "submitted"

    def test_get_task_not_found(self, test_project, agent_hub_root):
        """없는 task 조회 시 FileNotFoundError."""
        api = HubAPI(agent_hub_root)
        with pytest.raises(FileNotFoundError):
            api.get_task(test_project["name"], "99999")

    def test_submit_source_default(self, test_project, agent_hub_root):
        """source 미지정 시 기본값은 'cli'."""
        api = HubAPI(agent_hub_root)
        result = api.submit(test_project["name"], title="기본 source", description="")
        task = api.get_task(test_project["name"], result.task_id)
        assert task["submitted_via"] == "cli"

    def test_submit_source_custom(self, test_project, agent_hub_root):
        """source를 지정하면 해당 값이 기록된다."""
        api = HubAPI(agent_hub_root)
        result = api.submit(test_project["name"], title="커스텀 source", description="", source="telegram")
        task = api.get_task(test_project["name"], result.task_id)
        assert task["submitted_via"] == "telegram"

    def test_mark_notification_read(self, test_project, agent_hub_root):
        """mark_notification_read가 정상 동작한다."""
        from notification import emit_notification, get_notifications

        emit_notification(
            test_project["dir"],
            event_type="task_completed",
            task_id="00001",
            message="완료",
        )

        # 읽기 전: unread
        notis = get_notifications(test_project["dir"], unread_only=True)
        assert len(notis) == 1

        # 읽음 처리
        api = HubAPI(agent_hub_root)
        api.mark_notification_read(test_project["name"])

        # 읽은 후: unread 0개
        notis_after = get_notifications(test_project["dir"], unread_only=True)
        assert len(notis_after) == 0


# ═══════════════════════════════════════════════════════════
# Response.to_dict
# ═══════════════════════════════════════════════════════════


class TestResponseSerialization:
    """Response.to_dict 직렬화 테스트."""

    def test_to_dict_basic(self):
        """기본 Response를 dict로 변환한다."""
        resp = Response(success=True, data={"key": "value"}, message="ok")
        d = resp.to_dict()
        assert d["success"] is True
        assert d["data"]["key"] == "value"

    def test_to_dict_with_dataclass(self, test_project, agent_hub_root):
        """dataclass data도 dict로 변환된다."""
        api = HubAPI(agent_hub_root)
        result = api.submit(test_project["name"], title="직렬화 테스트", description="")
        resp = Response(success=True, data=result, message="ok")
        d = resp.to_dict()
        assert d["data"]["task_id"] == "00001"

    def test_to_dict_with_list(self, test_project, agent_hub_root):
        """list[dataclass] data도 변환된다."""
        api = HubAPI(agent_hub_root)
        api.submit(test_project["name"], title="목록 직렬화", description="")
        tasks = api.list_tasks(project=test_project["name"])
        resp = Response(success=True, data=tasks, message="ok")
        d = resp.to_dict()
        assert isinstance(d["data"], list)
        assert d["data"][0]["task_id"] == "00001"

    def test_to_dict_error(self):
        """에러 Response도 dict로 변환된다."""
        resp = Response(
            success=False,
            error={"code": "TEST_ERROR", "message": "테스트 에러"},
            message="실패",
        )
        d = resp.to_dict()
        assert d["success"] is False
        assert d["error"]["code"] == "TEST_ERROR"


# ═══════════════════════════════════════════════════════════
# get_action_descriptions
# ═══════════════════════════════════════════════════════════


class TestActionDescriptions:
    """Chatbot 시스템 프롬프트용 action 설명 생성 테스트."""

    def test_contains_all_actions(self):
        """모든 등록된 action이 설명에 포함된다."""
        desc = get_action_descriptions()
        for action_name in ACTION_REGISTRY:
            assert action_name in desc, f"'{action_name}'이 설명에 없음"

    def test_contains_required_params(self):
        """필수 파라미터 정보가 포함된다."""
        desc = get_action_descriptions()
        assert "title" in desc  # submit의 필수 param
        assert "task_id" in desc  # approve의 필수 param

    def test_output_is_string(self):
        """문자열을 반환한다."""
        desc = get_action_descriptions()
        assert isinstance(desc, str)
        assert len(desc) > 100


# ═══════════════════════════════════════════════════════════
# Session 관리
# ═══════════════════════════════════════════════════════════


class TestSessionManagement:
    """세션 생성, 저장, 로드, 목록 조회 테스트."""

    def test_generate_session_id_format(self):
        """session_id가 YYYYMMDD_HHMMSS_xxxx 형식이다."""
        sid = generate_session_id()
        # 예: 20260403_143052_a3f1
        parts = sid.split("_")
        assert len(parts) == 3
        assert len(parts[0]) == 8  # YYYYMMDD
        assert len(parts[1]) == 6  # HHMMSS
        assert len(parts[2]) == 4  # 랜덤 4자

    def test_generate_session_id_unique(self):
        """연속 생성 시 다른 ID가 나온다."""
        ids = {generate_session_id() for _ in range(10)}
        assert len(ids) == 10

    def test_save_and_load_session(self, tmp_path):
        """세션을 저장하고 로드할 수 있다."""
        root = str(tmp_path)
        sid = "20260403_143052_a3f1"
        history = [
            {"role": "user", "content": "안녕"},
            {"role": "assistant", "content": "안녕하세요!"},
        ]
        save_session(root, sid, history)
        loaded = load_session(root, sid)
        assert loaded == history

    def test_load_nonexistent_session(self, tmp_path):
        """없는 세션 로드 시 None 반환."""
        loaded = load_session(str(tmp_path), "nonexistent_session_id")
        assert loaded is None

    def test_save_preserves_created_at(self, tmp_path):
        """여러 번 저장해도 created_at은 최초 값을 유지한다."""
        root = str(tmp_path)
        sid = "20260403_143052_a3f1"
        save_session(root, sid, [{"role": "user", "content": "1"}])

        # 최초 created_at 읽기
        session_path = os.path.join(root, "session_history", "chatbot", f"{sid}.json")
        with open(session_path) as f:
            first = json.load(f)
        first_created = first["created_at"]

        # 두 번째 저장
        save_session(root, sid, [
            {"role": "user", "content": "1"},
            {"role": "user", "content": "2"},
        ])
        with open(session_path) as f:
            second = json.load(f)
        assert second["created_at"] == first_created
        assert second["turn_count"] == 2

    def test_list_sessions_empty(self, tmp_path):
        """세션이 없으면 빈 리스트."""
        sessions = list_sessions(str(tmp_path))
        assert sessions == []

    def test_list_sessions_sorted(self, tmp_path):
        """세션 목록이 최신순으로 정렬된다."""
        root = str(tmp_path)
        save_session(root, "20260401_100000_aaaa", [{"role": "user", "content": "1"}])
        save_session(root, "20260403_100000_bbbb", [{"role": "user", "content": "2"}])
        save_session(root, "20260402_100000_cccc", [{"role": "user", "content": "3"}])

        sessions = list_sessions(root)
        ids = [s["session_id"] for s in sessions]
        assert ids == ["20260403_100000_bbbb", "20260402_100000_cccc", "20260401_100000_aaaa"]

    def test_session_file_structure(self, tmp_path):
        """저장된 세션 파일의 JSON 구조가 올바르다."""
        root = str(tmp_path)
        sid = "20260403_143052_a3f1"
        save_session(root, sid, [{"role": "user", "content": "테스트"}])

        session_path = os.path.join(root, "session_history", "chatbot", f"{sid}.json")
        with open(session_path) as f:
            data = json.load(f)

        assert data["session_id"] == sid
        assert data["frontend"] == "chatbot"
        assert "created_at" in data
        assert "updated_at" in data
        assert data["turn_count"] == 1
        assert len(data["history"]) == 1

    def test_save_custom_frontend(self, tmp_path):
        """frontend를 지정하면 해당 하위 디렉토리에 저장된다."""
        root = str(tmp_path)
        sid = "20260403_143052_a3f1"
        save_session(root, sid, [{"role": "user", "content": "슬랙"}], frontend="slack")

        session_path = os.path.join(root, "session_history", "slack", f"{sid}.json")
        assert os.path.isfile(session_path)

        loaded = load_session(root, sid, frontend="slack")
        assert loaded[0]["content"] == "슬랙"
