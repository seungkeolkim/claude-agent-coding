"""Telegram bridge — 상주 프로세스 진입점 (Phase 2.3).

세 개의 백그라운드 스레드로 구성된다:

1. Update loop         : `getUpdates` long polling → router → dispatch
2. Notification poller : projects/*/notifications.json 신규 항목 → Telegram 전송
3. Command poller      : data/telegram_commands/*.json (HubAPI hook이 기록) → 처리

설계 원칙:
- 모든 상태(processed offset, last notification timestamp 등)는 `data/telegram_*.json`에
  파일로 영속한다. 프로세스 재기동 시 중복 처리 없음.
- 실패(네트워크 일시 오류 등)는 로그만 남기고 다음 사이클에서 자연스럽게 재시도된다.
- `config.yaml`의 `telegram.enabled=false`면 프로세스는 즉시 종료. run_system.sh는
  에러가 아닌 "비활성" 상태로 취급한다.

본 모듈은 Phase A+B의 MVP 수준으로, reconciler는 주기 hook만 두고 실제 orphan 탐지
로직은 후속 세션(§12.10)에서 채운다.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import yaml  # noqa: E402

from telegram.client import TelegramClient, TelegramAPIError  # noqa: E402
from telegram.formatter import (  # noqa: E402
    format_notification,
    reply_markup_for_notification,
    escape_markdown_v2,
)
from telegram.router import route, RoutingDecision  # noqa: E402
from telegram.session import get_session, drop_session  # noqa: E402
from hub_api.core import HubAPI  # noqa: E402
from hub_api.protocol import Request, dispatch  # noqa: E402

logger = logging.getLogger("telegram_bridge")


# ─── 경로 상수 ───

def _data_dir(agent_hub_root: str) -> str:
    return os.path.join(agent_hub_root, "data")


def _offset_path(agent_hub_root: str) -> str:
    return os.path.join(_data_dir(agent_hub_root), "telegram_offset.json")


def _last_notification_path(agent_hub_root: str) -> str:
    return os.path.join(_data_dir(agent_hub_root), "telegram_last_notification.json")


def _commands_dir(agent_hub_root: str) -> str:
    return os.path.join(_data_dir(agent_hub_root), "telegram_commands")


def _pending_modify_path(agent_hub_root: str) -> str:
    """'📝 수정' 버튼을 누른 뒤 아직 피드백 텍스트를 보내지 않은 pending 상태를 보관한다.

    파일 스키마: {"pending": {"<chat_id>_<thread_id>": {project, task_id,
                                                        requested_at, prompt_message_id}}}
    """
    return os.path.join(_data_dir(agent_hub_root), "telegram_pending_modify.json")


# '수정' 버튼을 눌렀지만 답장을 보내지 않고 방치한 pending의 TTL.
# 너무 짧으면 사용자가 입력하는 사이 만료되고, 너무 길면 다른 상호작용을 방해한다.
_PENDING_MODIFY_TTL_SECONDS = 10 * 60


def _project_state_path(agent_hub_root: str, project: str) -> str:
    return os.path.join(agent_hub_root, "projects", project, "project_state.json")


# ─── 영속 파일 util ───

def _load_json(path: str, default):
    """JSON 파일을 읽어 반환. 없거나 손상이면 default."""
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def _save_json_atomic(path: str, data):
    """JSON atomic write. 부모 dir 없으면 생성."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + f".tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    with open(tmp_path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp_path, path)


# ═══════════════════════════════════════════════════════════
# Command queue (HubAPI hook → bridge)
# ═══════════════════════════════════════════════════════════

