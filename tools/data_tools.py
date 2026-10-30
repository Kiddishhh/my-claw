"""Workspace-bound local table tools; numerical calculations use Decimal."""
from collections import defaultdict
from decimal import Decimal, localcontext
from functools import wraps
from pathlib import Path
from time import perf_counter
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict
from langchain.tools import tool
from langchain_core.tools import BaseTool, ToolException

from tools._table_store import TableStore, decimal_value


class TableFilter(BaseModel):
    """Explicit comparisons; no Python or SQL expressions are evaluated."""
    model_config = ConfigDict(extra="forbid")
    column: str
    operator: Literal["eq", "ne", "contains", "gt", "ge", "lt", "le", "is_null", "not_null"]
    value: str | None = None


def _guard(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        from infra.logging import get_logger
        logger = get_logger()
        started = perf_counter()
        logger.info("表格工具开始：{}", fn.__name__)
        try:
            result = fn(*args, **kwargs)
        except ToolException:
            logger.warning("表格工具失败：{}", fn.__name__)
            raise
        except (OSError, ValueError, KeyError, TypeError, OverflowError) as error:
            logger.warning("表格工具失败：{} type={}", fn.__name__, type(error).__name__)
            raise ToolException("表格处理失败，请检查文件格式、字段和参数；原始数据未修改。") from None
        logger.info("表格工具完成：{} rows={} 耗时={:.0f}ms", fn.__name__, result.get("row_count", "-"), (perf_counter()-started)*1000)
        return result
    return wrapped


def _columns_exist(columns: list[str], requested: list[str]) -> None:
    if any(column not in columns for column in requested):
        raise ToolException("字段不存在，请先用 inspect_table 检查实际列名。")


def _matches(row: dict, condition: TableFilter) -> bool:
    value = row[condition.column]
    if condition.operator == "is_null":
        return value is None
    if condition.operator == "not_null":
        return value is not None
    if condition.value is None:
        raise ToolException("此筛选操作需要 value。")
    if condition.operator == "eq":
        return value == condition.value
    if condition.operator == "ne":
        return value != condition.value
    if value is None:
        return False
    if condition.operator == "contains":
        return condition.value in value
    left, right = decimal_value(value), decimal_value(condition.value)
    return {"gt": left > right, "ge": left >= right, "lt": left < right, "le": left <= right}[condition.operator]


def create_data_tools(workspace_root: Path) -> list[BaseTool]:
    """Create four tools sharing an isolated workspace and result store."""
    store = TableStore(workspace_root)

    @tool
    @_guard
    def inspect_table(file_path: str, sheet_name: str | None = None, encoding: str = "utf-8-sig") -> dict:
        """检查本地 CSV/XLSX 的字段、缺失、重复行与前5行，返回 dataset_id 供 analyze_table 使用。

        file_path 为 data/inputs/ 内相对路径（如 sales.csv）；不接受外部路径或 URL。
        XLSX 多工作表必须指定 sheet_name；CSV 编码可用 utf-8-sig、utf-8、gb18030。
        只读取原文件并保存分析快照，不清洗或计算公式。上限10 MiB、10万行、100列。
        样本可能含业务数据；完整数据不进入返回消息。字段按文本保留，数字类型仅为建议。
        """
        data = store.read(file_path, sheet_name, encoding)
        rows, columns = data["rows"], data["columns"]
        types = {}
        for column in columns:
            values = [row[column] for row in rows if row[column] is not None]
            try:
                for value in values:
                    decimal_value(value)
                types[column] = "decimal_candidate" if values else "empty"
            except ToolException:
                types[column] = "text_or_date"
        return {"dataset_id": store.save("dataset", data), "source": data["source"],
                "sheet_name": data["sheet_name"], "sheets": data["sheets"], "columns": columns,
                "row_count": len(rows), "types": types,
                "missing": {column: sum(row[column] is None for row in rows) for column in columns},
                "duplicate_rows": len(rows) - len({tuple(row.values()) for row in rows}), "preview": rows[:5]}

    @tool
    @_guard
    def analyze_table(
        dataset_id: str, metric: str | None = None,
        operation: Literal["sum", "mean", "min", "max", "count"] = "sum",
        group_by: list[str] | None = None, filters: list[TableFilter] | None = None,
        missing: Literal["error", "drop"] = "error", deduplicate: bool = False,
        date_column: str | None = None, date_format: str | None = None,
        frequency: Literal["day", "month", "year"] = "month",
        descending: bool = False, top_n: int | None = None,
        join_dataset_id: str | None = None, join_on: str | None = None,
        join_how: Literal["left", "inner"] = "left",
    ) -> dict:
        """对 inspect_table 的快照执行条件筛选、可选去重、多对一关联及 Decimal 分组统计。

        dataset_id 使用 inspect_table 返回值。sum/mean/min/max 必须指定 metric；count 统计行数，不接受 metric。
        group_by 指定分组列（缺失分组保留为 null）；结果指标列统一为 value，原始分组列不能叫 value。
        filters 是 AND 条件列表，含 column/operator/value；eq/ne 为文本精确比较，gt/ge/lt/le 比较十进制数。
        missing=error 拒绝缺失指标；仅用户确认后用 drop 丢弃缺失指标行。非法数值始终报错，不自动补0。
        date_column 将日期变为日/月/年分组，须指定 date_format（如 %Y-%m-%d）；无效日期报错。
        join_dataset_id 仅支持同名 join_on 的多对一关联，右表键必须唯一且非空，避免金额膨胀。
        descending 控制按 value 排序；top_n 为1～10000，省略保留全部组。返回前20行及 result_id，完整结果存本地。
        金额以十进制字符串返回；均值使用50位精度，不自动货币舍入。不会执行任意 Python/SQL。
        """
        data = store.load(dataset_id, "dataset")
        rows = data["rows"]
        columns = list(data["columns"])
        source_rows = len(rows)
        provenance = [{key: data[key] for key in ("source", "sha256", "sheet_name")}]
        if join_dataset_id:
            if not join_on:
                raise ToolException("关联需要指定 join_on。")
            right = store.load(join_dataset_id, "dataset")
            _columns_exist(columns, [join_on])
            _columns_exist(right["columns"], [join_on])
            if (set(columns) & set(right["columns"])) - {join_on}:
                raise ToolException("关联表有重名非键字段，请先明确重命名。")
            lookup = {}
            for row in right["rows"]:
                key = row[join_on]
                if key is None or key in lookup:
                    raise ToolException("右表关联键必须非空且唯一。")
                lookup[key] = row
            extra = [c for c in right["columns"] if c != join_on]
            rows = [{**row, **{c: lookup.get(row[join_on], {}).get(c) for c in extra}}
                    for row in rows if join_how == "left" or row[join_on] in lookup]
            columns += extra
            provenance.append({key: right[key] for key in ("source", "sha256", "sheet_name")})
        elif join_on:
            raise ToolException("join_on 必须与 join_dataset_id 一起使用。")
        groups = list(group_by or [])
        if len(set(groups)) != len(groups) or "value" in groups or date_column == "value":
            raise ToolException("分组字段不可重复，也不能使用保留结果列名 value。")
        if operation != "count" and not metric:
            raise ToolException("数值统计必须指定 metric。")
        if operation == "count" and metric:
            raise ToolException("count 统计行数，请省略 metric。")
        _columns_exist(columns, groups + ([metric] if metric else []) + ([date_column] if date_column else []))
        conditions = filters or []
        _columns_exist(columns, [f.column for f in conditions])
        rows = [row for row in rows if all(_matches(row, f) for f in conditions)]
        filtered_rows = len(rows)
        if deduplicate:
            unique = {tuple(row[c] for c in columns): row for row in rows}
            rows = list(unique.values())
        duplicate_dropped = filtered_rows - len(rows)
        missing_dropped = 0
        if metric:
            missing_dropped = sum(row[metric] is None for row in rows)
            if missing_dropped and missing == "error":
                raise ToolException("指标存在缺失值；请明确筛选规则或使用 missing=drop。")
            rows = [row for row in rows if row[metric] is not None]
        if date_column:
            if not date_format:
                raise ToolException("日期分组必须显式提供 date_format。")
            if any(row[date_column] is None for row in rows):
                raise ToolException("日期列存在缺失值，请先明确筛选规则。")
            try:
                dates = pd.to_datetime([row[date_column] for row in rows], format=date_format, errors="raise")
                if dates.isna().any():
                    raise ValueError("Missing date")
                labels = dates.strftime({"day": "%Y-%m-%d", "month": "%Y-%m", "year": "%Y"}[frequency])
            except (ValueError, TypeError):
                raise ToolException("日期格式无效或与 date_format 不匹配。") from None
            rows = [{**row, date_column: label} for row, label in zip(rows, labels)]
            if date_column not in groups:
                groups.append(date_column)
        buckets = defaultdict(list)
        for row in rows:
            buckets[tuple(row[c] for c in groups)].append(decimal_value(row[metric]) if metric else Decimal(1))
        records = []
        with localcontext() as context:
            context.prec = 50
            for key, values in buckets.items():
                # Preserve exact sums even when valid decimals have different scales.
                context.prec = max(50, max(v.adjusted() for v in values)
                                   - min(v.as_tuple().exponent for v in values)
                                   + len(str(len(values))) + 2)
                total = sum(values, Decimal(0))
                number = {"sum": lambda: total, "mean": lambda: total / len(values),
                          "min": lambda: min(values), "max": lambda: max(values),
                          "count": lambda: Decimal(len(values))}[operation]()
                records.append({**dict(zip(groups, key)), "value": format(number, "f")})
        records.sort(key=lambda r: decimal_value(r["value"], computed=True), reverse=descending)
        if top_n is not None and (type(top_n) is not int or not 1 <= top_n <= 10000):
            raise ToolException("top_n 必须为1～10000的整数。")
        group_count = len(records)
        records = records[:top_n] if top_n is not None else records
        result = {"columns": groups + ["value"], "rows": records, "sources": provenance,
                  "dataset_id": dataset_id, "source_rows": source_rows, "rows_used": len(rows),
                  "filtered_rows": filtered_rows, "duplicate_dropped": duplicate_dropped,
                  "missing_dropped": missing_dropped, "group_count": group_count,
                  "parameters": {"operation": operation, "metric": metric, "group_by": groups,
                    "filters": [f.model_dump() for f in conditions], "missing": missing,
                    "deduplicate": deduplicate, "date_column": date_column, "date_format": date_format,
                    "frequency": frequency, "descending": descending, "top_n": top_n,
                    "join_dataset_id": join_dataset_id, "join_on": join_on, "join_how": join_how}}
        identity = store.save("result", result)
        return {"result_id": identity, "columns": result["columns"], "row_count": len(records),
                "rows_used": len(rows), "missing_dropped": missing_dropped,
                "duplicate_dropped": duplicate_dropped, "group_count": group_count,
                "preview": records[:20], "preview_truncated": len(records) > 20,
                "path": f"outputs/analysis/{identity}/result.json"}

    from tools.report_tools import create_report_tools
    tools = [inspect_table, analyze_table, *create_report_tools(store)]
    for item in tools:
        item.handle_tool_error = True
        item.handle_validation_error = "分析参数无效，请按工具描述提供字段和参数。"
    return tools
