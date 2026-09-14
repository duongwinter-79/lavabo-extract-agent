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
import zipfile
from datetime import date, timedelta
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from .spec import (DOC_SPECS, PLACEHOLDER, SHEETS, STATIC_DOCS, DocSpec,
                   SheetSpec)

log = logging.getLogger(__name__)

HEADER_FILL = PatternFill("solid", fgColor="1F3864")
HEADER_FONT = Font(color="FFFFFF", bold=True)
REQUIRED_FILL = PatternFill("solid", fgColor="C00000")
EXAMPLE_FONT = Font(color="808080", italic=True)
TITLE_FONT = Font(bold=True, size=13)

INT_COLUMNS_FORMAT = "#,##0"
DATE_FORMAT = "yyyy-mm-dd"

IMAGE_NOTE = """Bỏ ảnh sản phẩm vào thư mục này.

Đặt tên theo mã sản phẩm trong catalog.xlsx:

    BC52-80-TRANG__front.jpg      ảnh chính, chụp thẳng
    BC52-80-TRANG__angle.jpg      chụp nghiêng
    BC52-80-TRANG__detail.jpg     cận cảnh chi tiết
    BC52-80-TRANG__lapdat.jpg     ảnh đã lắp trong phòng tắm thật
    BC52-80-TRANG__size.jpg       bản vẽ kích thước

Mỗi mã ít nhất 1 ảnh. Mẫu bán chạy nên có 3-5 ảnh.
Ảnh JPG hoặc PNG, cạnh dài từ 1200px trở lên, dưới 8MB.
Ảnh iPhone dạng HEIC phải đổi sang JPG trước.

Nếu không muốn đổi tên file: cứ để tên máy ảnh (IMG_4821.jpg) và khai
vào file images.xlsx. Ảnh không có tên trong catalog.xlsx hoặc
images.xlsx thì AI không nhìn thấy.

ẢNH ĐANG Ở TRONG ĐIỆN THOẠI? Đừng đổi tên từng file. Đọc
00-GUI-ANH-TU-DIEN-THOAI.md — chỉ cần xếp vào thư mục theo mã sản
phẩm, và video demo cũng gửi được.

KHÔNG gửi: ảnh chụp màn hình có tên hoặc số điện thoại khách, ảnh có
logo chìm của shop khác.
"""


def write_intake(directory: Path, *, force: bool = False) -> tuple[list[Path], list[Path]]:
    """Create the intake folder. Returns (written, skipped-because-they-exist)."""
    directory.mkdir(parents=True, exist_ok=True)
    images = directory / "images"
    images.mkdir(exist_ok=True)
    # An empty folder does not survive being zipped and emailed, and the naming rule has
    # to travel with the folder it applies to.
    note = images / "00-DAT-TEN-ANH.txt"
    if not note.exists() or force:
        note.write_text(IMAGE_NOTE, encoding="utf-8")

    written: list[Path] = []
    skipped: list[Path] = []

    for sheet in SHEETS:
        path = directory / sheet.filename
        if path.exists() and not force:
            skipped.append(path)
            continue
        _write_workbook(path, sheet)
        written.append(path)

    for doc in DOC_SPECS:
        path = directory / doc.filename
        if path.exists() and not force:
            skipped.append(path)
            continue
        path.write_text(render_doc(doc), encoding="utf-8")
        written.append(path)

    for name, body in STATIC_DOCS.items():
        path = directory / name
        if path.exists() and not force:
            skipped.append(path)
            continue
        path.write_text(body, encoding="utf-8")
        written.append(path)

    return written, skipped


def render_doc(doc: DocSpec) -> str:
    """A fill-in-the-blanks form, not a set of headings.

    Every blank is the same marker, so the shop can see at a glance what is left and
    `kb check` can say which fields are still empty. Without it a file returned
    untouched and a file somebody deliberately left empty look identical.
    """
    out = [f"# {doc.title}", "", f"> {doc.why}", ">",
           f"> Thay `{PLACEHOLDER}` bằng câu trả lời của shop. Phần sau mũi tên ← là ví dụ,",
           "> xoá đi cũng được. Mục nào chưa biết thì cứ để nguyên — gửi được đến đâu hay đến đó.",
           ""]

    for section in doc.sections:
        out += [f"## {section.heading}", ""]
        if section.intro:
            out += [f"*{section.intro}*", ""]

        if section.prose:
            out += [PLACEHOLDER, ""]
            continue

        for field in section.fields:
            line = f"- **{field.label}:** {PLACEHOLDER}"
            if field.example:
                line += f"   ← ví dụ: {field.example}"
            if not field.required:
                line += "   *(không bắt buộc)*"
            out.append(line)
            if field.note:
                out.append(f"  *{field.note}*")
        out.append("")

    return "\n".join(out).rstrip() + "\n"


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

    if sheet.seed_rows:
        # Seeded content is the answer, not an example of one, so it is not greyed out.
        for row in sheet.seed_rows:
            ws.append(list(row))
    else:
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


def write_zip(directory: Path, out: Path) -> Path:
    """Zip the pack, because the person forwarding it should not have to send 14 files.

    Entry order and timestamps are pinned so a rebuild that changed nothing produces a
    recognisably similar archive. The bytes still differ run to run -- openpyxl stamps a
    creation time inside every workbook -- so nothing should compare these by hash; the
    drift test compares the pack's CONTENT instead.
    """
    files = sorted(q for q in directory.rglob("*") if q.is_file())
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")

    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            info = zipfile.ZipInfo(
                str(Path(directory.name) / path.relative_to(directory)),
                date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())

    os.replace(tmp, out)
    log.info("wrote %s (%d files)", out, len(files))
    return out
