#!/bin/bash
# Agent Hub 가상환경 활성화 스크립트
# 사용법: source activate.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

if [ ! -d "$VENV_DIR" ]; then
    echo "[activate] .venv가 없습니다. 생성합니다..."
    python3 -m venv "$VENV_DIR"
    source "$VENV_DIR/bin/activate"
    echo "[activate] pip install -r requirements.txt 실행..."
    pip install -r "$SCRIPT_DIR/requirements.txt"
else
    source "$VENV_DIR/bin/activate"
fi

echo "[activate] 가상환경 활성화 완료: $VENV_DIR"
