#!/usr/bin/env bash
# run_claude_agent.sh — Claude Code 세션 기동 래퍼 (v2)
#
# 사용법:
#   ./scripts/run_claude_agent.sh <agent_type> \
#       --config <config.yaml 경로> \
#       --project-yaml <project.yaml 경로> \
#       --task-file <task JSON 경로> \
#       [--subtask-file <subtask JSON 경로>] \
#       [--dry-run]
#
# agent_type: planner | coder | reviewer | setup | unit_tester | e2e_tester | reporter

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_HUB_ROOT="$(dirname "$SCRIPT_DIR")"

# ─── 유효한 agent 목록 ───
VALID_AGENTS="planner coder reviewer setup unit_tester e2e_tester reporter memory_updater summarizer"

# ─── 인자 파싱 ───
AGENT_TYPE="${1:?agent_type을 지정하세요 (planner|coder|reviewer|setup|unit_tester|e2e_tester|reporter)}"
shift

CONFIG_FILE=""
PROJECT_YAML=""
TASK_FILE=""
SUBTASK_FILE=""
DRY_RUN=false
DUMMY=false
FORCE_RESULT=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --config)
            CONFIG_FILE="$2"
            shift 2
            ;;
        --project-yaml)
            PROJECT_YAML="$2"
            shift 2
            ;;
        --task-file)
            TASK_FILE="$2"
            shift 2
            ;;
        --subtask-file)
            SUBTASK_FILE="$2"
            shift 2
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --dummy)
            DUMMY=true
            shift
            ;;
        --force-result)
            FORCE_RESULT="$2"
            shift 2
            ;;
        *)
            echo "[ERROR] 알 수 없는 옵션: $1" >&2
            exit 1
            ;;
    esac
done

# ─── 필수 인자 검증 ───
if [[ -z "$CONFIG_FILE" ]]; then
    echo "[ERROR] --config 옵션이 필요합니다." >&2
    exit 1
fi
if [[ -z "$PROJECT_YAML" ]]; then
    echo "[ERROR] --project-yaml 옵션이 필요합니다." >&2
    exit 1
fi
if [[ -z "$TASK_FILE" ]]; then
    echo "[ERROR] --task-file 옵션이 필요합니다." >&2
    exit 1
fi

# agent_type 유효성 검증
if ! echo "$VALID_AGENTS" | grep -qw "$AGENT_TYPE"; then
    echo "[ERROR] 유효하지 않은 agent_type: ${AGENT_TYPE}" >&2
    echo "[ERROR] 가능한 값: ${VALID_AGENTS}" >&2
    exit 1
fi

# 파일 존재 검증
for check_file in "$CONFIG_FILE" "$PROJECT_YAML" "$TASK_FILE"; do
    if [[ ! -f "$check_file" ]]; then
        echo "[ERROR] 파일을 찾을 수 없음: ${check_file}" >&2
        exit 1
    fi
done
if [[ -n "$SUBTASK_FILE" && ! -f "$SUBTASK_FILE" ]]; then
    echo "[ERROR] subtask 파일을 찾을 수 없음: ${SUBTASK_FILE}" >&2
    exit 1
fi

# 프롬프트 파일 검증
PROMPT_FILE="${AGENT_HUB_ROOT}/config/agent_prompts/${AGENT_TYPE}.md"
if [[ ! -f "$PROMPT_FILE" ]]; then
    echo "[ERROR] 프롬프트 파일 없음: ${PROMPT_FILE}" >&2
    exit 1
fi

# ─── YAML 값 읽기 헬퍼 ───
# 점 표기법으로 nested key에 접근. 키가 없으면 빈 문자열 반환.
read_yaml_value() {
    local yaml_file="$1"
    local key_path="$2"
    python3 -c "
import yaml
with open('${yaml_file}') as f:
    config = yaml.safe_load(f)
keys = '${key_path}'.split('.')
val = config
for k in keys:
    if val is None or not isinstance(val, dict):
        val = None
        break
    val = val.get(k)
print(val if val is not None else '')
"
}

# ─── 모델 결정 (config.yaml → project.yaml override) ───
# 1차: config.yaml에서 claude.{agent_type}_model 읽기
MODEL=$(read_yaml_value "$CONFIG_FILE" "claude.${AGENT_TYPE}_model")

# fallback: config.yaml의 claude.coder_model
if [[ -z "$MODEL" ]]; then
    MODEL=$(read_yaml_value "$CONFIG_FILE" "claude.coder_model")
fi

# 2차: project.yaml에서 claude.{agent_type}_model override 확인
PROJECT_MODEL=$(read_yaml_value "$PROJECT_YAML" "claude.${AGENT_TYPE}_model")
if [[ -n "$PROJECT_MODEL" ]]; then
    MODEL="$PROJECT_MODEL"
fi

# 최종 fallback
if [[ -z "$MODEL" ]]; then
    MODEL="sonnet"
fi

# ─── 코드베이스 경로 ───
CODEBASE_PATH=$(read_yaml_value "$PROJECT_YAML" "codebase.path")
if [[ -z "$CODEBASE_PATH" ]]; then
    echo "[ERROR] project.yaml에 codebase.path가 설정되지 않았습니다." >&2
    exit 1
fi
if [[ ! -d "$CODEBASE_PATH" ]]; then
    echo "[ERROR] 코드베이스 경로가 존재하지 않음: ${CODEBASE_PATH}" >&2
    exit 1
fi

