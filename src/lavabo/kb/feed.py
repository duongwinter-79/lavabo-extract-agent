"""catalog.xlsx -> a Meta Commerce product feed.

Business AI answers price and availability questions from a Facebook Catalog connected to
the Page, so the catalogue has to arrive as product data rather than as a document the
model reads. This builds that file.

It refuses to run while `kb check` has a fatal problem, and that is the point: a feed is
the moment a price stops being a spreadsheet and becomes something the Page says out loud.

Two fields the shop cannot satisfy on its own, both documented in docs/13 §6.2-6.3:

  link        Required by the feed spec, and this shop has no website. Pass --link with
              the Page URL (or --link-template with {ma_sp} if per-product URLs exist).
  image_link  Must be a PUBLICLY reachable URL. Phone photos in a folder are not that.
              Pass --image-base if the images are hosted; otherwise the rows come out
              without images and Meta will reject those rows -- which is the honest
              output, and the reason docs/13 recommends adding items by hand in Commerce
              Manager for a first catalogue of fifty.

Field names and the required set are from Meta's feed spec as of Sept 2026; re-check
before a first real upload.
"""

from __future__ import annotations

import csv
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

from ..tz import zone as tz_zone
from .check import _as_date, _int, _text
from .spec import CATALOG

log = logging.getLogger(__name__)

# id/title/description/availability/condition/price/link/image_link/brand are the
# required set; mpn is what satisfies the universal-ID rule for a shop with no GTIN.
COLUMNS = [
    "id", "title", "description", "availability", "condition", "price",
    "link", "image_link", "additional_image_link", "brand", "mpn",
    "sale_price", "sale_price_effective_date", "product_type", "custom_label_0",
]

AVAILABILITY = {
    "còn hàng": "in stock",
    "hết hàng": "out of stock",
    "đặt trước": "preorder",
}

TITLE_MAX = 200
DESCRIPTION_MAX = 9999
CURRENCY = "VND"


@dataclass(slots=True)
class FeedResult:
    path: Path
    rows: int
    without_image: int
    on_sale: int


def build_feed(
    rows: list[dict],
    out: Path,
    *,
    link: str = "",
    link_template: str = "",
    image_base: str = "",
    brand: str = "",
    timezone_name: str = "Asia/Ho_Chi_Minh",
) -> FeedResult:
    offset = _utc_offset(timezone_name)
    without_image = 0
    on_sale = 0

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")

    with tmp.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for row in rows:
            item = _item(row, link=link, link_template=link_template,
                         image_base=image_base, brand=brand, offset=offset)
            if not item["image_link"]:
                without_image += 1
            if item["sale_price"]:
                on_sale += 1
            writer.writerow(item)

    os.replace(tmp, out)
    log.info("wrote %s (%d rows)", out, len(rows))
    return FeedResult(path=out, rows=len(rows), without_image=without_image,
                      on_sale=on_sale)


def _item(row: dict, *, link: str, link_template: str, image_base: str,
          brand: str, offset: str) -> dict:
    sku = _text(row.get("ma_sp"))
    images = [n.strip() for n in _text(row.get("anh")).split(";") if n.strip()]
    urls = [_join(image_base, name) for name in images] if image_base else []

    listed = _int(row.get("gia_niem_yet"))
    promo = _int(row.get("gia_km"))
    until = _as_date(row.get("km_den_ngay"))

    sale_price, sale_window = "", ""
    # check.py has already rejected an expired promo and one that is not below the list
    # price, so reaching here means the discount is live and safe to publish.
    if promo is not None and until is not None:
        sale_price = _money(promo)
        sale_window = _window(until, offset)

    return {
        "id": sku,
        "title": _title(row),
        "description": _description(row),
        "availability": AVAILABILITY.get(_text(row.get("tinh_trang")).lower(), "preorder"),
        "condition": "new",
        "price": _money(listed),
        "link": link_template.replace("{ma_sp}", sku) if link_template else link,
        "image_link": urls[0] if urls else "",
        "additional_image_link": ",".join(urls[1:6]),
        "brand": brand,
        "mpn": sku,
        "sale_price": sale_price,
        "sale_price_effective_date": sale_window,
        "product_type": _text(row.get("loai")),
        # Carried through so a human reading the catalog in Commerce Manager can see how
        # stale the row is without opening the spreadsheet it came from.
        "custom_label_0": f"cap_nhat {_as_date(row.get('cap_nhat_ngay')) or ''}".strip(),
    }


def _title(row: dict) -> str:
    """Name, then size and colour -- but only the ones the name does not already carry.

    "Tủ lavabo BC52 80cm" plus a kich_thuoc of "80cm" reads as "Tủ lavabo BC52 80cm -
    80cm", and this is a customer-facing string in the Facebook catalogue, not an
    internal key.
    """
    name = _text(row.get("ten_sp"))
    parts = [name]
    for key in ("kich_thuoc", "mau"):
        value = "/".join(v.strip() for v in _text(row.get(key)).split(";") if v.strip())
        if value and value.lower() not in name.lower():
            parts.append(value)
    return " - ".join(p for p in parts if p)[:TITLE_MAX]


def _description(row: dict) -> str:
    """What the model reads when it talks about this product.

    gia_gom rides along deliberately: docs/13 §7 requires the agent to say what the price
    includes every time it quotes one, and the description is the only field that travels
    with the price into the catalogue.
    """
    bits = [
        _text(row.get("ghi_chu_tu_van")),
        f"Chất liệu: {v}" if (v := _text(row.get("chat_lieu"))) else "",
        f"Kích thước: {v}" if (v := _text(row.get("kich_thuoc"))) else "",
        f"Bảo hành: {v}" if (v := _text(row.get("bao_hanh"))) else "",
        f"Thời gian giao: {v}" if (v := _text(row.get("thoi_gian_giao"))) else "",
        f"Giá {v}" if (v := _text(row.get("gia_gom"))) else "",
    ]
    text = ". ".join(b.rstrip(". ") for b in bits if b)
    return (text or _text(row.get("ten_sp")))[:DESCRIPTION_MAX]


def _money(amount: int | None) -> str:
    return "" if amount is None else f"{amount} {CURRENCY}"


def _window(until: date, offset: str) -> str:
    """ISO8601 interval Meta reads as "the sale runs from now until end of that day"."""
    start = datetime.combine(date.today(), time(0, 0)).strftime("%Y-%m-%dT%H:%M")
    end = datetime.combine(until, time(23, 59)).strftime("%Y-%m-%dT%H:%M")
    return f"{start}{offset}/{end}{offset}"


def _join(base: str, name: str) -> str:
    return f"{base.rstrip('/')}/{name.lstrip('/')}"


def _utc_offset(name: str) -> str:
    delta = datetime.now(tz_zone(name)).utcoffset() or timedelta(0)
    minutes = int(delta.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    return f"{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"
