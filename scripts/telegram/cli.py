"""Telegram bridge 보조 CLI.

`./run_system.sh telegram <sub>`에서 호출된다. 실제 Telegram Bot API를 사용해
forum topic을 직접 조작한다. bridge 프로세스와 독립적으로 동작 가능 (단, bind 후의
hub_chat_id가 config.yaml에 기록돼 있어야 한다).

서브명령:
- list-orphans: 알려진 topic binding 목록 출력. (실제 reconciliation은 Phase B의
  reconciler.py로 위임 — 본 CLI는 project_state.json에 기록된 binding을 그대로 보여준다.)
- delete-topic <thread_id>: deleteForumTopic API 직접 호출.
- prune-orphans: list + 사용자 확인 후 일괄 삭제.

참고: Telegram Bot API에는 그룹의 모든 forum topic을 조회하는 endpoint가 없다.
따라서 "orphan" 판정은 우리 측 ledger(project_state.json)와의 대조로만 가능하며,
완전한 reconciliation은 reconciler.py(차기 Phase)에서 처리한다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from glob import glob
from typing import Optional

import yaml

from telegram.client import TelegramAPIError, TelegramClient


def _load_config(config_path: str) -> dict:
    with open(config_path) as f:
        return yaml.safe_load(f) or {}


def _projects_dir(agent_hub_root: str, config: dict) -> str:
    paths = config.get("paths") or {}
    return paths.get("projects_dir") or os.path.join(agent_hub_root, "projects")


def _scan_bindings(projects_dir: str) -> list[dict]:
    """projects/*/project_state.json에서 telegram binding이 있는 항목을 모은다."""
    rows = []
    for state_path in sorted(glob(os.path.join(projects_dir, "*", "project_state.json"))):
        try:
            with open(state_path) as f:
                state = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        binding = state.get("telegram") or {}
        thread_id = binding.get("thread_id")
        if not thread_id:
            continue
        rows.append({
            "project": state.get("project_name") or os.path.basename(os.path.dirname(state_path)),
            "lifecycle": state.get("lifecycle", "active"),
            "chat_id": binding.get("chat_id"),
            "thread_id": thread_id,
            "state_path": state_path,
        })
    return rows


def _client_from_config(config: dict) -> Optional[TelegramClient]:
    tg = config.get("telegram") or {}
    token = tg.get("bot_token") or ""
    if not token:
        print("[ERROR] config.yaml에 telegram.bot_token이 없습니다.", file=sys.stderr)
        return None
    return TelegramClient(
        bot_token=token,
        send_interval_seconds=float(tg.get("send_interval_seconds") or 0.05),
    )


def _hub_chat_id(config: dict) -> int:
    return int((config.get("telegram") or {}).get("hub_chat_id") or 0)


def _enqueue_command(agent_hub_root: str, action: str, project: str) -> str:
    """data/telegram_commands/ 에 명령 파일을 atomic write 한다. 경로 반환."""
    cmd_dir = os.path.join(agent_hub_root, "data", "telegram_commands")
    os.makedirs(cmd_dir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    cid = uuid.uuid4().hex[:8]
    path = os.path.join(cmd_dir, f"{ts}_{cid}_{action}.json")
    payload = {
        "action": action,
        "project": project,
        "requested_at": datetime.now(timezone.utc).isoformat(),
    }
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return path


# ─── subcommands ───

def cmd_register(agent_hub_root: str, projects_dir: str, project: str, force: bool) -> int:
    """기존 프로젝트를 telegram에 수동 등록한다.

    project_state.json에 telegram binding이 이미 있으면 force 없으면 거부.
    bridge가 큐를 폴링해 createForumTopic을 호출한다.
    """
    project_dir = os.path.join(projects_dir, project)
    state_path = os.path.join(project_dir, "project_state.json")
    if not os.path.isdir(project_dir):
        print(f"[ERROR] 프로젝트 '{project}'를 찾을 수 없습니다: {project_dir}", file=sys.stderr)
        return 2
    if os.path.exists(state_path):
        try:
            with open(state_path) as f:
                state = json.load(f)
            binding = state.get("telegram") or {}
            if binding.get("thread_id") and not force:
                print(f"[SKIP] '{project}'에 이미 topic이 등록돼 있습니다 "
                      f"(thread_id={binding['thread_id']}). --force로 재등록 가능.")
                return 0
        except (OSError, json.JSONDecodeError):
            pass
    path = _enqueue_command(agent_hub_root, "create_topic", project)
    print(f"[OK] create_topic 명령 enqueue: {os.path.basename(path)}")
    print("    bridge가 1~2초 내 처리합니다. 그룹의 새 topic + 환영 메시지를 확인하세요.")
    print("    실패 시 logs/telegram_bridge.log 를 확인하세요.")
    return 0


def cmd_list_orphans(config: dict, projects_dir: str) -> int:
    rows = _scan_bindings(projects_dir)
    if not rows:
        print("등록된 topic binding이 없습니다.")
        return 0
    print(f"{'project':30}  {'lifecycle':10}  {'chat_id':>14}  {'thread_id':>10}")
    print("-" * 72)
    for r in rows:
        print(f"{r['project']:30}  {r['lifecycle']:10}  {r['chat_id']:>14}  {r['thread_id']:>10}")
    print()
    print("※ Bot API에는 그룹의 forum topic을 일괄 조회하는 endpoint가 없습니다.")
    print("  실제 orphan 판정(우리 ledger에는 없으나 그룹에 남은 topic)은 reconciler.py(차기 Phase)에서 수행합니다.")
    return 0


def cmd_delete_topic(config: dict, thread_id: int) -> int:
    chat_id = _hub_chat_id(config)
    if not chat_id:
        print("[ERROR] hub_chat_id가 0입니다. /bind_hub 후에 시도하세요.", file=sys.stderr)
        return 2
    client = _client_from_config(config)
    if client is None:
        return 2
    try:
        client.delete_forum_topic(chat_id=chat_id, message_thread_id=thread_id)
        print(f"[OK] thread_id={thread_id} 삭제 완료.")
        return 0
    except TelegramAPIError as e:
        print(f"[ERROR] 삭제 실패: {e}", file=sys.stderr)
        return 1


def cmd_prune_orphans(config: dict, projects_dir: str, assume_yes: bool) -> int:
    rows = _scan_bindings(projects_dir)
    closed = [r for r in rows if r["lifecycle"] == "closed"]
    if not closed:
        print("삭제 대상(lifecycle=closed인 binding)이 없습니다.")
        return 0
    print("아래 topic을 삭제합니다 (lifecycle=closed):")
    for r in closed:
        print(f"  - {r['project']:30}  thread_id={r['thread_id']}")
    if not assume_yes:
        ans = input("진행하시겠습니까? [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("취소되었습니다.")
            return 0
    client = _client_from_config(config)
    if client is None:
        return 2
    chat_id = _hub_chat_id(config)
    if not chat_id:
        print("[ERROR] hub_chat_id가 0입니다.", file=sys.stderr)
        return 2
    failed = 0
    for r in closed:
        try:
            client.delete_forum_topic(chat_id=chat_id, message_thread_id=r["thread_id"])
            print(f"[OK] {r['project']} (thread_id={r['thread_id']}) 삭제")
        except TelegramAPIError as e:
            print(f"[ERROR] {r['project']} 삭제 실패: {e}", file=sys.stderr)
            failed += 1
    return 1 if failed else 0


# ─── main ───

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="run_system.sh telegram",
                                     description="Telegram bridge 보조 CLI")
    parser.add_argument("--config", required=True, help="config.yaml 경로")
    sub = parser.add_subparsers(dest="sub", required=True)

    p_reg = sub.add_parser("register", help="기존 프로젝트를 telegram topic으로 수동 등록")
    p_reg.add_argument("project", help="프로젝트 이름")
    p_reg.add_argument("-f", "--force", action="store_true",
                       help="이미 binding이 있어도 재등록 (기존 thread_id를 덮어쓴다)")

    sub.add_parser("list-orphans", help="알려진 topic binding 목록 출력")

    p_del = sub.add_parser("delete-topic", help="thread_id 지정 topic 삭제")
    p_del.add_argument("thread_id", type=int)

    p_prune = sub.add_parser("prune-orphans", help="lifecycle=closed binding 일괄 삭제")
    p_prune.add_argument("-y", "--yes", action="store_true", help="확인 prompt 없이 진행")

    args = parser.parse_args(argv)

    if not os.path.exists(args.config):
        print(f"config.yaml을 찾을 수 없습니다: {args.config}", file=sys.stderr)
        return 2
    config = _load_config(args.config)
    agent_hub_root = os.environ.get("AGENT_HUB_ROOT") or os.path.dirname(os.path.abspath(args.config))
    projects_dir = _projects_dir(agent_hub_root, config)

    if args.sub == "register":
        return cmd_register(agent_hub_root, projects_dir, args.project, args.force)
    if args.sub == "list-orphans":
        return cmd_list_orphans(config, projects_dir)
    if args.sub == "delete-topic":
        return cmd_delete_topic(config, args.thread_id)
    if args.sub == "prune-orphans":
        return cmd_prune_orphans(config, projects_dir, args.yes)
    return 2


if __name__ == "__main__":
    sys.exit(main())
