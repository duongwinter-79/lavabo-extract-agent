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

# Screenshots of the Page's own inbox are a better price source than a marketing graphic:
# the number is in the shop's own words, to a real customer, recently. But only when the
# SHOP said it. A price the customer quotes is somebody else's, and a price the shop
# quotes may be a deal for that customer rather than the list price -- so who spoke, and
# what they actually typed, are columns rather than an assumption.
CHAT_SYSTEM = (
    "Bạn đọc ảnh chụp màn hình một cuộc trò chuyện Messenger giữa SHOP bán thiết bị vệ "
    "sinh và KHÁCH. Ghi lại đúng những gì nhìn thấy. Không suy đoán. "
    "Không chắc ai nói thì để 'khong_ro' — đoán sai người nói là lỗi nặng nhất ở đây."
)

CHAT_USER = (
    "Trong ảnh chụp màn hình này:\n"
    "- Có ai nói về giá của sản phẩm nào không?\n"
    "- nguoi_bao_gia: 'shop' nếu tin nhắn nằm bên phía shop, 'khach' nếu bên phía khách, "
    "'khong_ro' nếu không phân biệt được.\n"
    "- cau_noi_nguyen_van: chép NGUYÊN VĂN câu có chứa giá.\n"
    "- gia: đổi sang số nguyên đồng ('2tr850' -> 2850000). Không rõ thì null.\n"
    "- la_gia_khuyen_mai: true nếu câu đó nói rõ là giảm giá, khuyến mãi, giá riêng cho "
    "khách này."
)

CHAT_SCHEMA = {
    "type": "object",
    "properties": {
        "co_noi_ve_gia": {"type": "boolean"},
        "nguoi_bao_gia": {"type": ["string", "null"],
                          "enum": ["shop", "khach", "khong_ro", None]},
        "san_pham": {"type": ["string", "null"]},
        "kich_thuoc": {"type": ["string", "null"]},
        "gia": {"type": ["integer", "null"]},
        "cau_noi_nguyen_van": {"type": ["string", "null"]},
        "la_gia_khuyen_mai": {"type": ["boolean", "null"]},
        "ngay_thang_neu_co": {"type": ["string", "null"]},
    },
    "required": ["co_noi_ve_gia", "nguoi_bao_gia", "san_pham", "kich_thuoc", "gia",
                 "cau_noi_nguyen_van", "la_gia_khuyen_mai", "ngay_thang_neu_co"],
    "additionalProperties": False,
}

CHAT_COLUMNS = ["ten_file", "co_noi_ve_gia", "nguoi_bao_gia", "san_pham", "kich_thuoc",
                "gia_doc_duoc", "la_gia_khuyen_mai", "cau_noi_nguyen_van",
                "ngay_thang_neu_co", "ma_sp", "da_kiem_tra"]

MODES = {
    "product": (SYSTEM, USER, SCHEMA, DRAFT_COLUMNS),
    "chat": (CHAT_SYSTEM, CHAT_USER, CHAT_SCHEMA, CHAT_COLUMNS),
}


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
    mode: str = "product"

    @property
    def products(self) -> list[ImageReading]:
        key = "co_noi_ve_gia" if self.mode == "chat" else "la_san_pham"
        return [r for r in self.readings if not r.error and r.values.get(key)]

    @property
    def with_price(self) -> list[ImageReading]:
        return [r for r in self.products if r.values.get("gia")]

    @property
    def quoted_by_shop(self) -> list[ImageReading]:
        """The only readings whose price the shop could be asked to honour."""
        return [r for r in self.with_price if r.values.get("nguoi_bao_gia") == "shop"]

    @property
    def failed(self) -> list[ImageReading]:
        return [r for r in self.readings if r.error]


def read_folder(source: Path, extractor, *, limit: int | None = None,
                mode: str = "product") -> DraftReport:
    """Ask the model about each image in turn. `extractor` is anything exposing
    `complete_json_images`, which both providers already do."""
    system, user, schema, _ = MODES[mode]
    report = DraftReport(mode=mode)
    photos = sorted(p for p in source.rglob("*")
                    if p.is_file() and p.suffix.lower() in PHOTO_SUFFIXES)

    for path in photos[:limit] if limit else photos:
        try:
            completion = extractor.complete_json_images(
                system, user, [path.read_bytes()], schema,
                mime_type=MIME.get(path.suffix.lower(), "image/jpeg"))
        except Exception as exc:                      # one unreadable image is not a run
            log.warning("%s: %s", path.name, exc)
            report.readings.append(ImageReading(path=path, values={}, error=str(exc)))
            continue

        report.readings.append(ImageReading(path=path, values=completion.data))
        report.input_tokens += completion.input_tokens
        report.output_tokens += completion.output_tokens

    return report


def write_draft(report: DraftReport, target: Path) -> Path:
    """A draft for a human to confirm, shaped so confirming is quick.

    `gia_doc_duoc` is named for what it is -- a number read off a picture -- so nobody
    mistakes the file for a price list. `da_kiem_tra` is the column that turns it into one.
    """
    columns = MODES[report.mode][3]
    warning = ("BẢN NHÁP đọc từ ảnh chụp hội thoại — PHẢI đối chiếu với shop. "
               "Giá khách nói KHÔNG phải giá của shop; giá shop nói có thể là giá "
               "riêng cho khách đó, không phải giá niêm yết."
               if report.mode == "chat" else
               "BẢN NHÁP đọc từ ảnh — PHẢI đối chiếu với shop trước khi dùng. "
               "Giá trên ảnh có thể cũ hoặc của nơi khác.")

    wb = Workbook()
    ws = wb.active
    ws.title = "Dữ liệu"
    ws.append([warning])
    ws.append(columns)

    for reading in report.readings:
        if reading.error:
            ws.append([reading.path.name, "LỖI", reading.error[:120]])
            continue
        v = reading.values
        if report.mode == "chat":
            ws.append([reading.path.name,
                       "có" if v.get("co_noi_ve_gia") else "không",
                       v.get("nguoi_bao_gia"), v.get("san_pham"), v.get("kich_thuoc"),
                       v.get("gia"), "có" if v.get("la_gia_khuyen_mai") else "",
                       v.get("cau_noi_nguyen_van"), v.get("ngay_thang_neu_co"), "", ""])
        else:
            ws.append([reading.path.name,
                       "có" if v.get("la_san_pham") else "không",
                       v.get("ten_sp"), v.get("loai"), v.get("kich_thuoc"), v.get("mau"),
                       v.get("chat_lieu"), v.get("gia"), v.get("gia_nhin_thay_o"),
                       v.get("chu_khac_trong_anh"), "", ""])

    for column, width in zip("ABCDEFGHIJKL",
                             (28, 12, 30, 14, 14, 16, 18, 40, 20, 20, 12, 12)):
        ws.column_dimensions[column].width = width
    ws.freeze_panes = "A3"

    target.parent.mkdir(parents=True, exist_ok=True)
    wb.save(target)
    log.info("wrote %s", target)
    return target
