#!/usr/bin/env python3
"""대화형 프로젝트 초기화 스크립트.

사용자에게 프로젝트 정보를 물어본 뒤:
  - projects/{name}/ 디렉토리 + 하위 runtime 디렉토리 생성
  - project.yaml 자동 작성
  - project_state.json 초기화
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml


# agent-hub 레포의 루트 디렉토리
AGENT_HUB_ROOT = Path(__file__).resolve().parent.parent

# 프로젝트 이름 유효성 패턴: 영문소문자, 숫자, 하이픈만 허용
PROJECT_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9\-]*[a-z0-9]$|^[a-z0-9]$")

# 아직 설정되지 않은 필드임을 나타내는 플레이스홀더 값.
# 프로젝트 실행 전 반드시 사용자가 실제 값으로 교체해야 한다.
UNCONFIGURED_PLACEHOLDER = "__UNCONFIGURED__"

# 프로젝트 하위에 생성할 runtime 디렉토리 목록
RUNTIME_DIRECTORIES = [
    "tasks",
    "handoffs",
    "commands",
    "logs",
    "archive",
    "attachments",
]


def ask_project_name() -> str:
    """프로젝트 이름을 입력받아 유효성 검사 후 반환한다.

    규칙: 영문소문자, 숫자, 하이픈만 허용. 하이픈으로 시작/끝 불가.
    이미 존재하는 프로젝트명이면 재입력 유도.
    """
    while True:
        name = input("\n프로젝트 이름을 입력하세요 (영문소문자, 숫자, 하이픈): ").strip()

        if not name:
            print("  → 이름을 입력해주세요.")
            continue

        if not PROJECT_NAME_PATTERN.match(name):
            print("  → 영문소문자, 숫자, 하이픈만 사용 가능합니다. 하이픈으로 시작/끝 불가.")
            continue

        project_directory = AGENT_HUB_ROOT / "projects" / name
        if project_directory.exists():
            print(f"  → projects/{name}/ 디렉토리가 이미 존재합니다. 다른 이름을 입력하세요.")
            continue

        return name


def ask_project_description() -> str:
    """프로젝트 설명을 여러 줄로 입력받는다.

    빈 줄을 입력하면 종료. 최소 1줄 이상 입력 필수.
    """
    print("\n프로젝트 설명을 입력하세요 (기술 스택, 목적 등).")
    print("여러 줄 입력 가능. 빈 줄을 입력하면 종료:")

    lines = []
    while True:
        line = input("  > ")
        if line.strip() == "" and lines:
            break
        if line.strip() == "" and not lines:
            print("  → 최소 1줄 이상 입력해주세요.")
            continue
        lines.append(line)

    return "\n".join(lines) + "\n"


def ask_codebase_path() -> tuple[str, bool]:
    """코드베이스 절대경로를 입력받고, 존재 여부를 확인한다.

    Returns:
        (절대경로 문자열, 신규생성 여부)
    """
    while True:
        raw_path = input("\n코드베이스 경로를 입력하세요 (절대경로): ").strip()

        if not raw_path:
            print("  → 경로를 입력해주세요.")
            continue

        # ~ 확장
        expanded_path = Path(raw_path).expanduser().resolve()

        if not expanded_path.is_absolute():
            print("  → 절대경로를 입력해주세요.")
            continue

        if expanded_path.exists():
            if not expanded_path.is_dir():
                print("  → 해당 경로는 디렉토리가 아닙니다.")
                continue
            print(f"  ✓ 기존 디렉토리 확인: {expanded_path}")
            return str(expanded_path), False

        # 디렉토리가 존재하지 않는 경우
        answer = input(f"  → {expanded_path} 가 존재하지 않습니다. 생성할까요? (y/n): ").strip().lower()
        if answer in ("y", "yes"):
            expanded_path.mkdir(parents=True, exist_ok=True)
            print(f"  ✓ 디렉토리 생성 완료: {expanded_path}")
            return str(expanded_path), True

        print("  → 다시 입력해주세요.")


def ask_git_settings() -> dict:
    """git 연동 여부 및 기본 설정을 입력받는다."""
    answer = input("\ngit remote를 연동할까요? (y/n, 기본: y): ").strip().lower()

    if answer in ("n", "no"):
        return {
            "enabled": False,
            "remote": "origin",
            "author_name": "agent-bot",
            "author_email": "agent@example.com",
            "base_branch": "main",
            "pr_target_branch": "main",
            "merge_strategy": "require_human",
        }

    # git 활성화
    remote = input("  remote 이름 (기본: origin): ").strip() or "origin"
    base_branch = input("  feature branch 기준 브랜치 (기본: main): ").strip() or "main"
    pr_target = input("  PR 대상 브랜치 (기본: main): ").strip() or "main"
    author_name = input("  커밋 작성자 이름 (기본: agent-bot): ").strip() or "agent-bot"
    author_email = input("  커밋 작성자 이메일 (기본: agent@example.com): ").strip() or "agent@example.com"

    return {
        "enabled": True,
        "remote": remote,
        "author_name": author_name,
        "author_email": author_email,
        "base_branch": base_branch,
        "pr_target_branch": pr_target,
        "merge_strategy": "require_human",
    }


def create_project_directory_structure(project_root: Path) -> None:
    """프로젝트 루트 및 하위 runtime 디렉토리를 생성한다."""
    project_root.mkdir(parents=True, exist_ok=True)

    for directory_name in RUNTIME_DIRECTORIES:
        (project_root / directory_name).mkdir(exist_ok=True)


def generate_project_yaml(
    project_root: Path,
    project_name: str,
    description: str,
    codebase_path: str,
    git_settings: dict,
) -> Path:
    """project.yaml 파일을 생성한다.

    Returns:
        생성된 project.yaml 파일의 Path.
    """
    project_config = {
        "project": {
            "name": project_name,
            "description": description,
            "default_branch": git_settings.get("pr_target_branch", "main"),
        },
        "codebase": {
            "path": codebase_path,
            "service_bind_address": git_settings.pop("_service_bind_address", "0.0.0.0"),
            "service_port": git_settings.pop("_service_port", 3000),
        },
        "git": git_settings,
        "testing": {
            "unit_test": {
                "enabled": False,
                "available_suites": [],
                "default_suites": [],
            },
            "e2e_test": {
                "enabled": False,
                "tool": "playwright",
                "test_accounts": [],
            },
            "integration_test": {
                "enabled": False,
                "suites": [],
                "include_e2e": False,
            },
        },
    }

    yaml_path = project_root / "project.yaml"
    with open(yaml_path, "w", encoding="utf-8") as yaml_file:
        # 헤더 주석 추가
        yaml_file.write(f"# projects/{project_name}/project.yaml\n")
        yaml_file.write("# 프로젝트별 정적 설정. 테스트 설정 등은 나중에 활성화할 수 있습니다.\n")
        yaml_file.write(f"# __UNCONFIGURED__ 값은 실행 전 반드시 실제 값으로 변경해야 합니다.\n\n")
        yaml.dump(
            project_config,
            yaml_file,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        )
    return yaml_path


def initialize_project_state(project_root: Path, project_name: str) -> Path:
    """project_state.json을 초기 상태로 생성한다.

    Returns:
        생성된 project_state.json 파일의 Path.
    """
    initial_state = {
        "project_name": project_name,
        "lifecycle": "active",
        "status": "idle",
        "current_task_id": None,
        "last_activity_at": datetime.now(timezone.utc).isoformat(),
        "overrides": {},
        "update_history": [],
    }

    state_path = project_root / "project_state.json"
    with open(state_path, "w", encoding="utf-8") as state_file:
        json.dump(initial_state, state_file, ensure_ascii=False, indent=2)
    return state_path


def main() -> None:
    """대화형 프로젝트 초기화 메인 흐름.

    사용자에게 대화형으로 정보를 수집한 뒤 HubAPI.create_project()를 호출한다.
    """
    print("=" * 60)
    print("  Agent Hub — 프로젝트 초기화")
    print("=" * 60)

    # 1. 프로젝트 이름
    project_name = ask_project_name()

    # 2. 설명
    description = ask_project_description()

    # 3. 코드베이스 경로
    codebase_path, is_new_codebase = ask_codebase_path()

    # 4. git 설정
    git_settings = ask_git_settings()

    # 5. HubAPI를 통해 프로젝트 생성
    # hub_api를 사용하는 대화형 경로에서는 사용자가 직접 값을 입력했으므로
    # 플레이스홀더가 아닌 실제 값이 들어간다.
    sys.path.insert(0, str(AGENT_HUB_ROOT / "scripts"))
    from hub_api.core import HubAPI

    api = HubAPI(str(AGENT_HUB_ROOT))
    result = api.create_project(
        name=project_name,
        description=description,
        codebase_path=codebase_path,
        git_settings=git_settings,
    )

    # 6. 신규 코드베이스에 git init (대화형 전용 — 프로그래밍 API에서는 수행하지 않음)
    if is_new_codebase and git_settings.get("enabled"):
        import subprocess

        subprocess.run(["git", "init"], cwd=codebase_path, check=True, capture_output=True)
        print(f"  ✓ git init 완료: {codebase_path}")

    # 완료 메시지
    print("\n" + "=" * 60)
    print(f"  ✓ 프로젝트 '{project_name}' 초기화 완료!")
    print(f"  → 설정: {result.project_yaml_path}")
    print(f"  → 상태: {result.project_state_path}")
    print()
    print("  테스트는 나중에 project.yaml에서 활성화할 수 있습니다.")
    print(f"  __UNCONFIGURED__ 값이 있으면 실행 전 반드시 수정하세요.")
    print("  다음 단계: task JSON을 작성하고 run_agent.sh run으로 실행하세요.")
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n취소되었습니다.")
        sys.exit(1)
