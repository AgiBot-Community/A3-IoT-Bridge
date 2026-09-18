import argparse
import json
import logging

from .bridge import HomeBridge
from .config import load_config


def main() -> None:
    parser = argparse.ArgumentParser(description="智元 A3—小鸿 SE 智能家居桥接程序")
    parser.add_argument("--config", default="config.json", help="JSON 配置文件")
    parser.add_argument("--once", help="只处理一句测试文本后退出")
    parser.add_argument("--health", action="store_true", help="只执行健康检查")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    bridge = HomeBridge(load_config(args.config))

    if args.health:
        print(json.dumps(bridge.health(), ensure_ascii=False, indent=2))
        return
    if args.once:
        print(bridge.handle_text(args.once).text)
        return

    print("输入自然语言指令进行测试，输入 exit 退出。")
    while True:
        try:
            text = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if text.lower() in ("exit", "quit", "退出"):
            return
        if text:
            print(bridge.handle_text(text).text)


if __name__ == "__main__":
    main()
