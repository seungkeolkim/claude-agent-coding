#!/usr/bin/env python3
"""
Agent Hub CLI 프론트엔드.

HubAPI를 import하여 사용자 명령을 처리한다.
run_agent.sh에서 서브커맨드로 호출됨.

사용법:
    python3 scripts/cli.py submit --project <name> --title "제목" --description "설명"
    python3 scripts/cli.py list [--project <name>] [--status <status>]
    python3 scripts/cli.py pending [--project <name>]
    python3 scripts/cli.py approve <task_id> --project <name> [--message "메시지"]
    python3 scripts/cli.py reject <task_id> --project <name> --message "메시지"
    python3 scripts/cli.py feedback <task_id> --project <name> --message "메시지"
    python3 scripts/cli.py config --project <name> --set "key=value"
    python3 scripts/cli.py pause --project <name> [<task_id>]
    python3 scripts/cli.py resume --project <name> [<task_id>]
    python3 scripts/cli.py cancel <task_id> --project <name>
"""

import argparse
import json
import os
import sys
from pathlib import Path

# hub_api 패키지를 import할 수 있도록 scripts/ 디렉토리를 path에 추가
SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from hub_api.core import HubAPI


# ─── 색상 출력 ───
GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
RED = "\033[0;31m"
CYAN = "\033[0;36m"
PURPLE_BOLD = "\033[1;35m"
BOLD = "\033[1m"
NC = "\033[0m"

# 상태별 색상
STATUS_COLORS = {
    "submitted": CYAN,
    "queued": CYAN,
    "planned": GREEN,
    "waiting_for_human_plan_confirm": YELLOW,
    "in_progress": PURPLE_BOLD,
    "running": PURPLE_BOLD,
    "waiting_for_human_pr_approve": YELLOW,
    "completed": GREEN,
    "needs_replan": YELLOW,
    "escalated": RED,
    "failed": RED,
    "cancelled": RED,
    "idle": NC,
}


def colored_status(status: str) -> str:
    """상태에 맞는 색상을 적용한 문자열을 반환한다."""
    color = STATUS_COLORS.get(status, NC)
    return f"{color}{status}{NC}"


def get_hub_api() -> HubAPI:
    """프로젝트 루트를 자동 탐지하여 HubAPI 인스턴스를 생성한다."""
    # 환경변수 우선, 없으면 스크립트 위치 기준
    root = os.environ.get("AGENT_HUB_ROOT")
    if not root:
        root = str(Path(__file__).resolve().parent.parent)
    return HubAPI(root)


# ═══════════════════════════════════════════════════════════
# 서브커맨드 핸들러
# ═══════════════════════════════════════════════════════════


def cmd_submit(args):
    """task를 제출한다."""
    api = get_hub_api()

    # 첨부파일 처리
    attachments = None
    if args.attach:
        attachments = []
        for path in args.attach:
            if not os.path.isfile(path):
                print(f"{RED}[ERROR]{NC} 첨부파일을 찾을 수 없음: {path}", file=sys.stderr)
                sys.exit(1)
            attachments.append({
                "path": os.path.abspath(path),
                "filename": os.path.basename(path),
                "type": "reference",
                "description": "",
            })

    # config_override 처리
    config_override = None
    if args.test == "none":
        config_override = {
            "testing": {
                "unit_test": {"enabled": False},
                "e2e_test": {"enabled": False},
                "integration_test": {"enabled": False},
            }
        }

    try:
        result = api.submit(
            project=args.project,
            title=args.title,
            description=args.description or "",
            attachments=attachments,
            config_override=config_override,
        )
        print(f"{GREEN}[OK]{NC} task 제출 완료")
        print(f"  task_id:  {BOLD}{result.task_id}{NC}")
        print(f"  project:  {result.project}")
        print(f"  file:     {os.path.basename(result.file_path)}")
    except FileNotFoundError as e:
        print(f"{RED}[ERROR]{NC} {e}", file=sys.stderr)
        sys.exit(1)


