"""飞书正式服务启动文件，可在 PyCharm 中右键 Run 或点击运行。

解释器选择项目 .venv/Scripts/python.exe，无需填写运行参数。
此入口会连接数据库、Redis、飞书；收到用户消息后调用真实 Agent。
同一个飞书应用只运行一个接收进程，避免多个长连接分摊消息。
"""
import os
from pathlib import Path


def main() -> None:
    # PyCharm 的工作目录可能是 scripts/。统一到项目根目录，
    # 让日志和依赖相对路径的配置与终端启动保持一致。
    os.chdir(Path(__file__).resolve().parents[1])

    # 延迟导入：工作目录确定后再初始化日志；SDK 的事件循环仍由
    # channels.feishu.__main__.main 管理，不在此处额外 asyncio.run。
    from channels.feishu.__main__ import main as run_feishu
    run_feishu()


if __name__ == "__main__":
    main()
