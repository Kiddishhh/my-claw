"""Charts and reports derived only from stored computation results."""
import csv
import json
import math
from threading import Lock
from typing import Literal
from uuid import uuid4

from langchain.tools import tool
from langchain_core.tools import BaseTool, ToolException
from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

from tools._table_store import TableStore, decimal_value

_PLOT_LOCK = Lock()


def create_report_tools(store: TableStore) -> list[BaseTool]:
    """Bind plot and export tools to the same store as table analysis."""
    from tools.data_tools import _guard

    @tool
    @_guard
    def plot_analysis(
        result_id: str, x: str, y: str = "value",
        chart_type: Literal["bar", "line", "scatter"] = "bar",
        title: str = "Analysis", x_label: str = "", y_label: str = "",
    ) -> dict:
        """将 analyze_table 已计算结果绘制为 PNG，返回本地图片路径。

        result_id 必须来自 analyze_table；x/y 必须是结果列，默认 y=value。
        bar 用于类别比较，line 用于有序日期趋势（按 x 文本排序），scatter 的 x/y 必须是数值。
        类别图要求 x 唯一；多个分组维度需先重新聚合。最多绘制1000行，不静默采样。
        title、x_label、y_label 说明含义及单位；不接受自编数值或 Python 绘图代码。
        图表使用浮点显示，精确统计值仍以 result.json 和导出表格中的十进制字符串为准。
        """
        data = store.load(result_id, "result")
        if x not in data["columns"] or y not in data["columns"]:
            raise ToolException("图表字段不在分析结果中。")
        rows = data["rows"]
        if not rows or len(rows) > 1000:
            raise ToolException("图表需要1～1000行结果，请先调整筛选或分组。")
        if any(row[x] is None or row[y] is None for row in rows):
            raise ToolException("图表坐标存在缺失值，请先调整分析范围。")
        if chart_type != "scatter" and len({row[x] for row in rows}) != len(rows):
            raise ToolException("类别或时间轴标签重复，请减少分组维度或重新聚合。")
        if chart_type == "line":
            rows = sorted(rows, key=lambda row: row[x])
        values = [float(decimal_value(row[y], computed=True)) for row in rows]
        labels = [row[x] for row in rows]
        if chart_type == "scatter":
            labels = [float(decimal_value(label, computed=True)) for label in labels]
        if any(not math.isfinite(v) for v in values):
            raise ToolException("图表数值超出显示范围。")
        path = store.output(result_id, f"chart-{uuid4().hex}.png")
        with _PLOT_LOCK:
            from matplotlib import rc_context
            from matplotlib.figure import Figure
            from matplotlib.backends.backend_agg import FigureCanvasAgg
            from matplotlib import font_manager
            available = {font.name for font in font_manager.fontManager.ttflist}
            font = next((name for name in ("Microsoft YaHei", "SimHei", "Noto Sans CJK SC") if name in available), "DejaVu Sans")
            with rc_context({"font.family": font, "axes.unicode_minus": False}):
                figure = Figure(figsize=(10, 5), layout="constrained")
                FigureCanvasAgg(figure)
                axis = figure.subplots()
                if chart_type == "bar":
                    axis.bar(labels, values)
                elif chart_type == "line":
                    axis.plot(labels, values, marker="o")
                else:
                    axis.scatter(labels, values)
                axis.set(title=title[:200], xlabel=(x_label or x)[:200], ylabel=(y_label or y)[:200])
                axis.tick_params(axis="x", labelrotation=30)
                axis.grid(axis="y", alpha=0.2)
                figure.savefig(path, dpi=150)
                figure.clear()
        return {"path": store.relative(path), "chart_type": chart_type, "row_count": len(rows)}

    @tool
    @_guard
    def export_report(result_id: str, title: str = "数据分析报告") -> dict:
        """导出已计算结果为 CSV、XLSX 和 Markdown 报告，返回三个文件路径。

        result_id 使用 analyze_table 返回值；title 仅为报告标题，不作为文件路径。
        报告包含来源及 SHA256、筛选/关联/分组口径、使用和丢弃行数、结果摘要与已有图表链接。
        CSV/XLSX 包含全部结果行；报告预览前50行。十进制值以文本导出，避免 Excel 精度损失。
        CSV 对可能被表格软件解释为公式的文本加单引号，XLSX 单元格强制为字符串。
        不接受模型自编统计数值，也不覆盖源文件；每次导出使用新文件名。
        """
        data = store.load(result_id, "result")
        prefix = f"report-{uuid4().hex}"
        paths = [store.output(result_id, prefix + suffix) for suffix in (".csv", ".xlsx", ".md")]
        columns, rows = data["columns"], data["rows"]

        # openpyxl truncates long strings silently; reject before creating any export.
        for value in [*columns, *(value for row in rows for value in row.values())]:
            if value is not None and (len(str(value)) > 32767 or ILLEGAL_CHARACTERS_RE.search(str(value))):
                raise ToolException("结果含 Excel 不支持的控制字符或超过32767字符的单元格；请缩小字段后导出。")

        def text(value):
            return "" if value is None else str(value)

        def csv_safe(value):
            value = text(value)
            if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r")):
                try:
                    decimal_value(value)
                except ToolException:
                    return "'" + value
            return value

        with paths[0].open("w", encoding="utf-8-sig", newline="") as output:
            writer = csv.writer(output)
            writer.writerow([csv_safe(c) for c in columns])
            writer.writerows([csv_safe(row[c]) for c in columns] for row in rows)
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Analysis"
        for values in [columns, *[[text(row[c]) for c in columns] for row in rows]]:
            sheet.append(values)
            for cell in sheet[sheet.max_row]:
                cell.data_type = "s"
        sheet.freeze_panes = "A2"
        workbook.save(paths[1])
        workbook.close()

        def md(value):
            return text(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ").replace("<", "&lt;").replace(">", "&gt;")

        lines = [f"# {md(title)}", "", f"结果 ID：{result_id}", "", "## 数据与口径", "",
                 "```json", json.dumps({key: data[key] for key in ("sources", "parameters", "source_rows", "filtered_rows", "rows_used", "missing_dropped", "duplicate_dropped", "group_count")}, ensure_ascii=False, indent=2), "```", "",
                 "## 计算结果", "", f"导出 {len(rows)} 行；下表最多展示50行。金额/数值按十进制文本保存，均值未自动货币舍入。", "",
                 "| " + " | ".join(md(c) for c in columns) + " |", "| " + " | ".join("---" for _ in columns) + " |"]
        lines += ["| " + " | ".join(md(row[c]) for c in columns) + " |" for row in rows[:50]]
        lines += ["", "## 图表", ""]
        lines += [f"![分析图表]({chart.name})" for chart in paths[2].parent.glob("chart-*.png")]
        lines += ["", "## 局限", "", "结果仅反映所选文件、工作表及过滤口径；缺失值/去重/关联策略见上文。未进行因果推断或自动异常值删除。"]
        paths[2].write_text("\n".join(lines) + "\n", encoding="utf-8")
        return {"paths": [store.relative(path) for path in paths], "row_count": len(rows)}

    return [plot_analysis, export_report]
