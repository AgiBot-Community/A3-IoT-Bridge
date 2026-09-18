"""Short Xiaohong SE dance-lighting test; never calls an AimDK motion API."""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from a3_bridge.config import load_config
from a3_bridge.xiaohong_client import XiaohongSEClient


def main() -> None:
    parser = argparse.ArgumentParser(description="只测试小鸿 SE 跳舞灯效，不启动机器人动作")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--run-seconds", type=float, default=2.0)
    args = parser.parse_args()

    config = load_config(args.config)
    client = XiaohongSEClient(config.gateway)
    health = client.health()
    print(
        json.dumps(
            {
                "stage": "health",
                "status": health.get("status"),
                "firmware_version": health.get("firmware_version"),
                "connected_module_count": health.get("connected_module_count"),
                "dance_lighting_active": health.get("dance_lighting_active"),
                "endpoint": client.current_base_url,
            },
            ensure_ascii=False,
        )
    )

    started = client.control_scene(
        "dance_lighting",
        request_id=client.new_request_id("a3-light-only"),
        params={"duration_sec": 15, "interval_ms": 600},
    )
    print(json.dumps({"stage": "start", "result": started}, ensure_ascii=False))
    try:
        time.sleep(max(0.2, min(args.run_seconds, 5.0)))
    finally:
        stopped = client.control_scene(
            "dance_lighting_stop",
            request_id=client.new_request_id("a3-light-only-stop"),
        )
        print(json.dumps({"stage": "stop", "result": stopped}, ensure_ascii=False))

    status = client.get_dance_lighting_status()
    print(json.dumps({"stage": "status", "result": status}, ensure_ascii=False))


if __name__ == "__main__":
    main()
