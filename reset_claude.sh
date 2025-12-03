#!/bin/bash
# 用于在 pytradekit 中执行，把 .claude 软链到另外两个项目

set -e

BASE_DIR=$(cd "$(dirname "$0")"; pwd)

TARGETS=(
  "../new_monitor"
  "../market_making"
)

for target in "${TARGETS[@]}"; do
  TARGET_PATH="$BASE_DIR/$target/.claude"
  rm -rf "$TARGET_PATH"
  ln -sfn "$BASE_DIR/.claude" "$TARGET_PATH"
  echo "已更新 .claude -> $TARGET_PATH"
done

echo "reset_claude.sh 完成"

