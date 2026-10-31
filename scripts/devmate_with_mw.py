"""默认远程入口：--thread-id issue-001 --task '任务'；同会话复用远程文件。"""
import argparse
import asyncio
from pathlib import Path
import threading

from agent.main import PROJECT_ROOT, build_model
from agent.dispatcher import run_task
from infra.logging import get_logger
from infra.settings import SandboxSettings, load_langsmith_environment
from sandbox.manager import cleanup_idle_sandboxes, create_daytona_client, sandbox_for_thread

logger = get_logger()


def main() -> None:
    """先准备沙箱，再构建 Agent；远程失败不自动切回本机。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--thread-id", required=True)
    parser.add_argument("--user-id", default="local-user")
    parser.add_argument("--task", required=True)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    args = parser.parse_args()
    load_langsmith_environment()
    settings = SandboxSettings()
    model = build_model()
    client = create_daytona_client(settings)

    # 程序存活期间扫描旧会话；本次任务持锁，回收器会跳过。
    # 退出后由平台回收兜底。持续本地扫描需另运行 sandbox_cleanup --watch。
    stop = threading.Event()

    def sweep() -> None:
        while not stop.is_set():
            try:
                counts = cleanup_idle_sandboxes(args.project_root, settings=settings, client=client)
                if counts["deleted"] or counts["failed"]:
                    logger.info("沙箱回收摘要：{}", counts)
            except Exception as error:
                logger.error("回收扫描失败：{}", type(error).__name__)
            stop.wait(settings.syc_sandbox_cleanup_seconds)

    worker = threading.Thread(target=sweep, name="sandbox-cleanup", daemon=True)
    worker.start()
    try:
        result = asyncio.run(run_task(
            args.thread_id, args.task, user_id=args.user_id,
            project_root=args.project_root, settings=settings, client=client, model=model,
        ))
        print(result["reply"])
        logger.info("工作区保存：saved={} path={} error={}",
                    result["workspace_saved"], result["workspace_path"], result["backup_error"])

    finally:
        stop.set()
        worker.join(timeout=5)


if __name__ == "__main__":
    main()