def cmd_list(args):
    """task 목록을 조회한다."""
    api = get_hub_api()
    tasks = api.list_tasks(project=args.project, status=args.status)

    if not tasks:
        print(f"{YELLOW}[INFO]{NC} 조건에 맞는 task가 없습니다.")
        return

    # 테이블 출력
    print(f"\n{'ID':<8} {'프로젝트':<20} {'상태':<20} {'제목'}")
    print("─" * 75)
    for t in tasks:
        status_str = colored_status(t.status)
        # 색상 코드를 포함하므로 ljust 수동 패딩
        status_padding = 20 + len(status_str) - len(t.status)
        print(f"{t.task_id:<8} {t.project:<20} {status_str:<{status_padding}} {t.title}")

    print(f"\n총 {len(tasks)}개")


def cmd_pending(args):
    """사용자 응답 대기 중인 interaction을 조회한다."""
    api = get_hub_api()
    pending = api.pending(project=args.project)

    if not pending:
        print(f"{GREEN}[INFO]{NC} 대기 중인 항목이 없습니다.")
        return

    for hi in pending:
        print(f"\n{YELLOW}[PENDING]{NC} {hi.project} / task {BOLD}{hi.task_id}{NC}")
        print(f"  유형:    {hi.interaction_type}")
        print(f"  메시지:  {hi.message}")
        if hi.options:
            print(f"  옵션:    {', '.join(hi.options)}")
        if hi.payload_path:
            print(f"  상세:    {hi.payload_path}")
        if hi.requested_at:
            print(f"  요청일:  {hi.requested_at}")

    print(f"\n총 {len(pending)}개 대기 중")


def cmd_approve(args):
    """plan/replan을 승인한다."""
    api = get_hub_api()
    try:
        ok = api.approve(args.project, args.task_id, message=args.message)
        if ok:
            print(f"{GREEN}[OK]{NC} task {args.task_id} 승인 완료")
        else:
            print(f"{YELLOW}[WARN]{NC} task {args.task_id}가 승인 대기 상태가 아닙니다.")
    except FileNotFoundError as e:
        print(f"{RED}[ERROR]{NC} {e}", file=sys.stderr)
        sys.exit(1)


def cmd_reject(args):
    """plan/replan을 거부(수정 요청)한다."""
    api = get_hub_api()
    if not args.message:
        print(f"{RED}[ERROR]{NC} --message는 필수입니다 (거부 사유).", file=sys.stderr)
        sys.exit(1)
    try:
        ok = api.reject(args.project, args.task_id, message=args.message)
        if ok:
            print(f"{GREEN}[OK]{NC} task {args.task_id} 수정 요청 완료")
        else:
            print(f"{YELLOW}[WARN]{NC} task {args.task_id}가 승인 대기 상태가 아닙니다.")
    except FileNotFoundError as e:
        print(f"{RED}[ERROR]{NC} {e}", file=sys.stderr)
        sys.exit(1)


def cmd_feedback(args):
    """실행 중인 task에 피드백을 추가한다."""
    api = get_hub_api()
    if not args.message:
        print(f"{RED}[ERROR]{NC} --message는 필수입니다.", file=sys.stderr)
        sys.exit(1)
    try:
        api.feedback(args.project, args.task_id, message=args.message)
        print(f"{GREEN}[OK]{NC} task {args.task_id}에 피드백 추가 완료")
    except FileNotFoundError as e:
        print(f"{RED}[ERROR]{NC} {e}", file=sys.stderr)
        sys.exit(1)