def enqueue_command(agent_hub_root: str, action: str, project: str,
                    extra: Optional[dict] = None) -> str:
    """HubAPI hook이 호출하는 진입점. data/telegram_commands/ 에 요청 파일을 기록한다.

    Bridge가 기동 중이 아니어도 호출 가능하며, 파일은 bridge가 다음 기동 시 소비한다.

    Returns:
        생성된 파일 경로.
    """
    cmd_dir = _commands_dir(agent_hub_root)
    os.makedirs(cmd_dir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    cid = uuid.uuid4().hex[:8]
    payload = {
        "action": action,
        "project": project,
        "requested_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        payload.update(extra)
    path = os.path.join(cmd_dir, f"{ts}_{cid}_{action}.json")
    _save_json_atomic(path, payload)
    return path


# ═══════════════════════════════════════════════════════════
# Bridge
# ═══════════════════════════════════════════════════════════

class TelegramBridge:
    """3개 스레드로 구성된 상주 브릿지.

    thread-safe: config 읽기 작업은 self._config_lock, 상태 파일은 atomic write에 의존.
    """

    def __init__(self, agent_hub_root: str, config_path: str):
        self._root = agent_hub_root
        self._config_path = config_path
        self._config_lock = threading.Lock()
        self._config = _load_yaml(config_path)

        tg = (self._config.get("telegram") or {})
        self._enabled = bool(tg.get("enabled")) and bool(tg.get("bot_token"))
        if not self._enabled:
            self._client = None
        else:
            self._client = TelegramClient(
                bot_token=tg.get("bot_token", ""),
                send_interval_seconds=float(tg.get("send_interval_seconds", 0.05)),
            )

        self._stop_event = threading.Event()
        self._reload_event = threading.Event()
        self._hub_api = HubAPI(agent_hub_root)
        # 수정 버튼 → 피드백 입력 대기 상태의 동시성 보호용 락.
        # 파일은 atomic write로 저장하지만 read-modify-write 구간을 한 번에 감싼다.
        self._pending_modify_lock = threading.Lock()

    # ─── 진입점 ───

    def run(self) -> None:
        """메인 루프. SIGTERM 수신까지 block."""
        if not self._enabled:
            logger.info("telegram.enabled=false 또는 bot_token 미설정 — bridge 즉시 종료")
            return

        self._install_signal_handlers()

        threads = [
            threading.Thread(target=self._update_loop, name="tg-updates", daemon=True),
            threading.Thread(target=self._notification_loop, name="tg-noti", daemon=True),
            threading.Thread(target=self._command_loop, name="tg-cmd", daemon=True),
        ]
        for t in threads:
            t.start()

        logger.info("telegram_bridge 시작됨 (pid=%s)", os.getpid())

        # 메인 스레드는 stop_event를 대기. 워커 스레드가 daemon이라 자연 종료.
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=1.0)
            if self._reload_event.is_set():
                self._reload_event.clear()
                self._reload_config()

        logger.info("telegram_bridge 종료 중 ...")
        # daemon thread이므로 별도 join 없음 (long polling HTTP는 timeout까지 대기).

    # ─── 시그널 ───

    def _install_signal_handlers(self) -> None:
        signal.signal(signal.SIGTERM, self._on_sigterm)
        signal.signal(signal.SIGINT, self._on_sigterm)
        # SIGHUP은 Windows에 없을 수 있으므로 방어적으로.
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, self._on_sighup)

    def _on_sigterm(self, signum, frame) -> None:
        logger.info("signal %s 수신 — graceful shutdown", signum)
        self._stop_event.set()

    def _on_sighup(self, signum, frame) -> None:
        logger.info("SIGHUP 수신 — config reload 예약")
        self._reload_event.set()

    def _reload_config(self) -> None:
        with self._config_lock:
            try:
                self._config = _load_yaml(self._config_path)
                logger.info("config reload 완료")
            except Exception as exc:  # noqa: BLE001
                logger.exception("config reload 실패: %s", exc)

    def _tg_config(self) -> dict:
        with self._config_lock:
            return dict(self._config.get("telegram") or {})

    # ═══════════════════════════════════════════════════════════
    # 1) Update loop
    # ═══════════════════════════════════════════════════════════

    def _update_loop(self) -> None:
        """getUpdates long polling → RoutingDecision 별 처리."""
        offset = self._load_offset()
        tg_conf = self._tg_config()
        poll_timeout = int(tg_conf.get("long_polling_timeout_seconds", 30))

        while not self._stop_event.is_set():
            try:
                updates = self._client.get_updates(
                    offset=offset + 1 if offset else None,
                    timeout=poll_timeout,
                )
            except TelegramAPIError as exc:
                logger.warning("getUpdates 실패: %s — 2초 후 재시도", exc)
                self._stop_event.wait(timeout=2.0)
                continue
            except Exception as exc:  # noqa: BLE001
                logger.exception("getUpdates 예기치 못한 실패: %s", exc)
                self._stop_event.wait(timeout=5.0)
                continue

            for update in updates:
                update_id = update.get("update_id", 0)
                try:
                    self._handle_update(update)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("update 처리 실패 (id=%s): %s", update_id, exc)
                if update_id > offset:
                    offset = update_id
                    self._save_offset(offset)

    def _handle_update(self, update: dict) -> None:
        decision = route(update, self._tg_config())

        # 수정 버튼으로 피드백을 기다리는 pending state가 있으면,
        # 해당 topic에서 들어오는 다음 자연어 메시지 1건을 피드백으로 소비한다.
        # 슬래시 명령이 먼저 도착하면 "사용자가 의도를 바꿨다"는 신호로 보고
        # pending만 취소하고 원래 명령을 정상 처리한다.
        if decision.kind == "natural_message":
            if self._try_consume_modify_feedback(decision):
                return
        elif decision.kind == "slash_command":
            popped = self._pop_pending_modify(decision.chat_id, decision.thread_id)
            if popped:
                self._safe_send(
                    decision.chat_id, decision.thread_id,
                    "ℹ️ 대기 중이던 수정 요청이 취소되었습니다 (슬래시 명령 수신). "
                    "필요하면 '📝 수정' 버튼을 다시 눌러주세요.",
                )

        if decision.kind == "ignore":
            return
        if decision.kind == "reply":
            self._safe_send(decision.chat_id, decision.thread_id, decision.text)
            return
        if decision.kind == "bind_hub":
            self._handle_bind_hub(decision)
            return
        if decision.kind == "callback_query":
            self._handle_callback_query(decision)
            return
        if decision.kind == "slash_command":
            self._handle_slash(decision)
            return
        if decision.kind == "natural_message":
            self._handle_natural(decision)
            return

    def _try_consume_modify_feedback(self, d: RoutingDecision) -> bool:
        """수정 피드백 pending이 있으면 이 메시지의 텍스트로 hub_api.reject를 호출한다.

        pending을 소비했는지(성공/실패 무관)를 True/False로 돌려준다. True인 경우
        호출자는 기존 ChatProcessor 라우팅을 건너뛴다 — 같은 메시지를 두 경로에
        중복 전달하지 않기 위함이다.
        """
        pending = self._pop_pending_modify(d.chat_id, d.thread_id)
        if not pending:
            return False

        project = pending.get("project")
        task_id = pending.get("task_id")
        feedback = (d.text or "").strip()

        if not feedback or not project or not task_id:
            self._safe_send(
                d.chat_id, d.thread_id,
                "⚠️ 수정 요청으로 처리할 메시지가 비어 있었습니다. "
                "버튼을 다시 눌러주세요.",
            )
            return True

        # 같은 task가 이미 다른 채널(Web/CLI)에서 응답됐을 수 있다.
        already = self._already_responded_notice(project, task_id)
        if already:
            self._safe_send(d.chat_id, d.thread_id, already)
            return True

        self._dispatch_and_reply(d, "reject", project=project,
                                 params={"task_id": task_id,
                                         "message": feedback})
        return True

    def _handle_bind_hub(self, d: RoutingDecision) -> None:
        """/bind_hub <secret> 처리. secret 일치 시 chat_id 저장 + secret 소비."""
        tg_conf = self._tg_config()
        expected = tg_conf.get("bind_secret") or ""
        if not expected:
            self._safe_send(d.chat_id, d.thread_id,
                            "⚠️ bind_secret이 비어 있습니다. 관리자에게 문의하세요.")
            return
        if d.bind_secret != expected:
            self._safe_send(d.chat_id, d.thread_id, "⚠️ bind_secret 불일치.")
            return

        # config.yaml 업데이트 — 주석/포맷 보존을 위해 라인 단위 in-place 치환.
        self._persist_bind(d.chat_id)
        self._safe_send(d.chat_id, d.thread_id,
                        "✅ Agent Hub 연결됨. 프로젝트 생성 시 자동으로 topic이 추가됩니다.")

    def _persist_bind(self, chat_id: int) -> None:
        """telegram.hub_chat_id / telegram.bind_secret 두 줄만 in-place 치환한다.

        PyYAML의 dump는 주석을 모두 잃기 때문에 정규식 기반 라인 치환을 사용한다.
        ruamel.yaml 의존성 추가를 피하려는 의도. telegram 섹션의 각 키는
        단일 라인이라는 가정 — list/dict 값은 등장하지 않는 두 키만 다룬다.
        """
        import re
        with self._config_lock:
            with open(self._config_path) as f:
                text = f.read()
            new_text = re.sub(
                r"^(\s*hub_chat_id:\s*).*$",
                lambda m: f"{m.group(1)}{int(chat_id)}",
                text, count=1, flags=re.MULTILINE,
            )
            new_text = re.sub(
                r"^(\s*bind_secret:\s*).*$",
                lambda m: f'{m.group(1)}""',
                new_text, count=1, flags=re.MULTILINE,
            )
            tmp = self._config_path + ".tmp"
            with open(tmp, "w") as f:
                f.write(new_text)
            os.replace(tmp, self._config_path)
            # 메모리 캐시도 갱신
            self._config = _load_yaml(self._config_path)

    def _handle_slash(self, d: RoutingDecision) -> None:
        """지원 슬래시 명령을 HubAPI dispatch로 매핑. 복잡한 파싱은 후속 세션에서 확장."""
        cmd = d.command
        # Topic → project 해석
        project = self._project_for_thread(d.chat_id, d.thread_id)

        if cmd == "help":
            self._safe_send(d.chat_id, d.thread_id, _HELP_TEXT)
            return

        if cmd == "new_session":
            if d.thread_id is None:
                return
            drop_session(d.chat_id, d.thread_id)
            self._safe_send(d.chat_id, d.thread_id, "🧹 세션이 초기화되었습니다.")
            return

        if project is None and cmd != "status":
            self._safe_send(d.chat_id, d.thread_id,
                            "⚠️ 이 topic은 프로젝트와 연결되어 있지 않습니다.")
            return

        if cmd == "status":
            self._dispatch_and_reply(d, "status", project=project)
            return

        if cmd == "list":
            params = {}
            args = d.args
            i = 0
            while i < len(args):
                if args[i] == "--status" and i + 1 < len(args):
                    params["status"] = args[i + 1]
                    i += 2
                else:
                    i += 1
            self._dispatch_and_reply(d, "list", project=project, params=params)
            return

        if cmd == "pending":
            self._dispatch_and_reply(d, "pending", project=project)
            return

        if cmd == "cancel":
            if not d.args:
                self._safe_send(d.chat_id, d.thread_id, "사용법: /cancel <task_id>")
                return
            task_id = d.args[0]
            self._dispatch_and_reply(d, "cancel", project=project,
                                     params={"task_id": task_id})
            return

    def _handle_natural(self, d: RoutingDecision) -> None:
        """자연어 → ChatProcessor. Web Chat과 동일한 엔진을 재사용한다."""
        if d.thread_id is None:
            self._safe_send(d.chat_id, None,
                            "⚠️ Topic이 아닌 General 채널에서는 자연어를 처리하지 않습니다. "
                            "프로젝트 topic에서 말해 주세요.")
            return

        # 해당 topic에 메시지를 되돌려주는 on_message 콜백을 묶는다.
        chat_id_fixed = d.chat_id
        thread_id_fixed = d.thread_id

        def _on_message(event: dict) -> None:
            role = event.get("type") or event.get("role") or "assistant"
            content = event.get("content", "")
            if role == "chat_typing":
                return  # Telegram엔 typing 액션 별도 — 현 Phase에선 생략
            if not content:
                return
            prefix = ""
            if role == "system":
                prefix = "🔔 "
            self._safe_send(chat_id_fixed, thread_id_fixed, prefix + content,
                            parse_mode=None)

        requested_by = _requested_by_for(d)
        session = get_session(self._root, d.chat_id, d.thread_id, _on_message,
                              requested_by=requested_by)
        session.submit_message(d.text)

    def _handle_callback_query(self, d: RoutingDecision) -> None:
        """버튼 클릭 ack 먼저 → action 매핑 → dispatch → 결과 회신."""
        # 15초 제한 있으므로 ack를 먼저 보낸다.
        try:
            self._client.answer_callback_query(d.callback_query_id, text="처리 중...")
        except TelegramAPIError as exc:
            logger.warning("answerCallbackQuery 실패: %s", exc)

        action = d.callback_action
        project = d.callback_project
        task_id = d.callback_task_id

        # 이미 다른 채널(Web/CLI)에서 처리된 버튼인지 먼저 확인.
        # approve/reject_modify/reject_cancel은 모두 waiting_for_human_plan_confirm
        # 상태에서만 유효하므로, 그 외 상태면 친절한 "이미 처리됨" 메시지로 대응.
        if action in ("approve", "reject_modify", "reject_cancel"):
            already = self._already_responded_notice(project, task_id)
            if already:
                self._safe_send(d.chat_id, d.thread_id, already)
                return

        if action == "approve":
            self._dispatch_and_reply(d, "approve", project=project,
                                     params={"task_id": task_id})
            return
        if action == "reject_modify":
            # 피드백 텍스트를 받아 replan에 반영해야 하므로 즉시 dispatch하지 않는다.
            # force_reply 프롬프트를 띄우고, 다음 메시지를 pending 소비로 처리한다.
            if not self._prompt_modify_feedback(d, project, task_id):
                self._safe_send(
                    d.chat_id, d.thread_id,
                    "⚠️ 수정 요청 프롬프트 전송에 실패했습니다. 잠시 후 다시 시도해주세요.",
                )
            return
        if action == "reject_cancel":
            self._dispatch_and_reply(d, "cancel", project=project,
                                     params={"task_id": task_id})
            return
        if action == "view":
            self._dispatch_and_reply(d, "get_task", project=project,
                                     params={"task_id": task_id})
            return
        self._safe_send(d.chat_id, d.thread_id,
                        f"⚠️ 처리되지 않은 버튼 action: {action}")

    def _already_responded_notice(self, project: Optional[str],
                                   task_id: Optional[str]) -> Optional[str]:
        """task가 이미 응답 처리된 상태면 안내 메시지를 반환한다.

        waiting_for_human_plan_confirm 상태에 **있지 않으면** 이미 누군가
        Web/CLI/다른 채널에서 처리했거나, task가 이미 다른 phase로 넘어간 것.
        그 사실과 함께 누가 언제 어느 채널로 응답했는지 human_interaction.response에서
        읽어 사용자에게 알려준다.
        """
        if not project or not task_id:
            return None
        try:
            task = self._hub_api.get_task(project, task_id)
        except Exception:
            return None
        if task is None:
            return None

        status = getattr(task, "status", None) or (
            task.get("status") if isinstance(task, dict) else None
        )
        if status == "waiting_for_human_plan_confirm":
            return None  # 아직 대기 중 — 정상 dispatch 진행

        # 응답 기록 조회 (get_task가 Task dataclass일 수도, dict일 수도 있음).
        hi = None
        if isinstance(task, dict):
            hi = task.get("human_interaction")
        else:
            hi = getattr(task, "human_interaction", None)
            if hi is not None and not isinstance(hi, dict):
                hi = getattr(hi, "__dict__", None) or {}

        response = (hi or {}).get("response") if isinstance(hi, dict) else None
        if not response:
            # 응답이 안 보이면 최소한 현재 상태만이라도 알려준다.
            return (
                f"⚠️ 이 버튼은 더 이상 유효하지 않습니다.\n"
                f"task {task_id} 현재 상태: {status or 'unknown'}"
            )

        action_label = {
            "approve": "✅ 승인됨",
            "modify": "📝 수정 요청됨",
        }.get(response.get("action"), response.get("action", "처리됨"))
        by = response.get("responded_by") or "unknown"
        src = response.get("source") or "unknown"
        at = response.get("responded_at", "")
        time_str = at[:19].replace("T", " ") if at else ""
        return (
            f"ℹ️ 이 요청은 이미 {action_label}되었습니다.\n"
            f"by {by} (via {src}){' · ' + time_str if time_str else ''}\n"
            f"현재 상태: {status or 'unknown'}"
        )

    # ─── '수정 피드백 대기' pending state ───

    @staticmethod
    def _pending_modify_key(chat_id: Optional[int],
                            thread_id: Optional[int]) -> str:
        """pending 저장소에서 한 topic을 식별하는 키. thread_id가 없으면 0으로 정규화."""
        return f"{chat_id}_{thread_id or 0}"

    def _set_pending_modify(self, chat_id: int, thread_id: Optional[int],
                            project: str, task_id: str,
                            prompt_message_id: Optional[int]) -> None:
        """'📝 수정' 버튼을 눌러 피드백 텍스트 입력을 기다리는 상태를 영속한다.

        같은 topic에서 기존 pending이 있으면 덮어쓴다 (사용자가 한 번 더 수정 버튼을
        눌렀다면 새 task가 대상이 되는 상황이므로 최신 것이 맞다).
        """
        path = _pending_modify_path(self._root)
        with self._pending_modify_lock:
            state = _load_json(path, {"pending": {}})
            pending_map = state.setdefault("pending", {})
            pending_map[self._pending_modify_key(chat_id, thread_id)] = {
                "project": project,
                "task_id": task_id,
                "requested_at": datetime.now(timezone.utc).isoformat(),
                "prompt_message_id": prompt_message_id,
            }
            _save_json_atomic(path, state)

    def _pop_pending_modify(self, chat_id: int,
                            thread_id: Optional[int]) -> Optional[dict]:
        """pending 항목을 제거하고 돌려준다. TTL이 지났다면 제거만 하고 None 반환.

        다음 메시지 도착 시 1회용으로 소비되어야 하므로 존재 여부에 관계없이 삭제한다.
        """
        path = _pending_modify_path(self._root)
        key = self._pending_modify_key(chat_id, thread_id)
        with self._pending_modify_lock:
            state = _load_json(path, {"pending": {}})
            pending_map = state.get("pending") or {}
            entry = pending_map.pop(key, None)
            state["pending"] = pending_map
            _save_json_atomic(path, state)
        if not entry:
            return None
        # TTL — 사용자가 수정 버튼을 누르고 한참 뒤 다른 메시지를 보낸 경우 소비하지 않는다.
        requested_at_str = entry.get("requested_at", "")
        try:
            requested_at = datetime.fromisoformat(requested_at_str)
            age_seconds = (datetime.now(timezone.utc) - requested_at).total_seconds()
        except (TypeError, ValueError):
            age_seconds = 0
        if age_seconds > _PENDING_MODIFY_TTL_SECONDS:
            logger.info("pending modify 만료(%.0fs 경과, key=%s) — 무시",
                        age_seconds, key)
            return None
        return entry

    def _prompt_modify_feedback(self, d: RoutingDecision,
                                 project: str, task_id: str) -> bool:
        """'수정' 버튼 클릭 시 피드백을 요청하는 force_reply 프롬프트를 띄운다.

        성공 시 pending state에 (chat_id, thread_id) 키로 task 정보를 기록한다.
        다음 메시지 도착 시 update_loop가 이 pending을 소비해 hub_api.reject로 dispatch한다.

        Returns:
            프롬프트 전송이 성공했으면 True. 실패 시 False — 호출자가 사용자에게 안내한다.
        """
        prompt_text = (
            f"📝 task {task_id}에 대한 수정 요청 내용을 이 topic에 답장으로 보내주세요.\n"
            f"이 메시지가 다음 Planner에 그대로 전달됩니다.\n"
            f"(10분 안에 보내지 않으면 취소됩니다. 그 사이 슬래시 명령을 보내면 "
            f"수정 요청이 취소되고 해당 명령이 실행됩니다.)"
        )
        force_reply_markup = {
            "force_reply": True,
            "input_field_placeholder": "수정 요청 내용을 입력하세요",
            "selective": True,
        }
        try:
            resp = self._client.send_message(
                chat_id=d.chat_id,
                text=prompt_text,
                message_thread_id=d.thread_id,
                reply_markup=force_reply_markup,
            )
        except TelegramAPIError as exc:
            logger.warning("수정 피드백 프롬프트 전송 실패: %s", exc)
            return False

        prompt_message_id = (resp.get("result") or {}).get("message_id")
        self._set_pending_modify(d.chat_id, d.thread_id, project, task_id,
                                 prompt_message_id)
        return True

    def _dispatch_and_reply(self, d: RoutingDecision, action: str,
                            project: Optional[str] = None,
                            params: Optional[dict] = None) -> None:
        """hub_api.dispatch 호출 후 응답을 짧게 요약해 topic으로 회신."""
        try:
            req = Request(action=action, project=project,
                          params=params or {}, source="telegram",
                          requested_by=_requested_by_for(d))
            resp = dispatch(self._hub_api, req)
            if resp.success:
                summary = self._summarize_response(action, resp.data, resp.message)
            else:
                err_msg = (resp.error or {}).get("message", resp.message or "unknown")
                summary = f"⚠️ {action} 실패: {err_msg}"
        except Exception as exc:  # noqa: BLE001
            logger.exception("dispatch 실패 (%s): %s", action, exc)
            summary = f"⚠️ {action} 실행 중 오류: {exc}"
        self._safe_send(d.chat_id, d.thread_id, summary)

    def _summarize_response(self, action: str, data, message: str = "") -> str:
        """응답 요약. MarkdownV2 escape가 번거로워 plain text로 반환한다."""
        if not data:
            return message or f"✅ {action} 완료"
        try:
            text = json.dumps(data, ensure_ascii=False, indent=2,
                              default=_dataclass_to_dict)
        except Exception:  # noqa: BLE001
            text = str(data)
        if len(text) > 3500:
            text = text[:3500] + "\n... (잘림)"
        header = message or f"✅ {action}"
        return f"{header}\n{text}"

    # ═══════════════════════════════════════════════════════════
    # 2) Notification loop
    # ═══════════════════════════════════════════════════════════

    def _notification_loop(self) -> None:
        """2초 주기로 projects/*/notifications.json을 스캔 → 신규 항목 전송."""
        state_path = _last_notification_path(self._root)
        state = _load_json(state_path, {"last_created_at": {}})
        projects_dir = os.path.join(self._root, "projects")

        while not self._stop_event.is_set():
            try:
                for project in _list_projects(projects_dir):
                    self._flush_project_notifications(project, state)
                _save_json_atomic(state_path, state)
            except Exception as exc:  # noqa: BLE001
                logger.exception("notification loop 오류: %s", exc)
            self._stop_event.wait(timeout=2.0)

    def _flush_project_notifications(self, project: str, state: dict) -> None:
        project_dir = os.path.join(self._root, "projects", project)
        noti_path = os.path.join(project_dir, "notifications.json")
        if not os.path.exists(noti_path):
            return
        binding = self._telegram_binding(project)
        if not binding:
            return  # topic이 아직 없는 프로젝트는 알림 전송 대상 아님

        try:
            with open(noti_path) as f:
                notifications = json.load(f)
        except (json.JSONDecodeError, OSError):
            return

        last_ts = state.get("last_created_at", {}).get(project, "")
        new_items = [n for n in notifications
                     if n.get("created_at", "") > last_ts]
        if not new_items:
            return

        # created_at 오름차순으로 전송
        new_items.sort(key=lambda n: n.get("created_at", ""))
        max_sent = last_ts
        for noti in new_items:
            noti["project"] = project  # formatter가 reply_markup에서 참조
            text = format_notification(noti)
            keyboard = reply_markup_for_notification(noti)
            try:
                self._client.send_message(
                    chat_id=binding["chat_id"],
                    text=text,
                    message_thread_id=binding["thread_id"],
                    parse_mode="MarkdownV2",
                    reply_markup=keyboard,
                )
                max_sent = max(max_sent, noti.get("created_at", ""))
            except TelegramAPIError as exc:
                logger.warning("알림 전송 실패 (project=%s): %s — 다음 사이클 재시도",
                               project, exc)
                break  # 이후 알림은 다음 사이클에 재시도 (순서 유지)

        state.setdefault("last_created_at", {})[project] = max_sent

    # ═══════════════════════════════════════════════════════════
    # 3) Command loop (HubAPI hook → bridge)
    # ═══════════════════════════════════════════════════════════

    def _command_loop(self) -> None:
        """data/telegram_commands/*.json 폴링 → 처리 → 파일 삭제."""
        cmd_dir = _commands_dir(self._root)
        os.makedirs(cmd_dir, exist_ok=True)

        while not self._stop_event.is_set():
            try:
                for fname in sorted(os.listdir(cmd_dir)):
                    if not fname.endswith(".json"):
                        continue
                    fpath = os.path.join(cmd_dir, fname)
                    try:
                        with open(fpath) as f:
                            cmd = json.load(f)
                        self._handle_command(cmd)
                    except Exception as exc:  # noqa: BLE001
                        logger.exception("command 처리 실패 (%s): %s", fname, exc)
                    finally:
                        # 실패해도 무한 재시도 방지를 위해 제거 (상위 로그로 복구 판단).
                        try:
                            os.unlink(fpath)
                        except OSError:
                            pass
            except Exception as exc:  # noqa: BLE001
                logger.exception("command loop 오류: %s", exc)
            self._stop_event.wait(timeout=1.0)

    def _handle_command(self, cmd: dict) -> None:
        action = cmd.get("action")
        project = cmd.get("project")
        if not action or not project:
            logger.warning("잘못된 command: %s", cmd)
            return

        tg_conf = self._tg_config()
        hub_chat_id = tg_conf.get("hub_chat_id") or 0
        if not hub_chat_id:
            logger.warning("hub_chat_id 미설정 — command %s 무시 (project=%s). "
                           "/bind_hub 먼저 수행하세요.", action, project)
            return

        if action == "create_topic":
            self._cmd_create_topic(hub_chat_id, project)
        elif action == "close_topic":
            self._cmd_close_topic(hub_chat_id, project)
        elif action == "reopen_topic":
            self._cmd_reopen_topic(hub_chat_id, project)
        else:
            logger.warning("알 수 없는 command action: %s", action)

    def _cmd_create_topic(self, chat_id: int, project: str) -> None:
        binding = self._telegram_binding(project)
        if binding and binding.get("thread_id"):
            logger.info("project=%s 이미 topic 존재 (thread_id=%s) — skip",
                        project, binding["thread_id"])
            return
        try:
            resp = self._client.create_forum_topic(chat_id=chat_id, name=project)
            thread_id = resp["result"]["message_thread_id"]
        except TelegramAPIError as exc:
            logger.error("createForumTopic 실패 (project=%s): %s", project, exc)
            return

        self._write_binding(project, {"chat_id": chat_id, "thread_id": thread_id})
        welcome = (
            f"🆕 프로젝트 *{escape_markdown_v2(project)}* 연결됨\n"
            f"사용 가능 명령: /status /list /pending /help\n"
            "또는 자연어로 직접 요청하세요\\."
        )
        try:
            self._client.send_message(chat_id=chat_id, text=welcome,
                                      message_thread_id=thread_id,
                                      parse_mode="MarkdownV2")
        except TelegramAPIError as exc:
            logger.warning("환영 메시지 전송 실패 (project=%s): %s", project, exc)

    def _cmd_close_topic(self, chat_id: int, project: str) -> None:
        binding = self._telegram_binding(project)
        if not binding or not binding.get("thread_id"):
            return
        try:
            self._client.close_forum_topic(chat_id, binding["thread_id"])
        except TelegramAPIError as exc:
            logger.warning("closeForumTopic 실패 (project=%s): %s", project, exc)

    def _cmd_reopen_topic(self, chat_id: int, project: str) -> None:
        binding = self._telegram_binding(project)
        if not binding or not binding.get("thread_id"):
            return
        try:
            self._client.reopen_forum_topic(chat_id, binding["thread_id"])
        except TelegramAPIError as exc:
            logger.warning("reopenForumTopic 실패 (project=%s): %s", project, exc)

    # ═══════════════════════════════════════════════════════════
    # project_state.json telegram binding util
    # ═══════════════════════════════════════════════════════════

    def _telegram_binding(self, project: str) -> Optional[dict]:
        state = _load_json(_project_state_path(self._root, project), {})
        return state.get("telegram") if isinstance(state, dict) else None

    def _write_binding(self, project: str, binding: dict) -> None:
        path = _project_state_path(self._root, project)
        state = _load_json(path, {})
        if not isinstance(state, dict):
            state = {}
        state["telegram"] = binding
        _save_json_atomic(path, state)

    def _project_for_thread(self, chat_id: int, thread_id: Optional[int]) -> Optional[str]:
        """chat_id + thread_id 조합이 바인딩된 프로젝트를 찾는다 (선형 스캔)."""
        if thread_id is None:
            return None
        projects_dir = os.path.join(self._root, "projects")
        for project in _list_projects(projects_dir):
            binding = self._telegram_binding(project)
            if (binding and binding.get("chat_id") == chat_id
                    and binding.get("thread_id") == thread_id):
                return project
        return None

    # ═══════════════════════════════════════════════════════════
    # offset / sending util
    # ═══════════════════════════════════════════════════════════

    def _load_offset(self) -> int:
        data = _load_json(_offset_path(self._root), {})
        return int(data.get("offset", 0))

    def _save_offset(self, offset: int) -> None:
        _save_json_atomic(_offset_path(self._root), {"offset": int(offset)})

    def _safe_send(self, chat_id: int, thread_id: Optional[int], text: str,
                   parse_mode: Optional[str] = None) -> None:
        try:
            self._client.send_message(chat_id=chat_id, text=text,
                                      message_thread_id=thread_id,
                                      parse_mode=parse_mode)
        except TelegramAPIError as exc:
            logger.warning("sendMessage 실패: %s", exc)


