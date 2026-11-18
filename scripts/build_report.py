"""트래커 Excel 생성. 표 스펙(헤더·필드·서식·폭)만 받아 그리는 얇은 렌더러."""
from __future__ import annotations

import datetime as dt

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

FONT = "Arial"
NAVY = "1F3864"
F_TITLE = Font(name=FONT, size=14, bold=True, color=NAVY)
F_SUB = Font(name=FONT, size=9, italic=True, color="595959")
F_SEC = Font(name=FONT, size=10, bold=True, color="FFFFFF")
F_HDR = Font(name=FONT, size=9, bold=True, color=NAVY)
F_BODY = Font(name=FONT, size=9)
F_BLUE = Font(name=FONT, size=9, color="0000FF")
FILL_SEC = PatternFill("solid", fgColor=NAVY)
FILL_HDR = PatternFill("solid", fgColor="D9E1F2")
FILL = {
    "in_strong": PatternFill("solid", fgColor="C6EFCE"),
    "in_maybe": PatternFill("solid", fgColor="EAF5E1"),
    "out_strong": PatternFill("solid", fgColor="FFC7CE"),
    "out_maybe": PatternFill("solid", fgColor="FCE4E4"),
    "watch": PatternFill("solid", fgColor="FFF2CC"),
}
THIN = Side(style="thin", color="BFBFBF")
B_BOTTOM = Border(bottom=THIN)

FMT = {
    "int": "#,##0", "num1": "#,##0.0", "num2": "#,##0.00", "pct": "0.0%", "pct_pt": '0.0"%"',
    "krw_jo": '#,##0.00"조"', "usd_bn": '"$"#,##0.0"B"', "date": "yyyy-mm-dd", "x": '0.00"x"', "text": "@",
}


def verdict_fill(v) -> PatternFill | None:
    s = str(v or "")
    if not s:
        return None
    if s.startswith("편입 유력") or s.startswith("Fast Entry") or s.startswith("편입 확정"):
        return FILL["in_strong"]
    if s.startswith("편입"):
        return FILL["in_maybe"]
    if s.startswith("편출 유력") or s.startswith("편출 확정"):
        return FILL["out_strong"]
    if s.startswith("편출"):
        return FILL["out_maybe"]
    if "경합" in s or "관찰" in s or "확인" in s:
        return FILL["watch"]
    return None


def title(ws, text: str, sub: str = ""):
    ws["A1"] = text
    ws["A1"].font = F_TITLE
    if sub:
        ws["A2"] = sub
        ws["A2"].font = F_SUB
    ws.sheet_view.showGridLines = False


def section(ws, row: int, text: str, ncols: int) -> int:
    for c in range(1, ncols + 1):
        ws.cell(row=row, column=c).fill = FILL_SEC
    ws.cell(row=row, column=1, value=text).font = F_SEC
    return row + 1


def table(ws, row: int, df: pd.DataFrame, cols: list[tuple], verdict_col: str | None = "판정",
          empty_msg: str = "해당 없음") -> int:
    """cols = [(헤더, 필드, 서식키, 폭)]. 반환: 다음 빈 행(+1 간격)."""
    for j, (h, _, _, w) in enumerate(cols, start=1):
        c = ws.cell(row=row, column=j, value=h)
        c.font, c.fill, c.border = F_HDR, FILL_HDR, B_BOTTOM
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cur = ws.column_dimensions[get_column_letter(j)].width or 0
        ws.column_dimensions[get_column_letter(j)].width = max(cur, w)
    row += 1
    if df is None or df.empty:
        ws.cell(row=row, column=1, value=empty_msg).font = F_SUB
        return row + 2
    for rec in df.to_dict("records"):
        fill = verdict_fill(rec.get(verdict_col)) if verdict_col else None
        for j, (_, f, fmt, _) in enumerate(cols, start=1):
            v = rec.get(f)
            if isinstance(v, float) and pd.isna(v):
                v = None
            if isinstance(v, pd.Timestamp):
                v = v.date()
            c = ws.cell(row=row, column=j, value=v)
            c.font = F_BODY
            if fmt in FMT and fmt != "text":
                c.number_format = FMT[fmt]
            if fill is not None:
                c.fill = fill
        row += 1
    return row + 1


def kv(ws, row: int, items: list[tuple], blue_keys: set[str] = frozenset()) -> int:
    for k, v in items:
        ws.cell(row=row, column=1, value=k).font = F_HDR
        c = ws.cell(row=row, column=2, value=v)
        c.font = F_BLUE if k in blue_keys else F_BODY
        row += 1
    return row + 1


def new_workbook() -> Workbook:
    wb = Workbook()
    wb.remove(wb.active)
    return wb


def finish(wb: Workbook, path):
    for ws in wb.worksheets:
        ws.sheet_properties.outlinePr.summaryBelow = False
    wb.save(path)
