"""启动基础 API：uv run python -m scripts.serve_api，默认 127.0.0.1:8000。

当前只支持单进程启动；setup 在启动时执行，暂不增加 worker/队列。
"""
import argparse
import uvicorn
from infra.async_runtime import run_async


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    args = parser.parse_args()
    server = uvicorn.Server(uvicorn.Config("api.app:app", host=args.host, port=args.port))
    run_async(server.serve())


if __name__ == "__main__":
    main()
