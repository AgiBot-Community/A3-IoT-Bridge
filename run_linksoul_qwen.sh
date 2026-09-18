#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

set -a
source ./.env
set +a

QWEN_KEY_FILE="${QWEN_KEY_FILE:-/agibot/data/home/agi/.secrets/dashscope_api_key}"
if [[ -z "${DASHSCOPE_API_KEY:-}" ]]; then
  if [[ ! -r "$QWEN_KEY_FILE" ]]; then
    echo "Set DASHSCOPE_API_KEY or provide a readable QWEN_KEY_FILE: $QWEN_KEY_FILE" >&2
    exit 2
  fi
  export DASHSCOPE_API_KEY
  DASHSCOPE_API_KEY="$(tr -d '\r\n' < "$QWEN_KEY_FILE")"
fi
export QWEN_MODEL="${QWEN_MODEL:-qwen3.5-omni-flash}"
export QWEN_BASE_URL="${QWEN_BASE_URL:-https://dashscope.aliyuncs.com/compatible-mode/v1}"

exec .venv/bin/python -m a3_bridge.linksoul_app --config config.json
