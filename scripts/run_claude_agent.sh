#!/usr/bin/env bash
# run_claude_agent.sh — Claude Code 세션 기동 래퍼
#
# 사용법:
#   ./scripts/run_claude_agent.sh <agent_type> <task_id> [subtask_id]
#
# agent_type: planner | coder | reviewer | setup | unit_tester | e2e_tester | reporter
# 각 agent는 config/agent_prompts/{agent_type}.md 프롬프트를 사용한다.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

AGENT_TYPE="${1:?agent_type을 지정하세요 (planner|coder|reviewer|setup|unit_tester|e2e_tester|reporter)}"
TASK_ID="${2:?task_id를 지정하세요}"
SUBTASK_ID="${3:-}"

# 설정 로드
CONFIG_FILE="${PROJECT_ROOT}/config.yaml"
PROMPT_FILE="${PROJECT_ROOT}/config/agent_prompts/${AGENT_TYPE}.md"

if [[ ! -f "$PROMPT_FILE" ]]; then
    echo "[ERROR] 프롬프트 파일 없음: ${PROMPT_FILE}" >&2
    exit 1
fi

# agent별 모델 결정
get_model_for_agent() {
    local agent="$1"
    case "$agent" in
        planner)    echo "opus" ;;
        coder)      echo "sonnet" ;;
        reviewer)   echo "opus" ;;
        e2e_tester) echo "sonnet" ;;
        *)          echo "sonnet" ;;
    esac
}

MODEL=$(get_model_for_agent "$AGENT_TYPE")

# config.yaml에서 경로 읽기 (python으로 yaml 파싱)
read_config_value() {
    python3 -c "
import yaml, sys
with open('${CONFIG_FILE}') as f:
    config = yaml.safe_load(f)
keys = '$1'.split('.')
val = config
for k in keys:
    val = val[k]
print(val)
"
}

# 대상 프로젝트 경로 (agent가 실제 코드를 작성/수정하는 곳)
CODEBASE_PATH=$(read_config_value "executor.codebase_path")

# runtime 데이터 경로 (tasks, logs, handoffs)
WORKSPACE_DIR=$(read_config_value "executor.workspace_dir")

if [[ ! -d "$CODEBASE_PATH" ]]; then
    echo "[ERROR] 대상 프로젝트 경로가 존재하지 않음: ${CODEBASE_PATH}" >&2
    echo "[ERROR] config.yaml의 executor.codebase_path를 확인하세요." >&2
    exit 1
fi

# 로그 디렉토리 생성
LOG_DIR="${WORKSPACE_DIR}/logs/${TASK_ID}"
mkdir -p "$LOG_DIR"

# 로그 파일명 결정
if [[ -n "$SUBTASK_ID" ]]; then
    LOG_FILE="${LOG_DIR}/${AGENT_TYPE}_${SUBTASK_ID}.log"
else
    LOG_FILE="${LOG_DIR}/${AGENT_TYPE}.log"
fi

# 프롬프트 조합
PROMPT="$(cat "$PROMPT_FILE")"

# task 컨텍스트 추가
TASK_FILE="${WORKSPACE_DIR}/tasks/${TASK_ID}.json"
if [[ -f "$TASK_FILE" ]]; then
    PROMPT="${PROMPT}

---
## Task Context
\`\`\`json
$(cat "$TASK_FILE")
\`\`\`"
fi

# subtask 컨텍스트 추가
if [[ -n "$SUBTASK_ID" ]]; then
    SUBTASK_FILE="${WORKSPACE_DIR}/tasks/${SUBTASK_ID}.json"
    if [[ -f "$SUBTASK_FILE" ]]; then
        PROMPT="${PROMPT}

## Subtask Context
\`\`\`json
$(cat "$SUBTASK_FILE")
\`\`\`"
    fi
fi

echo "[run_claude_agent] agent=${AGENT_TYPE} task=${TASK_ID} subtask=${SUBTASK_ID:-none} model=${MODEL}"
echo "[run_claude_agent] 로그: ${LOG_FILE}"

# Claude Code CLI 실행
# 대상 프로젝트 디렉토리에서 claude를 실행해야 해당 코드베이스를 인식한다
echo "[run_claude_agent] 대상 프로젝트: ${CODEBASE_PATH}"

cd "$CODEBASE_PATH"
claude -p "${PROMPT}" --model "${MODEL}" 2>&1 | tee "$LOG_FILE"
