"""
submit 확인 카드에서 task에 적용될 최종 설정을 트리로 미리 보여주기 위한 헬퍼.

4계층 merge(config.yaml → project.yaml → project_state.json → task.config_override)를
재사용하여 effective config를 계산하고, 사용자가 명시적으로 넣은 값은 (수정됨),
나머지는 (기본값)으로 태깅한 indented 평문 트리를 반환한다.

정책:
- HIDDEN_SECTIONS: task 단위로 바꿀 일이 거의 없는 섹션을 블랙리스트로 숨김.
  새 섹션이 추가되면 코드 변경 없이 자동 노출된다(fail-open).
- HIDDEN_PATHS: credential 등 노출 금지 경로를 블랙리스트로 숨김.
"""

import json
import os
import sys
from pathlib import Path


# task 레벨 확인 카드에서 숨길 섹션 (정적 정체성/노이즈)
HIDDEN_SECTIONS = frozenset({"codebase", "project", "claude", "logging"})

# 확인 카드에 절대 노출해선 안 될 경로 (credential 등)
HIDDEN_PATHS = frozenset({
    "git.auth_token",
    "git.author_name",
    "git.author_email",
})


def _load_yaml_or_empty(path):
    """YAML 파일을 읽어 dict로 반환, 없거나 에러면 빈 dict."""
    if not os.path.isfile(path):
        return {}
    try:
        import yaml
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _load_json_or_empty(path):
    """JSON 파일을 읽어 dict로 반환, 없거나 에러면 빈 dict."""
    if not os.path.isfile(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def _flatten_paths(config_override, prefix=""):
    """
    사용자가 명시한 config_override의 leaf 경로를 dotted 문자열 집합으로 반환한다.
    예: {"git": {"merge_strategy": "auto_merge"}} → {"git.merge_strategy"}
    """
    paths = set()
    if not isinstance(config_override, dict):
        return paths
    for key, value in config_override.items():
        full = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            paths |= _flatten_paths(value, full)
        else:
            paths.add(full)
    return paths


def compute_effective_config(agent_hub_root, project, config_override):
    """
    상위 3계층 + task.config_override를 합쳐 effective config를 계산한다.

    workflow_controller.resolve_effective_config를 재사용하여 병합 규칙의
    단일 출처를 유지한다.

    Returns:
        (effective dict, override_paths set)
    """
    scripts_dir = str(Path(agent_hub_root) / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    from workflow_controller import resolve_effective_config

    config = _load_yaml_or_empty(os.path.join(agent_hub_root, "config.yaml"))
    project_yaml = _load_yaml_or_empty(
        os.path.join(agent_hub_root, "projects", project, "project.yaml")
    )
    project_state = _load_json_or_empty(
        os.path.join(agent_hub_root, "projects", project, "project_state.json")
    )
    task = {"config_override": config_override or {}}
    effective = resolve_effective_config(config, project_yaml, project_state, task)
    override_paths = _flatten_paths(config_override or {})
    return effective, override_paths


def _format_value(value):
    """leaf 값을 사람이 읽기 좋은 문자열로 변환."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, str) and value == "":
        return '""'
    if isinstance(value, list):
        if not value:
            return "[]"
        return "[" + ", ".join(str(x) for x in value) + "]"
    return str(value)


def _tag(path, override_paths):
    """override된 경로에만 (수정됨) 표식, 기본값은 태그 없음."""
    return " (수정됨)" if path in override_paths else ""


def _render_subtree(value, override_paths, current_path, lines, indent):
    """
    dict를 재귀 순회하며 indented 트리를 lines에 누적한다.
    사용자가 명시한 leaf에만 (수정됨) 태그를 붙이고, 기본값은 태그 없이 값만 표시.
    """
    if not isinstance(value, dict) or not value:
        lines.append(f"{' ' * indent}- {current_path.rsplit('.', 1)[-1]} : "
                     f"{_format_value(value)}{_tag(current_path, override_paths)}")
        return

    for key, child in value.items():
        full_path = f"{current_path}.{key}" if current_path else key
        if full_path in HIDDEN_PATHS:
            continue
        if isinstance(child, dict) and child:
            lines.append(f"{' ' * indent}- {key}")
            _render_subtree(child, override_paths, full_path, lines, indent + 2)
        else:
            lines.append(f"{' ' * indent}- {key} : {_format_value(child)}"
                         f"{_tag(full_path, override_paths)}")


def render_config_tree(effective, override_paths):
    """
    effective config 전체를 indented 트리로 렌더한다.
    HIDDEN_SECTIONS에 속한 섹션과 HIDDEN_PATHS에 속한 경로는 숨긴다.
    """
    lines = ["config_override"]
    for section, value in effective.items():
        if section in HIDDEN_SECTIONS:
            continue
        if isinstance(value, dict) and not value:
            # 빈 섹션은 건너뜀 (예: testing 섹션이 아예 비어있는 프로젝트)
            continue
        lines.append(f"  - {section}")
        if isinstance(value, dict):
            _render_subtree(value, override_paths, section, lines, 4)
        else:
            lines.append(f"    - {_format_value(value)}{_tag(section, override_paths)}")
    return "\n".join(lines)


def format_config_override_for_confirmation(agent_hub_root, project, config_override):
    """
    submit 확인 카드용 전체 트리 문자열을 반환한다.
    에러 발생 시 None을 반환하여 호출자가 fallback 포맷을 쓸 수 있게 한다.
    """
    if not agent_hub_root or not project:
        return None
    try:
        effective, override_paths = compute_effective_config(
            agent_hub_root, project, config_override or {}
        )
    except Exception:
        return None
    return render_config_tree(effective, override_paths)