# ─── task ID 추출 (task JSON에서) ───
TASK_ID=$(python3 -c "
import json
with open('${TASK_FILE}') as f:
    print(json.load(f).get('task_id', 'UNKNOWN'))
")

# ─── 프로젝트 디렉토리 (project.yaml의 상위) ───
PROJECT_DIR="$(dirname "$PROJECT_YAML")"

# ─── 로그 디렉토리 ───
LOG_DIR="${PROJECT_DIR}/logs/${TASK_ID}"
mkdir -p "$LOG_DIR"

# ─── 파이프라인 단계 넘버링 ───
# 전체 파이프라인 순서에 맞게 번호를 부여한다.
# 테스트가 비활성화되어 실행되지 않아도 번호는 고정이다.
get_step_number() {
    case "$1" in
        planner)        echo "01" ;;
        coder)          echo "02" ;;
        reviewer)       echo "03" ;;
        setup)          echo "04" ;;
        unit_tester)    echo "05" ;;
        e2e_tester)     echo "06" ;;
        reporter)       echo "07" ;;
        memory_updater) echo "08" ;;
        summarizer)     echo "09" ;;
        *)              echo "99" ;;
    esac
}

# agent 이름을 로그용 표기로 변환 (snake_case → kebab-case)
get_step_name() {
    case "$1" in
        planner)        echo "planner" ;;
        coder)          echo "coder" ;;
        reviewer)       echo "reviewer" ;;
        setup)          echo "setup" ;;
        unit_tester)    echo "unit-tester" ;;
        e2e_tester)     echo "e2e-tester" ;;
        reporter)       echo "reporter" ;;
        memory_updater) echo "memory-updater" ;;
        summarizer)     echo "summarizer" ;;
        *)              echo "$1" ;;
    esac
}

STEP_NUM=$(get_step_number "$AGENT_TYPE")
STEP_NAME=$(get_step_name "$AGENT_TYPE")

