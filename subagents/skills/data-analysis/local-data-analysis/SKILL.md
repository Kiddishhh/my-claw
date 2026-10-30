---
name: local-data-analysis
description: 分析本地 Excel/XLSX 或 CSV 表格，检查数据质量、明确指标口径、筛选、关联、分组统计或分析时间趋势时使用。适用于已注册的表格工具，或在远程 execute 中调用同名计算库；不用于网页搜索或修改原始表格。
---

# 本地表格分析

## 根据运行环境选择调用方式

- 本地模式：直接调用已注册的 inspect_table/analyze_table 等工具。
- Daytona 模式：通过 execute 在远程工作目录运行 Python 脚本；使用 `python -m uv run --locked --extra daytona python 脚本绝对路径`。
- 远程脚本导入 `from tools.data_tools import create_data_tools`，以 `Path(远程项目根目录)` 创建工具字典。
  在同一个 Python 进程、同一组工具实例中依次执行检查、计算、绘图和导出；dataset_id/result_id 对应内存存储，不跨命令复用。
- 所有表格计算仍由该库完成，不凭模型估算。只打印摘要与产物路径，不打印全表。
- 输入必须已经存在于远程 data/inputs/；未上传时报告缺少输入，不读取宿主文件。

## 数据与工具边界

- 输入位于当前 Agent 工作目录的 `data/inputs/`，使用 `inspect_table`，不要用 read_file 读取 XLSX 二进制。
- 原表只读，工具返回 dataset_id；后续使用该 ID 对快照计算，避免文件变更造成同次分析口径漂移。
- 支持逗号分隔 CSV 和 XLSX。CSV 默认 UTF-8 BOM 兼容；明确是中文旧编码时可使用 gb18030。
- 多个工作表时询问用户选择或根据明确任务选择；不任意合并。公式必须先转为值，旧 XLS 先转为 XLSX。
- 当前限制为10 MiB、100000行、100列，超过限制请用户拆分或缩小范围，不声称已分析全量。

## 分析方法

1. 对用户指定文件运行 inspect_table。根据字段、行数、缺失值、重复行、样本确认数据含义。数字候选不意味着业务上是数值；编号保持原样。
2. 明确目标指标、时间范围、单位、订单状态、退款口径。缺少关键口径时询问，不自行假设“金额列=销售收入”。
3. 调用 analyze_table。所有数值由工具计算；模型不能凭样本推算全量结果。
4. missing 默认 error。只有用户确认丢弃缺失指标行时使用 drop；不将缺失金额补为0。不自动去重，除非重复行确属同一记录。
5. sum/mean/min/max 指定 metric；count 是行数，不传 metric。filters 采用 AND，eq/ne 是文本比较，范围比较是十进制数。
6. 日期分组明确 date_column 与 date_format，如 `%Y-%m-%d`。ISO 时间字符串可指定实际格式，不猜测日/月顺序。
7. 多表关联先检查两表；只支持同名键、多对一关联。右表重复键必须先解决。重名非键字段不能直接关联；inner 会排除未匹配行，需说明。
8. 读取结果中的 rows_used、missing_dropped、duplicate_dropped、group_count，解释筛选影响。预览最多20行不是完整数据；完整结果位于返回的 path。

## 示例：按产品统计已付款金额

```json
{"file_path":"sales.csv"}
```

得到 dataset_id 后：

```json
{"dataset_id":"使用真实返回的ID","metric":"amount","operation":"sum","group_by":["product"],"filters":[{"column":"status","operator":"eq","value":"paid"}],"descending":true,"top_n":3}
```

`value` 为结果指标列且以十进制字符串返回。不能把字符串直接按字典序当作数值排序。
工具不自动做金额舍入或异常值剔除；均值为高精度十进制近似，报告注明单位及显示精度。

需要图表或报告时再读取 `analysis-reporting`。终端输出操作摘要，避免复制整表到对话。
模型消息及工具返回可能被已启用的 LangSmith 记录；不在提示词中粘贴额外原始明细。
