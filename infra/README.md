# 本地数据库

Docker Desktop 使用 WSL 2 后端。首次启用 Windows 虚拟机平台后，需要重启电脑，
然后启动 Docker Desktop，等待引擎就绪。

在项目根目录运行：

```powershell
docker compose --env-file .env.docker up -d --wait
docker compose --env-file .env.docker ps
```

`.env.docker` 从项目 `.env` 的数据库连接配置生成，已被 Git 忽略。
只包含数据库容器需要的配置，不包含模型和 LangSmith 密钥。
如果修改 `.env` 的数据库用户名、密码、数据库名或端口，也要同步修改 `.env.docker`。
PostgreSQL 的初始化环境变量只在空数据目录上生效，已有数据库需通过 SQL 更新账号或数据库。

服务仅绑定本机 `127.0.0.1`，PostgreSQL 默认端口为 5432，Redis 为 6379。
Redis 按当前项目配置不设密码，开启 AOF 持久化。

容器和数据目录：

| 服务 | 容器名称 | 镜像 | 本地数据目录 |
| --- | --- | --- | --- |
| PostgreSQL | syc-postgres | pgvector/pgvector:pg17 | C:\Users\WW\Desktop\code\docker-data\postgres |
| Redis | syc-redis | redis:8-alpine | C:\Users\WW\Desktop\code\docker-data\redis |

Compose 使用相对路径 `../docker-data`，数据位于项目目录的上一层 `code` 文件夹中。
首次初始化数据库时会自动启用 `vector` 扩展。已有数据库可执行：

```powershell
docker exec syc-postgres psql -U syc -d shanyang -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

停止容器并保留数据：

```powershell
docker compose --env-file .env.docker down
```

`down` 不会删除绑定到本机的数据库文件。保留 `docker-data` 目录即可保留数据。

项目 Python 依赖由 uv 管理，均安装在 `.venv`：

```powershell
uv sync --locked --all-extras
uv run --all-extras python -m tests.verify_env
```
