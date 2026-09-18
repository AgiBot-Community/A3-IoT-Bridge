#!/usr/bin/env bash
set -eo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SDK_DIR="/agibot/data/home/agi/Desktop/AimDK-A3-V3.2-0815/agibot_a3_aimdk"

cd "$PROJECT_DIR"
source .venv/bin/activate
source /agibot/software/v0/entry/env/env.sh
source "$SDK_DIR/prebuilt/ros2_plugin_proto_aarch64/share/ros2_plugin_proto/local_setup.bash"
source "$SDK_DIR/prebuilt/audio_msgs_proto_aarch64/share/audio_msgs/local_setup.bash"
source .env

exec python -m a3_bridge.offline_voice_app "$@"
