"""Blank intake workbooks, generated from `spec.py`.

A blank page is why the pack never arrives. This turns it into a form: the columns are
already there, the dropdowns only offer legal values, and a price cell refuses "2tr850"
at the moment somebody types it rather than a week later when the file is rejected.

Nothing here overwrites a file that already exists -- the one thing this must never do is
eat a catalogue somebody spent an evening filling in.
"""

from __future__ import annotations

import logging
import os
from datetime import date, timedelta
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from .spec import DOCS, SHEETS, SheetSpec

log = logging.getLogger(__name__)

HEADER_FILL = PatternFill("solid", fgColor="1F3864")
HEADER_FONT = Font(color="FFFFFF", bold=True)
REQUIRED_FILL = PatternFill("solid", fgColor="C00000")
EXAMPLE_FONT = Font(color="808080", italic=True)
TITLE_FONT = Font(bold=True, size=13)

INT_COLUMNS_FORMAT = "#,##0"
DATE_FORMAT = "yyyy-mm-dd"


def write_intake(directory: Path, *, force: bool = False) -> tuple[list[Path], list[Path]]:
    """Create the intake folder. Returns (written, skipped-because-they-exist)."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "images").mkdir(exist_ok=True)

    written: list[Path] = []
    skipped: list[Path] = []

    for sheet in SHEETS:
        path = directory / sheet.filename
        if path.exists() and not force:
            skipped.append(path)
            continue
        _write_workbook(path, sheet)
        written.append(path)

    for name, body in DOCS.items():
        path = directory / name
        if path.exists() and not force:
            skipped.append(path)
            continue
        path.write_text(body, encoding="utf-8")
        written.append(path)

    return written, skipped


def _write_workbook(path: Path, sheet: SheetSpec) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Dữ liệu"

    ws.append([f.name for f in sheet.fields])
    for idx, fld in enumerate(sheet.fields, start=1):
        cell = ws.cell(row=1, column=idx)
        cell.fill = REQUIRED_FILL if fld.required else HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        # The header's own comment is the only documentation most people will read.
        cell.comment = _comment(fld)
        ws.column_dimensions[get_column_letter(idx)].width = _width(fld)

    ws.append(_example_row(sheet))
    for idx in range(1, len(sheet.fields) + 1):
        ws.cell(row=2, column=idx).font = EXAMPLE_FONT

    _apply_formats(ws, sheet)
    _apply_validation(ws, sheet)

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(sheet.fields))}1"

    _instructions(wb.create_sheet("Hướng dẫn"), sheet)

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    wb.save(tmp)
    os.replace(tmp, path)
    log.info("wrote %s", path)


def _comment(fld):
    from openpyxl.comments import Comment
    bits = [fld.label]
    if fld.required:
        bits.append("BẮT BUỘC")
    if fld.enum:
        bits.append("Chọn: " + " / ".join(fld.enum))
    if fld.note:
        bits.append(fld.note)
    c = Comment("\n".join(bits), "lavabo")
    c.width, c.height = 320, 160
    return c


def _width(fld) -> int:
    if fld.kind in ("integer", "date"):
        return 16
    return 40 if fld.name in ("ghi_chu_tu_van", "tra_loi", "cau_hoi", "noi_dung") else 22


def _example_row(sheet: SheetSpec) -> list:
    """One greyed-out row showing the shape. Marked so `kb check` can spot it later."""
    today = date.today()
    row = []
    for fld in sheet.fields:
        if fld.example:
            row.append(int(fld.example) if fld.kind == "integer" else fld.example)
        elif fld.kind == "date":
            # A promo that expired the day it was written would fail the validator on
            # the shop's first run, which reads as the tool being broken.
            row.append(today + timedelta(days=45) if fld.name in ("den_ngay", "km_den_ngay")
                       else today)
        else:
            row.append("")
    return row


def _apply_formats(ws, sheet: SheetSpec) -> None:
    for idx, fld in enumerate(sheet.fields, start=1):
        fmt = {"integer": INT_COLUMNS_FORMAT, "date": DATE_FORMAT}.get(fld.kind)
        if not fmt:
            continue
        for row in range(2, 500):
            ws.cell(row=row, column=idx).number_format = fmt


def _apply_validation(ws, sheet: SheetSpec) -> None:
    for idx, fld in enumerate(sheet.fields, start=1):
        col = get_column_letter(idx)
        rng = f"{col}2:{col}500"

        if fld.enum:
            dv = DataValidation(
                type="list",
                formula1='"' + ",".join(fld.enum) + '"',
                allow_blank=True,
                showErrorMessage=fld.strict_enum,
            )
            dv.errorTitle, dv.error = "Giá trị không hợp lệ", "Chọn: " + " / ".join(fld.enum)
        elif fld.kind == "integer":
            # Catches "2tr850" where it happens, which is the only place it is cheap to fix.
            dv = DataValidation(type="whole", operator="greaterThanOrEqual", formula1=0,
                                allow_blank=True, showErrorMessage=True)
            dv.errorTitle = "Chỉ nhập chữ số"
            dv.error = ("Ô này chỉ nhận số nguyên: 2850000.\n"
                        "Không nhập 2tr850, 2.850.000đ hay '2-3tr'.")
        elif fld.kind == "date":
            dv = DataValidation(type="date", operator="greaterThan", formula1="DATE(2020,1,1)",
                                allow_blank=True, showErrorMessage=True)
            dv.errorTitle, dv.error = "Ngày không hợp lệ", "Nhập dạng 2026-09-20."
        else:
            continue

        ws.add_data_validation(dv)
        dv.add(rng)


def _instructions(ws, sheet: SheetSpec) -> None:
    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 26
    ws.column_dimensions["C"].width = 12
    ws.column_dimensions["D"].width = 70

    ws["A1"] = sheet.title
    ws["A1"].font = TITLE_FONT
    ws["A2"] = sheet.intro
    ws["A2"].alignment = Alignment(wrap_text=True, vertical="top")
    ws.merge_cells("A2:D2")
    ws.row_dimensions[2].height = 45

    ws.append([])
    ws.append(["Cột", "Là gì", "Bắt buộc", "Lưu ý"])
    for cell in ws[4]:
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT

    for fld in sheet.fields:
        note = fld.note
        if fld.enum:
            note = ("Chọn: " + " / ".join(fld.enum) + (". " + note if note else ""))
        ws.append([fld.name, fld.label, "có" if fld.required else "", note])
        ws.cell(row=ws.max_row, column=4).alignment = Alignment(wrap_text=True, vertical="top")

    ws.append([])
    ws.append(["", "", "", "Dòng chữ xám trong sheet Dữ liệu là ví dụ — xoá đi trước khi gửi."])
    ws.cell(row=ws.max_row, column=4).font = EXAMPLE_FONT
