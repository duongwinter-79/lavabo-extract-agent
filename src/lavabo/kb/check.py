"""Validate the intake pack before any of it is uploaded to Meta.

The rule this enforces is the one from docs/13 §4: a catalogue with one bad row is a
catalogue with one public lie. So a fatal problem rejects the whole file rather than
letting the good rows through -- a partially-uploaded price list is worse than none,
because it looks finished.

Fatal stops the upload. A warning is something the shop should know and can ship without.
Messages are Vietnamese and carry the row number, because the person who fixes them is
looking at the spreadsheet, not at this code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from openpyxl import load_workbook

from ..money import parse_vnd
from .spec import (CATALOG, EXAMPLE_MARKER, FAQ, IMAGES, PROMOTIONS, SHEETS,
                   STALE_FAIL_DAYS, STALE_WARN_DAYS, Field, SheetSpec)


@dataclass(frozen=True, slots=True)
class Problem:
    file: str
    message: str
    row: int | None = None
    fatal: bool = True

    def __str__(self) -> str:
        where = f"{self.file}" + (f" dòng {self.row}" if self.row else "")
        return f"{where}: {self.message}"


# "2-3tr", "2 - 3 triệu", "từ 2tr": a range is not a price, and guessing which end the
# shop meant is exactly the behaviour this whole pipeline exists to prevent.
_RANGE = re.compile(r"\d\s*[-–—]\s*\d|^\s*(từ|khoảng|tầm)\b", re.IGNORECASE)
_MONEY_IN_TEXT = re.compile(r"\b\d{1,3}(?:[.,]\d{3})+\b|\b\d+\s*(?:tr|triệu|trieu|k|nghìn)\b",
                            re.IGNORECASE)

# "thứ 5", "20/9", "ngày 20": docs/13 §3 wants a range in thoi_gian_giao, because a
# named day read out by a bot is a delivery promise nobody in the shop agreed to.
# The folder also carries the naming instructions we put there; a .txt file is not a
# photo the shop forgot to attach to a product.
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif", ".bmp"}

_SPECIFIC_DAY = re.compile(r"\bthứ\s*[2-7]\b|\bchủ\s*nhật\b|\bngày\s*\d{1,2}\b|"
                           r"\b\d{1,2}\s*/\s*\d{1,2}\b", re.IGNORECASE)


def check_intake(directory: Path) -> list[Problem]:
    problems: list[Problem] = []
    tables: dict[str, list[dict]] = {}

    for sheet in SHEETS:
        path = directory / sheet.filename
        if not path.exists():
            if sheet.required_file:
                problems.append(Problem(sheet.filename, "thiếu file này"))
            continue
        rows, file_problems = _read_and_check(path, sheet)
        problems.extend(file_problems)
        tables[sheet.filename] = rows

    problems.extend(_cross_checks(directory, tables))
    return problems


# --------------------------------------------------------------------------- per file

def _read_and_check(path: Path, sheet: SheetSpec) -> tuple[list[dict], list[Problem]]:
    problems: list[Problem] = []
    wb = load_workbook(path, data_only=True)
    ws = wb["Dữ liệu"] if "Dữ liệu" in wb.sheetnames else wb.worksheets[0]

    header = [str(c).strip().lower() if c is not None else "" for c in next(
        ws.iter_rows(min_row=1, max_row=1, values_only=True), ())]
    index = {name: i for i, name in enumerate(header) if name}

    missing = [f.name for f in sheet.fields if f.required and f.name not in index]
    if missing:
        problems.append(Problem(sheet.filename,
                                f"thiếu cột bắt buộc: {', '.join(missing)}"))
        return [], problems

    rows: list[dict] = []
    seen: dict[str, int] = {}

    for number, values in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if all(v is None or str(v).strip() == "" for v in values):
            continue
        row = {name: values[i] if i < len(values) else None for name, i in index.items()}
        row["_row"] = number
        rows.append(row)

        for fld in sheet.fields:
            if fld.name in index:
                problems.extend(_check_cell(sheet, fld, row, number))

        if sheet.unique:
            key = _text(row.get(sheet.unique))
            if key:
                if key.lower() in seen:
                    problems.append(Problem(
                        sheet.filename,
                        f"{sheet.unique} {key!r} đã có ở dòng {seen[key.lower()]}. "
                        "Mỗi mã chỉ được xuất hiện một lần.", row=number))
                seen[key.lower()] = number

    if not rows:
        problems.append(Problem(sheet.filename, "file không có dòng dữ liệu nào"))

    problems.extend(_sheet_rules(sheet, rows))
    return rows, problems


def _check_cell(sheet: SheetSpec, fld: Field, row: dict, number: int) -> list[Problem]:
    out: list[Problem] = []
    value = row.get(fld.name)
    blank = value is None or str(value).strip() == ""

    if blank:
        if fld.required:
            out.append(Problem(sheet.filename, f"cột {fld.name} ({fld.label}) đang trống",
                               row=number))
        elif fld.required_with and not _blank(row.get(fld.required_with)):
            out.append(Problem(
                sheet.filename,
                f"có {fld.required_with} thì bắt buộc phải có {fld.name}. {fld.note}",
                row=number))
        return out

    if fld.kind == "integer":
        out.extend(_check_integer(sheet, fld, value, number))
    elif fld.kind == "date" and _as_date(value) is None:
        out.append(Problem(sheet.filename,
                           f"cột {fld.name}: {value!r} không phải ngày. Nhập dạng 2026-09-20.",
                           row=number))
    elif fld.kind == "enum" and fld.strict_enum:
        if _text(value).lower() not in {e.lower() for e in fld.enum}:
            out.append(Problem(
                sheet.filename,
                f"cột {fld.name}: {_text(value)!r} không hợp lệ. "
                f"Chỉ nhận: {' / '.join(fld.enum)}", row=number))
    return out


def _check_integer(sheet: SheetSpec, fld: Field, value, number: int) -> list[Problem]:
    if isinstance(value, (int, float)) and float(value).is_integer():
        return ([] if value >= 0 else
                [Problem(sheet.filename, f"cột {fld.name}: số âm ({value})", row=number)])

    text = _text(value)
    if _RANGE.search(text):
        return [Problem(
            sheet.filename,
            f"cột {fld.name}: {text!r} là một khoảng giá, không phải một giá. "
            "Nếu giá thay đổi theo đơn thì bỏ dòng này ra khỏi danh mục và "
            "để nhân viên báo giá.", row=number)]

    if text.replace(".", "").replace(",", "").replace(" ", "").isdigit():
        # "2.850.000" typed as text: unambiguous, but say so rather than silently accept.
        return [Problem(
            sheet.filename,
            f"cột {fld.name}: {text!r} đang là chữ, không phải số. "
            f"Nhập {int(re.sub(r'[^0-9]', '', text))} (chỉ chữ số).", row=number)]

    guess = parse_vnd(text)
    fix = (f"Nhập {guess} (chỉ chữ số)." if guess
           else "Ô giá chỉ nhận chữ số, ví dụ 2850000.")
    return [Problem(sheet.filename,
                    f"cột {fld.name}: {text!r} không phải số nguyên. {fix}", row=number)]


def _sheet_rules(sheet: SheetSpec, rows: list[dict]) -> list[Problem]:
    if sheet is CATALOG:
        return _catalog_rules(rows)
    if sheet is PROMOTIONS:
        return [Problem(sheet.filename,
                        f"khuyến mãi {_text(r.get('ten_km'))!r} đã hết hạn — nên xoá khỏi file",
                        row=r["_row"], fatal=False)
                for r in rows
                if (d := _as_date(r.get("den_ngay"))) and d < date.today()]
    if sheet is FAQ:
        return [Problem(sheet.filename,
                        "câu trả lời có vẻ chứa con số giá. Giá chỉ được nằm trong "
                        "catalog.xlsx — để ở hai nơi thì một nơi sẽ cũ.",
                        row=r["_row"], fatal=False)
                for r in rows if _MONEY_IN_TEXT.search(_text(r.get("tra_loi")))]
    return []


def _catalog_rules(rows: list[dict]) -> list[Problem]:
    out: list[Problem] = []
    today = date.today()
    names: dict[str, int] = {}

    for r in rows:
        n = r["_row"]
        listed, promo = _int(r.get("gia_niem_yet")), _int(r.get("gia_km"))

        if listed is not None and promo is not None and promo >= listed:
            out.append(Problem(CATALOG.filename,
                               f"giá khuyến mãi ({promo}) không nhỏ hơn giá niêm yết ({listed})",
                               row=n))

        if promo is not None and (until := _as_date(r.get("km_den_ngay"))) and until < today:
            out.append(Problem(
                CATALOG.filename,
                f"giá khuyến mãi đã hết hạn ngày {until} nhưng vẫn còn trong file. "
                "AI sẽ báo giá này cho khách. Xoá gia_km hoặc gia hạn km_den_ngay.", row=n))

        if updated := _as_date(r.get("cap_nhat_ngay")):
            age = (today - updated).days
            if age > STALE_FAIL_DAYS:
                out.append(Problem(CATALOG.filename,
                                   f"dòng này chưa được kiểm tra lại {age} ngày "
                                   f"(quá {STALE_FAIL_DAYS}). Xác nhận lại giá rồi cập nhật "
                                   "cột cap_nhat_ngay.", row=n))
            elif age > STALE_WARN_DAYS:
                out.append(Problem(CATALOG.filename,
                                   f"chưa kiểm tra lại {age} ngày", row=n, fatal=False))

        if _SPECIFIC_DAY.search(_text(r.get("thoi_gian_giao"))):
            out.append(Problem(CATALOG.filename,
                               f"thoi_gian_giao {_text(r.get('thoi_gian_giao'))!r} là một ngày "
                               "cụ thể. Ghi khoảng ('3-5 ngày') — AI đọc cột này ra thành "
                               "lời hứa giao hàng.", row=n, fatal=False))
        if _blank(r.get("anh")):
            out.append(Problem(CATALOG.filename, "chưa có ảnh", row=n, fatal=False))
        if _blank(r.get("ghi_chu_tu_van")):
            out.append(Problem(CATALOG.filename, "chưa có ghi chú tư vấn", row=n, fatal=False))
        if EXAMPLE_MARKER in _text(r.get("ma_sp")):
            out.append(Problem(CATALOG.filename,
                               "dòng ví dụ mẫu vẫn còn trong file — xoá trước khi gửi",
                               row=n, fatal=False))

        key = re.sub(r"\s+", " ", _text(r.get("ten_sp"))).lower()
        if key and key in names:
            out.append(Problem(CATALOG.filename,
                               f"tên trùng với dòng {names[key]} — có phải dòng lặp không?",
                               row=n, fatal=False))
        names.setdefault(key, n)

    return out


# ------------------------------------------------------------------------ cross-file

def _cross_checks(directory: Path, tables: dict[str, list[dict]]) -> list[Problem]:
    out: list[Problem] = []
    catalog = tables.get(CATALOG.filename, [])
    if not catalog:
        return out

    images_dir = directory / "images"
    on_disk = {p.name.lower() for p in images_dir.iterdir()
               if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
               } if images_dir.is_dir() else set()
    mapped = {_text(r.get("ten_file")).lower() for r in tables.get(IMAGES.filename, [])}

    if on_disk:
        for r in catalog:
            for name in [n.strip() for n in _text(r.get("anh")).split(";") if n.strip()]:
                if name.lower() not in on_disk:
                    out.append(Problem(CATALOG.filename,
                                       f"ảnh {name!r} không có trong thư mục images/",
                                       row=r["_row"], fatal=False))
        unused = on_disk - {n.strip().lower()
                            for r in catalog
                            for n in _text(r.get("anh")).split(";") if n.strip()} - mapped
        if unused:
            out.append(Problem("images/", f"{len(unused)} ảnh không gắn với sản phẩm nào "
                                          "— AI sẽ không bao giờ gửi chúng", fatal=False))

    known = {_text(r.get("ma_sp")).lower() for r in catalog}
    for r in tables.get(IMAGES.filename, []):
        sku = _text(r.get("ma_sp"))
        if sku and sku.lower() not in known:
            out.append(Problem(IMAGES.filename,
                               f"mã {sku!r} không có trong catalog.xlsx", row=r["_row"]))
    return out


# ----------------------------------------------------------------------------- utils

def _text(value) -> str:
    return "" if value is None else str(value).strip()


def _blank(value) -> bool:
    return _text(value) == ""


def _int(value) -> int | None:
    if isinstance(value, (int, float)) and float(value).is_integer():
        return int(value)
    text = _text(value)
    return int(text) if text.isdigit() else None


def _as_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _text(value)
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def report(problems: list[Problem]) -> str:
    fatal = [p for p in problems if p.fatal]
    warn = [p for p in problems if not p.fatal]
    lines: list[str] = []

    if fatal:
        lines.append(f"LỖI ({len(fatal)}) — phải sửa trước khi tải lên:")
        lines += [f"  - {p}" for p in fatal]
    if warn:
        if fatal:
            lines.append("")
        lines.append(f"CẢNH BÁO ({len(warn)}) — nên sửa, nhưng không chặn:")
        lines += [f"  - {p}" for p in warn]
    if not problems:
        lines.append("Không có lỗi nào.")
    return "\n".join(lines)
