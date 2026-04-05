#!/bin/bash
# Agent Hub 가상환경 활성화 스크립트
# 사용법: source activate.sh

# 서브쉘(./activate_venv.sh)로 실행하면 venv가 현재 쉘에 적용되지 않음
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    echo "[activate] ERROR: source로 실행해주세요: source activate_venv.sh" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

if [ ! -d "$VENV_DIR" ]; then
    echo "[activate] .venv가 없습니다. 생성합니다..."
    if ! python3 -m venv "$VENV_DIR"; then
        echo "[activate] ERROR: venv 생성 실패" >&2
        return 1 2>/dev/null || exit 1
    fi
    source "$VENV_DIR/bin/activate"
    echo "[activate] pip install -r requirements.txt 실행..."
    pip install -r "$SCRIPT_DIR/requirements.txt"
else
    source "$VENV_DIR/bin/activate"
fi

echo "[activate] 가상환경 활성화 완료: $VENV_DIR"
