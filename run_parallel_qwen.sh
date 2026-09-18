#!/usr/bin/env bash
set -eo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SDK_DIR="/agibot/data/home/agi/Desktop/AimDK-A3-V3.2-0815/agibot_a3_aimdk"
QWEN_KEY_FILE="${QWEN_KEY_FILE:-/agibot/data/home/agi/.secrets/dashscope_api_key}"

cd "$PROJECT_DIR"
source .venv/bin/activate
source /agibot/software/v0/entry/env/env.sh
source "$SDK_DIR/prebuilt/ros2_plugin_proto_aarch64/share/ros2_plugin_proto/local_setup.bash"
source .env

export DASHSCOPE_API_KEY="$(tr -d '\r\n' < "$QWEN_KEY_FILE")"
export QWEN_MODEL="${QWEN_MODEL:-qwen3.5-omni-flash}"
export QWEN_BASE_URL="${QWEN_BASE_URL:-https://dashscope.aliyuncs.com/compatible-mode/v1}"

exec python -m a3_bridge.parallel_qwen_app "$@"