# 로그 파일명 결정
# 형식:
#   task-level:    {task_id}_{step}-{name}.json
#   subtask-level: {task_id}_{subtask_num}_{step}-{name}_attempt-{N}.json
if [[ -n "$SUBTASK_FILE" ]]; then
    SUBTASK_ID=$(python3 -c "
import json
with open('${SUBTASK_FILE}') as f:
    print(json.load(f).get('subtask_id', 'UNKNOWN'))
")
    # subtask_id에서 순번 추출 (예: 00001-2 → 02)
    SUBTASK_SEQ=$(python3 -c "
sid = '${SUBTASK_ID}'
num = sid.split('-')[-1]
print(num.zfill(2))
")
    # retry_count 추출해서 attempt 번호로 사용
    RETRY_COUNT=$(python3 -c "
import json
with open('${TASK_FILE}') as f:
    task = json.load(f)
retry = task.get('counters', {}).get('current_subtask_retry', 0)
if retry == 0:
    with open('${SUBTASK_FILE}') as f:
        retry = json.load(f).get('retry_count', 0)
print(retry)
")
    ATTEMPT=$((RETRY_COUNT + 1))
    LOG_FILE="${LOG_DIR}/${TASK_ID}_${SUBTASK_SEQ}_${STEP_NUM}-${STEP_NAME}_attempt-${ATTEMPT}.json"
else
    SUBTASK_ID=""
    # task-level agent: planner=00, 그 외(integration test 등)=99
    if [[ "$AGENT_TYPE" == "planner" ]]; then
        TASK_LEVEL_SEQ="00"
    else
        TASK_LEVEL_SEQ="99"
    fi
    LOG_FILE="${LOG_DIR}/${TASK_ID}_${TASK_LEVEL_SEQ}_${STEP_NUM}-${STEP_NAME}.json"
fi

# ─── 실행 로그 파일 (.log = 전체 stdout/stderr, .json = claude 결과만) ───
EXEC_LOG_FILE="${LOG_FILE%.json}.log"

# stdout/stderr을 터미널과 실행 로그 파일 양쪽에 기록한다
exec > >(tee -a "$EXEC_LOG_FILE") 2>&1

# ─── 프로젝트 설명 추출 ───
PROJECT_DESCRIPTION=$(read_yaml_value "$PROJECT_YAML" "project.description")

# ─── 첨부 이미지 참조 지시 생성 ───
ATTACHMENT_INSTRUCTIONS=$(python3 -c "
import json
with open('${TASK_FILE}') as f:
    task = json.load(f)
attachments = task.get('attachments', [])
if attachments:
    print('## 첨부 자료')
    print('다음 첨부 파일을 Read 도구로 확인하세요:')
    for att in attachments:
        path = att.get('path', '')
        desc = att.get('description', '')
        print(f'- {path}: {desc}')
")

# ─── 사용자 원문 요청 추출 ───
# chatbot/web이 title/description으로 재해석하기 전에 사용자가 자연어로
# 말한 원문. submit 시 함께 저장되며, 재해석 과정에서 잃어버린 의도를
# 복원하는 "최우선 근거"로 모든 agent의 프롬프트 상단에 그대로 노출된다.
USER_REQUEST_RAW=$(python3 -c "
import json
with open('${TASK_FILE}') as f:
    task = json.load(f)
raw = task.get('user_request_raw') or ''
print(raw)
")

# ─── 프롬프트 조합 ───
PROMPT="$(cat "$PROMPT_FILE")"

# 사용자 원문 요청을 role 프롬프트 바로 다음에 배치 (가장 먼저 눈에 띄도록)
if [[ -n "$USER_REQUEST_RAW" ]]; then
    PROMPT="${PROMPT}

---
## 사용자 원문 요청 (최우선 근거)
아래는 사용자가 chatbot/Web에게 말한 **자연어 원문**입니다.
이후 나오는 title/description/subtask는 chatbot 또는 Planner가 이 원문을
재구성한 2차 가공물이므로, 해석 차이가 있다면 **아래 원문의 의도를 우선**합니다.
원문에서 읽히지 않는 요구사항을 임의로 추가하지 마세요.

\`\`\`
${USER_REQUEST_RAW}
\`\`\`"
fi

# 프로젝트 설명 추가
if [[ -n "$PROJECT_DESCRIPTION" ]]; then
    PROMPT="${PROMPT}

---
## 프로젝트 정보
${PROJECT_DESCRIPTION}"
fi

# 첨부 자료 참조 추가
if [[ -n "$ATTACHMENT_INSTRUCTIONS" ]]; then
    PROMPT="${PROMPT}

${ATTACHMENT_INSTRUCTIONS}"
fi

# task 컨텍스트 추가
PROMPT="${PROMPT}

---
## Task Context
\`\`\`json
$(cat "$TASK_FILE")
\`\`\`"

# subtask 컨텍스트 추가
if [[ -n "$SUBTASK_FILE" ]]; then
    PROMPT="${PROMPT}

## Subtask Context
\`\`\`json
$(cat "$SUBTASK_FILE")
\`\`\`"
fi

# plan 컨텍스트 추가 (존재하면)
PLAN_FILE="${PROJECT_DIR}/tasks/${TASK_ID}-plan.json"
if [[ -f "$PLAN_FILE" ]]; then
    PROMPT="${PROMPT}

## Plan Context
\`\`\`json
$(cat "$PLAN_FILE")
\`\`\`"
fi

# ─── 실행 정보 출력 ───
echo "[run_claude_agent] agent=${AGENT_TYPE} task=${TASK_ID} subtask=${SUBTASK_ID:-none} model=${MODEL}"
echo "[run_claude_agent] 코드베이스: ${CODEBASE_PATH}"
echo "[run_claude_agent] 로그: ${LOG_FILE}"

# ─── safety limit 체크 (dry-run, dummy 모드에서는 건너뜀) ───
if [[ "$DRY_RUN" != "true" && "$DUMMY" != "true" ]]; then
    python3 "${SCRIPT_DIR}/check_safety_limits.py" \
        --config "$CONFIG_FILE" \
        --project-yaml "$PROJECT_YAML" \
        --task-file "$TASK_FILE" \
        --agent-type "$AGENT_TYPE"

    if [[ $? -ne 0 ]]; then
        echo "[run_claude_agent] safety limit 초과로 실행 중단" >&2
        exit 1
    fi
fi

# ─── task JSON의 test_scenario에서 force_result 확인 ───
# CLI --force-result가 없을 때만 task JSON에서 읽는다
if [[ -z "$FORCE_RESULT" ]]; then
    FORCE_RESULT=$(python3 -c "
import json
with open('${TASK_FILE}') as f:
    task = json.load(f)
scenario = task.get('test_scenario', {})
agent_scenario = scenario.get('${AGENT_TYPE}', {})
force = agent_scenario.get('force_result', '')
at_attempt = agent_scenario.get('at_attempt', None)
if force and at_attempt is not None:
    # retry_count 기반으로 현재 attempt 계산
    counters = task.get('counters', {})
    current_attempt = counters.get('current_subtask_retry', 0) + 1
    if current_attempt != at_attempt:
        force = ''  # at_attempt와 현재 attempt가 다르면 정상 실행
print(force)
" 2>/dev/null || echo "")
fi

# ─── force-result 모드: claude 호출 없이 강제 결과 출력 ───
if [[ -n "$FORCE_RESULT" ]]; then
    echo ""
    echo "========== FORCE RESULT 모드: ${FORCE_RESULT} =========="

    case "${AGENT_TYPE}:${FORCE_RESULT}" in
        planner:approve)
            FORCED_JSON=$(cat <<EOJSON
{
  "action": "plan_created",
  "task_id": "${TASK_ID}",
  "forced": true,
  "subtasks": [
    {
      "subtask_id": "${TASK_ID}-1",
      "title": "[forced] 기본 구조 작성",
      "primary_responsibility": "기본 파일 구조와 설정 생성",
      "guidance": "프로젝트 초기 구조를 잡는다"
    }
  ]
}
EOJSON
)
            ;;
        coder:approve)
            FORCED_JSON=$(cat <<EOJSON
{
  "action": "code_complete",
  "task_id": "${TASK_ID}",
  "subtask_id": "${SUBTASK_ID:-none}",
  "forced": true,
  "changes_made": [
    {"file": "forced_file.txt", "change_type": "created", "summary": "[forced] 강제 완료"}
  ]
}
EOJSON
)
            ;;
        reviewer:approve)
            FORCED_JSON=$(cat <<EOJSON
{
  "action": "approved",
  "task_id": "${TASK_ID}",
  "subtask_id": "${SUBTASK_ID:-none}",
  "forced": true,
  "current_state_summary": "[forced] 강제 승인 — 상태 확인 생략",
  "summary": "[forced] 강제 승인"
}
EOJSON
)
            ;;
        reviewer:reject)
            FORCED_JSON=$(cat <<EOJSON
{
  "action": "rejected",
  "task_id": "${TASK_ID}",
  "subtask_id": "${SUBTASK_ID:-none}",
  "forced": true,
  "retry_mode": "continue",
  "current_state_summary": "[forced] 강제 거절 시나리오 — 실제 상태 확인 생략",
  "what_is_wrong": "[forced] 강제 거절 트리거",
  "what_should_be": "[forced] 후속 attempt에서 수정",
  "actionable_instructions": ["[forced] 강제 거절로 인한 재시도 — 실제 지시 없음"],
  "feedback": "[forced] 강제 거절 — 루프백 테스트"
}
EOJSON
)
            ;;
        reporter:pass)
            FORCED_JSON=$(cat <<EOJSON
{
  "action": "report_complete",
  "task_id": "${TASK_ID}",
  "subtask_id": "${SUBTASK_ID:-none}",
  "forced": true,
  "verdict": "pass",
  "needs_replan": false,
  "summary": "[forced] 강제 통과"
}
EOJSON
)
            ;;
        reporter:fail)
            FORCED_JSON=$(cat <<EOJSON
{
  "action": "report_complete",
  "task_id": "${TASK_ID}",
  "subtask_id": "${SUBTASK_ID:-none}",
  "forced": true,
  "verdict": "fail",
  "needs_replan": false,
  "summary": "[forced] 강제 실패"
}
EOJSON
)
            ;;
        reporter:replan)
            FORCED_JSON=$(cat <<EOJSON
{
  "action": "report_complete",
  "task_id": "${TASK_ID}",
  "subtask_id": "${SUBTASK_ID:-none}",
  "forced": true,
  "verdict": "fail",
  "needs_replan": true,
  "summary": "[forced] 강제 replan 요청"
}
EOJSON
)
            ;;
        *)
            echo "[ERROR] 지원하지 않는 force-result 조합: ${AGENT_TYPE}:${FORCE_RESULT}" >&2
            echo "가능한 조합:" >&2
            echo "  planner:approve, coder:approve" >&2
            echo "  reviewer:approve, reviewer:reject" >&2
            echo "  reporter:pass, reporter:fail, reporter:replan" >&2
            exit 1
            ;;
    esac

    echo "$FORCED_JSON" | tee "$LOG_FILE"
    echo ""
    echo "[run_claude_agent] force-result 저장됨: ${LOG_FILE}"
    exit 0
fi

# ─── dry-run 모드: claude 호출 대신 프롬프트 출력 ───
if [[ "$DRY_RUN" == "true" ]]; then
    echo ""
    echo "========== DRY RUN 모드 =========="
    echo "모델: ${MODEL}"
    echo "실행 디렉토리: ${CODEBASE_PATH}"
    echo "로그 경로: ${LOG_FILE}"
    echo "프롬프트 길이: $(echo -n "$PROMPT" | wc -c) bytes"
    echo ""
    echo "────── 조합된 프롬프트 ──────"
    echo "$PROMPT"
    echo "────── 프롬프트 끝 ──────"
    exit 0
fi

# ─── dummy 모드: claude 호출 대신 agent별 더미 JSON 출력 ───
if [[ "$DUMMY" == "true" ]]; then
    echo ""
    echo "========== DUMMY 모드 =========="

    # agent별 더미 응답 생성
    case "$AGENT_TYPE" in
        planner)
            # task_type을 읽어 memory_refresh이면 빈 subtasks를 반환한다
            # (memory_refresh는 Planner가 코드 변경 계획을 세우지 않고 MemoryUpdater에 위임).
            PLANNER_TASK_TYPE=$(python3 -c "
import json
with open('${TASK_FILE}') as f:
    print(json.load(f).get('task_type', 'feature'))
" 2>/dev/null || echo "feature")
            if [[ "$PLANNER_TASK_TYPE" == "memory_refresh" ]]; then
                DUMMY_RESULT=$(cat <<EOJSON
{
  "action": "plan_created",
  "task_id": "${TASK_ID}",
  "strategy_note": "[dummy] memory_refresh — codebase 변경 계획 없음. MemoryUpdater가 full-scan으로 PROJECT_NOTES.md 재생성",
  "subtasks": []
}
EOJSON
)
            else
                DUMMY_RESULT=$(cat <<EOJSON
{
  "action": "plan_created",
  "task_id": "${TASK_ID}",
  "subtasks": [
    {
      "subtask_id": "${TASK_ID}-1",
      "title": "[dummy] 기본 구조 작성",
      "primary_responsibility": "기본 파일 구조와 설정 생성",
      "guidance": "프로젝트 초기 구조를 잡는다"
    },
    {
      "subtask_id": "${TASK_ID}-2",
      "title": "[dummy] 핵심 기능 구현",
      "primary_responsibility": "주요 비즈니스 로직 구현",
      "guidance": "${TASK_ID}-1에서 만든 구조 위에 기능을 추가한다"
    }
  ]
}
EOJSON
)
            fi
            ;;
        coder)
            DUMMY_RESULT=$(cat <<EOJSON
{
  "action": "code_complete",
  "task_id": "${TASK_ID}",
  "subtask_id": "${SUBTASK_ID:-none}",
  "changes_made": [
    {
      "file": "dummy_file.txt",
      "change_type": "created",
      "summary": "[dummy] 더미 파일 생성"
    }
  ]
}
EOJSON
)
            ;;
        reviewer)
            DUMMY_RESULT=$(cat <<EOJSON
{
  "action": "approved",
  "task_id": "${TASK_ID}",
  "subtask_id": "${SUBTASK_ID:-none}",
  "summary": "[dummy] 코드 리뷰 통과. 변경사항이 적절합니다."
}
EOJSON
)
            ;;
        setup)
            DUMMY_RESULT=$(cat <<EOJSON
{
  "action": "setup_complete",
  "task_id": "${TASK_ID}",
  "subtask_id": "${SUBTASK_ID:-none}",
  "service_url": "http://localhost:3000",
  "summary": "[dummy] 환경 구성 및 서비스 기동 완료"
}
EOJSON
)
            ;;
        unit_tester)
            DUMMY_RESULT=$(cat <<EOJSON
{
  "action": "tests_passed",
  "task_id": "${TASK_ID}",
  "subtask_id": "${SUBTASK_ID:-none}",
  "test_results": [
    {"suite": "dummy_suite", "passed": 3, "failed": 0, "skipped": 0}
  ],
  "summary": "[dummy] 단위 테스트 전체 통과"
}
EOJSON
)
            ;;
        e2e_tester)
            DUMMY_RESULT=$(cat <<EOJSON
{
  "action": "e2e_complete",
  "task_id": "${TASK_ID}",
  "subtask_id": "${SUBTASK_ID:-none}",
  "overall_result": "pass",
  "test_results": [
    {"name": "dummy_scenario", "result": "pass", "duration_seconds": 2.1}
  ],
  "summary": "[dummy] E2E 테스트 통과"
}
EOJSON
)
            ;;
        reporter)
            DUMMY_RESULT=$(cat <<EOJSON
{
  "action": "report_complete",
  "task_id": "${TASK_ID}",
  "subtask_id": "${SUBTASK_ID:-none}",
  "verdict": "pass",
  "needs_replan": false,
  "summary": "[dummy] 모든 단계 정상 완료. 커밋 가능."
}
EOJSON
)
            ;;
        memory_updater)
            DUMMY_RESULT=$(cat <<EOJSON
{
  "action": "memory_update_complete",
  "updated": false,
  "sections_changed": [],
  "rationale": "[dummy] 더미 모드에서는 장기 메모리를 수정하지 않습니다."
}
EOJSON
)
            ;;
        summarizer)
            DUMMY_RESULT=$(cat <<EOJSON
{
  "action": "summary_complete",
  "pr_title": "[dummy] Add feature for task ${TASK_ID}",
  "pr_body": "## Summary\n- [dummy] Automated changes for task ${TASK_ID}\n\n## Changes\n- Dummy file modifications\n\n## Test Plan\n- Manual verification",
  "task_summary": "[dummy] task ${TASK_ID}의 작업이 완료되었습니다."
}
EOJSON
)
            ;;
    esac

    echo "$DUMMY_RESULT" | tee "$LOG_FILE"
    echo ""
    echo "[run_claude_agent] dummy 결과 저장됨: ${LOG_FILE}"
    exit 0
fi

# ═══════════════════════════════════════════════════════════
# e2e_tester 전용: Playwright + MCP Docker 컨테이너 준비
# ═══════════════════════════════════════════════════════════
# docs/e2e-test-design-decision.md §5.3/§5.8 구현.
# e2e_tester 호출 전에 컨테이너를 기동하고 임시 .mcp.json을 만들어
# Claude 세션에 MCP를 주입한다. trap으로 컨테이너 정리 보장.
#
# 여기서 설정되는 변수들(후속 블록에서 사용):
#   E2E_CONTAINER_NAME       컨테이너 이름
#   E2E_MCP_CONFIG_FILE      임시 .mcp.json 경로
#   E2E_HOST_PORT            MCP SSE 호스트 포트
#   E2E_ARTIFACTS_DIR        호스트 스크린샷/비디오/trace 경로
#   E2E_TESTS_DIR            호스트 .spec.ts 경로 (볼륨 마운트)
#   E2E_PROMPT_EXTRA         프롬프트에 덧붙일 "## E2E 실행 설정" 섹션

E2E_CONTAINER_NAME=""
E2E_MCP_CONFIG_FILE=""
E2E_HOST_PORT=""
E2E_ARTIFACTS_DIR=""
E2E_TESTS_DIR=""
E2E_PROMPT_EXTRA=""
E2E_MCP_LOG_RETENTION="on-failure"

if [[ "$AGENT_TYPE" == "e2e_tester" && "$DRY_RUN" != "true" && "$DUMMY" != "true" && -n "${SUBTASK_FILE:-}" ]]; then
    # ─── config.yaml / project.yaml 에서 tester 설정 읽기 ───
    E2E_IMAGE=$(read_yaml_value "$CONFIG_FILE" "machines.tester.docker.image")
    [[ -z "$E2E_IMAGE" ]] && E2E_IMAGE="agent-hub-e2e-playwright"
    E2E_AUTO_BUILD=$(read_yaml_value "$CONFIG_FILE" "machines.tester.docker.auto_build")
    [[ -z "$E2E_AUTO_BUILD" ]] && E2E_AUTO_BUILD="true"
    E2E_NETWORK=$(read_yaml_value "$CONFIG_FILE" "machines.tester.docker.network")
    [[ -z "$E2E_NETWORK" ]] && E2E_NETWORK="host"
    E2E_HEALTHCHECK_TIMEOUT=$(read_yaml_value "$CONFIG_FILE" "machines.tester.docker.healthcheck_timeout_seconds")
    [[ -z "$E2E_HEALTHCHECK_TIMEOUT" ]] && E2E_HEALTHCHECK_TIMEOUT="30"
    E2E_MCP_ISOLATED=$(read_yaml_value "$CONFIG_FILE" "machines.tester.mcp.isolated")
    [[ -z "$E2E_MCP_ISOLATED" ]] && E2E_MCP_ISOLATED="true"
    E2E_MCP_INTERNAL_PORT=$(read_yaml_value "$CONFIG_FILE" "machines.tester.mcp.internal_port")
    [[ -z "$E2E_MCP_INTERNAL_PORT" ]] && E2E_MCP_INTERNAL_PORT="8931"
    E2E_MCP_LOG_RETENTION=$(read_yaml_value "$CONFIG_FILE" "machines.tester.mcp.log_retention")
    [[ -z "$E2E_MCP_LOG_RETENTION" ]] && E2E_MCP_LOG_RETENTION="on-failure"

    E2E_BROWSER=$(read_yaml_value "$CONFIG_FILE" "machines.tester.browser")
    [[ -z "$E2E_BROWSER" ]] && E2E_BROWSER="chromium"
    E2E_VIEWPORT_W=$(read_yaml_value "$CONFIG_FILE" "machines.tester.viewport.width")
    [[ -z "$E2E_VIEWPORT_W" ]] && E2E_VIEWPORT_W="1280"
    E2E_VIEWPORT_H=$(read_yaml_value "$CONFIG_FILE" "machines.tester.viewport.height")
    [[ -z "$E2E_VIEWPORT_H" ]] && E2E_VIEWPORT_H="720"

    # artifacts 설정: screenshots / video / trace
    E2E_SCREENSHOTS=$(read_yaml_value "$CONFIG_FILE" "machines.tester.artifacts.screenshots")
    [[ -z "$E2E_SCREENSHOTS" ]] && E2E_SCREENSHOTS="only-on-failure"
    E2E_VIDEO=$(read_yaml_value "$CONFIG_FILE" "machines.tester.artifacts.video")
    [[ -z "$E2E_VIDEO" ]] && E2E_VIDEO="off"
    E2E_TRACE=$(read_yaml_value "$CONFIG_FILE" "machines.tester.artifacts.trace")
    [[ -z "$E2E_TRACE" ]] && E2E_TRACE="retain-on-failure"

    # retry_count: project.yaml override가 우선
    E2E_RETRY_COUNT=$(read_yaml_value "$PROJECT_YAML" "testing.e2e_test.retry_count")
    if [[ -z "$E2E_RETRY_COUNT" ]]; then
        E2E_RETRY_COUNT=$(read_yaml_value "$CONFIG_FILE" "machines.tester.retry_count")
    fi
    [[ -z "$E2E_RETRY_COUNT" ]] && E2E_RETRY_COUNT="0"

    # test_source / base_url / static_test_dir
    E2E_TEST_SOURCE=$(read_yaml_value "$PROJECT_YAML" "testing.e2e_test.test_source")
    [[ -z "$E2E_TEST_SOURCE" ]] && E2E_TEST_SOURCE="dynamic"
    E2E_MODE=$(read_yaml_value "$PROJECT_YAML" "testing.e2e_test.mode")
    [[ -z "$E2E_MODE" ]] && E2E_MODE="browser"
    E2E_BASE_URL=$(read_yaml_value "$PROJECT_YAML" "testing.e2e_test.base_url")
    E2E_STATIC_TEST_DIR=$(read_yaml_value "$PROJECT_YAML" "testing.e2e_test.static_test_dir")

    # base_url 자동 추론 (§4.6-2)
    if [[ -z "$E2E_BASE_URL" ]]; then
        _SERVICE_PORT=$(read_yaml_value "$PROJECT_YAML" "codebase.service_port")
        if [[ -n "$_SERVICE_PORT" ]]; then
            E2E_BASE_URL="http://localhost:${_SERVICE_PORT}"
        fi
    fi

    # ─── artifacts / tests 디렉토리 준비 ───
    _PROJECT_NAME="$(basename "$PROJECT_DIR")"
    _SUB_SEQ="${SUBTASK_SEQ:-00}"
    E2E_CONTAINER_NAME="e2e-${_PROJECT_NAME}-${TASK_ID}-${_SUB_SEQ}"
    E2E_ARTIFACTS_DIR="${LOG_DIR}/e2e-artifacts/${SUBTASK_ID:-unknown}"
    E2E_TESTS_DIR="${CODEBASE_PATH}/e2e-tests/${SUBTASK_ID:-unknown}"
    mkdir -p "$E2E_ARTIFACTS_DIR" "$E2E_TESTS_DIR"

    # ─── static/both 모드: 기존 테스트 파일을 tests_dir에 복사 ───
    # 컨테이너는 E2E_TESTS_DIR만 /e2e/tests/에 마운트하므로,
    # static_test_dir의 .spec.ts를 여기에 복사해야 Playwright가 찾을 수 있다.
    E2E_STATIC_FILE_LIST=""
    if [[ "$E2E_TEST_SOURCE" == "static" || "$E2E_TEST_SOURCE" == "both" ]]; then
        if [[ -n "$E2E_STATIC_TEST_DIR" ]]; then
            _STATIC_ABS="${CODEBASE_PATH}/${E2E_STATIC_TEST_DIR}"
            if [[ -d "$_STATIC_ABS" ]]; then
                echo "[run_claude_agent] static 테스트 복사: $_STATIC_ABS → $E2E_TESTS_DIR"
                cp -r "$_STATIC_ABS"/*.spec.ts "$E2E_TESTS_DIR/" 2>/dev/null || true
                cp -r "$_STATIC_ABS"/*.spec.js "$E2E_TESTS_DIR/" 2>/dev/null || true
                # both 모드 AND 판정용: static 테스트 파일 목록 수집
                E2E_STATIC_FILE_LIST=$(cd "$E2E_TESTS_DIR" && ls *.spec.ts *.spec.js 2>/dev/null | tr '\n' ',' | sed 's/,$//')
                echo "[run_claude_agent] static 파일 목록: $E2E_STATIC_FILE_LIST"
            else
                echo "[run_claude_agent] 경고: static_test_dir 존재하지 않음: $_STATIC_ABS" >&2
            fi
        else
            echo "[run_claude_agent] 경고: test_source=$E2E_TEST_SOURCE이지만 static_test_dir이 비어있음" >&2
        fi
    fi

    # ─── 컨테이너 기동 (호스트 포트 획득) ───
    echo "[run_claude_agent] E2E 컨테이너 기동 중: $E2E_CONTAINER_NAME"
    E2E_HOST_PORT=$(
        E2E_IMAGE="$E2E_IMAGE" \
        E2E_AUTO_BUILD="$E2E_AUTO_BUILD" \
        E2E_NETWORK="$E2E_NETWORK" \
        E2E_HEALTHCHECK_TIMEOUT="$E2E_HEALTHCHECK_TIMEOUT" \
        E2E_MCP_ISOLATED="$E2E_MCP_ISOLATED" \
        E2E_MCP_INTERNAL_PORT="$E2E_MCP_INTERNAL_PORT" \
        "${SCRIPT_DIR}/e2e_container_runner.sh" start \
            "$E2E_CONTAINER_NAME" "$E2E_TESTS_DIR" "$E2E_ARTIFACTS_DIR"
    )
    if [[ -z "$E2E_HOST_PORT" ]]; then
        echo "[run_claude_agent] E2E 컨테이너 기동 실패" >&2
        exit 1
    fi
    echo "[run_claude_agent] E2E 컨테이너 ready (host_port=$E2E_HOST_PORT)"

    # ─── 임시 .mcp.json 생성 ───
    E2E_MCP_CONFIG_FILE="/tmp/mcp-${TASK_ID}-${_SUB_SEQ}.$$.json"
    cat > "$E2E_MCP_CONFIG_FILE" <<EOMCP
{
  "mcpServers": {
    "playwright": {
      "type": "sse",
      "url": "http://localhost:${E2E_HOST_PORT}/sse"
    }
  }
}
EOMCP
    echo "[run_claude_agent] MCP config 생성: $E2E_MCP_CONFIG_FILE"

    # ─── 프롬프트에 주입할 E2E 실행 설정 ───
    E2E_TEST_ACCOUNTS_JSON=$(python3 -c "
import yaml, json
with open('${PROJECT_YAML}') as f:
    cfg = yaml.safe_load(f) or {}
accounts = cfg.get('testing', {}).get('e2e_test', {}).get('test_accounts', []) or []
print(json.dumps(accounts, ensure_ascii=False))
")

    E2E_PROMPT_EXTRA="

---
## E2E 실행 설정
- AGENT_HUB_ROOT: ${AGENT_HUB_ROOT}
- mode: ${E2E_MODE}
- test_source: ${E2E_TEST_SOURCE}
- browser: ${E2E_BROWSER}
- viewport_w: ${E2E_VIEWPORT_W}
- viewport_h: ${E2E_VIEWPORT_H}
- base_url: ${E2E_BASE_URL:-(unset)}
- static_test_dir: ${E2E_STATIC_TEST_DIR:-(unset)}
- static_files: ${E2E_STATIC_FILE_LIST:-(none)}
- tests_dir (호스트, 볼륨 마운트됨 → 컨테이너의 /e2e/tests): ${E2E_TESTS_DIR}
- artifacts_dir (호스트, 볼륨 마운트됨 → 컨테이너의 /e2e/test-results): ${E2E_ARTIFACTS_DIR}
- container: ${E2E_CONTAINER_NAME}
- mcp_sse_url: http://localhost:${E2E_HOST_PORT}/sse
- retry_count: ${E2E_RETRY_COUNT}
- test_accounts: ${E2E_TEST_ACCOUNTS_JSON}

Phase 3 검증 명령(그대로 Bash tool에서 실행 가능):
\`\`\`bash
${AGENT_HUB_ROOT}/scripts/e2e_container_runner.sh exec-test ${E2E_CONTAINER_NAME} \\
  --browser ${E2E_BROWSER} \\
  --base-url '${E2E_BASE_URL}' \\
  --retries ${E2E_RETRY_COUNT} \\
  --viewport-w ${E2E_VIEWPORT_W} \\
  --viewport-h ${E2E_VIEWPORT_H} \\
  --screenshots ${E2E_SCREENSHOTS} \\
  --video ${E2E_VIDEO} \\
  --trace ${E2E_TRACE}
\`\`\`

실행 후 \`${E2E_ARTIFACTS_DIR}/report.json\`을 Read로 읽어 집계하세요.
"
    # PROMPT에 E2E 실행 설정 append
    PROMPT="${PROMPT}${E2E_PROMPT_EXTRA}"
fi

# ─── PID 관리 디렉토리 ───
PID_DIR="${AGENT_HUB_ROOT}/.pids"
mkdir -p "$PID_DIR"

# ─── Claude 세션 재사용 결정 ───
# (task_id, agent_type) 단위로 UUID를 발급해 같은 task 안 같은 agent의
# 모든 subtask/attempt가 하나의 세션을 공유한다. retry/다음 subtask에서
# "이전에 왜 이렇게 바꿨는가"라는 의도가 자연스럽게 전달된다.
#
# config.yaml의 claude.session_reuse가 false이면 비활성화. 기본값 true.
SESSION_REUSE=$(read_yaml_value "$CONFIG_FILE" "claude.session_reuse")
if [[ -z "$SESSION_REUSE" ]]; then
    SESSION_REUSE="true"
fi

SESSION_ID=""
SESSION_MODE=""
if [[ "$SESSION_REUSE" == "true" ]]; then
    SESSION_OUTPUT=$(python3 "${SCRIPT_DIR}/allocate_session_id.py" \
        "$TASK_FILE" "$AGENT_TYPE" || true)
    if [[ -n "$SESSION_OUTPUT" ]]; then
        SESSION_ID=$(echo "$SESSION_OUTPUT" | awk '{print $1}')
        SESSION_MODE=$(echo "$SESSION_OUTPUT" | awk '{print $2}')
        echo "[run_claude_agent] session: id=${SESSION_ID} mode=${SESSION_MODE}"
    else
        echo "[run_claude_agent] session_id 발급 실패 — cold start로 진행" >&2
    fi
fi

# resume 세션에는 "이전 맥락은 참고용, 이번 지시가 최우선" 가드를 프롬프트 맨 앞에 붙인다.
# coder가 subtask2에서 subtask1 때의 의도를 기억하되 그것에 고착되지 않도록 한다.
if [[ "$SESSION_MODE" == "resume" ]]; then
    PROMPT="## 세션 재개 안내
이 세션에는 같은 task의 이전 호출(다른 subtask 또는 이전 attempt) 맥락이 남아 있을 수 있습니다.
그 맥락은 '왜 그렇게 바꿨는가'를 이해하는 배경 자료이며, 이번 턴의 우선순위는 다음과 같습니다:

1. 아래 Task/Subtask/Plan Context와 명시적 지시사항이 최우선
2. 이전 세션에서 내린 결정이 이번 subtask의 요구사항과 충돌하면, 이번 요구사항을 따른다
3. 이전 판단을 재사용하기 전, 이번 context에서 여전히 유효한지 검증

---
${PROMPT}"
fi

# ─── Claude Code CLI 실행 ───
cd "$CODEBASE_PATH"

# claude 인자 조립 (세션 재사용 여부에 따라 --session-id / --resume 분기)
CLAUDE_ARGS=(--model "$MODEL" -p "${PROMPT}" --output-format json --dangerously-skip-permissions)
if [[ -n "$SESSION_ID" ]]; then
    if [[ "$SESSION_MODE" == "resume" ]]; then
        CLAUDE_ARGS+=(--resume "$SESSION_ID")
    else
        CLAUDE_ARGS+=(--session-id "$SESSION_ID")
    fi
fi

# e2e_tester: MCP 주입
if [[ -n "$E2E_MCP_CONFIG_FILE" ]]; then
    CLAUDE_ARGS+=(--mcp-config "$E2E_MCP_CONFIG_FILE")
fi

# claude를 백그라운드로 실행하고 PID를 기록한다
# 종료 시 PID 파일을 자동 삭제하기 위해 trap을 설정한다
claude "${CLAUDE_ARGS[@]}" 2>&1 | tee "$LOG_FILE" &
CLAUDE_PID=$!

# PID 파일 생성 (agent 정보 포함)
# 파일명 규칙: {project}_{task}_{subtask}_{agent}_attempt-{N}.{PID}.pid
# subtask가 없는 경우 (planner, summarizer): {project}_{task}_{agent}.{PID}.pid
_PID_PROJECT="$(basename "$PROJECT_DIR")"
if [[ -n "${SUBTASK_ID:-}" ]]; then
    _PID_SUBTASK_SEQ=$(echo "$SUBTASK_ID" | sed 's/.*-//')
    _PID_SUBTASK_SEQ=$(printf "%02d" "$_PID_SUBTASK_SEQ")
    PID_FILENAME="${_PID_PROJECT}_${TASK_ID}_${_PID_SUBTASK_SEQ}_${AGENT_TYPE}_attempt-${ATTEMPT:-1}.${CLAUDE_PID}.pid"
else
    PID_FILENAME="${_PID_PROJECT}_${TASK_ID}_${AGENT_TYPE}.${CLAUDE_PID}.pid"
fi
PID_FILE="${PID_DIR}/${PID_FILENAME}"
cat > "$PID_FILE" <<EOPID
{
  "pid": ${CLAUDE_PID},
  "agent_type": "${AGENT_TYPE}",
  "task_id": "${TASK_ID}",
  "subtask_id": "${SUBTASK_ID:-none}",
  "project_dir": "${PROJECT_DIR}",
  "started_at": "$(date -Iseconds)",
  "log_file": "${LOG_FILE}"
}
EOPID

echo "[run_claude_agent] PID ${CLAUDE_PID} 기록됨: ${PID_FILE}"

# 프로세스 종료 시 PID 파일 정리 + e2e 컨테이너/MCP config 정리 + MCP 로그 보존
cleanup_pid() {
    rm -f "$PID_FILE"
    echo "[run_claude_agent] PID 파일 정리됨: ${PID_FILE}"

    # e2e_tester 정리 (E2E_CONTAINER_NAME이 세팅된 경우에만)
    if [[ -n "${E2E_CONTAINER_NAME:-}" ]]; then
        # MCP 로그 보존 정책 판정
        local should_save_mcp="false"
        case "${E2E_MCP_LOG_RETENTION:-on-failure}" in
            always)
                should_save_mcp="true"
                ;;
            on-failure)
                # report.json의 stats.unexpected (failed 개수)로 판정. 파일 없으면 저장(비정상 종료)
                if [[ -f "${E2E_ARTIFACTS_DIR}/report.json" ]]; then
                    local _failed
                    _failed=$(python3 -c "
import json
try:
    d = json.load(open('${E2E_ARTIFACTS_DIR}/report.json'))
    print(d.get('stats', {}).get('unexpected', 1))
except Exception:
    print(1)
" 2>/dev/null || echo "1")
                    [[ "$_failed" != "0" ]] && should_save_mcp="true"
                else
                    should_save_mcp="true"
                fi
                ;;
            never)
                should_save_mcp="false"
                ;;
        esac

        if [[ "$should_save_mcp" == "true" && -d "${E2E_ARTIFACTS_DIR:-}" ]]; then
            docker logs "$E2E_CONTAINER_NAME" > "${E2E_ARTIFACTS_DIR}/mcp-session.log" 2>&1 || true
            echo "[run_claude_agent] MCP 세션 로그 보존: ${E2E_ARTIFACTS_DIR}/mcp-session.log"
        fi

        "${SCRIPT_DIR}/e2e_container_runner.sh" stop \
            "$E2E_CONTAINER_NAME" \
            --mcp-config "${E2E_MCP_CONFIG_FILE:-}" || true
    fi
}
trap cleanup_pid EXIT

# claude 프로세스가 끝날 때까지 대기
wait $CLAUDE_PID
