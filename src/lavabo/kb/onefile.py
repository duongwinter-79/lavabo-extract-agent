"""The whole intake pack as one workbook, for Google Sheets.

The folder pack assumes a PC, Excel, and somebody comfortable with files. A shop owner
working from a phone has none of those, and a .zip over Zalo is where the pack goes to
die. One workbook uploads to Google Sheets as one link: they fill it in a browser, on a
phone, and nobody sends anything back.

The text forms become sheets too -- label, answer, example -- which is a better form than
markdown was for this audience anyway, and makes every part of the pack checkable by the
same code.

What does not survive the trip to Sheets: the whole-number guard on price cells is an
Excel feature, so `2tr850` can be typed there. `kb check --file` still catches it; the
difference is when, not whether.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter

from .spec import (CATALOG, DOC_SPECS, FAQ, HANDOFF, IMAGES, PLACEHOLDER,
                   PROMOTIONS, SHEETS, SHIPPING, STATIC_DOCS, SYNONYMS, DocSpec,
                   SheetSpec)
from .templates import (EXAMPLE_FONT, HEADER_FILL, HEADER_FONT, REQUIRED_FILL,
                        TITLE_FONT, _apply_formats, _apply_validation, _comment,
                        _width, seed_or_example)

log = logging.getLogger(__name__)

# Tab names the shop reads, in the order they should fill them.
TABS: dict[str, SheetSpec] = {
    "Danh mục sản phẩm": CATALOG,
    "Phí vận chuyển": SHIPPING,
    "Câu hỏi thường gặp": FAQ,
    "Không được tự trả lời": HANDOFF,
    "Từ khách hay dùng": SYNONYMS,
    "Khuyến mãi": PROMOTIONS,
    "Ảnh sản phẩm": IMAGES,
}
DOC_TABS: dict[str, DocSpec] = {
    "Cửa hàng": DOC_SPECS[0],
    "Chính sách": DOC_SPECS[1],
    "Giọng shop": DOC_SPECS[2],
}
DOC_HEADERS = ["Mục", "Trả lời", "Ví dụ", "Bắt buộc"]
ANSWER_COLUMN = 2


def write_one_file(out: Path, *, force: bool = False) -> Path:
    if out.exists() and not force:
        raise FileExistsError(f"{out} đã có sẵn — dùng --force để ghi đè")

    wb = Workbook()
    _instructions(wb.active)
    for title, doc in DOC_TABS.items():
        _doc_tab(wb.create_sheet(title), doc)
    for title, sheet in TABS.items():
        _data_tab(wb.create_sheet(title), sheet)

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    wb.save(tmp)
    os.replace(tmp, out)
    log.info("wrote %s (%d tabs)", out, len(wb.sheetnames))
    return out


# ------------------------------------------------------------------------- writing

def _instructions(ws) -> None:
    ws.title = "Đọc trước"
    ws.column_dimensions["A"].width = 100
    ws["A1"] = "Bộ biểu mẫu cho trợ lý trả lời tin nhắn Facebook"
    ws["A1"].font = TITLE_FONT
    row = 3
    for line in STATIC_DOCS["00-DOC-TRUOC.md"].splitlines():
        text = line.strip()
        if text.startswith("# "):
            continue
        cell = ws.cell(row=row, column=1, value=text.lstrip("#").strip())
        if line.startswith("##"):
            cell.font = Font(bold=True, size=12)
        cell.alignment = Alignment(wrap_text=True, vertical="top")
        row += 1
    ws.cell(row=row + 1, column=1,
            value="Các tab bên dưới: điền tab nào trước cũng được. "
                  "Tab 'Danh mục sản phẩm' có thể để sau cùng.").font = EXAMPLE_FONT


def _doc_tab(ws, doc: DocSpec) -> None:
    ws.append(DOC_HEADERS)
    for idx, _ in enumerate(DOC_HEADERS, start=1):
        cell = ws.cell(row=1, column=idx)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
    for width, letter in zip((46, 52, 52, 12), "ABCD"):
        ws.column_dimensions[letter].width = width

    ws.append([doc.why, "", "", ""])
    ws.cell(row=2, column=1).font = EXAMPLE_FONT
    ws.cell(row=2, column=1).alignment = Alignment(wrap_text=True, vertical="top")

    for section in doc.sections:
        ws.append([f"— {section.heading} —", "", "", ""])
        ws.cell(row=ws.max_row, column=1).font = Font(bold=True)

        if section.prose:
            ws.append([section.heading, "", section.intro, "có"])
            ws.cell(row=ws.max_row, column=2).alignment = Alignment(wrap_text=True,
                                                                    vertical="top")
            continue

        for field in section.fields:
            ws.append([field.label, "", field.example or field.note,
                       "có" if field.required else ""])
            ws.cell(row=ws.max_row, column=3).font = EXAMPLE_FONT

    ws.freeze_panes = "A2"


def _data_tab(ws, sheet: SheetSpec) -> None:
    ws.append([f.name for f in sheet.fields])
    for idx, field in enumerate(sheet.fields, start=1):
        cell = ws.cell(row=1, column=idx)
        cell.fill = REQUIRED_FILL if field.required else HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        cell.comment = _comment(field)
        ws.column_dimensions[get_column_letter(idx)].width = _width(field)

    seed_or_example(ws, sheet)

    _apply_formats(ws, sheet)
    _apply_validation(ws, sheet)
    ws.freeze_panes = "A2"


# ------------------------------------------------------------------------- reading

def read_one_file(path: Path):
    """Split the workbook back into what `check` already knows how to validate.

    Returns (worksheet-by-spec, answers-by-doc) so the folder pack and the single
    workbook run the same rules instead of two drifting copies of them.
    """
    wb = load_workbook(path, data_only=True)
    sheets: list[tuple[str, SheetSpec, object]] = []
    docs: list[tuple[str, DocSpec, dict[str, str]]] = []

    for title, spec in TABS.items():
        if title in wb.sheetnames:
            sheets.append((title, spec, wb[title]))
    for title, doc in DOC_TABS.items():
        if title in wb.sheetnames:
            docs.append((title, doc, _answers(wb[title])))
    return sheets, docs


def _answers(ws) -> dict[str, str]:
    out: dict[str, str] = {}
    for label, answer, *_ in ws.iter_rows(min_row=2, values_only=True):
        text = "" if label is None else str(label).strip()
        if not text or text.startswith("—"):
            continue
        value = "" if answer is None else str(answer).strip()
        out[text.lower()] = "" if value == PLACEHOLDER else value
    return out