def cmd_config(args):
    """프로젝트 설정을 동적으로 변경한다."""
    api = get_hub_api()

    if not args.set:
        print(f"{RED}[ERROR]{NC} --set 옵션이 필요합니다.", file=sys.stderr)
        print(f"  예: --set 'testing.unit_test.enabled=true'")
        sys.exit(1)

    # --set "key=value" 파싱 → 중첩 dict로 변환
    changes = {}
    for kv in args.set:
        if "=" not in kv:
            print(f"{RED}[ERROR]{NC} 잘못된 형식: {kv} (key=value 형식 필요)", file=sys.stderr)
            sys.exit(1)
        key, value = kv.split("=", 1)
        value = _parse_value(value)
        _set_nested(changes, key.split("."), value)

    try:
        overrides = api.config(args.project, changes)
        print(f"{GREEN}[OK]{NC} {args.project} 설정 변경 완료")
        print(json.dumps(overrides, ensure_ascii=False, indent=2))
    except FileNotFoundError as e:
        print(f"{RED}[ERROR]{NC} {e}", file=sys.stderr)
        sys.exit(1)


def cmd_pause(args):
    """프로젝트 또는 task를 일시정지한다."""
    api = get_hub_api()
    try:
        api.pause(args.project, task_id=args.task_id)
        target = f"task {args.task_id}" if args.task_id else args.project
        print(f"{GREEN}[OK]{NC} {target} 일시정지 요청 전달")
    except FileNotFoundError as e:
        print(f"{RED}[ERROR]{NC} {e}", file=sys.stderr)
        sys.exit(1)


def cmd_resume(args):
    """프로젝트 또는 task를 재개한다."""
    api = get_hub_api()
    try:
        api.resume(args.project, task_id=args.task_id)
        target = f"task {args.task_id}" if args.task_id else args.project
        print(f"{GREEN}[OK]{NC} {target} 재개 요청 전달")
    except FileNotFoundError as e:
        print(f"{RED}[ERROR]{NC} {e}", file=sys.stderr)
        sys.exit(1)


