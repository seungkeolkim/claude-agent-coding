"""
Memory Updater 통합 테스트.

MemoryUpdater stage가 파이프라인에 올바르게 통합되었는지 검증한다:
1. STEP_NUMBER/STEP_NAME 상수에 memory_updater가 등록되어 있고 Summarizer 직전에 위치
2. init_project의 codebase 메모리 파일 생성 헬퍼가 독립적으로 동작
3. memory_updater agent prompt 파일이 존재
"""

import os

import pytest

# scripts/ 경로는 conftest.py에서 이미 sys.path에 추가된다.
from workflow_controller import STEP_NUMBER, STEP_NAME
import init_project


AGENT_HUB_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestStepNumbering:
    """파이프라인 단계 번호가 memory_updater → summarizer 순서를 반영한다."""

    def test_memory_updater_registered(self):
        """STEP_NUMBER/STEP_NAME에 memory_updater가 포함된다."""
        assert "memory_updater" in STEP_NUMBER
        assert "memory_updater" in STEP_NAME
        assert STEP_NAME["memory_updater"] == "memory-updater"

    def test_memory_updater_before_summarizer(self):
        """memory_updater 번호가 summarizer 번호보다 작다 (Summarizer 직전 실행)."""
        memory_step = int(STEP_NUMBER["memory_updater"])
        summarizer_step = int(STEP_NUMBER["summarizer"])
        assert memory_step < summarizer_step, (
            f"memory_updater({memory_step})는 summarizer({summarizer_step})보다 "
            "앞 번호를 가져야 합니다"
        )

    def test_summarizer_bumped_to_09(self):
        """Summarizer 단계가 08 → 09로 재조정되었다."""
        assert STEP_NUMBER["summarizer"] == "09"
        assert STEP_NUMBER["memory_updater"] == "08"

    def test_run_claude_agent_sh_matches(self):
        """run_claude_agent.sh의 step 매핑이 Python 측과 일치한다."""
        script_path = os.path.join(AGENT_HUB_ROOT, "scripts", "run_claude_agent.sh")
        content = open(script_path, encoding="utf-8").read()
        # shell case문에 memory_updater / summarizer가 올바른 번호로 들어갔는지 확인
        assert 'memory_updater) echo "08"' in content
        assert 'summarizer)     echo "09"' in content
        assert 'memory_updater) echo "memory-updater"' in content


class TestCodebaseMemoryFiles:
    """init_project의 codebase 메모리 파일 생성 헬퍼 검증."""

    def test_generates_both_files_when_absent(self, tmp_path):
        """두 파일이 모두 없으면 둘 다 새로 생성한다."""
        codebase = tmp_path / "fresh-codebase"
        codebase.mkdir()

        created = init_project.generate_codebase_memory_files(
            str(codebase), "sample-project", "샘플 설명입니다.",
        )

        assert created == {"project_notes": True, "claude_pointer": True}
        assert (codebase / "PROJECT_NOTES.md").exists()
        assert (codebase / "CLAUDE.md").exists()

        # 본문에 프로젝트명·설명이 들어간다
        notes = (codebase / "PROJECT_NOTES.md").read_text(encoding="utf-8")
        assert "sample-project" in notes
        assert "샘플 설명입니다." in notes
        # CLAUDE.md는 포인터 한 줄짜리
        claude_md = (codebase / "CLAUDE.md").read_text(encoding="utf-8")
        assert "PROJECT_NOTES.md" in claude_md

    def test_preserves_existing_files(self, tmp_path):
        """이미 존재하는 파일은 덮어쓰지 않는다."""
        codebase = tmp_path / "codebase-with-docs"
        codebase.mkdir()
        existing_notes = "# 기존 노트\n\n유지되어야 함.\n"
        existing_claude = "# 기존 CLAUDE\n"
        (codebase / "PROJECT_NOTES.md").write_text(existing_notes, encoding="utf-8")
        (codebase / "CLAUDE.md").write_text(existing_claude, encoding="utf-8")

        created = init_project.generate_codebase_memory_files(
            str(codebase), "any-name", "any description",
        )

        assert created == {"project_notes": False, "claude_pointer": False}
        assert (codebase / "PROJECT_NOTES.md").read_text(encoding="utf-8") == existing_notes
        assert (codebase / "CLAUDE.md").read_text(encoding="utf-8") == existing_claude

    def test_partial_existing(self, tmp_path):
        """한쪽 파일만 이미 있으면 없는 쪽만 생성한다."""
        codebase = tmp_path / "codebase-partial"
        codebase.mkdir()
        (codebase / "CLAUDE.md").write_text("# 기존 CLAUDE만\n", encoding="utf-8")

        created = init_project.generate_codebase_memory_files(
            str(codebase), "proj", "설명",
        )

        assert created == {"project_notes": True, "claude_pointer": False}
        assert (codebase / "PROJECT_NOTES.md").exists()
        assert "# 기존 CLAUDE만" in (codebase / "CLAUDE.md").read_text(encoding="utf-8")


