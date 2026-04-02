#!/usr/bin/env bash
set -euo pipefail

# WFC 산출물 정리 (메인 task JSON은 보존)
rm -rf projects/test-project/tasks/00001/
rm -rf projects/test-project/logs/00001/

# task JSON 초기화
cp 00001-update-readme.json.backup projects/test-project/tasks/00001-update-readme.json

# 파이프라인 실행
./run_agent.sh pipeline --project test-project --task 00001 --dummy