def cmd_notifications(args):
    """알림 목록을 조회한다."""
    api = get_hub_api()
    notifications = api.notifications(
        project=args.project,
        limit=args.limit,
        unread_only=args.unread,
    )

    if not notifications:
        print(f"{GREEN}[INFO]{NC} 알림이 없습니다.")
        return

    # notification 모듈에서 포맷 함수 가져오기
    scripts_dir = str(Path(__file__).resolve().parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    from notification import format_notification_cli

    for n in notifications:
        print(format_notification_cli(n, project_name=n.get("project")))

    print(f"\n총 {len(notifications)}개")


def cmd_cancel(args):
    """task를 취소한다."""
    api = get_hub_api()
    try:
        ok = api.cancel(args.project, args.task_id)
        if ok:
            print(f"{GREEN}[OK]{NC} task {args.task_id} 취소 완료")
        else:
            print(f"{YELLOW}[WARN]{NC} task {args.task_id}는 취소할 수 없는 상태입니다.")
    except FileNotFoundError as e:
        print(f"{RED}[ERROR]{NC} {e}", file=sys.stderr)
        sys.exit(1)


# ═══════════════════════════════════════════════════════════
# 유틸리티
# ═══════════════════════════════════════════════════════════


def _parse_value(value_str: str):
    """문자열 값을 적절한 Python 타입으로 변환한다."""
    lower = value_str.lower()
    if lower == "true":
        return True
    if lower == "false":
        return False
    if lower == "null" or lower == "none":
        return None
    try:
        return int(value_str)
    except ValueError:
        pass
    try:
        return float(value_str)
    except ValueError:
        pass
    return value_str


def _set_nested(d: dict, keys: list, value):
    """점(.) 으로 구분된 키 경로로 중첩 dict에 값을 설정한다."""
    for key in keys[:-1]:
        if key not in d or not isinstance(d[key], dict):
            d[key] = {}
        d = d[key]
    d[keys[-1]] = value


# ═══════════════════════════════════════════════════════════
# argparse 설정
# ═══════════════════════════════════════════════════════════


def build_parser() -> argparse.ArgumentParser:
    """CLI 파서를 구성한다."""
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="Agent Hub CLI — task 제출, 조회, 승인/거부, 설정 변경",
    )
    subparsers = parser.add_subparsers(dest="command", help="사용 가능한 명령")

    # ─── submit ───
    sp = subparsers.add_parser("submit", help="새 task 제출")
    sp.add_argument("--project", required=True, help="프로젝트명")
    sp.add_argument("--title", required=True, help="task 제목")
    sp.add_argument("--description", default="", help="task 상세 설명")
    sp.add_argument("--attach", action="append", help="첨부파일 경로 (여러 개 가능)")
    sp.add_argument("--test", choices=["none"], help="테스트 설정 (none: 전부 비활성화)")
    sp.set_defaults(func=cmd_submit)

    # ─── list ───
    sp = subparsers.add_parser("list", help="task 목록 조회")
    sp.add_argument("--project", help="프로젝트명 (미지정시 전체)")
    sp.add_argument("--status", help="상태 필터 (submitted, in_progress, completed, failed 등)")
    sp.set_defaults(func=cmd_list)

    # ─── pending ───
    sp = subparsers.add_parser("pending", help="사용자 응답 대기 항목 조회")
    sp.add_argument("--project", help="프로젝트명 (미지정시 전체)")
    sp.set_defaults(func=cmd_pending)

    # ─── approve ───
    sp = subparsers.add_parser("approve", help="plan/replan 승인")
    sp.add_argument("task_id", help="task ID")
    sp.add_argument("--project", required=True, help="프로젝트명")
    sp.add_argument("--message", help="승인 코멘트 (선택)")
    sp.set_defaults(func=cmd_approve)

    # ─── reject ───
    sp = subparsers.add_parser("reject", help="plan/replan 거부 (수정 요청)")
    sp.add_argument("task_id", help="task ID")
    sp.add_argument("--project", required=True, help="프로젝트명")
    sp.add_argument("--message", required=True, help="거부 사유 및 수정 요청 내용")
    sp.add_argument("--attach", action="append", help="첨부파일 경로")
    sp.set_defaults(func=cmd_reject)

    # ─── feedback ───
    sp = subparsers.add_parser("feedback", help="실행 중 task에 피드백 추가")
    sp.add_argument("task_id", help="task ID")
    sp.add_argument("--project", required=True, help="프로젝트명")
    sp.add_argument("--message", required=True, help="피드백 내용")
    sp.add_argument("--attach", action="append", help="첨부파일 경로")
    sp.set_defaults(func=cmd_feedback)

    # ─── config ───
    sp = subparsers.add_parser("config", help="프로젝트 설정 동적 변경")
    sp.add_argument("--project", required=True, help="프로젝트명")
    sp.add_argument("--set", action="append",
                    help="설정 변경 (key=value, 점(.)으로 중첩 가능, 여러 개 가능)")
    sp.set_defaults(func=cmd_config)

    # ─── pause ───
    sp = subparsers.add_parser("pause", help="프로젝트 또는 task 일시정지")
    sp.add_argument("--project", required=True, help="프로젝트명")
    sp.add_argument("task_id", nargs="?", help="task ID (미지정시 프로젝트 전체)")
    sp.set_defaults(func=cmd_pause)

    # ─── resume ───
    sp = subparsers.add_parser("resume", help="프로젝트 또는 task 재개")
    sp.add_argument("--project", required=True, help="프로젝트명")
    sp.add_argument("task_id", nargs="?", help="task ID (미지정시 프로젝트 전체)")
    sp.set_defaults(func=cmd_resume)

    # ─── cancel ───
    sp = subparsers.add_parser("cancel", help="task 취소")
    sp.add_argument("task_id", help="task ID")
    sp.add_argument("--project", required=True, help="프로젝트명")
    sp.set_defaults(func=cmd_cancel)

    # ─── notifications ───
    sp = subparsers.add_parser("notifications", help="알림 목록 조회")
    sp.add_argument("--project", help="프로젝트명 (미지정시 전체)")
    sp.add_argument("--limit", type=int, default=20, help="최대 표시 개수 (기본 20)")
    sp.add_argument("--unread", action="store_true", help="안 읽은 알림만 표시")
    sp.set_defaults(func=cmd_notifications)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