class TestMemoryRefreshTaskType:
    """task_type=memory_refresh 경로 동작 검증."""

    def test_submit_accepts_memory_refresh_type(self, agent_hub_root, tmp_path):
        """submit이 task_type='memory_refresh'를 받고 task JSON에 기록한다."""
        # 지연 import — conftest의 sys.path 세팅을 사용
        from hub_api.core import HubAPI
        import shutil as _shutil

        api = HubAPI(agent_hub_root)
        codebase = str(tmp_path / "codebase-mr")
        project_name = f"test_mr_{os.getpid()}"

        api.create_project(
            name=project_name,
            description="memory_refresh 테스트",
            codebase_path=codebase,
        )
        try:
            result = api.submit(
                project=project_name,
                title="memory refresh",
                description="full-scan",
                task_type="memory_refresh",
            )
            import json as _json
            with open(result.file_path, encoding="utf-8") as f:
                task = _json.load(f)
            assert task["task_type"] == "memory_refresh"
        finally:
            project_dir = os.path.join(agent_hub_root, "projects", project_name)
            _shutil.rmtree(project_dir, ignore_errors=True)

    def test_submit_default_task_type_is_feature(self, agent_hub_root, tmp_path):
        """task_type 미지정 시 기본값 'feature'가 들어간다 (하위 호환)."""
        from hub_api.core import HubAPI
        import shutil as _shutil

        api = HubAPI(agent_hub_root)
        codebase = str(tmp_path / "codebase-default")
        project_name = f"test_default_{os.getpid()}"

        api.create_project(
            name=project_name, description="기본 테스트", codebase_path=codebase,
        )
        try:
            result = api.submit(
                project=project_name, title="일반 task", description="",
            )
            import json as _json
            with open(result.file_path, encoding="utf-8") as f:
                task = _json.load(f)
            assert task["task_type"] == "feature"
        finally:
            project_dir = os.path.join(agent_hub_root, "projects", project_name)
            _shutil.rmtree(project_dir, ignore_errors=True)

    def test_submit_rejects_invalid_task_type(self, agent_hub_root, tmp_path):
        """알 수 없는 task_type은 ValueError로 거부된다."""
        from hub_api.core import HubAPI
        import shutil as _shutil

        api = HubAPI(agent_hub_root)
        codebase = str(tmp_path / "codebase-bad")
        project_name = f"test_bad_{os.getpid()}"

        api.create_project(
            name=project_name, description="", codebase_path=codebase,
        )
        try:
            with pytest.raises(ValueError, match="task_type"):
                api.submit(
                    project=project_name, title="x", description="",
                    task_type="unknown_type",
                )
        finally:
            project_dir = os.path.join(agent_hub_root, "projects", project_name)
            _shutil.rmtree(project_dir, ignore_errors=True)



class TestMemoryUpdaterPrompt:
    """memory_updater agent prompt 파일이 올바른 위치에 있고 필수 섹션을 담는다."""

    def test_prompt_file_exists(self):
        prompt_path = os.path.join(
            AGENT_HUB_ROOT, "config", "agent_prompts", "memory_updater.md",
        )
        assert os.path.isfile(prompt_path), "memory_updater.md agent prompt가 없습니다"

    def test_prompt_declares_output_schema(self):
        """prompt가 출력 스키마(action/updated/sections_changed/rationale)를 명시한다."""
        prompt_path = os.path.join(
            AGENT_HUB_ROOT, "config", "agent_prompts", "memory_updater.md",
        )
        content = open(prompt_path, encoding="utf-8").read()
        for required_key in ["memory_update_complete", "updated", "sections_changed", "rationale"]:
            assert required_key in content, (
                f"memory_updater.md에 '{required_key}' 키가 명시되지 않았습니다"
            )
