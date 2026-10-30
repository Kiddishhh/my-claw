"""Workspace-local immutable table snapshots and artifact boundaries."""
import csv
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any
from uuid import uuid4
from zipfile import BadZipFile, ZipFile

from langchain_core.tools import ToolException
from openpyxl import load_workbook

MAX_ROWS = 100000
MAX_COLUMNS = 100
MAX_BYTES = 10 * 1024 * 1024


def decimal_value(value: str, *, computed: bool = False) -> Decimal:
    """Parse a finite decimal without guessing currency or thousands separators."""
    try:
        number = Decimal(value)
        if not number.is_finite() or len(number.as_tuple().digits) > (512 if computed else 38) or abs(number.adjusted()) > (110 if computed else 100):
            raise InvalidOperation
        return number
    except (InvalidOperation, ValueError):
        raise ToolException("存在无效数值；请明确处理货币符号、千分位或异常值后重试。") from None


class TableStore:
    """Bind input and output paths to the same workspace used by the Agent."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()

    def bound(self, relative: str) -> Path:
        """Resolve a generated workspace path and reject external symlink targets."""
        path = (self.workspace / relative).resolve()
        if not path.is_relative_to(self.workspace):
            raise ToolException("路径必须位于当前 Agent 工作目录内。")
        return path

    def input_path(self, file_path: str) -> Path:
        """Accept filenames or virtual /data/inputs paths, never host absolute paths."""
        value = file_path.replace("\\", "/")
        if value.startswith("/data/inputs/"):
            value = value[1:]
        if ":" in value or value.startswith("/") or ".." in value.split("/"):
            raise ToolException("只允许 data/inputs/ 内的相对文件路径。")
        if not value.startswith("data/inputs/"):
            value = "data/inputs/" + value
        path = self.bound(value)
        if not path.is_relative_to(self.bound("data/inputs")) or not path.is_file():
            raise ToolException("输入文件不存在或超出 data/inputs/。")
        if path.suffix.lower() not in {".csv", ".xlsx"}:
            raise ToolException("仅支持 CSV 和 XLSX；旧版 XLS 请先另存为 XLSX。")
        if path.stat().st_size > MAX_BYTES:
            raise ToolException("输入文件超过 10 MiB 限制。")
        return path

    def save(self, kind: str, content: dict[str, Any]) -> str:
        """Save an immutable snapshot with a generated identifier."""
        identity = uuid4().hex
        folder = self.bound(f"outputs/analysis/{identity}")
        folder.mkdir(parents=True, exist_ok=False)
        (folder / f"{kind}.json").write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
        return identity

    def load(self, identity: str, kind: str) -> dict[str, Any]:
        """Resolve an artifact ID without accepting arbitrary file paths."""
        if not re.fullmatch(r"[a-f0-9]{32}", identity):
            raise ToolException("无效的数据集或分析结果 ID，请使用工具返回的 ID。")
        path = self.bound(f"outputs/analysis/{identity}/{kind}.json")
        if not path.is_file():
            raise ToolException("当前工作目录中找不到该 ID 对应的数据。")
        if path.stat().st_size > 100 * MAX_BYTES:
            raise ToolException("分析记录超过读取限制。")
        return json.loads(path.read_text(encoding="utf-8"))

    def output(self, identity: str, name: str) -> Path:
        """Create an output path only for a known analysis result."""
        self.load(identity, "result")
        return self.bound(f"outputs/analysis/{identity}/{name}")

    def relative(self, path: Path) -> str:
        """Return an artifact path relative to the Agent workspace."""
        return path.relative_to(self.workspace).as_posix()

    def read(self, file_path: str, sheet_name: str | None, encoding: str) -> dict[str, Any]:
        """Read bounded tables, preserving text, leading zeroes and missing cells."""
        path = self.input_path(file_path)
        sheets: list[str] = []
        workbook = None
        stream = None
        try:
            if path.suffix.lower() == ".csv":
                if sheet_name:
                    raise ToolException("CSV 不接受 sheet_name。")
                if encoding not in {"utf-8-sig", "utf-8", "gb18030"}:
                    raise ToolException("encoding 仅支持 utf-8-sig、utf-8、gb18030。")
                stream = path.open(encoding=encoding, newline="")
                rows = iter(csv.reader(stream))
            else:
                with ZipFile(path) as archive:
                    if sum(item.file_size for item in archive.infolist()) > 50 * MAX_BYTES:
                        raise ToolException("XLSX 解压后过大，请缩小数据范围。")
                workbook = load_workbook(path, read_only=True, data_only=False)
                sheets = workbook.sheetnames
                if sheet_name is None:
                    if len(sheets) != 1:
                        raise ToolException("多个工作表，请指定 sheet_name：" + ", ".join(sheets))
                    sheet_name = sheets[0]
                if sheet_name not in sheets:
                    raise ToolException("工作表不存在，可用 sheet_name：" + ", ".join(sheets))
                sheet = workbook[sheet_name]
                if sheet.max_row and sheet.max_row > MAX_ROWS + 1:
                    raise ToolException("工作表超过 100000 行限制。")
                if sheet.max_column and sheet.max_column > MAX_COLUMNS:
                    raise ToolException("工作表超过 100 列限制。")

                def excel_rows():
                    for row in sheet.iter_rows():
                        if any(cell.data_type == "f" for cell in row):
                            raise ToolException("工作表含公式，请先另存为值；工具不计算 Excel 公式。")
                        if any(cell.data_type == "e" for cell in row):
                            raise ToolException("工作表含 Excel 错误单元格，请先处理。")
                        yield [cell.value for cell in row]
                rows = iter(excel_rows())
            header = next(rows, None)
            if not header or len(header) > MAX_COLUMNS:
                raise ToolException("缺少有效表头或超过 100 列限制。")
            columns = [str(value).strip() if value is not None else "" for value in header]
            if any(not value for value in columns) or len(set(columns)) != len(columns):
                raise ToolException("表头存在空名称或重复名称，请先明确字段。")
            records = []
            for row in rows:
                if len(records) >= MAX_ROWS:
                    raise ToolException("表格超过 100000 行限制。")
                if not row:
                    continue
                if len(row) != len(columns):
                    raise ToolException("数据行列数与表头不一致。")
                converted = []
                for value in row:
                    if isinstance(value, (date, datetime)):
                        value = value.isoformat()
                    text = str(value) if value is not None else ""
                    converted.append(text if text.strip() else None)
                records.append(dict(zip(columns, converted)))
        except UnicodeError:
            raise ToolException("CSV 编码不匹配，请显式指定 encoding，例如 gb18030。") from None
        except (csv.Error, BadZipFile):
            raise ToolException("文件损坏或 CSV 字段超长，请检查格式后重试。") from None
        finally:
            if stream:
                stream.close()
            if workbook:
                workbook.close()
        return {"columns": columns, "rows": records, "source": self.relative(path),
                "sha256": sha256(path.read_bytes()).hexdigest(), "sheet_name": sheet_name,
                "sheets": sheets, "encoding": encoding if path.suffix.lower() == ".csv" else None}
