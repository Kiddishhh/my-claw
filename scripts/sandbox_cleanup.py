"""回收入口：默认执行一次，--watch 周期执行；可交给系统任务计划程序。"""
import argparse
from pathlib import Path
import time

from agent.main import PROJECT_ROOT
from infra.logging import get_logger
from infra.settings import SandboxSettings
from sandbox.manager import cleanup_idle_sandboxes, create_daytona_client


def main() -> None:
    """独立回收不要求模型 API Key，也不调用模型。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    args = parser.parse_args()
    settings = SandboxSettings()
    client = create_daytona_client(settings)
    while True:
        try:
            result = cleanup_idle_sandboxes(args.project_root, settings=settings, client=client)
            print(result, flush=True)
            if not args.watch:
                raise SystemExit(1 if result["failed"] else 0)
        except Exception as error:
            get_logger().error("回收失败：{}", type(error).__name__)
            if not args.watch:
                raise SystemExit(1) from None
        time.sleep(settings.syc_sandbox_cleanup_seconds)


if __name__ == "__main__":
    main()
