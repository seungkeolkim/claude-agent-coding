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
VALID_AGENTS="planner coder reviewer setup unit_tester e2e_tester reporter"

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
        planner)      echo "01" ;;
        coder)        echo "02" ;;
        reviewer)     echo "03" ;;
        setup)        echo "04" ;;
        unit_tester)  echo "05" ;;
        e2e_tester)   echo "06" ;;
        reporter)     echo "07" ;;
        *)            echo "99" ;;
    esac
}

# agent 이름을 로그용 표기로 변환 (snake_case → kebab-case)
get_step_name() {
    case "$1" in
        planner)      echo "planner" ;;
        coder)        echo "coder" ;;
        reviewer)     echo "reviewer" ;;
        setup)        echo "setup" ;;
        unit_tester)  echo "unit-tester" ;;
        e2e_tester)   echo "e2e-tester" ;;
        reporter)     echo "reporter" ;;
        *)            echo "$1" ;;
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

# ─── 프롬프트 조합 ───
PROMPT="$(cat "$PROMPT_FILE")"

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
  "feedback": "[forced] 강제 거절 — 루프백 테스트",
  "issues": ["강제 거절 트리거"]
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
    esac

    echo "$DUMMY_RESULT" | tee "$LOG_FILE"
    echo ""
    echo "[run_claude_agent] dummy 결과 저장됨: ${LOG_FILE}"
    exit 0
fi

# ─── PID 관리 디렉토리 ───
PID_DIR="${AGENT_HUB_ROOT}/.pids"
mkdir -p "$PID_DIR"

# ─── Claude Code CLI 실행 ───
cd "$CODEBASE_PATH"

# claude를 백그라운드로 실행하고 PID를 기록한다
# 종료 시 PID 파일을 자동 삭제하기 위해 trap을 설정한다
claude --model "$MODEL" -p "${PROMPT}" --output-format json --dangerously-skip-permissions 2>&1 | tee "$LOG_FILE" &
CLAUDE_PID=$!

# PID 파일 생성 (agent 정보 포함)
PID_FILE="${PID_DIR}/${CLAUDE_PID}.pid"
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

# 프로세스 종료 시 PID 파일 정리
cleanup_pid() {
    rm -f "$PID_FILE"
    echo "[run_claude_agent] PID 파일 정리됨: ${PID_FILE}"
}
trap cleanup_pid EXIT

# claude 프로세스가 끝날 때까지 대기
wait $CLAUDE_PID