# ═══════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════

_HELP_TEXT = (
    "사용 가능한 명령:\n"
    "/status — 프로젝트 현재 상태\n"
    "/list [--status <s>] — task 목록\n"
    "/pending — 승인 대기 항목\n"
    "/cancel <task_id> — task 취소\n"
    "/new_session — 대화 세션 초기화\n"
    "/help — 이 도움말\n"
    "또는 자연어로 요청하세요 (예: \"로그인 기능 구현해줘\")."
)


def _requested_by_for(d: RoutingDecision) -> Optional[str]:
    """RoutingDecision → 'tg:<display>' 태그. user_display가 없으면 user_id fallback."""
    tag = d.user_display or (str(d.user_id) if d.user_id is not None else None)
    if not tag:
        return None
    return f"tg:{tag}"


def _list_projects(projects_dir: str) -> list[str]:
    if not os.path.isdir(projects_dir):
        return []
    return sorted(d for d in os.listdir(projects_dir)
                  if os.path.isdir(os.path.join(projects_dir, d)))


def _load_yaml(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _save_yaml(path: str, data: dict) -> None:
    tmp = path + f".tmp-{os.getpid()}"
    with open(tmp, "w") as f:
        yaml.safe_dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    os.replace(tmp, path)


def _dataclass_to_dict(obj):
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    raise TypeError(f"Type not serializable: {type(obj).__name__}")


# ─── main ───

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Telegram bridge (Phase 2.3)")
    parser.add_argument("--config", help="config.yaml 경로 (기본: <agent_hub_root>/config.yaml)")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args(argv)

    agent_hub_root = os.environ.get("AGENT_HUB_ROOT") or os.path.dirname(_SCRIPT_DIR)
    config_path = args.config or os.path.join(agent_hub_root, "config.yaml")
    if not os.path.exists(config_path):
        print(f"config.yaml을 찾을 수 없습니다: {config_path}", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    bridge = TelegramBridge(agent_hub_root=agent_hub_root, config_path=config_path)
    if not bridge._enabled:
        logger.info("telegram 비활성 — 종료")
        return 0
    bridge.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
