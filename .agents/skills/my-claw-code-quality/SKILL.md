---
name: my-claw-code-quality
description: Use when implementing, fixing, refactoring, or reviewing Python code in the my-claw repository, particularly Agent middleware, LangGraph state, uv packaging, configuration, logging, and local database integration. Also applies when the user requests code standards or a code review for this project.
---

# my-claw 代码规范与 Review

先读取仓库根目录的 [AGENTS.md](../../../AGENTS.md)，公共规范以该文件为准。
本技能提供任务执行和审查方法，不授予模型调用、外部发布或数据删除权限。

## 选择模式

| 用户目标 | 模式 | 产出 |
| --- | --- | --- |
| 实现、修复、重构 | 开发 | 聚焦的代码改动、验证结果、未验证范围 |
| 检查、review、审查 diff | Review | 有证据的发现；默认只读 |
| 修改并 review | 开发后自查 | 分开报告改动、验证和剩余风险 |

用户只问代码含义时直接解释，不把教学问题变成重构任务。

## 开发模式

1. 读取 `git status --short`、相关 diff、调用方和已有测试，区分用户改动与本次改动。
2. 用真实错误或最小输入定位原因。导入问题先检查 `sys.executable`、项目是否可编辑安装及包发现范围；`cwd` 正确不等于项目可导入。
3. 使用当前 `.venv` 的库签名或官方资料确认第三方 API。修改最小行为边界，保留用户指定的实现与已有兼容契约。
4. 按改动选择行为验证。修复须复现原问题；对关键逻辑使用离线假模型或 handler，保留框架协议，避免真实请求。
5. 根据影响运行相关测试；涉及运行逻辑、包结构或依赖时运行现有 unittest 套件。纯文档只校验结构、链接和内容一致性。
6. 交付实际修改、执行过的验证、结果和明确的未验证事项。

常用命令从项目根目录运行：

```powershell
uv sync --locked --all-extras
uv run --all-extras python -m unittest discover -s tests -v
uv pip check --python .venv\Scripts\python.exe
git diff --check
```

直接运行 `scripts/devmate_with_mw.py` 会调用真实模型，不能用它代替纯导入测试。
优先使用 `tests/test_project_imports.py` 的隔离方式。读取实际文件确认当前入口集合。
部分模块导入时会初始化日志、加载 settings 并写入相对 cwd 的 logs；导入验证使用临时工作目录、占位凭据和关闭的追踪，不将“未执行 main”等同于没有副作用。

## Review 模式

先明确范围：指定文件、未提交 diff、某个提交或分支比较。用户未指定时查看工作区变更和未跟踪源码；不要把整个历史代码库都作为新增问题。
沿调用链核对真实触发条件、状态来源、输出去向和清理路径，再按相关性检查下表。

| 检查边界 | 要回答的问题 |
| --- | --- |
| 解释器与打包 | 项目是否装入实际使用的 .venv？文件启动、模块启动和外部 cwd 是否成立？新包是否被发现？ |
| 异步协议 | handler 是否正确 await、注解是否为 Awaitable？是否阻塞事件循环、吞掉取消、缺少超时或资源释放？ |
| 状态更新 | state_schema 是否只被当作结构？NotRequired 缺失是否处理？节点字典、模型 ExtendedModelResponse 和工具 Command 是否符合对应协议？ |
| 累计与恢复 | 普通字段是覆盖还是 reducer 合并？重试、并发或恢复是否能在具体路径中重复计数或丢更新？ |
| 工具审计 | 抛异常与返回 status=error 是否区分？是否能用请求/工具调用 ID 关联记录？ |
| 用量提取 | usage 缺失与真实零是否区分？是否只取了一条消息？是否错误宣称金额或预算限制已经实现？ |
| 身份上下文 | 值来自可信调用方还是可修改输入？共享实例是否隔离用户？正常 CLI 单用户模式是否被误报为多用户漏洞？ |
| 日志与配置 | 是否泄露 key、含密码 URL 或异常局部变量？终端与调试是否读取同一 .env？ |
| 数据库 | 本机端口、凭据、数据挂载是否一致？初始化 SQL 在旧目录上是否执行？容器 healthy 是否被误当成应用连接通过？ |
| 验证 | 测试是否真正覆盖改动行为？是否需要真实服务？是否把“没运行”或环境失败写成通过？ |

只把能说明触发条件和实际后果的问题作为 finding。已有问题注明“既有”；推测、未来部署风险和环境限制单独列为待验证项。类型注解、注释和风格建议不自动提升为功能缺陷。

Review 输出先列发现，按影响排序。每条包含：

- `[P0/P1/P2/P3] 简短标题`：P0 为紧急系统性问题，P1 为高影响问题，P2 为普通功能缺陷，P3 为低影响问题。
- `位置`：可点击文件与准确行号，尽量指向最小相关范围。
- `触发与影响`：何时发生、产生什么可观察后果。
- `证据`：源码路径、复现结果或测试结果；明确未运行的检查。
- `建议`：针对根因的最小处理方向，不擅自实施修复。

没有可确认的问题时明确写“未发现可确认的问题”，随后说明覆盖范围和验证缺口，不为凑数量制造结论。

## 常见误判

- `model_provider=openai` 不能单独证明请求发往 OpenAI；核对实际 base_url 和 SDK 配置。
- `ToolMessage` 正常返回不代表工具业务成功；检查 status 或具体结果协议。 “完成”文案本身不等于宣称成功；若错误结果使用同一日志，准确描述为缺少结果区分，不凭文案推断工具业务行为错误。
- 字段没有 reducer 时提交新累计值是覆盖写入；只有存在相应并发或重试路径时才能判定丢更新或重复计算。
- 中间件构造参数固定用户可用于单用户 Agent；是否构成身份泄露取决于是否跨用户复用该实例。
- schema 声明、模型响应、图状态与 runtime context 是不同层次；不要把状态字段误当成自动发送给模型的参数。
