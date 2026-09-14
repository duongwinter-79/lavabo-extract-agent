"""Read product details off the pictures the shop already has.

Marketing images for this trade usually carry the specification burnt into the picture:
kích thước, màu, and very often a price. That is a catalogue nobody has typed up yet, and
the repo already knows how to ask a model for structured JSON about an image
(`Extractor.complete_json_images`, built for reading order screenshots).

Two rules shape everything here.

**Nothing it produces is a price.** A number on a promotional image is what somebody
charged on the day the image was made, possibly a different shop. It goes into a DRAFT
that a human confirms, never into catalog.xlsx, and the draft says which file each number
came from so confirming means looking at one picture rather than trusting a spreadsheet.

**One image, one call.** A bad image cannot poison a batch, progress survives an
interruption, and a re-run costs only the images that failed -- the same reasoning as
`docs/02` §3 for conversations.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from openpyxl import Workbook

log = logging.getLogger(__name__)

PHOTO_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".webp": "image/webp"}

SYSTEM = (
    "Bạn đọc ảnh sản phẩm thiết bị vệ sinh (tủ lavabo, gương, sen tắm, chậu rửa) và ghi "
    "lại ĐÚNG những gì NHÌN THẤY trong ảnh. Không suy đoán, không bịa. "
    "Thông tin nào ảnh không ghi rõ thì để null — null là câu trả lời đúng, "
    "đoán mò là sai."
)

USER = (
    "Đọc ảnh này và ghi lại thông tin sản phẩm.\n"
    "- Chỉ lấy chữ/số CÓ TRONG ẢNH. Giá không nhìn thấy rõ thì để null.\n"
    "- Giá ghi bằng số nguyên đồng: '2tr850' -> 2850000, '2.850.000đ' -> 2850000.\n"
    "- gia_nhin_thay_o: chép lại nguyên văn đoạn chữ chứa giá, để người kiểm tra đối chiếu.\n"
    "- Ảnh không phải ảnh sản phẩm (ảnh bìa, logo, ảnh cửa hàng) thì la_san_pham = false."
)

# Deliberately not the catalog schema: this is what an IMAGE can say. No ma_sp, because a
# photograph does not carry a product code, and a model asked for one will invent it.
SCHEMA = {
    "type": "object",
    "properties": {
        "la_san_pham": {"type": "boolean"},
        "ten_sp": {"type": ["string", "null"]},
        "loai": {"type": ["string", "null"]},
        "kich_thuoc": {"type": ["string", "null"]},
        "mau": {"type": ["string", "null"]},
        "chat_lieu": {"type": ["string", "null"]},
        "gia": {"type": ["integer", "null"]},
        "gia_nhin_thay_o": {"type": ["string", "null"]},
        "chu_khac_trong_anh": {"type": ["string", "null"]},
    },
    "required": ["la_san_pham", "ten_sp", "loai", "kich_thuoc", "mau", "chat_lieu",
                 "gia", "gia_nhin_thay_o", "chu_khac_trong_anh"],
    "additionalProperties": False,
}

DRAFT_COLUMNS = ["ten_file", "la_san_pham", "ten_sp", "loai", "kich_thuoc", "mau",
                 "chat_lieu", "gia_doc_duoc", "gia_nhin_thay_o", "chu_khac_trong_anh",
                 "ma_sp", "da_kiem_tra"]


@dataclass
class ImageReading:
    path: Path
    values: dict
    error: str = ""


@dataclass
class DraftReport:
    readings: list[ImageReading] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def products(self) -> list[ImageReading]:
        return [r for r in self.readings if not r.error and r.values.get("la_san_pham")]

    @property
    def with_price(self) -> list[ImageReading]:
        return [r for r in self.products if r.values.get("gia")]

    @property
    def failed(self) -> list[ImageReading]:
        return [r for r in self.readings if r.error]


def read_folder(source: Path, extractor, *, limit: int | None = None) -> DraftReport:
    """Ask the model about each image in turn. `extractor` is anything exposing
    `complete_json_images`, which both providers already do."""
    report = DraftReport()
    photos = sorted(p for p in source.rglob("*")
                    if p.is_file() and p.suffix.lower() in PHOTO_SUFFIXES)

    for path in photos[:limit] if limit else photos:
        try:
            completion = extractor.complete_json_images(
                SYSTEM, USER, [path.read_bytes()], SCHEMA,
                mime_type=MIME.get(path.suffix.lower(), "image/jpeg"))
        except Exception as exc:                      # one unreadable image is not a run
            log.warning("%s: %s", path.name, exc)
            report.readings.append(ImageReading(path=path, values={}, error=str(exc)))
            continue

        report.readings.append(ImageReading(path=path, values=completion.values))
        report.input_tokens += completion.input_tokens
        report.output_tokens += completion.output_tokens

    return report


def write_draft(report: DraftReport, target: Path) -> Path:
    """A draft for a human to confirm, shaped so confirming is quick.

    `gia_doc_duoc` is named for what it is -- a number read off a picture -- so nobody
    mistakes the file for a price list. `da_kiem_tra` is the column that turns it into one.
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "Dữ liệu"
    ws.append(["BẢN NHÁP đọc từ ảnh — PHẢI đối chiếu với shop trước khi dùng. "
               "Giá trên ảnh có thể cũ hoặc của nơi khác."])
    ws.append(DRAFT_COLUMNS)

    for reading in report.readings:
        if reading.error:
            ws.append([reading.path.name, "LỖI", reading.error[:120]])
            continue
        v = reading.values
        ws.append([reading.path.name,
                   "có" if v.get("la_san_pham") else "không",
                   v.get("ten_sp"), v.get("loai"), v.get("kich_thuoc"), v.get("mau"),
                   v.get("chat_lieu"), v.get("gia"), v.get("gia_nhin_thay_o"),
                   v.get("chu_khac_trong_anh"), "", ""])

    for column, width in zip("ABCDEFGHIJKL",
                             (28, 12, 30, 14, 14, 14, 18, 16, 30, 40, 20, 12)):
        ws.column_dimensions[column].width = width
    ws.freeze_panes = "A3"

    target.parent.mkdir(parents=True, exist_ok=True)
    wb.save(target)
    log.info("wrote %s", target)
    return target
