"""Writer for the QUẢN LÝ ĐƠN SENKAHOMES layout.

Differs from the generic writer in shape: an order occupies a GROUP of rows. The
first carries every order-level field, and each further product adds a row where
only the name and quantity are filled -- matching the existing workbook, which
averages 3.23 item rows per order.

Most columns never reach the model. Date, order number and customer come parsed
from the note's header; Xe thu hộ is arithmetic (Tổng - Cọc, verified against 89 of
90 real orders); STT is a counter. Only the address, the line items and the two
money strings are extracted, and the money is converted here rather than by the
model. See docs/06-output-mapping.md.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from ..models import Conversation, ExtractionResult
from ..money import parse_vnd

log = logging.getLogger(__name__)

HEADERS = [
    "STT", "NGÀY CHỐT", "Tên KH", "Địa chỉ", "Tên sản phẩm", "Số lượng",
    "Tổng Tiền hóa đơn", "Xe thu hộ", "Cọc", "Trạng thái", "Ngày hẹn giao",
    "Người chốt đơn",
]

MONEY_COLUMNS = {7, 8, 9}          # 1-based: Tổng, Xe thu hộ, Cọc
MONEY_FORMAT = "#,##0"
DATE_FORMAT = "DD/MM/YYYY"

HEADER_FILL = PatternFill("solid", fgColor="1F3864")
HEADER_FONT = Font(color="FFFFFF", bold=True)
MISSING_FILL = PatternFill("solid", fgColor="FFF2CC")
THIN = Side(style="thin", color="BFBFBF")


def _order_sort_key(conv: Conversation) -> tuple:
    raw = conv.raw or {}
    return (raw.get("order_month") or 0, raw.get("order_day") or 0,
            raw.get("order_number") or 0, conv.conversation_id)


def _order_date(conv: Conversation, default_year: int) -> date | None:
    raw = conv.raw or {}
    day, month = raw.get("order_day"), raw.get("order_month")
    if not day or not month:
        return None
    try:
        # Headers almost never carry a year; the capture is month-scoped, so the
        # run's year is the right default rather than something inferred per row.
        return date(raw.get("order_year") or default_year, int(month), int(day))
    except ValueError:
        return None


def _address(values: dict[str, Any]) -> str | None:
    """Address with the phone appended, matching the existing sheet's convention."""
    address = (values.get("address") or "").strip()
    phone = (values.get("phone") or "").strip()
    if address and phone and phone not in address:
        return f"{address} - {phone}"
    return address or phone or None


def _items(values: dict[str, Any]) -> list[tuple[str, Any]]:
    """[(name, quantity)] from the extracted object array, tolerating odd shapes."""
    out: list[tuple[str, Any]] = []
    for entry in values.get("items") or []:
        if isinstance(entry, dict):
            name = (entry.get("name") or "").strip()
            qty = entry.get("quantity")
        else:
            name, qty = str(entry).strip(), None
        if not name:
            continue
        try:
            qty = int(qty) if qty is not None and float(qty) == int(float(qty)) else qty
        except (TypeError, ValueError):
            pass
        out.append((name, qty if qty is not None else 1))
    return out


def write_orders_workbook(
    path: Path,
    conversations: list[Conversation],
    results: dict[str, ExtractionResult],
    *,
    sheet_name: str | None = None,
    default_year: int | None = None,
    default_status: str = "New",
    closer: str | None = None,
) -> Path:
    default_year = default_year or date.today().year
    orders = sorted(conversations, key=_order_sort_key)

    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name or f"{orders[0].raw.get('order_month', date.today().month):02d}{default_year}" \
        if orders else f"{date.today():%m%Y}"
    ws.append(HEADERS)

    stt = 0
    missing: list[str] = []

    for conv in orders:
        res = results.get(conv.conversation_id)
        values = res.values if res and not res.error else {}
        if not res or res.error:
            missing.append(conv.conversation_id)

        total = parse_vnd(values.get("total_text"))
        deposit = parse_vnd(values.get("deposit_text")) or 0
        # Not extracted: the existing workbook computes it, and arithmetic here
        # cannot disagree with itself the way a second extraction could.
        collect = (total - deposit) if total is not None else None

        items = _items(values) or [(None, None)]
        stt += 1
        first = True

        for name, qty in items:
            if first:
                ws.append([
                    stt,
                    _order_date(conv, default_year),
                    conv.customer_name,
                    _address(values),
                    name, qty,
                    total, collect, deposit,
                    default_status,
                    values.get("delivery_date_text") or None,
                    closer or None,
                ])
                first = False
            else:
                ws.append([None, None, None, None, name, qty,
                           None, None, None, None, None, None])

    _finish(ws, len(orders))
    _sheet_run(wb.create_sheet("Run"), orders, results, missing, default_status, closer)

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    wb.save(tmp)
    os.replace(tmp, path)

    log.info("wrote %s (%d orders, %d rows)", path, len(orders), ws.max_row - 1)
    if missing:
        log.warning("%d order(s) had no successful extraction and are blank beyond the "
                    "header fields", len(missing))
    return path


def _finish(ws, order_count: int) -> None:
    for i in range(1, len(HEADERS) + 1):
        cell = ws.cell(row=1, column=i)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    widths = [6, 13, 20, 42, 46, 9, 17, 14, 12, 13, 14, 15]
    for i, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = width

    for row in ws.iter_rows(min_row=2, max_col=len(HEADERS)):
        for cell in row:
            cell.border = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
            cell.alignment = Alignment(vertical="top", wrap_text=cell.column in (4, 5))
            if cell.column in MONEY_COLUMNS and isinstance(cell.value, (int, float)):
                cell.number_format = MONEY_FORMAT
            if cell.column == 2 and isinstance(cell.value, date):
                cell.number_format = DATE_FORMAT
        # Flag an order whose total never made it through, so a blank is not read as zero.
        if row[0].value is not None and row[6].value is None:
            row[6].fill = MISSING_FILL

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(HEADERS))}{ws.max_row}"


def _sheet_run(ws, orders, results, missing, status, closer) -> None:
    ok = [r for r in results.values() if not r.error]
    rows = [
        ("generated_at", datetime.now().isoformat(timespec="seconds")),
        ("orders", len(orders)),
        ("extractions_ok", len(ok)),
        ("orders_without_extraction", len(missing)),
        ("default_trạng thái", status),
        ("người chốt đơn", closer or "(not set — see docs/06-output-mapping.md)"),
        ("input_tokens", sum(r.input_tokens for r in results.values())),
        ("output_tokens", sum(r.output_tokens for r in results.values())),
        ("model", next((r.model for r in results.values() if r.model), "-")),
    ]
    ws.append(["key", "value"])
    for key, value in rows:
        ws.append([key, value])
    for i in (1, 2):
        ws.cell(row=1, column=i).fill = HEADER_FILL
        ws.cell(row=1, column=i).font = HEADER_FONT
        ws.column_dimensions[get_column_letter(i)].width = 34
    if missing:
        ws.append([])
        ws.append(["orders without extraction", ""])
        for cid in missing:
            ws.append(["", cid])
